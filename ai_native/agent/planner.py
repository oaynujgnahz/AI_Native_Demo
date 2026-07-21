from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Protocol, Sequence

from pydantic import ValidationError

from ai_native.agent.actions import AgentAction, SafeObservation
from ai_native.agent.llm import CMPF_ROUTING_GUIDANCE
from ai_native.gateway.errors import GatewayAgentError

logger = logging.getLogger(__name__)

_ACTION_TOOL_NAME = "submit_agent_action"
_SAFE_CONTEXT_FIELDS = frozenset({"company_id", "year", "locale"})
_SAFE_REMAINING_FIELDS = frozenset({"planner", "tools", "clarifications"})
_BEARER_VALUE = re.compile(r"\bBearer\s+[^\s\"']+", re.IGNORECASE)
_SITE_WORDS = ("拠点", "据点", "site", "base")
_COMPARE_WORDS = ("比較", "比べ", "比较", "对比", "compare", "versus", " vs ")
_MULTI_STEP_WORDS = _SITE_WORDS + _COMPARE_WORDS
_ANNUAL_WORDS = ("年間", "年度", "年次", "年总", "年度总量", "annual", "yearly")
_EMISSION_WORDS = ("排出", "排放", "emission", "ghg", "co2", "co₂")
_NON_ANNUAL_WORDS = (
    "月別",
    "月度",
    "每月",
    "monthly",
    "trend",
    "推移",
    "趋势",
    "内訳",
    "明细",
    "breakdown",
    "構成",
    "占比",
    "composition",
    "top",
    "上位",
    "排名",
)


class AgentPlanner(Protocol):
    def plan(
        self,
        *,
        goal: str,
        trusted_context: Mapping[str, Any],
        observations: Sequence[SafeObservation | Mapping[str, Any]],
        artifact_summaries: Sequence[Mapping[str, Any]],
        remaining: Mapping[str, Any],
    ) -> AgentAction: ...


class OpenAIActionPlanner:
    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    @classmethod
    def from_env(cls) -> "OpenAIActionPlanner | None":
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            logger.info("Action planner disabled: provider API key is not set")
            return None
        from openai import OpenAI

        client_kwargs: dict[str, str] = {"api_key": api_key}
        base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        return cls(
            OpenAI(**client_kwargs),
            os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )

    def plan(
        self,
        *,
        goal: str,
        trusted_context: Mapping[str, Any],
        observations: Sequence[SafeObservation | Mapping[str, Any]],
        artifact_summaries: Sequence[Mapping[str, Any]],
        remaining: Mapping[str, Any],
    ) -> AgentAction:
        safe_observations = [
            _redact_value(SafeObservation.model_validate(item).model_dump(mode="json"))
            for item in observations
        ]
        safe_artifacts = _safe_artifact_summaries(artifact_summaries)
        safe_context = _safe_mapping(trusted_context, _SAFE_CONTEXT_FIELDS)
        safe_remaining = _safe_mapping(remaining, _SAFE_REMAINING_FIELDS)
        safe_goal = _redact_text(goal)

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _planner_instructions()},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "goal": safe_goal,
                                "trusted_context": safe_context,
                                "observations": safe_observations,
                                "artifacts": safe_artifacts,
                                "remaining": safe_remaining,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                tools=[_action_tool_schema()],
                tool_choice={
                    "type": "function",
                    "function": {"name": _ACTION_TOOL_NAME},
                },
            )
        except Exception as exc:
            fallback = _deterministic_fallback(
                safe_goal,
                safe_context,
                safe_observations,
                safe_artifacts,
            )
            if fallback is not None:
                logger.warning("Model provider unavailable; using single-tool fallback")
                return fallback
            raise GatewayAgentError(
                category="model",
                code="model_unavailable",
                retryable=True,
            ) from exc

        try:
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if len(tool_calls) != 1:
                raise ValueError("planner must return exactly one action")
            function = tool_calls[0].function
            if function.name != _ACTION_TOOL_NAME:
                raise ValueError("planner returned an unexpected tool")
            raw_arguments = function.arguments
            if not isinstance(raw_arguments, str):
                raise TypeError("planner action arguments must be JSON text")
            return AgentAction.model_validate_json(raw_arguments)
        except (AttributeError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise GatewayAgentError(
                category="model",
                code="model_invalid_action",
                retryable=True,
            ) from exc


def _planner_instructions() -> str:
    return (
        "You are the planner in a policy-gated CMPF carbon-emission agent loop. "
        "Submit exactly one structured action for this iteration. Do not answer with "
        "business data. Use only trusted context and safe observations supplied by "
        "the gateway. A finish action may reference existing artifact IDs only. "
        "For a site-specific analysis or site comparison without resolved base IDs in "
        "the observations, first call list_analysis_bases to resolve the written names; "
        "after resolution, call the requested analysis tool in a later iteration. "
        f"{CMPF_ROUTING_GUIDANCE}"
    )


def _action_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _ACTION_TOOL_NAME,
            "description": "Submit one validated action for the controlled agent loop.",
            "parameters": AgentAction.model_json_schema(),
        },
    }


def _safe_mapping(
    values: Mapping[str, Any], allowed_fields: frozenset[str]
) -> dict[str, Any]:
    return {
        key: _redact_value(value)
        for key, value in values.items()
        if key in allowed_fields and isinstance(value, (str, int, float, bool, type(None)))
    }


def _safe_artifact_summaries(
    artifact_summaries: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for item in artifact_summaries:
        artifact_id = item.get("id")
        kind = item.get("kind")
        if isinstance(artifact_id, str) and isinstance(kind, str):
            summaries.append(
                {"id": _redact_text(artifact_id), "kind": _redact_text(kind)}
            )
    return summaries


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _redact_text(value: str) -> str:
    return _BEARER_VALUE.sub("[REDACTED]", value)


def _deterministic_fallback(
    goal: str,
    trusted_context: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    artifact_summaries: Sequence[Mapping[str, str]],
) -> AgentAction | None:
    if observations or artifact_summaries:
        return None
    text = goal.casefold()
    if any(word in text for word in _MULTI_STEP_WORDS):
        return None
    if any(word in text for word in _NON_ANNUAL_WORDS):
        return None
    is_annual = any(word in text for word in _ANNUAL_WORDS) or bool(
        re.search(r"(?<!\d)20\d{2}年", text)
    )
    if not is_annual or not any(word in text for word in _EMISSION_WORDS):
        return None
    arguments = {
        key: trusted_context[key]
        for key in ("company_id", "year")
        if trusted_context.get(key) is not None
    }
    return AgentAction(
        kind="call_tool",
        tool_name="get_annual_emission_summary",
        arguments=arguments,
        reason="high-confidence annual summary fallback",
    )
