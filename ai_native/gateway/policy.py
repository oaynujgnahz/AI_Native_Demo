from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ai_native.agent.actions import AgentAction, JsonValue
from ai_native.agent.budgets import RunCounters, canonical_tool_signature
from ai_native.agent.state import AgentState
from ai_native.gateway.errors import GatewayAgentError
from ai_native.gateway.runtime_context import RuntimeContext
from ai_native.gateway.tooling import ToolCatalog


_FORBIDDEN_DECISION_KEY = re.compile(
    r"token|auth|authorization|cookie|raw(?:_|-)?(?:payload|dto)|series|values|emissionvolume|chartspec",
    re.IGNORECASE,
)


class PolicyDecision(BaseModel):
    """Serializable policy output containing no request-scoped capabilities."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["approved", "clarification_required", "denied"]
    error_code: str | None = None
    approval_id: str | None = None
    tool_name: str | None = None
    validated_arguments_json: str = "{}"
    signature: str | None = None
    question: str | None = None
    missing_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_variant(self) -> "PolicyDecision":
        _decode_arguments_json(self.validated_arguments_json)
        if self.status == "approved" and not self.approval_id:
            raise ValueError("approved decisions require an approval ID")
        if self.status == "denied" and not self.error_code:
            raise ValueError("denied decisions require an error code")
        return self

    @property
    def validated_arguments(self) -> dict[str, JsonValue]:
        """Return a fresh decoded copy; signed storage remains immutable."""

        return _decode_arguments_json(self.validated_arguments_json)


class PolicyEngine:
    def __init__(
        self,
        catalog: ToolCatalog,
        *,
        approval_signing_key: bytes | None = None,
    ) -> None:
        if approval_signing_key is not None and (
            not isinstance(approval_signing_key, bytes) or not approval_signing_key
        ):
            raise ValueError("approval signing key must be non-empty bytes")
        self._catalog = catalog
        self.__approval_signing_key = approval_signing_key or secrets.token_bytes(32)

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
            if not run_id or not self._run_is_active(
                context.repository, run_id, state, context
            ):
                return self._denied("active_run_conflict", category="conflict")
            counters.consume_clarification()
            return PolicyDecision(
                status="clarification_required",
                question=action.question,
                missing_fields=tuple(action.missing_fields),
            )
        if action.kind == "finish":
            if not run_id or not self._run_is_active(
                context.repository, run_id, state, context
            ):
                return self._denied("active_run_conflict", category="conflict")
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

        if not run_id or not self._run_is_active(
            context.repository, run_id, state, context
        ):
            return self._denied("active_run_conflict", category="conflict")

        counters.consume_tool(signature)
        return self._approved(
            tool_name=tool_name,
            validated_arguments=validated_arguments,
            signature=signature,
        )

    def verify_approval(self, decision: PolicyDecision) -> bool:
        if decision.status != "approved" or not decision.approval_id:
            return False
        try:
            arguments = _decode_arguments_json(decision.validated_arguments_json)
            canonical_json = _canonical_arguments_json(arguments)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if canonical_json != decision.validated_arguments_json:
            return False
        expected = self._sign_approval(
            decision.tool_name,
            decision.signature,
            decision.validated_arguments_json,
        )
        return hmac.compare_digest(decision.approval_id, expected)

    def _approved(
        self,
        *,
        tool_name: str | None = None,
        validated_arguments: Mapping[str, JsonValue] | None = None,
        signature: str | None = None,
    ) -> PolicyDecision:
        arguments_json = _canonical_arguments_json(validated_arguments or {})
        return PolicyDecision(
            status="approved",
            approval_id=self._sign_approval(tool_name, signature, arguments_json),
            tool_name=tool_name,
            validated_arguments_json=arguments_json,
            signature=signature,
        )

    def _sign_approval(
        self,
        tool_name: str | None,
        signature: str | None,
        arguments_json: str,
    ) -> str:
        payload = json.dumps(
            [tool_name, signature, arguments_json],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(
            self.__approval_signing_key,
            payload,
            hashlib.sha256,
        ).hexdigest()

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
    def _run_is_active(
        repository: object,
        run_id: str,
        state: AgentState,
        context: RuntimeContext,
    ) -> bool:
        get_run = getattr(repository, "get_run", None)
        if not callable(get_run):
            return False
        run = get_run(run_id)
        if run is None:
            return False
        value = run.get if isinstance(run, Mapping) else lambda key: getattr(run, key, None)
        state_company_id = state.get("company_id")
        conversation_id = state.get("conversation_id")
        principal = context.principal
        if not state_company_id or not conversation_id:
            return False
        return (
            value("status") == "running"
            and value("user_id") == principal.user_id
            and value("company_id") == state_company_id
            and value("company_id") == principal.company_id
            and value("conversation_id") == conversation_id
        )


def _canonical_arguments_json(arguments: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _decode_arguments_json(arguments_json: str) -> dict[str, JsonValue]:
    parsed = json.loads(arguments_json, parse_constant=_reject_json_constant)
    if not isinstance(parsed, dict):
        raise ValueError("validated arguments JSON must contain an object")
    _reject_sensitive_keys(parsed)
    canonical = _canonical_arguments_json(parsed)
    if canonical != arguments_json:
        raise ValueError("validated arguments JSON must be canonical")
    return parsed


def _reject_sensitive_keys(value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _FORBIDDEN_DECISION_KEY.search(key):
                raise ValueError(f"validated arguments contain sensitive key: {key}")
            _reject_sensitive_keys(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_keys(item)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
