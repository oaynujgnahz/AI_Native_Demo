# CMPF Enterprise Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 CMPF 只读 Agent 原型升级为具备可信身份、租户隔离、可评测、可观测和可部署能力的企业内部生产系统。

**Architecture:** FastAPI 作为受信任的接入层，验证 Keycloak JWT 并生成不可由客户端伪造的 `BusinessContext`；LangGraph 只负责有界编排，所有工具调用统一经过参数校验、策略决策、审计和 CMPF Gateway。会话状态、审计、追踪和评测独立于模型供应商，保证模型可替换且不成为权限边界。

**Tech Stack:** Python 3.13、FastAPI、Pydantic、LangGraph、Keycloak OIDC/JWKS、PyJWT、httpx、PostgreSQL、Redis、OpenTelemetry、pytest、Docker。

## Global Constraints

- 权限、用户、租户和公司范围只能从已验证的 JWT 或服务端策略产生，不能接受客户端自报。
- Access Token 只能通过 `Authorization: Bearer` 传输，不进入请求 JSON、Agent state、普通日志或审计参数。
- 模型只能提出工具调用建议；工具参数验证和授权决策必须由确定性服务端代码完成。
- 生产环境禁止 mock gateway、默认公司 `cmpf-demo`、默认租户和缺失身份降级。
- 所有改动采用测试先行；每个任务完成后运行全量测试。
- 第一阶段只保留只读工具，不增加写操作和无限自主循环。

---

## 目标架构

```text
Browser / API Client
        |
        | OIDC Authorization Code + PKCE / Bearer JWT
        v
FastAPI Authentication Dependency
        |-- JWT signature / issuer / audience / expiry
        |-- user_id / tenant_id / roles / company scope
        v
Trusted BusinessContext
        v
Bounded LangGraph Orchestrator
        v
ToolRegistry
        |-- Pydantic input validation
        |-- tenant/company authorization
        |-- timeout/retry/error mapping
        |-- immutable audit event
        v
CMPF Gateway ---- OpenTelemetry / Metrics / Logs
```

## 里程碑与排期

| 阶段 | 建议周期 | 目标 | 发布门槛 |
|---|---:|---|---|
| M1 可信安全边界 | 2 周 | 可安全进入内部测试环境 | 未认证、伪造权限、跨租户访问全部被阻断 |
| M2 可用 Agent | 2～3 周 | 支持可靠多轮查询和稳定降级 | 会话隔离、参数澄清、错误恢复、核心评测通过 |
| M3 生产化 | 2～3 周 | 可观测、可部署、可回滚 | CI/CD、SLO、告警、容量与安全门禁通过 |
| M4 业务扩展 | 持续迭代 | 趋势、对比、报表、审批式写操作 | 每个新工具独立授权、评测和审批 |

---

### Task 1: 建立 Keycloak JWT 验证边界

**Files:**
- Create: `ai_native/auth/__init__.py`
- Create: `ai_native/auth/models.py`
- Create: `ai_native/auth/jwt_verifier.py`
- Create: `tests/test_auth.py`
- Modify: `requirements.txt`
- Modify: `.env.example`

**Interfaces:**
- Produces: `AuthenticatedPrincipal(user_id: str, tenant_id: str, roles: frozenset[str], permissions: frozenset[str], company_ids: frozenset[str])`
- Produces: `KeycloakJwtVerifier.verify(token: str) -> AuthenticatedPrincipal`
- Validation: RS256 signature、`iss`、`aud`、`exp`、`nbf`；拒绝 `alg=none` 和未知算法。

- [ ] 添加 `PyJWT[crypto]` 依赖和 Keycloak issuer、audience、claim-name 配置。
- [ ] 先编写 JWT 缺失、过期、错误 audience、错误 issuer、错误签名和合法 token 测试。
- [ ] 运行 `python -m unittest tests.test_auth -v`，确认新测试失败。
- [ ] 实现 `AuthenticatedPrincipal` 和带 JWKS 缓存的 `KeycloakJwtVerifier`。
- [ ] 运行 `python -m unittest tests.test_auth -v`，预期全部通过。
- [ ] 运行 `python -m unittest discover -s tests -v`，预期无回归。
- [ ] 提交：`git commit -m "feat: verify Keycloak JWT identity"`。

**Acceptance:** 任意未验证 token 都不能产生 `AuthenticatedPrincipal`；错误返回 401，响应和日志不包含 token。

### Task 2: 从 API 请求中移除自报身份和权限

