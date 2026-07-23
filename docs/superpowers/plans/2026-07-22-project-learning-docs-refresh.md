# AutoOnCall 项目学习文档全量更新实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` with the user-approved parallel exception: independent single-file tasks may run three at a time.

**Goal:** 以 2026-07-22 完整工作区为事实源，全量审计并按影响深度重写 `docs/project/` 的课程文档，使其形成从零学习到秋招深挖的递进技术故事。

**Architecture:** 每篇文档是独立编辑单元，由一个新 agent 负责；专题先完成，目录计划与阅读导航最后收口。Agent 共享当前 dirty workspace 以读取未提交实现，但只能修改分配文件。主 agent 负责批间核验、跨篇一致性和最终总审。

**Tech Stack:** Markdown、Python 3.11、FastAPI、Pydantic、LangChain/LangGraph、Milvus、SQLite/MySQL、pytest，以及仓库内 RAG/AIOps/评测脚本。

## Global Constraints

- 事实基线是 2026-07-22 当前完整工作区，包括已提交与未提交实现。
- 不得覆盖、回退或顺手修改用户现有代码、知识资产、脚本、测试和 UI 改动。
- 每个实现 agent 只能编辑任务指定的一个 Markdown 文件。
- 全量事实审计、按影响深度重写；保留准确且教学效果好的段落。
- 各篇结构允许不同，不套统一标题模板。
- 正文是课程技术长文：从问题、失败、概念、设计、工程取舍和边界展开，不写成文件、类、函数说明书。
- Redis 02:07 Incident 是贯穿案例，但每篇只推进本章负责的认知状态，避免重复完整故事。
- 每篇承接前文并为后文留下自然问题，保证全系列层层递进。
- 代码路径仅作为事实证据和章末核验入口。
- 区分 implementation、automated test、offline_fixture、local_live、controlled_fault、production；声明不得越过证据。
- 无法从当前代码、测试或可复现 Artifact 核验的旧数字必须降级表述或删除。
- 审计日期统一更新为 2026-07-22，事实范围必须说明包含当前工作区但不代表生产证据。
- 不创建 Git commit，不修改图示资产，除非最终审计证明现有图示与课程正文存在关键事实冲突。

---

### Task 1: 全系列事实口径与生产边界

**File:** docs/project/topics 下的 00《全系列事实口径与生产边界》

**Fact domains:** `app/main.py`、`app/config.py`、`app/core/auth.py`、健康检查、存储策略、当前测试与工作区状态。

**Deliverable:** 更新全系列共同证据口径，纳入当前运行后端身份、RAG 修复重试成本、能力级 readiness 等新事实，作为后续全部章节的约束。

### Task 2: 从人工值班到受控诊断闭环

**File:** docs/project/topics 下的 01《从人工值班到受控诊断闭环》

**Fact domains:** 主路由、Incident、RAG、Plan—Execute—Replan、报告、审批、Replay 的端到端编排与测试。

**Deliverable:** 用 Redis Incident 推导完整架构生长线，并把新版 Evidence Plan、回答覆盖和运行状态嵌入总览，而不抢后文细节。

### Task 3: 告警如何变成故障事件

**File:** docs/project/topics 下的 02《告警如何变成故障事件》

**Fact domains:** alert ingestion、incident lifecycle/state builder、alerts/incidents API、幂等与持久化测试。

**Deliverable:** 核对告警归一化、Incident 身份、生命周期和读模型投影，明确自动诊断、并发和生产事件关联边界。

### Task 4: 运维知识如何可靠入库

**File:** docs/project/topics 下的 03《运维知识如何可靠入库》

**Fact domains:** 文档 loaders、splitter、indexing quality、vector/lexical index、重建与知识升级脚本、RAG 资产变更。

**Deliverable:** 深度更新多格式解析、语义切分、知识质量评分、双索引身份、stale/migration 和新版高价值知识升级链路。

### Task 5: 可信混合检索

**File:** docs/project/topics 下的 04《可信混合检索》

**Fact domains:** `app/services/rag_retrieval/`、question/evidence plan、Milvus 定向探测、lexical fallback、coverage selection、Trust Gate 及测试。

**Deliverable:** 深度重构检索故事，准确解释运行时后端、召回与融合、来源/标题覆盖、Evidence Plan、降级和可观测字段。

### Task 6: 可审计的检索增强回答

**File:** docs/project/topics 下的 05《可审计的检索增强回答》

**Fact domains:** RAG agent、generation context/guard、answer policy/coverage/evidence plan、chat API 与引用测试。

**Deliverable:** 深度重构从可信 chunk 到可信回答的链路，纳入必要来源修复、子目标修复、引用重试、抽取式兜底、成本合并和拒答边界。

### Task 7: 规划执行再规划诊断循环

**File:** docs/project/topics 下的 06《规划执行再规划诊断循环》

**Fact domains:** planner、executor、replanner、fallback、resume、state 与相关测试。

**Deliverable:** 核对状态协议、继续/停止条件、审批暂停、fallback 与恢复语义，保持与 Evidence/Hypothesis 后续章节的职责分界。

### Task 8: 智能体工具工程

**File:** docs/project/topics 下的 07《智能体工具工程》

