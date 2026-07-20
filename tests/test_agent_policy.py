from __future__ import annotations

import json
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ai_native.agent.actions import AgentAction
from ai_native.agent.budgets import AgentBudgets, RunCounters, canonical_tool_signature
from ai_native.gateway.auth import Principal
from pydantic import ValidationError


class RecordingRepository:
    def __init__(self, status: str = "running") -> None:
        self.status = status
        self.calls: list[str] = []

    def get_run(self, run_id: str):
        self.calls.append(run_id)
        return SimpleNamespace(id=run_id, status=self.status)


def make_state(**overrides):
    state = {
        "run_id": "run-1",
        "company_id": "100",
        "allowed_company_ids": ["100", "200"],
        "counters": RunCounters(AgentBudgets()),
    }
    state.update(overrides)
    return state


def make_context(
    *,
    company_id: str = "100",
    repository=None,
    cancelled: bool = False,
    deadline: datetime | None = None,
):
    from ai_native.gateway.runtime_context import RuntimeContext

    return RuntimeContext(
        principal=Principal(
            subject="subject-1",
            user_id="user-1",
            company_id=company_id,
            role_id="role-1",
        ),
        bearer_token="request-secret",
        deadline=deadline or datetime.now(timezone.utc) + timedelta(minutes=1),
        repository=repository or RecordingRepository(),
        is_cancelled=lambda: cancelled,
    )


def make_policy(**kwargs):
    from ai_native.gateway.policy import PolicyEngine
    from ai_native.gateway.tooling import build_enterprise_catalog

    return PolicyEngine(build_enterprise_catalog(), **kwargs)


