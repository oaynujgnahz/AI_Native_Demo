# Controlled Gateway Agent Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-shot CMPF dispatcher with a policy-gated, checkpointed LangGraph ReAct loop supporting multi-tool planning, clarification/resume, cancellation, structured logs, and optional OpenTelemetry/Jaeger.

**Architecture:** FastAPI authenticates each request and creates an ephemeral context containing the current Keycloak token. LangGraph persists only safe state; every LLM action passes through deterministic policy before one catalogued tool runs, then a safe observer replans, interrupts, or returns deterministic artifacts. PostgreSQL stores conversations, run indexes, audits, and checkpoints; JSON logging is mandatory and OTel is optional.

**Tech Stack:** Python 3.13, FastAPI 0.139, Pydantic 2.13, LangGraph 1.2, PostgreSQL 16, psycopg 3, OpenAI-compatible tool calling, httpx, OpenTelemetry, Jaeger, vanilla JavaScript, ECharts, `unittest`.

## Global Constraints

- Modify only `AI_Native`; CMPF remains unchanged and authoritative for final authorization.
- Preserve all thirteen read-only tools and existing ChartSpec safety limits.
- Never send Token, raw CMPF DTO, emission values, complete ChartSpec, or hidden reasoning to the LLM.
- Never persist Token in state, checkpoint, logs, audits, or spans.
- Default budgets: 8 planner iterations, 6 tool actions, 2 clarifications, 45 seconds per request segment.
- Reject repeated canonical `tool_name + arguments` signatures.
- Allow one active or waiting run per conversation.
- Keep existing SSE events compatible; add `clarification` and `run_id` only.
- `OTEL_ENABLED=false` must require no tracing infrastructure.
- HTTP mode never falls back to mock business data.
- Use TDD and keep the full suite green after every task.

## Target File Structure

```text
ai_native/agent/actions.py          # validated AgentAction and SafeObservation
ai_native/agent/budgets.py          # hard loop limits and counters
ai_native/agent/planner.py          # planner protocol and OpenAI adapter
ai_native/agent/runtime.py          # LangGraph nodes and routing
ai_native/gateway/tooling.py        # typed thirteen-tool catalog
ai_native/gateway/executor.py       # one approved action -> CMPF/artifact
ai_native/gateway/policy.py         # authorization, validation, dedupe, budget
ai_native/gateway/observer.py       # safe observation, no business values
ai_native/gateway/runtime_context.py# ephemeral Principal/token/deadline
ai_native/gateway/checkpointer.py   # memory/PostgreSQL saver factory
ai_native/gateway/errors.py         # stable error taxonomy
ai_native/observability/logging.py  # JSON context and redaction
ai_native/observability/tracing.py  # optional OTel bootstrap
```

---

### Task 1: Define checkpoint-safe actions, budgets, state, and errors

**Files:**
- Create: `ai_native/agent/actions.py`
- Create: `ai_native/agent/budgets.py`
- Create: `ai_native/gateway/errors.py`
- Modify: `ai_native/agent/state.py`
- Test: `tests/test_agent_actions.py`

**Interfaces:**
- Produces `AgentAction`, `SafeObservation`, `AgentBudgets`, `RunCounters`, `BudgetExceeded`, `GatewayAgentError`, and checkpoint-safe `AgentState`.
- `AgentAction.kind` is exactly `call_tool`, `clarify`, or `finish`; extra fields are rejected.

- [ ] **Step 1: Write failing model and budget tests**

```python
# tests/test_agent_actions.py
import json
import unittest
from pydantic import ValidationError


class AgentActionTest(unittest.TestCase):
    def test_call_tool_requires_a_name(self):
        from ai_native.agent.actions import AgentAction
        action = AgentAction(kind="call_tool", tool_name="list_analysis_bases",
                             arguments={"company_id": "100"}, reason="resolve site")
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
        value = SafeObservation(tool_name="resolve_analysis_base", status="success",
            facts={"base_id": "10185", "base_name": "親社拠点2"}, result_count=1)
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
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_actions -v`  
Expected: FAIL because the new modules do not exist.

- [ ] **Step 3: Implement strict models and counters**

