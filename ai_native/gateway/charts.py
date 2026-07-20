from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ChartSeries(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    values: List[float] = Field(max_length=100)


class ChartSource(BaseModel):
    tool_name: str = Field(min_length=1, max_length=80)
    company_id: str = Field(min_length=1, max_length=80)
    company_name: str = Field(min_length=1, max_length=200)
    period: str = Field(min_length=1, max_length=80)
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ChartSpec(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    chart_id: str = Field(default_factory=lambda: str(uuid4()))
    chart_type: Literal["pie", "line", "horizontal_bar", "grouped_bar"]
    title: str = Field(min_length=1, max_length=240)
    unit: Literal["t-CO₂e"] = "t-CO₂e"
    categories: List[str] = Field(max_length=100)
    series: List[ChartSeries] = Field(min_length=1, max_length=5)
    source: ChartSource

    @model_validator(mode="after")
    def validate_safe_shape(self) -> "ChartSpec":
        if not self.categories:
            raise ValueError("chart categories are required")
        if sum(len(series.values) for series in self.series) > 100:
            raise ValueError("chart total data points must not exceed 100")
        for category in self.categories:
            if not category or len(category) > 200:
                raise ValueError("invalid chart category")
        for series in self.series:
            if len(series.values) != len(self.categories):
                raise ValueError("series values must match categories")
            if not all(math.isfinite(value) for value in series.values):
                raise ValueError("chart values must be finite")
        return self