class PolicyEngineTest(unittest.TestCase):
    def test_runtime_context_is_frozen_and_not_part_of_checkpoint_state(self):
        from ai_native.agent.state import AgentState
        from ai_native.gateway.runtime_context import RuntimeContext

        self.assertEqual(
            [item.name for item in fields(RuntimeContext)],
            ["principal", "bearer_token", "deadline", "repository", "is_cancelled"],
        )
        self.assertNotIn("runtime_context", AgentState.__annotations__)
        self.assertNotIn("bearer_token", AgentState.__annotations__)

    def test_runtime_context_repr_redacts_bearer_token(self):
        self.assertNotIn("request-secret", repr(make_context()))

    def test_unknown_tool_is_denied(self):
        result = make_policy().evaluate(
            AgentAction(kind="call_tool", tool_name="delete_company", arguments={}),
            make_state(),
            make_context(),
        )
        self.assertEqual(
            (result.status, result.error_code),
            ("denied", "tool_not_allowed"),
        )

    def test_company_outside_trusted_scope_is_denied_without_secret(self):
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "999"},
            ),
            make_state(company_id="100", allowed_company_ids=["100", "200"]),
            make_context(company_id="100"),
        )
        self.assertEqual(result.error_code, "company_forbidden")
        self.assertNotIn("request-secret", json.dumps(result.model_dump()))

    def test_approved_tool_has_server_approval_and_validated_arguments(self):
        result = make_policy(approval_id_factory=lambda: "approval-1").evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments={"company_id": "100", "year": 2025},
            ),
            state := make_state(),
            make_context(),
        )

        self.assertEqual(result.status, "approved")
        self.assertEqual(result.approval_id, "approval-1")
        self.assertEqual(
            result.validated_arguments,
            {"company_id": "100", "year": 2025},
        )
        self.assertEqual(state["counters"].tool_calls, 1)
        self.assertEqual(state["counters"].tool_signatures, [result.signature])
        self.assertNotIn("request-secret", json.dumps(result.model_dump()))

    def test_cancelled_precedes_tool_checks_and_has_no_side_effects(self):
        repository = RecordingRepository()
        state = make_state()

        result = make_policy().evaluate(
            AgentAction(kind="call_tool", tool_name="delete_company", arguments={}),
            state,
            make_context(repository=repository, cancelled=True),
        )

        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual(state["counters"].tool_calls, 0)
        self.assertEqual(repository.calls, [])

    def test_deadline_precedes_action_kind(self):
        result = make_policy().evaluate(
            AgentAction(kind="clarify", question="Which year?"),
            make_state(),
            make_context(deadline=datetime.now(timezone.utc) - timedelta(seconds=1)),
        )
        self.assertEqual(
            (result.status, result.error_code),
            ("denied", "request_timeout"),
        )

    def test_clarify_action_reserves_clarification_after_active_run_check(self):
        repository = RecordingRepository()
        state = make_state()
        result = make_policy().evaluate(
            AgentAction(
                kind="clarify",
                question="Which year?",
                missing_fields=["year"],
            ),
            state,
            make_context(repository=repository),
        )

        self.assertEqual(result.status, "clarification_required")
        self.assertEqual(result.question, "Which year?")
        self.assertEqual(result.missing_fields, ["year"])
        self.assertEqual(state["counters"].tool_calls, 0)
        self.assertEqual(state["counters"].clarification_calls, 1)
        self.assertEqual(repository.calls, ["run-1"])

    def test_exhausted_clarification_budget_precedes_active_run_check(self):
        repository = RecordingRepository(status="completed")
        counters = RunCounters(
            AgentBudgets(clarifications=0),
        )
        result = make_policy().evaluate(
            AgentAction(kind="clarify", question="Which year?"),
            make_state(counters=counters),
            make_context(repository=repository),
        )

        self.assertEqual(result.error_code, "clarification_budget_exhausted")
        self.assertEqual(counters.clarification_calls, 0)
        self.assertEqual(repository.calls, [])

    def test_inactive_run_does_not_reserve_clarification(self):
        repository = RecordingRepository(status="completed")
        counters = RunCounters(AgentBudgets())
        result = make_policy().evaluate(
            AgentAction(kind="clarify", question="Which year?"),
            make_state(counters=counters),
            make_context(repository=repository),
        )

        self.assertEqual(result.error_code, "run_inactive")
        self.assertEqual(counters.clarification_calls, 0)

    def test_finish_action_is_approved_without_tool_side_effect(self):
        repository = RecordingRepository()
        state = make_state()
        result = make_policy(approval_id_factory=lambda: "approval-finish").evaluate(
            AgentAction(kind="finish", artifact_ids=["artifact-1"]),
            state,
            make_context(repository=repository),
        )

        self.assertEqual(
            (result.status, result.approval_id),
            ("approved", "approval-finish"),
        )
        self.assertEqual(result.validated_arguments, {})
        self.assertEqual(state["counters"].tool_calls, 0)
        self.assertEqual(repository.calls, ["run-1"])

    def test_policy_decision_enforces_server_approval_marker(self):
        from ai_native.gateway.policy import PolicyDecision

        with self.assertRaises(ValidationError):
            PolicyDecision(status="approved")

    def test_unknown_tool_precedes_company_scope(self):
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="delete_company",
                arguments={"company_id": "999"},
            ),
            make_state(),
            make_context(),
        )
        self.assertEqual(result.error_code, "tool_not_allowed")

    def test_company_scope_precedes_argument_validation(self):
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments={"company_id": "999", "year": "2025"},
            ),
            make_state(),
            make_context(),
        )
        self.assertEqual(result.error_code, "company_forbidden")

    def test_strict_argument_validation_precedes_duplicate_and_has_no_side_effects(self):
        signature = canonical_tool_signature(
            "get_annual_emission_summary",
            {"company_id": "100", "year": "2025"},
        )
        counters = RunCounters(AgentBudgets(), tool_signatures=[signature])
        repository = RecordingRepository()

        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments={"company_id": "100", "year": "2025"},
            ),
            make_state(counters=counters),
            make_context(repository=repository),
        )

        self.assertEqual(result.error_code, "invalid_tool_arguments")
        self.assertEqual(counters.tool_calls, 0)
        self.assertEqual(repository.calls, [])

    def test_duplicate_signature_precedes_exhausted_budget(self):
        arguments = {"company_id": "100", "year": 2025}
        signature = canonical_tool_signature("get_annual_emission_summary", arguments)
        counters = RunCounters(
            AgentBudgets(tools=0),
            tool_signatures=[signature],
        )
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments=arguments,
            ),
            make_state(counters=counters),
            make_context(),
        )
        self.assertEqual(result.error_code, "duplicate_tool_call")
        self.assertEqual(counters.tool_calls, 0)

    def test_exhausted_budget_precedes_active_run_check(self):
        repository = RecordingRepository(status="completed")
        counters = RunCounters(AgentBudgets(tools=0))
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(counters=counters),
            make_context(repository=repository),
        )

        self.assertEqual(result.error_code, "tool_budget_exhausted")
        self.assertEqual(repository.calls, [])
        self.assertEqual(counters.tool_calls, 0)

    def test_inactive_run_is_denied_without_reserving_budget(self):
        repository = RecordingRepository(status="completed")
        counters = RunCounters(AgentBudgets())
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(counters=counters),
            make_context(repository=repository),
        )

        self.assertEqual(result.error_code, "run_inactive")
        self.assertEqual(repository.calls, ["run-1"])
        self.assertEqual(counters.tool_calls, 0)
        self.assertEqual(counters.tool_signatures, [])


if __name__ == "__main__":
    unittest.main()
