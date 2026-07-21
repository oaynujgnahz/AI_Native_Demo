# CMPF Enterprise Agent Gateway Demo

独立于 CMPF 的只读 Enterprise Agent Gateway。Demo 阶段不修改 CMPF Java、Vue、配置或数据库；Gateway 使用用户的 Keycloak access token 调用既有 CMPF API，并输出文本与受限 ECharts `ChartSpec`。

## 已实现

- Keycloak JWT：JWKS 签名、issuer、audience、exp 校验，从 claim 读取 `userId`、`companyId`、`roleId`、locale。
- 公司范围：自社 + `/user/company/options?mode=01` 返回的直接子会社；CMPF 保持最终授权权威。
- 会话 API、SSE、PostgreSQL 会话/消息/图表/审计存储；未配置 `DATABASE_URL` 时使用进程内存，仅适合 Demo 测试。
- 十三个只读工具：会社信息、年度排放、Scope 内訳、Scope 构成、Scope 月度趋势、活动项目 Top10，以及拠点列表、拠点构成、拠点月度趋势、指定拠点构成、指定拠点月度趋势、拠点比较和期间比较。
- OpenAI-compatible LLM 自动工具选择：理解自然日语、中文和英语，只允许从十三个只读工具中选择一个；非法工具或模型故障时回退本地规则。
- 安全图表：仅允许 `pie`、`line`、`horizontal_bar`、`grouped_bar`，最多 5 个 series / 100 个总数据点，拒绝非有限数值和任意 ECharts option/JavaScript。
- 独立 Demo 页面：`GET /`，使用 Keycloak Authorization Code + PKCE 登录，不嵌入或修改 CMPF。
- 每用户 20 请求/分钟、2 个并发执行；审计不保存 token 和原始业务 payload。

LLM 只接收用户问题和经过校验的页面默认参数，用于意图识别与参数提取。CMPF API 返回的数据和生成的 ChartSpec 不发送给模型，工具执行、会社范围与最终权限判断仍由 Gateway/CMPF 完成。

## 本地启动

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入真实 CMPF / Keycloak / LLM 配置
docker compose up -d postgres
export DATABASE_URL=postgresql://cmpf_agent:cmpf_agent@localhost:5432/cmpf_agent
.venv/bin/uvicorn ai_native.api:app --host 127.0.0.1 --port 8787 --reload
```

Gateway **仅支持真实 HTTP API**（`CMPF_GATEWAY_MODE=mock` 已移除）。单元测试使用 `tests/fakes.py` 中的 Fake gateway，不会走 mock 业务数据。

## PostgreSQL

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql://cmpf_agent:cmpf_agent@localhost:5432/cmpf_agent
```

Gateway 首次启动会创建：

- `agent_conversations`，默认有效期 7 天。
- `agent_messages`，包含持久化 `ChartSpec`。
- `agent_audit`，默认有效期 90 天。

## 连接真实 CMPF / Keycloak

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

Keycloak 的 `CaM-js` 需要允许 `http://localhost:8787/*`（以及实际使用的 `127.0.0.1` 地址时对应的 URI）作为 Valid Redirect URI，并允许该 Web Origin。打开 `http://localhost:8787/` 后点击 **Keycloak Login**；登录回调完成后，页面会调用 `/v1/cmpf/connection`，只有真实访问 Company API 成功才显示 `CMPF connected`。Gateway 不接收用户名或密码。

生产环境仍建议为 Gateway 配置独立 audience `cmpf-agent-gateway`；`CaM-app` 只作为当前本地联调兼容 audience。

## API

- `POST /v1/conversations`
- `GET /v1/conversations/{id}/messages`
- `POST /v1/conversations/{id}/messages/stream`
- `DELETE /v1/conversations/{id}`
- `GET /v1/cmpf/connection`
- `GET /v1/public-config`
- `GET /health/live`
- `GET /health/ready`

所有会话 API 必须携带 `Authorization: Bearer <Keycloak access token>`。

```bash
CONVERSATION_ID=$(curl -s -X POST http://127.0.0.1:8787/v1/conversations \
  -H 'Authorization: Bearer cmpf-demo-token' -H 'Content-Type: application/json' \
  -d '{}' | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -N -X POST "http://127.0.0.1:8787/v1/conversations/$CONVERSATION_ID/messages/stream" \
  -H 'Authorization: Bearer cmpf-demo-token' -H 'Content-Type: application/json' \
  -d '{"message":"2025年の月別排出量推移グラフを表示","context":{"company_id":"cmpf-demo","year":2025,"locale":"ja"}}'
```

## CMPF API 映射

- `GET /user/company/options?mode=01`
- `GET /user/company/getCompanyStartMonth?mode=01`
- `GET /user/company/getCompanyInfo?companyId=...`
- `GET /dashBoard/scope_total_emission_volume`
- `GET /dashBoard/scope_emission_volume`
- `GET /analysis/scopeSummary`
- `GET /analysis/scopeEmissionForMonth`
- `GET /analysis/topActivityItemsByEmission`
- `GET /analysis/baseInfoByCompanyGroup`
- `POST /analysis/baseTypeEmission`
- `POST /analysis/baseTypeEmissionForMonth`
- `GET /analysis/baseLargeItemEmission`
- `GET /analysis/baseMonthEmission`
- `POST /analysis/compareByBase`
- `POST /analysis/compareByDuration`

`get_monthly_emission_trend_chart` 明确映射到排出量分析接口 `GET /analysis/scopeEmissionForMonth`。新增工具名为：

- `list_analysis_bases`
- `get_base_emission_composition_chart`
- `get_base_monthly_emission_chart`
- `get_base_detail_composition_chart`
- `get_base_detail_monthly_chart`
- `compare_base_emissions_chart`
- `compare_emission_periods_chart`

涉及拠点的请求使用受控 Loop：Gateway 先验证会社范围，再通过 CMPF 查询拠点。拠点名称只允许精确匹配或不区分拉丁字母大小写的完整匹配，取得并验证 `baseId` 后自动继续分析。同名或无匹配时返回安全候选，不调用最终分析接口。

The bounded loop allows at most 3 preparation calls (company information, fiscal start month, and site list) and at most one final business analysis tool. The mandatory self/direct-subsidiary authorization check runs before the loop and is never skipped. CMPF responses, ChartSpec data, and Bearer Tokens are never sent to the model.

自然语言示例：

- `東京拠点の2025年月別排出量を表示して`
- `比较东京据点和大阪据点 2025 年排出量`
- `Compare emissions for 2024/04–2025/03 and 2025/04–2026/03`

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```
