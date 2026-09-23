"""Irrigation plan generation.

Turns a daily watering schedule (local start times + durations + target
water amounts) into concrete execution batches across a date range,
resolving everything through the requested IANA timezone:

- durations are anchored in UTC, so a batch crossing midnight or a DST
  transition still lasts exactly the requested number of minutes;
- local times that never occur (spring-forward gap) are shifted forward to
  the first valid instant; local times that occur twice (fall-back) use the
  first occurrence — both are flagged via ``dst_note`` and ``warnings``;
- batches are sorted by start time and merged when the gap between them is
  at most one minute (merging also works across midnight);
- per-day water demand is checked against the device capacity and any
  shortfall is reported explicitly, never silently truncated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import (
    IrrigationBatch,
    IrrigationDayCheck,
    IrrigationPlanRequest,
    IrrigationPlanResult,
    IrrigationSource,
)

# Batches whose start is at most this far after the previous batch's end are
# merged into a single run.
MERGE_GAP = timedelta(minutes=1)


@dataclass
class _Batch:
    """One scheduled run before merging."""

    day: date  # local calendar date the session starts on
    session_index: int
    requested_start: str  # "HH:MM" as supplied by the user
    duration_minutes: int
    start_utc: datetime
    end_utc: datetime
    water_ml: float
    dst_note: str | None


def _resolve_local(
    naive: datetime, tz: ZoneInfo
) -> tuple[datetime, str | None]:
    """Attach ``tz`` to a naive local datetime, resolving DST edge cases.

    Returns ``(aware datetime, note)`` where note is ``None`` for ordinary
    times, ``"nonexistent"`` when the wall time is skipped by a
    spring-forward transition (shifted forward to the first valid instant),
    or ``"ambiguous"`` when the wall time occurs twice during a fall-back
    transition (the first occurrence is used).
    """
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, None
    # Round-trip through UTC: a wall time that does not survive the trip
    # never existed locally (the spring-forward gap).
    roundtrip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        return first, "nonexistent"
    return first, "ambiguous"


def _expand_daily_batches(
    req: IrrigationPlanRequest, tz: ZoneInfo, warnings: list[str]
) -> list[_Batch]:
    """Instantiate one batch per session per calendar day."""
    batches: list[_Batch] = []

    day = req.start_date
    while day <= req.end_date:
        for idx, sess in enumerate(req.sessions):
            naive = datetime.combine(day, sess.start_time)
            start_local, note = _resolve_local(naive, tz)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = start_utc + timedelta(minutes=sess.duration_minutes)
            # Wall-clock fields of ``start_local`` still echo the requested
            # time even for a spring-forward gap; the real execution time is
            # obtained by converting back from UTC.
            actual_local = start_utc.astimezone(tz)

            dst_note = None
            hhmm = sess.start_time.strftime("%H:%M")
            if note == "nonexistent":
                dst_note = "nonexistent_shifted_forward"
                warnings.append(
                    f"{day} {hhmm} 在 {req.timezone} 不存在（夏令时拨快跳过），"
                    f"已顺延至 {actual_local:%H:%M} 执行"
                )
            elif note == "ambiguous":
                dst_note = "ambiguous_first_occurrence"
                warnings.append(
                    f"{day} {hhmm} 在 {req.timezone} 出现两次（夏令时拨回重复），"
                    f"已按第一次出现（UTC{actual_local:%z}）执行"
                )

            batches.append(
                _Batch(
                    day=day,
                    session_index=idx,
                    requested_start=hhmm,
                    duration_minutes=sess.duration_minutes,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    water_ml=sess.water_ml,
                    dst_note=dst_note,
                )
            )
        day += timedelta(days=1)

    return batches


def _merge_batches(batches: list[_Batch]) -> list[dict]:
    """Sort by start time and glue batches whose gap is <= 1 minute.

    A later batch may start exactly at the previous end (gap 0), one minute
    later (gap 1), or even overlap (e.g. sessions straddling midnight) — all
    of these are merged. The merged water amount is the sum of its sources.
    """
    ordered = sorted(
        batches, key=lambda b: (b.start_utc, b.day, b.session_index)
    )
    merged: list[dict] = []
    for batch in ordered:
        if merged and batch.start_utc <= merged[-1]["end_utc"] + MERGE_GAP:
            current = merged[-1]
            current["end_utc"] = max(current["end_utc"], batch.end_utc)
            current["water_ml"] += batch.water_ml
            current["sources"].append(batch)
        else:
            merged.append(
                {
                    "start_utc": batch.start_utc,
                    "end_utc": batch.end_utc,
                    "water_ml": batch.water_ml,
                    "sources": [batch],
                }
            )
    return merged


def _check_daily_capacity(
    req: IrrigationPlanRequest, warnings: list[str]
) -> tuple[list[IrrigationDayCheck], bool]:
    """Per-day capacity validation. Demand is attributed by local start date."""
    demand = sum(s.water_ml for s in req.sessions)
    capacity = req.daily_capacity_ml

    checks: list[IrrigationDayCheck] = []
    capacity_ok = True
    day = req.start_date
    while day <= req.end_date:
        shortfall = max(0.0, demand - capacity)
        within = shortfall <= 0.0
        if not within:
            capacity_ok = False
            warnings.append(
                f"{day} 需水 {demand:g} mL 超出设备每日容量 {capacity:g} mL，"
                f"缺口 {shortfall:g} mL（不截断批次，请调整计划或容量）"
            )
        checks.append(
            IrrigationDayCheck(
                date=day,
                sessions=len(req.sessions),
                demand_ml=round(demand, 3),
                capacity_ml=capacity,
                remaining_ml=round(max(0.0, capacity - demand), 3),
                shortfall_ml=round(shortfall, 3),
                within_capacity=within,
            )
        )
        day += timedelta(days=1)

    return checks, capacity_ok


def build_plan(req: IrrigationPlanRequest) -> IrrigationPlanResult:
    """Expand ``req`` into timezone-aware batches and capacity validation."""
    tz = ZoneInfo(req.timezone)
    warnings: list[str] = []

    raw_batches = _expand_daily_batches(req, tz, warnings)
    merged = _merge_batches(raw_batches)

    batch_models: list[IrrigationBatch] = []
    for group in merged:
        start_local = group["start_utc"].astimezone(tz)
        end_local = group["end_utc"].astimezone(tz)
        duration = int(
            (group["end_utc"] - group["start_utc"]).total_seconds() // 60
        )
        batch_models.append(
            IrrigationBatch(
                start_utc=group["start_utc"],
                end_utc=group["end_utc"],
                start_local=start_local,
                end_local=end_local,
                duration_minutes=duration,
                water_ml=round(group["water_ml"], 3),
                merged_sessions=len(group["sources"]),
                sources=[
                    IrrigationSource(
                        date=src.day,
                        start_time=src.requested_start,
                        duration_minutes=src.duration_minutes,
                        water_ml=src.water_ml,
                        dst_note=src.dst_note,
                    )
                    for src in group["sources"]
                ],
            )
        )

    days, capacity_ok = _check_daily_capacity(req, warnings)

    return IrrigationPlanResult(
        timezone=req.timezone,
        start_date=req.start_date,
        end_date=req.end_date,
        batches_before_merge=len(raw_batches),
        batches_after_merge=len(merged),
        capacity_ok=capacity_ok,
        days=days,
        batches=batch_models,
        warnings=warnings,
    )
