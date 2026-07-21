from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


CMPF_ROUTING_GUIDANCE = (
    "Understand natural Japanese, Chinese, and English. "
    "Never invent a company ID, year, or Scope. Omit a missing argument "
    "so the gateway can use trusted page context or ask for clarification. "
    "Do not request write, delete, approval, export, or arbitrary tools. "
    "Routing rules: "
    "Use get_company_info ONLY for company metadata such as 会社の名称・住所, "
    "公司名称/地址, company name/address; never use it for emissions. "
    "Use get_annual_emission_summary for annual totals or 年間/年度/总量. "
    "Use get_scope_breakdown for Scope breakdown, 内訳, 明细. "
    "Use get_scope_composition_chart for composition/share/pie, 構成/割合/円グラフ/占比. "
    "Use get_monthly_emission_trend_chart for 毎月/月別/月ごと/推移, "
    "每月/月度/趋势, monthly/trend/line chart. "
    "Use get_top_emission_activities_chart for 上位/最大/ランキング, "
    "排名/最高, top/largest activities. "
    "For 拠点/据点/site requests, use list_analysis_bases to list available sites "
    "or resolve written site names in a controlled multi-step loop. "
    "use get_base_detail_monthly_chart for one site's 月別/每月/monthly trend; "
    "use get_base_detail_composition_chart for one site's 構成/占比/composition; "
    "use get_base_emission_composition_chart for company-wide site/area composition; "
    "use get_base_monthly_emission_chart for grouped site trends; "
    "use compare_base_emissions_chart for comparing 2-5 sites. "
    "Use compare_emission_periods_chart for 期間比較/期间比较/period comparison. "
    "When the user asks to draw, show, visualize, or graph, prefer a chart tool. "
)

_LEGACY_SITE_ROUTING_GUIDANCE = (
    "For the legacy one-shot path, use list_analysis_bases ONLY when the user asks "
    "which sites exist, available sites, 拠点一覧, or 据点列表. "
    "NEVER use list_analysis_bases for emissions, year, monthly, trend, or chart requests. "
    "Pass site names as written; the gateway resolves and validates base IDs. "
)


@dataclass(frozen=True)
class ToolCallDecision:
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    direct_answer: Optional[str] = None


