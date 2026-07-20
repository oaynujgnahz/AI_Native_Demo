from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Literal, Mapping, Sequence
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime as LangGraphRuntime
from langgraph.types import Command, interrupt

from ai_native.agent.actions import AgentAction, SafeObservation
from ai_native.agent.budgets import AgentBudgets, BudgetExceeded, RunCounters
from ai_native.agent.planner import AgentPlanner, OpenAIActionPlanner
from ai_native.agent.state import AgentState
from ai_native.gateway.charts import ChartSpec
from ai_native.gateway.errors import (
    CompanyForbiddenError,
    GatewayAgentError,
    RequestValidationError,
)
from ai_native.gateway.executor import EnterpriseToolExecutor
from ai_native.gateway.observer import Artifact, ExecutionResult, ObservationBuilder
from ai_native.gateway.policy import PolicyDecision, PolicyEngine
from ai_native.gateway.runtime_context import RuntimeContext
from ai_native.gateway.tooling import ToolCatalog, build_enterprise_catalog


RuntimeStatus = Literal[
    "completed",
    "clarification_required",
    "failed",
    "cancelled",
    "exhausted",
]


@dataclass(frozen=True)
class AgentRuntimeResult:
    run_id: str
    status: RuntimeStatus
    answer: str = ""
    chart: ChartSpec | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    question: str | None = None
    missing_fields: tuple[str, ...] = ()
    candidates: tuple[dict[str, str], ...] = ()


class _UnavailablePlanner:
    def plan(self, **_: Any) -> AgentAction:
        raise GatewayAgentError(
            category="model",
            code="model_unavailable",
            retryable=True,
        )