**Fact domains:** tool base/registry、各工具、MCP client/server、integrations、预算/Artifact/日志安全测试。

**Deliverable:** 更新工具 Contract、Registry、适配器、只读与写入边界、预算、超时、错误和 Artifact 工程故事。

### Task 9: 从工具结果到根因假设

**File:** docs/project/topics 下的 08《从工具结果到根因假设》

**Fact domains:** evidence analyzer/recommendations、evidence graph/quality、hypothesis 模型、replanner decision 与测试。

**Deliverable:** 核对 ToolResult→Evidence→Hypothesis 的语义转换、冲突处理、置信与续查逻辑，避免把排名写成确认。

### Task 10: 从根因假设到可审计报告

**File:** docs/project/topics 下的 09《从根因假设到可审计报告》

**Fact domains:** report builder/generator/lifecycle/quality/markdown、evidence graph、读模型及测试。

**Deliverable:** 更新充分性、结论—证据对齐、报告生命周期、降级报告和安全披露边界。

### Task 11: 身份工具策略与人工审批

**File:** docs/project/topics 下的 10《身份工具策略与人工审批》

**Fact domains:** auth/RBAC、ownership、risk controller、approval policy/service/workflow/API、审批测试。

**Deliverable:** 核对认证、授权、审计、工具策略、职责分离、审批恢复与 fail-closed 行为，修正陈旧角色或状态描述。

### Task 12: 安全变更的真实执行边界

**File:** docs/project/topics 下的 11《安全变更的真实执行边界》

**Fact domains:** change plan/builder、execution checks/service/projections/read models、SQL safety、外部写适配器与测试。

**Deliverable:** 更新批准后状态机、幂等、dry-run/校验/回滚、执行回执与真实外部写入边界。

### Task 13: 从运行事件到故障重建

**File:** docs/project/topics 下的 12《从运行事件到故障重建》

**Fact domains:** SSE、trace、snapshot、store、read models、resume、replay builders/metrics/evaluation 与测试。

**Deliverable:** 核对事件、最新状态、持久化、恢复和 Replay 的不同语义，纳入拆分后的 read model 架构。

### Task 14: 检索增强与智能体分层评测

**File:** docs/project/topics 下的 13《检索增强与智能体分层评测》

**Fact domains:** `scripts/eval/`、eval cases、RAG scorecard、RAGAS、RCA/replanner/safety/performance 评测和当前测试。

**Deliverable:** 深度更新 Evidence Plan/coverage/citation repair 的评测维度、Milvus 运行后端事实、知识价值评分及 candidate/official/stale 口径。

### Task 15: 智能体协作业务门面

**File:** docs/project/topics 下的 14《智能体协作业务门面》

**Fact domains:** A2A API/models/facade/messages/payloads/skills、SSE 映射与测试。

**Deliverable:** 核对任务、消息、Artifact、状态映射、scope 和协议兼容边界，不夸大官方互操作认证。

### Task 16: 项目量化指标与校招验收

**File:** docs/project/topics 下的 15《项目量化指标与校招验收》

**Fact domains:** 全部 eval/benchmark/acceptance 脚本、测试规模、当前 Artifact、项目规模与未提交 RAG 质量改进。

**Deliverable:** 复核所有数字、样本和日期；无法复跑的降级；把新版能力转化为准确的简历表述、30 秒/3 分钟回答和追问证据。

### Task 17: 专题目录计划收口

**File:** docs/project 下的《AutoOnCall 专题文档目录计划》

**Fact domains:** 更新后的 00—15 专题、设计规格和课程整体顺序。

**Deliverable:** 让目录、专题职责、阅读依赖、贯穿案例、事实规范和 Definition of Done 与最终专题一致；明确篇章结构不统一但叙事原则统一。

### Task 18: 专题阅读导航收口

**File:** docs/project/topics 下的《专题阅读导航》

**Fact domains:** 更新后的全部专题、岗位路线、时间路线和跨篇亮点。

**Deliverable:** 提供从零阅读、岗位速读和面试复习三种入口，准确描述每篇学习产出和新版项目亮点。

## Execution Batches

- Batch A: Tasks 1—3
- Batch B: Tasks 4—6
- Batch C: Tasks 7—9
- Batch D: Tasks 10—12
- Batch E: Tasks 13—15
- Batch F: Task 16，然后 Task 17—18（目录与导航必须读取已更新专题）

## Per-Task Verification

每个 agent 完成后必须报告读取的事实源、主要改动、保留内容、能力边界和自检结果。主 agent 在进入下一批前检查：仅目标文件被修改；审计日期正确；关键新事实有代码/测试来源；没有明显旧路径、旧日期或越界声明。

## Final Verification

1. 检查 18 个目标文件均有当前任务产生的差异；
2. 扫描旧审计日期、TBD/TODO、失效相对链接和不存在的代码路径；
3. 统计并比对跨篇核心术语、状态和 Redis 主线；
4. 审查近期 RAG 能力是否在 03/04/05/13/15、目录和导航闭环出现；
5. 运行文档引用测试、仓库引用测试及与事实变更相关的定向 pytest；
6. 由独立 reviewer 做全系列事实、教学性、重复度和面试价值总审；
7. 修复 Critical/Important 问题后重新验证。