class OpenAIToolPlanner:
    def __init__(
        self,
        client: Optional[Any] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.client = client
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")

    @classmethod
    def from_env(cls) -> Optional["OpenAIToolPlanner"]:
        openai_key = os.getenv("OPENAI_API_KEY")
        deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if openai_key:
            api_key = openai_key
            base_url = os.getenv("OPENAI_BASE_URL") or None
            provider = "openai-compatible"
        elif deepseek_key:
            api_key = deepseek_key
            base_url = os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            provider = "deepseek"
        else:
            logger.info(
                "LLM planner disabled: OPENAI_API_KEY/DEEPSEEK_API_KEY is not set"
            )
            return None
        from openai import OpenAI

        logger.info(
            "LLM planner enabled: model=%s base_url=%s provider=%s",
            os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            base_url or "openai-default",
            provider,
        )
        client_kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        return cls(client=OpenAI(**client_kwargs), base_url=base_url)

    def plan(
        self,
        user_text: str,
        registry: SupportsOpenAITools,
        context: Optional[Dict[str, Any]] = None,
    ) -> ToolCallDecision:
        tools = registry.openai_tools()
        logger.info(
            "LLM planning request: model=%s tools=%s user_text_length=%s",
            self.model,
            [tool["function"]["name"] for tool in tools],
            len(user_text),
        )
        safe_context = context or {}
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a CMPF carbon-emission business agent. "
                        f"{CMPF_ROUTING_GUIDANCE}"
                        f"{_LEGACY_SITE_ROUTING_GUIDANCE}"
                        f"Trusted page defaults: {json.dumps(safe_context, ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice="auto",
        )
        message = completion.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            content = getattr(message, "content", None)
            logger.info(
                "LLM planning response: direct_answer_received=%s",
                bool(content),
            )
            return ToolCallDecision(direct_answer=content)

        function = tool_calls[0].function
        try:
            arguments = json.loads(function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
            logger.warning(
                "LLM returned invalid tool arguments: tool=%s",
                function.name,
            )
        logger.info(
            "LLM planning response: tool=%s arguments_received=%s",
            function.name,
            bool(arguments),
        )
        return ToolCallDecision(tool_name=function.name, arguments=arguments)

    def plan_for_tool(
        self,
        user_text: str,
        registry: SupportsOpenAITools,
        tool_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> ToolCallDecision:
        tools = registry.openai_tools()
        allowed_names = {item["function"]["name"] for item in tools}
        if tool_name not in allowed_names:
            raise ValueError("forced tool is not registered")
        safe_context = context or {}
        logger.info(
            "LLM forced planning request: model=%s tool=%s user_text_length=%s",
            self.model,
            tool_name,
            len(user_text),
        )
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Extract arguments for the required CMPF tool {tool_name}. "
                        "Preserve an explicitly written site name exactly as base_name. "
                        "Never invent missing identifiers or dates. "
                        f"Trusted page defaults: {json.dumps(safe_context, ensure_ascii=False)}"
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        tool_calls = getattr(completion.choices[0].message, "tool_calls", None) or []
        if not tool_calls:
            raise ValueError("forced tool call missing")
        function = tool_calls[0].function
        try:
            arguments = json.loads(function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        logger.info(
            "LLM forced planning response: tool=%s arguments_received=%s",
            function.name,
            bool(arguments),
        )
        return ToolCallDecision(tool_name=function.name, arguments=arguments)


def _system_prompt(tools: list[dict[str, Any]], safe_context: Dict[str, Any]) -> str:
    names = {item["function"]["name"] for item in tools}
    if "get_monthly_emission_trend_chart" in names:
        routing = (
            "Routing rules: "
            "Use get_company_info ONLY for company metadata such as 会社の名称・住所, "
            "公司名称/地址, company name/address; never use it for emissions. "
            "Use get_annual_emission_summary for annual totals or 年間/年度/总量. "
            "Use get_scope_breakdown for Scope breakdown, 内訳, 明细. "
            "Use get_scope_composition_chart for composition/share/pie, 構成/割合/円グラフ/占比. "
            "Use get_monthly_emission_trend_chart for 毎月/月別/月ごと/推移, "
            "每月/月度/趋势, monthly/trend/line chart. "
            "Use get_top_emission_activities_chart for 上位/最大/ランキング, "
            "排名/最高, top/largest activities. "
            "For 拠点/据点/site requests: use list_analysis_bases ONLY when the user "
            "asks which sites exist, available sites, 拠点一覧, or 据点列表. "
            "NEVER use list_analysis_bases for emissions, year, monthly, trend, or chart requests. "
            "use get_base_detail_monthly_chart for one site's 月別/每月/monthly trend; "
            "use get_base_detail_composition_chart for one site's 構成/占比/composition; "
            "use get_base_emission_composition_chart for company-wide site/area composition; "
            "use get_base_monthly_emission_chart for grouped site trends; "
            "use compare_base_emissions_chart for comparing 2-5 sites. "
            "Use compare_emission_periods_chart for 期間比較/期间比较/period comparison. "
            "Pass site names as written; the gateway resolves and validates base IDs. "
            "When the user asks to draw, show, visualize, or graph, prefer a chart tool. "
        )
    else:
        routing = (
            "Only choose a tool from the provided tools list. "
            "Use get_company_info for company metadata. "
            "Use get_emission_dashboard for annual totals. "
            "Use get_scope_breakdown for Scope breakdown / 内訳 / 明细. "
        )
    return (
        "You are a CMPF carbon-emission business agent. "
        "Choose at most one tool when business data is needed. "
        "Understand natural Japanese, Chinese, and English. "
        "Never invent a company ID, year, or Scope. Omit a missing argument "
        "so the gateway can use trusted page context or ask for clarification. "
        "Do not request write, delete, approval, export, or arbitrary tools. "
        f"{routing}"
        f"Trusted page defaults: {json.dumps(safe_context, ensure_ascii=False)}"
    )
