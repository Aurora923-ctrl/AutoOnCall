# AutoOnCall 项目分析书

生成日期：2026-06-30  
项目路径：`/Users/adolph/Desktop/AutoOnCall-main`

## 1. 项目定位

AutoOnCall 是一个面向 OnCall 故障诊断场景的 RAG + AIOps Agent 原型系统。它不是普通聊天机器人，也不是单纯的 RAG 文档问答，而是把告警接入、结构化 Incident、Runbook 检索、多源工具取证、证据分析、风险审批、安全变更、Trace 和报告生成串成一条可解释的诊断闭环。

当前项目更适合作为“大模型应用工程 / AI Agent 工程 / 后端工程 / AIOps 平台方向”的校招主项目。它的核心价值不是“模型会回答”，而是把大模型放进一个有业务对象、有工具契约、有安全边界、有评测验证的工程系统里。

一句话概括：

> AutoOnCall 是面向企业 OnCall 场景的可解释故障诊断 Agent，通过 Alertmanager 告警接入、RAG Runbook、Plan-Execute-Replan、Tool Registry、多源证据、风险审批和安全变更 dry-run，辅助 SRE 完成告警分析、根因定位、处置建议和复盘沉淀。

## 2. 当前能力总览

| 能力域 | 当前实现 | 项目价值 |
| --- | --- | --- |
| 告警接入 | `POST /api/alerts/alertmanager`、`AlertEvent`、fingerprint 去重、IncidentState 更新 | 让系统从手动诊断工具升级为事件驱动入口 |
| RAG 知识库 | Markdown / 文本上传、切分、DashScope Embedding、Milvus、本地词法索引、hybrid search、rerank、引用和拒答 | 让 Runbook 问答可追溯、可拒答、可评测 |
| AIOps Agent | LangGraph `planner -> executor -> replanner` | 将故障诊断拆成可观察、可控制的多步流程 |
| 工具治理 | Tool Registry、ToolContract、ToolExecutionResult | 统一工具名、输入输出、风险等级、数据源和失败结构 |
| 多源证据 | Alertmanager、Prometheus、Loki/日志网关、Trace、K8s、Redis、MySQL、Redpanda、CMDB、发布历史、Ticket、Runbook | 覆盖排障常见证据面 |
| 证据分析 | Evidence、RootCauseHypothesis、缺失证据、冲突、置信度 | 避免完全依赖模型自由推理 |
| 风险控制 | allow / approval_required / forbidden | 防止 Agent 自动执行危险动作 |
| 审批闭环 | ApprovalRequest、approve/reject、Trace、报告状态同步 | 高风险动作进入人工确认 |
| 安全变更 | pre-check、dry-run、sandbox、manual_record、observation、rollback recommendation | 审批后仍不直接改生产 |
| 运行态存储 | SQLite / MySQL 保存 alert、trace、approval、report、session、incident state、change execution | 支撑事件工作台和跨接口查询 |
| 前端工作台 | RAG、告警、诊断、Incident、Trace、报告、审批、变更、评测、健康状态 | 能完整演示主链路 |
| 离线评测 | AIOps、RAG、安全变更和适配器验收脚本 | 让项目质量可复现，而不是只靠演示 |

## 3. 主业务链路

```text
Alertmanager / 人工 Incident / Demo Incident
  -> AlertEvent 或 Incident
  -> IncidentState
  -> AIOpsService
  -> Planner 生成 PlanStep
  -> Executor 调用 Tool Registry
  -> ToolExecutionResult
  -> Evidence + ToolCallRecord
  -> Evidence Analyzer
  -> Replanner 决策
  -> Approval / Report / Escalation
  -> Safe Change pre-check / dry-run / manual_record
  -> Trace + Report + Incident 工作台展示
```

这条链路说明项目已经从“问答 Demo”进入“业务流程原型”。用户能看到 Agent 为什么这么排查、用了哪些工具、证据从哪里来、哪些动作被审批或禁止、最终报告如何形成。

## 4. RAG 技术分析

AutoOnCall 的 RAG 模块围绕“可信知识来源”设计，而不是简单把文档塞进向量库。

### 4.1 知识进入系统

```text
POST /api/upload 或 /api/index_directory
  -> 文件名、扩展名、大小和目录白名单校验
  -> Markdown 结构化切分
  -> chunk 元数据：source_file、chunk_id、标题、版本哈希
  -> DashScope Embedding
  -> Milvus 向量索引
  -> 本地 BM25-like 词法索引
```

关键设计：

