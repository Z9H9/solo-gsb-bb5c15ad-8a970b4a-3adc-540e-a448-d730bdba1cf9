"""Smoke test: exercises the solver + API end to end via TestClient.

The A/B concentrate numbers are constructed so that the exact target mix
for 10 L is A = 1500 mL + B = 125 mL + 8375 mL water (16.25% concentrate):

    N: 1500 mL * 1200 mg/L / 1000 = 1800 mg -> 180 mg/L
    P:  125 mL * 4000 mg/L / 1000 =  500 mg ->  50 mg/L
    K: 1500 * 400/1000 + 125 * 2000/1000
                                 =  850 mg ->  85 mg/L
"""
import json
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def base_request() -> dict:
    return {
        "target_volume_ml": 10000,
        "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
        "concentrates": [
            {"name": "A液", "available_volume": 2000,
             "concentrations": {"N": 1200, "K": 400}},
            {"name": "B液", "available_volume": 2000,
             "concentrations": {"P": 4000, "K": 2000}},
        ],
        "max_concentrate_fraction": 0.20,
        "min_water_ml": 7000,
        "tolerance": 0.05,
    }


with TestClient(app) as client:
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # 1) feasible trial --------------------------------------------------- #
    req = base_request()
    r = client.post("/api/solve", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    assert data["water_volume_ml"] >= 7000
    assert data["concentrate_total_ml"] <= 2000 + 1e-6
    assert abs(
        sum(a["volume_ml"] for a in data["allocations"])
        + data["water_volume_ml"] - 10000
    ) < 1e-4
    for el in data["elements"]:
        assert el["status"] == "ok", el
    alloc = {a["concentrate"]: a["volume_ml"] for a in data["allocations"]}
    assert abs(alloc["A液"] - 1500) < 1e-4, alloc
    assert abs(alloc["B液"] - 125) < 1e-4, alloc
    assert abs(data["water_volume_ml"] - 8375) < 1e-4
    print("1) feasible allocations:", alloc,
          "water:", data["water_volume_ml"])

    # 2) infeasible: A bottle nearly empty -> N shortage ------------------ #
    bad = base_request()
    bad["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/solve", json=bad)
    data = r.json()
    assert data["feasible"] is False, data
    nv = [v for v in data["violations"] if v["element"] == "N"]
    assert nv and nv[0]["code"] == "ELEMENT_SHORTAGE", data["violations"]
    # 50 mL * 1.2 mg/mL = 60 mg max vs target 1800 mg
    assert abs(nv[0]["actual"] - 60.0) < 1e-6, nv
    print("2) shortage:", nv[0]["message"])

    # 3) hard volume conflict: 5% concentrate cap implies at most 9500 mL
    # water, but the user demands >= 9600 mL water ----------------------- #
    bad2 = base_request()
    bad2["min_water_ml"] = 9600
    bad2["max_concentrate_fraction"] = 0.05
    data = client.post("/api/solve", json=bad2).json()
    assert data["feasible"] is False
    assert data["violations"][0]["code"] == "CONCENTRATE_FRACTION_CONFLICT"
    print("3) conflict:", data["violations"][0]["message"])

    # 4) water min exceeds total ------------------------------------------ #
    bad3 = base_request()
    bad3["min_water_ml"] = 12000
    data = client.post("/api/solve", json=bad3).json()
    assert data["violations"][0]["code"] == "WATER_MIN_EXCEEDS_VOLUME"
    print("4) water > total:", data["violations"][0]["message"])

    # 5) per-bottle max_volume binds -> N shortage ------------------------ #
    bad4 = base_request()
    bad4["concentrates"][0]["max_volume"] = 5
    data = client.post("/api/solve", json=bad4).json()
    assert data["feasible"] is False
    assert any(v["code"] == "ELEMENT_SHORTAGE" and v["element"] == "N"
               for v in data["violations"]), data["violations"]
    print("5) max_volume cap binds")

    # 6) misspelled inner key is rejected, not silently ignored ----------- #
    bad5 = base_request()
    bad5["concentrates"][1]["concentrates"] = bad5["concentrates"][1].pop(
        "concentrations")
    r = client.post("/api/solve", json=bad5)
    assert r.status_code == 422, r.text
    print("6) typo rejected with 422")

    # 7) unknown element rejected ----------------------------------------- #
    bad6 = base_request()
    bad6["targets"]["concentrations"]["Xx"] = 1
    assert client.post("/api/solve", json=bad6).status_code == 422

    # 7b) element keys are case-insensitive (ca/CA/fe all accepted) ------- #
    ci = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"ca": 50, "FE": 2.5}},
        "concentrates": [
            {"name": "C", "available_volume": 100,
             "concentrations": {"CA": 25000}},
            {"name": "E", "available_volume": 100,
             "concentrations": {"fe": 1250}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    r = client.post("/api/solve", json=ci)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    got = {e["element"]: e["status"] for e in data["elements"]}
    assert got == {"Ca": "ok", "Fe": "ok"}, got

    # 8) duplicate concentrate names rejected ----------------------------- #
    bad7 = base_request()
    bad7["concentrates"][1]["name"] = "A液"
    assert client.post("/api/solve", json=bad7).status_code == 422

    # 9) save feasible recipe --------------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "description": "test", "note": "v1",
        "request": base_request()})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1 and v1["result"]["feasible"]

    # 10) second save -> version 2 ---------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "note": "v2", "request": base_request()})
    assert r.status_code == 201 and r.json()["version"] == 2

    # 11) infeasible save rejected, then forced --------------------------- #
    r = client.post("/api/recipes", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 422
    r = client.post("/api/recipes?force=true", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 201 and r.json()["result"]["feasible"] is False

    # 12) list / get / versions / export ---------------------------------- #
    names = [x["name"] for x in client.get("/api/recipes").json()]
    assert "叶菜通用配方" in names and len(names) == 2
    rid = v1["recipe_id"]
    versions = client.get(f"/api/recipes/{rid}/versions").json()
    assert versions[0]["version"] == 2
    assert client.get(f"/api/recipes/{rid}/versions/1").json()["version"] == 1
    r = client.get(f"/api/recipes/{rid}/versions/1/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["recipe"]["name"] == "叶菜通用配方"
    assert payload["version"]["result"]["feasible"] is True

    assert client.get("/api/recipes/999").status_code == 404
    assert client.get(f"/api/recipes/{rid}/versions/99").status_code == 404

    # 13) element surplus: N and P coupled 1:1, P target far too low ------ #
    surp_req = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 200, "P": 1}},
        "concentrates": [
            {"name": "BNP", "available_volume": 100,
             "concentrations": {"N": 100000, "P": 100000}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    data = client.post("/api/solve", json=surp_req).json()
    assert data["feasible"] is False, data
    assert any(v["code"] == "ELEMENT_SURPLUS" and v["element"] == "P"
               for v in data["violations"]), data["violations"]
    print("13) surplus:",
          [v["message"] for v in data["violations"]
           if v["code"] == "ELEMENT_SURPLUS"][0])

    # 14) micronutrients: coupled Fe/N ratio exactly matches target ------- #
    # 1 L, N=100 mg/L (100 mg), Fe=2 mg/L (2 mg). Use 2 mL of a mother
    # liquor with N=50000 mg/L and Fe=1000 mg/L -> N 100 mg, Fe 2 mg.
    micro = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/solve", json=micro)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    fe = [e for e in data["elements"] if e["element"] == "Fe"][0]
    assert fe["status"] == "ok"
    assert abs(data["allocations"][0]["volume_ml"] - 2.0) < 1e-4
    print("14) micro mix ok:", [(e["element"], e["status"])
                                for e in data["elements"]])

    # 15) element present in a concentrate but not targeted is reported as
    #     "unconstrained" (actual concentration still shown) -------------- #
    unc = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             # B comes along for the ride but has no target
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    # B is untargeted: no row for it, because targets drive element rows;
    # it only matters if the user lists it. Confirm targeted rows are ok
    # and B does not block feasibility.
    assert {e["element"] for e in data["elements"]} == {"N", "Fe"}
    # zero target element -> unconstrained row
    unc["targets"] = {"concentrations": {"N": 100, "Fe": 2, "B": 0}}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    b_row = [e for e in data["elements"] if e["element"] == "B"][0]
    assert b_row["status"] == "unconstrained"
    assert b_row["relative_deviation"] is None
    print("15) untargeted/zero-target handling ok")

    # 16) irrigation: UTC conversion + batch crossing midnight ------------- #
    irrigation_base = {
        "timezone": "Asia/Shanghai",
        "start_date": "2026-09-23",
        "end_date": "2026-09-24",
        "sessions": [
            {"start_time": "06:00", "duration_minutes": 30, "water_ml": 200},
            {"start_time": "23:30", "duration_minutes": 90, "water_ml": 300},
        ],
        "daily_capacity_ml": 1000,
    }
    r = client.post("/api/irrigation/plan", json=irrigation_base)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["batches_before_merge"] == 4
    assert plan["batches_after_merge"] == 4
    assert plan["capacity_ok"] is True
    assert len(plan["days"]) == 2
    assert plan["days"][0]["demand_ml"] == 500
    assert plan["days"][0]["remaining_ml"] == 500
    night = [b for b in plan["batches"]
             if b["start_local"] == "2026-09-23T23:30:00+08:00"][0]
    # 23:30 CST == 15:30 UTC; 90-minute run ends 01:00 next local day
    assert night["start_utc"] == "2026-09-23T15:30:00Z", night
    assert night["end_local"] == "2026-09-24T01:00:00+08:00", night
    assert night["end_utc"] == "2026-09-23T17:00:00Z", night
    assert night["duration_minutes"] == 90
    assert night["merged_sessions"] == 1
    print("16) irrigation UTC conversion + midnight crossing ok")

    # 17) merge: gap of 1 minute merges; gap of 19 minutes stays separate - #
    merge_req = {
        "timezone": "UTC",
        "start_date": "2026-09-23",
        "end_date": "2026-09-23",
        "sessions": [
            {"start_time": "06:00", "duration_minutes": 30, "water_ml": 100},
            {"start_time": "06:31", "duration_minutes": 10, "water_ml": 50},
            {"start_time": "07:00", "duration_minutes": 10, "water_ml": 25},
        ],
        "daily_capacity_ml": 1000,
    }
    plan = client.post("/api/irrigation/plan", json=merge_req).json()
    assert plan["batches_before_merge"] == 3
    assert plan["batches_after_merge"] == 2, plan
    merged, late = plan["batches"]
    assert merged["merged_sessions"] == 2
    assert merged["water_ml"] == 150
    assert merged["start_local"] == "2026-09-23T06:00:00Z"
    assert merged["end_local"] == "2026-09-23T06:41:00Z"
    assert merged["duration_minutes"] == 41
    assert late["water_ml"] == 25
    # gap of exactly 2 minutes must NOT merge
    merge_req["sessions"][1]["start_time"] = "06:32"
    plan = client.post("/api/irrigation/plan", json=merge_req).json()
    assert plan["batches_after_merge"] == 3, plan
    print("17) irrigation merge threshold ok")

    # 18) merge across midnight (two-day range; 23:50 run overlaps the next
    #     day's 00:05 run) ------------------------------------------------- #
    cross_req = {
        "timezone": "UTC",
        "start_date": "2026-09-23",
        "end_date": "2026-09-24",
        "sessions": [
            {"start_time": "00:05", "duration_minutes": 10, "water_ml": 80},
            {"start_time": "23:50", "duration_minutes": 20, "water_ml": 100},
        ],
        "daily_capacity_ml": 1000,
    }
    plan = client.post("/api/irrigation/plan", json=cross_req).json()
    assert plan["batches_before_merge"] == 4
    assert plan["batches_after_merge"] == 3, plan
    span = [b for b in plan["batches"] if b["merged_sessions"] == 2][0]
    assert span["start_local"] == "2026-09-23T23:50:00Z", span
    assert span["end_local"] == "2026-09-24T00:15:00Z", span
    assert span["duration_minutes"] == 25
    assert span["water_ml"] == 180
    dates = {s["date"] for s in span["sources"]}
    assert dates == {"2026-09-23", "2026-09-24"}, span
    print("18) irrigation cross-midnight merge ok")

    # 19) capacity shortfall is explicit and never truncates batches ------ #
    cap_req = {
        "timezone": "UTC",
        "start_date": "2026-09-23",
        "end_date": "2026-09-23",
        "sessions": [
            {"start_time": "06:00", "duration_minutes": 30, "water_ml": 200},
            {"start_time": "18:00", "duration_minutes": 90, "water_ml": 300},
        ],
        "daily_capacity_ml": 400,
    }
    plan = client.post("/api/irrigation/plan", json=cap_req).json()
    assert plan["capacity_ok"] is False
    day0 = plan["days"][0]
    assert day0["demand_ml"] == 500
    assert day0["shortfall_ml"] == 100
    assert day0["remaining_ml"] == 0
    assert day0["within_capacity"] is False
    # batches are not truncated: full 500 mL is still scheduled
    assert sum(b["water_ml"] for b in plan["batches"]) == 500
    assert any("缺口" in w for w in plan["warnings"]), plan["warnings"]
    print("19) irrigation capacity shortfall reported without truncation")

    # 20) DST spring forward: nonexistent 02:30 shifted to 03:30 ---------- #
    gap_req = {
        "timezone": "America/New_York",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "sessions": [
            {"start_time": "02:30", "duration_minutes": 30, "water_ml": 100},
        ],
        "daily_capacity_ml": 1000,
    }
    plan = client.post("/api/irrigation/plan", json=gap_req).json()
    b = plan["batches"][0]
    # 02:30 does not exist (EST -> EDT skips the hour); folds to 03:30 EDT
    assert b["start_local"] == "2026-03-08T03:30:00-04:00", b
    assert b["start_utc"] == "2026-03-08T07:30:00Z", b
    assert b["sources"][0]["dst_note"] == "nonexistent_shifted_forward"
    assert any("不存在" in w and "03:30" in w for w in plan["warnings"]), plan["warnings"]

    # 21) DST fall back: ambiguous 01:30 uses the first occurrence (EDT) --- #
    amb_req = {
        "timezone": "America/New_York",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "sessions": [
            {"start_time": "01:30", "duration_minutes": 30, "water_ml": 100},
        ],
        "daily_capacity_ml": 1000,
    }
    plan = client.post("/api/irrigation/plan", json=amb_req).json()
    b = plan["batches"][0]
    assert b["start_local"] == "2026-11-01T01:30:00-04:00", b
    assert b["start_utc"] == "2026-11-01T05:30:00Z", b
    assert b["sources"][0]["dst_note"] == "ambiguous_first_occurrence"
    assert any("出现两次" in w and "-0400" in w
               for w in plan["warnings"]), plan["warnings"]
    print("20/21) DST gap and ambiguity resolved ok")

    # 22) batch spanning the spring-forward transition keeps UTC duration - #
    span_req = {
        "timezone": "America/New_York",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "sessions": [
            {"start_time": "01:30", "duration_minutes": 90, "water_ml": 100},
        ],
        "daily_capacity_ml": 1000,
    }
    plan = client.post("/api/irrigation/plan", json=span_req).json()
    b = plan["batches"][0]
    assert b["start_local"] == "2026-03-08T01:30:00-05:00", b
    assert b["end_local"] == "2026-03-08T04:00:00-04:00", b
    assert b["duration_minutes"] == 90, b
    print("22) duration stays exact across DST transition ok")

    # 23) invalid input is rejected with 422 ------------------------------- #
    for bad in [
        {"timezone": "GMT+8"},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
        {"sessions": [
            {"start_time": "25:00", "duration_minutes": 30, "water_ml": 100}]},
        {"sessions": [
            {"start_time": "23:30:30", "duration_minutes": 30, "water_ml": 100}]},
        {"sessions": [
            {"start_time": "23:30+08:00", "duration_minutes": 30, "water_ml": 100}]},
        {"sessions": [
            {"start_time": "06:00", "duration_minutes": -5, "water_ml": 100}]},
        {"sessions": [
            {"start_time": "06:00", "duration_minutes": 30, "water_ml": -1}]},
        {"daily_capacity_ml": -100},
        {"start_date": "2026-09-25", "end_date": "2026-09-23"},
        {"sessions": []},
        {"unknown_field": 1},
    ]:
        payload = {**cap_req, **bad}
        assert client.post("/api/irrigation/plan", json=payload).status_code == 422, bad
    print("23) irrigation validation rejects bad input with 422")

print("\nALL SMOKE TESTS PASSED")
