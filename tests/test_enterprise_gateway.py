from __future__ import annotations

import json
import math
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fastapi.testclient import TestClient


class FakeAuthenticator:
    def authenticate(self, token: str):
        from ai_native.gateway.auth import Principal

        if token != "valid-token":
            raise ValueError("invalid token")
        return Principal(
            subject="subject-1",
            user_id="user-from-token",
            company_id="100",
            role_id="role-1",
            locale="ja",
        )


class FakeCmpfGateway:
    mode = "mock"

    def list_direct_child_companies(self, auth_token=None):
        return [{"value": "200", "label": "Child 200"}]

    def get_company_start_months(self, auth_token=None):
        return {"100": 4, "200": 1}

    def get_company_info(self, company_id, auth_token=None):
        return {"body": {"companyId": company_id, "companyName": f"Company {company_id}"}}

    def get_dashboard_summary(self, company_id, year, company_start_month=1, auth_token=None):
        return {
            "body": {
                "companyId": company_id,
                "year": year,
                "scope1": 10.0,
                "scope2": 20.0,
                "scope3": 70.0,
                "total": 100.0,
            }
        }

    def get_scope_breakdown(self, company_id, year, company_start_month=1, auth_token=None):
        return {"body": [{"scope": "Scope 1", "emissionVolume": 10.0}]}

    def get_scope_summary(self, company_id, year, company_start_month, scope, locale, auth_token=None):
        return {
            "body": [
                {"largeItem": "燃料", "emissionVolume": 12.5},
                {"largeItem": "電力", "emissionVolume": 20.0},
            ]
        }

    def get_scope_emission_for_month(
        self, company_id, year, company_start_month, scope, locale, auth_token=None
    ):
        return {
            "body": [
                {"activityMonth": "2025-04", "emissionVolume": 1.5},
                {"activityMonth": "2025-05", "emissionVolume": 2.5},
            ]
        }

    def get_top_activity_items_by_emission(
        self, company_id, year, company_start_month, locale, auth_token=None
    ):
        return {
            "body": {
                "items": [
                    {"emissionSourceName": "Gas", "emissionVolume": 50.0},
                    {"emissionSourceName": "Power", "emissionVolume": 40.0},
                ]
            }
        }


class ChartSpecTest(unittest.TestCase):
    def test_grouped_bar_accepts_multiple_safe_series(self):
        from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec

        chart = ChartSpec(
            chart_type="grouped_bar",
            title="2024 vs 2025",
            categories=["Scope 1", "Scope 2"],
            series=[
                ChartSeries(name="2024", values=[1.0, 2.0]),
                ChartSeries(name="2025", values=[1.5, 2.5]),
            ],
            source=ChartSource(
                tool_name="compare_emission_periods_chart",
                company_id="100",
                company_name="Company 100",
                period="2024-2025",
            ),
        )

        self.assertEqual(chart.chart_type, "grouped_bar")

    def test_rejects_more_than_100_total_points_across_series(self):
        from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec

        with self.assertRaises(ValueError):
            ChartSpec(
                chart_type="grouped_bar",
                title="Too many",
                categories=[str(index) for index in range(51)],
                series=[
                    ChartSeries(name="A", values=[1.0] * 51),
                    ChartSeries(name="B", values=[2.0] * 51),
                ],
                source=ChartSource(
                    tool_name="compare_base_emissions_chart",
                    company_id="100",
                    company_name="Company 100",
                    period="2025",
                ),
            )

    def test_rejects_non_finite_values(self):
        from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec

        source = ChartSource(
            tool_name="test",
            company_id="100",
            company_name="Company 100",
            period="2025",
        )
        with self.assertRaises(ValueError):
            ChartSpec(
                chart_type="line",
                title="Unsafe",
                categories=["2025-01"],
                series=[ChartSeries(name="Emission", values=[math.inf])],
                source=source,
            )

    def test_rejects_more_than_100_points(self):
        from ai_native.gateway.charts import ChartSeries, ChartSource, ChartSpec

        with self.assertRaises(ValueError):
            ChartSpec(
                chart_type="line",
                title="Too large",
                categories=[str(index) for index in range(101)],
                series=[ChartSeries(name="Emission", values=list(range(101)))],
                source=ChartSource(
                    tool_name="test",
                    company_id="100",
                    company_name="Company 100",
                    period="2025",
                ),
            )


