from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from typing_extensions import NotRequired, TypedDict

from langgraph.graph.message import add_messages

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
    pending_question: NotRequired[str]
    missing_fields: NotRequired[List[str]]
    stop_reason: NotRequired[str]
    error_code: NotRequired[str]


class LegacyAgentState(TypedDict):
    """Non-checkpointed state for the one-shot graph; removed in Task 6."""

    messages: Annotated[List[Any], add_messages]
    company_id: NotRequired[Optional[str]]
    year: NotRequired[Optional[int]]
    intent: NotRequired[Optional[str]]
    tool_name: NotRequired[Optional[str]]
    tool_arguments: NotRequired[Dict[str, Any]]
    tool_results: NotRequired[Dict[str, Any]]
    direct_answer: NotRequired[Optional[str]]
    permissions: NotRequired[List[str]]
    user_id: NotRequired[str]
    tenant_id: NotRequired[str]
    auth_token: NotRequired[Optional[str]]
