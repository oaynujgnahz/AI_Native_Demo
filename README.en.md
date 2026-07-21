# CMPF Enterprise Agent Gateway Demo

[English](README.en.md) | [中文](README.md)

A read-only Enterprise Agent Gateway that sits beside CMPF. It uses the user's Keycloak access token to call existing CMPF APIs and returns text plus a constrained ECharts `ChartSpec` to the browser. This project does not modify CMPF Java, Vue, configuration, or databases: **CMPF remains unchanged and remains the final authority for company scope and business data authorization**.

> The automated and live CMPF acceptance commands below are an executable checklist. This README update did not run those commands and does not claim they have passed.

## Runtime architecture

Requests are authenticated with Keycloak JWT in FastAPI, then a per-request `RuntimeContext` is created (Principal, Bearer token, deadline, repository, cancellation checks). A **Policy-gated ReAct** LangGraph then runs the controlled loop:

```text
planner -> policy -> executor -> observer -> planner
                    |             |
                    |             +--> terminal_error
                    +--> clarifier --(checkpoint + user input)--> planner
planner -> responder -> end
```

- `ai_native/agent/runtime.py`: graph nodes, routing, resume, and controlled results.
- `ai_native/agent/planner.py`: constrains model output to a `call_tool`, `clarify`, or `finish` action schema; when the model is unavailable it does not invent unsafe business fallbacks.
- `ai_native/gateway/tooling.py`: the single typed catalog of thirteen read-only tools.
- `ai_native/gateway/policy.py`: before every tool execution, checks cancellation, deadline, company scope, arguments, duplicate signatures, budgets, and the active run; only a signed approval reaches the executor.
- `ai_native/gateway/executor.py`: runs one approved CMPF call and produces deterministic text or a safe `ChartSpec` artifact.
- `ai_native/gateway/observer.py`: feeds only safe metadata back to the planner.
- `ai_native/gateway/repository.py`, `checkpointer.py`: in-memory / PostgreSQL implementations for run indexes, artifacts, audit, and checkpoints.
- `ai_native/observability/`: required JSON logging with redaction, plus optional OTel tracing.

`AgentState`, logs, audit records, and spans never store tokens, raw CMPF DTOs, emission numbers, full ChartSpecs, or hidden reasoning. The planner-visible `SafeObservation` contains only company/site identifiers, year, scope, candidates, counts, and artifact references. The real answer/chart is an executor artifact returned by the responder/API. The Bearer token exists only briefly in `RuntimeContext` and is never written into a checkpoint.

Default hard budgets are 8 planner turns, 6 tools, and 2 clarifications, with a 45-second default deadline per request segment. A repeated canonical `tool_name + arguments` signature is rejected and cannot run again.

### Runs, checkpoints, and retention

A conversation may have only one `running` or `waiting_for_user` run at a time. Public statuses are: `running`, `waiting_for_user`, `completed`, `failed`, `cancelled`, `exhausted`. Clarification interrupts at the `clarifier` node and writes a checkpoint; the next streaming message on the same conversation claims that waiting run and resumes with the same `run_id` instead of creating a new run.

Conversations, runs, and artifacts are retained for 7 days by default; audit records for 90 days. Cleanup should delete expired-run checkpoints first, then execution results, runs, messages/conversations, and finally audit rows. The app does not schedule cleanup automatically; operators should call repository `delete_expired_agent_data` according to their retention policy.

