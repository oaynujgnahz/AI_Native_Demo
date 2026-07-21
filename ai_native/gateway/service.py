from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ai_native.agent.llm import ToolCallDecision
from ai_native.gateway.auth import Principal
from ai_native.gateway.base_resolver import (
    AnalysisBase,
    AnalysisBaseResolver,
    BaseResolutionError,
)
from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec

logger = logging.getLogger(__name__)


class CompanyForbiddenError(Exception):
    pass


class RequestValidationError(Exception):
    def __init__(
        self, code: str, candidates: Optional[list[dict[str, str]]] = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.candidates = list(candidates or [])[:20]


@dataclass(frozen=True)
class AgentResponse:
    answer: str
    tool_name: str
    chart: Optional[ChartSpec] = None


@dataclass
class _ExecutionContext:
    company_name: Optional[str] = None
    start_month: Optional[int] = None
    bases_payload: Any = None
    bases_loaded: bool = False
    preparation_steps: int = 0

    def step(self) -> None:
        self.preparation_steps += 1
        if self.preparation_steps > 3:
            raise RequestValidationError("tool_loop_limit")


ENTERPRISE_TOOL_NAMES = frozenset(
    {
        "get_company_info",
        "get_annual_emission_summary",
        "get_scope_breakdown",
        "get_scope_composition_chart",
        "get_monthly_emission_trend_chart",
        "get_top_emission_activities_chart",
        "list_analysis_bases",
        "get_base_emission_composition_chart",
        "get_base_monthly_emission_chart",
        "get_base_detail_composition_chart",
        "get_base_detail_monthly_chart",
        "compare_base_emissions_chart",
        "compare_emission_periods_chart",
    }
)


class EnterpriseToolCatalog:
    def openai_tools(self) -> list[dict[str, Any]]:
        return [_tool_schema(name) for name in ENTERPRISE_TOOL_NAMES]


class EnterpriseAgentService:
    def __init__(self, gateway: Any, repository: Any, planner: Any = None) -> None:
        self.gateway = gateway
        self.repository = repository
        self.planner = planner
        self.tool_catalog = EnterpriseToolCatalog()
        self.base_resolver = AnalysisBaseResolver()

    def answer(
        self,
        *,
        principal: Principal,
        bearer_token: str,
        message: str,
        context: Dict[str, Any],
    ) -> AgentResponse:
        locale = _locale(context.get("locale") or principal.locale)
        decision = self._plan(message, context, principal)
        if decision.direct_answer and not decision.tool_name:
            self.repository.write_audit(
                {
                    "user_id": principal.user_id,
                    "company_id": principal.company_id,
                    "tool_name": "llm_direct_answer",
                    "status": "success",
                    "result_count": 0,
                }
            )
            return AgentResponse(
                answer=decision.direct_answer,
                tool_name="llm_direct_answer",
            )

        arguments = decision.arguments if decision.tool_name else {}
        tool_name = (
            decision.tool_name
            if decision.tool_name in ENTERPRISE_TOOL_NAMES
            else _select_tool(message)
        )
        company_id = str(
            arguments.get("company_id")
            or context.get("company_id")
            or principal.company_id
        )
        year = _year(_coalesce(arguments.get("year"), context.get("year")), message)
        scope = _scope_value(arguments.get("scope"), message)

        self._require_company_access(principal, company_id, bearer_token)
        if tool_name not in {
            "get_company_info",
            "list_analysis_bases",
            "compare_emission_periods_chart",
        } and year is None:
            raise RequestValidationError("year_required")
        execution = _ExecutionContext()
        company_name = self._company_name_cached(
            execution, principal, company_id, bearer_token
        )
        chart = None
        audit_details: Dict[str, Any] = {}
        try:
            if tool_name == "get_company_info":
                data = self.gateway.get_company_info(company_id, auth_token=bearer_token)
                answer = _company_answer(data, company_id, locale)
            elif tool_name == "list_analysis_bases":
                payload = self._bases_payload_cached(
                    execution, principal, company_id, locale, bearer_token
                )
                bases = self.base_resolver.list(payload, company_id=company_id)
                answer = _base_list_answer(bases, company_name, locale)
                audit_details["result_count"] = len(bases)
            elif tool_name == "get_annual_emission_summary":
                start_month = self._company_start_month_cached(
                    execution, principal, company_id, bearer_token
                )
                data = self.gateway.get_dashboard_summary(
                    company_id, year, start_month, auth_token=bearer_token
                )
                answer = _summary_answer(data, company_name, year, locale)
            elif tool_name == "get_scope_breakdown":
                start_month = self._company_start_month_cached(
                    execution, principal, company_id, bearer_token
                )
                data = self.gateway.get_scope_breakdown(
                    company_id, year, start_month, auth_token=bearer_token
                )
                answer = _breakdown_answer(data, company_name, year, locale)
            else:
                start_month = self._company_start_month_cached(
                    execution, principal, company_id, bearer_token
                )
                if tool_name == "get_scope_composition_chart":
                    if scope is None:
                        raise RequestValidationError("scope_required")
                    data = self.gateway.get_scope_summary(
                        company_id,
                        year,
                        start_month,
                        scope,
                        locale,
                        auth_token=bearer_token,
                    )
                    chart = _composition_chart(
                        data, company_id, company_name, year, scope
                    )
                elif tool_name == "get_monthly_emission_trend_chart":
                    data = self.gateway.get_scope_emission_for_month(
                        company_id,
                        year,
                        start_month,
                        scope,
                        locale,
                        auth_token=bearer_token,
                    )
                    chart = _monthly_chart(data, company_id, company_name, year)
                elif tool_name == "get_top_emission_activities_chart":
                    data = self.gateway.get_top_activity_items_by_emission(
                        company_id, year, start_month, locale, auth_token=bearer_token
                    )
                    chart = _top_chart(data, company_id, company_name, year)
                elif tool_name == "get_base_detail_monthly_chart":
                    base = self._resolve_base(
                        execution,
                        principal,
                        company_id,
                        locale,
                        bearer_token,
                        base_id=arguments.get("base_id"),
                        base_name=arguments.get("base_name"),
                    )
                    data = self.gateway.get_base_month_emission(
                        company_id,
                        base.base_id,
                        year,
                        start_month,
                        auth_token=bearer_token,
                    )
                    chart = _base_detail_monthly_chart(
                        data, company_id, company_name, base, year
                    )
                    audit_details["base_ids"] = [base.base_id]
                elif tool_name == "get_base_detail_composition_chart":
                    base = self._resolve_base(
                        execution,
                        principal,
                        company_id,
                        locale,
                        bearer_token,
                        base_id=arguments.get("base_id"),
                        base_name=arguments.get("base_name"),
                    )
                    period_start, period_end = _fiscal_months(year, start_month)
                    data = self.gateway.get_base_large_item_emission(
                        company_id,
                        base.base_id,
                        period_start,
                        period_end,
                        auth_token=bearer_token,
                    )
                    chart = _base_detail_composition_chart(
                        data, company_id, company_name, base, year
                    )
                    audit_details["base_ids"] = [base.base_id]
                elif tool_name == "get_base_emission_composition_chart":
                    period_start, period_end = _fiscal_months(year, start_month)
                    group_by = _group_by(arguments.get("group_by"))
                    payload = {
                        "companyId": company_id,
                        "year": year,
                        "companyStartMonth": start_month,
                        "startMonth": period_start,
                        "endMonth": period_end,
                        "baseList": [],
                        "dynamicTab": group_by,
                    }
                    data = self.gateway.get_base_type_emission(
                        payload, auth_token=bearer_token
                    )
                    chart = _base_group_composition_chart(
                        data, company_id, company_name, year, group_by
                    )
                elif tool_name == "get_base_monthly_emission_chart":
                    bases = self._resolve_bases(
                        execution,
                        principal,
                        company_id,
                        locale,
                        bearer_token,
                        base_ids=arguments.get("base_ids"),
                        base_names=arguments.get("base_names"),
                        minimum=1,
                    )
                    group_by = _group_by(arguments.get("group_by"))
                    period_start, period_end = _fiscal_months(year, start_month)
                    payload = {
                        "companyId": company_id,
                        "year": year,
                        "companyStartMonth": start_month,
                        "startMonth": period_start,
                        "endMonth": period_end,
                        "baseList": [base.base_id for base in bases],
                        "dynamicTab": group_by,
                    }
                    data = self.gateway.get_base_type_emission_for_month(
                        payload, auth_token=bearer_token
                    )
                    chart = _base_group_monthly_chart(
                        data, company_id, company_name, bases, year
                    )
                    audit_details["base_ids"] = [base.base_id for base in bases]
                elif tool_name == "compare_base_emissions_chart":
                    bases = self._resolve_bases(
                        execution,
                        principal,
                        company_id,
                        locale,
                        bearer_token,
                        base_ids=arguments.get("base_ids"),
                        base_names=arguments.get("base_names"),
                        minimum=2,
                    )
                    payload = {
                        "companyId": company_id,
                        "aimYear": str(year),
                        "companyStartMonth": start_month,
                        "baseId": [base.base_id for base in bases],
                    }
                    data = self.gateway.compare_emissions_by_base(
                        payload, auth_token=bearer_token
                    )
                    chart = _base_comparison_chart(
                        data, company_id, company_name, bases, year
                    )
                    audit_details["base_ids"] = [base.base_id for base in bases]
                elif tool_name == "compare_emission_periods_chart":
                    first = _validated_period(
                        arguments.get("start_month"), arguments.get("end_month")
                    )
                    second = _validated_period(
                        arguments.get("comparison_start_month"),
                        arguments.get("comparison_end_month"),
                    )
                    payload = _period_comparison_payload(
                        company_id, first, second, start_month
                    )
                    data = self.gateway.compare_emissions_by_duration(
                        payload, auth_token=bearer_token
                    )
                    chart = _period_comparison_chart(
                        data, company_id, company_name, first, second
                    )
                    audit_details.update(
                        {
                            "period_start": first[0],
                            "period_end": first[1],
                            "comparison_period": f"{second[0]}-{second[1]}",
                        }
                    )
                else:
                    raise RequestValidationError("tool_not_implemented")
                answer = (
                    _period_comparison_answer(chart, locale)
                    if tool_name == "compare_emission_periods_chart"
                    else _chart_answer(chart, locale)
                )
        except ValueError as exc:
            raise RequestValidationError("invalid_chart_payload") from exc

        self.repository.write_audit(
            {
                "user_id": principal.user_id,
                "company_id": company_id,
                "tool_name": tool_name,
                "status": "success",
                "year": year,
                "result_count": audit_details.pop(
                    "result_count", len(chart.categories) if chart else 1
                ),
                **audit_details,
            }
        )
        return AgentResponse(answer=answer, tool_name=tool_name, chart=chart)

    def _plan(
        self, message: str, context: Dict[str, Any], principal: Principal
    ) -> ToolCallDecision:
        if self.planner is None:
            return ToolCallDecision()
        safe_context = {
            "company_id": str(context.get("company_id") or principal.company_id),
            "year": context.get("year"),
            "locale": _locale(context.get("locale") or principal.locale),
        }
        try:
            decision = self.planner.plan(
                message, self.tool_catalog, context=safe_context
            )
        except Exception as exc:
            logger.warning("LLM tool planning failed; using rules: %s", type(exc).__name__)
            return ToolCallDecision()
        if decision.tool_name and decision.tool_name not in ENTERPRISE_TOOL_NAMES:
            logger.warning("LLM selected a non-whitelisted tool; using rules")
            return ToolCallDecision()
        rule_tool = _select_tool(message)
        if (
            decision.tool_name == "list_analysis_bases"
            and rule_tool
            in {
                "get_base_detail_composition_chart",
                "get_base_detail_monthly_chart",
                "get_base_emission_composition_chart",
                "get_base_monthly_emission_chart",
                "compare_base_emissions_chart",
            }
        ):
            replan = getattr(self.planner, "plan_for_tool", None)
            if callable(replan):
                try:
                    forced = replan(
                        message,
                        self.tool_catalog,
                        rule_tool,
                        context=safe_context,
                    )
                except Exception as exc:
                    logger.warning(
                        "LLM semantic replan failed; keeping original decision: %s",
                        type(exc).__name__,
                    )
                else:
                    if forced.tool_name == rule_tool:
                        return forced
        return decision

    def _require_company_access(
        self, principal: Principal, company_id: str, bearer_token: str
    ) -> None:
        allowed = {principal.company_id}
        payload = self.gateway.list_direct_child_companies(auth_token=bearer_token)
        for item in _body(payload):
            if isinstance(item, dict):
                value = item.get("value") or item.get("companyId") or item.get("id")
                if value is not None:
                    allowed.add(str(value))
        if company_id not in allowed:
            self.repository.write_audit(
                {
                    "user_id": principal.user_id,
                    "company_id": company_id,
                    "tool_name": "company_scope_check",
                    "status": "denied",
                    "error_code": "company_forbidden",
                }
            )
            raise CompanyForbiddenError(company_id)

    def _company_start_month(self, company_id: str, bearer_token: str) -> int:
        payload = _body(self.gateway.get_company_start_months(auth_token=bearer_token))
        if isinstance(payload, dict):
            value = payload.get(company_id)
            if value is None and company_id.isdigit():
                value = payload.get(int(company_id))
            if value:
                return int(value)
        return 1

    def _company_name(self, company_id: str, bearer_token: str) -> str:
        payload = _body(self.gateway.get_company_info(company_id, auth_token=bearer_token))
        if isinstance(payload, dict):
            return str(payload.get("companyName") or payload.get("company_name") or company_id)
        return company_id

    def _company_name_cached(
        self,
        execution: _ExecutionContext,
        principal: Principal,
        company_id: str,
        bearer_token: str,
    ) -> str:
        if execution.company_name is None:
            execution.step()
            execution.company_name = self._company_name(company_id, bearer_token)
            self._write_preparation_audit(
                principal, company_id, "resolve_company", "success", 1
            )
        return execution.company_name

    def _company_start_month_cached(
        self,
        execution: _ExecutionContext,
        principal: Principal,
        company_id: str,
        bearer_token: str,
    ) -> int:
        if execution.start_month is None:
            execution.step()
            execution.start_month = self._company_start_month(company_id, bearer_token)
            self._write_preparation_audit(
                principal, company_id, "resolve_fiscal_start_month", "success", 1
            )
        return execution.start_month

    def _bases_payload_cached(
        self,
        execution: _ExecutionContext,
        principal: Principal,
        company_id: str,
        locale: str,
        bearer_token: str,
    ) -> Any:
        if not execution.bases_loaded:
            execution.step()
            execution.bases_payload = self.gateway.list_analysis_bases(
                company_id, locale, auth_token=bearer_token
            )
            execution.bases_loaded = True
            self._write_preparation_audit(
                principal,
                company_id,
                "list_analysis_bases",
                "success",
                len(
                    self.base_resolver.list(
                        execution.bases_payload, company_id=company_id
                    )
                ),
            )
        return execution.bases_payload

    def _resolve_base(
        self,
        execution: _ExecutionContext,
        principal: Principal,
        company_id: str,
        locale: str,
        bearer_token: str,
        *,
        base_id: Any = None,
        base_name: Any = None,
    ) -> AnalysisBase:
        payload = self._bases_payload_cached(
            execution, principal, company_id, locale, bearer_token
        )
        try:
            base = self.base_resolver.resolve(
                payload,
                company_id=company_id,
                base_id=base_id,
                base_name=base_name,
            )
        except BaseResolutionError as exc:
            self._write_preparation_audit(
                principal, company_id, "resolve_base", "failed", len(exc.candidates)
            )
            raise RequestValidationError(
                exc.code,
                candidates=[
                    {"base_id": item.base_id, "name": item.name}
                    for item in exc.candidates
                ],
            ) from exc
        self._write_preparation_audit(
            principal, company_id, "resolve_base", "success", 1
        )
        return base

    def _resolve_bases(
        self,
        execution: _ExecutionContext,
        principal: Principal,
        company_id: str,
        locale: str,
        bearer_token: str,
        *,
        base_ids: Any = None,
        base_names: Any = None,
        minimum: int,
    ) -> list[AnalysisBase]:
        raw_ids = list(base_ids or [])
        raw_names = list(base_names or [])
        values = [(value, None) for value in raw_ids] or [
            (None, value) for value in raw_names
        ]
        if len(values) < minimum:
            raise RequestValidationError("base_required")
        if len(values) > 5:
            raise RequestValidationError("too_many_bases")
        bases = [
            self._resolve_base(
                execution,
                principal,
                company_id,
                locale,
                bearer_token,
                base_id=base_id,
                base_name=base_name,
            )
            for base_id, base_name in values
        ]
        if len({base.base_id for base in bases}) != len(bases):
            raise RequestValidationError("base_ambiguous")
        return bases

    def _write_preparation_audit(
        self,
        principal: Principal,
        company_id: str,
        tool_name: str,
        status: str,
        result_count: int,
    ) -> None:
        self.repository.write_audit(
            {
                "user_id": principal.user_id,
                "company_id": company_id,
                "tool_name": tool_name,
                "status": status,
                "result_count": result_count,
            }
        )


def _select_tool(message: str) -> str:
    text = message.lower()
    base_words = ("拠点", "据点", "site", "base")
    compare_words = ("比較", "比べ", "比较", "对比", "compare", "versus", " vs ")
    period_words = ("期間", "時期", "期间", "时期", "period", "duration")
    has_base = any(word in text for word in base_words)
    has_compare = any(word in text for word in compare_words)
    if has_compare and any(word in text for word in period_words):
        return "compare_emission_periods_chart"
    if has_compare and has_base:
        return "compare_base_emissions_chart"
    if has_base and any(word in text for word in ("一覧", "リスト", "列表", "list")):
        return "list_analysis_bases"
    if has_base and any(
        word in text for word in ("月別", "月度", "每月", "monthly", "推移", "趋势", "trend")
    ):
        return "get_base_detail_monthly_chart"
    if has_base and any(
        word in text for word in ("構成", "割合", "占比", "composition", "円グラフ", "饼图", "pie")
    ):
        return "get_base_detail_composition_chart"
    if any(word in text for word in ("top", "上位", "排名", "活動項目", "活动项目")):
        return "get_top_emission_activities_chart"
    if any(word in text for word in ("月別", "月度", "monthly", "推移", "趋势", "trend")):
        return "get_monthly_emission_trend_chart"
    if any(word in text for word in ("構成", "占比", "composition", "円グラフ", "饼图", "pie")):
        return "get_scope_composition_chart"
    if any(word in text for word in ("会社情報", "公司信息", "company info", "住所", "address")):
        return "get_company_info"
    if any(word in text for word in ("内訳", "明细", "breakdown", "scope")):
        return "get_scope_breakdown"
    return "get_annual_emission_summary"


def _year(value: Any, message: str) -> Optional[int]:
    if value is not None and str(value).strip() != "":
        try:
            year = int(value)
        except (TypeError, ValueError):
            match = re.search(r"(20\d{2})", str(value))
            if match:
                return int(match.group(1))
        else:
            return year if 2000 <= year <= 2100 else None
    match = re.search(r"\b(20\d{2})\b", message) or re.search(r"(20\d{2})", message)
    return int(match.group(1)) if match else None


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _scope(message: str) -> Optional[int]:
    match = re.search(r"scope\s*([123])", message, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _scope_value(value: Any, message: str) -> Optional[int]:
    if value is not None:
        try:
            scope = int(value)
        except (TypeError, ValueError):
            return _scope(message)
        return scope if scope in (1, 2, 3) else _scope(message)
    return _scope(message)


def _tool_schema(name: str) -> dict[str, Any]:
    descriptions = {
        "get_company_info": "Get CMPF company profile information, such as name or address.",
        "get_annual_emission_summary": "Get total annual GHG emissions and Scope totals.",
        "get_scope_breakdown": "Get annual Scope emission breakdown details.",
        "get_scope_composition_chart": "Create a pie chart for a specified Scope composition.",
        "get_monthly_emission_trend_chart": "Create a monthly GHG emission trend line chart.",
        "get_top_emission_activities_chart": "Create a Top 10 emission activities bar chart.",
        "list_analysis_bases": "List which CMPF analysis sites exist for the selected company. Use only for list/available-site questions; never use for emissions, year, monthly, trend, or chart requests.",
        "get_base_emission_composition_chart": "Create an emission composition pie chart grouped by site, area, or fixed site category.",
        "get_base_monthly_emission_chart": "Create a monthly emission trend for selected sites or site groups.",
        "get_base_detail_composition_chart": "Create a large-item emission composition pie chart for one site.",
        "get_base_detail_monthly_chart": "Create a monthly emission trend chart for one site.",
        "compare_base_emissions_chart": "Compare emissions for two to five sites in a grouped bar chart.",
        "compare_emission_periods_chart": "Compare emissions between two explicit month ranges.",
    }
    properties: dict[str, Any] = {
        "company_id": {
            "type": "string",
            "description": "CMPF company ID. Omit to use the current company context.",
        }
    }
    if name not in {
        "get_company_info",
        "list_analysis_bases",
        "compare_emission_periods_chart",
    }:
        properties["year"] = {
            "type": "integer",
            "minimum": 2000,
            "maximum": 2100,
            "description": "Target fiscal year. Omit to use page context.",
        }
    if name in {"get_scope_composition_chart", "get_monthly_emission_trend_chart"}:
        properties["scope"] = {
            "type": "integer",
            "enum": [1, 2, 3],
            "description": "Optional Scope number; required for composition charts.",
        }
    if name in {
        "get_base_detail_composition_chart",
        "get_base_detail_monthly_chart",
    }:
        properties["base_name"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Exact CMPF site name. The gateway resolves and validates its baseId.",
        }
        properties["base_id"] = {
            "type": "string",
            "description": "CMPF site ID when explicitly known; always revalidated.",
        }
    if name in {"get_base_emission_composition_chart", "get_base_monthly_emission_chart"}:
        properties["group_by"] = {
            "type": "string",
            "enum": ["base", "area", "category"],
        }
    if name == "get_base_monthly_emission_chart":
        properties["base_names"] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "minItems": 1,
            "maxItems": 5,
        }
    if name == "compare_base_emissions_chart":
        properties["base_names"] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "minItems": 2,
            "maxItems": 5,
        }
        properties["base_ids"] = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 5,
        }
    if name == "compare_emission_periods_chart":
        month = {
            "type": "string",
            "pattern": r"^20\d{2}(0[1-9]|1[0-2])$",
            "description": "Month in YYYYMM format.",
        }
        properties.update(
            {
                "start_month": month,
                "end_month": month,
                "comparison_start_month": month,
                "comparison_end_month": month,
            }
        )
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": descriptions[name],
            "parameters": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        },
    }


