# AutoOnCall 专题文档目录计划

> 状态：课程型长文目录基线，待按篇实现  
> 制定日期：2026-07-13  
> 适用仓库：AutoOnCall  
> 目标读者：第一次接触 RAG、Agent 和 AIOps 的开发者，以及需要据此准备校招简历和面试的项目作者

## 1. 这份计划解决什么问题

本文件只定义后续专题文档的目录、边界、依赖关系和验收标准，不在本阶段展开每篇正文。

专题文档需要同时实现三个目标：

1. 让初学者先理解运维问题，再理解模型、代码和工程取舍。
2. 让每个技术结论都能回到当前仓库的真实实现、测试或评测证据。
3. 让项目作者能够从文档中提炼诚实、可追问、可现场演示的简历内容。

本系列不是产品宣传材料，也不承担以下目标：

- 不把本地测试或离线评测包装成生产结果。
- 不把规则基线描述成通用智能推理。
- 不把兼容接口描述成完整标准实现。
- 不用尚未落地的目标架构解释当前代码。
- 不复制没有日期、命令和 Artifact 来源的指标。

## 2. 最终目录决策

采用“1 份简短阅读基线 + 13 篇核心长文 + 1 篇选修长文”的结构。

- `00` 是全系列的事实口径和生产边界阅读说明，只负责教读者识别证据等级和能力边界，不写成一篇沉重的技术章。
- `01` 至 `13` 构成完整主线，后续必须实现。
- `14` 是 A2A 北向协作选修，不影响核心闭环的完整性。
- 原计划中同时承载证据分析和报告生成的内容拆成 `08`、`09` 两篇。
- 生产化边界不再作为普通选修，而是前置为 `00`。

未来专题文件统一放到当前计划所在 `project/` 目录的 `topics/` 子目录，文件名使用中文：

```text
project/
├── AutoOnCall专题文档目录计划.md
└── topics/
    ├── 专题阅读导航.md
    ├── 00-全系列事实口径与生产边界.md
    ├── 01-从人工值班到受控诊断闭环.md
    ├── 02-告警如何变成故障事件.md
    ├── 03-运维知识如何可靠入库.md
    ├── 04-可信混合检索.md
    ├── 05-可审计的检索增强回答.md
    ├── 06-规划执行再规划诊断循环.md
    ├── 07-智能体工具工程.md
    ├── 08-从工具结果到根因假设.md
    ├── 09-从根因假设到可审计报告.md
    ├── 10-身份工具策略与人工审批.md
    ├── 11-安全变更的真实执行边界.md
    ├── 12-从运行事件到故障重建.md
    ├── 13-检索增强与智能体分层评测.md
    └── 14-智能体协作业务门面.md
```

不创建空的专题占位文件。开始实现某一篇时再创建对应文件，并在 `专题阅读导航.md` 中登记状态。

后续导航必须遵守两条规则：专题目录内部优先使用同目录相对链接；目标文件尚未创建时只能用纯文本登记，不能提前创建失效链接。中文文件名只用于项目叙事文档，运行期资产和知识库资产仍遵守仓库原有稳定路径约束。

## 3. 阅读阶段与依赖关系

| 阶段 | 专题 | 学习目标 | 前置依赖 |
|---|---|---|---|
| 第零阶段：统一口径 | 00 | 先区分实现、验证和生产证据 | 无 |
| 第一阶段：理解系统为何存在 | 01–02 | 从人工 OnCall、告警和 Incident 建模理解业务问题 | 00 |
| 第二阶段：理解可信知识链路 | 03–05 | 理解知识入库、检索和有依据回答 | 01 |
| 第三阶段：理解诊断 Agent | 06–09 | 理解规划、工具、证据、假设和报告 | 01–02，05 |
| 第四阶段：理解治理与运行态 | 10–12 | 理解风险、审批、变更边界、状态和回放 | 06–09 |
| 第五阶段：证明系统没有退化 | 13 | 理解分层评测、证据等级和反馈治理 | 03–12 |
| 选修阶段：北向互操作 | 14 | 理解如何把已有业务能力暴露给其他 Agent | 01，05–06，10，12 |

不建议合并以下专题：

- `03`、`04`、`05` 分别属于离线知识构建、在线检索决策和回答生成，输入输出契约不同。
- `08` 负责从工具结果形成根因假设，`09` 负责把假设变成可审计报告。
- `10` 负责执行前的授权决策，`11` 负责批准后的安全推进，威胁模型不同。
- `12` 负责运行态重建，`13` 负责跨运行质量评价，证据粒度不同。

跨篇重复内容按以下规则归属：

- `00` 只定义证据术语、暴露风险和全局声明边界；`13` 解释评测实现、指标和 Artifact。
- `07` 只解释工具契约、风险元数据和执行 envelope；`10` 解释策略如何判定并执行授权。
- `09` 负责报告生成和质量门；`12` 只解释报告如何进入运行态查询和 Replay 聚合。

## 4. 全系列长文生成方法

### 4.1 文档类型：课程章，而不是代码说明书

每篇长文都要让读者产生“我跟着问题一步步把这个能力推导出来了”的感受。不能一开始就平铺框架、目录、模型和字段，也不能把代码路径数量当作内容深度。

正文首先回答“为什么需要”，然后回答“最小机制是什么”，最后才回答“AutoOnCall 具体怎样实现”。代码地图、测试清单和证据等级放在文章后半段或附录，不能打断概念形成。

### 4.2 三条必须同时推进的主线

每篇文章必须同时推进三条线：

1. **故障故事线**：持续使用 Redis 主案例，并用 MySQL、K8s 或安全负例作对照。
2. **架构生长线**：每篇只引入一个主要新能力，说明没有它时上一阶段为什么无法继续。
3. **面试表达线**：从本章内容提炼概念解释、调用链、设计取舍、失败案例和诚实边界。

