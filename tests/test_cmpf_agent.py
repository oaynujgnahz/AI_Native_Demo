import json
import tempfile
import unittest


class FakePlanner:
    def __init__(self, decision):
        self.decision = decision

    def plan(self, user_text, registry):
        return self.decision


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

    def test_gateway_uses_request_auth_token_when_provided(self):
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
            auth_token="env-token",
            http_client=FakeHttpClient(),
        )

        gateway.get_dashboard_summary(
            company_id="c-1",
            year=2026,
            auth_token="request-token",
        )

        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer request-token")

    def test_gateway_accepts_bearer_prefixed_request_token(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class FakeHttpClient:
            def get(self, url, params=None, headers=None):
                calls.append({"headers": headers})

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"ok": True}

                return Response()

        gateway = CmpfGateway(
            mode="http",
            carbon_api_base_url="http://localhost:8080/api",
            http_client=FakeHttpClient(),
        )

        gateway.get_dashboard_summary(
            company_id="c-1",
            year=2026,
            auth_token="Bearer request-token",
        )

        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer request-token")

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

    def test_gateway_uses_authoritative_company_info_endpoint(self):
        from ai_native.gateway.cmpf_client import CmpfGateway

        calls = []

        class FakeHttpClient:
            def get(self, url, params=None, headers=None):
                calls.append({"url": url, "params": params, "headers": headers})

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"body": {"companyName": "CMPF 528"}}

                return Response()

        gateway = CmpfGateway(
            mode="http",
            user_api_base_url="http://localhost:3333",
            http_client=FakeHttpClient(),
        )

        result = gateway.get_company_info(company_id="528", auth_token="user-token")

        self.assertEqual(result["body"]["companyName"], "CMPF 528")
        self.assertEqual(
            calls[0]["url"],
            "http://localhost:3333/user/company/getCompanyInfo",
        )
        self.assertEqual(calls[0]["params"], {"companyId": "528"})
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer user-token")

    def test_keycloak_token_client_uses_password_grant(self):
        from ai_native.gateway.keycloak import KeycloakTokenClient

        calls = []

        class FakeHttpClient:
            def post(self, url, data=None, headers=None):
                calls.append({"url": url, "data": data, "headers": headers})

                class Response:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {
                            "access_token": "access-token-1",
                            "expires_in": 300,
                            "token_type": "Bearer",
                        }

                return Response()

        client = KeycloakTokenClient(
            base_url="https://authdev.carbon-management.ntt.com/",
            realm="cmpf",
            client_id="cmpf-web",
            http_client=FakeHttpClient(),
        )

        token = client.password_login(username="alice", password="secret")

        self.assertEqual(token.access_token, "access-token-1")
        self.assertEqual(
            calls[0]["url"],
            "https://authdev.carbon-management.ntt.com/realms/cmpf/protocol/openid-connect/token",
        )
        self.assertEqual(calls[0]["data"]["grant_type"], "password")
        self.assertEqual(calls[0]["data"]["client_id"], "cmpf-web")
        self.assertEqual(calls[0]["data"]["username"], "alice")
        self.assertEqual(calls[0]["data"]["password"], "secret")

    def test_keycloak_token_client_raises_safe_authentication_error(self):
        from ai_native.gateway.keycloak import KeycloakAuthenticationError, KeycloakTokenClient

        class FakeHttpClient:
            def post(self, url, data=None, headers=None):
                class Response:
                    status_code = 401

                    def raise_for_status(self):
                        from httpx import HTTPStatusError, Request, Response as HttpxResponse

                        request = Request("POST", url)
                        response = HttpxResponse(401, request=request)
                        raise HTTPStatusError("401 Unauthorized", request=request, response=response)

                    def json(self):
                        return {
                            "error": "invalid_client",
                            "error_description": "Invalid client credentials",
                        }

                return Response()

        client = KeycloakTokenClient(
            base_url="https://authdev.carbon-management.ntt.com/auth",
            realm="TEST",
            client_id="cmpf-web",
            http_client=FakeHttpClient(),
        )

        with self.assertRaises(KeycloakAuthenticationError) as raised:
            client.password_login(username="alice", password="secret")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.error, "invalid_client")
        self.assertIn("Invalid client credentials", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

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

    def test_tool_registry_passes_context_auth_token_to_gateway(self):
        from ai_native.gateway.context import BusinessContext
        from ai_native.gateway.registry import ToolRegistry

        class FakeGateway:
            mode = "http"

            def get_dashboard_summary(self, company_id, year, auth_token=None):
                return {
                    "company_id": company_id,
                    "year": year,
                    "auth_token": auth_token,
                }

            def get_scope_breakdown(self, company_id, year, auth_token=None):
                return {}

            def get_company_info(self, company_id, auth_token=None):
                return {}

        registry = ToolRegistry(FakeGateway())

        result = registry.execute(
            "get_emission_dashboard",
            {"company_id": "cmpf-demo", "year": 2025},
            BusinessContext(
                user_id="u-1",
                company_id="cmpf-demo",
                permissions=["cmpf:read"],
                auth_token="request-token",
            ),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.data["auth_token"], "request-token")

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

    def test_agent_uses_llm_tool_calling_decision_when_available(self):
        from ai_native.agent.graph import build_graph
        from ai_native.agent.llm import ToolCallDecision
        from ai_native.gateway.cmpf_client import CmpfGateway

        graph = build_graph(
            CmpfGateway(mode="mock"),
            planner=FakePlanner(
                ToolCallDecision(
                    tool_name="get_scope_breakdown",
                    arguments={"company_id": "cmpf-demo", "year": 2025},
                )
            ),
        )

        result = graph.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "帮我看看这个公司的分类排放",
                    }
                ],
                "permissions": ["cmpf:read"],
            }
        )

        answer = result["messages"][-1].content
        self.assertIn("Scope 明细", answer)
        self.assertIn("Scope1", answer)

    def test_openai_tool_planner_parses_first_tool_call(self):
        from ai_native.agent.llm import OpenAIToolPlanner
        from ai_native.gateway.cmpf_client import CmpfGateway
        from ai_native.gateway.registry import ToolRegistry
        from ai_native.gateway.service import EnterpriseToolCatalog

        class Function:
            name = "get_company_info"
            arguments = '{"company_id": "cmpf-demo"}'

        class ToolCall:
            function = Function()

        class Message:
            tool_calls = [ToolCall()]
            content = None

        class Choice:
            message = Message()

        class Completion:
            choices = [Choice()]

        class Completions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return Completion()

        class Chat:
            def __init__(self):
                self.completions = Completions()

        class FakeOpenAIClient:
            def __init__(self):
                self.chat = Chat()

        planner = OpenAIToolPlanner(client=FakeOpenAIClient(), model="test-model")
        decision = planner.plan("查询公司信息", EnterpriseToolCatalog())

        self.assertEqual(decision.tool_name, "get_company_info")
        self.assertEqual(decision.arguments, {"company_id": "cmpf-demo"})
        system_prompt = planner.client.chat.completions.kwargs["messages"][0]["content"]
        self.assertIn("毎月", system_prompt)
        self.assertIn("get_monthly_emission_trend_chart", system_prompt)
        self.assertIn("会社の名称・住所", system_prompt)
        self.assertIn("拠点", system_prompt)
        self.assertIn("据点", system_prompt)
        self.assertIn("period comparison", system_prompt)
        self.assertIn(
            "NEVER use list_analysis_bases for emissions, year, monthly, trend, or chart requests",
            system_prompt,
        )

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

    def test_api_exposes_health_and_removes_insecure_legacy_chat(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app
        from ai_native.gateway.cmpf_client import CmpfGateway

        client = TestClient(
            create_app(use_env_planner=False, gateway=CmpfGateway(mode="mock"))
        )

        health = client.get("/health")
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        chat = client.post("/chat", json={"user_id": "forged"})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(chat.status_code, 404)

    def test_conversation_api_rejects_missing_authorization_header(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app
        from ai_native.gateway.cmpf_client import CmpfGateway

        client = TestClient(
            create_app(use_env_planner=False, gateway=CmpfGateway(mode="mock"))
        )

        response = client.post("/v1/conversations", json={})

        self.assertEqual(response.status_code, 401)

    def test_api_does_not_expose_password_grant_login(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app
        from ai_native.gateway.cmpf_client import CmpfGateway
        from ai_native.gateway.keycloak import KeycloakToken

        class FakeTokenClient:
            def password_login(self, username, password):
                self.username = username
                self.password = password
                return KeycloakToken(
                    access_token="login-token",
                    expires_in=300,
                    token_type="Bearer",
                )

        token_client = FakeTokenClient()
        client = TestClient(
            create_app(
                use_env_planner=False,
                token_client=token_client,
                gateway=CmpfGateway(mode="mock"),
            )
        )

        response = client.post(
            "/auth/login",
            json={"username": "alice", "password": "secret"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(hasattr(token_client, "username"))

    def test_removed_login_does_not_call_keycloak_password_grant(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app
        from ai_native.gateway.cmpf_client import CmpfGateway
        from ai_native.gateway.keycloak import KeycloakAuthenticationError

        class FakeTokenClient:
            def password_login(self, username, password):
                raise KeycloakAuthenticationError(
                    status_code=401,
                    error="invalid_grant",
                    description="Invalid user credentials",
                )

        client = TestClient(
            create_app(
                use_env_planner=False,
                token_client=FakeTokenClient(),
                gateway=CmpfGateway(mode="mock"),
            )
        )

        response = client.post(
            "/auth/login",
            json={"username": "alice", "password": "secret"},
        )

        self.assertEqual(response.status_code, 404)

    def test_api_serves_browser_chat_page(self):
        from fastapi.testclient import TestClient

        from ai_native.api import create_app
        from ai_native.gateway.cmpf_client import CmpfGateway

        client = TestClient(
            create_app(use_env_planner=False, gateway=CmpfGateway(mode="mock"))
        )

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("CMPF Enterprise Agent Gateway Demo", response.text)
        self.assertNotIn("/auth/login", response.text)
        self.assertIn("/v1/conversations", response.text)
        self.assertIn("echarts", response.text)


if __name__ == "__main__":
    unittest.main()