def _locale(value: Any) -> str:
    return "en" if str(value).lower().startswith("en") else "ja"


def _body(payload: Any) -> Any:
    if isinstance(payload, dict) and "body" in payload:
        return payload["body"]
    return payload


def _rows(payload: Any) -> list[dict]:
    body = _body(payload)
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in ("items", "records", "list", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _number(row: dict, *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return float(value)
    return 0.0


def _group_by(value: Any) -> str:
    group_by = str(value or "base")
    if group_by not in {"base", "area", "category"}:
        raise RequestValidationError("invalid_group_by")
    return group_by


def _fiscal_months(year: int, company_start_month: int) -> tuple[str, str]:
    start = f"{year}{company_start_month:02d}"
    if company_start_month == 1:
        return start, f"{year}12"
    return start, f"{year + 1}{company_start_month - 1:02d}"


def _validated_period(start_value: Any, end_value: Any) -> tuple[str, str]:
    start_text = str(start_value or "")
    end_text = str(end_value or "")
    pattern = r"20\d{2}(0[1-9]|1[0-2])"
    if not re.fullmatch(pattern, start_text) or not re.fullmatch(pattern, end_text):
        raise RequestValidationError("invalid_period")
    start = datetime.strptime(start_text, "%Y%m")
    end = datetime.strptime(end_text, "%Y%m")
    if start > end:
        raise RequestValidationError("invalid_period")
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if months > 36:
        raise RequestValidationError("period_too_long")
    return start_text, end_text


def _fiscal_year(month: str, company_start_month: int) -> int:
    year = int(month[:4])
    return year if int(month[4:]) >= company_start_month else year - 1


def _slash_month(month: str) -> str:
    return f"{month[:4]}/{month[4:]}"


def _period_comparison_payload(
    company_id: str,
    first: tuple[str, str],
    second: tuple[str, str],
    company_start_month: int,
) -> dict[str, Any]:
    return {
        "companyId": company_id,
        "startTime1": _slash_month(first[0]),
        "endTime1": _slash_month(first[1]),
        "startTime2": _slash_month(second[0]),
        "endTime2": _slash_month(second[1]),
        "startYear1": str(_fiscal_year(first[0], company_start_month)),
        "endYear1": str(_fiscal_year(first[1], company_start_month)),
        "startYear2": str(_fiscal_year(second[0], company_start_month)),
        "endYear2": str(_fiscal_year(second[1], company_start_month)),
        "page": "1",
        "size": "100",
        "sort": "emissionVolume,desc",
    }


def _source(tool: str, company_id: str, company_name: str, period: str) -> ChartSource:
    return ChartSource(
        tool_name=tool,
        company_id=company_id,
        company_name=company_name,
        period=period,
    )


def _composition_chart(payload, company_id, company_name, year, scope) -> ChartSpec:
    rows = _rows(payload)[:100]
    categories = [str(row.get("largeItem") or row.get("emissionSourceName") or "-") for row in rows]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="pie",
        title=f"{company_name} {year} Scope {scope}",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source("get_scope_composition_chart", company_id, company_name, str(year)),
    )


def _monthly_chart(payload, company_id, company_name, year) -> ChartSpec:
    rows = _rows(payload)[:100]
    categories = [str(row.get("activityMonth") or row.get("month") or "-") for row in rows]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="line",
        title=f"{company_name} {year} Monthly GHG Emissions",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source("get_monthly_emission_trend_chart", company_id, company_name, str(year)),
    )


