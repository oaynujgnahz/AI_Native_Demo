# CMPF Enterprise Agent Gateway Demo

[English](README.en.md) | 中文

独立于 CMPF 的只读 Enterprise Agent Gateway。它使用用户的 Keycloak access token 调用既有 CMPF API，并向浏览器返回文本与受限的 ECharts `ChartSpec`。本项目不修改 CMPF 的 Java、Vue、配置或数据库：**CMPF 保持不变，并继续作为公司范围与业务数据的最终授权权威**。

> 下文中的自动化与真实 CMPF 验收命令是可执行操作清单；本 README 更新本身未执行这些命令，也不宣称它们已通过。

## 运行时架构

请求先在 FastAPI 中完成 Keycloak JWT 认证，并创建仅存在于本次执行的 `RuntimeContext`（Principal、Bearer token、deadline、repository、取消检查）。随后由 **Policy-gated ReAct** LangGraph 执行受控循环：

```text
planner -> policy -> executor -> observer -> planner
                    |             |
                    |             +--> terminal_error
                    +--> clarifier --(checkpoint + user input)--> planner
planner -> responder -> end
```

- `ai_native/agent/runtime.py`：图节点、路由、恢复与受控结果。
- `ai_native/agent/planner.py`：将模型输出限制为 `call_tool`、`clarify` 或 `finish` 的 action schema；模型不可用时不会做不安全的业务回退。
- `ai_native/gateway/tooling.py`：唯一的十三个只读 typed-tool catalog。
- `ai_native/gateway/policy.py`：每次工具执行前检查取消、deadline、公司范围、参数、重复签名、预算与 active run；只有签名后的 approval 才能进入 executor。
- `ai_native/gateway/executor.py`：执行已批准的 CMPF 调用，生成确定性文本或安全的 `ChartSpec` artifact。
- `ai_native/gateway/observer.py`：仅把安全元数据回传给 planner。
- `ai_native/gateway/repository.py`、`checkpointer.py`：运行索引、artifact、审计与 checkpoint 的内存 / PostgreSQL 实现。
- `ai_native/observability/`：强制 JSON 日志与脱敏，以及可选 OTel tracing。

`AgentState`、日志、审计和 span **不会**保存 token、原始 CMPF DTO、排放数值、完整 ChartSpec 或隐藏推理。planner 可见的 `SafeObservation` 仅含公司 / 拠点标识、年份、scope、候选、计数和 artifact reference；真正的 answer / chart 由 executor 产出，再经 responder / API 返回。Bearer token 只短暂存在于 `RuntimeContext`，不会写入 checkpoint。

默认硬预算：8 次 planner、6 次工具、2 次 clarification；每个请求 segment 默认 deadline 为 45 秒。canonical `tool_name + arguments` 签名重复会被拒绝，不可再次执行。

### Run、checkpoint 与保留策略

一个 conversation 同时只能有一个 `running` 或 `waiting_for_user` 的 run。公开状态包括：`running`、`waiting_for_user`、`completed`、`failed`、`cancelled`、`exhausted`。需要 clarification 时，`clarifier` 节点会 interrupt 并写入 checkpoint；同一 conversation 的下一条 streaming message 会认领同一个 waiting run，用同一个 `run_id` resume，而不是新建 run。

会话、run、artifact 默认保留 7 天；审计默认保留 90 天。清理顺序应是：先清过期 run 的 checkpoint，再清 execution result、run、message / conversation，最后清审计。应用不会自动调度清理；部署方应按自身策略调用 repository 的 `delete_expired_agent_data`。

## 安装与本地内存 Demo

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入真实 CMPF / Keycloak / LLM 配置
export CMPF_GATEWAY_MODE=http
export CMPF_AGENT_DEMO_MODE=true
export CMPF_AGENT_DEMO_TOKEN='replace-with-a-local-secret'
unset DATABASE_URL
lsof -nP -iTCP:8787 -sTCP:LISTEN
kill <PID>
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787 --reload
```

Gateway **只支持真实 CMPF HTTP API**（`CMPF_GATEWAY_MODE=mock` 会被拒绝）。未设置 `DATABASE_URL` 时，conversation / run / artifact 与 checkpoint 使用进程内内存（适合本地 Demo；进程重启后无法 resume）。单元测试使用 `tests/fakes.py`，不会走 mock 业务数据。浏览器 Demo 的 Keycloak 登录面向真实环境；下方 demo token 的 curl 仅用于显式本地 Demo 认证。

创建 conversation 并发起 SSE 请求：

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

`X-Agent-Run-Id` 是本次响应的权威 run 标识。终端 SSE 顺序通常为：`status`（`tool_completed`）→ `answer.delta` → 可选 `visualization` → `answer.completed`。所有 SSE payload 都包含 `run_id`；`visualization` 仅包含已校验的 `ChartSpec`。

## HTTP API 与 clarification / resume

所有 conversation / run 接口都需要 `Authorization: Bearer <Keycloak access token>`，并校验归属。`POST /v1/conversations` 创建会话；`GET /v1/conversations/{conversation_id}/messages` 返回已持久化的 user / assistant 消息。

| 操作 | 精确路径 | 说明 |
| --- | --- | --- |
| 开始或恢复 | `POST /v1/conversations/{conversation_id}/messages/stream` | `text/event-stream`；向此接口提交 clarification 输入即可 resume 待处理 run。 |
| 查看 run | `GET /v1/conversations/{conversation_id}/runs/{run_id}` | 返回 `run_id`、status、`cancel_requested`、时间戳。 |
| 取消 run | `POST /v1/conversations/{conversation_id}/runs/{run_id}/cancel` | 返回 202 与公开 run status。对应文档中的 `/runs/{runId}/cancel`。 |
| 删除会话 | `DELETE /v1/conversations/{conversation_id}` | 返回 204。 |
| 检查 CMPF 连接 | `GET /v1/cmpf/connection` | 验证既有 Company API 访问是否可用。 |
| 公开 OIDC 配置 | `GET /v1/public-config` | 供浏览器 PKCE Demo 使用。 |
| 存活 / 就绪 | `GET /health/live`、`GET /health/ready` | readiness 检查的是已配置的 repository，不是 Jaeger。 |

若图需要年份、拠点选择或其他安全输入，stream 会发出：

```text
event: status
data: {"run_id":"…","state":"waiting_for_user"}

