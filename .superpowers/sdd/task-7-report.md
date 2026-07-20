# Task 7 report: persistent runs and checkpoints

## Status

Implemented and committed as **unverified**, following the explicit instruction not
to run tests or perform self-review.

## Delivered

- Added memory and PostgreSQL `AgentRun` lifecycle persistence with the six
  specified statuses, a one-active-or-waiting-run constraint per conversation,
  optimistic version claims, owner-scoped persistent cancellation, pending-run
  lookup, and status transitions.
- Added the PostgreSQL `agent_runs` schema and partial unique index.
- Added strict LangGraph serializer construction with an exact allowlist for
  `AgentAction`, observation models, `AgentBudgets`, and `RunCounters`.
- Added a memory/PostgreSQL checkpointer factory. PostgreSQL connections use
  autocommit, `prepare_threshold=0`, and `dict_row`, then run saver setup.
- Added `langgraph-checkpoint-postgres` to `requirements.txt`.
- Added persistent execution-result/artifact storage outside LangGraph state.
  Checkpoints retain only `pending_result_id` and artifact IDs; deterministic
  answer/chart payloads are loaded from the repository after process restart.
- Updated the runtime to persist a tool result before the executor node completes,
  recover it in the observer node, and load referenced artifacts in the responder.
  Bearer Token and request runtime context are never passed to these persistence
  APIs.
- Added seven-day execution-result/run retention and ordered cleanup of checkpoint
  writes/blobs/checkpoints before execution results, runs, messages, and
  conversations. Audit rows retain their independent ninety-day lifetime.
- Hardened audit and execution-result JSON persistence by recursively removing
  token, authorization, cookie, and raw-payload keys.
- Added essential memory/PostgreSQL repository contract scaffolding and extended
  checkpoint inspection assertions for cookie/raw payload keys.

## Verification intentionally omitted

No unit tests, PostgreSQL integration tests, compile checks, diff checks, or
self-review were run after implementation, per instruction. In particular, the
Task 7 RED/GREEN commands and Docker PostgreSQL startup were not executed.

## Known blockers and risks

- `langgraph-checkpoint-postgres` was added to requirements but was not installed in
  the existing virtualenv, so its factory integration is not locally exercised.
- PostgreSQL schema, cleanup queries, optimistic claims, and second-runtime restore
  behavior remain unverified until the deferred test pass.
- Task 8 still needs to connect API ingress/status/resume/cancel transitions to the
  new run repository methods.