读者不应只是知道“系统有哪些模块”，而应能说明“系统为什么必然需要这些模块”。

### 4.3 章节之间必须有明确接力

每篇开头固定写“架构定位”：

- 上一篇已经解决了什么。
- 当前还剩下什么具体问题。
- 本篇只新增哪一个核心能力。
- 后一篇会继续解决什么。

第一篇没有前置技术章节，应从人工 OnCall 的真实工作方式开始；后续文章必须复用已解释过的概念，不重复从零定义整个系统。

### 4.4 单篇长文的标准递进结构

每篇正文按以下教学顺序生成。若某一步不适用，需要明确说明原因。

1. **架构定位**：说明上一章、本章和下一章的关系，以及本章完成后的学习成果。
2. **问题现场**：用一个具体事故、请求或失败现象建立共同背景。
3. **当前最朴素的版本**：先展示没有本章能力时系统能做到什么。
4. **朴素版本为什么失败**：通过真实反例让读者感受到缺口，而不是直接给结论。
5. **引出核心概念**：只解释解决当前缺口所需的新概念，术语随用随讲。
6. **最小心智模型**：使用类比、短伪代码、状态变化或小型数据流解释本质机制。
7. **逐步加入工程约束**：依次讨论停止、失败、降级、并发、安全、可观察性或持久化，不能一次罗列全部企业能力。
8. **落到 AutoOnCall**：说明该概念如何映射到当前项目，但先讲职责，再讲文件名。
9. **贯穿案例完整运行**：把前文步骤串成一条从输入到输出的正常路径。
10. **负例与边界**：展示至少一个失败、拒绝、冲突或转人工案例。
11. **真实代码调用链**：此时再进入 API、服务、模型、工具、存储和关键代码片段。
12. **设计取舍**：比较当前实现与至少一个替代方案，并解释为什么没有选择更复杂方案。
13. **本章里程碑**：明确系统比上一章多了什么能力、仍缺什么、下一章为什么自然出现。
14. **面试转化**：提供 30 秒回答、3 分钟回答、简历表述、常见追问和不可夸大表述。
15. **动手练习**：至少包含一个只读追踪题和一个需要设计权衡的小型改造题。
16. **实现证据附录**：集中列出代码入口、测试、命令、证据等级和“已实现 / 已验证 / 尚未实现”。

### 4.5 信息分配

每篇长文建议按以下比例组织：

- 约 70% 用于问题、演化过程、核心概念、完整案例和工程取舍。
- 约 20% 用于真实代码调用链和关键实现片段。
- 约 10% 用于测试、证据等级、能力边界和索引。

正文前三分之一原则上不密集出现文件路径、字段大全或测试列表。模型字段只在影响当前问题时解释，完整字段表放到代码落地部分。

### 4.6 写作语言与节奏

- 先用日常开发或值班直觉解释，再给正式术语。
- 一个小节只回答一个问题，标题尽量使用“为什么”“如果没有会怎样”“怎样继续”等问题式表达。
- 使用同一案例反复推进，不为每个概念重新发明背景。
- 每完成一个阶段，用一两句话说明“到这里系统已经能做什么，但还不能做什么”。
- 伪代码只表达核心机制，真实代码片段只保留影响设计的部分。
- 不展示或声称获得模型私有思维过程；用结构化计划、动作、观察和决策摘要解释 Agent 行为。
- 不因为参考资料使用 ReAct 就把 AutoOnCall 的 Plan-Execute-Replan 改称 ReAct。

### 4.7 图和表的使用

每篇最多使用一到两个真正帮助理解的架构图、时序图或状态图。图必须在读者理解问题以后出现，不能把一张全景图放在开头要求初学者自行消化。

表格主要用于对比方案、状态或边界，不用于堆积几十个字段。图中的节点和正文术语必须能映射到真实代码对象。

### 4.8 面试完成标准

每篇结束后，读者至少应能完成：

- 用 30 秒解释本章解决的问题。
- 用 3 分钟讲清从朴素版本到当前设计的演化。
- 追踪一条真实代码调用链。
- 解释一个核心设计取舍。
- 描述一个失败或降级案例。
- 说出一项当前没有实现的生产能力。
- 回答“为什么不用更简单或更自动的方案”。

## 5. 证据与措辞规范

### 5.1 验证形态与运行证据等级

文档中的关键结论应同时说明“用什么验证”和“在哪类环境运行”。两者不是同一个枚举，不能把代码存在或单元测试覆盖写进运行时 `evidence_level`。

验证形态：

| 类型 | 含义 | 可以证明什么 |
|---|---|---|
| Implementation | 当前代码存在该分支或数据结构 | 能力已编码，不能证明可用性 |
| Automated Test | 单元、API 或契约测试覆盖对应行为 | 在受控输入下满足被测试的契约 |

代码和评测 Artifact 使用的运行证据等级只有以下四类：

| `evidence_level` | 含义 | 可以证明什么 |
|---|---|---|
| `offline_fixture` | 使用固定数据或替身评测 | 可复现的离线质量，不是实时能力 |
| `local_live` | 连接本地真实服务或真实模型 | 本地集成可运行，不是生产稳定性 |
| `controlled_fault` | 在受控故障环境验证 | 能处理指定故障，不代表覆盖未知故障 |
| `production` | 来自真实生产流量和治理环境 | 只能在确有生产 Artifact 时使用 |

每个量化结论都必须附带：运行日期、运行命令、数据集或 case hash、环境类型、Artifact 路径、代码 commit，以及工作区是否干净。

### 5.2 事实来源优先级

发生冲突时按以下方式处理：

1. 先检查当前运行代码和数据模型。
2. 再检查能约束行为的自动化测试。
3. 再检查带 provenance 的评测或运行 Artifact。
4. README、历史文档和图示只能用于定位，不能单独覆盖前三项。
5. 生产结论必须有生产证据，不能由代码、测试或 local-live 推导。