```python
# ai_native/agent/actions.py
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["call_tool", "clarify", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=300)
    question: str | None = Field(default=None, max_length=1000)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_variant(self):
        if self.kind == "call_tool" and not self.tool_name:
            raise ValueError("call_tool requires tool_name")
        if self.kind == "clarify" and not self.question:
            raise ValueError("clarify requires question")
        if self.kind != "call_tool" and (self.tool_name or self.arguments):
            raise ValueError("only call_tool accepts tool fields")
        return self


class SafeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: str
    status: Literal["success", "clarification_required", "failed"]
    facts: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = None
    result_count: int = Field(default=0, ge=0)
    error_code: str | None = None
```

Implement `AgentBudgets` and `RunCounters` as dataclasses. Check duplicate signature before the tool count so the error is stable. Replace `AgentState.auth_token` with serializable IDs, goal, actions, observations, artifact references, counters, pending question, stop reason, and error code.

- [ ] **Step 4: Verify GREEN and regression safety**

Run: `.venv/bin/python -m unittest tests.test_agent_actions tests.test_cmpf_agent -v`  
Expected: PASS; `AgentState` has no `auth_token` key.

- [ ] **Step 5: Commit**

```bash
git add ai_native/agent/actions.py ai_native/agent/budgets.py ai_native/agent/state.py ai_native/gateway/errors.py tests/test_agent_actions.py
git commit -m "feat: define safe agent loop state"
```

---

### Task 2: Replace the service switch with a typed thirteen-tool catalog

**Files:**
- Create: `ai_native/gateway/tooling.py`
- Create: `ai_native/gateway/executor.py`
- Modify: `ai_native/gateway/service.py`
- Test: `tests/test_agent_tooling.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Produces `ToolDefinition`, `ToolCatalog`, `build_enterprise_catalog()`, `EnterpriseToolExecutor.execute()`.
- `ExecutionResult` contains safe facts, artifact, count, and endpoint; never Token.
- Keep `EnterpriseAgentService.answer()` temporarily as a compatibility facade until Task 6.

- [ ] **Step 1: Write failing catalog contract tests**

```python
# tests/test_agent_tooling.py
import unittest
from pydantic import ValidationError


EXPECTED_TOOLS = {
    "get_company_info", "get_annual_emission_summary", "get_scope_breakdown",
    "get_scope_composition_chart", "get_monthly_emission_trend_chart",
    "get_top_emission_activities_chart", "list_analysis_bases",
    "get_base_emission_composition_chart", "get_base_monthly_emission_chart",
    "get_base_detail_composition_chart", "get_base_detail_monthly_chart",
    "compare_base_emissions_chart", "compare_emission_periods_chart",
}


class ToolCatalogTest(unittest.TestCase):
    def test_catalog_has_exactly_thirteen_read_only_tools(self):
        from ai_native.gateway.tooling import build_enterprise_catalog
        catalog = build_enterprise_catalog()
        self.assertEqual(set(catalog.names()), EXPECTED_TOOLS)
        self.assertTrue(all(catalog.get(name).risk == "read_only" for name in EXPECTED_TOOLS))

    def test_arguments_and_model_schema_exclude_credentials(self):
        from ai_native.gateway.tooling import build_enterprise_catalog
        catalog = build_enterprise_catalog()
        with self.assertRaises(ValidationError):
            catalog.get("get_company_info").argument_model(company_id="100", token="secret")
        encoded = str(catalog.openai_tools())
        self.assertNotIn("auth_token", encoded)
        self.assertNotIn("endpoint", encoded)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_tooling -v`  
Expected: FAIL because `gateway.tooling` does not exist.

- [ ] **Step 3: Implement catalog primitives and explicit definitions**

```python
# core interfaces in ai_native/gateway/tooling.py
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    argument_model: type[BaseModel]
    required_permission: str
    risk: Literal["read_only"]
    endpoint: str
    handler_name: str


class ToolCatalog:
    def __init__(self, definitions: list[ToolDefinition]):
        self._items = {item.name: item for item in definitions}
        if len(self._items) != len(definitions):
            raise ValueError("duplicate tool name")

    def names(self) -> list[str]:
        return sorted(self._items)

    def get(self, name: str) -> ToolDefinition:
        return self._items[name]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": item.name,
            "description": item.description,
            "parameters": item.argument_model.model_json_schema()}}
            for item in self._items.values()]
```

Create strict argument models for company, year, Scope, one base, multiple bases, grouping, and period comparison. Define all thirteen tools explicitly with their existing endpoint mappings. Move each branch from `EnterpriseAgentService.answer()` to a named `EnterpriseToolExecutor` handler and reuse the current deterministic chart functions.

- [ ] **Step 4: Verify catalog and all existing tool contracts**

Run: `.venv/bin/python -m unittest tests.test_agent_tooling tests.test_enterprise_gateway -v`  
Expected: PASS; existing chart and DTO mappings are unchanged.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/tooling.py ai_native/gateway/executor.py ai_native/gateway/service.py tests/test_agent_tooling.py tests/test_enterprise_gateway.py
git commit -m "refactor: catalog CMPF enterprise tools"
```

