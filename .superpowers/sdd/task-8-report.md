# Task 8 report: resumable run APIs and Demo wiring

## Status

Implemented and ready to commit as **unverified**, following the explicit
instruction not to run tests or perform self-review.

## Delivered

- Replaced the API's one-shot service call with a graph-backed `AgentRuntime`
  constructed once with the configured repository and checkpointer.
- Made the message stream create a persistent run for a new request or
  optimistically claim the conversation's waiting run for resume, always
  persisting the user message before graph invocation.
- Built `allowed_company_ids` at every start and resume from the authenticated
  principal plus the current Token's direct-child-company response. Forged
  request companies are denied at ingress, and a checkpoint's prior target is
  revalidated before a waiting run is claimed or any graph node resumes.
- Kept the Bearer Token in request-scoped `RuntimeContext`; only safe trusted
  company IDs, year, and locale enter checkpoint state.
- Passed clarification input and refreshed trusted context through
  `Command(resume={"message": ..., "context": ...})`, updating checkpoint-safe
  context before resumed planning.
- Persisted `waiting_for_user`, terminal run statuses, clarification questions,
  completed assistant messages, and deterministic ChartSpec payloads.
- Added owner/conversation/company-scoped run status and persistent cancel APIs.
- Added `run_id` to every emitted SSE event and added the `clarification` event
  while preserving `status`, `answer.delta`, `visualization`, and
  `answer.completed`.
- Added the run ID response header used by the Demo and exposed it through CORS.
- Updated the Demo to retain a waiting run ID, render clarification as an
  assistant question, resume on the next Send, and call server cancellation
  before aborting the browser stream.
- Preserved the previous injected one-shot planner contract through a focused
  adapter while allowing native action planners to drive the controlled loop.
- Applied the two minimal runtime integration corrections required by the API:
  refreshed trusted context on resume, and active-run ownership that permits a
  policy-validated direct-child target while binding the run to the principal's
  owning company.
- Preserved safe validation candidates from executor failures so pre-stream
  stable validation responses remain useful without exposing raw payloads.

## Verification intentionally omitted

No unit tests, JavaScript syntax check, Python compile check, diff check, or
self-review was run, per instruction. The Task 8 RED/GREEN and Demo syntax
commands remain deferred.

## Known blockers and risks

- Task 6 and Task 7 final edits were already unverified, so this integration is
  layered on unverified graph/checkpointer and persistence behavior.
- PostgreSQL checkpoint resume, optimistic-claim races, and cancellation during
  an actively executing tool remain unexercised.
- The legacy planner adapter and the new native action-planner path have not
  been exercised against the existing API compatibility suite.
