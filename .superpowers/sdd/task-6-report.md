# Task 6 report: policy-gated LangGraph runtime

## Status

Implemented the Task 6 runtime and compatibility refactor. Final verification,
compile, and self-review were intentionally stopped at the user's explicit
request. The commit is therefore delivered as **implemented but not freshly
verified after the final edits**.

## Delivered

- Added `ai_native.agent.runtime` with `build_agent_runtime()` and runtime
  `invoke()`/`resume()` methods.
- Added the stable LangGraph nodes `planner`, `policy`, `executor`, `observer`,
  `clarifier`, `responder`, and `terminal_error`.
- Routed every executable tool action through `PolicyEngine`, persisted only
  the signed approval's safe scalar fields, and re-verified the HMAC approval
  immediately before execution.
- Passed `RuntimeContext` only through LangGraph's `context_schema`/runtime
  injection. The graph config contains only `thread_id`; Bearer credentials are
  never added to `AgentState`.
- Kept raw `ExecutionResult` and artifact payloads in a runtime-owned ephemeral
  side channel. Checkpoint state contains safe observations and artifact IDs,
  not CMPF values or complete ChartSpec payloads.
- Implemented planner/tool/clarification/deadline budgets, duplicate-call
  rejection, cancellation, model/executor error routing, finish validation,
  deterministic artifact rendering, and LangGraph `interrupt()` resume flow.
- Removed `LegacyAgentState` and replaced the historical one-shot graph with a
  compatibility facade backed by the new controlled runtime.
- Preserved the existing API-facing `EnterpriseAgentService` behavior until
  Task 8.
- Removed the executor/service bidirectional lazy imports. Shared compatibility
  errors now live in `gateway.errors`; DTO/chart/answer helpers used by the
  executor live in `gateway.execution_support`.
- Added a concrete MemorySaver checkpoint inspection test that scans compiled
  graph state and all checkpoint records for a Bearer-token sentinel,
  `RuntimeContext`, `bearer_token`, and `authorization`.

## Exact TDD evidence

### RED

Command:

```text
.venv/bin/python -m unittest tests.test_agent_runtime -v
```

Observed before runtime implementation:

```text
Ran 12 tests in 0.006s
FAILED (failures=1, errors=15)
ModuleNotFoundError: No module named 'ai_native.agent.runtime'
AssertionError: executor still contained ai_native.gateway.service imports
```

This was the expected missing runtime plus the recorded Task 2 dependency
cycle.

### Interim GREEN/remaining RED

The same focused command was run after the initial runtime implementation and
before the final branch/cycle edits:

```text
Ran 12 tests in 0.416s
FAILED (failures=2)
```

Ten runtime tests passed, including the two-tool replan loop, clarification and
resume with a refreshed Token, checkpoint Token exclusion, duplicate calls,
all budgets, cancellation, model errors, finish validation, policy denial, and
stable node names. The two observed failures were:

1. executor errors still flowed to `observer`, producing
   `execution_result_missing` instead of the original stable error;
2. the deferred executor-to-service lazy imports were still present.

Both production causes were edited afterward: executor now conditionally
routes errors to `terminal_error`, and executor dependencies now point to
neutral/execution-owned modules. Per the user's later instruction, the focused
test was not rerun and no final GREEN claim is made.

## Verification intentionally omitted

The user explicitly instructed the agent to stop running tests and self-review,
finish the required implementation, and commit. Consequently, these requested
Task 6 commands were not run after the final edits:

```text
.venv/bin/python -m unittest tests.test_agent_runtime tests.test_agent_tooling tests.test_cmpf_agent -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q ai_native app.py
git diff --check
```

## Known concerns

- The final code is unverified after the compatibility and import-cycle edits.
- LangGraph's default permissive msgpack serializer emitted forward-looking
  warnings when restoring the checkpoint-safe `AgentAction`, `RunCounters`, and
  `SafeObservation` types. Task 7's checkpointer factory should explicitly
  allowlist these types or persist primitive projections before strict msgpack
  becomes the default.
- Artifact payloads are deliberately runtime-local in Task 6. Task 7 must keep
  checkpoint persistence free of business values while defining recovery for
  a process failure between `executor` and `observer`.
- The API continues through the compatibility service until Task 8, as required
  by the phased plan.