### 5.3 全系列禁用的夸大表述

| 不应直接使用 | 当前更准确的表述 |
|---|---|
| 通用自主根因分析平台 | 面向限定信号和工具集合的受控 Incident 诊断 Agent |
| 告警 exactly-once | 指纹 upsert、进程内去重与并发限制 |
| 事件溯源状态机 | 最新 IncidentState 投影和生命周期优先级合并 |
| 模型化 reranker | 规则加权融合或 RRF |
| 通用 OOD 检测 | Trust Gate、阈值和无可信来源拒答 |
| 逐句事实核验 | 引用 ID 契约和生成前后引用检查 |
| 通用因果推理 | 规则、关键词和静态信号目录驱动的 RCA 基线 |
| 图推理数据库 | 报告内可移植 Evidence Graph 读模型 |
| 任意节点持久化恢复 | MemorySaver 加 latest Snapshot，以及审批恢复 fallback |
| 事件重放或 time travel | 聚合 Trace、Snapshot、审批、变更、报告和评测的 Replay 读模型 |
| 真实沙箱变更 | 结构化 dry-run、人工结果记录和本地 fixture sandbox |
| 自动生产修复和回滚 | 生产写默认不存在，报告提供人工执行和回滚建议 |
| 完整 A2A 标准实现 | A2A-compatible 业务能力 facade |
| 生产准确率、生产 MTTR | 指定环境和数据集上的离线或本地评测指标 |

## 6. 贯穿案例与内容归属

系列统一使用少量案例，避免每篇重新发明背景：

| 案例 | 用途 | 主要出现专题 |
|---|---|---|
| Redis 延迟或 maxclients | 主案例，贯穿告警、诊断、证据、报告和评测 | 01–13 |
| MySQL 慢查询或连接耗尽 | 对照不同信号、工具和根因模式 | 02，06–09，13 |
| K8s CrashLoop/OOM | 展示离线 golden case 与适配器边界 | 03–05，07–09，13 |
| Milvus 不可用 | 展示检索降级和拒答 | 04–05，13 |
| 无匹配 Runbook | 展示证据不足而不是编造答案 | 05，08–09 |
| 危险 SQL 或写操作 | 展示 pre-tool 风险控制 | 07，10–11 |
| 等待审批后进程重启 | 展示 Snapshot 恢复能力和限制 | 10，12 |
| A2A 查询状态与 Replay | 展示业务 facade 而非底层工具暴露 | 14 |

同一案例在不同文章中只解释本篇负责的切面。例如 `04` 只解释召回与融合，不展开 LLM 生成；`08` 只解释 Evidence 和 Hypothesis，不提前生成完整报告。

## 7. 专题实施卡片

### 00. 从 Demo 到可暴露服务：全系列事实口径与生产边界

- **计划文件**：`00-全系列事实口径与生产边界.md`
- **定位**：必读前置基线，不是 FastAPI 入门教程。
- **核心问题**：当前项目哪些能力已经实现、验证到什么程度、距离生产还有什么缺口？
- **必须覆盖**：
  - FastAPI 路由、静态 token/scope RBAC 和默认关闭鉴权的含义。
  - liveness、全局 readiness、RAG/AIOps 分能力 readiness。
  - SQLite/MySQL、Milvus、外部适配器和本地文件 Artifact 的部署边界。
  - 外部绑定、CORS、auth、mock/fallback 的实际检查逻辑与文档漂移。
  - SSO/OIDC、TLS、限流、密钥治理、多租户、HA/DR、生产压测等缺口。
- **不能写成**：已完成企业级生产平台或通过生产安全认证。
- **核心代码入口**：`app/main.py`、`app/config.py`、`app/core/auth.py`、`app/api/health.py`。
- **主要测试**：`tests/test_auth_rbac.py`、`tests/test_health_api.py`、`tests/test_operational_scripts.py`。
- **完成标准**：形成统一术语表、证据等级表和全系列声明黑名单，后续各篇不得与之冲突。

### 01. 从人工 OnCall 到受控诊断闭环：AutoOnCall 为什么存在

- **计划文件**：`01-从人工值班到受控诊断闭环.md`
- **核心问题**：为什么告警通知、普通聊天机器人或单次 RAG 问答都不足以完成运维诊断？
- **学习结果**：读者能够描述从告警进入、诊断、取证、审批到报告查询的完整业务闭环。
- **必须覆盖**：
  - 从人工 OnCall、告警通知、普通聊天、RAG、实时工具到受控诊断循环的逐步演化。
  - 每个朴素版本解决了什么，又为什么不足以构成诊断闭环。
  - 为什么知识、现场观测、诊断决策、风险授权和运行记录必须分层。
  - 在读者理解问题以后，再映射 FastAPI、LangGraph、工具适配器、持久化和前端工作台。
- **不能写成**：无需人工值守的自治运维系统。
- **核心代码入口**：`app/main.py`、`app/api/aiops.py`、`app/api/alerts.py`、`app/services/aiops_service.py`。
- **主要测试**：`tests/test_aiops_mainline_api.py`、`tests/test_aiops_e2e_api.py`、`tests/test_static_aiops_demo.py`。
- **主案例**：Redis 告警从进入系统到生成报告的全景，不在本篇深入算法细节。
- **完成标准**：新手能画出系统边界并说明每层解决的问题，而不是只复述目录名。

### 02. 告警如何变成可管理的 Incident：标准化、身份与生命周期投影