**Files:**
- Create: `ai_native/auth/dependencies.py`
- Modify: `ai_native/api.py`
- Modify: `ai_native/gateway/context.py`
- Test: `tests/test_cmpf_agent.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `KeycloakJwtVerifier.verify()`。
- Produces: FastAPI dependency `require_principal(authorization: str) -> AuthenticatedPrincipal`。
- Produces: `BusinessContext.from_principal(principal, company_id) -> BusinessContext`。

- [ ] 修改 API 测试，证明匿名 `/chat` 返回 401、客户端传入 `permissions` 返回 422、合法 JWT 身份进入上下文。
- [ ] 运行目标测试并确认失败。
- [ ] 从 `ChatRequest` 删除 `user_id`、`tenant_id`、`permissions`、`auth_token`。
- [ ] 只从 Authorization header 读取 Bearer token，并由 dependency 注入 principal。
- [ ] 删除生产路径中的 `local-user`、`local` 和默认 `cmpf:read`。
- [ ] 浏览器端停止把 access token 放入 JSON body，统一使用 Authorization header。
- [ ] 运行 API 测试和全量测试。
- [ ] 提交：`git commit -m "fix: derive agent identity from verified token"`。

**Acceptance:** 修改 JSON 请求无法提升权限、伪造用户或切换租户；审计中的身份来自已验证 claims。

### Task 3: 实现租户和公司范围授权

**Files:**
- Create: `ai_native/policy/__init__.py`
- Create: `ai_native/policy/authorization.py`
- Create: `tests/test_authorization.py`
- Modify: `ai_native/gateway/registry.py`
- Modify: `ai_native/gateway/context.py`

**Interfaces:**
- Produces: `AuthorizationDecision(allowed: bool, reason: str)`。
- Produces: `ToolAuthorizationPolicy.authorize(context, tool, arguments) -> AuthorizationDecision`。
- Rule: 同时满足工具 permission 与 `arguments.company_id in context.company_ids`。

- [ ] 编写同租户允许、跨租户拒绝、空公司范围拒绝、工具权限不足拒绝测试。
- [ ] 运行测试确认失败。
- [ ] 在 ToolRegistry 调用 handler 前执行策略决策。
- [ ] 对拒绝结果写入 `permission_denied` 或 `company_scope_denied` 审计事件。
- [ ] 验证模型生成不同 company_id 时仍被服务端拒绝。
- [ ] 运行授权测试和全量测试。
- [ ] 提交：`git commit -m "feat: enforce tenant and company tool policy"`。

**Acceptance:** 用户无法通过 Prompt、请求参数或模型工具参数查询授权范围外的公司。

### Task 4: 为工具建立强类型输入输出契约

**Files:**
- Create: `ai_native/tools/schemas.py`
- Modify: `ai_native/gateway/registry.py`
- Modify: `ai_native/agent/graph.py`
- Test: `tests/test_tool_validation.py`

**Interfaces:**
- Produces: `EmissionQuery(company_id: str, year: int)`，year 范围 `2000..2100`。
- Produces: `CompanyQuery(company_id: str)`，company_id 长度 `1..64`，仅允许字母、数字、下划线和连字符。
- Produces: `ToolSpec.input_model: type[BaseModel]`。

- [ ] 编写缺字段、多余字段、非法 company_id、越界 year 和错误类型测试。
- [ ] 运行测试确认失败。
- [ ] 使用 Pydantic 模型验证所有 LLM 和规则生成的参数。
- [ ] 从模型 schema 和服务端模型生成同一份 JSON Schema，避免双重定义漂移。
- [ ] 删除 `cmpf-demo` 和 `2025` 静默默认值；缺参数返回结构化 `clarification_required`。
- [ ] 运行工具验证测试和全量测试。
- [ ] 提交：`git commit -m "feat: validate tool contracts server-side"`。

**Acceptance:** 无效模型输出不会调用 CMPF，不会形成 500；缺失公司或年度时 Agent 明确追问。

### Task 5: 统一上游错误、重试和失败审计

**Files:**
- Create: `ai_native/errors.py`
- Modify: `ai_native/gateway/cmpf_client.py`
- Modify: `ai_native/gateway/registry.py`
- Modify: `ai_native/gateway/audit.py`
- Modify: `ai_native/api.py`
- Test: `tests/test_gateway_failures.py`

**Interfaces:**
- Produces: `AgentError(code: str, safe_message: str, retryable: bool)`。
- Codes: `AUTH_FAILED`、`INVALID_ARGUMENT`、`PERMISSION_DENIED`、`UPSTREAM_TIMEOUT`、`UPSTREAM_UNAVAILABLE`、`MODEL_UNAVAILABLE`、`TOOL_EXECUTION_FAILED`。

- [ ] 编写 CMPF timeout、401、403、429、500、无效 JSON 和网络错误测试。
- [ ] 运行测试确认失败。
- [ ] 将 httpx 异常映射成稳定的 `AgentError`，只对 timeout、429 和 5xx 做最多两次指数退避重试。
- [ ] 确保 ToolRegistry 用 `try/finally` 写 success、denied 或 failed 审计。
- [ ] API 返回稳定错误码和 request_id，不暴露堆栈、token 或上游响应正文。
- [ ] 运行失败路径测试和全量测试。
- [ ] 提交：`git commit -m "feat: add resilient tool error handling"`。

**Acceptance:** 上游故障不会泄漏敏感信息；每次工具尝试都有最终审计状态。

### Task 6: 安全日志、关联 ID 和真实健康检查

**Files:**
- Create: `ai_native/observability.py`
- Modify: `ai_native/logging_config.py`
- Modify: `ai_native/api.py`
- Modify: `ai_native/agent/llm.py`
- Modify: `ai_native/agent/graph.py`
- Test: `tests/test_observability.py`

**Interfaces:**
- Produces: `request_id`、`conversation_id`、`trace_id` 结构化字段。
- Produces: `/health/live` 仅检查进程；`/health/ready` 检查必要配置和依赖。

- [ ] 编写日志中不出现 token、密码和完整用户消息的测试。
- [ ] 添加 request-id middleware，并把 ID 传入审计事件和 API 响应 header。
- [ ] 将用户正文替换为长度、哈希或显式开启的脱敏采样。
- [ ] 增加 live/readiness endpoint；生产配置为 mock 或缺少 issuer/audience 时 readiness 失败。
- [ ] 增加模型耗时、工具耗时、结果状态和 token usage 指标接口。
- [ ] 运行可观测测试和全量测试。
- [ ] 提交：`git commit -m "feat: add safe structured observability"`。

**M1 发布门禁:**

- [ ] 匿名访问、伪造权限、错误 JWT、跨租户 company_id 测试全部通过。
- [ ] 日志和审计经过 secret/PII 扫描。
- [ ] mock mode 无法在 production profile 启动。
- [ ] 在 staging 使用真实 Keycloak 和只读 CMPF API 完成集成测试。

---

### Task 7: 多轮会话和状态隔离

**Files:**
- Create: `ai_native/conversation/service.py`
- Create: `ai_native/conversation/models.py`
- Modify: `ai_native/agent/graph.py`
- Modify: `ai_native/api.py`
- Test: `tests/test_conversation.py`

**Interfaces:**
- API 接受 `conversation_id: UUID | None`，服务端校验其归属用户和租户。
- LangGraph 使用 PostgreSQL checkpointer；Redis 仅用于短期缓存和限流。

- [ ] 增加新会话、续接会话、跨用户读取拒绝、跨租户读取拒绝和过期清理测试。
- [ ] 接入持久化 checkpointer，并为每次调用设置 user/tenant namespaced thread key。
- [ ] 实现上下文窗口预算、历史摘要和数据保留期。
- [ ] 支持“去年”“刚才那家公司”等多轮引用测试。
- [ ] 提交：`git commit -m "feat: persist isolated agent conversations"`。

### Task 8: 有界 Agent 循环与澄清流程

**Files:**
- Modify: `ai_native/agent/state.py`
- Modify: `ai_native/agent/graph.py`
- Create: `tests/test_agent_workflow.py`

**Interfaces:**
- 状态包含 `step_count`、`max_steps=4`、`deadline_ms`、`pending_clarification`。
- 单次请求最多 4 个节点步骤、最多 2 次工具调用，只允许 registry 中的只读工具。

- [ ] 编写缺参数澄清、工具失败恢复、循环终止、超时和多工具组合测试。
- [ ] 实现 `plan -> validate -> authorize -> tool -> assess -> answer/clarify` 有界图。
- [ ] 对达到预算的任务返回可解释的部分结果，不继续自主调用。
- [ ] 提交：`git commit -m "feat: add bounded agent workflow"`。

### Task 9: 建立 Agent 评测门禁

**Files:**
- Create: `evals/cmpf_queries.jsonl`
- Create: `evals/security_cases.jsonl`
- Create: `evals/run_evals.py`
- Create: `tests/test_evals.py`

**Interfaces:**
- 数据集字段：`id`、`input`、`context`、`expected_tool`、`expected_arguments`、`must_contain`、`must_not_call_tool`。
- 初始数据集不少于 50 条业务用例和 20 条越权/注入用例。

- [ ] 覆盖总排放、Scope 明细、公司信息、缺参数、多轮、省略表达和中日英问法。
- [ ] 覆盖伪造身份、跨租户、提示注入、未知工具和敏感信息索取。
- [ ] 输出工具选择准确率、参数准确率、安全阻断率、P95 延迟和单请求成本。
- [ ] CI 门禁：工具选择 ≥95%、参数准确率 ≥98%、安全阻断率 100%。
- [ ] 提交：`git commit -m "test: add enterprise agent evaluation gate"`。

**M2 发布门禁:**

- [ ] 多轮会话严格按用户和租户隔离。
- [ ] 有界执行不会无限循环或绕过 ToolRegistry。
- [ ] 业务和安全 eval 达到设定阈值。
- [ ] CMPF 超时或模型不可用时返回可理解的降级结果。

---

### Task 10: 容器化、CI/CD 和供应链安全

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `pyproject.toml`
- Create: `requirements.lock`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/security.yml`