---

### Task 3: Add ephemeral runtime context and deterministic Policy Engine

**Files:**
- Create: `ai_native/gateway/runtime_context.py`
- Create: `ai_native/gateway/policy.py`
- Test: `tests/test_agent_policy.py`

**Interfaces:**
- Produces `RuntimeContext`, `PolicyDecision`, and `PolicyEngine.evaluate(action, state, context)`.
- Runtime context is never serialized. Approved decisions contain a server-generated approval ID and validated arguments.

- [ ] **Step 1: Write failing policy tests**

```python
# tests/test_agent_policy.py
class PolicyEngineTest(unittest.TestCase):
    def test_unknown_tool_is_denied(self):
        result = make_policy().evaluate(
            AgentAction(kind="call_tool", tool_name="delete_company", arguments={}),
            make_state(), make_context())
        self.assertEqual((result.status, result.error_code),
                         ("denied", "tool_not_allowed"))

    def test_company_outside_trusted_scope_is_denied(self):
        result = make_policy().evaluate(
            AgentAction(kind="call_tool", tool_name="get_company_info",
                        arguments={"company_id": "999"}),
            make_state(company_id="100", allowed_company_ids=["100", "200"]),
            make_context(company_id="100"))
        self.assertEqual(result.error_code, "company_forbidden")
        self.assertNotIn("request-secret", json.dumps(result.model_dump()))
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_policy -v`  
Expected: FAIL because policy/runtime-context modules do not exist.

- [ ] **Step 3: Implement checks in fixed order**

```python
# ai_native/gateway/runtime_context.py
@dataclass(frozen=True)
class RuntimeContext:
    principal: Principal
    bearer_token: str
    deadline: datetime
    repository: Any
    is_cancelled: Callable[[], bool]
```

Evaluate cancellation/deadline, action kind, catalog membership, company scope, strict argument model, canonical signature, remaining budget, and active run in that order. Canonicalize with sorted compact JSON and SHA-256. Return `approved`, `clarification_required`, or `denied`.

- [ ] **Step 4: Verify GREEN and security regressions**

Run: `.venv/bin/python -m unittest tests.test_agent_policy tests.test_enterprise_gateway.SecurityBoundaryTest -v`  
Expected: PASS; serialized decisions contain no Token.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/runtime_context.py ai_native/gateway/policy.py tests/test_agent_policy.py
git commit -m "feat: gate agent actions with policy"
```

---

### Task 4: Separate model observations from deterministic artifacts

**Files:**
- Create: `ai_native/gateway/observer.py`
- Modify: `ai_native/gateway/executor.py`
- Test: `tests/test_agent_tooling.py`

**Interfaces:**
- Produces `Artifact`, `ExecutionResult`, and `ObservationBuilder.from_result()`.
- Safe facts may contain authorized IDs/names, periods, status, artifact kind, and counts; not emissions.

- [ ] **Step 1: Write failing separation test**

```python
class ObservationSafetyTest(unittest.TestCase):
    def test_chart_values_stay_in_artifact(self):
        result = ExecutionResult(
            tool_name="get_base_detail_monthly_chart",
            endpoint="/analysis/baseMonthEmission",
            safe_facts={"base_id": "10185", "base_name": "親社拠点2"},
            artifact=Artifact("artifact-1", "chart",
                              {"series": [{"values": [132360.075]}]}),
            result_count=12)
        observation = ObservationBuilder().from_result(result)
        encoded = json.dumps(observation.model_dump(), ensure_ascii=False)
        self.assertIn("10185", encoded)
        self.assertNotIn("132360.075", encoded)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_tooling.ObservationSafetyTest -v`  
Expected: FAIL because observer types do not exist.

- [ ] **Step 3: Implement artifact isolation**

```python
@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ExecutionResult:
    tool_name: str
    endpoint: str
    safe_facts: dict[str, Any]
    artifact: Artifact | None
    result_count: int


class ObservationBuilder:
    def from_result(self, result: ExecutionResult) -> SafeObservation:
        facts = dict(result.safe_facts)
        if result.artifact:
            facts["artifact_kind"] = result.artifact.kind
        return SafeObservation(tool_name=result.tool_name, status="success",
            facts=facts, artifact_id=result.artifact.id if result.artifact else None,
            result_count=result.result_count)
