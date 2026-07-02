from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from typing_extensions import NotRequired, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[List[Any], add_messages]
    company_id: NotRequired[Optional[str]]
    year: NotRequired[Optional[int]]
    intent: NotRequired[Optional[str]]
    tool_name: NotRequired[Optional[str]]
    tool_results: NotRequired[Dict[str, Any]]
    permissions: NotRequired[List[str]]
    user_id: NotRequired[str]
    tenant_id: NotRequired[str]