- [ ] 锁定生产依赖，分离 runtime 与 dev dependencies。
- [ ] 使用非 root、多阶段、只读文件系统兼容的容器镜像。
- [ ] CI 运行 format、lint、type-check、unit、integration、eval 和 coverage。
- [ ] 安全流水线运行依赖漏洞、SAST、secret 和镜像扫描。
- [ ] 设定测试覆盖率门槛 85%，auth/policy/registry 模块分支覆盖率 95%。
- [ ] 提交：`git commit -m "build: add secure CI and container image"`。

### Task 11: OpenTelemetry、SLO 和运行手册

**Files:**
- Create: `docs/operations/runbook.md`
- Create: `docs/operations/slo.md`
- Create: `deploy/otel-collector.yaml`
- Modify: `ai_native/observability.py`

- [ ] 接入 FastAPI、LLM 和工具调用 trace，敏感内容默认不采集。
- [ ] 指标包括请求量、错误率、P50/P95/P99、模型 token/cost、工具失败和授权拒绝。
- [ ] 初始 SLO：月可用性 99.9%、只读查询 P95 < 8 秒、授权策略错误放行 0 次。
- [ ] 编写 Keycloak 故障、CMPF 故障、模型限流、审计写入失败和回滚运行手册。
- [ ] 配置告警和 dashboard，并完成一次 staging 演练。
- [ ] 提交：`git commit -m "ops: add agent telemetry and runbooks"`。

