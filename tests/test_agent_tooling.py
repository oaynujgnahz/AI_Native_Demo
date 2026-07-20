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

    def test_catalog_records_the_existing_cmpf_endpoints(self):
        from ai_native.gateway.tooling import build_enterprise_catalog

        catalog = build_enterprise_catalog()

        self.assertEqual(
            catalog.get("get_company_info").endpoint,
            "/user/company/getCompanyInfo",
        )
        self.assertEqual(
            catalog.get("compare_emission_periods_chart").endpoint,
            "/analysis/compareByDuration",
        )


class EnterpriseToolExecutorTest(unittest.TestCase):
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