- **计划文件**：`02-告警如何变成故障事件.md`
- **核心问题**：多次、重复、乱序的告警怎样映射为稳定的诊断输入和当前 Incident 状态？
- **必须覆盖**：
  - `AlertEvent`、`Incident`、`IncidentState` 三个对象的职责和区别。
  - fingerprint、upsert、latest snapshot 和生命周期优先级合并。
  - webhook 自动诊断开关、进程内去重、Semaphore 并发限制。
  - SQLite/MySQL 中告警和状态投影的持久化方式。
- **失败案例**：同一告警重复到达、状态乱序、服务多副本时进程内去重失效。
- **不能写成**：append-only 告警事件库、严格 FSM、分布式 exactly-once。
- **核心代码入口**：`app/models/alert.py`、`app/models/incident.py`、`app/models/incident_state.py`、`app/services/alert_ingestion_service.py`、`app/services/incident_lifecycle.py`、`app/services/incident_state_builder.py`。
- **主要测试**：`tests/test_alert_ingestion_service.py`、`tests/test_alerts_api.py`、`tests/test_incident_overview_api.py`。
- **完成标准**：给出对象关系、身份规则、乱序合并示例和多副本限制。

### 03. 运维知识如何可靠入库：解析、切分、双索引与质量观测

- **计划文件**：`03-运维知识如何可靠入库.md`
- **核心问题**：不同格式的 Runbook、复盘和工单怎样变成稳定、可追踪的检索单元？
- **必须覆盖**：
  - TXT、Markdown、PDF、HTML、CSV、XLSX loader 的输入边界。
  - Markdown 两阶段切分与其他格式“逻辑单元加递归切分”的差异。
  - `_chunk_id`、文档哈希、chunk 哈希和向量 ID 的稳定性差异。
  - Embedding 批处理、重试、Milvus 固定向量维度与索引设置。
  - Milvus 向量索引和本地 lexical 索引双写，以及 stale source 的 fail-closed 行为。
  - 索引质量统计和多格式样例资产。
- **失败案例**：扫描版 PDF、Embedding 部分失败、向量写入成功但 lexical 写入失败。
- **不能写成**：OCR 文档平台、通用 Wiki connector、事务性双索引、全局内容寻址 chunk ID。
- **核心代码入口**：`app/api/file.py`、`app/services/document_loaders/`、`app/services/document_splitter_service.py`、`app/services/vector_embedding_service.py`、`app/services/vector_index_service.py`、`app/services/lexical_index_service.py`、`app/services/indexing_quality_service.py`。
- **主要测试**：`tests/test_document_loaders.py`、`tests/test_file_api_boundaries.py`、`tests/test_indexing_quality_service.py`、`tests/test_vector_store_manager.py`。
- **完成标准**：至少追踪 Markdown 和表格文件各一条完整入库路径，并说明部分失败后的状态。

### 04. 可信 Hybrid Retrieval：双路召回、融合、Trust Gate 与降级

- **计划文件**：`04-可信混合检索.md`
- **核心问题**：系统怎样组合语义相关性和关键词精确匹配，并判断结果是否足以交给生成模型？
- **必须覆盖**：
  - Milvus 向量召回和本地 BM25-like lexical 召回。
  - weighted fusion、RRF、基础排名和 intent rule multiplier。
  - 规则 rerank、上下文预算和当前有限的 required source 约束。
  - Trust Gate、无可信来源、向量失败后的 lexical degraded 路径。
- **失败案例**：Milvus 不可用、只有低分 chunk、查询需要来源多样性但当前规则未覆盖。
- **不能写成**：Cross-Encoder/LLM reranker、完整 BM25 服务、通用来源多样性算法、通用 OOD 分类器。
- **核心代码入口**：`app/services/rag_retrieval_service.py`、`app/services/lexical_index_service.py`、`app/services/vector_store_manager.py`、`app/services/context_budget.py`、`app/services/rag_read_models.py`。
- **主要测试**：`tests/test_rag_retrieval_service.py`、`tests/test_lexical_index_service.py`、`tests/test_rag_boundaries.py`、`tests/test_milvus_client_boundaries.py`。
- **完成标准**：使用一个查询手算或逐步展示两路得分、融合排序、门禁和降级结果。

### 05. 可审计的 RAG 回答：工具隔离生成、引用契约与拒答

- **计划文件**：`05-可审计的检索增强回答.md`
- **核心问题**：怎样让模型只基于检索上下文回答，并让用户能够检查回答依据？
- **必须覆盖**：
  - `/api/chat` 的显式 retrieval 加 tool-isolated grounded generation 路径。
  - chunk ID、prompt 约束、生成前后引用检查和最终 `done` 事件契约。
  - Trust Gate、模型拒答、无可信来源和 citation failure 的不同语义。
  - non-stream 与 stream 的共同最终契约，以及流式内容先于 post-check 发送的差异。
- **失败案例**：无匹配 Runbook、模型引用不存在的 chunk、流式回答最终被引用策略判失败。
- **不能写成**：通用工具 Agent、逐 claim 语义蕴含、完整知识库 Prompt Injection 防御、持久化多轮 grounded memory。
- **核心代码入口**：`app/api/chat.py`、`app/services/rag_agent_service.py`、`app/services/rag_answer_policy.py`、`app/services/rag_read_models.py`。
- **主要测试**：`tests/test_chat_rag_api.py`、`tests/test_rag_agent_citations.py`、`tests/test_rag_stream_boundaries.py`、`tests/test_ragas_eval_cases.py`。
- **完成标准**：给出可信回答、拒答和错误引用三条可复现链路，并区分 token 流与最终状态。

### 06. Plan-Execute-Replan：从诊断目标到证据驱动续查

- **计划文件**：`06-规划执行再规划诊断循环.md`
- **核心问题**：Incident 怎样被拆成受控诊断步骤，系统又怎样根据证据继续或停止？
- **必须覆盖**：
  - `AIOpsState`、`PlanStep`、Planner、Executor、Replanner 的契约。
  - Planner 的结构化 LLM 主路径与规则 fallback。
  - 一次初始规划后，Executor 与 Replanner 之间的循环。
  - Replanner 默认确定性 Evidence Analyzer，以及可选受约束 LLM critic。
  - 最大步数、预算、失败工具和停止条件。
