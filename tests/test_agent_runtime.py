from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver

from ai_native.agent.actions import AgentAction
from ai_native.agent.budgets import AgentBudgets
from ai_native.gateway.auth import Principal
from ai_native.gateway.errors import GatewayAgentError
from ai_native.gateway.observer import Artifact, ExecutionResult


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        return action


class RecordingRepository:
    def __init__(self, *, status="running"):
        self.status = status
        self.audits = []

    def get_run(self, run_id):
        return SimpleNamespace(
            id=run_id,
            status=self.status,
            user_id="user-1",
            company_id="100",
            conversation_id=run_id,
        )

    def write_audit(self, entry):
        self.audits.append(dict(entry))


class RecordingExecutor:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        tool_name = kwargs["tool_name"]
        if tool_name == "list_analysis_bases":
            return ExecutionResult(
                tool_name=tool_name,
                endpoint="/analysis/baseInfoByCompanyGroup",
                safe_facts={
                    "company_id": "100",
                    "candidates": [
                        {"base_id": "10185", "name": "親社拠点2"}
                    ],
                },
                artifact=Artifact(
                    id="bases-1",
                    kind="answer",
                    payload={"answer": "親社拠点2（ID: 10185）"},
                ),
                result_count=1,
            )
        return ExecutionResult(
            tool_name=tool_name,
            endpoint="/analysis/baseMonthEmission",
            safe_facts={
                "company_id": "100",
                "base_id": "10185",
                "base_name": "親社拠点2",
                "year": 2025,
            },
            artifact=Artifact(
                id="chart-1",
                kind="chart",
                payload={
                    "answer": "親社拠点2の2025年月別排出量を表示します。",
                    "chart": {
                        "chart_id": "chart-1",
                        "chart_type": "line",
                        "title": "親社拠点2 2025 Monthly GHG Emissions",
                        "categories": ["202501"],
                        "series": [{"name": "親社拠点2", "values": [12.5]}],
                        "source": {
                            "tool_name": tool_name,
                            "company_id": "100",
                            "company_name": "Company 100",
                            "period": "2025",
                        },
                    },
                },
            ),
            result_count=1,
        )


class NodeRecorder:
    def __init__(self):
        self.nodes = []

    def __call__(self, node):
        self.nodes.append(node)


def make_runtime_context(
    *,
    repository=None,
    cancelled=False,
    deadline=None,
    token="sentinel-runtime-token",
):
    from ai_native.gateway.runtime_context import RuntimeContext

    return RuntimeContext(
        principal=Principal(
            subject="subject-1",
            user_id="user-1",
            company_id="100",
            role_id="role-1",
            locale="ja",
        ),
        bearer_token=token,
        deadline=deadline or datetime.now(timezone.utc) + timedelta(minutes=1),
        repository=repository or RecordingRepository(),
        is_cancelled=lambda: cancelled,
    )


def build_test_runtime(planner, *, executor=None, budgets=None, checkpointer=None):
    from ai_native.agent.runtime import build_agent_runtime
    from ai_native.gateway.policy import PolicyEngine
    from ai_native.gateway.tooling import build_enterprise_catalog

    recorder = NodeRecorder()
    catalog = build_enterprise_catalog()
    runtime = build_agent_runtime(
        planner=planner,
        policy_engine=PolicyEngine(
            catalog,
            approval_signing_key=b"runtime-test-signing-key",
        ),
        executor=executor or RecordingExecutor(),
        budgets=budgets or AgentBudgets(),
        checkpointer=checkpointer or InMemorySaver(),
        on_node=recorder,
    )
    return runtime, recorder


def tool_action(tool_name="get_company_info", arguments=None):
    return AgentAction(
        kind="call_tool",
        tool_name=tool_name,
        arguments=arguments or {"company_id": "100"},
        reason="test",
    )


