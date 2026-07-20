"""Compatibility facade backed by the policy-gated agent runtime.

The historical one-shot LangGraph implementation was removed in Task 6.  This
module keeps ``build_graph().invoke(...)`` available for callers that still use
the original demo contract; Task 8 migrates those callers to ``AgentRuntime``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage

from ai_native.agent.actions import AgentAction, SafeObservation
from ai_native.agent.llm import OpenAIToolPlanner
from ai_native.agent.runtime import build_agent_runtime
from ai_native.gateway.auth import Principal
from ai_native.gateway.context import BusinessContext
from ai_native.gateway.errors import GatewayAgentError
from ai_native.gateway.observer import Artifact, ExecutionResult
from ai_native.gateway.policy import PolicyEngine
from ai_native.gateway.registry import ToolRegistry
from ai_native.gateway.runtime_context import RuntimeContext
from ai_native.gateway.tooling import build_enterprise_catalog


class _CompatibilityPlanner:
    def __init__(self, planner: Any, registry: ToolRegistry) -> None:
        self.planner = planner
        self.registry = registry
        self.direct_answer: str | None = None

    def plan(
        self,
        *,
        goal: str,
        trusted_context: Mapping[str, Any],
        observations: Sequence[SafeObservation | Mapping[str, Any]],
        artifact_summaries: Sequence[Mapping[str, Any]],
        remaining: Mapping[str, Any],
    ) -> AgentAction:
        del remaining
        if artifact_summaries:
            return AgentAction(
                kind="finish",
                artifact_ids=[str(artifact_summaries[-1]["id"])],
            )
        decision = None
        if self.planner is not None:
            try:
                decision = self.planner.plan(goal, self.registry)
            except Exception:
                decision = None
        company_id = str(trusted_context.get("company_id") or "cmpf-demo")
        year = int(trusted_context.get("year") or _extract_year(goal) or 2025)
        if decision is not None and decision.direct_answer and not decision.tool_name:
            self.direct_answer = decision.direct_answer
            return AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": company_id},
            )
        selected = decision.tool_name if decision is not None else None
        arguments = dict(decision.arguments) if selected else {}
        if selected == "get_emission_dashboard":
            selected = "get_annual_emission_summary"
        if selected not in {
            "get_annual_emission_summary",
            "get_scope_breakdown",
            "get_company_info",
        }:
            selected = _select_compatibility_tool(goal)
        if selected is None:
            self.direct_answer = (
                "我现在已经可以接入 CMPF 业务工具。请告诉我要查询的公司和年度，"
                "例如：查 cmpf-demo 公司 2025 年碳排放情况。"
            )
            selected = "get_company_info"
        arguments.setdefault("company_id", company_id)
        if selected != "get_company_info":
            arguments.setdefault("year", year)
        return AgentAction(
            kind="call_tool",
            tool_name=selected,
            arguments=arguments,
            reason="legacy compatibility routing",
        )


class _CompatibilityExecutor:
    def __init__(self, planner: _CompatibilityPlanner, registry: ToolRegistry) -> None:
        self.planner = planner
        self.registry = registry

    def execute(self, **kwargs: Any) -> ExecutionResult:
        tool_name = str(kwargs["tool_name"])
        arguments = dict(kwargs["arguments"])
        direct_answer = self.planner.direct_answer
        if direct_answer is not None:
            self.planner.direct_answer = None
            return _answer_result(tool_name, arguments, direct_answer)
        registry_tool = (
            "get_emission_dashboard"
            if tool_name == "get_annual_emission_summary"
            else tool_name
        )
        principal = kwargs["principal"]
        result = self.registry.execute(
            registry_tool,
            arguments,
            BusinessContext(
                user_id=principal.user_id,
                tenant_id=principal.role_id,
                company_id=str(arguments.get("company_id") or principal.company_id),
                permissions=["cmpf:read"],
                auth_token=kwargs["bearer_token"],
            ),
        )
        if not result.allowed:
            raise GatewayAgentError("policy", result.error_code or "tool_denied")
        data = result.data or {}
        if registry_tool == "get_scope_breakdown":
            answer = _format_scope_breakdown_answer(data)
        elif registry_tool == "get_company_info":
            answer = _format_company_info_answer(data)
        else:
            answer = _format_dashboard_answer(data)
        return _answer_result(tool_name, arguments, answer)


class _CompatibilityRepository:
    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    def get_run(self, run_id: str):
        return SimpleNamespace(
            id=run_id,
            status="running",
            user_id=self.principal.user_id,
            company_id=self.principal.company_id,
            conversation_id=run_id,
        )

    def write_audit(self, entry: Mapping[str, Any]) -> None:
        del entry


class _CompatibilityGraph:
    def __init__(self, gateway: Any, planner: Any) -> None:
        self.gateway = gateway
        self.planner = planner

    def invoke(self, state: Mapping[str, Any]) -> dict[str, Any]:
        goal = _last_user_text(state)
        company_id = str(state.get("company_id") or _extract_company_id(goal) or "cmpf-demo")
        principal = Principal(
            subject=str(state.get("user_id") or "local-user"),
            user_id=str(state.get("user_id") or "local-user"),
            company_id=company_id,
            role_id=str(state.get("tenant_id") or "local"),
        )
        repository = _CompatibilityRepository(principal)
        registry = ToolRegistry(self.gateway)
        planner = _CompatibilityPlanner(self.planner, registry)
        catalog = build_enterprise_catalog()
        runtime = build_agent_runtime(
            planner=planner,
            policy_engine=PolicyEngine(catalog),
            executor=_CompatibilityExecutor(planner, registry),
        )
        result = runtime.invoke(
            RuntimeContext(
                principal=principal,
                bearer_token=str(state.get("auth_token") or ""),
                deadline=datetime.now(timezone.utc) + timedelta(seconds=45),
                repository=repository,
                is_cancelled=lambda: False,
            ),
            goal,
            company_id=company_id,
            allowed_company_ids=[company_id],
            year=state.get("year") or _extract_year(goal),
        )
        if result.status != "completed":
            raise GatewayAgentError("runtime", result.error_code or result.status)
        messages = list(state.get("messages", []))
        messages.append(AIMessage(content=result.answer))
        return {**dict(state), "messages": messages}


def build_graph(gateway=None, planner=None, use_env_planner: bool = True):
    from ai_native.gateway.cmpf_client import CmpfGateway

    selected_gateway = gateway or CmpfGateway()
    selected_planner = planner
    if selected_planner is None and use_env_planner:
        selected_planner = OpenAIToolPlanner.from_env()
    return _CompatibilityGraph(selected_gateway, selected_planner)


def _answer_result(
    tool_name: str, arguments: Mapping[str, Any], answer: str
) -> ExecutionResult:
    artifact_id = str(uuid4())
    return ExecutionResult(
        tool_name=tool_name,
        endpoint=f"compatibility:{tool_name}",
        safe_facts={
            "company_id": str(arguments.get("company_id") or "cmpf-demo"),
            **({"year": arguments["year"]} if "year" in arguments else {}),
        },
        artifact=Artifact(
            id=artifact_id,
            kind="answer",
            payload={"answer": answer},
        ),
        result_count=1,
    )


def _last_user_text(state: Mapping[str, Any]) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, BaseMessage) and message.type == "human":
            return str(message.content)
        if isinstance(message, Mapping) and message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _select_compatibility_tool(text: str) -> str | None:
    lowered = text.casefold()
    looks_business = any(
        word in lowered
        for word in ("碳排放", "排放", "emission", "scope", "co2", "co₂", "公司", "企業", "企业")
    )
    if not looks_business:
        return None
    if any(word in lowered for word in ("明细", "内訳", "breakdown", "scope")):
        return "get_scope_breakdown"
    if any(word in lowered for word in ("会社情報", "公司信息", "company info", "住所", "address")):
        return "get_company_info"
    return "get_annual_emission_summary"


def _extract_year(text: str) -> int | None:
    match = re.search(r"(20\d{2})", text)
    return int(match.group(1)) if match else None


def _extract_company_id(text: str) -> str | None:
    match = re.search(r"([A-Za-z][A-Za-z0-9_-]{2,})\s*(?:公司|企業|企业)?", text)
    return match.group(1) if match else None


def _format_dashboard_answer(result: Mapping[str, Any]) -> str:
    if "body" in result:
        return "已从 CMPF dashBoard/scope_total_emission_volume 接口取得结果。\n" f"原始返回：{dict(result)}"
    company_id = result.get("company_id", "unknown")
    year = result.get("year", "unknown")
    return (
        f"{company_id} 公司 {year} 年碳排放情况如下（数据源：{result.get('source', 'cmpf')}）：\n"
        f"- Scope1：{result.get('scope1_tco2e', 0)} tCO2e\n"
        f"- Scope2：{result.get('scope2_tco2e', 0)} tCO2e\n"
        f"- Scope3：{result.get('scope3_tco2e', 0)} tCO2e\n"
        f"- 总排放：{result.get('total_tco2e', 0)} tCO2e"
    )


def _format_scope_breakdown_answer(result: Mapping[str, Any]) -> str:
    if "body" in result:
        return "已从 CMPF dashBoard/scope_emission_volume 接口取得 Scope 明细。\n" f"原始返回：{dict(result)}"
    lines = [
        f"{result.get('company_id', 'unknown')} 公司 {result.get('year', 'unknown')} 年 Scope 明细如下（数据源：{result.get('source', 'cmpf')}）："
    ]
    for item in result.get("scopes", []):
        lines.append(
            f"- {item.get('scope')}：{item.get('emission_tco2e')} tCO2e，占比 {item.get('share')}"
        )
    return "\n".join(lines)


def _format_company_info_answer(result: Mapping[str, Any]) -> str:
    if "body" in result:
        return "已从 CMPF 接口取得公司信息。\n" f"原始返回：{dict(result)}"
    lines = [
        f"{result.get('company_id', 'unknown')} 公司信息如下（数据源：{result.get('source', 'cmpf')}）："
    ]
    for item in result.get("company_info", []):
        lines.append(
            "- "
            f"公司名：{item.get('company_name', '-')}; "
            f"地址：{item.get('company_address', '-')}; "
            f"电话：{item.get('company_phone', '-')}; "
            f"邮箱：{item.get('company_email', '-')}"
        )
    return "\n".join(lines)


__all__ = ["build_agent_runtime", "build_graph"]
