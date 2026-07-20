from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CompanyArguments(ToolArguments):
    company_id: str = Field(default=None, min_length=1, max_length=80)


class YearArguments(CompanyArguments):
    year: int = Field(default=None, ge=2000, le=2100)


class ScopeArguments(YearArguments):
    scope: Literal[1, 2, 3] = None


class OneBaseArguments(YearArguments):
    base_id: str = Field(default=None, min_length=1, max_length=80)
    base_name: str = Field(default=None, min_length=1, max_length=200)


class GroupingArguments(YearArguments):
    group_by: Literal["base", "area", "category"] = None


BaseId = Annotated[str, Field(min_length=1, max_length=80)]
BaseName = Annotated[str, Field(min_length=1, max_length=200)]


class MultipleBasesArguments(GroupingArguments):
    base_ids: list[BaseId] = Field(default=None, min_length=1, max_length=5)
    base_names: list[BaseName] = Field(default=None, min_length=1, max_length=5)


class BaseComparisonArguments(YearArguments):
    base_ids: list[BaseId] = Field(default=None, min_length=2, max_length=5)
    base_names: list[BaseName] = Field(default=None, min_length=2, max_length=5)


Month = str


class PeriodComparisonArguments(CompanyArguments):
    start_month: Month = Field(
        default=None, pattern=r"^20\d{2}(0[1-9]|1[0-2])$"
    )
    end_month: Month = Field(
        default=None, pattern=r"^20\d{2}(0[1-9]|1[0-2])$"
    )
    comparison_start_month: Month = Field(
        default=None, pattern=r"^20\d{2}(0[1-9]|1[0-2])$"
    )
    comparison_end_month: Month = Field(
        default=None, pattern=r"^20\d{2}(0[1-9]|1[0-2])$"
    )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    argument_model: type[BaseModel]
    required_permission: str
    risk: Literal["read_only"]
    endpoint: str
    handler_name: str


class ToolCatalog:
    def __init__(self, definitions: list[ToolDefinition]):
        self._items = {item.name: item for item in definitions}
        if len(self._items) != len(definitions):
            raise ValueError("duplicate tool name")

    def names(self) -> list[str]:
        return sorted(self._items)

    def get(self, name: str) -> ToolDefinition:
        return self._items[name]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": item.name,
                    "description": item.description,
                    "parameters": item.argument_model.model_json_schema(),
                },
            }
            for item in self._items.values()
        ]


def build_enterprise_catalog() -> ToolCatalog:
    definitions = [
        ToolDefinition(
            name="get_company_info",
            description="Get CMPF company profile information, such as name or address.",
            argument_model=CompanyArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/user/company/getCompanyInfo",
            handler_name="get_company_info",
        ),
        ToolDefinition(
            name="get_annual_emission_summary",
            description="Get total annual GHG emissions and Scope totals.",
            argument_model=YearArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/dashBoard/scope_total_emission_volume",
            handler_name="get_annual_emission_summary",
        ),
        ToolDefinition(
            name="get_scope_breakdown",
            description="Get annual Scope emission breakdown details.",
            argument_model=YearArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/dashBoard/scope_emission_volume",
            handler_name="get_scope_breakdown",
        ),
        ToolDefinition(
            name="get_scope_composition_chart",
            description="Create a pie chart for a specified Scope composition.",
            argument_model=ScopeArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/scopeSummary",
            handler_name="get_scope_composition_chart",
        ),
        ToolDefinition(
            name="get_monthly_emission_trend_chart",
            description="Create a monthly GHG emission trend line chart.",
            argument_model=ScopeArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/scopeEmissionForMonth",
            handler_name="get_monthly_emission_trend_chart",
        ),
        ToolDefinition(
            name="get_top_emission_activities_chart",
            description="Create a Top 10 emission activities bar chart.",
            argument_model=YearArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/topActivityItemsByEmission",
            handler_name="get_top_emission_activities_chart",
        ),
        ToolDefinition(
            name="list_analysis_bases",
            description=(
                "List which CMPF analysis sites exist for the selected company. "
                "Use only for list/available-site questions; never use for emissions, "
                "year, monthly, trend, or chart requests."
            ),
            argument_model=CompanyArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/baseInfoByCompanyGroup",
            handler_name="list_analysis_bases",
        ),
        ToolDefinition(
            name="get_base_emission_composition_chart",
            description=(
                "Create an emission composition pie chart grouped by site, area, "
                "or fixed site category."
            ),
            argument_model=GroupingArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/baseTypeEmission",
            handler_name="get_base_emission_composition_chart",
        ),
        ToolDefinition(
            name="get_base_monthly_emission_chart",
            description="Create a monthly emission trend for selected sites or site groups.",
            argument_model=MultipleBasesArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/baseTypeEmissionForMonth",
            handler_name="get_base_monthly_emission_chart",
        ),
        ToolDefinition(
            name="get_base_detail_composition_chart",
            description="Create a large-item emission composition pie chart for one site.",
            argument_model=OneBaseArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/baseLargeItemEmission",
            handler_name="get_base_detail_composition_chart",
        ),
        ToolDefinition(
            name="get_base_detail_monthly_chart",
            description="Create a monthly emission trend chart for one site.",
            argument_model=OneBaseArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/baseMonthEmission",
            handler_name="get_base_detail_monthly_chart",
        ),
        ToolDefinition(
            name="compare_base_emissions_chart",
            description="Compare emissions for two to five sites in a grouped bar chart.",
            argument_model=BaseComparisonArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/compareByBase",
            handler_name="compare_base_emissions_chart",
        ),
        ToolDefinition(
            name="compare_emission_periods_chart",
            description="Compare emissions between two explicit month ranges.",
            argument_model=PeriodComparisonArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/analysis/compareByDuration",
            handler_name="compare_emission_periods_chart",
        ),
    ]
    return ToolCatalog(definitions)


ENTERPRISE_TOOL_NAMES = frozenset(build_enterprise_catalog().names())