class AgentRuntime:
    """Policy-gated LangGraph loop with request capabilities outside state."""

    def __init__(
        self,
        *,
        planner: AgentPlanner,
        policy_engine: PolicyEngine,
        executor: EnterpriseToolExecutor,
        observer: ObservationBuilder,
        budgets: AgentBudgets,
        checkpointer: Any,
        on_node: Callable[[str], None] | None = None,
    ) -> None:
        self.planner = planner
        self.policy_engine = policy_engine
        self.executor = executor
        self.observer = observer
        self.budgets = budgets
        self.checkpointer = checkpointer
        self._on_node = on_node
        self._pending_results: dict[str, ExecutionResult] = {}
        self._artifacts: dict[tuple[str, str], Artifact] = {}
        self._responses: dict[str, AgentRuntimeResult] = {}
        self._lock = RLock()
        self.graph = self._compile_graph()

    def invoke(
        self,
        context: RuntimeContext,
        goal: str,
        *,
        run_id: str | None = None,
        conversation_id: str | None = None,
        company_id: str | None = None,
        allowed_company_ids: Sequence[str] | None = None,
        year: int | None = None,
        locale: str | None = None,
    ) -> AgentRuntimeResult:
        active_run_id = run_id or str(uuid4())
        active_conversation_id = conversation_id or active_run_id
        trusted_company_id = str(company_id or context.principal.company_id)
        trusted_allowed_ids = [
            str(item)
            for item in (allowed_company_ids or [context.principal.company_id])
        ]
        state: AgentState = {
            "run_id": active_run_id,
            "conversation_id": active_conversation_id,
            "user_id": context.principal.user_id,
            "tenant_id": context.principal.role_id,
            "company_id": trusted_company_id,
            "allowed_company_ids": trusted_allowed_ids,
            "locale": locale or context.principal.locale,
            "goal": goal,
            "actions": [],
            "observations": [],
            "artifact_ids": [],
            "counters": RunCounters(self.budgets),
        }
        trusted_year = year if year is not None else _year_from_goal(goal)
        if trusted_year is not None:
            state["year"] = trusted_year
        config = _config(active_run_id)
        result = self.graph.invoke(state, config=config, context=context)
        return self._to_result(active_run_id, result)

    def resume(
        self,
        context: RuntimeContext,
        run_id: str,
        user_input: str,
        *,
        trusted_context: Mapping[str, Any] | None = None,
    ) -> AgentRuntimeResult:
        result = self.graph.invoke(
            Command(
                resume={
                    "message": user_input,
                    "context": dict(trusted_context or {}),
                }
            ),
            config=_config(run_id),
            context=context,
        )
        return self._to_result(run_id, result)

    def _compile_graph(self):
        builder = StateGraph(AgentState, context_schema=RuntimeContext)
        builder.add_node("planner", self._planner_node)
        builder.add_node("policy", self._policy_node)
        builder.add_node("executor", self._executor_node)
        builder.add_node("observer", self._observer_node)
        builder.add_node("clarifier", self._clarifier_node)
        builder.add_node("responder", self._responder_node)
        builder.add_node("terminal_error", self._terminal_error_node)

        builder.add_edge(START, "planner")
        builder.add_conditional_edges(
            "planner",
            self._route_action,
            {
                "policy": "policy",
                "responder": "responder",
                "terminal_error": "terminal_error",
            },
        )
        builder.add_conditional_edges(
            "policy",
            self._route_policy,
            {
                "executor": "executor",
                "clarifier": "clarifier",
                "terminal_error": "terminal_error",
            },
        )
        builder.add_conditional_edges(
            "executor",
            self._route_executor,
            {"observer": "observer", "terminal_error": "terminal_error"},
        )
        builder.add_conditional_edges(
            "observer",
            self._route_observer,
            {"planner": "planner", "terminal_error": "terminal_error"},
        )
        # The node remains suspended at interrupt(). Once a fresh request resumes
        # it, the supplied clarification is incorporated before planning continues.
        builder.add_edge("clarifier", "planner")
        builder.add_edge("responder", END)
        builder.add_edge("terminal_error", END)
        return builder.compile(checkpointer=self.checkpointer)

    def _planner_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        self._record("planner")
        preflight = _preflight_error(runtime.context)
        if preflight is not None:
            return _error_update(*preflight)
        counters = state.get("counters")
        if not isinstance(counters, RunCounters):
            return _error_update("budget", "budget_state_missing")
        try:
            counters.consume_planner()
            action = self.planner.plan(
                goal=state.get("goal", ""),
                trusted_context={
                    "company_id": state.get("company_id"),
                    "year": state.get("year"),
                    "locale": state.get("locale"),
                },
                observations=list(state.get("observations", [])),
                artifact_summaries=_artifact_summaries(
                    state.get("observations", [])
                ),
                remaining=_remaining(counters),
            )
        except BudgetExceeded as exc:
            return _error_update(exc.category, exc.code, counters=counters)
        except GatewayAgentError as exc:
            return _error_update(exc.category, exc.code, counters=counters)
        except Exception:
            return _error_update(
                "model", "model_invalid_action", counters=counters
            )
        return {
            "actions": [*state.get("actions", []), action],
            "counters": counters,
        }

    def _policy_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        self._record("policy")
        action = _last_action(state)
        if action is None:
            return _error_update("model", "model_invalid_action")
        decision = self.policy_engine.evaluate(action, state, runtime.context)
        update: AgentState = {
            "policy_status": decision.status,
            "counters": state["counters"],
        }
        if decision.status == "denied":
            update.update(
                _error_update(
                    _category_for_code(decision.error_code),
                    decision.error_code or "policy_denied",
                )
            )
        elif decision.status == "clarification_required":
            update["pending_question"] = decision.question or action.question or ""
            update["missing_fields"] = list(decision.missing_fields)
        else:
            update["approval_id"] = decision.approval_id
            update["approved_tool_name"] = decision.tool_name
            update["approved_arguments_json"] = decision.validated_arguments_json
            update["approval_signature"] = decision.signature
        return update

    def _executor_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        self._record("executor")
        preflight = _preflight_error(runtime.context)
        if preflight is not None:
            return _error_update(*preflight)
        decision = PolicyDecision(
            status="approved",
            approval_id=state.get("approval_id"),
            tool_name=state.get("approved_tool_name"),
            validated_arguments_json=state.get("approved_arguments_json", "{}"),
            signature=state.get("approval_signature"),
        )
        if not self.policy_engine.verify_approval(decision):
            return _error_update("policy", "approval_invalid")
        if decision.tool_name is None:
            return _error_update("policy", "approval_invalid")
        try:
            result = self.executor.execute(
                tool_name=decision.tool_name,
                arguments=decision.validated_arguments,
                principal=runtime.context.principal,
                bearer_token=runtime.context.bearer_token,
                message=state.get("goal", ""),
                context={
                    "company_id": state.get("company_id"),
                    "year": state.get("year"),
                    "locale": state.get("locale"),
                },
            )
        except RequestValidationError as exc:
            update = _error_update("validation", exc.code)
            if exc.candidates:
                update["candidates"] = list(exc.candidates)
            return update
        except CompanyForbiddenError:
            return _error_update("authorization", "company_forbidden")
        except GatewayAgentError as exc:
            return _error_update(exc.category, exc.code)
        except Exception:
            return _error_update("upstream", "executor_error")
        run_id = state.get("run_id", "")
        result_id = str(uuid4())
        save_execution_result = getattr(
            runtime.context.repository, "save_execution_result", None
        )
        if callable(save_execution_result):
            try:
                result_id = str(save_execution_result(run_id, result))
            except GatewayAgentError as exc:
                return _error_update(exc.category, exc.code)
            except Exception:
                return _error_update("persistence", "execution_result_store_failed")
        with self._lock:
            self._pending_results[result_id] = result
        return {"pending_result_id": result_id}

    def _observer_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        self._record("observer")
        preflight = _preflight_error(runtime.context)
        if preflight is not None:
            return _error_update(*preflight)
        run_id = state.get("run_id", "")
        result_id = state.get("pending_result_id", "")
        with self._lock:
            result = self._pending_results.pop(result_id, None)
        if result is None:
            get_execution_result = getattr(
                runtime.context.repository, "get_execution_result", None
            )
            if callable(get_execution_result):
                stored = get_execution_result(run_id, result_id)
                if stored is not None:
                    result = _restore_execution_result(stored)
        if result is None:
            return _error_update("upstream", "execution_result_missing")
        try:
            observation = self.observer.from_result(result)
        except Exception:
            return _error_update("upstream", "observation_invalid")
        artifact_ids = list(state.get("artifact_ids", []))
        if result.artifact is not None:
            with self._lock:
                self._artifacts[(run_id, result.artifact.id)] = result.artifact
            artifact_ids.append(result.artifact.id)
        _write_execution_audit(runtime.context, result)
        return {
            "observations": [*state.get("observations", []), observation],
            "artifact_ids": artifact_ids,
            "pending_result_id": "",
        }

    def _clarifier_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        del runtime
        self._record("clarifier")
        payload = {
            "run_id": state.get("run_id", ""),
            "question": state.get("pending_question", ""),
            "missing_fields": list(state.get("missing_fields", [])),
            "candidates": _safe_candidates(state.get("observations", [])),
        }
        resumed = interrupt(payload)
        if isinstance(resumed, Mapping):
            user_input = resumed.get("message", "")
            trusted_context = resumed.get("context", {})
        else:
            user_input = resumed
            trusted_context = {}
        clarification = str(user_input).strip()
        goal = state.get("goal", "")
        if clarification:
            goal = f"{goal}\nUser clarification: {clarification}"
        update: AgentState = {
            "goal": goal,
            "pending_question": "",
            "missing_fields": [],
        }
        if isinstance(trusted_context, Mapping):
            company_id = trusted_context.get("company_id")
            if company_id is not None:
                update["company_id"] = str(company_id)
            allowed_company_ids = trusted_context.get("allowed_company_ids")
            if isinstance(allowed_company_ids, Sequence) and not isinstance(
                allowed_company_ids, (str, bytes)
            ):
                update["allowed_company_ids"] = [
                    str(item) for item in allowed_company_ids
                ]
            year = trusted_context.get("year")
            if year is not None:
                update["year"] = int(year)
            locale = trusted_context.get("locale")
            if locale is not None:
                update["locale"] = str(locale)
        return update

    def _responder_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        self._record("responder")
        run_id = state.get("run_id", "")
        action = _last_action(state)
        requested = list(action.artifact_ids) if action is not None else []
        if not requested:
            result = AgentRuntimeResult(
                run_id=run_id,
                status="failed",
                error_code="finish_artifact_required",
            )
            with self._lock:
                self._responses[run_id] = result
            return _error_update("validation", "finish_artifact_required")
        existing_ids = set(state.get("artifact_ids", []))
        if any(item not in existing_ids for item in requested):
            result = AgentRuntimeResult(
                run_id=run_id,
                status="failed",
                error_code="artifact_not_found",
            )
            with self._lock:
                self._responses[run_id] = result
            return _error_update("validation", "artifact_not_found")
        artifacts: list[Artifact | None] = []
        get_artifact = getattr(runtime.context.repository, "get_artifact", None)
        for artifact_id in requested:
            with self._lock:
                artifact = self._artifacts.get((run_id, artifact_id))
            if artifact is None and callable(get_artifact):
                stored = get_artifact(run_id, artifact_id)
                if stored is not None:
                    artifact = Artifact(
                        id=stored.artifact_id,
                        kind=stored.artifact_kind,
                        payload=dict(stored.artifact_payload),
                    )
                    with self._lock:
                        self._artifacts[(run_id, artifact_id)] = artifact
            artifacts.append(artifact)
        if any(item is None for item in artifacts):
            result = AgentRuntimeResult(
                run_id=run_id,
                status="failed",
                error_code="artifact_not_found",
            )
            with self._lock:
                self._responses[run_id] = result
            return _error_update("validation", "artifact_not_found")
        answers: list[str] = []
        chart: ChartSpec | None = None
        for artifact in artifacts:
            assert artifact is not None
            answer = artifact.payload.get("answer")
            if isinstance(answer, str) and answer and answer not in answers:
                answers.append(answer)
            chart_payload = artifact.payload.get("chart")
            if isinstance(chart_payload, Mapping):
                chart = ChartSpec.model_validate(chart_payload)
        result = AgentRuntimeResult(
            run_id=run_id,
            status="completed",
            answer="\n".join(answers),
            chart=chart,
            artifact_ids=tuple(requested),
        )
        with self._lock:
            self._responses[run_id] = result
        return {"artifact_ids": requested, "stop_reason": "completed"}

    def _terminal_error_node(
        self,
        state: AgentState,
        runtime: LangGraphRuntime[RuntimeContext],
    ) -> AgentState:
        del runtime
        self._record("terminal_error")
        run_id = state.get("run_id", "")
        code = state.get("error_code", "agent_error")
        category = state.get("error_category", "error")
        result = AgentRuntimeResult(
            run_id=run_id,
            status=_status_for_error(category, code),
            error_code=code,
            artifact_ids=tuple(state.get("artifact_ids", [])),
            candidates=tuple(state.get("candidates", [])),
        )
        with self._lock:
            self._responses[run_id] = result
        return {"stop_reason": result.status}

    @staticmethod
    def _route_action(
        state: AgentState,
    ) -> Literal["policy", "responder", "terminal_error"]:
        if state.get("error_code"):
            return "terminal_error"
        action = _last_action(state)
        if action is None:
            return "terminal_error"
        if action.kind == "finish":
            return "responder"
        return "policy"

    @staticmethod
    def _route_policy(
        state: AgentState,
    ) -> Literal["executor", "clarifier", "terminal_error"]:
        status = state.get("policy_status")
        if status == "approved":
            return "executor"
        if status == "clarification_required":
            return "clarifier"
        return "terminal_error"

    @staticmethod
    def _route_observer(
        state: AgentState,
    ) -> Literal["planner", "terminal_error"]:
        return "terminal_error" if state.get("error_code") else "planner"

    @staticmethod
    def _route_executor(
        state: AgentState,
    ) -> Literal["observer", "terminal_error"]:
        return "terminal_error" if state.get("error_code") else "observer"

    def _to_result(
        self,
        run_id: str,
        state: Mapping[str, Any],
    ) -> AgentRuntimeResult:
        interruptions = state.get("__interrupt__")
        if interruptions:
            value = interruptions[0].value
            return AgentRuntimeResult(
                run_id=run_id,
                status="clarification_required",
                question=str(value.get("question") or ""),
                missing_fields=tuple(value.get("missing_fields") or ()),
                candidates=tuple(value.get("candidates") or ()),
                artifact_ids=tuple(state.get("artifact_ids", [])),
            )
        with self._lock:
            response = self._responses.pop(run_id, None)
        if response is not None:
            return response
        code = state.get("error_code")
        if isinstance(code, str):
            return AgentRuntimeResult(
                run_id=run_id,
                status=_status_for_error(
                    str(state.get("error_category", "error")), code
                ),
                error_code=code,
                artifact_ids=tuple(state.get("artifact_ids", [])),
                candidates=tuple(state.get("candidates", [])),
            )
        return AgentRuntimeResult(
            run_id=run_id,
            status="failed",
            error_code="runtime_result_missing",
        )

    def _record(self, node: str) -> None:
        if self._on_node is not None:
            self._on_node(node)


