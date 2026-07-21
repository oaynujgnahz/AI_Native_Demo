from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ai_native.agent.llm import ToolCallDecision
from ai_native.gateway.auth import Principal
from ai_native.gateway.base_resolver import AnalysisBase
from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec
from ai_native.gateway.errors import CompanyForbiddenError, RequestValidationError
from ai_native.gateway.executor import EnterpriseToolExecutor
from ai_native.gateway.tooling import (
    ENTERPRISE_TOOL_NAMES,
    ToolCatalog,
    build_enterprise_catalog,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentResponse:
    answer: str
    tool_name: str
    chart: Optional[ChartSpec] = None


class EnterpriseToolCatalog(ToolCatalog):
    """Compatibility name retained while callers migrate to ToolCatalog."""

    def __init__(self) -> None:
        catalog = build_enterprise_catalog()
        super().__init__([catalog.get(name) for name in catalog.names()])


class EnterpriseAgentService:
    def __init__(self, gateway: Any, repository: Any, planner: Any = None) -> None:
        self.gateway = gateway
        self.repository = repository
        self.planner = planner
        self.tool_catalog = EnterpriseToolCatalog()
        self.executor = EnterpriseToolExecutor(
            gateway, repository, catalog=self.tool_catalog
        )

    def answer(
        self,
        *,
        principal: Principal,
        bearer_token: str,
        message: str,
        context: Dict[str, Any],
    ) -> AgentResponse:
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
        execution = self.executor.execute(
            tool_name=tool_name,
            arguments=arguments,
            principal=principal,
            bearer_token=bearer_token,
            message=message,
            context=context,
        )
        company_id = execution.safe_facts["company_id"]
        year = execution.safe_facts.get("year")
        audit_details = dict(execution.audit_details)

        self.repository.write_audit(
            {
                "user_id": principal.user_id,
                "company_id": company_id,
                "tool_name": tool_name,
                "status": "success",
                "year": year,
                "result_count": execution.result_count,
                **audit_details,
            }
        )
        return AgentResponse(
            answer=execution.answer,
            tool_name=tool_name,
            chart=execution.chart,
        )

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
