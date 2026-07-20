import json
import unittest

from pydantic import ValidationError


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

    def test_safe_observation_has_no_business_values(self):
        from ai_native.agent.actions import SafeObservation

        value = SafeObservation(
            tool_name="resolve_analysis_base",
            status="success",
            facts={"base_id": "10185", "base_name": "親社拠点2"},
            result_count=1,
        )
        self.assertNotIn("emissionVolume", json.dumps(value.model_dump()))


class BudgetTest(unittest.TestCase):
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
