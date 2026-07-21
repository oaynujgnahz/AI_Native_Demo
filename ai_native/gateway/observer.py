from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_native.agent.actions import SafeObservation


@dataclass(frozen=True)
class Artifact:
    """Deterministic output retained outside model-visible observations."""

    id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ExecutionResult:
    """A tool execution's safe metadata and its opaque deterministic output."""

    tool_name: str
    endpoint: str
    safe_facts: dict[str, Any]
    artifact: Artifact | None
    result_count: int
    audit_details: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def answer(self) -> str:
        """Compatibility view for the legacy one-shot service."""

        if self.artifact is None:
            return ""
        answer = self.artifact.payload.get("answer", "")
        return answer if isinstance(answer, str) else ""

    @property
    def chart(self) -> Any | None:
        """Compatibility view for the legacy chart response contract."""

        if self.artifact is None or self.artifact.kind != "chart":
            return None
        payload = self.artifact.payload.get("chart")
        if not isinstance(payload, dict):
            return None
        from ai_native.gateway.charts import ChartSpec

        return ChartSpec.model_validate(payload)

    def as_safe_dict(self) -> dict[str, Any]:
        artifact = None
        if self.artifact is not None:
            artifact = {"id": self.artifact.id, "kind": self.artifact.kind}
        return {
            "tool_name": self.tool_name,
            "endpoint": self.endpoint,
            "safe_facts": dict(self.safe_facts),
            "artifact": artifact,
            "result_count": self.result_count,
        }


class ObservationBuilder:
    def from_result(self, result: ExecutionResult) -> SafeObservation:
        facts = dict(result.safe_facts)
        if result.artifact is not None:
            facts["artifact_kind"] = result.artifact.kind
        return SafeObservation(
            tool_name=result.tool_name,
            status="success",
            facts=facts,
            artifact_id=result.artifact.id if result.artifact else None,
            result_count=result.result_count,
        )
