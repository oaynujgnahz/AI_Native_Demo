from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from typing_extensions import NotRequired, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[List[Any], add_messages]
    run_id: NotRequired[str]
    conversation_id: NotRequired[str]
    company_id: NotRequired[Optional[str]]
    allowed_company_ids: NotRequired[List[str]]
    year: NotRequired[Optional[int]]
    locale: NotRequired[Optional[str]]
    goal: NotRequired[str]
    intent: NotRequired[Optional[str]]
    actions: NotRequired[List[Dict[str, Any]]]
    observations: NotRequired[List[Dict[str, Any]]]
    artifact_references: NotRequired[List[Dict[str, Any]]]
    counters: NotRequired[Dict[str, Any]]
    pending_question: NotRequired[Optional[str]]
    missing_fields: NotRequired[List[str]]
    stop_reason: NotRequired[Optional[str]]
    error_code: NotRequired[Optional[str]]
    tool_name: NotRequired[Optional[str]]
    tool_arguments: NotRequired[Dict[str, Any]]
    tool_results: NotRequired[Dict[str, Any]]
    direct_answer: NotRequired[Optional[str]]
    permissions: NotRequired[List[str]]
    user_id: NotRequired[str]
    tenant_id: NotRequired[str]
