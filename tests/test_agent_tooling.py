import json
import unittest

from pydantic import ValidationError


EXPECTED_TOOLS = {
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

EXPECTED_ENDPOINTS = {
    "get_company_info": "/user/company/getCompanyInfo",
    "get_annual_emission_summary": "/dashBoard/scope_total_emission_volume",
    "get_scope_breakdown": "/dashBoard/scope_emission_volume",
    "get_scope_composition_chart": "/analysis/scopeSummary",
    "get_monthly_emission_trend_chart": "/analysis/scopeEmissionForMonth",
    "get_top_emission_activities_chart": "/analysis/topActivityItemsByEmission",
    "list_analysis_bases": "/analysis/baseInfoByCompanyGroup",
    "get_base_emission_composition_chart": "/analysis/baseTypeEmission",
    "get_base_monthly_emission_chart": "/analysis/baseTypeEmissionForMonth",
    "get_base_detail_composition_chart": "/analysis/baseLargeItemEmission",
    "get_base_detail_monthly_chart": "/analysis/baseMonthEmission",
    "compare_base_emissions_chart": "/analysis/compareByBase",
    "compare_emission_periods_chart": "/analysis/compareByDuration",
}


class ToolCatalogTest(unittest.TestCase):
    def test_catalog_has_exactly_thirteen_read_only_tools(self):
        from ai_native.gateway.tooling import build_enterprise_catalog

        catalog = build_enterprise_catalog()

        self.assertEqual(set(catalog.names()), EXPECTED_TOOLS)
        self.assertTrue(
            all(catalog.get(name).risk == "read_only" for name in EXPECTED_TOOLS)
        )

    def test_arguments_and_model_schema_exclude_credentials(self):
        from ai_native.gateway.tooling import build_enterprise_catalog

        catalog = build_enterprise_catalog()

        with self.assertRaises(ValidationError):
            catalog.get("get_company_info").argument_model(
                company_id="100", token="secret"
            )
        encoded = str(catalog.openai_tools())
        self.assertNotIn("auth_token", encoded)
        self.assertNotIn("endpoint", encoded)

    def test_catalog_rejects_duplicate_names(self):
        from ai_native.gateway.tooling import (
            CompanyArguments,
            ToolCatalog,
            ToolDefinition,
        )

        item = ToolDefinition(
            name="duplicate",
            description="test",
            argument_model=CompanyArguments,
            required_permission="cmpf:read",
            risk="read_only",
            endpoint="/test",
            handler_name="test",
        )

        with self.assertRaisesRegex(ValueError, "duplicate tool name"):
            ToolCatalog([item, item])

    def test_catalog_records_all_endpoints_and_callable_handlers(self):
        from ai_native.gateway.executor import EnterpriseToolExecutor
        from ai_native.gateway.repository import InMemoryConversationRepository
        from ai_native.gateway.tooling import build_enterprise_catalog

        catalog = build_enterprise_catalog()
        executor = EnterpriseToolExecutor(
            object(), InMemoryConversationRepository(), catalog=catalog
        )

        self.assertEqual(set(EXPECTED_ENDPOINTS), EXPECTED_TOOLS)
        for tool_name, endpoint in EXPECTED_ENDPOINTS.items():
            with self.subTest(tool_name=tool_name):
                definition = catalog.get(tool_name)
                self.assertEqual(definition.endpoint, endpoint)
                self.assertTrue(callable(getattr(executor, definition.handler_name)))


class EnterpriseToolExecutorTest(unittest.TestCase):
    def _assert_invalid_before_gateway_call(self, tool_name, arguments):
        from ai_native.gateway.auth import Principal
        from ai_native.gateway.executor import EnterpriseToolExecutor
        from ai_native.gateway.repository import InMemoryConversationRepository
        from ai_native.gateway.service import RequestValidationError

        class Gateway:
            def __init__(self):
                self.calls = []

            def list_direct_child_companies(self, auth_token=None):
                self.calls.append("list_direct_child_companies")
                return {"body": []}

            def get_company_info(self, company_id, auth_token=None):
                self.calls.append("get_company_info")
                return {
                    "body": {
                        "companyId": company_id,
                        "companyName": "Company 100",
                    }
                }

            def get_company_start_months(self, auth_token=None):
                self.calls.append("get_company_start_months")
                return {"body": {"100": 1}}

            def get_dashboard_summary(
                self,
                company_id,
                year,
                company_start_month=1,
                auth_token=None,
            ):
                self.calls.append("get_dashboard_summary")
                return {
                    "body": {
                        "companyId": company_id,
                        "year": year,
                        "scope1": 1.0,
                        "scope2": 2.0,
                        "scope3": 3.0,
                        "total": 6.0,
                    }
                }

        gateway = Gateway()
        principal = Principal(
            subject="subject-1",
            user_id="user-1",
            company_id="100",
            role_id="role-1",
            locale="ja",
        )

        with self.assertRaises(RequestValidationError) as raised:
            EnterpriseToolExecutor(
                gateway, InMemoryConversationRepository()
            ).execute(
                tool_name=tool_name,
                arguments=arguments,
                principal=principal,
                bearer_token="sentinel-secret",
                message="invalid arguments",
                context={},
            )

        self.assertEqual(raised.exception.code, "invalid_tool_arguments")
        self.assertEqual(gateway.calls, [])

    def test_execute_rejects_extra_token_before_gateway_call(self):
        self._assert_invalid_before_gateway_call(
            "get_company_info", {"company_id": "100", "token": "secret"}
        )

    def test_execute_rejects_string_year_before_gateway_call(self):
        self._assert_invalid_before_gateway_call(
            "get_annual_emission_summary",
            {"company_id": "100", "year": "2025"},
        )

    def test_execute_rejects_invalid_scope_before_gateway_call(self):
        self._assert_invalid_before_gateway_call(
            "get_scope_composition_chart",
            {"company_id": "100", "year": 2025, "scope": 4},
        )

    def test_execute_returns_only_safe_metadata_and_compatibility_answer(self):
        from ai_native.gateway.auth import Principal
        from ai_native.gateway.executor import EnterpriseToolExecutor
        from ai_native.gateway.repository import InMemoryConversationRepository

        class Gateway:
            def list_direct_child_companies(self, auth_token=None):
                return {"body": []}

            def get_company_info(self, company_id, auth_token=None):
                return {
                    "body": {
                        "companyId": company_id,
                        "companyName": "Company 100",
                    }
                }

        principal = Principal(
            subject="subject-1",
            user_id="user-1",
            company_id="100",
            role_id="role-1",
            locale="ja",
        )
        result = EnterpriseToolExecutor(
            Gateway(), InMemoryConversationRepository()
        ).execute(
            tool_name="get_company_info",
            arguments={"company_id": "100"},
            principal=principal,
            bearer_token="sentinel-secret",
            message="会社情報",
            context={},
        )

        self.assertEqual(result.endpoint, "/user/company/getCompanyInfo")
        self.assertEqual(result.safe_facts["company_id"], "100")
        self.assertEqual(result.result_count, 1)
        self.assertIn("Company 100", result.answer)
        self.assertNotIn("sentinel-secret", json.dumps(result.as_safe_dict()))