def _base_detail_monthly_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    base: AnalysisBase,
    year: int,
) -> ChartSpec:
    rows = _rows(payload)[:100]
    categories = [
        str(row.get("activityMonth") or row.get("month") or "-") for row in rows
    ]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="line",
        title=f"{company_name} {base.name} {year} Monthly GHG Emissions",
        categories=categories,
        series=[ChartSeries(name=base.name, values=values)],
        source=_source(
            "get_base_detail_monthly_chart",
            company_id,
            company_name,
            str(year),
        ),
    )


def _base_detail_composition_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    base: AnalysisBase,
    year: int,
) -> ChartSpec:
    rows = _rows(payload)[:100]
    return ChartSpec(
        chart_type="pie",
        title=f"{company_name} {base.name} {year} GHG Emission Composition",
        categories=[
            str(row.get("largeItem") or row.get("emissionSourceName") or "-")
            for row in rows
        ],
        series=[
            ChartSeries(
                name=base.name,
                values=[
                    _number(row, "emissionVolume", "emission_volume", "total")
                    for row in rows
                ],
            )
        ],
        source=_source(
            "get_base_detail_composition_chart",
            company_id,
            company_name,
            str(year),
        ),
    )


def _base_group_composition_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    year: int,
    group_by: str,
) -> ChartSpec:
    rows = _rows(payload)[:100]
    name_keys = {
        "base": ("baseGroupName", "baseName"),
        "area": ("areaName", "baseGroupName"),
        "category": ("categoryKbnName", "categoryName", "baseGroupName"),
    }[group_by]
    categories = []
    values = []
    for row in rows:
        name = next((row.get(key) for key in name_keys if row.get(key)), "-")
        categories.append(str(name))
        values.append(_number(row, "emissionVolume", "total", "emission_volume"))
    return ChartSpec(
        chart_type="pie",
        title=f"{company_name} {year} Site Emission Composition",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source(
            "get_base_emission_composition_chart",
            company_id,
            company_name,
            str(year),
        ),
    )