event: clarification
data: {"run_id":"…","question":"…","missing_fields":["year"],"candidates":[]}
```

向同一 stream 接口再次 POST 即可 resume 同一个 run。服务端仅在通过公司范围校验后采信受信任的 `context.company_id`、`year`、`locale`；checkpoint 中已有、本次未再提供的可信上下文会保留。

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

取消是服务端协同式：repository 将 active / waiting run 标记为 `cancelled`；policy 在每次 action 与工具调用前检查取消状态。可在另一终端对等待中 / 运行中的 stream 发起：

```bash
curl -sS -X POST \
  "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/runs/$RUN_ID/cancel" \
  -H 'Authorization: Bearer cmpf-demo-token'
```

不要把 202 当成“上游 HTTP 请求已被中断”的证明。验收流程应确认之后不再出现分析工具调用或 `visualization` 事件。

## PostgreSQL checkpoint 与 Jaeger

启动 PostgreSQL，并为 repository 与 LangGraph `PostgresSaver` 使用同一 `DATABASE_URL`：

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql://cmpf_agent:cmpf_agent@localhost:5432/cmpf_agent
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787
```

配置了 `DATABASE_URL` 后，启动时会创建 `agent_conversations`、`agent_messages`、`agent_audit`、`agent_runs`、`agent_execution_results`；saver 会创建自身 checkpoint 表。只要使用同一数据库且 run 未过期，新的应用进程仍可 resume `waiting_for_user` 的 run。

Jaeger 可选。先启动 observability profile，再开启 OTel 后启动 / 重启 Gateway：

```bash
docker compose --profile observability up -d
export OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787
```