- 上传入口限制 `txt`、`md`、`markdown`，避免二进制或脚本进入知识库。
- 目录索引只允许 `INDEX_ALLOWED_ROOTS` 中的安全目录，避免任意读本地文件。
- 每个 chunk 带来源、编号、标题和版本信息，方便引用和重建。
- 本地词法索引用于补足向量召回对错误码、告警名、英文缩写的盲区。

### 4.2 问答检索链路

```text
用户问题
  -> 向量召回
  -> 词法召回
  -> 候选合并
  -> rerank
  -> L2 距离阈值过滤
  -> 无可信来源拒答 / 有可信来源回答
  -> citations 兜底
```

亮点：

- top k 不等于可信，项目用 `RAG_MAX_L2_DISTANCE` 控制可信边界。
- 没有可信片段时返回 no-answer，不让模型凭通用知识硬答。
- 有可信片段时才调用模型，并要求回答基于检索上下文。
- 服务层会补齐 `source_file` 和 `chunk_id`，避免模型漏写引用。

### 4.3 RAG 边界

- 当前知识类型以 Markdown / 文本为主，不是完整企业知识治理平台。
- 本地词法索引适合校招项目和演示，不等同于 Elasticsearch/OpenSearch。
- RAG 提供知识依据，不等于实时线上事实；当前故障事实仍应以工具证据为准。
- 离线 RAG eval 证明的是当前 case 的召回和拒答逻辑，不代表线上准确率。

## 5. AIOps Agent 架构分析

### 5.1 Planner

Planner 负责把 Incident 转成结构化 `PlanStep`。每个步骤包含工具名、输入参数、期望证据和风险等级。LLM 失败时，系统会使用规则 fallback plan，保证主链路可运行。

价值：

- 不让模型直接生成最终答案。
- 把“排查意图”转成 Executor 可消费的结构。
- 后续可以通过工具契约和评测检查计划是否合理。

### 5.2 Executor

Executor 在执行每个步骤前先经过 Risk Controller，再通过 Tool Registry 调用工具。工具返回统一的 `ToolExecutionResult`，再转成 `Evidence` 和 `ToolCallRecord`。

价值：

- 工具调用可审计。
- 失败工具也会进入证据链，而不是被隐藏。
- 数据源会被标记为真实适配器、mock、not_configured 或 failed。

### 5.3 Evidence Analyzer

Evidence Analyzer 根据指标、日志、Redis、MySQL、K8s、Runbook、历史工单等证据形成根因假设、缺失证据、冲突和置信度。

价值：

- 根因判断不是完全由模型自由发挥。
- 报告能区分事实、推断、不确定性和证据缺口。
- Replanner 可以基于缺失证据继续补查。

### 5.4 Replanner

Replanner 根据当前状态决定继续执行、补充步骤、重试失败工具、请求审批、生成报告或升级人工。

价值：

- 诊断流程不是死板执行完整计划。
- 证据不足时可补查。
- 工具失败时可降级生成不完整报告或升级人工。

## 6. 安全与生产边界

AutoOnCall 当前最值得强调的安全边界是：**系统不会自动执行生产写操作**。

| 动作类型 | 当前策略 |
| --- | --- |
| 查询指标、日志、Runbook、状态 | 自动执行 |
| 重启、扩容、回滚、配置变更 | 进入审批 |
| 删除 Pod、危险 shell、未审核 SQL | 直接禁止 |
| 审批通过后的变更 | pre-check、dry-run、sandbox 或人工记录 |

其他边界：

- 本地演示可能使用 mock/fallback，生产或严格验收应设置 `AIOPS_MOCK_FALLBACK_ENABLED=false`。
- 默认 `AIOPS_STORE_RAW_EXTERNAL_PAYLOAD=false`，外部响应和 webhook payload 只保存精简内容。
- API token RBAC 适合内网演示，正式生产建议接 SSO/OIDC。
- SQLite 适合本地或单副本演示，多副本生产应切 MySQL。
- full-stack sandbox 是本地证明环境，不是生产环境。

## 7. 校招评分

当前综合评分：

> **87 / 100，强项目，可以作为校招主项目。**