def _base_group_monthly_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    bases: list[AnalysisBase],
    year: int,
) -> ChartSpec:
    rows = _rows(payload)
    categories = sorted(
        {
            str(row.get("activityMonth") or row.get("month"))
            for row in rows
            if row.get("activityMonth") or row.get("month")
        }
    )[:100]
    series = []
    for base in bases[:5]:
        values = []
        for month in categories:
            values.append(
                sum(
                    _number(row, "emissionVolume", "emission_volume")
                    for row in rows
                    if str(row.get("activityMonth") or row.get("month")) == month
                    and (
                        str(row.get("baseId")) == base.base_id
                        or str(row.get("baseName") or "").casefold()
                        == base.name.casefold()
                    )
                )
            )
        series.append(ChartSeries(name=base.name, values=values))
    return ChartSpec(
        chart_type="line",
        title=f"{company_name} {year} Site Monthly GHG Emissions",
        categories=categories,
        series=series,
        source=_source(
            "get_base_monthly_emission_chart",
            company_id,
            company_name,
            str(year),
        ),
    )


def _base_comparison_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    bases: list[AnalysisBase],
    year: int,
) -> ChartSpec:
    rows = _rows(payload)
    series = []
    for base in bases[:5]:
        row = next(
            (item for item in rows if str(item.get("baseId")) == base.base_id),
            None,
        )
        if row is None:
            raise RequestValidationError("base_comparison_data_missing")
        series.append(
            ChartSeries(
                name=base.name,
                values=[
                    _number(row, "emissionTotal", "total"),
                    _number(row, "scope1Emission", "scope1Volume"),
                    _number(row, "scope2Emission", "scope2Volume"),
                    _number(row, "scope3Emission", "scope3Volume"),
                ],
            )
        )
    return ChartSpec(
        chart_type="grouped_bar",
        title=f"{company_name} {year} Site GHG Emission Comparison",
        categories=["Total", "Scope 1", "Scope 2", "Scope 3"],
        series=series,
        source=_source(
            "compare_base_emissions_chart", company_id, company_name, str(year)
        ),
    )