- **失败案例**：Planner 模型不可用、工具连续失败、证据不足但预算耗尽。
- **不能写成**：每轮重新调用 Planner 的开放式自治规划、无限自我反思、模型可绕过安全决策。
- **核心代码入口**：`app/agent/aiops/state.py`、`app/agent/aiops/planner.py`、`app/agent/aiops/executor.py`、`app/agent/aiops/replanner.py`、`app/services/aiops_service.py`。
- **主要测试**：`tests/test_planner_degradation.py`、`tests/test_replanner_decision.py`、`tests/test_aiops_plan_fallback.py`、`tests/test_aiops_service_events.py`。
- **完成标准**：图和正文都准确表示 `Planner -> Executor <-> Replanner -> END`，并追踪一次 fallback。

### 07. Agent 工具工程：Contract、Registry、适配器、预算与 Artifact

- **计划文件**：`07-智能体工具工程.md`
- **核心问题**：怎样把指标、日志、数据库和 Runbook 调用变成可治理、可追踪的 Agent 工具？
- **必须覆盖**：
  - `ToolContract`、Registry、具体 Tool 和 Integration Adapter 的分层。
  - 权限、风险、环境、超时、预算、read-only 与 write 类别。
  - 工具内部重试与工作流级 Replan 重试的差别。
  - 相邻低风险只读步骤的有界 fan-out。
  - 大结果脱敏、摘要、哈希和本地 Artifact 指针。
- **失败案例**：超时、权限错误、外部系统未配置、过大结果、危险写操作。
- **不能写成**：运行时通用 JSON Schema 强校验、任意 MCP 动态插件市场、分布式 Artifact Store、所有调用都自动重试。
- **核心代码入口**：`app/tools/base.py`、`app/tools/registry.py`、`app/tools/`、`app/integrations/`、`app/agent/aiops/executor.py`、`app/services/aiops_execution_records.py`。
- **主要测试**：`tests/test_tool_registry.py`、`tests/test_aiops_tool_contract_api.py`、`tests/test_executor_evidence.py`、`tests/test_ops_tool_boundaries.py`。
- **完成标准**：至少追踪一个 read-only 工具和一个高风险工具，并解释两层重试及 Artifact 生命周期。

### 08. 从工具结果到根因假设：Evidence 建模、冲突与 Replan

- **计划文件**：`08-从工具结果到根因假设.md`
- **核心问题**：异构工具结果怎样被统一成证据、冲突和候选根因，并驱动下一步查询？
- **必须覆盖**：
  - `Evidence`、`Hypothesis`、`ToolCallRecord` 之间的关系。
  - 规则、关键词、诊断信号目录和 golden case 特殊规则。
  - symptom、root-cause、context、change/history 等证据角色。
  - 支持、反驳、冲突、缺失证据和补查步骤。
  - 安全决策为什么不交给可选 LLM critic 改写。
- **失败案例**：指标和日志互相冲突、只有症状无根因、Redis/MySQL 信号不足。
- **不能写成**：通用因果图学习、统计因果发现、任意领域 RCA、LLM 自由生成处置步骤。
- **核心代码入口**：`app/models/evidence.py`、`app/models/hypothesis.py`、`app/agent/aiops/evidence_analyzer.py`、`app/services/diagnostic_signal_catalog.py`、`app/services/diagnostic_signal_rules.py`、`app/agent/aiops/replanner.py`。
- **主要测试**：`tests/test_evidence_analyzer.py`、`tests/test_evidence_quality.py`、`tests/test_executor_evidence.py`、`tests/test_replanner_decision.py`。
- **完成标准**：用一组互相支持和冲突的工具结果，逐步生成 Evidence、Hypothesis 和 Replan 决策。

### 09. 从根因假设到可审计报告：充分性、Alignment 与 Evidence Graph

- **计划文件**：`09-从根因假设到可审计报告.md`
- **核心问题**：系统如何决定证据够不够，并让报告中的结论能够回到原始取证结果？
- **必须覆盖**：
  - 主域证据、症状指标/日志、Runbook/历史等 coverage gate。
  - `DiagnosisReport` 的结论、证据、建议、风险和引用结构。
  - Conclusion Alignment、citation 和 Evidence ID 回链。
  - Evidence Graph 的节点、边和报告派生过程。
  - 结构化 `DiagnosisReport` 作为单一事实源、Markdown 派生渲染，以及测试对关键映射的校验范围。
- **失败案例**：证据覆盖不足、引用丢失、结论只能宽泛回链、报告质量门禁失败。
- **不能写成**：数学充分性证明、语义事实蕴含验证、图数据库或图算法 RCA。
- **核心代码入口**：`app/models/report.py`、`app/services/report_generator.py`、`app/services/report_quality.py`、`app/services/evidence_quality.py`、`app/services/evidence_graph.py`、`app/services/report_markdown.py`。
- **主要测试**：`tests/test_report_generator.py`、`tests/test_demo_report_generation.py`、`tests/test_evidence_quality.py`、`tests/test_aiops_models.py`。
- **完成标准**：报告中的关键结论、证据、建议都能追踪到结构化字段，并明确回链不等于语义证明。

### 10. Agent 安全不是一个开关：身份、工具策略与人工审批

- **计划文件**：`10-身份工具策略与人工审批.md`
- **核心问题**：在 LLM 参与规划时，系统如何阻止越权或危险步骤直接触达基础设施？
- **必须覆盖**：
  - `allow / approval_required / forbidden` 三类 pre-tool 结果。
  - 工具元数据、PlanStep 风险、环境、参数、动作文本和有限正则规则。
  - Executor 和 Registry 的双重执行前检查。
  - viewer、operator、approver、admin 的静态 scope 映射。
  - 审批请求、pending 条件更新、重复决定和并发创建边界。
