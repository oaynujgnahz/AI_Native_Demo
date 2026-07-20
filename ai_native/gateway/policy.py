from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_native.agent.actions import AgentAction, JsonValue
from ai_native.agent.budgets import RunCounters, canonical_tool_signature
from ai_native.agent.state import AgentState
from ai_native.gateway.errors import GatewayAgentError
from ai_native.gateway.runtime_context import RuntimeContext
from ai_native.gateway.tooling import ToolCatalog


class PolicyDecision(BaseModel):
    """Serializable policy output containing no request-scoped capabilities."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["approved", "clarification_required", "denied"]
    error_code: str | None = None
    approval_id: str | None = None
    tool_name: str | None = None
    validated_arguments: dict[str, JsonValue] = Field(default_factory=dict)
    signature: str | None = None
    question: str | None = None
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_variant(self) -> "PolicyDecision":
        if self.status == "approved" and not self.approval_id:
            raise ValueError("approved decisions require an approval ID")
        if self.status == "denied" and not self.error_code:
            raise ValueError("denied decisions require an error code")
        return self


class PolicyEngine:
    def __init__(
        self,
        catalog: ToolCatalog,
        *,
        approval_id_factory: Callable[[], object] = uuid4,
    ) -> None:
        self._catalog = catalog
        self._approval_id_factory = approval_id_factory

    def evaluate(
        self,
        action: AgentAction,
        state: AgentState,
        context: RuntimeContext,
    ) -> PolicyDecision:
        # Security boundary: keep this order stable and do not reserve budget until
        # every rejection check has passed.
        if context.is_cancelled():
            return self._denied("cancelled", category="cancel")
        if self._deadline_reached(context.deadline):
            return self._denied("request_timeout", category="budget")

        run_id = state.get("run_id")
        if action.kind == "clarify":
            counters = state.get("counters")
            if not isinstance(counters, RunCounters):
                return self._denied("budget_state_missing", category="budget")
            if counters.clarification_calls >= counters.budgets.clarifications:
                return self._denied(
                    "clarification_budget_exhausted", category="budget"
                )
            if not run_id or not self._run_is_active(context.repository, run_id):
                return self._denied("run_inactive", category="conflict")
            counters.consume_clarification()
            return PolicyDecision(
                status="clarification_required",
                question=action.question,
                missing_fields=list(action.missing_fields),
            )
        if action.kind == "finish":
            if not run_id or not self._run_is_active(context.repository, run_id):
                return self._denied("run_inactive", category="conflict")
            return self._approved()

        tool_name = action.tool_name
        if tool_name is None or tool_name not in self._catalog.names():
            return self._denied("tool_not_allowed")
        definition = self._catalog.get(tool_name)

        target_company_id = action.arguments.get("company_id")
        if target_company_id is None:
            target_company_id = state.get("company_id")
        if target_company_id is None:
            target_company_id = context.principal.company_id
        allowed_company_ids = {
            str(company_id) for company_id in state.get("allowed_company_ids", [])
        }
        if not allowed_company_ids:
            allowed_company_ids.add(str(context.principal.company_id))
        if str(target_company_id) not in allowed_company_ids:
            return self._denied("company_forbidden", category="authorization")

        try:
            validated = definition.argument_model.model_validate(action.arguments)
        except ValidationError:
            return self._denied("invalid_tool_arguments", category="validation")
        validated_arguments = validated.model_dump(mode="python", exclude_none=True)

        signature = canonical_tool_signature(tool_name, validated_arguments)
        counters = state.get("counters")
        if not isinstance(counters, RunCounters):
            return self._denied("budget_state_missing", category="budget")
        if signature in counters.tool_signatures:
            return self._denied("duplicate_tool_call", category="budget")
        if counters.tool_calls >= counters.budgets.tools:
            return self._denied("tool_budget_exhausted", category="budget")

        if not run_id or not self._run_is_active(context.repository, run_id):
            return self._denied("run_inactive", category="conflict")

        counters.consume_tool(signature)
        return self._approved(
            tool_name=tool_name,
            validated_arguments=validated_arguments,
            signature=signature,
        )

    def _approved(
        self,
        *,
        tool_name: str | None = None,
        validated_arguments: Mapping[str, JsonValue] | None = None,
        signature: str | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            status="approved",
            approval_id=str(self._approval_id_factory()),
            tool_name=tool_name,
            validated_arguments=dict(validated_arguments or {}),
            signature=signature,
        )

    @staticmethod
    def _denied(code: str, *, category: str = "policy") -> PolicyDecision:
        error = GatewayAgentError(category=category, code=code)
        return PolicyDecision(status="denied", error_code=error.code)

    @staticmethod
    def _deadline_reached(deadline: datetime) -> bool:
        if deadline.tzinfo is None:
            return datetime.now() >= deadline
        return datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc)

    @staticmethod
    def _run_is_active(repository: object, run_id: str) -> bool:
        get_run = getattr(repository, "get_run", None)
        if not callable(get_run):
            return False
        run = get_run(run_id)
        if run is None:
            return False
        if isinstance(run, Mapping):
            return run.get("status") == "running"
        return getattr(run, "status", None) == "running"
