# CMPF 受控自主 Gateway Agent Loop 设计

**日期：** 2026-07-20  
**状态：** 已完成设计确认，等待实施计划  
**项目：** `AI_Native`  
**边界：** 不修改 CMPF Java、前端、数据库或 Keycloak 现有业务配置

## 1. 目标

将当前一次 LLM 选工具、服务内固定准备步骤、最多一次业务分析调用的 Demo，升级为接近企业生产实践的 Gateway Agent Demo。

新版使用 LangGraph 实现受控自主 ReAct Loop。LLM 可以根据安全工具观察继续选择下一步，但每个动作必须经过 Gateway 的工具白名单、身份与会社范围、参数、重复调用、执行预算和风险策略检查。系统支持 PostgreSQL checkpoint、多轮澄清与恢复、结构化 JSON 日志，以及可选 OpenTelemetry/Jaeger。

该版本不是专门的教学界面。产品页面继续提供正常的聊天、澄清、文本与图表体验；开发者通过清晰的代码分层、架构文档、测试、日志和标准 Trace 理解 Gateway 技术栈。

## 2. 非目标

- 不修改 CMPF 代码或权限模型。
- 不实现写入、删除、审批、批量导出或任意 SQL/API 调用。
- 不实现多 Agent Supervisor、动态 MCP 工具安装或模型生成代码。
- 不把 Bearer Token、完整 CMPF 原始响应、完整排放明细或模型隐藏推理写入 checkpoint、日志、Span 或发送给 LLM。
- 不建设管理员后台或专门的 Agent Trace 教学页面。
- 不把 Jaeger 作为 Gateway 可用性的强依赖。

## 3. 总体架构

系统分成七个明确层次：

1. **API Layer**：FastAPI、Keycloak JWT、限流、会话、SSE、run 状态和取消。
2. **Agent Runtime**：LangGraph、状态、结构化动作、interrupt/resume 和终止路由。
3. **Control Plane**：工具目录、策略引擎、参数验证、会社范围、风险和预算。
4. **Data Plane**：受控执行器、CMPF Client、DTO 映射和确定性 ChartSpec。
5. **Repository**：conversation、message、run、checkpoint 和 audit。
6. **Observability**：结构化日志、OpenTelemetry instrumentation 和敏感字段清理。
7. **Model Provider**：OpenAI-compatible LLM，只负责规划、参数提取和普通对话。

真实会话 API 统一进入新的 LangGraph Runtime。当前 `EnterpriseAgentService` 中混合的规划、准备调用、业务执行和回答生成被拆分；旧 `agent/graph.py` 的一次性示例不再作为第二套实现保留。

## 4. Agent 状态机

LangGraph 节点固定为：

- `planner`：基于用户目标、可信上下文和安全 observations 生成一个 `AgentAction`。
- `policy`：重新验证 action，不允许 planner 绕过。
- `executor`：一次只执行一个经过批准的工具动作。
- `observer`：将工具结果转换成 LLM 可见的最小安全摘要，并保存确定性 artifact。
- `clarifier`：缺少信息或存在歧义时生成用户问题并 interrupt。
- `responder`：使用模板和 artifact 生成最终文本、ChartSpec 与完成事件。
- `terminal_error`：输出稳定、安全、可审计的终止结果。

主循环为：

```text
planner → policy → executor → observer ─┐
   ↑                                   │
   └──────────────── replan ───────────┘

planner/policy → clarifier → checkpoint/interrupt
planner/observer → responder → end
policy/executor/budget → terminal_error → end
```

## 5. 结构化动作

LLM 每轮只能返回一个经过 Pydantic 验证的 `AgentAction`：

- `call_tool`：工具名、参数和与用户目标相关的简短理由。
- `clarify`：问题、缺少字段和可选安全候选。
- `finish`：引用已有 artifact 的完成指令，不携带任意业务数值。

模型不能直接发 HTTP、读取 Token、修改状态计数、生成任意 endpoint、调用目录外工具或生成任意 ECharts Option。无法解析、字段超量或 action 类型非法时视为 `model_invalid_action`，不会尝试宽松执行。

## 6. Tool Catalog 与执行边界

现有十三个只读业务能力继续保留，但从大型条件分支重构为独立工具描述：

- 名称和用途。
- Pydantic 参数模型。
- 风险等级，首版全部为 `read_only`。
- 所需权限和会社范围规则。
- CMPF endpoint 元数据。
- 超时和最大结果数。
- 执行函数。
- Observer 摘要器。
- 确定性 artifact/ChartSpec 生成器。

据点类请求允许真实的多步循环。例如：

1. Planner 选择据点解析工具。
2. Policy 验证目标会社和参数。
3. Executor 调用 CMPF 据点接口。
4. Observer 只暴露已授权的 `baseId`、规范名称和匹配状态。
5. Planner 使用该 observation 选择月度分析工具。
6. Executor 调用 CMPF 排放分析接口。
7. Observer 保存 ChartSpec，只告诉模型 artifact 已生成及数据点数。
8. Planner 返回 `finish`，Responder 确定性输出答案和图表。

