import json
import tempfile
import unittest


class CmpfAgentTest(unittest.TestCase):
    def test_mock_gateway_returns_dashboard_summary(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        gateway = CmpfGateway(mode="mock")

        summary = gateway.get_dashboard_summary(company_id="cmpf-demo", year=2025)

        self.assertEqual(summary["company_id"], "cmpf-demo")
        self.assertEqual(summary["year"], 2025)
        self.assertIn("scope1_tco2e", summary)
        self.assertIn("scope2_tco2e", summary)
        self.assertIn("scope3_tco2e", summary)

    def test_http_gateway_uses_configured_base_url(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class FakeHttpClient:
            def get(self, url, params=None, headers=None):
                calls.append({"url": url, "params": params, "headers": headers})

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"ok": True}

                return Response()

        gateway = CmpfGateway(
            mode="http",
            carbon_api_base_url="http://localhost:8080/api",
            auth_token="token-1",
            http_client=FakeHttpClient(),
        )

        result = gateway.get_dashboard_summary(company_id="c-1", year=2026)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0]["url"], "http://localhost:8080/api/dashBoard/scope_total_emission_volume")
        self.assertEqual(
            calls[0]["params"],
            {
                "companyId": "c-1",
                "year": "2026",
                "startMonth": "2026-01",
                "endMonth": "2026-12",
                "scopeFlg": "",
            },
        )
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer token-1")

    def test_agent_uses_business_tool_for_emission_question(self):
        from ai_native.agent.graph import build_graph
        from ai_native.gateway.cmpf_client import CmpfGateway

        graph = build_graph(CmpfGateway(mode="mock"))

        result = graph.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "帮我查一下 cmpf-demo 公司 2025 年碳排放情况",
                    }
                ],
                "company_id": "cmpf-demo",
                "year": 2025,
            }
        )

        answer = result["messages"][-1].content
        self.assertIn("cmpf-demo", answer)
        self.assertIn("2025", answer)
        self.assertIn("Scope1", answer)
        self.assertIn("总排放", answer)

    def test_gateway_supports_scope_breakdown_endpoint(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class FakeHttpClient:
            def get(self, url, params=None, headers=None):
                calls.append({"url": url, "params": params, "headers": headers})

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"body": [{"scope": "1", "emissionVolume": 12.3}]}

                return Response()

        gateway = CmpfGateway(
            mode="http",
            carbon_api_base_url="http://localhost:8080",
            http_client=FakeHttpClient(),
        )

        result = gateway.get_scope_breakdown(company_id="100", year=2025)

        self.assertEqual(result["body"][0]["scope"], "1")
        self.assertEqual(calls[0]["url"], "http://localhost:8080/dashBoard/scope_emission_volume")
        self.assertEqual(
            calls[0]["params"],
            {
                "companyId": "100",
                "startMonth": "2025-01",
                "endMonth": "2025-12",
                "scopeFlg": "",
            },
        )

    def test_tool_registry_enforces_read_permission_and_writes_audit(self):
        from ai_native.gateway.audit import JsonlAuditLogger
        from ai_native.gateway.cmpf_client import CmpfGateway
        from ai_native.gateway.context import BusinessContext
        from ai_native.gateway.registry import ToolRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            audit_path = f"{tmpdir}/audit.jsonl"
            registry = ToolRegistry(
                gateway=CmpfGateway(mode="mock"),
                audit_logger=JsonlAuditLogger(audit_path),
            )

            denied = registry.execute(
                "get_emission_dashboard",
                {"company_id": "cmpf-demo", "year": 2025},
                BusinessContext(user_id="u-1", company_id="cmpf-demo", permissions=[]),
            )
            allowed = registry.execute(
                "get_emission_dashboard",
                {"company_id": "cmpf-demo", "year": 2025},
                BusinessContext(user_id="u-1", company_id="cmpf-demo", permissions=["cmpf:read"]),
            )

            self.assertFalse(denied.allowed)
            self.assertEqual(denied.error_code, "permission_denied")
            self.assertTrue(allowed.allowed)
            self.assertEqual(allowed.data["company_id"], "cmpf-demo")

            with open(audit_path, "r", encoding="utf-8") as audit_file:
                entries = [json.loads(line) for line in audit_file]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["tool_name"], "get_emission_dashboard")
            self.assertEqual(entries[0]["status"], "denied")
            self.assertEqual(entries[1]["status"], "success")

    def test_agent_can_route_scope_breakdown_question(self):
        from ai_native.agent.graph import build_graph
        from ai_native.gateway.cmpf_client import CmpfGateway

        graph = build_graph(CmpfGateway(mode="mock"))

        result = graph.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "查看 cmpf-demo 2025 年 Scope 明细",
                    }
                ],
                "company_id": "cmpf-demo",
                "year": 2025,
                "permissions": ["cmpf:read"],
            }
        )

        answer = result["messages"][-1].content
        self.assertIn("Scope 明细", answer)
        self.assertIn("Scope1", answer)
        self.assertIn("Scope2", answer)

    def test_format_company_info_answer_formats_dict_rows(self):
        from ai_native.agent.graph import _format_company_info_answer

        answer = _format_company_info_answer(
            {
                "source": "mock",
                "company_id": "cmpf-demo",
                "company_info": [
                    {
                        "company_name": "Mock Company",
                        "company_address": "123 Main St",
                        "company_phone": "123-456-7890",
                        "company_email": "info@example.com",
                    }
                ],
            }
        )

        self.assertIn("cmpf-demo", answer)
        self.assertIn("Mock Company", answer)
        self.assertIn("123 Main St", answer)
        self.assertIn("123-456-7890", answer)
        self.assertIn("info@example.com", answer)

    def test_api_exposes_health_tools_and_chat(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app

        client = TestClient(create_app())

        health = client.get("/health")
        tools = client.get("/tools")
        chat = client.post(
            "/chat",
            json={
                "message": "帮我查一下 cmpf-demo 公司 2025 年碳排放情况",
                "company_id": "cmpf-demo",
                "year": 2025,
                "permissions": ["cmpf:read"],
            },
        )

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(tools.status_code, 200)
        self.assertIn("get_emission_dashboard", [tool["name"] for tool in tools.json()["tools"]])
        self.assertEqual(chat.status_code, 200)
        self.assertIn("总排放", chat.json()["answer"])

    def test_api_serves_browser_chat_page(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app

        client = TestClient(create_app())

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("CMPF AI Native Agent", response.text)
        self.assertIn("/chat", response.text)


if __name__ == "__main__":
    unittest.main()