def _period_comparison_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    first: tuple[str, str],
    second: tuple[str, str],
) -> ChartSpec:
    rows = _rows(payload)[:2]
    labels = [f"{first[0]}-{first[1]}", f"{second[0]}-{second[1]}"]
    series = []
    for index, label in enumerate(labels):
        container = rows[index] if index < len(rows) else {}
        scope_rows = container.get("scopeAndTotalData", [])
        row = scope_rows[0] if isinstance(scope_rows, list) and scope_rows else {}
        series.append(
            ChartSeries(
                name=label,
                values=[
                    _number(row, "total", "emissionTotal"),
                    _number(row, "scope1Volume", "scope1Emission"),
                    _number(row, "scope2Volume", "scope2Emission"),
                    _number(row, "scope3Volume", "scope3Emission"),
                ],
            )
        )
    return ChartSpec(
        chart_type="grouped_bar",
        title=f"{company_name} GHG Emission Period Comparison",
        categories=["Total", "Scope 1", "Scope 2", "Scope 3"],
        series=series,
        source=_source(
            "compare_emission_periods_chart",
            company_id,
            company_name,
            f"{labels[0]} vs {labels[1]}",
        ),
    )


def _top_chart(payload, company_id, company_name, year) -> ChartSpec:
    rows = _rows(payload)[:10]
    categories = [str(row.get("emissionSourceName") or row.get("largeItem") or "-") for row in rows]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="horizontal_bar",
        title=f"{company_name} {year} Top 10 GHG Emissions",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source("get_top_emission_activities_chart", company_id, company_name, str(year)),
    )