- **失败案例**：危险 SQL、生产写操作、operator 尝试批准、相同审批重复提交。
- **不能写成**：通用 Prompt Injection 防御、完整策略引擎、并发创建完全幂等、严格四眼分离、企业身份平台。
- **核心代码入口**：`app/agent/aiops/risk_controller.py`、`app/agent/aiops/executor.py`、`app/tools/registry.py`、`app/core/auth.py`、`app/models/approval.py`、`app/services/approval_service.py`、`app/services/approval_workflow.py`。
- **主要测试**：`tests/test_risk_controller.py`、`tests/test_auth_rbac.py`、`tests/test_approval_service.py`、`tests/test_change_eval_cases.py`。
- **完成标准**：从用户身份到工具执行画出完整授权链，并展示 allow、approval 和 forbidden 三条路径。

### 11. 批准以后发生什么：Safe Change 的状态与真实执行边界

- **计划文件**：`11-安全变更的真实执行边界.md`
- **核心问题**：审批通过后，系统如何安全推进变更，又为什么默认不会执行真实生产写入？
- **必须覆盖**：
  - `ChangePlan`、pre-check、dry-run、observation、rollback 等结构化状态。
  - `dry_run_only`、`manual_record`、`sandbox` 三种模式。
  - 稳定 ID、业务唯一键、SQLite/MySQL 持久化和重试语义。
  - 状态同步到 Trace、IncidentState 和 DiagnosisReport。
  - 生产环境 sandbox 默认标记为 `escalated` 并转人工接管，以及人工结果记录；这里不会自动创建一条新的审批请求。
- **失败案例**：pre-check 不通过、结构化 dry-run 拒绝、人工报告失败、重复执行请求。
- **不能写成**：真实 Kubernetes/Redis/MySQL dry-run、本地 fixture 等于真实沙箱、指标阈值自动观察、生产自动写入和自动 rollback。
- **核心代码入口**：`app/models/change_plan.py`、`app/models/change_execution.py`、`app/services/change_plan_builder.py`、`app/services/change_execution_checks.py`、`app/services/change_execution_service.py`、`app/services/change_execution_read_models.py`。
- **主要测试**：`tests/test_change_execution_checks.py`、`tests/test_change_execution_service.py`、`tests/test_change_execution_api.py`、`tests/test_change_execution_models.py`。
- **完成标准**：完整展示三种模式的状态流转，并以显眼边界框说明没有生产写适配器和自动回滚。

### 12. 从运行事件到 Incident 重建：SSE、Trace、Snapshot、Store 与 Replay

- **计划文件**：`12-从运行事件到故障重建.md`
- **核心问题**：一个长时间运行、可能等待审批的 Agent，怎样被观察、查询和有限恢复？
- **必须覆盖**：
  - SSE 进度事件、节点事件、工具事件、风险事件和变更事件。
  - Trace 脱敏、工具输出 Artifact、摘要、哈希和指针。
  - `AIOpsStateStore` Protocol 及 SQLite/MySQL 实现。
  - LangGraph MemorySaver 与 latest Session Snapshot 的职责区别。
  - 审批恢复的 Snapshot、Report fallback，以及 MemorySaver 不参与恢复决策。
  - Incident overview、run status 和 Replay 派生读模型。
  - Replay 中 Trace、审批、变更、报告和评测信息的聚合及启发式关联限制。
  - Snapshot 保存较完整状态，当前缺少统一的全局脱敏和体积上限；本地工具 Artifact 也尚未纳入数据库 retention 清理。
  - MySQL Store 虽有完整实现，但当前自动化测试未连接真实 MySQL 实例验证恢复链路。
  - Replay 的 eval 关联可能使用启发式 token overlap，当前不会完整记录匹配方式，stale summary 也仍可能被关联。
- **失败案例**：等待审批时重启、Snapshot 缺失、只有报告 fallback、stale eval 进入 Replay。
- **不能写成**：任意节点精确恢复、数据库 LangGraph durable checkpoint、事件重新执行、time travel、完整 Artifact retention 治理。
- **核心代码入口**：`app/api/sse.py`、`app/services/trace_service.py`、`app/services/aiops_store.py`、`app/services/sqlite_store.py`、`app/services/mysql_store.py`、`app/services/aiops_snapshot_service.py`、`app/services/aiops_read_models/`、`app/services/aiops_service.py`。
- **主要测试**：`tests/test_trace_service.py`、`tests/test_aiops_session_snapshot_store.py`、`tests/test_aiops_trace_events.py`、`tests/test_sqlite_aiops_recovery.py`、`tests/test_read_models.py`、`tests/test_incident_overview_api.py`。
- **完成标准**：分别定义“观察”“查询”“恢复”“重建”“回放”，并用进程重启案例展示可恢复和不可恢复部分。

### 13. 如何证明 RAG 与 Agent 没有退化：分层评测、证据等级与反馈治理

- **计划文件**：`13-检索增强与智能体分层评测.md`
- **核心问题**：怎样用可复现证据判断一次改动是否改善或破坏了系统？
- **必须覆盖**：
  - 单元/API contract、知识质量、检索、RAGAS、AIOps RCA、Replanner、Change、安全、性能和受控故障评测层。
  - deterministic surrogate、fixed-context real generation、runtime retrieval/generation 的区别。
  - `offline_fixture / local_live / controlled_fault / production` 证据等级。
  - run ID、commit、dirty worktree、case hash、依赖和配置指纹。
  - candidate 与 official baseline，以及 stale Artifact。
  - 用户反馈、bad case、review backlog 和显式 promotion。
