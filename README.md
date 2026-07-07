# CMPF AI Native Agent

LangGraph-based business agent for the local CMPF carbon emission system.

## Current Capabilities

- LangGraph state workflow: plan -> business tool -> answer.
- CMPF Gateway with two modes:
  - `mock`: local development without starting CMPF.
  - `http`: call the existing CMPF carbon API.
- Tool registry with read permission checks.
- JSONL audit log for every tool call.
- CLI entry point for local testing.
- FastAPI entry point for future UI integration.

## Run CLI

```bash
.venv/bin/python app.py
```

Example:

```text
帮我查一下 cmpf-demo 公司 2025 年碳排放情况
查看 cmpf-demo 2025 年 Scope 明细
```

## Run API

```bash
lsof -nP -iTCP:8787 -sTCP:LISTEN
kill <PID>
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787 --reload
```

Endpoints:

- `GET /health`
- `GET /tools`
- `POST /chat`

Example:

```bash
curl -X POST http://127.0.0.1:8787/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"帮我查一下 cmpf-demo 公司 2025 年碳排放情况","company_id":"cmpf-demo","year":2025,"permissions":["cmpf:read"]}'
```

## Connect Real CMPF

Copy `.env.example` to `.env` or export variables in the shell:

```bash
export CMPF_GATEWAY_MODE=http
export CMPF_CARBON_API_BASE_URL=http://localhost:8080
export CMPF_USER_API_BASE_URL=http://localhost:8081
export CMPF_AUTH_TOKEN='your-local-keycloak-token'
```

Currently mapped CMPF endpoints:

- `get_emission_dashboard` -> `GET /dashBoard/scope_total_emission_volume`
- `get_scope_breakdown` -> `GET /dashBoard/scope_emission_volume`

## Verify

```bash
.venv/bin/python -m unittest discover -s tests -v
```