公司名称、决算起始月等确定性依赖可以由 Tool Executor 内部的受审计 dependency resolver 获取并缓存；这类内部依赖不允许跳过 Policy，也计入 CMPF 调用统计，但不要求 LLM 机械地规划每个基础查询。

## 7. Policy Engine

每次 `call_tool` 必须依次检查：

1. action schema 与工具白名单。
2. 当前 Principal 与最新 Bearer Token 是否有效。
3. 自社或直接子会社范围。
4. 工具权限与只读风险等级。
5. 参数类型、期间、Scope、据点数量和结果上限。
6. 相同 `tool + canonical arguments` 是否已执行。
7. Planner、Tool、澄清、请求时长预算是否剩余。
8. conversation 是否已有另一个 active run 或已取消。

Policy 输出 `approved`、`clarification_required` 或 `denied`。Executor 只接受带不可伪造内部批准标记的 action。

## 8. AgentState 与 checkpoint

Checkpoint 保存：

- `run_id`、conversation ID、可信 user/company 标识。
- 用户目标、locale 和经过验证的页面上下文。
- 当前 action、approved action 和安全 observations。
- ChartSpec、回答片段等 artifact 引用。
- Planner、Tool 和澄清计数。
- 已执行调用的 canonical signature。
- pending question、missing fields、stop reason 和稳定错误代码。

Checkpoint 不保存 Bearer Token、完整 CMPF 原始响应、完整模型原始响应或隐藏推理。Token 只存在于单次 HTTP 请求的运行上下文中。

恢复时，Gateway 使用新请求的 Token 重新验证 issuer、audience、有效期、用户和会社范围。恢复请求必须属于 checkpoint 的同一用户；不允许通过 conversation ID 接管其他用户的 run。

## 9. 执行预算与停止条件

默认限制：

- 最多 8 次 Planner 循环。
- 最多 6 次显式 Tool 调用。
- 最多 2 次澄清 interrupt。
- 每个 HTTP 请求段总超时 45 秒。
- 每轮只能执行一个批准动作。
- 同一 `tool + canonical arguments` 在同一 run 中不得重复。
- 同一 conversation 同时只能有一个 active run。
- 保留现有每用户请求和并发限制。

Interrupt 等待用户的时间不计入 45 秒，但恢复后继续使用原 run 的剩余 Planner、Tool 和澄清预算。达到任一上限立即以 `exhausted` 结束，不继续询问模型。

## 10. Observation 与数据最小化

Observer 将结果拆成两部分：

- **安全 observation**：模型可见，用于规划下一步。例如匹配到的授权据点 ID/名称、期间验证成功、artifact 类型和数据点数。
- **artifact**：模型不可见，由 Gateway 保存并用于确定性回答。例如排放数字、ChartSpec 和表格数据。

模型不接收 CMPF 原始 DTO 或排放明细。最终数字、差异、百分比、图表和单位由确定性代码生成，避免模型改写或伪造业务数据。

## 11. 多轮澄清与恢复

缺少年度、Scope、据点名称，或据点匹配不唯一时，Graph 进入 `clarifier`。SSE 发送 `clarification`，包含 `run_id`、可读问题、缺少字段和最多 20 个安全候选。

Repository 将 run 标为 `waiting_for_user` 并保存 checkpoint。该 conversation 的下一条消息默认作为 pending run 的补充输入并恢复；如果用户选择开始新任务，前端先调用 cancel，再提交新消息。

进程重启后仍可通过 PostgreSQL checkpointer 恢复。内存 repository 模式只用于单元测试和本地最小 Demo，不承诺跨进程恢复。

## 12. API 与 SSE

保留现有 API：

- `POST /v1/conversations`
- `GET /v1/conversations/{conversationId}/messages`
- `POST /v1/conversations/{conversationId}/messages/stream`
- `DELETE /v1/conversations/{conversationId}`

新增：

- `GET /v1/conversations/{conversationId}/runs/{runId}`
- `POST /v1/conversations/{conversationId}/runs/{runId}/cancel`

SSE 保留 `status`、`answer.delta`、`visualization`、`answer.completed`、`error`，新增 `clarification`。每次 run 的事件包含 `run_id`。公开 `status` 只暴露适合产品 UI 的状态，例如 planning、executing、waiting、completed，不暴露敏感工具参数。

AbortController 只中断客户端读取；服务端取消接口设置持久化 cancel 标志。每个 Graph 节点和 Tool 调用前检查取消状态，避免客户端离开后继续无意义执行。

## 13. 错误模型与降级

稳定错误类型：