## Install and local memory demo

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # set real CMPF / Keycloak / LLM values
export CMPF_GATEWAY_MODE=http
export CMPF_AGENT_DEMO_MODE=true
export CMPF_AGENT_DEMO_TOKEN='replace-with-a-local-secret'
unset DATABASE_URL
lsof -nP -iTCP:8787 -sTCP:LISTEN
kill <PID>
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787 --reload
```

The Gateway **only supports real CMPF HTTP APIs** (`CMPF_GATEWAY_MODE=mock` is rejected). Without `DATABASE_URL`, conversation/run/artifact and checkpoint storage stay in-process memory (fine for local demos; resume does not survive restart). Unit tests use `tests/fakes.py`, not mock business data. Browser Demo Keycloak login targets a real environment; the demo-token curl below is only for explicit local demo auth.

Create a conversation and start an SSE request:

```bash
CONVERSATION_ID=$(curl -sS -X POST http://127.0.0.1:8787/v1/conversations \
  -H 'Authorization: Bearer cmpf-demo-token' \
  -H 'Content-Type: application/json' \
  -d '{}' | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -sS -N -D /tmp/cmpf-agent-headers -X POST \
  "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/messages/stream" \
  -H 'Authorization: Bearer cmpf-demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"message":"2025年の月別排出量推移グラフを表示","context":{"company_id":"cmpf-demo","year":2025,"locale":"ja"}}'

RUN_ID=$(awk 'tolower($1) == "x-agent-run-id:" {print $2}' /tmp/cmpf-agent-headers | tr -d '\r')
```

`X-Agent-Run-Id` is the authoritative run identifier for the response. The terminal SSE sequence is `status` (`tool_completed`), `answer.delta`, optional `visualization`, then `answer.completed`. All SSE payloads include `run_id`; `visualization` contains only a validated `ChartSpec`.

## HTTP API and clarification/resume

All conversation/run endpoints require `Authorization: Bearer <Keycloak access token>` and enforce ownership. `POST /v1/conversations` creates a conversation; `GET /v1/conversations/{conversation_id}/messages` returns persisted user/assistant messages.

| Operation | Exact endpoint | Notes |
| --- | --- | --- |
| Start or resume | `POST /v1/conversations/{conversation_id}/messages/stream` | `text/event-stream`; a pending run is resumed by posting clarification input here. |
| Inspect run | `GET /v1/conversations/{conversation_id}/runs/{run_id}` | Returns `run_id`, status, `cancel_requested`, timestamps. |
| Cancel run | `POST /v1/conversations/{conversation_id}/runs/{run_id}/cancel` | Returns 202 and the public run status. This is the concrete form of the `/runs/{runId}/cancel` route suffix. |
| Delete conversation | `DELETE /v1/conversations/{conversation_id}` | Returns 204. |
| Check CMPF connection | `GET /v1/cmpf/connection` | Verifies existing Company API access. |
| Public OIDC configuration | `GET /v1/public-config` | Used by the browser PKCE Demo. |
| Liveness/readiness | `GET /health/live`, `GET /health/ready` | Readiness checks the configured repository, not Jaeger. |

If the graph needs a year, site choice, or other safe input, the stream emits:

```text
event: status
data: {"run_id":"…","state":"waiting_for_user"}

event: clarification
data: {"run_id":"…","question":"…","missing_fields":["year"],"candidates":[]}
```

Resume the same run by posting the answer to the same stream endpoint. The server takes trusted `context.company_id`, `year`, and `locale` only after company-range validation; it retains missing trusted context from the checkpoint.

```bash
curl -sS -N -X POST \
  "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/messages/stream" \
  -H 'Authorization: Bearer cmpf-demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"message":"2025年","context":{"year":2025,"locale":"ja"}}'

curl -sS \
  -H 'Authorization: Bearer cmpf-demo-token' \
  "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/runs/$RUN_ID"
```

Cancellation is server-side and cooperative: the repository marks an active or waiting run `cancelled`; policy checks cancellation before every action and tool call. To request it from another terminal while a stream is waiting/running:

```bash
curl -sS -X POST \
  "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/runs/$RUN_ID/cancel" \
  -H 'Authorization: Bearer cmpf-demo-token'
```

Do not treat a 202 response as proof that an already in-flight upstream HTTP request was interrupted. The acceptance procedure below checks that no later analysis action or visualization is emitted.

## PostgreSQL checkpoints and Jaeger

Start PostgreSQL and use the same URL for the repository and LangGraph `PostgresSaver` checkpoint store:

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql://cmpf_agent:cmpf_agent@localhost:5432/cmpf_agent
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787
```

With `DATABASE_URL`, startup creates `agent_conversations`, `agent_messages`, `agent_audit`, `agent_runs`, and `agent_execution_results`; the saver creates its checkpoint tables. A new application runtime can resume a `waiting_for_user` run provided it uses the same database and the run has not expired.

Jaeger is optional. Start its compose profile and enable OTel before starting/restarting the Gateway:

```bash
docker compose --profile observability up -d
export OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787
```

Open [Jaeger](http://localhost:16686), select service `cmpf-agent-gateway`, then add tag `run_id=<RUN_ID>` to locate the request trace. A successful manual trace inspection should show FastAPI, `agent.graph`, graph-node spans (`agent.planner`, `agent.policy`, `agent.executor`, `agent.observer`, and where applicable `agent.clarifier`/`agent.responder`), checkpoint/SSE spans, plus model and CMPF/httpx spans when those calls occur. OTel is optional: `OTEL_ENABLED=false` does not require an exporter, and exporter errors are isolated from Gateway responses.

## Real CMPF and Keycloak flow

Use real endpoints and do not enable the demo token outside explicit local demo work:

```bash
export CMPF_GATEWAY_MODE=http
export CMPF_CARBON_API_BASE_URL=http://localhost:80
export CMPF_USER_API_BASE_URL=http://localhost:8083
export CMPF_KEYCLOAK_ISSUER='https://authdev.carbon-management.ntt.com/auth/realms/TEST'
export CMPF_KEYCLOAK_CLIENT_ID='CaM-js'
export CMPF_KEYCLOAK_AUDIENCE='CaM-app'
export CMPF_KEYCLOAK_ALLOWED_AUDIENCES='CaM-app'
export CMPF_AGENT_DEMO_MODE=false
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787 --reload
```

Configure the Keycloak client `CaM-js` with `http://localhost:8787/*` (and the matching `127.0.0.1` address when used) as a Valid Redirect URI and allow the corresponding Web Origin. Open `http://localhost:8787/`, select **Keycloak Login**, and complete the Authorization Code + PKCE flow. The page calls `/v1/cmpf/connection`; only a successful existing Company API access displays `CMPF connected`. Gateway never accepts a username or password.

For production, configure an audience dedicated to the Gateway (for example `cmpf-agent-gateway`); `CaM-app` above is local interoperability configuration. The Gateway makes read-only CMPF calls with the user token, validates self/direct-subsidiary scope first, and never substitutes mock business data in HTTP mode. CMPF must still enforce the final authorization decision.

## Configuration

Copy or source `.env.example` as appropriate. Important variables are:

| Variable | Purpose / default |
| --- | --- |
| `CMPF_GATEWAY_MODE` | Must be `http` (or unset). `mock` is rejected. |
| `CMPF_CARBON_API_BASE_URL`, `CMPF_USER_API_BASE_URL` | Existing CMPF Carbon/User API bases. |
| `CMPF_LANG`, `CMPF_SITE_NAME` | Existing CMPF request defaults, when applicable. |
| `CMPF_KEYCLOAK_BASE_URL`, `CMPF_KEYCLOAK_REALM`, `CMPF_KEYCLOAK_ISSUER` | Keycloak discovery/issuer configuration. |
| `CMPF_KEYCLOAK_CLIENT_ID`, `CMPF_KEYCLOAK_CLIENT_SECRET` | Browser client identity; do not expose a confidential secret to the browser. |
| `CMPF_KEYCLOAK_AUDIENCE`, `CMPF_KEYCLOAK_ALLOWED_AUDIENCES` | JWT audience validation. |
| `CMPF_AGENT_CORS_ORIGINS` | Comma-separated allowed browser origins. |
| `CMPF_AGENT_RATE_LIMIT_PER_MINUTE`, `CMPF_AGENT_CONCURRENT_STREAMS` | Per-user limits; defaults 20 and 2. |
| `CMPF_AGENT_RUN_TIMEOUT_SECONDS` | Per request-segment deadline; defaults 45. |
| `DATABASE_URL` | Enables PostgreSQL repository and checkpoint saver; unset means memory only. |
| `CMPF_AGENT_DEMO_MODE`, `CMPF_AGENT_DEMO_TOKEN`, `CMPF_AGENT_DEMO_COMPANY_ID` | Explicit local demo authentication only; keep demo mode false in shared/real environments. |
| `AI_NATIVE_LOG_LEVEL` | JSON logging level, default `INFO`. |
| `OTEL_ENABLED` | Enables optional tracing; default `false`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_TIMEOUT` | OTLP HTTP target/override and exporter timeout. |
| `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` | Comma-separated health paths excluded from FastAPI instrumentation. |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | OpenAI-compatible planner settings. |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` | Optional compatible planner settings. |

## CMPF tool catalog

The single typed catalog contains thirteen read-only tools: `get_company_info`, `get_annual_emission_summary`, `get_scope_breakdown`, `get_scope_composition_chart`, `get_monthly_emission_trend_chart`, `get_top_emission_activities_chart`, `list_analysis_bases`, `get_base_emission_composition_chart`, `get_base_monthly_emission_chart`, `get_base_detail_composition_chart`, `get_base_detail_monthly_chart`, `compare_base_emissions_chart`, and `compare_emission_periods_chart`.

Relevant existing CMPF mappings include `/user/company/options?mode=01`, `/user/company/getCompanyStartMonth?mode=01`, `/user/company/getCompanyInfo?companyId=...`, `/dashBoard/scope_total_emission_volume`, `/dashBoard/scope_emission_volume`, `/analysis/scopeSummary`, `/analysis/scopeEmissionForMonth`, `/analysis/topActivityItemsByEmission`, `/analysis/baseInfoByCompanyGroup`, `/analysis/baseTypeEmission`, `/analysis/baseTypeEmissionForMonth`, `/analysis/baseLargeItemEmission`, `/analysis/baseMonthEmission`, `/analysis/compareByBase`, and `/analysis/compareByDuration`.

For site analysis, the executor checks company scope, resolves a site through CMPF, and permits only an exact (or Latin-case-insensitive whole-name) match. It may use at most three preparation calls (company information, fiscal start month, site list) before one final analysis tool. Ambiguous/no-match sites produce safe candidates or clarification instead of issuing the final analysis request.

## Acceptance checklist (not executed by this documentation task)

Use a real authorized CMPF/Keycloak environment, PostgreSQL, and—when checking tracing—the Jaeger profile. Record the run IDs, SSE output, CMPF audit evidence, and Jaeger result for each case.

1. Submit `親社拠点2の2025年月別排出量推移をグラフで表示して`; verify site resolution/replan, a single `/analysis/baseMonthEmission` business call, and a `visualization` event.
2. Submit `親社拠点2の月別排出量推移を表示して`; verify `waiting_for_user` plus `clarification`, submit `2025年` to the same stream endpoint, then verify the same `run_id` resumes and produces the result.
3. Use an ambiguous site name; verify safe candidates are offered, choose one, and resume the same run.
4. Compare two sites or two periods; verify at least two agent actions in the trace/audit and a safe grouped chart response.
5. Cancel a waiting and a running run through the cancellation endpoint; verify its status is `cancelled` and that no subsequent analysis call or `visualization` event occurs.
6. Enable OTel and search Jaeger by `run_id`; verify one trace contains the expected FastAPI, graph-node, model, CMPF/httpx, checkpoint, and SSE spans without credentials or raw payloads.

## Automated verification commands (not executed by this documentation task)

```bash
.venv/bin/python -m unittest tests.test_enterprise_gateway.ControlledLoopDocumentationTest -v
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q ai_native app.py
awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check
git diff --check
```
