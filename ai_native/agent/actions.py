import re
from math import isfinite
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from typing_extensions import TypeAliasType

JsonValue = TypeAliasType(
    "JsonValue",
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)

_FORBIDDEN_ARGUMENT_KEY = re.compile(
    r"token|auth|authorization|cookie|raw(?:_|-)?(?:payload|dto)|series|values|emissionvolume|chartspec",
    re.IGNORECASE,
)


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["call_tool", "clarify", "finish"]
    tool_name: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=300)
    question: str | None = Field(default=None, max_length=1000)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("arguments", mode="before")
    @classmethod
    def reject_sensitive_argument_keys(
        cls, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        _validate_json_value(arguments)
        return arguments

    @model_validator(mode="after")
    def validate_variant(self) -> "AgentAction":
        if self.kind == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.kind == "clarify" and not self.question:
            raise ValueError("clarify requires question")
        if self.kind != "call_tool" and (self.tool_name or self.arguments):
            raise ValueError("only call_tool accepts tool fields")
        return self


class SafeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool_name: str
    status: Literal["success", "clarification_required", "failed"]
    facts: "SafeObservationFacts" = Field(default_factory=lambda: SafeObservationFacts())
    artifact_id: str | None = None
    result_count: int = Field(default=0, ge=0)
    error_code: str | None = None


class SafeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_id: str
    name: str


class SafeObservationFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    company_id: str | None = None
    company_name: str | None = None
    base_id: str | None = None
    base_name: str | None = None
    year: int | None = None
    scope: str | None = None
    period: str | None = None
    artifact_kind: str | None = None
    matched: bool | None = None
    validated: bool | None = None
    candidates: list[SafeCandidate] = Field(default_factory=list, max_length=20)
    data_points: int | None = Field(default=None, ge=0)


def _validate_json_value(value: object) -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise ValueError("argument keys must be strings")
            if _FORBIDDEN_ARGUMENT_KEY.search(key):
                raise ValueError(f"arguments may not contain sensitive key: {key}")
            _validate_json_value(nested_value)
    elif isinstance(value, list):
        for item in value:
            _validate_json_value(item)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("arguments must contain JSON-compatible values")
    elif isinstance(value, float) and not isfinite(value):
        raise ValueError("arguments may not contain non-finite numbers")