class SecurityBoundaryTest(unittest.TestCase):
    def test_principal_is_built_from_required_keycloak_claims(self):
        from ai_native.gateway.auth import _principal_from_claims

        principal = _principal_from_claims(
            {
                "sub": "s-1",
                "userId": "u-1",
                "companyId": 100,
                "roleId": "r-1",
                "locale": "en-US",
            }
        )
        self.assertEqual(principal.user_id, "u-1")
        self.assertEqual(principal.company_id, "100")
        self.assertEqual(principal.locale, "en")

    def test_rate_limiter_enforces_per_minute_limit(self):
        from ai_native.gateway.limits import RequestLimitExceeded, RequestLimiter

        clock = [1000.0]
        limiter = RequestLimiter(per_minute=2, concurrent=2, clock=lambda: clock[0])
        with limiter.limit("user-1"):
            pass
        with limiter.limit("user-1"):
            pass
        with self.assertRaises(RequestLimitExceeded):
            with limiter.limit("user-1"):
                pass
        clock[0] += 61
        with limiter.limit("user-1"):
            pass

    def test_audit_repository_never_keeps_token_fields(self):
        from ai_native.gateway.repository import InMemoryConversationRepository

        repository = InMemoryConversationRepository()
        repository.write_audit(
            {
                "user_id": "u-1",
                "company_id": "100",
                "status": "success",
                "token": "secret",
                "auth_token": "secret-2",
                "raw_payload": {"private": True},
            }
        )
        self.assertNotIn("secret", json.dumps(repository._audits))
        self.assertNotIn("private", json.dumps(repository._audits))


class DocumentationContractTest(unittest.TestCase):
    def test_readme_documents_emission_analysis_loop(self):
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertIn("compare_emission_periods_chart", readme)
        self.assertIn("/analysis/compareByDuration", readme)
        self.assertIn("3 preparation", readme)