```

Update every executor handler to use this result. Keep ChartSpec values only inside `Artifact.payload`.

- [ ] **Step 4: Verify GREEN and ChartSpec regressions**

Run: `.venv/bin/python -m unittest tests.test_agent_tooling tests.test_enterprise_gateway.ChartSpecTest -v`  
Expected: PASS; no observation contains series values.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/observer.py ai_native/gateway/executor.py tests/test_agent_tooling.py
git commit -m "feat: isolate observations from business artifacts"
```

---

### Task 5: Make the OpenAI-compatible planner emit one validated action

**Files:**
- Create: `ai_native/agent/planner.py`
- Modify: `ai_native/agent/llm.py`
- Test: `tests/test_agent_actions.py`

**Interfaces:**
- Produces `AgentPlanner.plan(goal, trusted_context, observations, artifact_summaries, remaining) -> AgentAction`.
- The request contains only safe observations and artifact IDs/kinds.

- [ ] **Step 1: Write failing parser and redaction tests**

```python
class PlannerTest(unittest.TestCase):
    def test_planner_returns_validated_action(self):
        client = RecordingOpenAIResponse(
            '{"kind":"call_tool","tool_name":"list_analysis_bases",'
            '"arguments":{"company_id":"100"},"reason":"resolve site"}')
        action = OpenAIActionPlanner(client, "model").plan(
            goal="親社拠点2の月別排出量", trusted_context={"company_id": "100"},
            observations=[], artifact_summaries=[], remaining={"planner": 7, "tools": 6})
        self.assertEqual((action.kind, action.tool_name),
                         ("call_tool", "list_analysis_bases"))

    def test_request_excludes_artifact_payload_and_token(self):
        client = RecordingOpenAIResponse(
            '{"kind":"finish","artifact_ids":["a1"]}')
        OpenAIActionPlanner(client, "model").plan(
            goal="show result", trusted_context={"company_id": "100"},
            observations=[], artifact_summaries=[{"id": "a1", "kind": "chart"}],
            remaining={"planner": 1, "tools": 0})
        encoded = json.dumps(client.last_request)
        self.assertNotIn("values", encoded)
        self.assertNotIn("Bearer", encoded)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_actions.PlannerTest -v`  
Expected: FAIL because `agent.planner` does not exist.

- [ ] **Step 3: Implement forced action-schema planning**

Define a synthetic tool named `submit_agent_action` with `AgentAction.model_json_schema()`, force its selection, and parse arguments with `AgentAction.model_validate_json()`. Convert malformed JSON or validation errors to `GatewayAgentError(category="model", code="model_invalid_action", retryable=True)`. Preserve multilingual CMPF routing guidance but remove “at most one tool per user turn.”

Keep a deterministic fallback only for high-confidence requests that require exactly one final tool and no safe observation/replan. Add tests showing annual summary can fall back after provider failure, while site-name resolution and comparison return `model_unavailable` without executing CMPF analysis.

- [ ] **Step 4: Verify GREEN and Japanese routing compatibility**

Run: `.venv/bin/python -m unittest tests.test_agent_actions tests.test_enterprise_gateway.SemanticReplanTest -v`  
Expected: PASS; Japanese site requests select site resolution as the first action.

- [ ] **Step 5: Commit**

```bash
git add ai_native/agent/planner.py ai_native/agent/llm.py tests/test_agent_actions.py tests/test_enterprise_gateway.py
git commit -m "feat: plan validated agent actions"
```

---

### Task 6: Build the policy-gated LangGraph runtime

**Files:**
- Create: `ai_native/agent/runtime.py`
- Modify: `ai_native/agent/graph.py`
- Modify: `ai_native/agent/state.py`
- Modify: `ai_native/gateway/service.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- Produces `build_agent_runtime()` and runtime `invoke()`/`resume()` methods.
- Stable node names: `planner`, `policy`, `executor`, `observer`, `clarifier`, `responder`, `terminal_error`.

- [ ] **Step 1: Write a failing two-tool loop test**

```python
# tests/test_agent_runtime.py
class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = iter(actions)
    def plan(self, **kwargs):
        return next(self.actions)


