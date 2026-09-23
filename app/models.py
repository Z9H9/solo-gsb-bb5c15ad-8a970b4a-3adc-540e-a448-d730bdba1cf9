"""Pydantic request/response models."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import DEFAULT_TOLERANCE, ELEMENTS, normalize_element


class Concentrate(BaseModel):
    """One stock concentrate bottle.

    Concentrations are in mg of the *element* per L of concentrate
    (water-soluble N/P/K, elemental for micros). ``available_volume`` and
    ``max_volume`` are in mL.
    """

    name: str = Field(..., min_length=1, examples=["A液-钙肥"])
    available_volume: float = Field(..., gt=0, description="可用体积 (mL)")
    max_volume: Optional[float] = Field(
        None,
        gt=0,
        description="本配方中单液使用量上限 (mL)，缺省取可用体积",
    )
    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="各元素浓度 mg/L，键如 N/P/K/Ca/Mg/Fe ...",
    )

    # Reject misspelled keys (e.g. "concentrates" inside a concentrate)
    # instead of silently dropping them.
    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_concentrations(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class Targets(BaseModel):
    """Target nutrient concentrations of the final solution (mg/L)."""

    concentrations: dict[str, float]

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_targets(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 目标不能为负")
            normalized[elem] = float(val)
        return normalized


class SolveRequest(BaseModel):
    target_volume_ml: float = Field(..., gt=0, description="目标配方总体积 (mL)")
    targets: Targets
    concentrates: list[Concentrate] = Field(..., min_length=1)
    min_water_ml: float = Field(
        0,
        ge=0,
        description="稀释水最低用量 (mL)，用于强制保留稀释空间",
    )
    max_concentrate_fraction: float = Field(
        1.0,
        gt=0,
        le=1.0,
        description="浓缩液合计占总体积的最大比例 (0~1]",
    )
    tolerance: float = Field(
        DEFAULT_TOLERANCE,
        ge=0,
        le=1,
        description="可接受的最大相对偏差；超过则在 violations 中列出",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrates")
    @classmethod
    def _unique_names(cls, v: list[Concentrate]) -> list[Concentrate]:
        names = [c.name for c in v]
        if len(names) != len(set(names)):
            raise ValueError("浓缩液名称不能重复")
        return v


class ElementResult(BaseModel):
    element: str
    target_concentration: float
    actual_concentration: float
    target_amount_mg: float
    actual_amount_mg: float
    relative_deviation: Optional[float]
    status: str  # ok | under | over | unconstrained


class Violation(BaseModel):
    code: str
    message: str
    element: Optional[str] = None
    limit: Optional[float] = None
    actual: Optional[float] = None


class SolveResult(BaseModel):
    feasible: bool
    optimal: bool
    max_relative_deviation: Optional[float]
    total_volume_ml: float
    water_volume_ml: float
    concentrate_total_ml: float
    allocations: list[dict[str, float | str]]
    elements: list[ElementResult]
    violations: list[Violation] = []
    message: str = ""


class SaveRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    note: str = ""
    request: SolveRequest

    model_config = ConfigDict(extra="forbid")


class RecipeSummary(BaseModel):
    id: int
    name: str
    description: str
    created_at: str
    latest_version: int


class VersionSummary(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str


class RecipeVersion(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str
    request: dict
    result: dict


# --------------------------------------------------------------------------- #
# Irrigation planning
# --------------------------------------------------------------------------- #

# An irrigation plan may cover at most this many calendar days (inclusive).
MAX_PLAN_RANGE_DAYS = 366


class IrrigationSession(BaseModel):
    """One daily irrigation session, specified in local wall-clock time."""

    start_time: time = Field(..., description="本地开始时间 HH:MM，如 23:30")
    duration_minutes: int = Field(
        ..., gt=0, le=24 * 60, description="持续分钟数（1~1440）"
    )
    water_ml: float = Field(..., gt=0, description="本次目标水量 (mL)")

    model_config = ConfigDict(extra="forbid")

    @field_validator("start_time")
    @classmethod
    def _validate_start_time(cls, v: time) -> time:
        # Only a naive HH:MM wall-clock time is meaningful here: seconds make
        # the generated batch disagree with the displayed start time, and an
        # offset would be silently dropped when combining with the date.
        if v.tzinfo is not None:
            raise ValueError("start_time 不能带时区偏移，需为本地时间 HH:MM")
        if v.second != 0 or v.microsecond != 0:
            raise ValueError("start_time 只支持到分钟，格式 HH:MM")
        return v


class IrrigationPlanRequest(BaseModel):
    """Request body for ``POST /api/irrigation/plan``."""

    timezone: str = Field(
        ..., description="IANA 时区名，如 Asia/Shanghai、America/New_York、UTC"
    )
    start_date: date = Field(..., description="起始日期（含），YYYY-MM-DD")
    end_date: date = Field(..., description="结束日期（含），YYYY-MM-DD")
    sessions: list[IrrigationSession] = Field(
        ..., min_length=1, description="每日若干灌溉时段"
    )
    daily_capacity_ml: float = Field(
        ..., ge=0, description="设备每日供水容量 (mL)；超出时报告缺口而非截断"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"非法时区 '{v}'：请使用 IANA 时区名（如 Asia/Shanghai、UTC）"
            ) from exc
        return v

    @model_validator(mode="after")
    def _validate_date_range(self) -> "IrrigationPlanRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        if (self.end_date - self.start_date).days > MAX_PLAN_RANGE_DAYS:
            raise ValueError(
                f"日期范围最多覆盖 {MAX_PLAN_RANGE_DAYS + 1} 天"
            )
        return self


class IrrigationSource(BaseModel):
    """One original scheduled session contributing to a batch."""

    date: date
    start_time: str = Field(..., description="请求的本地开始时间 HH:MM")
    duration_minutes: int
    water_ml: float
    dst_note: Optional[str] = Field(
        None,
        description=(
            "夏令时处理标记："
            "nonexistent_shifted_forward（本地时间不存在，已顺延）或 "
            "ambiguous_first_occurrence（本地时间重复，取第一次出现）"
        ),
    )


class IrrigationBatch(BaseModel):
    """One executable irrigation batch (possibly several merged sessions)."""

    start_utc: datetime = Field(..., description="UTC 开始时间")
    end_utc: datetime = Field(..., description="UTC 结束时间")
    start_local: datetime = Field(..., description="本地开始时间（含时区偏移）")
    end_local: datetime = Field(..., description="本地结束时间（含时区偏移，可跨午夜）")
    duration_minutes: int = Field(
        ..., description="实际持续分钟数（按 UTC 起止计算）"
    )
    water_ml: float = Field(..., description="合并后目标水量 (mL)")
    merged_sessions: int = Field(..., description="合并进来的原始时段数")
    sources: list[IrrigationSource]


class IrrigationDayCheck(BaseModel):
    """Per-day capacity validation."""

    date: date
    sessions: int = Field(..., description="当日时段数（合并前）")
    demand_ml: float = Field(..., description="当日需水总量 (mL)")
    capacity_ml: float = Field(..., description="设备每日容量 (mL)")
    remaining_ml: float = Field(..., description="容量剩余 (mL)，容量不足时为 0")
    shortfall_ml: float = Field(..., description="容量缺口 (mL)，无缺口时为 0")
    within_capacity: bool


class IrrigationPlanResult(BaseModel):
    """Irrigation plan response."""

    timezone: str
    start_date: date
    end_date: date
    batches_before_merge: int = Field(..., description="合并前批次数")
    batches_after_merge: int = Field(..., description="合并后批次数")
    capacity_ok: bool = Field(..., description="所有日期均未超出每日容量")
    days: list[IrrigationDayCheck]
    batches: list[IrrigationBatch]
    warnings: list[str] = Field(
        default_factory=list,
        description="夏令时调整、容量缺口等提示信息",
    )