- `validation`：缺失或非法参数。
- `policy_denied`：会社、权限、工具或风险拒绝。
- `upstream`：CMPF 401、403、timeout、5xx 或契约错误。
- `model`：Provider 故障、无效 action 或无法规划。
- `cancelled`：用户或服务端取消。
- `exhausted`：步骤、工具、澄清或时长预算耗尽。
- `conflict`：conversation 已有 active run。

前端不接收堆栈、Token、模型原始响应或 CMPF 原始 payload。

LLM 故障时，高置信规则只允许处理现有简单单工具请求。需要 observation/replan 的多步请求返回可重试 `model` 错误，不自动降级成未经模型验证的复杂流程。CMPF 或数据库故障绝不回退到 Mock 数据。

## 14. 结构化日志

默认输出 JSON 日志，字段固定为：

- `timestamp`、`level`、`service`。
- `trace_id`、`span_id`、`run_id`、`conversation_id`。
- `user_id`、`company_id`。
- `graph_node`、`tool_name`、`endpoint`。
- `duration_ms`、`status`、`error_code`、`result_count`。

统一 Log Processor 删除 Authorization、Token、Cookie、prompt 原文、完整用户消息和业务 payload。日志允许记录用户消息长度、参数键名和 canonical signature 哈希。

## 15. OpenTelemetry 与 Jaeger

`OTEL_ENABLED=false` 为默认值，关闭时 Gateway 零额外基础设施启动。开启后：

- 自动 instrument FastAPI 和 httpx。
- 手工创建 planner、policy、executor、observer、checkpoint、SSE span。
- 使用 `run_id` 和 conversation ID 的不可逆/安全标识关联日志。
- Docker Compose 提供 OpenTelemetry exporter 配置和 Jaeger。
- Jaeger 不参与 `/health/ready`；export 失败只记录限频告警，不影响业务请求。

Span 不记录 Token、prompt 正文、CMPF 原始响应或排放数值。

## 16. Repository 与运行状态

除现有 conversation、message、audit 表外，增加 run 状态持久化。LangGraph 使用 PostgreSQL checkpointer；业务 run 表只保存便于 API 查询和取消的索引状态，不复制完整 checkpoint。

Run 状态为：`running`、`waiting_for_user`、`completed`、`failed`、`cancelled`、`exhausted`。状态转换使用事务或乐观锁，防止两个恢复请求同时执行同一个 checkpoint。

会话与消息默认保留 7 天，审计默认 90 天；run/checkpoint 生命周期跟随会话。清理任务先删除过期 checkpoint，再删除 run、message 和 conversation。

## 17. 测试策略

### 17.1 单元测试

- `AgentAction` schema 和非法 action。
- Tool Catalog 元数据和 Pydantic 参数。
- Policy 的权限、会社、预算、重复签名和风险判断。
- Observer 的安全摘要与 artifact 隔离。
- Graph 每个条件分支和终止原因。
- ChartSpec、错误映射和敏感字段清理。

### 17.2 Graph/API 集成测试

- Scripted Planner 精确验证 plan → tool → observe → replan → finish。
- interrupt、PostgreSQL checkpoint 和进程重启后恢复。
- 恢复使用新 Token，checkpoint 中不含 Token。
- SSE 顺序、clarification、断线、取消和重复提交。
- active run 冲突和 optimistic locking。
- OTel 开/关、Span 父子关系和 exporter 故障降级。

### 17.3 真实 CMPF 验收

1. “亲社据点2的 2025 年月度趋势”完成据点解析、replan、分析和图表。
2. 缺少年度时 interrupt，补充“2025”后从同一 run 恢复。
3. 据点名称歧义时返回候选，选择后恢复。
4. 两个据点或两个期间比较至少经历两个 Agent action。
5. 越权会社、重复调用、预算耗尽和主动取消不产生图表。
6. 开启 OTel 后在 Jaeger 看到 API → Graph nodes → LLM → CMPF 的同一 trace。

## 18. 完成标准

- 当前 59 个测试继续通过。
- 新增测试覆盖每个 Graph 节点、路由和停止原因。
- 六个真实验收场景通过。
- 默认无 Jaeger 时可正常启动和执行。
- 日志、checkpoint、数据库和 Span 中未发现 Token 或完整业务 payload。
- CMPF 仓库零修改。
- README 说明如何启动 PostgreSQL/Jaeger、执行真实请求、查看一次 Trace，并解释各模块职责。

## 19. 实施范围控制

本次只重构和增强 `AI_Native`：

- 统一真实入口与 LangGraph。
- 重构现有十三个只读工具为目录化定义。
- 增加 Policy、Budget、Observer、Run、Cancel 和 Checkpoint。
- 增加结构化日志和可选 OTel/Jaeger。
- 对 Demo 做澄清、恢复和服务端取消所需的最小改动。
- 保留现有 ChartSpec 安全限制、Keycloak/CMPF Token 透传和会社范围逻辑。

管理员后台、写操作、复杂报告生成、多 Agent 和 CMPF 正式前端集成留待后续独立阶段。