class AgentRuntimeTest(unittest.TestCase):
    def test_observation_replans_before_finish(self):
        planner = ScriptedPlanner([
            AgentAction(kind="call_tool", tool_name="list_analysis_bases",
                        arguments={"company_id": "100"}, reason="resolve site"),
            AgentAction(kind="call_tool", tool_name="get_base_detail_monthly_chart",
                        arguments={"company_id": "100", "base_id": "10185", "year": 2025},
                        reason="query monthly emissions"),
            AgentAction(kind="finish", artifact_ids=["chart-1"]),
        ])
        runtime, recorder = build_test_runtime(planner)
        result = runtime.invoke(make_runtime_context(), "親社拠点2の2025年月別排出量")
        self.assertEqual(recorder.nodes, [
            "planner", "policy", "executor", "observer",
            "planner", "policy", "executor", "observer",
            "planner", "responder"])
        self.assertEqual(result.status, "completed")
```

Add focused tests for clarify interrupt, policy denial, duplicate call, every budget, model error, executor error, cancellation, and finish without an artifact.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_runtime -v`  
Expected: FAIL because `agent.runtime` does not exist.

- [ ] **Step 3: Implement nodes and conditional routes**

```python
builder = StateGraph(AgentState)
builder.add_node("planner", planner_node)
builder.add_node("policy", policy_node)
builder.add_node("executor", executor_node)
builder.add_node("observer", observer_node)
builder.add_node("clarifier", clarifier_node)
builder.add_node("responder", responder_node)
builder.add_node("terminal_error", terminal_error_node)
builder.add_edge(START, "planner")
builder.add_conditional_edges("planner", route_action)
builder.add_conditional_edges("policy", route_policy)
builder.add_edge("executor", "observer")
builder.add_edge("observer", "planner")
builder.add_edge("clarifier", END)
builder.add_edge("responder", END)
builder.add_edge("terminal_error", END)
```

Pass `RuntimeContext` through LangGraph configurable context, never state. `clarifier_node` calls `interrupt()` with run ID, question, missing fields, and safe candidates. Responder reads artifact payloads and uses deterministic answer/chart formatters. Remove the old one-shot graph path after all compatibility tests pass.

- [ ] **Step 4: Verify GREEN and legacy coverage**

Run: `.venv/bin/python -m unittest tests.test_agent_runtime tests.test_agent_tooling tests.test_cmpf_agent -v`  
Expected: PASS; the scripted run executes two tool actions and then finishes.

- [ ] **Step 5: Commit**

```bash
git add ai_native/agent/runtime.py ai_native/agent/graph.py ai_native/agent/state.py ai_native/gateway/service.py tests/test_agent_runtime.py
git commit -m "feat: run policy-gated LangGraph loop"
```

---

### Task 7: Persist run indexes and LangGraph checkpoints

**Files:**
- Create: `ai_native/gateway/checkpointer.py`
- Modify: `ai_native/gateway/repository.py`
- Modify: `requirements.txt`
- Test: `tests/test_agent_repository.py`

**Interfaces:**
- Produces `AgentRun`, `create_run`, `get_run`, `claim_run`, `set_run_status`, `request_cancel`, `is_cancelled`, `get_pending_run`, `build_checkpointer_from_env`.
- Statuses: `running`, `waiting_for_user`, `completed`, `failed`, `cancelled`, `exhausted`.

- [ ] **Step 1: Write failing memory/PostgreSQL repository contracts**

```python
# tests/test_agent_repository.py
class RunRepositoryContract:
    def test_one_active_or_waiting_run_per_conversation(self):
        run = self.repository.create_run("conversation-1", "user-1", "100")
        self.assertEqual(run.status, "running")
        with self.assertRaises(Exception) as conflict:
            self.repository.create_run("conversation-1", "user-1", "100")
        self.assertEqual(conflict.exception.code, "active_run_conflict")

    def test_cancel_is_persistent_and_owner_scoped(self):
        run = self.repository.create_run("conversation-1", "user-1", "100")
        self.repository.request_cancel(run.id, "user-1")
        self.assertTrue(self.repository.is_cancelled(run.id))
        with self.assertRaises(Exception):
            self.repository.request_cancel(run.id, "other-user")
```

Instantiate the same contract with memory repository and PostgreSQL when `TEST_DATABASE_URL` is set.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_repository -v`  
Expected: FAIL because run methods do not exist.

- [ ] **Step 3: Implement run schema, optimistic claim, and saver factory**

Add `agent_runs` with UUID ID, conversation/user/company, status, version, cancel flag, timestamps, and a partial unique index for `running`/`waiting_for_user`. `claim_run` updates only the expected version. Add `langgraph-checkpoint-postgres` to requirements.

```python
# ai_native/gateway/checkpointer.py
def build_checkpointer_from_env():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return InMemorySaver()
    from langgraph.checkpoint.postgres import PostgresSaver
    saver = PostgresSaver.from_conn_string(database_url)
    saver.setup()
    return saver