打开 [Jaeger](http://localhost:16686)，选择服务 `cmpf-agent-gateway`，按 tag `run_id=<RUN_ID>` 查找请求链路。成功的人工检查应能看到 FastAPI、`agent.graph`、图节点 span（`agent.planner`、`agent.policy`、`agent.executor`、`agent.observer`，以及适用时的 `agent.clarifier` / `agent.responder`）、checkpoint / SSE span，以及实际发生调用时的 model 与 CMPF / httpx span。OTel 可选：`OTEL_ENABLED=false` 时不需要 exporter，exporter 错误也不会影响 Gateway 响应。

## 真实 CMPF / Keycloak 联调

使用真实端点；除明确的本地 Demo 外不要开启 demo token：

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

请为 Keycloak 客户端 `CaM-js` 配置 Valid Redirect URI：`http://localhost:8787/*`（若使用 `127.0.0.1` 也要加上对应 URI），并允许对应 Web Origin。打开 `http://localhost:8787/`，点击 **Keycloak Login**，完成 Authorization Code + PKCE。页面会调用 `/v1/cmpf/connection`；只有既有 Company API 访问成功时才显示 `CMPF connected`。Gateway 不接收用户名或密码。

生产环境建议为 Gateway 配置独立 audience（例如 `cmpf-agent-gateway`）；上文的 `CaM-app` 仅用于本地联调兼容。Gateway 使用用户 token 发起只读 CMPF 调用，先校验自社 / 直接子会社范围，且不会在 HTTP 模式下替换 mock 业务数据。最终授权仍由 CMPF 执行。

## 配置项

按需复制或加载 `.env.example`。重要变量如下：

| 变量 | 用途 / 默认 |
| --- | --- |
| `CMPF_GATEWAY_MODE` | 必须为 `http`（或未设置）。`mock` 会被拒绝。 |
| `CMPF_CARBON_API_BASE_URL`、`CMPF_USER_API_BASE_URL` | 既有 CMPF Carbon / User API 基址。 |
| `CMPF_LANG`、`CMPF_SITE_NAME` | 既有 CMPF 请求默认值（如适用）。 |
| `CMPF_KEYCLOAK_BASE_URL`、`CMPF_KEYCLOAK_REALM`、`CMPF_KEYCLOAK_ISSUER` | Keycloak discovery / issuer 配置。 |
| `CMPF_KEYCLOAK_CLIENT_ID`、`CMPF_KEYCLOAK_CLIENT_SECRET` | 浏览器客户端标识；不要把 confidential secret 暴露给浏览器。 |
| `CMPF_KEYCLOAK_AUDIENCE`、`CMPF_KEYCLOAK_ALLOWED_AUDIENCES` | JWT audience 校验。 |
| `CMPF_AGENT_CORS_ORIGINS` | 逗号分隔的允许浏览器来源。 |
| `CMPF_AGENT_RATE_LIMIT_PER_MINUTE`、`CMPF_AGENT_CONCURRENT_STREAMS` | 每用户限制；默认 20 / 2。 |
| `CMPF_AGENT_RUN_TIMEOUT_SECONDS` | 每个请求 segment 的 deadline；默认 45。 |
| `DATABASE_URL` | 启用 PostgreSQL repository 与 checkpoint saver；未设置则仅用内存。 |
| `CMPF_AGENT_DEMO_MODE`、`CMPF_AGENT_DEMO_TOKEN`、`CMPF_AGENT_DEMO_COMPANY_ID` | 仅用于显式本地 Demo 认证；共享 / 真实环境请保持 demo mode 为 false。 |
| `AI_NATIVE_LOG_LEVEL` | JSON 日志级别，默认 `INFO`。 |
| `OTEL_ENABLED` | 是否启用可选 tracing；默认 `false`。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`、`OTEL_EXPORTER_OTLP_TIMEOUT` | OTLP HTTP 目标 / 覆盖与超时。 |
| `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` | 从 FastAPI 埋点中排除的健康检查路径（逗号分隔）。 |
| `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL` | OpenAI 兼容 planner 配置。 |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` | 可选的兼容 planner 配置。 |

## CMPF 工具目录

唯一 typed catalog 包含十三个只读工具：`get_company_info`、`get_annual_emission_summary`、`get_scope_breakdown`、`get_scope_composition_chart`、`get_monthly_emission_trend_chart`、`get_top_emission_activities_chart`、`list_analysis_bases`、`get_base_emission_composition_chart`、`get_base_monthly_emission_chart`、`get_base_detail_composition_chart`、`get_base_detail_monthly_chart`、`compare_base_emissions_chart`、`compare_emission_periods_chart`。

相关既有 CMPF 映射包括：`/user/company/options?mode=01`、`/user/company/getCompanyStartMonth?mode=01`、`/user/company/getCompanyInfo?companyId=...`、`/dashBoard/scope_total_emission_volume`、`/dashBoard/scope_emission_volume`、`/analysis/scopeSummary`、`/analysis/scopeEmissionForMonth`、`/analysis/topActivityItemsByEmission`、`/analysis/baseInfoByCompanyGroup`、`/analysis/baseTypeEmission`、`/analysis/baseTypeEmissionForMonth`、`/analysis/baseLargeItemEmission`、`/analysis/baseMonthEmission`、`/analysis/compareByBase`、`/analysis/compareByDuration`。

拠点分析时，executor 会先校验公司范围，再通过 CMPF 解析拠点，并只允许精确匹配（或拉丁字母整名大小写不敏感匹配）。在最终分析工具前，至多三次准备调用（会社信息、会计起始月、拠点列表）。匹配失败或歧义时返回安全候选或 clarification，而不会直接发出最终分析请求。

## 验收清单（本文档任务未实际执行）

请在真实授权的 CMPF / Keycloak 环境、PostgreSQL，以及（如需验证 tracing）Jaeger profile 下操作。为每个用例记录 run ID、SSE 输出、CMPF 审计证据与 Jaeger 结果。

1. 提交 `親社拠点2の2025年月別排出量推移をグラフで表示して`；确认拠点解析 / replan、仅一次 `/analysis/baseMonthEmission` 业务调用，以及 `visualization` 事件。
2. 提交 `親社拠点2の月別排出量推移を表示して`；确认出现 `waiting_for_user` 与 `clarification`，再向同一 stream 提交 `2025年`，确认同一 `run_id` resume 并产出结果。
3. 使用歧义拠点名；确认给出安全候选，选择其一后 resume 同一 run。
4. 比较两个拠点或两个期间；确认 trace / 审计中至少有两个 agent action，并返回安全的 grouped chart。
5. 通过取消接口分别取消 waiting 与 running run；确认状态为 `cancelled`，且之后不再出现分析调用或 `visualization`。
6. 开启 OTel，在 Jaeger 按 `run_id` 搜索；确认一条 trace 含预期的 FastAPI、图节点、model、CMPF / httpx、checkpoint、SSE span，且不含凭证或原始业务 payload。

## 自动化校验命令（本文档任务未实际执行）

```bash
.venv/bin/python -m unittest tests.test_enterprise_gateway.ControlledLoopDocumentationTest -v
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q ai_native app.py
awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check
git diff --check
```
