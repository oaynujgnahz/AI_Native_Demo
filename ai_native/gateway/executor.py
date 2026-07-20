from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from pydantic import ValidationError

from ai_native.gateway.auth import Principal
from ai_native.gateway.base_resolver import (
    AnalysisBase,
    AnalysisBaseResolver,
    BaseResolutionError,
)
from ai_native.gateway.charts import ChartSpec
from ai_native.gateway.errors import CompanyForbiddenError, RequestValidationError
from ai_native.gateway.execution_support import (
    base_comparison_chart as _base_comparison_chart,
    base_detail_composition_chart as _base_detail_composition_chart,
    base_detail_monthly_chart as _base_detail_monthly_chart,
    base_group_composition_chart as _base_group_composition_chart,
    base_group_monthly_chart as _base_group_monthly_chart,
    base_list_answer as _base_list_answer,
    body as _body,
    breakdown_answer as _breakdown_answer,
    chart_answer as _chart_answer,
    company_answer as _company_answer,
    composition_chart as _composition_chart,
    fiscal_months as _fiscal_months,
    group_by as _group_by,
    locale as _locale,
    monthly_chart as _monthly_chart,
    period_comparison_answer as _period_comparison_answer,
    period_comparison_chart as _period_comparison_chart,
    period_comparison_payload as _period_comparison_payload,
    scope_value as _scope_value,
    summary_answer as _summary_answer,
    top_chart as _top_chart,
    validated_period as _validated_period,
    year as _year,
)
from ai_native.gateway.observer import Artifact, ExecutionResult
from ai_native.gateway.tooling import ToolCatalog, build_enterprise_catalog


@dataclass
class _ExecutionContext:
    principal: Principal
    bearer_token: str
    locale: str
    company_id: str
    year: int | None
    scope: int | None
    company_name: str | None = None
    start_month: int | None = None
    bases_payload: Any = None
    bases_loaded: bool = False
    preparation_steps: int = 0

    def step(self) -> None:
        self.preparation_steps += 1
        if self.preparation_steps > 3:
            raise RequestValidationError("tool_loop_limit")


@dataclass(frozen=True)
class _HandlerResult:
    answer: str
    artifact: ChartSpec | None = None
    safe_facts: dict[str, Any] = field(default_factory=dict)
    result_count: int | None = None
    audit_details: dict[str, Any] = field(default_factory=dict)