def build_agent_runtime(
    *,
    planner: AgentPlanner | None = None,
    policy_engine: PolicyEngine | None = None,
    executor: EnterpriseToolExecutor | None = None,
    observer: ObservationBuilder | None = None,
    gateway: Any | None = None,
    repository: Any | None = None,
    catalog: ToolCatalog | None = None,
    budgets: AgentBudgets | None = None,
    checkpointer: Any | None = None,
    on_node: Callable[[str], None] | None = None,
) -> AgentRuntime:
    tool_catalog = catalog or build_enterprise_catalog()
    selected_planner = planner or OpenAIActionPlanner.from_env() or _UnavailablePlanner()
    selected_executor = executor
    if selected_executor is None:
        if gateway is None or repository is None:
            raise ValueError("executor or gateway and repository are required")
        selected_executor = EnterpriseToolExecutor(
            gateway,
            repository,
            catalog=tool_catalog,
        )
    return AgentRuntime(
        planner=selected_planner,
        policy_engine=policy_engine or PolicyEngine(tool_catalog),
        executor=selected_executor,
        observer=observer or ObservationBuilder(),
        budgets=budgets or AgentBudgets(),
        checkpointer=checkpointer or _build_default_checkpointer(),
        on_node=on_node,
    )


def _config(run_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": run_id}}


def _build_default_checkpointer() -> Any:
    from ai_native.gateway.checkpointer import build_checkpointer_from_env

    return build_checkpointer_from_env()


def _restore_execution_result(stored: Any) -> ExecutionResult:
    return ExecutionResult(
        tool_name=str(stored.tool_name),
        endpoint=str(stored.endpoint),
        safe_facts=dict(stored.safe_facts),
        artifact=Artifact(
            id=str(stored.artifact_id),
            kind=str(stored.artifact_kind),
            payload=dict(stored.artifact_payload),
        ),
        result_count=int(stored.result_count),
        audit_details=dict(stored.audit_details),
    )


def _year_from_goal(goal: str) -> int | None:
    match = re.search(r"(?<!\d)(20\d{2})(?!\d)", goal)
    return int(match.group(1)) if match else None


def _preflight_error(context: RuntimeContext) -> tuple[str, str] | None:
    if context.is_cancelled():
        return "cancel", "cancelled"
    deadline = context.deadline
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    if now >= deadline:
        return "budget", "request_timeout"
    return None


def _last_action(state: AgentState) -> AgentAction | None:
    actions = state.get("actions", [])
    return actions[-1] if actions else None


def _artifact_summaries(
    observations: Sequence[SafeObservation],
) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for observation in observations:
        if observation.artifact_id and observation.facts.artifact_kind:
            summaries.append(
                {
                    "id": observation.artifact_id,
                    "kind": observation.facts.artifact_kind,
                }
            )
    return summaries


def _safe_candidates(
    observations: Sequence[SafeObservation],
) -> list[dict[str, str]]:
    if not observations:
        return []
    return [
        {"base_id": item.base_id, "name": item.name}
        for item in observations[-1].facts.candidates[:20]
    ]


def _remaining(counters: RunCounters) -> dict[str, int]:
    return {
        "planner": max(0, counters.budgets.planner - counters.planner_calls),
        "tools": max(0, counters.budgets.tools - counters.tool_calls),
        "clarifications": max(
            0,
            counters.budgets.clarifications - counters.clarification_calls,
        ),
    }


def _error_update(
    category: str,
    code: str,
    *,
    counters: RunCounters | None = None,
) -> AgentState:
    update: AgentState = {"error_category": category, "error_code": code}
    if counters is not None:
        update["counters"] = counters
    return update


def _category_for_code(code: str | None) -> str:
    if code == "cancelled":
        return "cancel"
    if code in {
        "request_timeout",
        "budget_state_missing",
        "planner_budget_exhausted",
        "tool_budget_exhausted",
        "clarification_budget_exhausted",
        "duplicate_tool_call",
    }:
        return "budget"
    return "policy"


def _status_for_error(category: str, code: str) -> RuntimeStatus:
    if category == "cancel" or code == "cancelled":
        return "cancelled"
    if category == "budget" or code in {
        "request_timeout",
        "planner_budget_exhausted",
        "tool_budget_exhausted",
        "clarification_budget_exhausted",
        "duplicate_tool_call",
    }:
        return "exhausted"
    return "failed"


def _write_execution_audit(context: RuntimeContext, result: ExecutionResult) -> None:
    write_audit = getattr(context.repository, "write_audit", None)
    if not callable(write_audit):
        return
    write_audit(
        {
            "user_id": context.principal.user_id,
            "company_id": result.safe_facts.get(
                "company_id", context.principal.company_id
            ),
            "tool_name": result.tool_name,
            "status": "success",
            "year": result.safe_facts.get("year"),
            "result_count": result.result_count,
            **dict(result.audit_details),
        }
    )