| 维度 | 分值 | 得分 | 评价 |
| --- | ---: | ---: | --- |
| 业务场景与产品闭环 | 15 | 13.5 | 告警、Incident、诊断、审批、报告和安全变更闭环已具备 |
| 大模型应用能力 | 15 | 13 | 有可信 RAG、引用、拒答、Planner/Replanner 和工具调用 |
| Agent 架构与工具编排 | 15 | 13 | Plan-Execute-Replan、Tool Registry、风险控制清晰 |
| 后端工程与系统设计 | 15 | 13 | FastAPI、SSE、Pydantic、SQLite/MySQL、模块边界和测试较完整 |
| 企业级安全与可控性 | 10 | 8 | 有 RBAC token、审批、forbidden、dry-run；生产仍需 SSO/OIDC |
| 可观测性与可解释性 | 10 | 9 | Trace、ToolCall、Evidence、Report、IncidentState 完整 |
| 测试评测与质量保障 | 10 | 8.5 | pytest、API contract、AIOps/RAG/变更 eval 覆盖核心链路 |
| 前端演示与交互体验 | 5 | 4 | 工作台能展示主链路，但不是前端技术主导项目 |
| 文档与面试表达 | 5 | 5 | 主 README、项目分析书、面试讲解文档已收敛 |

岗位适配：

| 岗位方向 | 适配度 | 说明 |
| --- | --- | --- |
| 大模型应用开发 | 很高 | RAG、Agent、工具调用、拒答、引用、评测都能讲 |
| AI Agent 工程 | 很高 | Plan-Execute-Replan、工具治理、证据分析和风险控制是核心 |
| 后端开发 | 高 | API、SSE、模型、存储、鉴权、测试都有 |
| AIOps / SRE 平台 | 高 | 场景对口，告警入口和多源适配器具备雏形 |
| 平台工程 / DevOps | 中高 | 有部署、沙箱、健康检查、安全变更和适配器验收 |
| 算法 / 模型训练 | 中低 | 项目重点是应用工程，不是训练或微调 |

## 8. 当前最值得保留的亮点

1. 从 Alertmanager webhook 开始，而不是只靠手动输入。
2. RAG 不只召回，还做可信阈值、拒答和引用兜底。
3. Agent 不是一次性回答，而是计划、执行、证据分析、重规划。
4. 工具调用结果统一归一，报告和前端复用同一套证据结构。
5. 风险动作不会自动执行，审批后也只 dry-run 或人工记录。
6. Trace、审批、报告、会话、事件和变更都有持久化闭环。
7. 有离线评测和文档契约测试，能证明核心链路不容易漂移。

## 9. 当前不建议继续做的事

当前项目已经够校招展示，不建议为了“看起来更大”继续堆功能。

不建议：

- 继续扩展泛聊天能力。
- 宣传自动生产修复。
- 增加复杂多 Agent 名词。
- 接入大量无关工具。
- 把 mock 数据当真实生产能力展示。
- 把离线评测通过率宣传成线上准确率。

更值得做的是：

- 保持 README、项目分析书、面试讲解文档和代码一致。
- 准备一条稳定演示路径。
- 明确说明 mock、权限、存储、评测、生产写操作边界。

## 10. 生产化差距

如果把 AutoOnCall 按真实企业生产级 AIOps 平台衡量，还需要补：

- SSO/OIDC 和服务级权限。
- 后台任务队列，避免长诊断依赖单个 HTTP SSE 连接。
- 数据库 migration 和多副本部署治理。
- 更强的审计防篡改和数据脱敏。
- 组织级审批策略，例如自审批限制、双人审批和冻结窗口。
- 线上反馈闭环，将人工确认结果沉淀为评测用例或 Runbook 改进。
- 更大规模真实样本、SLO、服务图谱和影响面量化。

这些是生产化增强，不影响当前项目作为校招主项目成立。

## 11. 面试结论

最稳妥的项目表述：

> AutoOnCall 当前是一个 AIOps Agent 原型和工程化验证项目。它已经完成从告警接入、RAG Runbook、工具取证、证据分析、风险审批到报告和安全变更 dry-run 的核心闭环。本地可以走 mock/fallback 或 full-stack sandbox，严格环境可以关闭 mock，让未配置工具返回结构化失败。系统不会自动执行生产写操作，离线评测只代表当前 case 的回归结果。

推荐演示入口：

- 仓库入口：`README.md` 的“面试演示路径（10 分钟）”。
- 讲解手册：`文档/AutoOnCall项目从零上手与面试讲解.md` 的“推荐演示路径”。
- 固定 demo case：`redis_maxclients`、`mysql_slow_query`、`pod_crashloop`，安全边界可补充 `forbidden_sql`。
- 演示观察点：RAG 引用/拒答、Plan、ToolCall、Evidence、Trace、Report、Approval/Forbidden、离线评测。
- 边界说法：本地 demo 可使用 mock/fallback 或 full-stack sandbox；不要把离线评测通过率说成线上准确率。

最终判断：

> **不新增功能的情况下，当前 AutoOnCall 已经够校招主项目。接下来最重要的是文档收敛、演示稳定和边界表达。**
