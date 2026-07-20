from __future__ import annotations

import json
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pydantic import ValidationError

from ai_native.agent.actions import AgentAction
from ai_native.agent.budgets import AgentBudgets, RunCounters, canonical_tool_signature
from ai_native.gateway.auth import Principal


class RecordingRepository:
    def __init__(
        self,
        status: str = "running",
        user_id: str | None = "user-1",
        company_id: str | None = "100",
        conversation_id: str | None = "conversation-1",
    ) -> None:
        self.status = status
        self.user_id = user_id
        self.company_id = company_id
        self.conversation_id = conversation_id
        self.calls: list[str] = []

    def get_run(self, run_id: str):
        self.calls.append(run_id)
        return SimpleNamespace(
            id=run_id,
            status=self.status,
            user_id=self.user_id,
            company_id=self.company_id,
            conversation_id=self.conversation_id,
        )


def make_state(**overrides):
    state = {
        "run_id": "run-1",
        "conversation_id": "conversation-1",
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

    def test_approved_tool_has_signed_approval_and_validated_arguments(self):
        policy = make_policy(approval_signing_key=b"a" * 32)
        result = policy.evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments={"company_id": "100", "year": 2025},
            ),
            state := make_state(),
            make_context(),
        )

        self.assertEqual(result.status, "approved")
        self.assertTrue(result.approval_id)
        self.assertTrue(policy.verify_approval(result))
        self.assertEqual(
            result.validated_arguments,
            {"company_id": "100", "year": 2025},
        )
        self.assertEqual(state["counters"].tool_calls, 1)
        self.assertEqual(state["counters"].tool_signatures, [result.signature])
        self.assertNotIn("request-secret", json.dumps(result.model_dump()))

    def test_validated_arguments_are_fresh_and_do_not_change_signed_json(self):
        policy = make_policy(approval_signing_key=b"a" * 32)
        result = policy.evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_annual_emission_summary",
                arguments={"company_id": "100", "year": 2025},
            ),
            make_state(),
            make_context(),
        )
        stored = result.validated_arguments_json

        first = result.validated_arguments
        first["company_id"] = "999"

        self.assertEqual(result.validated_arguments["company_id"], "100")
        self.assertEqual(result.validated_arguments_json, stored)
        self.assertTrue(policy.verify_approval(result))

    def test_approved_decision_is_frozen(self):
        policy = make_policy(approval_signing_key=b"a" * 32)
        result = policy.evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(),
            make_context(),
        )

        with self.assertRaises(ValidationError):
            result.tool_name = "list_analysis_bases"
        with self.assertRaises(AttributeError):
            result.missing_fields.append("company_id")

    def test_forged_or_modified_approval_fails_verification(self):
        from ai_native.gateway.policy import PolicyDecision

        policy = make_policy(approval_signing_key=b"a" * 32)
        result = policy.evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(),
            make_context(),
        )
        forged = PolicyDecision(
            status="approved",
            approval_id="caller-supplied",
            tool_name=result.tool_name,
            signature=result.signature,
            validated_arguments_json=result.validated_arguments_json,
        )

        self.assertFalse(policy.verify_approval(forged))
        for modified in (
            result.model_copy(update={"approval_id": "caller-supplied"}),
            result.model_copy(update={"tool_name": "list_analysis_bases"}),
            result.model_copy(update={"signature": "0" * 64}),
            result.model_copy(
                update={"validated_arguments_json": '{"company_id":"200"}'}
            ),
        ):
            with self.subTest(modified=modified):
                self.assertFalse(policy.verify_approval(modified))

    def test_cross_engine_approval_fails_verification(self):
        first = make_policy(approval_signing_key=b"a" * 32)
        second = make_policy(approval_signing_key=b"b" * 32)
        result = first.evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(),
            make_context(),
        )

        self.assertTrue(first.verify_approval(result))
        self.assertFalse(second.verify_approval(result))

    def test_caller_constructed_decision_rejects_sensitive_nested_keys(self):
        from ai_native.gateway.policy import PolicyDecision

        for arguments_json in (
            '{"token":"secret"}',
            '{"filters":{"authorization":"secret"}}',
            '{"filters":[{"raw_payload":{"private":true}}]}',
            '{"chart":{"series":[{"values":[1]}]}}',
            '{"emissionVolume":12.3}',
            '{"cookie":"secret"}',
        ):
            with self.subTest(arguments_json=arguments_json):
                with self.assertRaisesRegex(ValidationError, "sensitive"):
                    PolicyDecision(
                        status="approved",
                        approval_id="forged",
                        tool_name="get_company_info",
                        signature="0" * 64,
                        validated_arguments_json=arguments_json,
                    )

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
        self.assertEqual(result.missing_fields, ("year",))
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

        self.assertEqual(result.error_code, "active_run_conflict")
        self.assertEqual(counters.clarification_calls, 0)

    def test_finish_action_is_approved_without_tool_side_effect(self):
        repository = RecordingRepository()
        state = make_state()
        policy = make_policy(approval_signing_key=b"a" * 32)
        result = policy.evaluate(
            AgentAction(kind="finish", artifact_ids=["artifact-1"]),
            state,
            make_context(repository=repository),
        )

        self.assertEqual(result.status, "approved")
        self.assertTrue(policy.verify_approval(result))
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

        self.assertEqual(result.error_code, "active_run_conflict")
        self.assertEqual(repository.calls, ["run-1"])
        self.assertEqual(counters.tool_calls, 0)
        self.assertEqual(counters.tool_signatures, [])

    def test_run_ownership_mismatch_is_denied_without_reserving_budget(self):
        cases = (
            RecordingRepository(user_id="other-user"),
            RecordingRepository(company_id="200"),
            RecordingRepository(conversation_id="other-conversation"),
            RecordingRepository(user_id=None),
            RecordingRepository(company_id=None),
            RecordingRepository(conversation_id=None),
        )
        for repository in cases:
            with self.subTest(run=repository.__dict__):
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

                self.assertEqual(result.error_code, "active_run_conflict")
                self.assertEqual(counters.tool_calls, 0)
                self.assertEqual(counters.tool_signatures, [])

    def test_run_must_match_current_principal_company(self):
        counters = RunCounters(AgentBudgets())
        result = make_policy().evaluate(
            AgentAction(
                kind="call_tool",
                tool_name="get_company_info",
                arguments={"company_id": "100"},
            ),
            make_state(counters=counters),
            make_context(company_id="999"),
        )

        self.assertEqual(result.error_code, "active_run_conflict")
        self.assertEqual(counters.tool_calls, 0)


if __name__ == "__main__":
    unittest.main()
