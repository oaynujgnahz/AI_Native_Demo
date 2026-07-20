from __future__ import annotations

from typing import List, Literal, Optional

from typing_extensions import NotRequired, TypedDict

from ai_native.agent.actions import AgentAction, SafeObservation
from ai_native.agent.budgets import RunCounters


class AgentState(TypedDict):
    """Checkpoint-safe state for the controlled agent loop."""

    run_id: NotRequired[str]
    conversation_id: NotRequired[str]
    user_id: NotRequired[str]
    tenant_id: NotRequired[str]
    company_id: NotRequired[str]
    allowed_company_ids: NotRequired[List[str]]
    year: NotRequired[int]
    locale: NotRequired[str]
    goal: NotRequired[str]
    actions: NotRequired[List[AgentAction]]
    observations: NotRequired[List[SafeObservation]]
    artifact_ids: NotRequired[List[str]]
    counters: NotRequired[RunCounters]
    policy_status: NotRequired[
        Literal["approved", "clarification_required", "denied"]
    ]
    approval_id: NotRequired[Optional[str]]
    approved_tool_name: NotRequired[Optional[str]]
    approved_arguments_json: NotRequired[str]
    approval_signature: NotRequired[Optional[str]]
    pending_question: NotRequired[str]
    missing_fields: NotRequired[List[str]]
    stop_reason: NotRequired[str]
    error_code: NotRequired[str]
    error_category: NotRequired[str]
