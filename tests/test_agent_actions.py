import json
import unittest
from dataclasses import fields, is_dataclass
from typing import Any, get_args, get_type_hints

from pydantic import BaseModel, ValidationError


class RecordingOpenAIResponse:
    def __init__(self, arguments=None, error=None, tool_name="submit_agent_action"):
        self.arguments = arguments
        self.error = error
        self.tool_name = tool_name
        self.last_request = None
        self.chat = self.Chat(self)

    class Chat:
        def __init__(self, owner):
            self.completions = RecordingOpenAIResponse.Completions(owner)

    class Completions:
        def __init__(self, owner):
            self.owner = owner

        def create(self, **kwargs):
            self.owner.last_request = kwargs
            if self.owner.error is not None:
                raise self.owner.error
            function = type(
                "Function",
                (),
                {
                    "name": self.owner.tool_name,
                    "arguments": self.owner.arguments,
                },
            )()
            tool_call = type("ToolCall", (), {"function": function})()
            message = type("Message", (), {"tool_calls": [tool_call]})()
            choice = type("Choice", (), {"message": message})()
            return type("Completion", (), {"choices": [choice]})()


class AgentActionTest(unittest.TestCase):
    def test_call_tool_requires_a_name(self):
        from ai_native.agent.actions import AgentAction

        action = AgentAction(
            kind="call_tool",
            tool_name="list_analysis_bases",
            arguments={"company_id": "100"},
            reason="resolve site",
        )
        self.assertEqual(action.tool_name, "list_analysis_bases")
        with self.assertRaises(ValidationError):
            AgentAction(kind="call_tool", arguments={})

    def test_action_rejects_token_and_model_supplied_answer(self):
        from ai_native.agent.actions import AgentAction

        with self.assertRaises(ValidationError):
            AgentAction(kind="finish", answer="invented")
        with self.assertRaises(ValidationError):
            AgentAction(kind="call_tool", tool_name="x", token="secret")

    def test_action_rejects_sensitive_or_non_json_arguments(self):
        from ai_native.agent.actions import AgentAction

        for arguments in (
            {"filters": {"authorization": "Bearer secret"}},
            {"request": {"raw_payload": {"body": "secret"}}},
            {"result": {"emissionVolume": 12.3}},
            {"chart": {"series": [{"values": [12.3]}]}},
            {"value": object()},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValidationError):
                    AgentAction(kind="call_tool", tool_name="x", arguments=arguments)

    def test_safe_observation_has_no_business_values(self):
        from ai_native.agent.actions import SafeObservation

        value = SafeObservation(
            tool_name="resolve_analysis_base",
            status="success",
            facts={"base_id": "10185", "base_name": "親社拠点2"},
            result_count=1,
        )
        self.assertNotIn("emissionVolume", json.dumps(value.model_dump()))

    def test_safe_observation_rejects_sensitive_or_business_facts(self):
        from ai_native.agent.actions import SafeObservation

        for facts in (
            {"base_id": "10185", "token": "secret"},
            {"candidates": [{"base_id": "10185", "name": "親社拠点2", "raw_dto": {}}]},
            {"emissionVolume": 12.3},
            {"chartSpec": {"series": [{"values": [12.3]}]}},
        ):
            with self.subTest(facts=facts):
                with self.assertRaises(ValidationError):
                    SafeObservation(
                        tool_name="resolve_analysis_base",
                        status="success",
                        facts=facts,
                    )


class AgentStateTest(unittest.TestCase):
    def test_checkpoint_state_has_no_any_or_sensitive_fields(self):
        from ai_native.agent.state import AgentState

        seen: set[object] = set()

        def assert_safe(annotation):
            if annotation in seen:
                return
            seen.add(annotation)
            self.assertIsNot(annotation, Any)
            for child in get_args(annotation):
                assert_safe(child)

            if isinstance(annotation, type) and (
                issubclass(annotation, BaseModel) or is_dataclass(annotation)
            ):
                type_hints = get_type_hints(annotation, include_extras=True)
                names = type_hints if issubclass(annotation, BaseModel) else {
                    item.name: type_hints[item.name] for item in fields(annotation)
                }
                for name, child in names.items():
                    self.assertNotRegex(name.lower(), r"auth|token")
                    assert_safe(child)

        for name, annotation in get_type_hints(AgentState, include_extras=True).items():
            self.assertNotRegex(name.lower(), r"auth|token")
            assert_safe(annotation)


