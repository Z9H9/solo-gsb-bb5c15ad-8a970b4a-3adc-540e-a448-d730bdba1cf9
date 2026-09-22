"""Pydantic request/response models."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