- **失败案例**：代码工作区不干净、数据集漂移、离线通过但 runtime 失败、stale summary 仍可查询。
- **不能写成**：official 即 production、离线 fixture 即真实检索、有限攻击 case 即通用安全率、反馈自动训练模型。
- **核心代码入口**：`scripts/eval/eval_environment.py`、`scripts/eval/run_benchmark_baseline.py`、`scripts/eval/verify_api_contracts.py`、`scripts/eval/eval_knowledge_quality.py`、`scripts/eval/eval_rag_cases.py`、`scripts/eval/eval_ragas_cases.py`、`scripts/eval/eval_change_cases.py`、`scripts/eval/eval_replanner_cases.py`、`scripts/eval/eval_performance.py`、`scripts/eval/export_bad_cases.py`、`scripts/sandbox/controlled_fault.py`、`scripts/sandbox/controlled_fault_runner.py`、`scripts/sandbox/controlled_fault_e2e.py`、`app/services/feedback_service.py`、`app/services/evaluation_read_models.py`。
- **主要测试**：`tests/test_api_contract_verifier.py`、`tests/test_eval_provenance.py`、`tests/test_benchmark_baseline.py`、`tests/test_knowledge_quality_eval.py`、`tests/test_rag_eval_cases.py`、`tests/test_ragas_eval_cases.py`、`tests/test_replanner_eval_cases.py`、`tests/test_change_eval_cases.py`、`tests/test_performance_eval.py`、`tests/test_controlled_fault.py`、`tests/test_feedback_service.py`。
- **完成标准**：同一个结论至少展示一种可接受证据和一种不可接受替代，并给出 baseline 晋级检查表。

### 14. 选修：从内部能力到 A2A 协作的业务 Facade

- **计划文件**：`14-智能体协作业务门面.md`
- **核心问题**：怎样在不暴露底层工具、审批和生产变更的情况下，把 AutoOnCall 能力提供给其他 Agent？
- **必须覆盖**：
  - A2A 默认关闭及启用边界。
  - Agent Card 中诊断、状态、Replay、Runbook 问答四个业务 Skill。
  - message send、SSE stream、Task 和 Artifact 映射。
  - read-only Skill 与 diagnosis Skill 的 scope 差异。
  - facade 如何复用现有领域服务和读模型。
- **失败案例**：未知 Skill、权限不足、请求取消不受支持、底层工具调用企图。
- **不能写成**：完整 A2A conformance、官方 SDK 实现、跨实现互操作验证、审批或 Change Skill。
- **核心代码入口**：`app/api/a2a.py`、`app/models/a2a.py`、`app/services/a2a_facade_core.py`、`app/services/a2a_payloads.py`、`app/services/a2a_skills.py`。
- **主要测试**：`tests/test_a2a_facade.py`、`tests/test_auth_rbac.py`。
- **完成标准**：展示一个 diagnosis Task 和一个 read-only Task，并明确 facade 与底层 Tool Registry 的隔离。

### 15. 项目量化指标与校招验收：从高分数字到可复核证据

- **计划文件**：`15-项目量化指标与校招验收.md`
- **核心问题**：怎样把知识质量、RAG、RAGAS、Agent、安全、性能和受控故障结果变成可计算、可复跑且不会越界的面试证据？
- **必须覆盖**：
  - candidate/official/stale baseline 状态与 `offline_fixture / local_live / controlled_fault / production` evidence level 的区别。
  - Recall@K、Precision@K、MRR、MAP、nDCG、Top-1/Top-3、Macro-F1、Tool Selection F1、P95 和 Wilson CI 的定义。
  - RAGAS `id-smoke / full` profile、四种 answer source、内置指标与项目 AspectCritic 指标的边界。
  - Faithfulness、Response Relevancy、ID Context Precision/Recall、Actionability、Completeness 和拒答门槛。
  - LangChain Core/Text Splitters/Milvus/MCP Adapters、LangGraph 与项目领域服务的职责分工。
  - 2026-07-12 冻结 run 的样本、指标、commit、Artifact 和失败 case；2026-07-13 candidate 必须单独标注。
  - 知识、检索、回答、Agent、安全、真实模型延迟、Token 和 controlled fault 的简历口径与禁用表述。
- **不能写成**：official 即 production、小样本 100% 即真实成功率、模拟评审即真人盲评、受控故障时间即生产 MTTD/MTTR、30 次请求即容量压测。
- **核心代码入口**：`scripts/eval/run_benchmark_baseline.py`、`scripts/eval/eval_environment.py`、`scripts/eval/eval_rag_cases.py`、`scripts/eval/eval_ragas_cases.py`、`scripts/eval/eval_cases.py`、`scripts/eval/eval_performance.py`、`scripts/eval/build_interview_summary.py`、`app/api/evaluations.py`。
- **主要测试**：`tests/test_benchmark_baseline.py`、`tests/test_eval_provenance.py`、`tests/test_rag_eval_cases.py`、`tests/test_ragas_eval_cases.py`、`tests/test_performance_eval.py`、`tests/test_change_eval_cases.py`、`tests/test_controlled_fault.py`、`tests/test_evaluation_api.py`。
- **完成标准**：任意一条简历数字都能回答定义、样本、阈值、日期、运行命令、commit、evidence level、Artifact 和不能外推的范围。

## 8. 实施顺序与阶段验收

后续逐篇实现时按以下批次推进，避免先写局部细节再反复修改总口径。

### 批次 A：基线与业务对象

- [ ] 创建 `专题阅读导航.md` 导航页。
- [ ] 完成 `00-全系列事实口径与生产边界.md`。
- [ ] 完成 `01-从人工值班到受控诊断闭环.md`。
- [ ] 完成 `02-告警如何变成故障事件.md`。
- [ ] 验收：术语、对象关系、生产边界和总链路不互相矛盾。

### 批次 B：RAG 链路