class AgentRuntimeTest(unittest.TestCase):
    def test_observation_replans_before_finish(self):
        planner = ScriptedPlanner(
            [
                tool_action("list_analysis_bases"),
                tool_action(
                    "get_base_detail_monthly_chart",
                    {"company_id": "100", "base_id": "10185", "year": 2025},
                ),
                AgentAction(kind="finish", artifact_ids=["chart-1"]),
            ]
        )
        executor = RecordingExecutor()
        runtime, recorder = build_test_runtime(planner, executor=executor)

        result = runtime.invoke(
            make_runtime_context(), "親社拠点2の2025年月別排出量"
        )

        self.assertEqual(
            recorder.nodes,
            [
                "planner",
                "policy",
                "executor",
                "observer",
                "planner",
                "policy",
                "executor",
                "observer",
                "planner",
                "responder",
            ],
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.artifact_ids, ("chart-1",))
        self.assertEqual(result.chart.chart_id, "chart-1")
        self.assertNotIn("values", json.dumps(planner.calls, default=str))
        self.assertEqual(len(executor.calls), 2)

    def test_clarifier_interrupts_and_resume_replans_with_new_request_context(self):
        planner = ScriptedPlanner(
            [
                AgentAction(
                    kind="clarify",
                    question="対象年度を指定してください。",
                    missing_fields=["year"],
                ),
                tool_action(
                    "get_base_detail_monthly_chart",
                    {"company_id": "100", "base_id": "10185", "year": 2025},
                ),
                AgentAction(kind="finish", artifact_ids=["chart-1"]),
            ]
        )
        runtime, recorder = build_test_runtime(planner)

        interrupted = runtime.invoke(make_runtime_context(token="old-token"), "月別排出量")

        self.assertEqual(interrupted.status, "clarification_required")
        self.assertEqual(interrupted.question, "対象年度を指定してください。")
        self.assertEqual(interrupted.missing_fields, ("year",))
        self.assertEqual(recorder.nodes, ["planner", "policy", "clarifier"])

        resumed = runtime.resume(
            make_runtime_context(token="refreshed-token"),
            interrupted.run_id,
            "2025年",
        )

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(runtime.executor.calls[-1]["bearer_token"], "refreshed-token")
        self.assertIn("2025年", planner.calls[-1]["goal"])

    def test_policy_denial_routes_to_terminal_error(self):
        runtime, recorder = build_test_runtime(
            ScriptedPlanner([tool_action("delete_company")])
        )

        result = runtime.invoke(make_runtime_context(), "delete")

        self.assertEqual((result.status, result.error_code), ("failed", "tool_not_allowed"))
        self.assertEqual(recorder.nodes, ["planner", "policy", "terminal_error"])

    def test_duplicate_call_is_stopped_before_second_execution(self):
        executor = RecordingExecutor()
        runtime, _ = build_test_runtime(
            ScriptedPlanner(
                [
                    tool_action("get_company_info"),
                    tool_action("get_company_info"),
                ]
            ),
            executor=executor,
        )

        result = runtime.invoke(make_runtime_context(), "会社情報")

        self.assertEqual((result.status, result.error_code), ("exhausted", "duplicate_tool_call"))
        self.assertEqual(len(executor.calls), 1)

    def test_each_budget_has_a_stable_terminal_error(self):
        cases = [
            (
                "planner",
                AgentBudgets(planner=0),
                ScriptedPlanner([]),
                make_runtime_context(),
                "planner_budget_exhausted",
            ),
            (
                "tools",
                AgentBudgets(tools=0),
                ScriptedPlanner([tool_action()]),
                make_runtime_context(),
                "tool_budget_exhausted",
            ),
            (
                "clarifications",
                AgentBudgets(clarifications=0),
                ScriptedPlanner(
                    [AgentAction(kind="clarify", question="Which year?")]
                ),
                make_runtime_context(),
                "clarification_budget_exhausted",
            ),
            (
                "deadline",
                AgentBudgets(),
                ScriptedPlanner([tool_action()]),
                make_runtime_context(
                    deadline=datetime.now(timezone.utc) - timedelta(seconds=1)
                ),
                "request_timeout",
            ),
        ]
        for name, budgets, planner, context, error_code in cases:
            with self.subTest(name=name):
                runtime, _ = build_test_runtime(planner, budgets=budgets)
                result = runtime.invoke(context, "test")
                self.assertEqual((result.status, result.error_code), ("exhausted", error_code))

    def test_model_error_is_stable_and_safe(self):
        runtime, _ = build_test_runtime(
            ScriptedPlanner(
                [GatewayAgentError("model", "model_unavailable", retryable=True)]
            )
        )

        result = runtime.invoke(make_runtime_context(), "multi-step")

        self.assertEqual((result.status, result.error_code), ("failed", "model_unavailable"))

    def test_executor_error_is_stable_and_safe(self):
        executor = RecordingExecutor(
            error=GatewayAgentError("upstream", "cmpf_upstream_error", retryable=True)
        )
        runtime, _ = build_test_runtime(
            ScriptedPlanner([tool_action()]), executor=executor
        )

        result = runtime.invoke(make_runtime_context(), "company")

        self.assertEqual((result.status, result.error_code), ("failed", "cmpf_upstream_error"))

    def test_cancellation_stops_before_planner_or_executor(self):
        planner = ScriptedPlanner([tool_action()])
        executor = RecordingExecutor()
        runtime, recorder = build_test_runtime(planner, executor=executor)

        result = runtime.invoke(make_runtime_context(cancelled=True), "company")

        self.assertEqual((result.status, result.error_code), ("cancelled", "cancelled"))
        self.assertEqual(planner.calls, [])
        self.assertEqual(executor.calls, [])
        self.assertEqual(recorder.nodes, ["planner", "terminal_error"])

    def test_finish_without_an_existing_artifact_fails_closed(self):
        for action in (
            AgentAction(kind="finish"),
            AgentAction(kind="finish", artifact_ids=["forged-artifact"]),
        ):
            with self.subTest(action=action):
                runtime, _ = build_test_runtime(ScriptedPlanner([action]))
                result = runtime.invoke(make_runtime_context(), "finish")
                self.assertEqual(result.status, "failed")
                self.assertIn(
                    result.error_code,
                    {"finish_artifact_required", "artifact_not_found"},
                )

    def test_compiled_graph_and_checkpoints_never_include_runtime_context_or_token(self):
        checkpointer = InMemorySaver()
        executor = RecordingExecutor()
        runtime, _ = build_test_runtime(
            ScriptedPlanner(
                [
                    tool_action("get_company_info"),
                    AgentAction(kind="finish", artifact_ids=["chart-1"]),
                ]
            ),
            executor=executor,
            checkpointer=checkpointer,
        )

        result = runtime.invoke(
            make_runtime_context(token="sentinel-never-checkpoint"), "company"
        )

        config = {"configurable": {"thread_id": result.run_id}}
        state = runtime.graph.get_state(config)
        checkpoint_records = list(checkpointer.list(config))
        encoded = json.dumps(
            {
                "state": state.values,
                "checkpoints": [
                    {"checkpoint": item.checkpoint, "metadata": item.metadata}
                    for item in checkpoint_records
                ],
            },
            default=str,
        )
        lowered = encoded.lower()
        self.assertNotIn("sentinel-never-checkpoint", encoded)
        self.assertNotIn("runtimecontext", lowered)
        self.assertNotIn("bearer_token", lowered)
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("raw_payload", lowered)
        self.assertIn("sentinel-never-checkpoint", executor.calls[0]["bearer_token"])

    def test_graph_exposes_only_the_stable_runtime_node_names(self):
        runtime, _ = build_test_runtime(ScriptedPlanner([]))
        public_nodes = set(runtime.graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(
            public_nodes,
            {
                "planner",
                "policy",
                "executor",
                "observer",
                "clarifier",
                "responder",
                "terminal_error",
            },
        )

    def test_executor_does_not_lazy_import_the_compatibility_service(self):
        from pathlib import Path

        source = Path("ai_native/gateway/executor.py").read_text(encoding="utf-8")
        self.assertNotIn("ai_native.gateway.service", source)


if __name__ == "__main__":
    unittest.main()