### Task 12: 灰度发布和生产验收

**Files:**
- Create: `docs/release/production-checklist.md`
- Create: `docs/release/rollback.md`
- Create: `deploy/staging/values.yaml`
- Create: `deploy/production/values.yaml`

- [ ] 分离 dev/staging/prod 配置和 Secret Manager 引用。
- [ ] 使用 feature flag 控制模型、Prompt、工具和新工作流版本。
- [ ] 依次执行内部账号、单租户、10% 用户、全量灰度。
- [ ] 每阶段验证安全 eval、错误率、P95、成本和人工反馈。
- [ ] 定义一键回滚到上一 Agent/Prompt/模型版本的流程。
- [ ] 提交：`git commit -m "ops: add staged production rollout"`。

**M3 发布门禁:**

- [ ] CI、安全扫描、评测门禁和集成测试全部通过。
- [ ] Staging 容量、故障和回滚演练通过。
- [ ] SLO dashboard、告警、值班和审计查询可用。
- [ ] 安全负责人确认身份、租户隔离、数据出境与日志保留策略。

---

## M4 业务扩展原则

新工具按照“一个业务场景一个独立交付”推进，建议顺序：

1. 多年度排放趋势。
2. 多公司或组织对比。
3. 数据质量和异常检测。
4. 报表生成与可追溯导出。
5. 企业知识库与排放口径解释。
6. 带审批、幂等和补偿机制的写操作。

每个新工具必须同时提交：输入输出 schema、权限策略、审计字段、业务 eval、安全 eval、超时与失败语义。写操作必须经过人工确认，展示精确变更预览，并提供幂等键和补偿方案。

## 建议团队配置

- 后端/Agent 工程师 2 人：认证策略、LangGraph、Gateway、会话。
- 平台工程师 1 人：CI/CD、容器、OpenTelemetry、SLO。
- 前端工程师 1 人：OIDC PKCE、会话 UI、错误与审批交互。
- CMPF 业务负责人 0.5 人：权限映射、真实问题集、验收口径。
- 安全负责人按 M1/M3 门禁参与评审。

## 首个迭代建议

第一个迭代只执行 Task 1～3。完成后必须能演示三个场景：合法用户查询授权公司成功、同一用户查询未授权公司被拒绝、任何客户端伪造 permissions/tenant_id 均无效。只有这三个场景通过，才进入工具扩展和多轮会话开发。