class CmpfAnalysisContractTest(unittest.TestCase):
    def test_mock_period_comparison_matches_real_cmpf_response_shape(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        payload = CmpfGateway(mode="mock").compare_emissions_by_duration({})

        self.assertEqual(len(payload["body"]), 2)
        self.assertIn("scopeAndTotalData", payload["body"][0])
        self.assertIn("total", payload["body"][0]["scopeAndTotalData"][0])

    def test_site_and_period_analysis_methods_use_fixed_cmpf_contracts(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"body": []}

        class HttpClient:
            def get(self, url, params=None, headers=None):
                calls.append(("GET", url, params, None, headers))
                return Response()

            def post(self, url, json=None, headers=None):
                calls.append(("POST", url, None, json, headers))
                return Response()

        gateway = CmpfGateway(
            mode="http", carbon_api_base_url="http://carbon", http_client=HttpClient()
        )
        gateway.list_analysis_bases("100", "ja", auth_token="token")
        gateway.get_base_type_emission({"companyId": "100"}, auth_token="token")
        gateway.get_base_type_emission_for_month(
            {"companyId": "100"}, auth_token="token"
        )
        gateway.get_base_large_item_emission(
            "100", "10", "202504", "202603", auth_token="token"
        )
        gateway.get_base_month_emission("100", "10", 2025, 4, auth_token="token")
        gateway.compare_emissions_by_base(
            {"companyId": "100", "baseId": ["10", "20"]}, auth_token="token"
        )
        gateway.compare_emissions_by_duration(
            {"companyId": "100", "startTime1": "2024/04"}, auth_token="token"
        )

        self.assertEqual(
            [(method, url.removeprefix("http://carbon")) for method, url, *_ in calls],
            [
                ("GET", "/analysis/baseInfoByCompanyGroup"),
                ("POST", "/analysis/baseTypeEmission"),
                ("POST", "/analysis/baseTypeEmissionForMonth"),
                ("GET", "/analysis/baseLargeItemEmission"),
                ("GET", "/analysis/baseMonthEmission"),
                ("POST", "/analysis/compareByBase"),
                ("POST", "/analysis/compareByDuration"),
            ],
        )
        self.assertEqual(calls[0][2], {"companyId": "100", "language": "0"})
        self.assertEqual(calls[1][3], {"companyId": "100"})
        self.assertEqual(calls[3][2]["baseId"], "10")
        self.assertEqual(calls[4][2]["companyStartMonth"], 4)
        self.assertEqual(calls[-1][4]["Authorization"], "Bearer token")

    def test_scope_summary_uses_existing_analysis_contract_and_fiscal_period(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class HttpClient:
            def get(self, url, params=None, headers=None):
                calls.append((url, params, headers))

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"body": []}

                return Response()

        gateway = CmpfGateway(
            mode="http", carbon_api_base_url="http://carbon", http_client=HttpClient()
        )
        gateway.get_scope_summary("100", 2025, 4, 3, "ja", auth_token="token")

        self.assertEqual(calls[0][0], "http://carbon/analysis/scopeSummary")
        self.assertEqual(calls[0][1]["startMonth"], "202504")
        self.assertEqual(calls[0][1]["endMonth"], "202603")
        self.assertEqual(calls[0][1]["clickLevel"], 0)
        self.assertEqual(calls[0][1]["language"], "0")


class AnalysisBaseResolverTest(unittest.TestCase):
    def setUp(self):
        from ai_native.gateway.base_resolver import AnalysisBaseResolver

        self.resolver = AnalysisBaseResolver()
        self.payload = {
            "body": [
                {"baseId": 10, "baseName": "東京拠点"},
                {"baseId": 20, "baseName": "OSAKA"},
            ]
        }

    def test_resolves_valid_id_and_exact_names(self):
        self.assertEqual(self.resolver.resolve(self.payload, base_id="10").name, "東京拠点")
        self.assertEqual(
            self.resolver.resolve(self.payload, base_name=" 東京拠点 ").base_id, "10"
        )
        self.assertEqual(self.resolver.resolve(self.payload, base_name="osaka").base_id, "20")

    def test_rejects_substring_unknown_id_and_missing_input(self):
        from ai_native.gateway.base_resolver import BaseResolutionError

        for arguments, expected in (
            ({"base_name": "東京"}, "base_not_found"),
            ({"base_id": "999"}, "base_not_found"),
            ({}, "base_required"),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(BaseResolutionError) as raised:
                    self.resolver.resolve(self.payload, **arguments)
                self.assertEqual(raised.exception.code, expected)

    def test_duplicate_name_is_ambiguous_and_candidates_are_safe(self):
        from ai_native.gateway.base_resolver import BaseResolutionError

        payload = {
            "body": [
                {"baseId": 10, "baseName": "共同拠点", "secret": "private"},
                {"baseId": 11, "baseName": "共同拠点", "secret": "private"},
            ]
        }
        with self.assertRaises(BaseResolutionError) as raised:
            self.resolver.resolve(payload, base_name="共同拠点")

        self.assertEqual(raised.exception.code, "base_ambiguous")
        self.assertEqual(
            [(item.base_id, item.name) for item in raised.exception.candidates],
            [("10", "共同拠点"), ("11", "共同拠点")],
        )
        self.assertNotIn("secret", repr(raised.exception.candidates))

    def test_candidate_list_is_capped_at_twenty(self):
        from ai_native.gateway.base_resolver import BaseResolutionError

        payload = {
            "body": [
                {"baseId": index, "baseName": f"拠点 {index}"}
                for index in range(25)
            ]
        }
        with self.assertRaises(BaseResolutionError) as raised:
            self.resolver.resolve(payload)

        self.assertEqual(len(raised.exception.candidates), 20)

    def test_company_id_filters_descendant_site_rows_when_present(self):
        from ai_native.gateway.base_resolver import BaseResolutionError

        payload = {
            "body": [
                {"baseId": 10, "baseName": "直接子会社拠点", "companyId": 100},
                {"baseId": 20, "baseName": "孫会社拠点", "companyId": 200},
            ]
        }

        self.assertEqual(
            self.resolver.resolve(payload, company_id="100", base_id="10").base_id,
            "10",
        )
        with self.assertRaises(BaseResolutionError) as raised:
            self.resolver.resolve(payload, company_id="100", base_id="20")
        self.assertEqual(raised.exception.code, "base_not_found")
        self.assertEqual(
            [item.base_id for item in self.resolver.list(payload, company_id="100")],
            ["10"],
        )


class ControlledAnalysisLoopTest(unittest.TestCase):
    def _service(
        self, *, base_rows, arguments, tool_name="get_base_detail_monthly_chart"
    ):
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.gateway.repository import InMemoryConversationRepository
        from ai_native.gateway.service import EnterpriseAgentService

        class Planner:
            def plan(self, user_text, registry, context=None):
                return ToolCallDecision(
                    tool_name=tool_name,
                    arguments=arguments,
                )

        class Gateway:
            def __init__(self):
                self.calls = []

            def list_direct_child_companies(self, auth_token=None):
                self.calls.append("list_direct_child_companies")
                return {"body": []}

            def get_company_info(self, company_id, auth_token=None):
                self.calls.append("get_company_info")
                return {"body": {"companyId": company_id, "companyName": "Company 100"}}

            def get_company_start_months(self, auth_token=None):
                self.calls.append("get_company_start_months")
                return {"body": {"100": 4}}

            def list_analysis_bases(self, company_id, locale, auth_token=None):
                self.calls.append("list_analysis_bases")
                return {"body": base_rows}

            def get_base_month_emission(
                self,
                company_id,
                base_id,
                year,
                company_start_month,
                auth_token=None,
            ):
                self.calls.append(f"get_base_month_emission:{base_id}")
                return {
                    "body": [
                        {"activityMonth": "202504", "emissionVolume": 10.0},
                        {"activityMonth": "202505", "emissionVolume": 12.0},
                    ]
                }

            def get_base_large_item_emission(
                self, company_id, base_id, start_month, end_month, auth_token=None
            ):
                self.calls.append(f"get_base_large_item_emission:{base_id}")
                return {
                    "body": [
                        {"largeItem": "燃料", "emissionVolume": 30.0},
                        {"largeItem": "電力", "emissionVolume": 70.0},
                    ]
                }

            def get_base_type_emission(self, payload, auth_token=None):
                self.calls.append("get_base_type_emission")
                return {
                    "body": [
                        {"baseGroupName": "工場", "emissionVolume": 70.0},
                        {"baseGroupName": "事務所", "emissionVolume": 30.0},
                    ]
                }

            def get_base_type_emission_for_month(self, payload, auth_token=None):
                self.calls.append("get_base_type_emission_for_month")
                return {
                    "body": [
                        {
                            "activityMonth": "202504",
                            "baseId": 10,
                            "baseName": "東京拠点",
                            "emissionVolume": 10.0,
                        },
                        {
                            "activityMonth": "202505",
                            "baseId": 10,
                            "baseName": "東京拠点",
                            "emissionVolume": 12.0,
                        },
                        {
                            "activityMonth": "202504",
                            "baseId": 20,
                            "baseName": "大阪拠点",
                            "emissionVolume": 8.0,
                        },
                        {
                            "activityMonth": "202505",
                            "baseId": 20,
                            "baseName": "大阪拠点",
                            "emissionVolume": 9.0,
                        },
                    ]
                }

            def compare_emissions_by_base(self, payload, auth_token=None):
                self.calls.append("compare_emissions_by_base")
                return {
                    "body": [
                        {
                            "baseId": 10,
                            "emissionTotal": "100",
                            "scope1Emission": "30",
                            "scope2Emission": "20",
                            "scope3Emission": "50",
                        },
                        {
                            "baseId": 20,
                            "emissionTotal": "80",
                            "scope1Emission": "20",
                            "scope2Emission": "20",
                            "scope3Emission": "40",
                        },
                    ]
                }

            def compare_emissions_by_duration(self, payload, auth_token=None):
                self.calls.append("compare_emissions_by_duration")
                return {
                    "body": [
                        {
                            "scopeAndTotalData": [
                                {
                                    "total": 100.0,
                                    "scope1Volume": 30.0,
                                    "scope2Volume": 20.0,
                                    "scope3Volume": 50.0,
                                }
                            ]
                        },
                        {
                            "scopeAndTotalData": [
                                {
                                    "total": 90.0,
                                    "scope1Volume": 25.0,
                                    "scope2Volume": 20.0,
                                    "scope3Volume": 45.0,
                                }
                            ]
                        },
                    ]
                }

        gateway = Gateway()
        repository = InMemoryConversationRepository()
        return (
            EnterpriseAgentService(gateway, repository, planner=Planner()),
            gateway,
            repository,
        )

    def _principal(self):
        from ai_native.gateway.auth import Principal

        return Principal(
            subject="subject-1",
            user_id="user-1",
            company_id="100",
            role_id="role-1",
            locale="ja",
        )

    def test_site_name_resolves_to_id_then_runs_monthly_analysis(self):
        service, gateway, repository = self._service(
            base_rows=[
                {"baseId": 10, "baseName": "東京拠点"},
                {"baseId": 20, "baseName": "大阪拠点"},
            ],
            arguments={"company_id": "100", "year": 2025, "base_name": "東京拠点"},
        )

        result = service.answer(
            principal=self._principal(),
            bearer_token="token",
            message="東京拠点の2025年月別排出量",
            context={},
        )

        self.assertEqual(
            gateway.calls,
            [
                "list_direct_child_companies",
                "get_company_info",
                "get_company_start_months",
                "list_analysis_bases",
                "get_base_month_emission:10",
            ],
        )
        self.assertEqual(result.chart.chart_type, "line")
        self.assertIn("東京拠点", result.chart.title)
        self.assertEqual(
            result.chart.source.tool_name, "get_base_detail_monthly_chart"
        )
        self.assertEqual(repository._audits[-1]["base_ids"], ["10"])

    def test_invalid_or_ambiguous_site_stops_before_analysis(self):
        from ai_native.gateway.service import RequestValidationError

        cases = [
            (
                [{"baseId": 10, "baseName": "東京拠点"}],
                {"year": 2025, "base_name": "東京"},
                "base_not_found",
            ),
            (
                [
                    {"baseId": 10, "baseName": "共同拠点"},
                    {"baseId": 11, "baseName": "共同拠点"},
                ],
                {"year": 2025, "base_name": "共同拠点"},
                "base_ambiguous",
            ),
            (
                [{"baseId": 10, "baseName": "東京拠点"}],
                {"year": 2025, "base_id": "999"},
                "base_not_found",
            ),
        ]
        for rows, arguments, expected in cases:
            with self.subTest(expected=expected):
                service, gateway, _ = self._service(
                    base_rows=rows, arguments=arguments
                )
                with self.assertRaises(RequestValidationError) as raised:
                    service.answer(
                        principal=self._principal(),
                        bearer_token="token",
                        message="拠点分析",
                        context={},
                    )
                self.assertEqual(str(raised.exception), expected)
                if expected == "base_ambiguous":
                    self.assertEqual(
                        raised.exception.candidates,
                        [
                            {"base_id": "10", "name": "共同拠点"},
                            {"base_id": "11", "name": "共同拠点"},
                        ],
                    )
                self.assertFalse(
                    any(call.startswith("get_base_month_emission") for call in gateway.calls)
                )

    def test_site_composition_and_grouped_site_tools(self):
        rows = [
            {"baseId": 10, "baseName": "東京拠点"},
            {"baseId": 20, "baseName": "大阪拠点"},
        ]
        cases = [
            (
                "get_base_detail_composition_chart",
                {"year": 2025, "base_name": "東京拠点"},
                "pie",
                "get_base_large_item_emission:10",
            ),
            (
                "get_base_emission_composition_chart",
                {"year": 2025, "group_by": "base"},
                "pie",
                "get_base_type_emission",
            ),
            (
                "get_base_monthly_emission_chart",
                {
                    "year": 2025,
                    "base_names": ["東京拠点", "大阪拠点"],
                    "group_by": "base",
                },
                "line",
                "get_base_type_emission_for_month",
            ),
        ]
        for tool_name, arguments, chart_type, final_call in cases:
            with self.subTest(tool_name=tool_name):
                service, gateway, _ = self._service(
                    base_rows=rows, arguments=arguments, tool_name=tool_name
                )
                result = service.answer(
                    principal=self._principal(),
                    bearer_token="token",
                    message=tool_name,
                    context={},
                )
                self.assertEqual(result.chart.chart_type, chart_type)
                self.assertIn(final_call, gateway.calls)

    def test_lists_analysis_sites_without_running_chart_tool(self):
        service, gateway, _ = self._service(
            base_rows=[
                {"baseId": 10, "baseName": "東京拠点"},
                {"baseId": 20, "baseName": "大阪拠点"},
            ],
            arguments={},
            tool_name="list_analysis_bases",
        )

        result = service.answer(
            principal=self._principal(),
            bearer_token="token",
            message="拠点一覧",
            context={},
        )

        self.assertIsNone(result.chart)
        self.assertIn("東京拠点", result.answer)
        self.assertEqual(gateway.calls.count("list_analysis_bases"), 1)

    def test_compares_resolved_sites_with_one_site_list_lookup(self):
        service, gateway, repository = self._service(
            base_rows=[
                {"baseId": 10, "baseName": "東京拠点"},
                {"baseId": 20, "baseName": "大阪拠点"},
            ],
            arguments={
                "year": 2025,
                "base_names": ["東京拠点", "大阪拠点"],
            },
            tool_name="compare_base_emissions_chart",
        )

        result = service.answer(
            principal=self._principal(),
            bearer_token="token",
            message="東京拠点と大阪拠点を比較",
            context={},
        )

        self.assertEqual(result.chart.chart_type, "grouped_bar")
        self.assertEqual(
            [series.name for series in result.chart.series],
            ["東京拠点", "大阪拠点"],
        )
        self.assertEqual(gateway.calls.count("list_analysis_bases"), 1)
        self.assertEqual(repository._audits[-1]["base_ids"], ["10", "20"])

    def test_compares_two_valid_periods_and_rejects_invalid_ranges(self):
        from ai_native.gateway.service import RequestValidationError

        valid = {
            "start_month": "202404",
            "end_month": "202503",
            "comparison_start_month": "202504",
            "comparison_end_month": "202603",
        }
        service, gateway, _ = self._service(
            base_rows=[],
            arguments=valid,
            tool_name="compare_emission_periods_chart",
        )
        result = service.answer(
            principal=self._principal(),
            bearer_token="token",
            message="期間比較",
            context={},
        )
        self.assertEqual(result.chart.chart_type, "grouped_bar")
        self.assertIn("10.00", result.answer)
        self.assertIn("-10.00%", result.answer)
        self.assertIn("compare_emissions_by_duration", gateway.calls)

        invalid_cases = [
            ({**valid, "end_month": "202403"}, "invalid_period"),
            (
                {
                    **valid,
                    "start_month": "202001",
                    "end_month": "202301",
                },
                "period_too_long",
            ),
        ]
        for arguments, expected in invalid_cases:
            with self.subTest(expected=expected):
                service, gateway, _ = self._service(
                    base_rows=[],
                    arguments=arguments,
                    tool_name="compare_emission_periods_chart",
                )
                with self.assertRaises(RequestValidationError) as raised:
                    service.answer(
                        principal=self._principal(),
                        bearer_token="token",
                        message="期間比較",
                        context={},
                    )
                self.assertEqual(str(raised.exception), expected)
                self.assertNotIn("compare_emissions_by_duration", gateway.calls)

    def test_rejects_more_than_five_sites_before_lookup(self):
        from ai_native.gateway.service import RequestValidationError

        service, gateway, _ = self._service(
            base_rows=[],
            arguments={
                "year": 2025,
                "base_names": [f"拠点 {index}" for index in range(6)],
            },
            tool_name="compare_base_emissions_chart",
        )
        with self.assertRaises(RequestValidationError) as raised:
            service.answer(
                principal=self._principal(),
                bearer_token="token",
                message="6拠点を比較",
                context={},
            )
        self.assertEqual(str(raised.exception), "too_many_bases")
        self.assertNotIn("list_analysis_bases", gateway.calls)


class SemanticReplanTest(unittest.TestCase):
    def test_list_selection_is_replanned_for_explicit_monthly_site_analysis(self):
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.gateway.auth import Principal
        from ai_native.gateway.repository import InMemoryConversationRepository
        from ai_native.gateway.service import EnterpriseAgentService

        class Planner:
            def __init__(self):
                self.forced_tool = None

            def plan(self, user_text, registry, context=None):
                return ToolCallDecision(tool_name="list_analysis_bases", arguments={})

            def plan_for_tool(
                self, user_text, registry, tool_name, context=None
            ):
                self.forced_tool = tool_name
                return ToolCallDecision(
                    tool_name=tool_name,
                    arguments={
                        "company_id": "533",
                        "year": 2025,
                        "base_name": "親社拠点2",
                    },
                )

        planner = Planner()
        service = EnterpriseAgentService(
            gateway=object(),
            repository=InMemoryConversationRepository(),
            planner=planner,
        )
        principal = Principal(
            subject="subject-1",
            user_id="user-1",
            company_id="533",
            role_id="role-1",
            locale="ja",
        )

        decision = service._plan(
            "親社拠点2の2025年月別排出量推移をグラフで表示して",
            {},
            principal,
        )

        self.assertEqual(planner.forced_tool, "get_base_detail_monthly_chart")
        self.assertEqual(decision.tool_name, "get_base_detail_monthly_chart")
        self.assertEqual(decision.arguments["base_name"], "親社拠点2")


class EnterpriseApiTest(unittest.TestCase):
    def setUp(self):
        from ai_native.api import create_app
        from ai_native.gateway.repository import InMemoryConversationRepository

        self.repository = InMemoryConversationRepository()
        self.client = TestClient(
            create_app(
                authenticator=FakeAuthenticator(),
                repository=self.repository,
                gateway=FakeCmpfGateway(),
                use_env_planner=False,
            )
        )
        self.headers = {"Authorization": "Bearer valid-token"}

    def test_conversation_requires_bearer_token(self):
        response = self.client.post("/v1/conversations", json={})
        self.assertEqual(response.status_code, 401)

    def test_public_oidc_config_exposes_only_browser_safe_values(self):
        with patch.dict(
            os.environ,
            {
                "CMPF_KEYCLOAK_ISSUER": "https://auth.example/realms/TEST",
                "CMPF_KEYCLOAK_CLIENT_ID": "CaM-js",
            },
            clear=False,
        ):
            response = self.client.get("/v1/public-config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["client_id"], "CaM-js")
        self.assertEqual(
            response.json()["authorization_endpoint"],
            "https://auth.example/realms/TEST/protocol/openid-connect/auth",
        )
        self.assertEqual(
            response.json()["token_endpoint"],
            "https://auth.example/realms/TEST/protocol/openid-connect/token",
        )
        self.assertNotIn("secret", json.dumps(response.json()).lower())

    def test_demo_uses_authorization_code_pkce_without_password_collection(self):
        html = Path("ai_native/demo.html").read_text(encoding="utf-8")

        self.assertIn("code_challenge_method", html)
        self.assertIn("S256", html)
        self.assertIn("crypto.subtle.digest", html)
        self.assertNotIn('type="password"', html)

    def test_demo_recreates_stale_in_memory_conversation_once(self):
        html = Path("ai_native/demo.html").read_text(encoding="utf-8")

        self.assertIn("conversation_not_found", html)
        self.assertIn("conversationId = null", html)
        self.assertIn("retryOnMissing", html)

    def test_identity_comes_only_from_token(self):
        response = self.client.post(
            "/v1/conversations",
            headers=self.headers,
            json={"user_id": "forged-user", "company_id": "999", "permissions": ["admin"]},
        )
        self.assertEqual(response.status_code, 201)
        conversation = self.repository.get_conversation(response.json()["id"])
        self.assertEqual(conversation.user_id, "user-from-token")
        self.assertEqual(conversation.company_id, "100")

    def test_cmpf_connection_check_uses_token_company_and_real_gateway(self):
        response = self.client.get("/v1/cmpf/connection", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "connected")
        self.assertEqual(response.json()["company_id"], "100")
        self.assertEqual(response.json()["company_name"], "Company 100")
        self.assertEqual(response.json()["direct_children"][0]["value"], "200")

    def test_cmpf_connection_reports_safe_upstream_stage_and_status(self):
        from ai_native.api import create_app
        from ai_native.gateway.repository import InMemoryConversationRepository

        class FailingGateway(FakeCmpfGateway):
            def get_company_info(self, company_id, auth_token=None):
                request = httpx.Request("GET", "http://user-api/user/company/getCompanyInfo")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        client = TestClient(
            create_app(
                authenticator=FakeAuthenticator(),
                repository=InMemoryConversationRepository(),
                gateway=FailingGateway(),
            )
        )
        response = client.get("/v1/cmpf/connection", headers=self.headers)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "cmpf_upstream_error",
                "stage": "get_company_info",
                "upstream_status": 401,
            },
        )
        self.assertNotIn("user-api", response.text)

    def test_stream_returns_safe_visualization_and_persists_it(self):
        created = self.client.post("/v1/conversations", headers=self.headers, json={}).json()
        response = self.client.post(
            f"/v1/conversations/{created['id']}/messages/stream",
            headers=self.headers,
            json={
                "message": "2025年の月別排出量推移グラフを表示して",
                "context": {"company_id": "200", "year": 2025, "locale": "ja"},
            },
        )
        self.assertEqual(response.status_code, 200)
        events = []
        for block in response.text.strip().split("\n\n"):
            lines = block.splitlines()
            event = lines[0].removeprefix("event: ")
            data = json.loads(lines[1].removeprefix("data: "))
            events.append((event, data))
        self.assertEqual(events[0][0], "status")
        self.assertIn("visualization", [event for event, _ in events])
        self.assertEqual(events[-1][0], "answer.completed")
        visualization = next(data for event, data in events if event == "visualization")
        self.assertEqual(visualization["chart_type"], "line")
        self.assertNotIn("formatter", json.dumps(visualization))

        history = self.client.get(
            f"/v1/conversations/{created['id']}/messages", headers=self.headers
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["messages"][-1]["chart"]["chart_type"], "line")

    def test_llm_selects_one_of_thirteen_enterprise_tools_for_natural_language(self):
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.api import create_app
        from ai_native.gateway.repository import InMemoryConversationRepository

        class RecordingPlanner:
            def __init__(self):
                self.tool_names = []

            def plan(self, user_text, registry, context=None):
                self.tool_names = [
                    item["function"]["name"] for item in registry.openai_tools()
                ]
                return ToolCallDecision(
                    tool_name="get_monthly_emission_trend_chart",
                    arguments={"company_id": "200", "year": 2025, "scope": 2},
                )

        class RecordingGateway(FakeCmpfGateway):
            def __init__(self):
                self.monthly_call = None

            def get_scope_emission_for_month(
                self, company_id, year, company_start_month, scope, locale, auth_token=None
            ):
                self.monthly_call = (company_id, year, scope, locale, auth_token)
                return super().get_scope_emission_for_month(
                    company_id, year, company_start_month, scope, locale, auth_token
                )

        planner = RecordingPlanner()
        gateway = RecordingGateway()
        client = TestClient(
            create_app(
                planner=planner,
                authenticator=FakeAuthenticator(),
                repository=InMemoryConversationRepository(),
                gateway=gateway,
            )
        )
        conversation = client.post(
            "/v1/conversations", headers=self.headers, json={}
        ).json()
        response = client.post(
            f"/v1/conversations/{conversation['id']}/messages/stream",
            headers=self.headers,
            json={
                "message": "去年每个月的变化大概是什么样？帮我画出来",
                "context": {"company_id": "100", "year": 2024, "locale": "ja"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(gateway.monthly_call, ("200", 2025, 2, "ja", "valid-token"))
        self.assertEqual(len(planner.tool_names), 13)
        self.assertIn("get_monthly_emission_trend_chart", planner.tool_names)
        self.assertIn("get_base_detail_monthly_chart", planner.tool_names)
        self.assertIn("compare_base_emissions_chart", planner.tool_names)
        self.assertIn("compare_emission_periods_chart", planner.tool_names)

    def test_new_tool_schemas_bound_site_lists_and_months(self):
        from ai_native.gateway.service import EnterpriseToolCatalog

        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in EnterpriseToolCatalog().openai_tools()
        }
        base_names = schemas["compare_base_emissions_chart"]["properties"][
            "base_names"
        ]
        self.assertEqual(base_names["minItems"], 2)
        self.assertEqual(base_names["maxItems"], 5)
        start_month = schemas["compare_emission_periods_chart"]["properties"][
            "start_month"
        ]
        self.assertEqual(start_month["pattern"], r"^20\d{2}(0[1-9]|1[0-2])$")

    def test_rule_fallback_distinguishes_site_and_period_analysis(self):
        from ai_native.gateway.service import _select_tool

        self.assertEqual(
            _select_tool("東京拠点の月別推移"),
            "get_base_detail_monthly_chart",
        )
        self.assertEqual(
            _select_tool("比较东京和大阪据点"),
            "compare_base_emissions_chart",
        )
        self.assertEqual(
            _select_tool("两个期间比较"),
            "compare_emission_periods_chart",
        )

    def test_invalid_llm_tool_name_falls_back_to_safe_rules(self):
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.api import create_app
        from ai_native.gateway.repository import InMemoryConversationRepository

        class InvalidPlanner:
            def plan(self, user_text, registry, context=None):
                return ToolCallDecision(
                    tool_name="delete_all_cmpf_data", arguments={"company_id": "200"}
                )

        client = TestClient(
            create_app(
                planner=InvalidPlanner(),
                authenticator=FakeAuthenticator(),
                repository=InMemoryConversationRepository(),
                gateway=FakeCmpfGateway(),
            )
        )
        conversation = client.post(
            "/v1/conversations", headers=self.headers, json={}
        ).json()
        response = client.post(
            f"/v1/conversations/{conversation['id']}/messages/stream",
            headers=self.headers,
            json={
                "message": "2025年の月別推移を表示",
                "context": {"company_id": "200", "year": 2025, "locale": "ja"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("get_monthly_emission_trend_chart", response.text)

    def test_ambiguous_site_returns_only_safe_candidates(self):
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.api import create_app
        from ai_native.gateway.repository import InMemoryConversationRepository

        class Planner:
            def plan(self, user_text, registry, context=None):
                return ToolCallDecision(
                    tool_name="get_base_detail_monthly_chart",
                    arguments={"year": 2025, "base_name": "共同拠点"},
                )

        class AmbiguousGateway(FakeCmpfGateway):
            def list_analysis_bases(self, company_id, locale, auth_token=None):
                return {
                    "body": [
                        {"baseId": 10, "baseName": "共同拠点", "secret": "private"},
                        {"baseId": 11, "baseName": "共同拠点", "secret": "private"},
                    ]
                }

        client = TestClient(
            create_app(
                planner=Planner(),
                authenticator=FakeAuthenticator(),
                repository=InMemoryConversationRepository(),
                gateway=AmbiguousGateway(),
            )
        )
        conversation = client.post(
            "/v1/conversations", headers=self.headers, json={}
        ).json()
        response = client.post(
            f"/v1/conversations/{conversation['id']}/messages/stream",
            headers=self.headers,
            json={"message": "共同拠点の月別推移", "context": {"locale": "ja"}},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "base_ambiguous")
        self.assertEqual(
            response.json()["detail"]["candidates"],
            [
                {"base_id": "10", "name": "共同拠点"},
                {"base_id": "11", "name": "共同拠点"},
            ],
        )
        self.assertNotIn("private", response.text)

    def test_company_outside_self_and_direct_children_is_forbidden(self):
        created = self.client.post("/v1/conversations", headers=self.headers, json={}).json()
        response = self.client.post(
            f"/v1/conversations/{created['id']}/messages/stream",
            headers=self.headers,
            json={
                "message": "会社情報を確認",
                "context": {"company_id": "999", "year": 2025, "locale": "ja"},
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_deleted_conversation_cannot_be_read(self):
        created = self.client.post("/v1/conversations", headers=self.headers, json={}).json()
        deleted = self.client.delete(
            f"/v1/conversations/{created['id']}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 204)
        missing = self.client.get(
            f"/v1/conversations/{created['id']}/messages", headers=self.headers
        )
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