class BudgetTest(unittest.TestCase):
    def test_canonical_signature_is_stable_for_equivalent_arguments(self):
        from ai_native.agent.budgets import canonical_tool_signature

        first = canonical_tool_signature(
            "list_analysis_bases",
            {"company_id": "100", "filter": {"year": 2025, "scope": "1"}},
        )
        second = canonical_tool_signature(
            "list_analysis_bases",
            {"filter": {"scope": "1", "year": 2025}, "company_id": "100"},
        )

        self.assertEqual(first, second)

    def test_duplicate_and_exhausted_calls_are_distinct(self):
        from ai_native.agent.budgets import AgentBudgets, BudgetExceeded, RunCounters

        counters = RunCounters(AgentBudgets(planner=1, tools=1, clarifications=1))
        counters.consume_tool("tool:hash")
        with self.assertRaises(BudgetExceeded) as duplicate:
            counters.consume_tool("tool:hash")
        self.assertEqual(duplicate.exception.code, "duplicate_tool_call")
        counters.consume_planner()
        with self.assertRaises(BudgetExceeded) as exhausted:
            counters.consume_planner()
        self.assertEqual(exhausted.exception.code, "planner_budget_exhausted")


class PlannerTest(unittest.TestCase):
    def _plan(self, client, **overrides):
        from ai_native.agent.planner import OpenAIActionPlanner

        arguments = {
            "goal": "2025年の年間排出量",
            "trusted_context": {"company_id": "100", "year": 2025},
            "observations": [],
            "artifact_summaries": [],
            "remaining": {"planner": 7, "tools": 6},
            **overrides,
        }
        return OpenAIActionPlanner(client, "model").plan(**arguments)

    def test_planner_returns_validated_action(self):
        client = RecordingOpenAIResponse(
            '{"kind":"call_tool","tool_name":"list_analysis_bases",'
            '"arguments":{"company_id":"100"},"reason":"resolve site"}'
        )

        action = self._plan(
            client,
            goal="親社拠点2の月別排出量",
            trusted_context={"company_id": "100"},
        )

        self.assertEqual(
            (action.kind, action.tool_name),
            ("call_tool", "list_analysis_bases"),
        )
        request = client.last_request
        self.assertEqual(
            request["tool_choice"],
            {"type": "function", "function": {"name": "submit_agent_action"}},
        )
        self.assertEqual(
            [tool["function"]["name"] for tool in request["tools"]],
            ["submit_agent_action"],
        )

    def test_request_excludes_artifact_payload_and_token(self):
        client = RecordingOpenAIResponse(
            '{"kind":"finish","artifact_ids":["a1"]}'
        )

        self._plan(
            client,
            goal="show result",
            trusted_context={
                "company_id": "100",
                "locale": "ja",
                "Authorization": "Bearer sentinel-secret",
            },
            artifact_summaries=[
                {
                    "id": "a1",
                    "kind": "chart",
                    "payload": {"series": [{"values": [132360.075]}]},
                }
            ],
        )

        encoded = json.dumps(client.last_request)
        self.assertNotIn("values", encoded)
        self.assertNotIn("132360.075", encoded)
        self.assertNotIn("Bearer", encoded)
        self.assertNotIn("sentinel-secret", encoded)

    def test_invalid_action_is_a_retryable_model_error(self):
        from ai_native.gateway.errors import GatewayAgentError

        for raw in (
            "not-json",
            '{"kind":"call_tool","tool_name":"x","token":"secret"}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(GatewayAgentError) as raised:
                    self._plan(RecordingOpenAIResponse(raw))
                self.assertEqual(raised.exception.category, "model")
                self.assertEqual(raised.exception.code, "model_invalid_action")
                self.assertTrue(raised.exception.retryable)

    def test_provider_failure_falls_back_only_for_single_final_tool(self):
        from ai_native.gateway.errors import GatewayAgentError

        annual = self._plan(
            RecordingOpenAIResponse(error=RuntimeError("provider down"))
        )
        self.assertEqual(annual.kind, "call_tool")
        self.assertEqual(annual.tool_name, "get_annual_emission_summary")
        self.assertEqual(
            annual.arguments,
            {"company_id": "100", "year": 2025},
        )

        for goal in (
            "親社拠点2の月別排出量",
            "東京拠点と大阪拠点を比較",
            "2024年と2025年を比較",
        ):
            with self.subTest(goal=goal):
                with self.assertRaises(GatewayAgentError) as raised:
                    self._plan(
                        RecordingOpenAIResponse(error=RuntimeError("provider down")),
                        goal=goal,
                    )
                self.assertEqual(raised.exception.category, "model")
                self.assertEqual(raised.exception.code, "model_unavailable")
                self.assertTrue(raised.exception.retryable)

    def test_provider_failure_never_falls_back_during_replan(self):
        from ai_native.agent.actions import SafeObservation
        from ai_native.gateway.errors import GatewayAgentError

        with self.assertRaises(GatewayAgentError) as raised:
            self._plan(
                RecordingOpenAIResponse(error=RuntimeError("provider down")),
                observations=[
                    SafeObservation(
                        tool_name="get_annual_emission_summary",
                        status="success",
                        facts={"company_id": "100", "year": 2025},
                        artifact_id="a1",
                        result_count=1,
                    )
                ],
                artifact_summaries=[{"id": "a1", "kind": "summary"}],
            )

        self.assertEqual(raised.exception.code, "model_unavailable")
