from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["call_tool", "clarify", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=300)
    question: str | None = Field(default=None, max_length=1000)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)

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
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: Literal["success", "clarification_required", "failed"]
    facts: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = None
    result_count: int = Field(default=0, ge=0)
    error_code: str | None = None
