"""FastAPI application: hydroponic nutrient mixing solver."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import database
from .irrigation import build_plan
from .models import (
    IrrigationPlanRequest,
    IrrigationPlanResult,
    RecipeSummary,
    RecipeVersion,
    SaveRecipeRequest,
    SolveRequest,
    SolveResult,
    VersionSummary,
)
from .solver import solve


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize / migrate SQLite on startup.
    database.init_db()
    yield


app = FastAPI(
    title="水培营养液配比求解 API",
    description=(
        "根据多种浓缩液的元素浓度、可用体积与稀释水约束，"
        "最小化各元素相对目标配方的最大偏差（minimax LP）；"
        "并提供按 IANA 时区展开的灌溉计划接口（跨午夜 / 夏令时安全）。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/solve", response_model=SolveResult, tags=["solver"])
def trial_solve(req: SolveRequest) -> SolveResult:
    """试算：不入库，返回配比、各元素偏差及不可行原因。"""
    return solve(req)


@app.post(
    "/api/irrigation/plan",
    response_model=IrrigationPlanResult,
    tags=["irrigation"],
)
def irrigation_plan(req: IrrigationPlanRequest) -> IrrigationPlanResult:
    """灌溉计划：按 IANA 时区生成逐日执行批次。

    - 时长锚定 UTC，跨午夜 / 跨夏令时切换的批次仍精确持续指定分钟数；
    - 春季拨快导致本地时间不存在时顺延到首个有效时刻，秋季拨回导致本地
      时间重复时取第一次出现，均在 warnings 中说明；
    - 按开始时间排序，合并相邻且间隔不超过 1 分钟（含重叠）的批次；
    - 按本地开始日期核算每日需水与设备容量，容量不足时返回明确缺口，
      不截断任何批次。
    """
    return build_plan(req)


@app.post(
    "/api/recipes",
    response_model=RecipeVersion,
    status_code=201,
    tags=["recipes"],
)
def save_recipe(
    payload: SaveRecipeRequest,
    force: bool = Query(
        False, description="为 true 时允许保存超出容差（不可行）的结果"
    ),
) -> RecipeVersion:
    """保存配方版本：同名配方自动追加新版本号，否则新建配方。"""
    result = solve(payload.request)
    if not result.feasible and not force:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "求解结果超出容差，未保存；可先调参，"
                "或带 ?force=true 强制保存。",
                "result": json.loads(result.model_dump_json()),
            },
        )

    recipe = database.ensure_recipe(payload.name, payload.description)
    row = database.create_version(
        recipe_id=recipe["id"],
        note=payload.note,
        request_payload=json.loads(payload.request.model_dump_json()),
        result_payload=json.loads(result.model_dump_json()),
    )
    return RecipeVersion(**database.serialize_version(row))


@app.get("/api/recipes", response_model=list[RecipeSummary], tags=["recipes"])
def list_recipes() -> list[RecipeSummary]:
    return [
        RecipeSummary(**database.serialize_recipe(r))
        for r in database.list_recipes()
    ]


@app.get("/api/recipes/{recipe_id}", response_model=RecipeSummary, tags=["recipes"])
def get_recipe(recipe_id: int) -> RecipeSummary:
    row = database.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404, "配方不存在")
    return RecipeSummary(**database.serialize_recipe(row))


@app.get(
    "/api/recipes/{recipe_id}/versions",
    response_model=list[VersionSummary],
    tags=["recipes"],
)
def list_versions(recipe_id: int) -> list[VersionSummary]:
    if database.get_recipe(recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    rows = database.list_versions(recipe_id)
    return [
        VersionSummary(
            id=r["id"],
            recipe_id=r["recipe_id"],
            version=r["version"],
            note=r["note"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get(
    "/api/recipes/{recipe_id}/versions/{version}",
    response_model=RecipeVersion,
    tags=["recipes"],
)
def get_version(recipe_id: int, version: int) -> RecipeVersion:
    if database.get_recipe(recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")
    return RecipeVersion(**database.serialize_version(row))


@app.get(
    "/api/recipes/{recipe_id}/versions/{version}/export",
    tags=["recipes"],
    summary="导出单个配方版本为 JSON 文件",
)
def export_version(recipe_id: int, version: int) -> JSONResponse:
    """以 attachment 方式下载该版本（含请求参数与求解结果）的 JSON。"""
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    payload = {
        "exported_at": database._now(),  # noqa: SLF001
        "recipe": database.serialize_recipe(recipe),
        "version": database.serialize_version(row),
    }
    # HTTP headers are latin-1: provide an ASCII fallback name plus an
    # RFC 5987 UTF-8 filename for Chinese recipe names.
    ascii_name = f"recipe-{recipe['id']}-v{version}.json"
    utf8_name = quote(f"recipe-{recipe['name']}-v{version}.json")
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{utf8_name}"
    )
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": disposition,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
