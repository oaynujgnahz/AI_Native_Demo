from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ai_native.gateway.base_resolver import AnalysisBase
from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec
from ai_native.gateway.errors import RequestValidationError


def year(value: Any, message: str) -> int | None:
    if value is not None and str(value).strip() != "":
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            match = re.search(r"(20\d{2})", str(value))
            if match:
                return int(match.group(1))
        else:
            return parsed if 2000 <= parsed <= 2100 else None
    match = re.search(r"\b(20\d{2})\b", message) or re.search(r"(20\d{2})", message)
    return int(match.group(1)) if match else None


def _scope(message: str) -> int | None:
    match = re.search(r"scope\s*([123])", message, re.IGNORECASE)
    return int(match.group(1)) if match else None


def scope_value(value: Any, message: str) -> int | None:
    if value is not None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return _scope(message)
        return parsed if parsed in (1, 2, 3) else _scope(message)
    return _scope(message)


def locale(value: Any) -> str:
    return "en" if str(value).lower().startswith("en") else "ja"


def body(payload: Any) -> Any:
    if isinstance(payload, dict) and "body" in payload:
        return payload["body"]
    return payload


def _rows(payload: Any) -> list[dict]:
    value = body(payload)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("items", "records", "list", "data"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _number(row: dict, *keys: str) -> float:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return float(value)
    return 0.0


def group_by(value: Any) -> str:
    parsed = str(value or "base")
    if parsed not in {"base", "area", "category"}:
        raise RequestValidationError("invalid_group_by")
    return parsed


def fiscal_months(year: int, company_start_month: int) -> tuple[str, str]:
    start = f"{year}{company_start_month:02d}"
    if company_start_month == 1:
        return start, f"{year}12"
    return start, f"{year + 1}{company_start_month - 1:02d}"


def validated_period(start_value: Any, end_value: Any) -> tuple[str, str]:
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
    parsed_year = int(month[:4])
    return parsed_year if int(month[4:]) >= company_start_month else parsed_year - 1


def _slash_month(month: str) -> str:
    return f"{month[:4]}/{month[4:]}"


def period_comparison_payload(
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


def _source(
    tool: str, company_id: str, company_name: str, period: str
) -> ChartSource:
    return ChartSource(
        tool_name=tool,
        company_id=company_id,
        company_name=company_name,
        period=period,
    )


def composition_chart(payload, company_id, company_name, year, scope) -> ChartSpec:
    rows = _rows(payload)[:100]
    categories = [
        str(row.get("largeItem") or row.get("emissionSourceName") or "-")
        for row in rows
    ]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="pie",
        title=f"{company_name} {year} Scope {scope}",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source(
            "get_scope_composition_chart", company_id, company_name, str(year)
        ),
    )


def monthly_chart(payload, company_id, company_name, year) -> ChartSpec:
    rows = _rows(payload)[:100]
    categories = [
        str(row.get("activityMonth") or row.get("month") or "-") for row in rows
    ]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="line",
        title=f"{company_name} {year} Monthly GHG Emissions",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source(
            "get_monthly_emission_trend_chart", company_id, company_name, str(year)
        ),
    )


def base_detail_monthly_chart(
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


def base_detail_composition_chart(
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


def base_group_composition_chart(
    payload: Any,
    company_id: str,
    company_name: str,
    year: int,
    grouping: str,
) -> ChartSpec:
    rows = _rows(payload)[:100]
    name_keys = {
        "base": ("baseGroupName", "baseName"),
        "area": ("areaName", "baseGroupName"),
        "category": ("categoryKbnName", "categoryName", "baseGroupName"),
    }[grouping]
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


def base_group_monthly_chart(
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


def base_comparison_chart(
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


def period_comparison_chart(
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


def top_chart(payload, company_id, company_name, year) -> ChartSpec:
    rows = _rows(payload)[:10]
    categories = [
        str(row.get("emissionSourceName") or row.get("largeItem") or "-")
        for row in rows
    ]
    values = [_number(row, "emissionVolume", "emission_volume") for row in rows]
    return ChartSpec(
        chart_type="horizontal_bar",
        title=f"{company_name} {year} Top 10 GHG Emissions",
        categories=categories,
        series=[ChartSeries(name="GHG Emissions", values=values)],
        source=_source(
            "get_top_emission_activities_chart", company_id, company_name, str(year)
        ),
    )


def company_answer(payload, company_id, selected_locale) -> str:
    value = body(payload)
    name = value.get("companyName", company_id) if isinstance(value, dict) else company_id
    if selected_locale == "ja":
        return f"会社情報：{name}（{company_id}）"
    return f"Company: {name} ({company_id})"


def base_list_answer(
    bases: list[AnalysisBase], company_name: str, selected_locale: str
) -> str:
    if not bases:
        if selected_locale == "ja":
            return f"{company_name} に利用可能な拠点はありません。"
        return f"No analysis sites are available for {company_name}."
    values = "、".join(f"{base.name}（ID: {base.base_id}）" for base in bases)
    if selected_locale == "ja":
        return f"{company_name} の拠点：{values}"
    return f"Sites for {company_name}: {values}"


def summary_answer(payload, company_name, selected_year, selected_locale) -> str:
    value = body(payload)
    total = (
        value.get("total")
        or value.get("totalEmissionVolume")
        or value.get("total_tco2e")
        if isinstance(value, dict)
        else None
    )
    display = f"{float(total):,.2f} t-CO₂e" if total is not None else "取得済み"
    if selected_locale == "ja":
        return f"{company_name} の {selected_year} 年度 GHG 排出量は {display} です。"
    return f"{company_name}'s {selected_year} GHG emissions are {display}."


def breakdown_answer(payload, company_name, selected_year, selected_locale) -> str:
    count = len(_rows(payload))
    if selected_locale == "ja":
        return f"{company_name} の {selected_year} 年度 Scope 内訳を {count} 件取得しました。"
    return f"Retrieved {count} Scope records for {company_name} in {selected_year}."


def chart_answer(chart: ChartSpec, selected_locale: str) -> str:
    if selected_locale == "ja":
        return f"{chart.title} を表示します。"
    return f"Displaying {chart.title}."


def period_comparison_answer(chart: ChartSpec, selected_locale: str) -> str:
    first_total = chart.series[0].values[0]
    second_total = chart.series[1].values[0]
    difference = second_total - first_total
    percentage = (
        f"（{difference / first_total * 100:+.2f}%）" if first_total != 0 else ""
    )
    if selected_locale == "ja":
        return (
            f"{chart.title} を表示します。期間合計の差は "
            f"{abs(difference):,.2f} t-CO₂e {percentage}です。"
        )
    return (
        f"Displaying {chart.title}. The absolute difference between period totals is "
        f"{abs(difference):,.2f} t-CO₂e {percentage}."
    )