```

Use run ID as LangGraph `thread_id`. Add a checkpoint inspection test rejecting keys matching token, authorization, cookie, and raw payload.

Add `delete_expired_agent_data()` and a repository contract test proving cleanup deletes expired checkpoints first, then runs, messages, and conversations while retaining unexpired audit rows. Run/checkpoint expiry follows the conversation's seven-day retention; audits retain ninety days.

- [ ] **Step 4: Verify GREEN with PostgreSQL**

Run: `docker compose up -d postgres`  
Run: `TEST_DATABASE_URL=postgresql://cmpf_agent:cmpf_agent@localhost:5432/cmpf_agent .venv/bin/python -m unittest tests.test_agent_repository -v`  
Expected: PASS for memory and PostgreSQL; a second runtime resumes the saved checkpoint.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/checkpointer.py ai_native/gateway/repository.py requirements.txt tests/test_agent_repository.py
git commit -m "feat: persist agent runs and checkpoints"
```

---

### Task 8: Replace streaming with run start/resume/status/cancel APIs

**Files:**
- Modify: `ai_native/api.py`
- Modify: `ai_native/demo.html`
- Test: `tests/test_agent_api.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Message stream starts a run or resumes the conversation's waiting run.
- Adds `GET /v1/conversations/{conversation_id}/runs/{run_id}`.
- Adds `POST /v1/conversations/{conversation_id}/runs/{run_id}/cancel`.
- Adds SSE `clarification`; all events contain `run_id`.

- [ ] **Step 1: Write failing resume and cancellation API tests**

```python
# tests/test_agent_api.py
class AgentRunApiTest(unittest.TestCase):
    def test_clarification_then_resume_uses_same_run(self):
        client = make_scripted_client()
        conversation_id = create_owned_conversation(client)
        first = post_stream(client, conversation_id, "親社拠点2の月別排出量", "valid-token")
        clarification = find_event(first.text, "clarification")
        run_id = clarification["run_id"]
        second = post_stream(client, conversation_id, "2025年", "refreshed-token")
        events = parse_sse(second.text)
        self.assertTrue(all(event["data"]["run_id"] == run_id for event in events))
        self.assertIn("answer.completed", [event["event"] for event in events])

    def test_cancel_stops_before_the_next_tool(self):
        client, conversation_id, run_id = make_waiting_client()
        response = client.post(
            f"/v1/conversations/{conversation_id}/runs/{run_id}/cancel",
            headers={"Authorization": "Bearer valid-token"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelled")
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_agent_api -v`  
Expected: FAIL because run endpoints and resume do not exist.

- [ ] **Step 3: Implement graph-backed API and Demo behavior**

Create/persist the user message and run before graph invocation. Pass current Token only in `RuntimeContext`. On resume use `Command(resume={"message": request.message, "context": trusted_context})`. Set `waiting_for_user` on interrupt and persist assistant/ChartSpec on finish. Map stable errors before streaming headers or emit `error` after streaming starts.

In the Demo, render `clarification` as a normal assistant question, retain `run_id`, and make the next Send resume. Stop must call the server cancellation endpoint before aborting the browser stream.

At run ingress, use the current Token to build `allowed_company_ids` from self plus `/user/company/options?mode=01`; store only IDs in state. Resume rebuilds and revalidates this set before any graph node executes. Add API tests showing a forged context company and a previously allowed company removed before resume are both denied before an analysis endpoint call.

- [ ] **Step 4: Verify GREEN, SSE compatibility, and JavaScript syntax**

Run: `.venv/bin/python -m unittest tests.test_agent_api tests.test_enterprise_gateway -v`  
Run: `awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check`  
Expected: PASS; old SSE event consumers continue working.

- [ ] **Step 5: Commit**

```bash
git add ai_native/api.py ai_native/demo.html tests/test_agent_api.py tests/test_enterprise_gateway.py
git commit -m "feat: stream resumable agent runs"
```

---

### Task 9: Add redacted JSON logs and correlation context

**Files:**
- Create: `ai_native/observability/__init__.py`
- Create: `ai_native/observability/logging.py`
- Modify: `ai_native/logging_config.py`
- Modify: `ai_native/api.py`
- Modify: `ai_native/gateway/cmpf_client.py`
- Test: `tests/test_observability.py`

