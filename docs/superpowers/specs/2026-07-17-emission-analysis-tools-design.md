# CMPF 排出量分析工具扩充设计

## 目标

在独立的 `AI_Native` Enterprise Agent Gateway 中扩充 CMPF 排出量分析能力，覆盖 Scope、拠点和期间比较。CMPF 代码、数据库、权限体系和既有接口保持不变。

用户可以使用日语、中文或英语自然语言请求分析。LLM 只选择工具并提取查询条件；会社和拠点权限校验、CMPF 调用、数据转换、图表生成及审计全部由 Gateway 确定性执行。

## 范围

保留现有工具：

- `get_scope_composition_chart`
- `get_monthly_emission_trend_chart`
- `get_top_emission_activities_chart`
- `get_annual_emission_summary`
- `get_scope_breakdown`
- `get_company_info`

新增以下只读能力：

| Agent 工具 | CMPF API | 输出 |
| --- | --- | --- |
| `list_analysis_bases` | `GET /analysis/baseInfoByCompanyGroup` | 拠点候选列表或文本 |
| `get_base_emission_composition_chart` | `POST /analysis/baseTypeEmission` | 拠点、区域或分类构成饼图 |
| `get_base_monthly_emission_chart` | `POST /analysis/baseTypeEmissionForMonth` | 一个或多个拠点的月度趋势图 |
| `get_base_detail_composition_chart` | `GET /analysis/baseLargeItemEmission` | 指定拠点的大项目构成饼图 |
| `get_base_detail_monthly_chart` | `GET /analysis/baseMonthEmission` | 指定拠点的月度排出量图 |
| `compare_base_emissions_chart` | `POST /analysis/compareByBase` | 最多 5 个拠点的分组柱状图 |
| `compare_emission_periods_chart` | `POST /analysis/compareByDuration` | 两个期间的分组柱状图和差额摘要 |

不开放保存分组、删除分组或其他写接口。不在本阶段加入碳足迹、组织碳强度、能源种类或活动项目分页明细。

## 方案选择

采用“画面能力型工具”，不把每个 CMPF API 直接一对一暴露给 LLM，也不使用一个参数庞大的通用分析工具。

每个 Agent 工具对应一个明确的业务意图。工具内部使用有上限、可审计的 Loop 执行准备查询，例如把会社名称解析为 `companyId`、把拠点名称解析为 `baseId`，但每轮最多执行一个最终业务分析工具。这种设计让 LLM 的可选工具和参数保持清晰，同时让审计记录能直接表达用户执行的分析类型。

## 调用流程

1. LLM 从消息和页面上下文提取工具名、会社、年度、Scope、期间以及拠点名称或 ID。
2. Gateway 启动受控工具 Loop，验证目标会社属于登录用户的自社或直接子会社。
3. 涉及拠点时，Gateway 调用 `GET /analysis/baseInfoByCompanyGroup` 获取该会社的合法拠点集合。
4. Gateway 将名称解析成 `baseId`，并验证明确提供的 `baseId` 也存在于合法集合中。
5. 参数完整且唯一匹配后，Loop 调用一个最终 CMPF 排出量分析 API，然后终止。
6. Gateway 把 CMPF DTO 确定性转换为受限 `ChartSpec` 和日英双语摘要。
7. Gateway 持久化助手消息、图表、Loop 步骤和审计记录，并通过 SSE 返回结果。

Loop 的限制如下：

- 每轮最多 3 个准备步骤和 1 个最终业务分析工具。
- 同一会社的会社信息、决算起始月和拠点列表在单轮上下文中分别最多查询一次。
- 找到唯一合法 `baseId` 后自动继续，不要求用户再次确认。
- 找不到或存在多个同名拠点时立即终止，不调用最终分析 API。
- LLM 不控制循环次数，不获得完整拠点列表或 CMPF 原始响应。
- 每个步骤记录步骤类型、目标会社、结果状态、耗时和安全结果数量，不记录 Token 或完整业务数据。

CMPF 仍执行最终的数据权限判断。Token 不写入数据库、日志、模型请求或 checkpoint。

## 拠点解析

拠点解析作为独立组件实现，输入为目标会社、拠点名称或 ID、locale 和 Token，输出为已验证的拠点对象。

解析规则：

1. 明确提供 `baseId` 时，必须验证它属于目标会社返回的拠点集合。
2. 名称先做去除首尾空白后的精确匹配。
3. 精确匹配失败后，允许不区分拉丁字母大小写的完整匹配。
4. 第一版不使用包含匹配、编辑距离或其他相似度算法自动选择。
5. 无匹配时返回 `base_not_found`，并提供有限候选项。
6. 多个同名结果时返回 `base_ambiguous`，候选项包含 `baseId` 和显示名称。

候选项最多返回 20 条，不把完整原始 DTO 暴露给前端或模型。

## 参数规则

- `company_id`：省略时使用 Token 中的当前会社；始终重新校验会社范围。
- `year`：年度型工具必填，可从页面上下文或自然语言提取；不猜测缺失年度。
- `scope`：Scope 构成工具必填，值仅允许 1、2、3；其他工具按 CMPF 契约决定是否可选。
- `base_name` / `base_id`：拠点详情工具必须提供一个；比较工具接受 2 到 5 个拠点。
- `start_month` / `end_month`：格式固定为 `YYYYMM`；开始月份不得晚于结束月份。
- 期间比较：必须提供两个有效、互不为空的期间；每个期间最多 36 个月。
- `group_by`：拠点构成和趋势只允许 `base`、`area` 或 CMPF 支持的固定分类枚举，不接受任意字段名。

