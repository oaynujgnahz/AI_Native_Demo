from __future__ import annotations

import logging
import re
from typing import Any, Dict, Literal, Optional

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph

from ai_native.agent.state import AgentState
from ai_native.agent.llm import OpenAIToolPlanner
from ai_native.gateway.cmpf_client import CmpfGateway
from ai_native.gateway.context import BusinessContext
from ai_native.gateway.registry import ToolRegistry

logger = logging.getLogger(__name__)


def build_graph(
    gateway: Optional[CmpfGateway] = None,
    planner: Optional[Any] = None,
    use_env_planner: bool = True,
):
    cmpf_gateway = gateway or CmpfGateway()
    registry = ToolRegistry(cmpf_gateway)
    tool_planner = planner if planner is not None else (
        OpenAIToolPlanner.from_env() if use_env_planner else None
    )
    logger.info(
        "Graph initialized: gateway_mode=%s planner=%s",
        cmpf_gateway.mode,
        type(tool_planner).__name__ if tool_planner else "rule-fallback",
    )

    graph_builder = StateGraph(AgentState)
    graph_builder.add_node("plan", _build_plan_node(registry, tool_planner))
    graph_builder.add_node("business_tool", _build_tool_node(registry))
    graph_builder.add_node("answer", _answer)

    graph_builder.add_edge(START, "plan")
    graph_builder.add_conditional_edges(
        "plan",
        _route_after_plan,
        {
            "tool": "business_tool",
            "answer": "answer",
        },
    )
    graph_builder.add_edge("business_tool", "answer")
    graph_builder.add_edge("answer", END)
    return graph_builder.compile()


def _build_plan_node(registry: ToolRegistry, planner: Optional[Any]):
    def plan_node(state: AgentState) -> Dict[str, Any]:
        user_text = _last_user_text(state)
        year = state.get("year") or _extract_year(user_text)
        company_id = state.get("company_id") or _extract_company_id(user_text)
        logger.info(
            "Planning input: user_text=%r extracted_company_id=%s extracted_year=%s",
            user_text,
            company_id,
            year,
        )

        if planner is not None:
            try:
                decision = planner.plan(user_text, registry)
            except Exception as exc:
                logger.warning(
                    "LLM planning failed, falling back to rules: %s",
                    exc,
                    exc_info=True,
                )
                decision = None
            if decision and decision.tool_name:
                arguments = dict(decision.arguments)
                arguments.setdefault("company_id", company_id or "cmpf-demo")
                if decision.tool_name != "get_company_info":
                    arguments.setdefault("year", int(year or 2025))
                logger.info(
                    "Planning selected by LLM: tool=%s arguments=%s",
                    decision.tool_name,
                    arguments,
                )
                return {
                    "intent": "tool",
                    "tool_name": decision.tool_name,
                    "tool_arguments": arguments,
                    "company_id": arguments.get("company_id", company_id),
                    "year": arguments.get("year", year),
                }
            if decision and decision.direct_answer:
                logger.info("Planning selected direct LLM answer")
                return {
                    "intent": "chat",
                    "direct_answer": decision.direct_answer,
                    "company_id": company_id,
                    "year": year,
                }

        tool_name = _select_tool(user_text)
        intent = "tool" if tool_name else "chat"
        logger.info(
            "Planning selected by rules: intent=%s tool=%s arguments=%s",
            intent,
            tool_name,
            _default_tool_arguments(tool_name, company_id, year),
        )
        return {
            "intent": intent,
            "tool_name": tool_name,
            "tool_arguments": _default_tool_arguments(tool_name, company_id, year),
            "company_id": company_id,
            "year": year,
        }

    return plan_node


def _route_after_plan(state: AgentState) -> Literal["tool", "answer"]:
    if state.get("intent") == "tool":
        return "tool"
    return "answer"


def _build_tool_node(registry: ToolRegistry):
    def tool_node(state: AgentState) -> Dict[str, Any]:
        tool_name = state.get("tool_name") or "get_emission_dashboard"
        company_id = state.get("company_id") or "cmpf-demo"
        context = BusinessContext(
            user_id=state.get("user_id", "local-user"),
            tenant_id=state.get("tenant_id", "local"),
            company_id=company_id,
            permissions=state.get("permissions", ["cmpf:read"]),
            auth_token=state.get("auth_token"),
        )
        arguments = state.get("tool_arguments") or _default_tool_arguments(
            tool_name,
            company_id,
            state.get("year"),
        )
        logger.info("Tool node executing: tool=%s arguments=%s", tool_name, arguments)
        result = registry.execute(tool_name, arguments, context)
        if not result.allowed:
            logger.warning(
                "Tool node denied: tool=%s error_code=%s",
                tool_name,
                result.error_code,
            )
            return {"tool_results": {tool_name: {"error_code": result.error_code}}}
        logger.info("Tool node completed: tool=%s result_keys=%s", tool_name, list((result.data or {}).keys()))
        return {"tool_results": {tool_name: result.data}}

    return tool_node