**Interfaces:**
- Produces `bind_log_context`, `clear_log_context`, `redact`, and `JsonFormatter`.
- Fixed correlation fields include trace/run/conversation/user/company/node/tool/endpoint/duration/status/error/count.

- [ ] **Step 1: Write failing redaction and context tests**

```python
# tests/test_observability.py
class StructuredLoggingTest(unittest.TestCase):
    def test_nested_sensitive_and_business_values_are_removed(self):
        safe = redact({"authorization": "Bearer secret",
            "nested": {"token": "secret", "company_id": "100"},
            "series": [{"values": [132360.075]}]})
        encoded = json.dumps(safe)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("132360.075", encoded)
        self.assertIn("100", encoded)

    def test_json_record_has_run_and_node(self):
        record = capture_one_log(run_id="run-1", graph_node="policy")
        parsed = json.loads(record)
        self.assertEqual(parsed["run_id"], "run-1")
        self.assertEqual(parsed["graph_node"], "policy")
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_observability.StructuredLoggingTest -v`  
Expected: FAIL because structured logging does not exist.

- [ ] **Step 3: Implement ContextVar JSON logging and recursive redaction**

Use `ContextVar` for correlation fields and remove keys matching `authorization|token|cookie|raw_payload|prompt|messages|values|series`. Output UTC ISO timestamps. Middleware binds request/trace IDs and clears them in `finally`; graph nodes bind run/node; CMPF client logs endpoint path, duration, status, and count only.

- [ ] **Step 4: Verify GREEN and security tests**

Run: `.venv/bin/python -m unittest tests.test_observability.StructuredLoggingTest tests.test_enterprise_gateway.SecurityBoundaryTest -v`  
Expected: PASS; sentinel secrets and values are absent.

- [ ] **Step 5: Commit**

```bash
git add ai_native/observability/__init__.py ai_native/observability/logging.py ai_native/logging_config.py ai_native/api.py ai_native/gateway/cmpf_client.py tests/test_observability.py
git commit -m "feat: add redacted structured gateway logs"
```

---

### Task 10: Add optional OpenTelemetry and Jaeger

**Files:**
- Create: `ai_native/observability/tracing.py`
- Modify: `requirements.txt`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `ai_native/api.py`
- Modify: `ai_native/agent/runtime.py`
- Test: `tests/test_observability.py`

**Interfaces:**
- Produces `configure_tracing(app, service_name)` and `agent_span(name, attributes)`.
- Disabled mode is a no-op; exporter failure cannot change business responses.

- [ ] **Step 1: Write failing disabled/enabled tests**

```python
class TracingTest(unittest.TestCase):
    def test_disabled_mode_needs_no_exporter(self):
        with patch.dict(os.environ, {"OTEL_ENABLED": "false"}, clear=False):
            self.assertIsNone(configure_tracing(FakeApp(), "cmpf-agent-gateway"))

    def test_nodes_share_one_trace_without_sensitive_attributes(self):
        exporter = InMemorySpanExporter()
        run_traced_agent_request(exporter)
        spans = exporter.get_finished_spans()
        self.assertTrue({"agent.planner", "agent.policy", "agent.executor"}
                        <= {span.name for span in spans})
        self.assertEqual(len({span.context.trace_id for span in spans}), 1)
        encoded = json.dumps([dict(span.attributes) for span in spans])
        self.assertNotIn("Bearer", encoded)
        self.assertNotIn("132360.075", encoded)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_observability.TracingTest -v`  
Expected: FAIL because tracing and OTel dependencies do not exist.

- [ ] **Step 3: Implement optional tracing and Compose Jaeger**

Add `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-fastapi`, and `opentelemetry-instrumentation-httpx`. Configure only when enabled. Add Jaeger all-in-one with OTLP HTTP port 4318 and UI port 16686. Instrument FastAPI/httpx and manual planner, policy, executor, observer, checkpoint, and SSE spans. Store IDs, node/tool, endpoint path, status, duration, error, and counts only.

Add health tests proving `/health/ready` depends on the repository but not Jaeger/exporter availability. Export failures use a rate-limited warning and never propagate into the request coroutine.

- [ ] **Step 4: Verify GREEN and exporter-failure degradation**

Run: `.venv/bin/python -m unittest tests.test_observability -v`  
Expected: PASS with OTel disabled, in-memory exporter enabled, and a failing exporter.