def _company_answer(payload, company_id, locale) -> str:
    body = _body(payload)
    name = body.get("companyName", company_id) if isinstance(body, dict) else company_id
    return f"会社情報：{name}（{company_id}）" if locale == "ja" else f"Company: {name} ({company_id})"


def _base_list_answer(
    bases: list[AnalysisBase], company_name: str, locale: str
) -> str:
    if not bases:
        return (
            f"{company_name} に利用可能な拠点はありません。"
            if locale == "ja"
            else f"No analysis sites are available for {company_name}."
        )
    values = "、".join(f"{base.name}（ID: {base.base_id}）" for base in bases)
    return (
        f"{company_name} の拠点：{values}"
        if locale == "ja"
        else f"Sites for {company_name}: {values}"
    )


def _summary_answer(payload, company_name, year, locale) -> str:
    body = _body(payload)
    total = (
        body.get("total")
        or body.get("totalEmissionVolume")
        or body.get("total_tco2e")
        if isinstance(body, dict)
        else None
    )
    value = f"{float(total):,.2f} t-CO₂e" if total is not None else "取得済み"
    return f"{company_name} の {year} 年度 GHG 排出量は {value} です。" if locale == "ja" else f"{company_name}'s {year} GHG emissions are {value}."


def _breakdown_answer(payload, company_name, year, locale) -> str:
    count = len(_rows(payload))
    return f"{company_name} の {year} 年度 Scope 内訳を {count} 件取得しました。" if locale == "ja" else f"Retrieved {count} Scope records for {company_name} in {year}."


def _chart_answer(chart: ChartSpec, locale: str) -> str:
    return f"{chart.title} を表示します。" if locale == "ja" else f"Displaying {chart.title}."


def _period_comparison_answer(chart: ChartSpec, locale: str) -> str:
    first_total = chart.series[0].values[0]
    second_total = chart.series[1].values[0]
    difference = second_total - first_total
    percentage = (
        f"（{difference / first_total * 100:+.2f}%）" if first_total != 0 else ""
    )
    if locale == "ja":
        return (
            f"{chart.title} を表示します。期間合計の差は "
            f"{abs(difference):,.2f} t-CO₂e {percentage}です。"
        )
    return (
        f"Displaying {chart.title}. The absolute difference between period totals is "
        f"{abs(difference):,.2f} t-CO₂e {percentage}."
    )