class EnterpriseToolExecutor:
    def __init__(
        self,
        gateway: Any,
        repository: Any,
        catalog: ToolCatalog | None = None,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.catalog = catalog or build_enterprise_catalog()
        self.base_resolver = AnalysisBaseResolver()

    def execute(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        principal: Principal,
        bearer_token: str,
        message: str,
        context: Mapping[str, Any],
    ) -> ExecutionResult:
        definition = self.catalog.get(tool_name)
        try:
            validated = definition.argument_model.model_validate(arguments)
        except ValidationError as exc:
            raise RequestValidationError("invalid_tool_arguments") from exc
        validated_arguments = validated.model_dump(mode="python", exclude_none=True)
        company_id = str(
            validated_arguments.get("company_id")
            or context.get("company_id")
            or principal.company_id
        )
        year = _year(
            validated_arguments.get("year", context.get("year")), message
        )
        scope = _scope_value(validated_arguments.get("scope"), message)
        locale = _locale(context.get("locale") or principal.locale)

        self._require_company_access(principal, company_id, bearer_token)
        if tool_name not in {
            "get_company_info",
            "list_analysis_bases",
            "compare_emission_periods_chart",
        } and year is None:
            raise RequestValidationError("year_required")

        execution = _ExecutionContext(
            principal=principal,
            bearer_token=bearer_token,
            locale=locale,
            company_id=company_id,
            year=year,
            scope=scope,
        )
        execution.company_name = self._company_name_cached(execution)
        handler = getattr(self, definition.handler_name)
        handled = handler(validated_arguments, execution)
        safe_facts = {
            "company_id": company_id,
            "company_name": execution.company_name,
            **({"year": year} if year is not None else {}),
            **handled.safe_facts,
        }
        count = handled.result_count
        if count is None:
            count = len(handled.artifact.categories) if handled.artifact else 1
        artifact = self._artifact_from_handler(handled)
        return ExecutionResult(
            tool_name=tool_name,
            endpoint=definition.endpoint,
            safe_facts=safe_facts,
            artifact=artifact,
            result_count=count,
            audit_details=handled.audit_details,
        )

    def get_company_info(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        data = self.gateway.get_company_info(
            execution.company_id, auth_token=execution.bearer_token
        )
        return _HandlerResult(
            answer=_company_answer(
                data, execution.company_id, execution.locale
            )
        )

    def list_analysis_bases(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        payload = self._bases_payload_cached(execution)
        bases = self.base_resolver.list(payload, company_id=execution.company_id)
        return _HandlerResult(
            answer=_base_list_answer(
                bases, execution.company_name or execution.company_id, execution.locale
            ),
            safe_facts={
                "candidates": [
                    {"base_id": base.base_id, "name": base.name}
                    for base in bases
                ]
            },
            result_count=len(bases),
        )

    def get_annual_emission_summary(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        data = self.gateway.get_dashboard_summary(
            execution.company_id,
            execution.year,
            start_month,
            auth_token=execution.bearer_token,
        )
        return _HandlerResult(
            answer=_summary_answer(
                data,
                execution.company_name or execution.company_id,
                execution.year,
                execution.locale,
            )
        )

    def get_scope_breakdown(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        data = self.gateway.get_scope_breakdown(
            execution.company_id,
            execution.year,
            start_month,
            auth_token=execution.bearer_token,
        )
        return _HandlerResult(
            answer=_breakdown_answer(
                data,
                execution.company_name or execution.company_id,
                execution.year,
                execution.locale,
            )
        )

    def get_scope_composition_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        if execution.scope is None:
            raise RequestValidationError("scope_required")
        start_month = self._company_start_month_cached(execution)
        data = self.gateway.get_scope_summary(
            execution.company_id,
            execution.year,
            start_month,
            execution.scope,
            execution.locale,
            auth_token=execution.bearer_token,
        )
        chart = _composition_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            execution.year,
            execution.scope,
        )
        return _HandlerResult(
            answer=_chart_answer(chart, execution.locale),
            artifact=chart,
            safe_facts={"scope": str(execution.scope)},
        )

    def get_monthly_emission_trend_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        data = self.gateway.get_scope_emission_for_month(
            execution.company_id,
            execution.year,
            start_month,
            execution.scope,
            execution.locale,
            auth_token=execution.bearer_token,
        )
        chart = _monthly_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            execution.year,
        )
        return _HandlerResult(
            answer=_chart_answer(chart, execution.locale),
            artifact=chart,
            safe_facts=(
                {"scope": str(execution.scope)}
                if execution.scope is not None
                else {}
            ),
        )

    def get_top_emission_activities_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        data = self.gateway.get_top_activity_items_by_emission(
            execution.company_id,
            execution.year,
            start_month,
            execution.locale,
            auth_token=execution.bearer_token,
        )
        chart = _top_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            execution.year,
        )
        return _HandlerResult(
            answer=_chart_answer(chart, execution.locale), artifact=chart
        )

    def get_base_detail_monthly_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        base = self._resolve_base(
            execution,
            base_id=arguments.get("base_id"),
            base_name=arguments.get("base_name"),
        )
        data = self.gateway.get_base_month_emission(
            execution.company_id,
            base.base_id,
            execution.year,
            start_month,
            auth_token=execution.bearer_token,
        )
        chart = _base_detail_monthly_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            base,
            execution.year,
        )
        return self._base_chart_result(chart, base, execution.locale)

    def get_base_detail_composition_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        base = self._resolve_base(
            execution,
            base_id=arguments.get("base_id"),
            base_name=arguments.get("base_name"),
        )
        period_start, period_end = _fiscal_months(execution.year, start_month)
        data = self.gateway.get_base_large_item_emission(
            execution.company_id,
            base.base_id,
            period_start,
            period_end,
            auth_token=execution.bearer_token,
        )
        chart = _base_detail_composition_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            base,
            execution.year,
        )
        return _HandlerResult(
            answer=_chart_answer(chart, execution.locale),
            artifact=chart,
            safe_facts={"base_id": base.base_id, "base_name": base.name},
            audit_details={"base_ids": [base.base_id]},
        )

    def get_base_emission_composition_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        period_start, period_end = _fiscal_months(execution.year, start_month)
        group_by = _group_by(arguments.get("group_by"))
        payload = {
            "companyId": execution.company_id,
            "year": execution.year,
            "companyStartMonth": start_month,
            "startMonth": period_start,
            "endMonth": period_end,
            "baseList": [],
            "dynamicTab": group_by,
        }
        data = self.gateway.get_base_type_emission(
            payload, auth_token=execution.bearer_token
        )
        chart = _base_group_composition_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            execution.year,
            group_by,
        )
        return _HandlerResult(
            answer=_chart_answer(chart, execution.locale),
            artifact=chart,
        )

    def get_base_monthly_emission_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        bases = self._resolve_bases(
            execution,
            base_ids=arguments.get("base_ids"),
            base_names=arguments.get("base_names"),
            minimum=1,
        )
        group_by = _group_by(arguments.get("group_by"))
        period_start, period_end = _fiscal_months(execution.year, start_month)
        payload = {
            "companyId": execution.company_id,
            "year": execution.year,
            "companyStartMonth": start_month,
            "startMonth": period_start,
            "endMonth": period_end,
            "baseList": [base.base_id for base in bases],
            "dynamicTab": group_by,
        }
        data = self.gateway.get_base_type_emission_for_month(
            payload, auth_token=execution.bearer_token
        )
        chart = _base_group_monthly_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            bases,
            execution.year,
        )
        return self._bases_chart_result(
            chart, bases, execution.locale, {"group_by": group_by}
        )

    def compare_base_emissions_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        bases = self._resolve_bases(
            execution,
            base_ids=arguments.get("base_ids"),
            base_names=arguments.get("base_names"),
            minimum=2,
        )
        payload = {
            "companyId": execution.company_id,
            "aimYear": str(execution.year),
            "companyStartMonth": start_month,
            "baseId": [base.base_id for base in bases],
        }
        data = self.gateway.compare_emissions_by_base(
            payload, auth_token=execution.bearer_token
        )
        chart = _base_comparison_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            bases,
            execution.year,
        )
        return self._bases_chart_result(chart, bases, execution.locale)

    def compare_emission_periods_chart(
        self, arguments: dict[str, Any], execution: _ExecutionContext
    ) -> _HandlerResult:
        start_month = self._company_start_month_cached(execution)
        first = _validated_period(
            arguments.get("start_month"), arguments.get("end_month")
        )
        second = _validated_period(
            arguments.get("comparison_start_month"),
            arguments.get("comparison_end_month"),
        )
        payload = _period_comparison_payload(
            execution.company_id, first, second, start_month
        )
        data = self.gateway.compare_emissions_by_duration(
            payload, auth_token=execution.bearer_token
        )
        chart = _period_comparison_chart(
            data,
            execution.company_id,
            execution.company_name or execution.company_id,
            first,
            second,
        )
        return _HandlerResult(
            answer=_period_comparison_answer(chart, execution.locale),
            artifact=chart,
            safe_facts={
                "period": f"{first[0]}-{first[1]};{second[0]}-{second[1]}",
            },
            audit_details={
                "period_start": first[0],
                "period_end": first[1],
                "comparison_period": f"{second[0]}-{second[1]}",
            },
        )

    def _base_chart_result(
        self, chart: ChartSpec, base: AnalysisBase, locale: str
    ) -> _HandlerResult:
        return _HandlerResult(
            answer=_chart_answer(chart, locale),
            artifact=chart,
            safe_facts={"base_id": base.base_id, "base_name": base.name},
            audit_details={"base_ids": [base.base_id]},
        )

    def _bases_chart_result(
        self,
        chart: ChartSpec,
        bases: list[AnalysisBase],
        locale: str,
        safe_facts: dict[str, Any] | None = None,
    ) -> _HandlerResult:
        base_facts = [
            {"base_id": base.base_id, "name": base.name} for base in bases
        ]
        return _HandlerResult(
            answer=_chart_answer(chart, locale),
            artifact=chart,
            safe_facts={"candidates": base_facts},
            audit_details={"base_ids": [base.base_id for base in bases]},
        )

    def _artifact_from_handler(self, handled: _HandlerResult) -> Artifact:
        if handled.artifact is not None:
            return Artifact(
                id=handled.artifact.chart_id,
                kind="chart",
                payload={
                    "chart": handled.artifact.model_dump(mode="json"),
                    "answer": handled.answer,
                },
            )
        return Artifact(
            id=str(uuid4()),
            kind="answer",
            payload={"answer": handled.answer},
        )

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

    def _company_start_month_cached(self, execution: _ExecutionContext) -> int:
        if execution.start_month is None:
            execution.step()
            payload = _body(
                self.gateway.get_company_start_months(
                    auth_token=execution.bearer_token
                )
            )
            value = None
            if isinstance(payload, dict):
                value = payload.get(execution.company_id)
                if value is None and execution.company_id.isdigit():
                    value = payload.get(int(execution.company_id))
            execution.start_month = int(value) if value else 1
            self._write_preparation_audit(
                execution, "resolve_fiscal_start_month", "success", 1
            )
        return execution.start_month

    def _company_name_cached(self, execution: _ExecutionContext) -> str:
        if execution.company_name is None:
            execution.step()
            payload = _body(
                self.gateway.get_company_info(
                    execution.company_id, auth_token=execution.bearer_token
                )
            )
            if isinstance(payload, dict):
                execution.company_name = str(
                    payload.get("companyName")
                    or payload.get("company_name")
                    or execution.company_id
                )
            else:
                execution.company_name = execution.company_id
            self._write_preparation_audit(
                execution, "resolve_company", "success", 1
            )
        return execution.company_name

    def _bases_payload_cached(self, execution: _ExecutionContext) -> Any:
        if not execution.bases_loaded:
            execution.step()
            execution.bases_payload = self.gateway.list_analysis_bases(
                execution.company_id,
                execution.locale,
                auth_token=execution.bearer_token,
            )
            execution.bases_loaded = True
            self._write_preparation_audit(
                execution,
                "list_analysis_bases",
                "success",
                len(
                    self.base_resolver.list(
                        execution.bases_payload, company_id=execution.company_id
                    )
                ),
            )
        return execution.bases_payload

    def _resolve_base(
        self,
        execution: _ExecutionContext,
        *,
        base_id: Any = None,
        base_name: Any = None,
    ) -> AnalysisBase:
        payload = self._bases_payload_cached(execution)
        try:
            base = self.base_resolver.resolve(
                payload,
                company_id=execution.company_id,
                base_id=base_id,
                base_name=base_name,
            )
        except BaseResolutionError as exc:
            self._write_preparation_audit(
                execution, "resolve_base", "failed", len(exc.candidates)
            )
            raise RequestValidationError(
                exc.code,
                candidates=[
                    {"base_id": item.base_id, "name": item.name}
                    for item in exc.candidates
                ],
            ) from exc
        self._write_preparation_audit(
            execution, "resolve_base", "success", 1
        )
        return base

    def _resolve_bases(
        self,
        execution: _ExecutionContext,
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
                execution, base_id=base_id, base_name=base_name
            )
            for base_id, base_name in values
        ]
        if len({base.base_id for base in bases}) != len(bases):
            raise RequestValidationError("base_ambiguous")
        return bases

    def _write_preparation_audit(
        self,
        execution: _ExecutionContext,
        tool_name: str,
        status: str,
        result_count: int,
    ) -> None:
        self.repository.write_audit(
            {
                "user_id": execution.principal.user_id,
                "company_id": execution.company_id,
                "tool_name": tool_name,
                "status": status,
                "result_count": result_count,
            }
        )