会社决算起始月继续通过 `/user/company/getCompanyStartMonth` 获取，Gateway 根据该值构造年度查询参数。

## CMPF Client 扩充

在现有 `CmpfGateway` 中增加明确命名的方法，而不是暴露任意路径请求：

- `list_analysis_bases`
- `get_base_type_emission`
- `get_base_type_emission_for_month`
- `get_base_large_item_emission`
- `get_base_month_emission`
- `compare_emissions_by_base`
- `compare_emissions_by_duration`

GET 请求使用查询参数，POST 请求使用 JSON body。所有请求复用当前用户 Bearer Token、CMPF `lang` 和 `site-name` header，以及现有超时和上游错误转换机制。

## 图表契约

继续使用受限 `ChartSpec`，新增 `grouped_bar` 图表类型：

- Scope 构成、拠点构成、指定拠点大项目构成：`pie`
- Scope 月度趋势、拠点月度趋势、指定拠点月度趋势：`line`
- 拠点比较、期间比较：`grouped_bar`

安全限制保持不变：

- 最多 5 个 series。
- 全图最多 100 个数据点。
- 只接受有限数值，拒绝 `NaN` 和 `Infinity`。
- 不允许 HTML、JavaScript、formatter 函数、custom series 或动态代码。
- 标题必须包含会社、年度或期间；拠点详情图还必须包含拠点名称。
- 单位保持 CMPF 排出量分析接口的原始单位 `t-CO₂e`。
- 图表数据随助手消息持久化，并支持表格降级展示。

## 回答与模型边界

LLM 可以看到工具名称、参数 schema、用户问题及安全的页面上下文，不可以看到：

- Keycloak Token。
- CMPF 原始业务响应。
- 合法会社或拠点的完整列表。
- 生成后的 ChartSpec 数据。

成功回答由 Gateway 模板生成，包含会社、拠点或分组、年度或期间以及数据点数量。期间比较额外给出两个期间总量和绝对差额；百分比仅在基准期间非零时计算。

## 错误处理

Gateway 对外使用稳定错误码：

- `company_forbidden`
- `year_required`
- `scope_required`
- `base_required`
- `base_not_found`
- `base_ambiguous`
- `too_many_bases`
- `invalid_period`
- `period_too_long`
- `cmpf_unauthorized`
- `cmpf_forbidden`
- `cmpf_timeout`
- `cmpf_business_error`

澄清类错误返回可操作的日语或英语提示，不调用最终分析 API。CMPF 的响应正文、内部 URL、堆栈和 Token 不返回前端。失败请求同样写审计，记录工具、会社、拠点 ID、期间、状态、稳定错误码和耗时，不记录完整业务数据。

## SSE 与持久化

成功事件顺序保持：

1. `status`
2. `visualization`
3. `answer.completed`

需要澄清时不发送 `visualization`，而是发送可读的助手回答后完成。上游或验证失败继续通过统一 `error` 事件返回。

消息持久化保存最终工具名和 `ChartSpec`。审计记录增加可选的 `base_ids`、`period_start`、`period_end` 和 `comparison_period` 字段；不要求修改 CMPF 数据库。

## 测试与验收

### 契约测试

- 七个新增 CMPF Client 方法的 HTTP method、path、query/body 和 Token 透传。
- 决算起始月对应的年度和期间构造。
- CMPF 成功包络、空数据和业务错误包络。

### 拠点解析测试

- ID 验证成功及越界拒绝。
- 精确名称与大小写不敏感完整匹配。
- 无匹配、多匹配及候选数量上限。
- 不进行模糊名称自动选择。

### Tool 与图表测试

- LLM 能用日语、中文和英语选择七个新增工具。
- 各工具缺少必填参数时返回对应澄清错误。
- 饼图、折线图、分组柱状图 DTO 映射。
- 5 series、100 数据点、非法数值和脚本标签限制。
- 期间比较总量、差额及零基准处理。

### 权限与端到端测试

- 自社和直接子会社的拠点查询成功。
- 越权会社和不属于目标会社的拠点在调用分析 API 前被拒绝。
- 使用真实 Keycloak Token 调用本地 CMPF，完成 Scope、拠点和期间比较各一个自然语言场景。
- 会话恢复后文本和图表完整。
- Gateway 或 CMPF 失败不影响 CMPF 原业务页面。

## 验收示例

- “2025 年 Scope 1 每月排出量趋势”生成折线图。
- “东京拠点 2025 年排出量构成”自动解析唯一拠点并生成饼图。
- 同名拠点存在时返回候选，不自动选择。
- “比较东京与大阪拠点 2025 年排出量”生成不超过 5 系列的分组柱状图。
- “比较 2024/04–2025/03 与 2025/04–2026/03”生成期间比较图和差额摘要。
- 指定其他会社的非法 `baseId` 时不产生任何排出量数据或图表。

## 明确假设

- CMPF 是会社关系、拠点、排出量数据和最终权限判断的唯一权威。
- 当前 CMPF API 契约能够支持上述查询，联调发现契约差异时只调整 Gateway 适配层。
- 不修改 CMPF Java、前端或数据库。
- 第一版不自动展开孙会社。
- 第一版每轮只执行一个最终业务分析工具。