def _answer(state: AgentState) -> Dict[str, Any]:
    tool_name = state.get("tool_name") or "get_emission_dashboard"
    result = state.get("tool_results", {}).get(tool_name)
    if result:
        if result.get("error_code") == "permission_denied":
            answer = "当前用户没有调用 CMPF 读取工具的权限，需要 `cmpf:read` 权限。"
        elif tool_name == "get_scope_breakdown":
            answer = _format_scope_breakdown_answer(result)
        elif tool_name == "get_company_info":
            answer = _format_company_info_answer(result)
        else:
            answer = _format_dashboard_answer(result)
    else:
        answer = state.get("direct_answer") or "我现在已经可以接入 CMPF 业务工具。请告诉我要查询的公司和年度，例如：查 cmpf-demo 公司 2025 年碳排放情况。"
    logger.info("Answer generated: tool=%s length=%s", tool_name, len(answer))
    return {"messages": [AIMessage(content=answer)]}


def _default_tool_arguments(
    tool_name: Optional[str],
    company_id: Optional[str],
    year: Optional[int],
) -> Dict[str, Any]:
    normalized_company_id = company_id or "cmpf-demo"
    if tool_name == "get_company_info":
        return {"company_id": normalized_company_id}
    return {"company_id": normalized_company_id, "year": int(year or 2025)}


def _format_dashboard_answer(result: Dict[str, Any]) -> str:
    if "body" in result:
        return (
            "已从 CMPF dashBoard/scope_total_emission_volume 接口取得结果。\n"
            f"原始返回：{result}"
        )

    company_id = result.get("company_id", "unknown")
    year = result.get("year", "unknown")
    scope1 = result.get("scope1_tco2e", 0)
    scope2 = result.get("scope2_tco2e", 0)
    scope3 = result.get("scope3_tco2e", 0)
    total = result.get("total_tco2e", 0)
    source = result.get("source", "cmpf")
    return (
        f"{company_id} 公司 {year} 年碳排放情况如下（数据源：{source}）：\n"
        f"- Scope1：{scope1} tCO2e\n"
        f"- Scope2：{scope2} tCO2e\n"
        f"- Scope3：{scope3} tCO2e\n"
        f"- 总排放：{total} tCO2e"
    )


def _format_scope_breakdown_answer(result: Dict[str, Any]) -> str:
    if "body" in result:
        return (
            "已从 CMPF dashBoard/scope_emission_volume 接口取得 Scope 明细。\n"
            f"原始返回：{result}"
        )

    company_id = result.get("company_id", "unknown")
    year = result.get("year", "unknown")
    scopes = result.get("scopes", [])
    lines = [
        f"{company_id} 公司 {year} 年 Scope 明细如下（数据源：{result.get('source', 'cmpf')}）："
    ]
    for item in scopes:
        lines.append(
            f"- {item.get('scope')}：{item.get('emission_tco2e')} tCO2e，占比 {item.get('share')}"
        )
    return "\n".join(lines)


def _format_company_info_answer(result: Dict[str, Any]) -> str:
    if "body" in result:
        return (
            "已从 CMPF dashBoard/scope_emission_volume 接口取得公司信息。\n"
            f"原始返回：{result}"
        )
    company_info = result.get("company_info", [])
    company_id = result.get("company_id", "unknown")
    lines = [
        f"{company_id} 公司信息如下（数据源：{result.get('source', 'cmpf')}）："
    ]
    for item in company_info:
        lines.append(
            "- "
            f"公司名：{item.get('company_name', '-')}; "
            f"地址：{item.get('company_address', '-')}; "
            f"电话：{item.get('company_phone', '-')}; "
            f"邮箱：{item.get('company_email', '-')}"
        )
    return "\n".join(lines)

def _last_user_text(state: AgentState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, BaseMessage):
            if message.type == "human":
                return str(message.content)
        elif isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _looks_like_emission_question(text: str) -> bool:
    keywords = ("碳排放", "排放", "emission", "Scope", "scope", "CO2", "CO₂", "公司", "企業", "企业")
    return any(keyword in text for keyword in keywords)


def _select_tool(text: str) -> Optional[str]:
    if not _looks_like_emission_question(text):
        return None
    if any(keyword in text for keyword in ("明细", "内訳", "breakdown", "Scope", "scope")):
        return "get_scope_breakdown"
    if any(keyword in text for keyword in ("碳排放", "排放", "emission", "CO2", "CO₂")):
        return "get_emission_dashboard"
    elif any(keyword in text for keyword in ("公司", "企業", "企业")):
        return "get_company_info"
    return "get_emission_dashboard"


def _extract_year(text: str) -> Optional[int]:
    match = re.search(r"(20\d{2})", text)
    if not match:
        return None
    return int(match.group(1))


def _extract_company_id(text: str) -> Optional[str]:
    match = re.search(r"([A-Za-z][A-Za-z0-9_-]{2,})\s*(?:公司|企業|企业)?", text)
    if match:
        return match.group(1)
    return None