- [ ] **Step 5: Commit**

```bash
git add ai_native/observability/tracing.py requirements.txt docker-compose.yml .env.example ai_native/api.py ai_native/agent/runtime.py tests/test_observability.py
git commit -m "feat: add optional OpenTelemetry tracing"
```

---

### Task 11: Document and verify the production-like Demo

**Files:**
- Modify: `README.md`
- Modify: `tests/test_enterprise_gateway.py`
- Modify: `docs/superpowers/plans/2026-07-20-controlled-gateway-agent-loop.md`

**Interfaces:**
- README contains runnable commands for memory, PostgreSQL, OTel/Jaeger, real CMPF, resume, cancellation, and trace lookup.

- [ ] **Step 1: Write failing documentation contract**

```python
class ControlledLoopDocumentationTest(unittest.TestCase):
    def test_readme_documents_runtime_and_observability(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        for required in ("Policy-gated ReAct", "waiting_for_user", "OTEL_ENABLED",
                         "http://localhost:16686", "clarification",
                         "/runs/{runId}/cancel", "checkpoint"):
            self.assertIn(required, readme)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.ControlledLoopDocumentationTest -v`  
Expected: FAIL on the first missing term.

- [ ] **Step 3: Write the operations and architecture guide**

Document module responsibilities, graph nodes, safe observation/artifact separation, budgets, run statuses, environment variables, Compose commands, streaming requests, clarification/resume, server cancel, and Jaeger lookup by run ID. State that CMPF remains unchanged and authoritative.

- [ ] **Step 4: Run complete automated verification**

Run: `.venv/bin/python -m unittest discover -s tests -q`  
Expected: all existing 59 tests and every new test PASS.

Run: `.venv/bin/python -m compileall -q ai_native app.py`  
Expected: exit 0.

Run: `awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check`  
Expected: exit 0.

Run: `git diff --check`  
Expected: exit 0 with no output.

- [ ] **Step 5: Run real CMPF acceptance**

1. Ask `親社拠点2の2025年月別排出量推移をグラフで表示して`; confirm site resolution, replan, `/analysis/baseMonthEmission`, and chart.
2. Ask `親社拠点2の月別排出量推移を表示して`; confirm clarification, answer `2025年`, and same-run resume.
3. Use an ambiguous site name; select a safe candidate and resume.
4. Compare two sites or periods; confirm at least two Agent actions.
5. Cancel a waiting/running run; confirm no later analysis call or visualization.
6. Enable OTel; confirm one Jaeger trace contains FastAPI, graph nodes, LLM, CMPF/httpx, and SSE spans.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md tests/test_enterprise_gateway.py docs/superpowers/plans/2026-07-20-controlled-gateway-agent-loop.md
git commit -m "docs: explain controlled gateway agent runtime"
```

## Final Review Checklist

- [ ] Every design section in `docs/superpowers/specs/2026-07-20-controlled-gateway-agent-loop-design.md` maps to a completed task.
- [ ] `AgentState` and checkpoints contain no Token or raw payload.
- [ ] All thirteen tools exist once in the typed catalog; the old runtime path is removed.
- [ ] Every tool action passes Policy; duplicate signatures cannot execute.
- [ ] PostgreSQL resume works after recreating the runtime.
- [ ] Server cancellation is checked before each node and tool call.
- [ ] Logs, audits, checkpoints, and spans pass sentinel redaction tests.
- [ ] OTel disabled and exporter-failure modes preserve Gateway behavior.
- [ ] CMPF repository has no changes made by this work.
- [ ] Full automated and real CMPF acceptance runs immediately before completion.

## Design Coverage Matrix

| Design requirement | Implementing task |
|---|---|
| Overall layering and removal of two runtimes | Tasks 2, 6, 8 |
| Structured action and Policy-gated ReAct | Tasks 1, 3, 5, 6 |
| Thirteen typed tools and deterministic execution | Tasks 2, 4 |
| Checkpoint-safe state and hard budgets | Tasks 1, 6, 7 |
| Safe observation/artifact split | Task 4 |
| Clarification, resume, status, cancellation | Tasks 6, 7, 8 |
| Stable errors and no unsafe fallback | Tasks 1, 5, 6, 8 |
| JSON logs and redaction | Task 9 |
| Optional OTel/Jaeger and readiness independence | Task 10 |
| Retention and cleanup ordering | Task 7 |
| Automated and real CMPF acceptance | Task 11 |