- [ ] 完成 `03-运维知识如何可靠入库.md`。
- [ ] 完成 `04-可信混合检索.md`。
- [ ] 完成 `05-可审计的检索增强回答.md`。
- [ ] 验收：离线构建、在线检索、生成与拒答的职责不混写；至少有一条降级链路。

### 批次 C：Agent 推理链路

- [ ] 完成 `06-规划执行再规划诊断循环.md`。
- [ ] 完成 `07-智能体工具工程.md`。
- [ ] 完成 `08-从工具结果到根因假设.md`。
- [ ] 完成 `09-从根因假设到可审计报告.md`。
- [ ] 验收：Graph 拓扑、重试层级、Evidence/Hypothesis/Report 边界与代码一致。

### 批次 D：治理和运行态

- [ ] 完成 `10-身份工具策略与人工审批.md`。
- [ ] 完成 `11-安全变更的真实执行边界.md`。
- [ ] 完成 `12-从运行事件到故障重建.md`。
- [ ] 验收：审批不等于执行、Snapshot 不等于任意恢复、Replay 不等于重新执行。

### 批次 E：评测和选修

- [ ] 完成 `13-检索增强与智能体分层评测.md`。
- [ ] 按需要完成 `14-智能体协作业务门面.md`。
- [ ] 完成 `15-项目量化指标与校招验收.md`。
- [ ] 验收：所有指标能追踪到定义、样本、阈值、运行证据和 Artifact；A2A 不暴露低层工具或 Change。

## 9. 单篇 Definition of Done

只有同时满足以下条件，某篇状态才能从“待编写”更新为“完成”：

- [ ] 文件名使用计划中约定的中文名称，导航只链接已经存在的文件。
- [ ] 开头明确上一章、本章、下一章，以及本章只新增的核心能力。
- [ ] 先展示问题、朴素版本和失败方式，再引入正式术语和项目实现。
- [ ] 正文前三分之一没有被文件路径、字段大全和测试列表占据。
- [ ] 同一贯穿案例从问题现场一直运行到本章终态，不是只在开头出现一次。
- [ ] 读者能说明当前设计相对上一阶段新增了什么、仍缺什么。
- [ ] 所有关键模型、服务、API 和测试路径在当前仓库真实存在。
- [ ] 至少追踪一条正常调用链和一条失败或降级调用链。
- [ ] 至少解释一个当前方案与替代方案的取舍。
- [ ] 明确区分实现、自动化测试，以及 `offline_fixture`、`local_live`、`controlled_fault`、`production` 四级运行证据。
- [ ] “已实现 / 已验证 / 尚未实现”边界表完整。
- [ ] 所有数字都有日期、环境、命令和 Artifact 来源。
- [ ] 提供至少一条可写简历表述和一条禁用表述。
- [ ] 提供 30 秒解释、3 分钟解释、设计取舍题和失败边界题。
- [ ] 运行对应定向测试，记录结果但不把它扩大为生产证据。
- [ ] 运行引用校验，确保没有失效链接和非 ASCII 运行路径。
- [ ] 额外检查普通反引号中的 `app/`、`tests/`、`scripts/` 路径和 Markdown heading anchor；当前引用校验器不会覆盖这两类引用。
- [ ] 与 `00-全系列事实口径与生产边界.md` 和相邻专题进行交叉一致性检查。

推荐的基础校验命令：

```powershell
.\.venv\Scripts\python.exe -m pytest <相关测试文件> -q -p no:cacheprovider --no-cov
.\.venv\Scripts\python.exe .\scripts\maintenance\verify_references.py
```

全系列完成后再运行仓库级快速或完整测试，并单独记录因外部依赖未配置而跳过的验证。

## 10. 简历方向映射

| 求职方向 | 主读专题 | 可以形成的能力证据 |
|---|---|---|
| RAG / 知识库 | 03–05，13，15 | 多格式入库、Hybrid Retrieval、引用门禁、RAGAS、量化验收 |
| Agent 工程 | 06–09，12–13，15 | 结构化规划、工具治理、证据驱动 Replan、可审计报告、运行态重建与 RCA 指标 |
| AIOps / 可观测性 | 01–02，07–13，15 | Incident 建模、跨源取证、风险控制、Trace、Replay、故障和性能评测 |
| Python 后端 / 安全治理 | 00，02，07，10–13，15 | FastAPI 契约、RBAC、幂等与持久化、安全变更、读模型与 Scorecard |
| Agent 互操作 | 14 | 业务 Skill、Task/Artifact 映射、北向协议 facade |

系列最终应帮助项目作者形成类似以下可追问表述：

> 基于 FastAPI 和 LangGraph 实现受控 Incident 诊断闭环；使用结构化 Planner、静态 Tool Registry、确定性 Evidence Replanner 和报告回链组织跨源证据，并通过 pre-tool 风险策略、人工审批、Trace、Snapshot、派生读模型及分层离线评测约束执行和验证质量。

这句话仍需根据实际完成的专题、演示环境和最新评测 Artifact 缩减，不能直接当作固定简历模板复制。

## 11. 计划维护规则

- 本文件只在专题边界、排序或验收标准发生变化时修改。
- 代码实现变化只更新受影响专题；若改变全局能力边界，同时更新 `00` 和本计划。
- `专题阅读导航.md` 负责记录文章状态、阅读顺序和最后审计 commit；只链接已经创建的文章，不能提前链接占位文件。
- 发现 README、代码、测试和评测 Artifact 不一致时，先记录差异并修正文档，不用模糊措辞掩盖。
- 如果未来新增真正的生产执行、持久化 LangGraph checkpointer、模型化 reranker 或 A2A conformance，应新建专题或重划边界，不能悄悄替换当前解释。
- 每轮大规模文档更新后运行引用校验和相关测试，避免目录计划再次领先于实际仓库。
