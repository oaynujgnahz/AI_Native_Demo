import json
import unittest
from dataclasses import fields, is_dataclass
from typing import Any, get_args, get_type_hints

from pydantic import BaseModel, ValidationError


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
