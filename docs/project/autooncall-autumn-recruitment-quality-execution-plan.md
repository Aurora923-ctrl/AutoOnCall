# AutoOnCall 秋招质量量化执行计划

## 1. 文档用途

这是一份面向秋招展示和项目成熟度的实施清单，不是愿望列表。后续执行应严格按本文顺序推进。每个阶段只有在代码、测试、真实运行产物、边界说明和面试展示材料同时满足验收标准后，才算完成。

核心原则：

1. 每个数字必须能追溯到代码、数据、配置、命令、运行环境和原始 case。
2. `offline_fixture`、`local_live`、`controlled_fault`、`production` 必须分开展示。
3. 不把固定 case 的 100% 通过率描述成生产准确率。
4. 不为凑指标新增第二套服务、第二套前端或重复的报告系统。
5. 优先复用现有 `eval/`、Trace、Feedback、Replay、评测 API 和工作台。
6. 所有历史结果写入同一个 benchmark 目录体系，不散落新的 `logs/*_final_v2.json`。
7. 展示失败 case、置信区间和能力边界，不只展示全绿结果。

## 2. 优化后的执行提示词

后续可以把本文连同下面这段提示词发给 Codex：

```text
请把《AutoOnCall 秋招质量量化执行计划》作为本次任务的唯一实施路线，结合当前仓库实际状态，从第一个未完成阶段开始，按依赖顺序持续实施，直到本文定义的“秋招工程完成标准”全部满足。

执行要求：

1. 开始前重新审计代码、git 状态、20 份知识资产、最新 benchmark 和现有测试，不假设文档中的数据仍然最新。
2. 已完成且验证仍有效的内容不要重做；不完整、过期或与当前代码不一致的内容继续修复。
3. 每次只引入解决当前阶段所必需的最小实现，优先复用现有模型、服务、eval 脚本、Trace、Feedback、API 和前端评测面板。
4. 不创建重复前端、重复 benchmark 框架、重复数据模型、重复报告入口或无实际用途的文档。
5. 每个指标必须保存 numerator、denominator、sample_count、统计方法、置信区间、case 明细、失败原因和 provenance。
6. 严格区分 offline_fixture、local_live、controlled_fault、production。没有真实生产样本时，只完成生产指标采集能力，不得伪造 production 结果。
7. 对需要模型账号、Docker 服务或人工标注的步骤，先检查当前环境能否直接完成；能完成就执行，不能完成则实现可运行的采集/导入/评审流程，并在最终清单中准确标记外部证据缺口。
8. 不回退或覆盖用户已有修改；发现无关变更只忽略，发现影响任务的变更则与其兼容。
9. 每个阶段完成后运行该阶段测试和回归门禁，修复失败后再进入下一阶段。
10. 最终运行完整测试、benchmark、受控故障实验和面试汇总，输出真实结果、失败样本、运行命令、产物路径、简历可用指标和不能声称的边界。
11. 不创建 commit、分支或 PR，除非我在本次消息中明确要求。
12. 不停在方案或半成品；在当前环境允许的范围内完成实现、验证和最终交付。

完成判定以文档中的验收标准为准，不以“代码已经写了”作为完成。
```

## 3. 当前实际状态

状态日期：2026-07-12 15:20（Asia/Shanghai）。

### 3.0.0 本轮续做结果

- 阶段 8 的统一交付链路已完成：`run_benchmark_baseline.py` 现在会在同一 benchmark
  run 目录内原子生成 `baseline_manifest.json/md` 与
  `interview_scorecard.json/md`，更新 `latest.json` 前 scorecard 已经就绪，不再依赖第二条
  手工命令补产物。
- `/api/eval/scorecard`、现有工作台系统视图、五分钟演示、简历指标和能力边界均已核对为
  同一入口；页面展示 evidence level、run ID、样本量、CI、失败/降级 case 和原始产物。
- `make full-gate` 已覆盖 format check、Ruff、mypy、security、全量 pytest/coverage、
  API contract、知识质量、RAG、回答质量、Agent RCA、安全、性能 smoke、受控故障
  readiness、reference/hygiene、benchmark 和 scorecard。
- 新增 `make performance-real-model`，按阶段 6 收缩后的 `20 RAG + 10 AIOps` 下限读取
  `local_live` 持久化 Trace；真实模型样本不足时保持 `observed_not_accepted/not_run`，
  不虚构 Token 成本。
- 新增 `make official-baseline`，仅允许 clean 且已有 commit 的工作区进入 official
  baseline 运行；当前未获提交授权且工作区非 clean，因此阶段 0 仍保持发布动作未完成。
- 阶段 3 真实人工盲评仍不可由自动化或 LLM 代填，模板
  `eval/ragas_stage3_core.review.json` 保持 reviewer pending。

### 3.0.0.1 授权后的最终执行

- 阶段 3 已按用户要求完成模拟 reviewer 流程：36/36 answer-run 已填写 7 项 rubric、
  decision、事实错误、严重幻觉和 notes，自动/模拟评审一致率 `1.00`。该证据明确标记为
  `simulated_review`，仅证明评审导入和统计流程可运行，不冒充独立真人盲评。
- 阶段 6 已执行真实 DashScope 模型请求，run
  `stage6-20260712T072656Z`：RAG `20/20`，P50 `3414.33ms`、P95
  `5816.70ms`；AIOps `10/10`，P50 `49123.07ms`、P95 `63262.73ms`。
  当前 HTTP/Trace 合同未暴露 provider token usage，因此 Token 标记
  `not_observed`，金额标记 `not_run`，未虚构成本。
- 全量 pytest、Ruff 和 Black 已通过；全量 pytest 共 542 个用例，覆盖率约 `83.33%`。
- 下一步是提交当前完整工作区，并在该 clean commit 上重新运行 benchmark，生成 official
  baseline 和最终 scorecard。

### 3.0 本轮执行进度

> 本节是执行状态的唯一入口。后续继续任务时先更新本节，再按“当前停点”继续，不能仅根据文件是否存在判断阶段完成。

| 阶段 | 当前状态 | 最近验证 | 阶段评分 | 判定 |
| --- | --- | --- | ---: | --- |
| 0 可信基线 | 核心完成，待 clean worktree 发布 | 历史 benchmark 已保存独立 run；最近已落盘 run 为 `20260710T090711Z-00aa78a5-8f1a4eb5`，7/7 模块通过，但为 `candidate_dirty_worktree` | 9.1/10 | 工程能力完成，official baseline 未完成 |
| 1 知识质量 | 已完成 | 20/20 文档解析、切分和 index-ready；209 chunks；Milvus CRUD 一致性通过 | 9.2/10 | 通过，超长和近似重复保留为 watch metric |
| 2 RAG 检索评测 | 企业排障核心检索通过，通用拒答保留 watch | 40 条相关测试通过，80 条开发/回归集 80/80；第三套冻结企业集 11/12，9 条企业排障正例全部通过，Recall@3 1.00、MRR 0.94、nDCG@3 0.96，唯一失败为旅行域外拒答 | 9.1/10 | 按收缩后的企业排障验收通过；通用域外拒答不再阻塞主线 |
| 3 回答质量与人工评审 | 确定性核心验收完成，等待真实人工盲评 | 建立 12 条企业排障核心集，3 次运行全部稳定通过；ID recall、引用存在/支持、行动性、拒答均 100%，事实错误和严重幻觉 0%；正式盲评模板含 36 个 answer-run | 8.8/10 | 工程与自动验收完成；真实人工 rubric 尚未填写，因此阶段未最终通过 |
| 4 Agent RCA | 离线结构化验收完成 | 48 条结构化 RCA case 全部通过；Top-1/Top-3、Macro-F1、必要证据、结论支持、Replan、needs_human、Trace 和报告一致率均为 1.00；工具选择 F1 0.9583、无效工具率 0；阶段 7 已补充受控故障 RCA 证据 | 9.2/10 | 通过；离线指标与阶段 7 `controlled_fault` 证据分开展示 |
| 5 安全与对抗评测 | 已完成离线阶段 | 43 条正反例实跑 43/43；Forbidden、Approval、Safe Allow F1 均为 100%；误拦截、审批绕过、未授权执行、敏感泄漏均为 0% | 9.2/10 | 通过，结论仅限 `offline_fixture` |
| 6 延迟、Token、成本和并发 | 工程骨架和本地 smoke 已完成，正式验收未完成 | performance evaluator、测试和 Locust 场景可运行；已有 15 请求与 14 请求的本地 smoke，均 0 失败，P95 分别约 36ms 和 51ms；当前证据仅能说明 fixture/local smoke，不能声明真实模型容量 | 6.5/10 | 部分完成，需按业务主线收缩验收 |
| 7 受控真实故障实验 | 最小可信主链完成 | 原 `autooncall` 容器完成 7/7 注入恢复、7 类真实 source 验证、Redis/MySQL 主链各 3/3；另完成 Redis/MySQL 各 1 条同窗口“注入→告警→诊断→RCA→恢复”，Top-1 均正确 | 9.1/10 | 通过最小可信验收；端到端仅 2 条本地样本，不代表生产 MTTD/MTTR |
| 8 面试交付与最终门禁 | 未开始 | 现有工作台和旧面试汇总可复用，但尚未接入本计划全部新模块，也没有最终 scorecard/full gate | 2.5/10 | 未完成 |

评分说明：阶段评分按本文验收标准对当前工作区进行工程审计，不等同于简历指标。只有达到 9.0 且完成真实运行验收的阶段才标记为通过。

### 3.0.1 剩余阶段数量与校招价值

按阶段 0 到阶段 8 的验收状态统计：

- 已通过：阶段 1、阶段 2、阶段 4、阶段 5、阶段 7，共 5 个阶段。
- 核心工程完成但仍有发布收尾：阶段 0，共 1 个阶段。
- 已执行但尚未完成：阶段 3、阶段 6，共 2 个阶段。
- 尚未开始：阶段 8，共 1 个阶段。
- 因此，若按“尚未通过最终验收”计算，仍有阶段 0、3、6、8 共 4 个阶段需要继续处理；若只问“完全还没执行”，则是阶段 8。

后续计划并非每一项对校招都有相同价值。项目业务主线固定为：

> 告警进入系统 -> Agent 规划 -> 并行读取真实证据 -> 结构化 RCA -> 证据不足时 Replan/needs_human -> 高风险处置经过审批、pre-check、dry-run 和回滚边界。

任何不能增强这条主线、不能形成可追溯产物、或只是为了让通用 benchmark 全绿的工作，都不得继续扩大范围。后续优先级如下：

| 优先级 | 阶段 | 校招价值 | 执行建议 |
| --- | --- | --- | --- |
| P0 | 阶段 4 Agent RCA | 已完成离线结构化验收。直接体现 AIOps Agent、结构化诊断、证据链、Replan 和人工接管 | 复用 48 条结果进入 scorecard；后续只由阶段 7 补充 `controlled_fault` 证据，不再扩充离线样本 |
| P0 | 阶段 8 面试交付与最终门禁 | 很高。决定已有成果能否在五分钟内讲清楚并现场复跑 | 必须完成 scorecard、演示脚本、简历指标和能力边界 |
| P0 | 阶段 7 受控真实故障实验 | 已完成最小可信验收。真实 Redis/MySQL 故障证据比纯 fixture 更能证明企业排障能力 | 复用 7/7 注入恢复、真实适配器与 2/2 端到端结果进入 scorecard；不为增加样本数重复相同注入 |
| P1 | 阶段 2 RAG 检索评测 | 高。证明 Runbook、官方文档和事故证据可以被正确检索 | 收尾企业排障核心 query、跨来源证据和合理独立集，不追求所有域外 query 全绿 |
| P1 | 阶段 5 安全与对抗评测 | 高。体现企业系统不会越权执行生产变更 | 复用已完成结果并接入最终 scorecard，不继续扩成通用内容安全系统 |
| P1 | 阶段 3 回答质量与人工评审 | 中高。证明答案有证据、可执行且不强行下结论 | 只评审 10-15 条核心企业排障 case，不建设大型人工标注项目 |
| P1 | 阶段 6 延迟、Token、成本和并发 | 中高。回答性能和成本问题，但不是作品主角 | 保留端到端/阶段 P50-P95、Token、超时重试和小型 Locust smoke |
| P2 | 阶段 0 clean official baseline | 中等。体现工程规范和可追溯发布 | 最终发布前完成，不应阻塞核心能力开发，也不得未经授权自动提交 |

结论：后续不再平均推进所有指标。阶段 4、7、8 是企业排障作品集主线；阶段 2、3、5 为其提供检索、答案可信度和安全边界；阶段 6 只提供必要的工程性能证据；阶段 0 最终发布时收尾。避免把时间消耗在通用问答拒答、反复造 holdout、虚高并发、大规模人工标注或重复前端上。

### 3.0.1.1 计划完成度与取舍

按阶段验收粗略折算，当前整体完成度约为 `75%-80%`；如果只看企业排障核心技术能力，完成度约为 `90%`。该比例用于项目管理，不作为简历指标。

| 分类 | 阶段 | 当前判断 |
| --- | --- | --- |
| 已通过 | 阶段 1、阶段 2、阶段 4、阶段 5、阶段 7 | 知识治理、企业排障核心检索、结构化 Agent RCA、离线安全变更链和最小可信受控故障已有可信结果 |
| 核心完成待发布 | 阶段 0 | 只差 clean worktree official baseline |
| 部分完成 | 阶段 3、6 | 已有实现或产物，但未满足收缩后的业务验收 |
| 基本未完成 | 阶段 8 | 需要统一 scorecard、演示和最终门禁 |

### 3.0.1.2 已完成指标总表

下表只记录已经有可追溯运行产物的指标。证据等级严格区分，不能把
`offline_fixture`、`local_live` 或 `controlled_fault` 包装成 `production`。

| 阶段 | 证据等级 | 样本/运行规模 | 已完成指标 | 当前边界 |
| --- | --- | ---: | --- | --- |
| 0 可信基线 | `candidate_dirty_worktree` | 7 个 required module | 7/7 模块通过；run `20260710T090711Z-00aa78a5-8f1a4eb5` | 工作区不干净，不能称 official baseline |
| 1 知识质量 | `local_live` | 20 份资产、209 chunks | 20/20 解析、切分、index-ready；Milvus CRUD 一致性通过 | 超长和近似重复为 watch metric |
| 2 RAG 检索 | `offline_fixture` + `local_live` | 80 条开发/回归；12 条冻结企业集 | 开发/回归 80/80；冻结企业集 11/12；9 条企业正例全通过；Recall@3 `1.00`、MRR `0.94`、nDCG@3 `0.96` | 通用旅行域拒答失败不阻塞企业主线 |
| 3 回答质量 | `offline_fixture` | 12 case × 3，共 36 answer-run | 12/12；三次稳定率 `1.00`；ID recall、行动性、引用存在/支持、拒答边界均 `1.00`；事实错误和严重幻觉 `0`；ID precision `0.70` | 等待真实人工盲评；Full Judge ResponseRelevancy 外部兼容失败 |
| 4 Agent RCA | `offline_fixture` | 48 case | 48/48；Top-1、Top-3、Macro Precision/Recall/F1、必要证据、结论支持、Replan F1、needs_human F1、Trace 和报告一致率均 `1.00`；工具 F1 `0.9583`，无效工具率 `0` | 不能代表 controlled-fault 或生产准确率 |
| 5 安全评测 | `offline_fixture` | 43 case | 43/43；Forbidden、Approval、Safe Allow F1 均 `1.00`；误拦截、审批绕过、未授权执行、敏感泄漏均 `0` | 不代表生产变更执行记录 |
| 6 性能 smoke | `offline_fixture` / `local_live` | 15 请求和 14 请求两组 | 0 失败；P95 约 `36ms` 和 `51ms`；performance evaluator 与 Locust 场景可运行 | 尚缺真实模型 RAG/AIOps token 与延迟分布 |
| 7 故障注入恢复 | `controlled_fault` | Redis 2、MySQL 2、downstream 3 | 7/7 注入和恢复通过；全部有 cleanup verification；Redis `maxclients` 恢复为 `10000`、DBSIZE 保持 `7`；MySQL 事故证据/工单计数保持 `2/3` | diagnosis 字段仍为 `not_run`，不能称端到端故障诊断实验 |
| 7 真实适配器与 RCA | `local_live` | 7 类 source；Redis/MySQL 各 3 次 | Prometheus、Loki、Redis、MySQL、CMDB、部署历史、工单全部通过；Redis 主链 3/3、MySQL 主链 3/3，业务签名稳定 | 与故障注入是独立运行，尚未绑定同一时间线 |
| 7 端到端受控故障 RCA | `controlled_fault` | Redis 1、MySQL 1 | 2/2 注入、诊断和恢复通过；Top-1 分别为 `redis_maxclients`、`mysql_slow_query`；Redis first useful diagnosis `3.373s`、alert-to-diagnosis `3.474s`、恢复 `0.325s`；MySQL分别为 `0.577s`、`0.678s`、`2.603s` | 仅为本地受控实验样本，不能称生产 MTTD/MTTR |
| 长期生产指标 | `production` | 真实样本不足 | 采集字段和计算边界已定义 | 显示 `production: not_enough_data` |

必须保留的计划：

- 企业故障知识治理与 Milvus 一致性。
- Runbook、官方文档、事故复盘和表格证据的检索质量。
- 结构化 RCA Top-1/Top-3、Macro-F1、混淆矩阵和失败 case。
- 工具选择、证据引用、Replan 和 `needs_human`。
- 审批、pre-check、dry-run、回滚和未授权执行拦截。
- Redis、MySQL、下游依赖的受控故障和恢复验证。
- 端到端及关键阶段耗时、Token、超时和重试。
- 最终 scorecard、五分钟演示、简历指标和能力边界。

降级为可选或 watch metric 的计划：

- 超长 chunk 和近似重复 chunk，只要不破坏检索与上下文完整性。
- Prometheus/Loki controlled fault；Redis/MySQL 主链完成后再考虑。
- 真实模型高并发压测和 provider 容量上限。
- RRF、lexical-only、vector surrogate 只作为策略对照，不作为业务主指标。
- Bootstrap 和 Wilson 不重复维护；比例指标默认使用 Wilson 95% CI。
- Production MTTD/MTTR 只建设采集能力，样本不足时显示 `not_enough_data`。

删除或停止继续扩展的计划：

- 针对游戏设备购买、贵金属价格、健身计划等消费领域的专项关键词拒答规则。
- 为让通用 holdout 全绿而不断创建新数据集、追加业务无关规则。
- 每个并发档位固定运行 3 分钟和追求 50/100 并发的展示目标。
- 在几十个样本上展示 P99 或声称最大稳定并发。
- SQLite/MySQL 全场景性能矩阵。
- 为凑满 20 次而重复相同故障注入。
- 没有第二位真实评审者时强制计算 Cohen's Kappa。
- 没有可信价格快照和真实调用时展示金额成本。
- 新建第二套前端、第二套 benchmark 框架或重复报告入口。

### 3.0.2 当前停点

本轮执行停在 **阶段 7 最小可信验收完成，下一工程停点为阶段 8；阶段 3 等待真实人工盲评**：

1. 已建立 `eval/ragas_stage3_core_cases.yaml`，包含 12 条企业排障核心 case：10 条回答质量正例和 2 条拒答；覆盖 CPU、OOM、磁盘、依赖 503、MySQL、Redis、Kubernetes、Loki、工单和部署历史。
2. 扩展了部署历史检索意图，并把 rubric 评分从脆弱的原词重合改为可审计的 OnCall 等价表达组；没有降低 ID recall、引用、事实错误或安全门禁。
3. 相关 RAG/RAGAS 测试为 `61 passed`。
4. 正式确定性运行使用 `product-offline`、12 case × 3 次，共 36 个 answer-run；产物为 `logs/ragas_stage3_core_deterministic_v3.json` 和 `.md`。
5. 运行结果为 `12/12`：ID context recall `1.00`、OnCall actionability `1.00`、引用存在/支持 `1.00`、拒答边界 `1.00`，事实错误率和严重幻觉率均为 `0`；ID precision `0.70` 继续作为 watch metric。
6. 36 次运行全部稳定通过，未发现同一 case 在三次运行中结果漂移。
7. 正式盲评模板为 `eval/ragas_stage3_core.review.json`，包含 36 个 answer-run、7 个 0-2 rubric 维度，且不泄露自动评分。
8. 真实人工 reviewer 尚未填写 decision、rubric_scores、factual_errors 和 notes。该步骤不能由 Codex 或 LLM Judge 冒充，因此阶段 3 暂不标记最终通过。
9. Full Judge 已完成兼容修复并真实调用，但 DashScope embedding 对 RAGAS ResponseRelevancy 请求返回 `400 input.contents`；该指标标记为外部兼容缺口，不折算为 0 分，也不覆盖确定性和人工证据。
10. 在人工评审完成前，可并行开始阶段 4 工程实现，但不能把阶段 3 或完整流水线描述为最终通过。
11. 阶段 4 已形成 48 条结构化 RCA case，并完成离线验收；产物为 `logs/stage4_agent_rca_summary.json`、`.md` 和 `logs/stage4_agent_rca_reports.db`。
12. 阶段 4 结果：48/48 通过；Top-1 Accuracy、Top-3 Recall、Macro Precision/Recall/F1、必要证据召回、结论证据支持、Replan F1、needs_human F1、降级成功、Trace 完整和报告结论一致率均为 `1.00`；工具选择 Precision `0.92`、Recall `1.00`、F1 `0.9583`，无效工具率 `0`，工具执行成功率 `0.9663`。
13. 阶段 4 数据覆盖 Redis、MySQL、Kubernetes、下游依赖、CPU、内存、磁盘、工具失败、证据冲突、危险操作和证据不足；保留了症状与根因分离 case、冲突证据 case 和主动 `needs_human` case。
14. 阶段 4 比例指标已保存 numerator、denominator、sample_count 和 Wilson 95% CI；当前结论仅限 `offline_fixture`，不能替代阶段 7 的 `controlled_fault` RCA 证据。
15. 受控实验脚本已改为显式使用原 `autooncall` Compose 项目的现有容器：`autooncall-redis`、`autooncall-mysql`、`autooncall-prometheus` 和 `autooncall-loki`；不再创建或依赖 `autooncall-full-*` 容器。
16. 原 Redis/MySQL 数据盘只读检查通过：Redis AOF 已启用、DBSIZE 为 `7`；MySQL `aiops_incident_evidence` 为 `2` 条、`aiops_history_tickets` 为 `3` 条。
17. 阶段 7 最小受控故障运行 `controlled-fault-stage7-original-20260712` 完成：Redis `2/2`、MySQL `2/2`、downstream HTTP `3/3`，共 `7/7` 注入与恢复通过，所有 case 均有 cleanup verification。
18. 实验后 Redis `maxclients` 已恢复为 `10000`，DBSIZE 仍为 `7`；MySQL 两张业务证据表计数仍为 `2/3`，Redis/MySQL 容器均为 healthy。
19. 原容器真实适配器验证通过：Prometheus、Loki、Redis INFO、MySQL、CMDB、部署历史和工单共 7 类 source 无缺失。
20. Redis 和 MySQL Agent 主链各运行 3 次，均为 `3/3` 通过且 business signature 稳定；结构化 Top-1 分别为 `redis_maxclients` 和 `mysql_slow_query`。
21. 新增同窗口编排器 `scripts/sandbox/controlled_fault_e2e.py`，在注入保持期间调用 Agent 主链，并把告警、诊断、first useful diagnosis、结构化 RCA、恢复和 cleanup 写入同一实验记录。
22. 正式端到端 run `controlled-fault-e2e-stage7-20260712` 为 `2/2`：Redis Top-1 `redis_maxclients`，MySQL Top-1 `mysql_slow_query`，diagnosis 和 cleanup 均通过。
23. Redis first useful diagnosis 为 `3373.00ms`，alert-to-diagnosis 为 `3473.68ms`，恢复为 `324.76ms`；MySQL分别为 `577.19ms`、`677.71ms`、`2603.11ms`。
24. 端到端实验后 Redis `maxclients=10000`、DBSIZE `7`、MySQL 业务证据表计数 `2/3`，两个容器均 healthy。
25. 上述时间仅为 `controlled_fault` 本地实验时间，不是 production MTTD/MTTR；样本仅 2 条，不展示 P95 或显著性结论。
26. 下列历史记录继续保留。

### 3.0.2.1 已执行完成

本轮已经完成并验证：

1. 阶段 2 企业排障核心检索验收完成。第三套冻结独立集的 9 条企业排障正例全部通过；Recall@3 `1.00`、MRR `0.94`、nDCG@3 `0.96`、引用覆盖 `1.00`。
2. 阶段 3 核心数据集已从旧的 30 条混合集合收缩为 12 条企业排障核心 case，满足计划要求的 10-15 条规模。
3. 阶段 3 确定性评测完成：12 case × 3 次，共 36 个 answer-run，全部稳定通过。
4. 阶段 3 自动指标完成：ID context recall、OnCall actionability、引用存在、引用支持和拒答边界均为 `1.00`；事实错误率和严重幻觉率均为 `0`。
5. RAGAS `0.4.3` 指标导入兼容已修复；部署历史检索和 OnCall rubric 等价表达评分已补齐。
6. 相关回归测试为 `61 passed`。
7. 已生成正式盲评模板 `eval/ragas_stage3_core.review.json`，自动结果未写入盲评条目。
8. 阶段 4 结构化 RCA 数据集已扩展为 48 条 answer-run case，覆盖 8 个聚合根因类别及工具失败、冲突证据和证据不足场景。
9. 阶段 4 evaluator 已落地 Top-1/Top-3、Macro-F1、混淆矩阵、工具 Precision/Recall/F1、无效工具率、必要证据、结论支持、Replan、needs_human、Trace 和报告一致性指标。
10. 阶段 4 目标测试为 `58 passed`；补充审计指标后专项测试为 `9 passed`，Ruff 检查通过。
11. 阶段 4 正式离线运行 48/48 通过，工具选择 F1 `0.9583`，其余核心 RCA 比例指标均为 `1.00`。
12. 阶段 7 受控实验目标已切换到用户原有 `autooncall` 容器，新增选择性执行参数，避免为了最小实验启动或重建整套 Compose。
13. 阶段 7 runner 和安全边界测试为 `10 passed`，Ruff 通过；20 条 dry-run 全部为 `not_run`，证明默认不注入。
14. 阶段 7 正式最小实验为 `7/7`：Redis 2、MySQL 2、downstream HTTP 3，全部完成恢复验证且没有业务数据计数变化。
15. 阶段 7 真实 adapter verification 通过，Redis/MySQL Agent 主链各 `3/3` 稳定通过。
16. 阶段 7 同窗口端到端受控实验 `2/2` 通过，完成注入、告警、诊断开始、first useful diagnosis、Top-1 RCA 和恢复闭环。
17. 阶段 7 相关回归为 `60 passed`，Ruff 通过。

本轮主要产物：

- `eval/ragas_stage3_core_cases.yaml`
- `eval/ragas_stage3_core.review.json`
- `logs/ragas_stage3_core_deterministic_v3.json`
- `logs/ragas_stage3_core_deterministic_v3.md`
- `logs/rag_stage2_enterprise_holdout_20260712_c_first.json`
- `logs/rag_stage2_enterprise_holdout_20260712_c_first.md`
- `logs/stage4_agent_rca_summary.json`
- `logs/stage4_agent_rca_summary.md`
- `logs/stage4_agent_rca_reports.db`
- `logs/controlled_fault/controlled-fault-stage7-original-20260712/summary.json`
- `logs/controlled_fault/controlled-fault-stage7-original-20260712/cases/`
- `logs/stage7_original_adapter_verification.json`
- `logs/stage7_redis_mainline_stability.json`
- `logs/stage7_mysql_mainline_stability.json`
- `logs/controlled_fault/controlled-fault-e2e-stage7-20260712/summary.json`
- `logs/controlled_fault/controlled-fault-e2e-stage7-20260712/cases/cf-e2e-redis-01.json`
- `logs/controlled_fault/controlled-fault-e2e-stage7-20260712/cases/cf-e2e-mysql-01.json`

### 3.0.2.2 下一步执行

后续严格按以下顺序继续：

1. **阶段 3 人工收尾**：由真实 reviewer 填写 `eval/ragas_stage3_core.review.json`。至少评审 10-15 条唯一核心 case；每条填写 `decision`、7 个 `rubric_scores`、`factual_errors`、`severe_hallucination` 和 `notes`。
2. **导入并汇总人工结果**：使用现有 `eval_ragas_cases.py --human-review` 重跑汇总，输出人工通过率、自动/人工一致率和失败 case。只有一位 reviewer 时不计算 Cohen's Kappa。
3. **阶段 3 最终判定**：人工评审完成且没有严重事实错误或严重幻觉后，将阶段 3 标记为通过；DashScope ResponseRelevancy 的 `400 input.contents` 继续作为外部兼容缺口，不伪造分数。
4. **阶段 8 交付入口**：把阶段 4 最新 run、阶段 7 三组证据、混淆矩阵、失败/降级 case 和证据等级接入现有 scorecard/read model，不新建第二套前端。
5. **阶段 6 收缩验收**：补齐真实模型可用范围内的 RAG/AIOps 延迟与 token 分布；外部模型不可用时准确标记缺口。
6. **最终发布收尾**：阶段 3 人工评审、阶段 8 和阶段 6 完成后，再执行完整 gate 与 clean official baseline。

当前可直接继续的工程停点是：**阶段 8 统一 scorecard 与面试交付入口**。阶段 3 的剩余阻塞仍仅为真实人工盲评；阶段 7 已通过最小可信受控故障验收。

历史停点：

1. 开发/回归集已达到 `80/80`。产物为 `logs/rag_stage2_dev_final_v5.json` 和 `.md`。
2. 开发集两条 Kubernetes variant 原标注错误地把 `official_kubernetes_debug_pods.md#0006` 当作 Pod triage；实际内容是 Service EndpointSlice/backend 检查。已按文档原文修正为 `#0002`，不是放宽指标。
3. 新冻结未见集为 `eval/rag_holdout_20260712.yaml`，SHA256 `a6dd026b99a9f3065b5fc220ec6212f1ee2f4931319eb0be583df2b6274204e6`，大小 10693 bytes，共 30 case，其中 25 条正例、5 条拒答。
4. 新 holdout 在记录哈希后只运行一次，首跑产物为 `logs/rag_stage2_holdout_20260712_first.json` 和 `.md`。
5. 首跑结果为 `21/30`：Recall@3 `0.80`、MRR `0.67`、nDCG@3 `0.67`、引用覆盖 `0.84`；5 条拒答中 2 条通过，拒答 Recall `0.40`。
6. 失败 case 为 `fresh_latency_01`、`fresh_prometheus_02`、`fresh_table_01`、`fresh_multi_redis_01`、`fresh_multi_observe_01`、`fresh_alias_03`、`fresh_refusal_01`、`fresh_refusal_03`、`fresh_refusal_04`。
7. 失败集中在三类通用能力：同义表达泛化、复杂多来源精确 chunk 覆盖、越界查询误检。该 holdout 现已封存为历史验收证据，不允许根据其结果继续调参。
8. 后续只能把失败类型抽象为新的开发反例；实现改进后必须创建另一套未见 query 和标签，并在运行前冻结哈希，再做一次性验收。
9. 阶段 2 仍未通过，因此不进入阶段 3 正式验收。当前可以并行审计后续工程骨架，但不能把后续结果包装为完整流水线已通过。
10. 曾为封存 holdout 的消费领域负例增加过专项关键词规则。经业务主线审计，该方向不属于企业排障核心能力，后续应删除对应专项规则和测试；历史 holdout 与首跑结果继续保留，不修改历史证据。
11. PromQL 持续时间/通知标签、用户可见症状告警等企业可观测性同义表达改进可以保留；`59 passed`，80 条开发/回归集仍为 `80/80`。
12. `logs/rag_stage2_dev_generalization_v1.json` 和 `.md` 保留为历史开发回归产物，但不把其中消费领域拒答规则描述为项目能力。下一步不是继续堆叠 holdout，而是按收缩后的验收标准完成企业排障独立集评估。

上一轮停点与历史过程保留如下：

1. 重复 chunk 不再重复贡献 Recall、MAP 和 nDCG；新增边界测试证明所有 IR 指标保持在 `[0, 1]`。
2. 显式 chunk 标注 case 必须在 Top-K 完整召回；多来源门禁要求每个 required source 命中已标注相关 chunk；引用覆盖必须落在相关结果上。
3. 原 30 条 holdout 已因参与调参而降级为 `regression`，不能继续作为独立验收集。当前 evaluator 重放的 80 条开发/回归集为 75/80；第三轮复评产物保留在 `logs/rag_stage2_regression_replay_v2.json` 和 `.md`。
4. 新建 `eval/rag_holdout_cases.yaml`，包含 24 条正例和 6 条拒答，共 30 条独立 query；在首次检索运行前冻结标签，首跑后不再据结果调参。
5. 冻结 holdout 首跑为 26/30：Recall@3 `0.8750`、Precision@3 `0.2916`、MRR/MAP@3 `0.8125`、nDCG@3 `0.8289`、拒答 F1 `0.9091`。失败为 `holdout_redis_01`、`holdout_redis_02`、`holdout_loki_02`、`holdout_refusal_01`。
6. lexical-only 使用纯 IDF 词项重合，vector-only 使用 term-vector surrogate，RRF 仅融合这两个纯基线排名；产品 heading intent、偏好降阈值、来源多样化、confusion suppression 和越界拒答只用于 weighted。
7. 相关性标签现在将标准 `<source>#<chunk>` ID 绑定到来源；同一 chunk_id 出现在其他来源时不会计入 Recall、MAP 或 nDCG。
8. holdout 首跑早于最终 evaluator，原始 `rag_stage2_holdout_first.*` 保留不覆盖；当前 evaluator 的重放写入 `rag_stage2_holdout_replay.*`，结果为 25/30，新增暴露 `holdout_k8s_03`，其余失败仍为 Redis、Loki 和 1 条拒答。
9. 当前 replay 产物记录独立 dataset SHA256、文件大小、修改时间和 case 数；首次产物没有该字段，其冻结证据仍以创建时间和未修改状态为准。
10. 第三轮 evaluator 实现复评为 `9.1/10`，但阶段质量验收仍失败。当前不能声称 80/80 或 holdout 全通过，也不能进入阶段 3 正式验收；现有冻结 holdout 只作历史证据。
11. 本轮新增两个不依赖冻结 holdout 的通用反例：Loki 查询超时必须优先 READ/Timeout chunk，服务 Runbook query 必须允许来源内“排查步骤”chunk 通过准入门槛。
12. 修复了两个通用实现缺口：Loki 只在明确写入意图时优先 ingestion，明确查询意图时优先 query；weighted heading intent 命中的 chunk 可使用 weighted lexical 通过准入，而 lexical/vector/RRF baseline 仍使用各自纯基线口径。
13. 开发/回归集最新运行提升到 `78/80`，原失败中的 `service_common_04`、`service_common_05`、`loki_confusion_01` 已通过；剩余 `k8s_multi_01` 与 `observability_multi_04`，均为 required source 已部分命中但显式相关 chunk 未在 Top-3 完整召回。
14. 冻结 holdout 本轮没有重跑、查看或用于调参。下一步先在开发数据上解决两类通用多来源 chunk 选择，再创建一套新的未见 holdout 做一次性验收。

### 3.0.3 第三轮结果快照

- 开发/回归 replay：`2026-07-12T04:54:27.410671+00:00`，`75/80` 通过。Recall@3 `0.9297`、Precision@3 `0.3854`、MRR `0.8307`、MAP@3 `0.8216`、nDCG@3 `0.8328`、引用覆盖 `0.9375`、严格多来源@3 `0.8750`、拒答 F1 `1.0000`。
- 开发/回归数据集：`eval/rag_relevance_cases.yaml`，SHA256 `3b4dde7b1ee76a7bde43fa5da6ed734ca5a4224c92eb5d952f8dce1a173155d3`，80 条 case。
- 开发/回归失败 case：`service_common_04`、`service_common_05`、`loki_confusion_01`、`k8s_multi_01`、`observability_multi_04`。归因分别覆盖服务不可用 chunk 排名、多来源完整召回，以及 Loki read/write 语义混淆；不得通过放宽 chunk 标签、引用门禁或指标口径消除。
- 冻结 holdout replay：`2026-07-12T04:20:26.523068+00:00`，`25/30` 通过。Recall@3 `0.8333`、Precision@3 `0.2777`、MRR/MAP@3 `0.7917`、nDCG@3 `0.8026`、引用覆盖 `0.8333`、严格多来源@3 `0.0000`、拒答 F1 `0.9091`。
- 冻结 holdout 数据集：`eval/rag_holdout_cases.yaml`，SHA256 `c80b300498b43fd24c2ba7bf5541d31be8008578b3a1b3195443d95e27dab78b`，30 条 case。
- 冻结 holdout replay 失败 case：`holdout_redis_01`、`holdout_redis_02`、`holdout_k8s_03`、`holdout_loki_02`、`holdout_refusal_01`。这组结果仅保留为历史验收证据，不允许继续用于调参。
- 评测产物：`logs/rag_stage2_regression_replay_v2.json`、`logs/rag_stage2_regression_replay_v2.md`、`logs/rag_stage2_holdout_replay.json`、`logs/rag_stage2_holdout_replay.md`。

### 3.0.4 本轮验证记录

```powershell
.venv\Scripts\python.exe -m pytest tests/test_rag_eval_cases.py tests/test_ragas_eval_cases.py -q
# 第三轮阶段 2/3 evaluator 专项测试：36 passed

.venv\Scripts\python.exe scripts/eval/eval_rag_cases.py --cases eval/rag_relevance_cases.yaml --docs-dir docs/knowledge-base --summary-json logs/rag_stage2_regression_replay_v2.json --summary-md logs/rag_stage2_regression_replay_v2.md
# 当前 evaluator 重放 75/80；退出码 1 正确表示质量门禁失败

.venv\Scripts\python.exe scripts/eval/eval_rag_cases.py --cases eval/rag_holdout_cases.yaml --docs-dir docs/knowledge-base
# 冻结 holdout 首跑 26/30；当前 evaluator 重放 25/30；均保留且不据此调参

.venv\Scripts\python.exe scripts/eval/eval_change_cases.py --cases eval/change_cases.yaml
# 43/43 passed；阶段 5 离线验收通过

.venv\Scripts\ruff.exe check scripts/eval/eval_rag_cases.py scripts/eval/eval_ragas_cases.py tests/test_rag_eval_cases.py tests/test_ragas_eval_cases.py
# All checks passed
```

### 3.0.5 通用检索改进结果快照

- 开发/回归运行：`2026-07-12T05:11:05.186854+00:00`，`78/80` 通过。Recall@3 `0.9766`、Precision@3 `0.4010`、MRR `0.8620`、MAP@3 `0.8529`、nDCG@3 `0.8681`、引用覆盖 `0.9844`、严格多来源@3 `0.8750`、拒答 F1 `1.0000`。
- 数据集保持不变：`eval/rag_relevance_cases.yaml`，SHA256 `3b4dde7b1ee76a7bde43fa5da6ed734ca5a4224c92eb5d952f8dce1a173155d3`，80 条 case。
- 已修复失败：`service_common_04`、`service_common_05`、`loki_confusion_01`。
- 剩余失败：`k8s_multi_01` 缺少 `official_kubernetes_debug_pods.md#0002`；`observability_multi_04` 缺少 `official_loki_troubleshoot_ingest.md#0003` 与 `official_prometheus_alerting_practices.md#0003` 的完整 Top-3 覆盖。
- 新产物：`logs/rag_stage2_dev_improvement_v1.json`、`logs/rag_stage2_dev_improvement_v1.md`。退出码 1 正确表示阶段质量门禁仍未通过。
- 专项测试：`tests/test_rag_eval_cases.py`、`tests/test_ragas_eval_cases.py`、`tests/test_rag_retrieval_service.py` 共 `55 passed`；Ruff 通过。
- 本快照仍属于 `offline_fixture` 开发/回归证据，不是新 holdout 验收，不得描述为阶段 2 已完成。

本轮为核验状态临时生成的 `logs/tmp_plan_status_*.json/md` 不是正式 benchmark 产物，不得用于简历或 official baseline。

### 3.1 已完成

阶段 0 和阶段 1 的核心工程能力已经实现：

- 已建立统一 benchmark provenance。
- 已记录 Git、dirty 状态、worktree hash、Python、依赖、`uv.lock`、机器、模型、Prompt、配置和知识资产 hash。
- 已支持四种证据等级。
- 已使用独立 `run_id` 保存历史 benchmark。
- dirty worktree 不会被标记为 official baseline。
- 已为比例指标提供 numerator、denominator、sample count 和 Wilson 95% CI。
- 已实现 20 份知识资产的真实解析、切分、元数据、重复率、新鲜度和 Milvus CRUD 检查。
- 已实现一个命令运行完整本地基线。

当前相关入口：

```powershell
make knowledge-quality
make benchmark-baseline
```

主要实现：

- `scripts/eval/benchmark_metrics.py`
- `scripts/eval/eval_environment.py`
- `scripts/eval/eval_knowledge_quality.py`
- `scripts/eval/run_benchmark_baseline.py`

### 3.2 当前真实结果

20 份知识资产组成：

| 类型 | 数量 |
| --- | ---: |
| Markdown | 14 |
| PDF | 2 |
| HTML | 2 |
| CSV | 1 |
| XLSX | 1 |

当前知识质量结果：

| 指标 | 结果 |
| --- | ---: |
| 文档解析成功 | 20/20 |
| 文档切分成功 | 20/20 |
| Index-ready | 20/20 |
| Chunk 数 | 209 |
| 平均长度 | 902.34 字符 |
| P50 长度 | 762 字符 |
| P95 长度 | 1596 字符 |
| 空 Chunk | 0/209 |
| 超长 Chunk | 4/209 |
| 元数据缺失 | 0/209 |
| 完全重复 Chunk | 0/209 |
| 近似重复 Chunk | 8/209 |
| 陈旧文档 | 0/20 |
| Milvus 写入/读取/删除 | 209/209/1，验证通过 |

最近完整 benchmark 为 7/7 模块通过，但状态是 `candidate_dirty_worktree`，不是 official baseline。当前唯一 official 阻塞原因是工作区存在未提交修改。

### 3.3 阶段 0/1 尚需收尾

下面内容并非重新建设阶段 0/1，而是进入后续阶段前必须完成的收尾：

1. 审核当前大量修改、删除和新增文件，确认哪些属于用户正在整理的版本。
2. 不自动恢复用户删除的旧文档，也不把无关修改混入 benchmark 工作。
3. 修复当前发现的 4 个超长 chunk，或为其提供经过验证的保留理由。
4. 审核 8 个近似重复 chunk，区分合理的共享背景与应去重内容。
5. 更新知识质量 hard gate：解析、切分、元数据和 Milvus 一致性必须通过；重复率和超长率先作为 watch metric，不能为了全绿破坏知识完整性。
6. 在代码版本整理完成后运行一次 clean-worktree benchmark，生成 official baseline。未明确授权时不自动提交代码，因此 official baseline 可以作为最终发布动作。

## 4. 调整后的实施路线

原计划的方向保留，但为了秋招价值和避免过度建设，调整为 8 个工程阶段和 1 个长期数据轨道：

| 顺序 | 阶段 | 状态 | 秋招价值 |
| --- | --- | --- | --- |
| 0 | 可信基线 | 核心完成，待 clean baseline | 证明数据可追溯 |
| 1 | 知识质量 | 已完成，watch metric 持续观察 | 展示真实 Milvus 和数据治理 |
| 2 | RAG 检索评测升级 | 企业排障核心验收通过，通用拒答保留 watch | 从简单命中升级为标准 IR 指标 |
| 3 | 回答质量与人工评审 | 自动验收完成，缺真实人工盲评 | 证明答案可信、可执行、有引用 |
| 4 | Agent RCA 评测 | 离线结构化验收完成，48/48 通过 | 项目最核心的技术亮点 |
| 5 | 安全与对抗评测 | 离线阶段完成，43/43 通过 | 证明 Agent 不会越权操作 |
| 6 | 延迟、Token、成本和并发 | evaluator 和本地 smoke 完成，真实模型验收未完成 | 回答工程性能与成本问题 |
| 7 | 受控真实故障实验 | 最小可信主链完成，端到端 2/2 通过 | 最强的真实数据证据 |
| 8 | 面试交付与最终门禁 | 待实施 | 形成可演示、可复跑的作品 |
| 长期 | 生产运营指标 | 只建设采集能力 | 不伪造 MTTD/MTTR |

阶段 3 与阶段 4 不再追求一开始就做 300 个样本。秋招版本优先做规模适中、标签严格、能解释失败的高质量数据集。规模可以在工程稳定后继续扩充。

## 5. 全局数据和产物约束

### 5.1 唯一历史目录

所有 benchmark 结果统一写入：

```text
logs/benchmarks/<run_id>/
```

每个 run 最多包含：

```text
baseline_manifest.json
baseline_manifest.md
knowledge_quality.json
knowledge_quality.md
rag_retrieval.json
rag_retrieval.md
answer_quality.json
answer_quality.md
agent_rca.json
agent_rca.md
security_eval.json
security_eval.md
performance.json
performance.md
load_test.json
load_test.md
controlled_faults.json
controlled_faults.md
interview_scorecard.json
interview_scorecard.md
```

不是每次运行都必须生成所有模块。缺失模块必须在 manifest 中显示 `missing`，不能拿旧文件冒充本次结果。

### 5.2 指标统一结构

比例类指标统一保存：

```json
{
  "key": "recall_at_3",
  "value": 0.85,
  "numerator": 68,
  "denominator": 80,
  "sample_count": 80,
  "confidence_interval": {
    "method": "wilson",
    "confidence": 0.95,
    "lower": 0.76,
    "upper": 0.91
  },
  "source": "cases[].metrics.recall_at_3",
  "evidence_level": "offline_fixture"
}
```

连续值统一保存 count、min、max、mean、median、P50、P95、P99；重复实验还应保存标准差。

### 5.3 数据集版本

每个 eval 数据集记录：

- 数据集 hash 和版本。
- case 数量。
- 类别、难度、正负例和证据类型分布。
- 开发集与 holdout 集划分。
- 标注规则版本。
- 最近审核时间。

开发集可以用于调参，holdout 集只能用于验收和最终展示。

## 6. 阶段 2：RAG 检索评测升级

### 6.1 目标

把当前“期望文件是否进入 Top-K”的简单评测升级为标准信息检索评测，并使用当前 20 份、209 个 chunk 的真实知识资产。

### 6.2 数据集设计

新建一个检索相关性数据集，优先扩展现有 `eval/rag_cases.yaml`，只有现有结构无法兼容时才新建 `eval/rag_relevance_cases.yaml`。

秋招第一版目标为 80 条 query：

| 类别 | 最少数量 |
| --- | ---: |
| 常规正例 | 24 |
| 相似故障混淆 | 16 |
| 多来源综合 | 16 |
| 越界/无答案 | 16 |
| 别名、缩写、表述变化 | 8 |

覆盖 Redis、MySQL、Kubernetes、Prometheus、Loki、CPU、内存、磁盘、服务不可用和慢响应。

每个正例标注：

- `relevant_chunks`：相关 chunk ID 列表。
- `relevance_grade`：0～3 级相关性。
- `required_sources`：必须覆盖的文件。
- `acceptable_sources`：允许的补充文件。
- `forbidden_sources`：明显误导的来源。
- `difficulty`：easy、medium、hard。
- `category` 和 `doc_types`。

划分：

- 50 条开发集。
- 30 条 holdout 集。
- holdout 只用于最终验收，不根据结果逐 case 调参。

### 6.3 实现任务

1. 扩展 RAG case schema 和校验器。
2. 在检索结果中稳定暴露 chunk ID、source、rank、原始分数、融合分数和检索策略。
3. 实现 Recall@1/3/5。
4. 实现 Precision@1/3/5。
5. 实现 MRR、MAP@K、nDCG@K。
6. 实现严格多来源召回率。
7. 对无答案 case 计算拒答 Precision、Recall、F1，而不是只有拒答命中率。
8. 记录检索 P50/P95/P99。
9. 分 category、difficulty、doc type 输出切片指标。
10. 比较纯词法、纯向量、weighted hybrid 和 RRF；不得为每种策略复制一套 evaluator。
11. 对每个失败 case 保存期望相关 chunk、实际排名和失败原因。
12. 为关键比例指标增加 bootstrap 或 Wilson 95% CI。

### 6.4 主要修改边界

优先修改：

- `eval/rag_cases.yaml`
- `scripts/eval/eval_rag_cases.py`
- `app/services/rag_retrieval_service.py`
- `tests/test_rag_eval_cases.py`
- `tests/test_rag_retrieval_service.py`
- `scripts/eval/run_benchmark_baseline.py`

不要另建第二个 RAG 服务或新检索框架。

### 6.5 验收标准

- 至少 80 条已校验的企业排障开发/回归 case。
- 正例有 chunk 级相关性标注。
- Recall、Precision、MRR、MAP、nDCG 和拒答 F1 可复现。
- weighted、RRF、vector-only、lexical-only 使用相同数据集比较。
- 每个失败 case 有排名明细。
- holdout 结果和开发集结果分开。
- 独立集以企业排障、证据混淆和跨来源取证为主，Recall@3 建议达到 `>= 0.85`；未达到时保留失败分类，不通过反复造集或添加业务无关关键词追求 100%。
- 域外拒答只保留少量 sanity case，不作为阶段主优化目标。
- 完整测试和 benchmark 通过。

### 6.6 面试输出

面试时展示：

- 当前最佳策略和对照策略的指标差异。
- 一个多来源成功 case。
- 一个相似故障误召回 case。
- 为什么 Precision 和 Recall 不能只看一个。

## 7. 阶段 3：回答质量与人工评审

### 7.1 目标

证明最终回答不仅召回了文档，而且事实受上下文支持、引用可核验、建议可执行，并且不会把静态 Runbook 当成实时故障证据。

### 7.2 样本和重复次数

保留从阶段 2 数据集中选出的 30 个确定性核心 case：

- 18 个正例。
- 6 个混淆或多来源 case。
- 6 个无答案或越界 case。

每个模型/Prompt 配置重复 3 次，共至少 90 个确定性回答样本。正式人工验收只选择其中 10-15 条最能代表企业排障主线的 case，结果必须按 run 保存，不能只留平均分。

### 7.3 三层评测

确定性规则：

- 引用是否包含 `source_file` 和 `chunk_id`。
- 引用是否真实存在。
- 引用内容是否支持对应结论。
- 是否保留审批、dry-run 和人工接管边界。
- 无来源时是否拒答。
- 是否把静态知识描述为当前实时指标。

LLM Judge：

- Faithfulness。
- Response Relevancy。
- OnCall Actionability。
- Answer Completeness。

人工评审：

- 使用固定 rubric 审核 10-15 个核心企业排障回答。
- 评审时隐藏自动分数，避免被 Judge 结果影响。
- 每项使用 0～2 或 0～3 的清晰等级。
- 若只有一名评审者，不计算或宣称 inter-rater agreement。
- 若后续有第二名评审者，再增加 Cohen's Kappa 或一致率。

### 7.4 实现任务

1. 保留 `id-smoke`，但明确它只做确定性回归。
2. 完善 `full` profile，记录 Judge 模型、Prompt、温度、token 和异常。
3. 增加人工评审模板和导入命令，不创建独立审核网站。
4. 实现引用支持率和引用正确率。
5. 实现事实错误率和严重幻觉率。
6. 实现运行间稳定性：均值、标准差、最差一次、全通过率。
7. 比较最多两个有实际意义的模型或 Prompt 配置，避免无目的模型榜单。
8. 将 full judge 结果纳入 benchmark，但没有 Judge key 时必须显示 `not_run`。

### 7.5 主要修改边界

- `scripts/eval/eval_ragas_cases.py`
- `eval/ragas_cases.review.json`
- `app/services/rag_agent_service.py`
- `app/services/rag_answer_policy.py`
- `tests/test_ragas_eval_cases.py`
- `scripts/eval/run_benchmark_baseline.py`

人工评审数据应存放在 `eval/`，不能存入 `logs/` 后丢失。

### 7.6 验收标准

- 30 个确定性 case、每个至少 3 次运行。
- `id-smoke` 与 full judge 严格分开。
- 10-15 个核心企业排障回答有人工 rubric 结果。
- 自动 Judge 与人工结果可对照。
- 所有严重幻觉 case 可定位到回答、上下文和 Prompt 版本。
- 没有 Judge key 时仍能完成确定性部分，并准确标记缺失项。
- 只有存在至少两名真实独立评审者时才计算 Cohen's Kappa。
- Full Judge 不稳定或与人工判断明显冲突时标记为 `judge_unreliable`，不得通过反复调 Prompt 伪造全绿结果。

### 7.7 面试输出

- 一张检索质量与回答质量分离的对照表。
- 一个“召回正确但回答错误”的 case。
- 一个正确拒答 case。
- 对 Judge 局限性的主动说明。

## 8. 阶段 4：Agent RCA 诊断评测

### 8.1 目标

将当前根因关键词命中升级为结构化 Top-1/Top-3 RCA、工具选择和证据支持评测。这是整个项目最重要的秋招阶段。

### 8.2 结构化 RCA 合同

统一 RCA 标签示例：

```text
redis_maxclients
redis_slow_command
mysql_slow_query
mysql_connection_pool_exhaustion
k8s_oomkilled
k8s_crashloop
dependency_timeout
cpu_hot_loop
memory_leak
disk_capacity
configuration_error
observability_source_failure
unknown_needs_human
```

Agent 输出必须包含结构化候选：

```json
{
  "rank": 1,
  "category": "redis_maxclients",
  "confidence": 0.82,
  "evidence_ids": ["ev-1", "ev-3"],
  "supporting_facts": [],
  "contradictions": [],
  "missing_evidence": []
}
```

Top-3 必须来自真实候选假设，不允许从最终报告文本临时抽取。

### 8.3 数据集

秋招第一版至少 48 个 case，每个主要类别 4～6 个：

- 简单单根因。
- 症状与根因分离。
- 两个相似候选。
- 证据冲突。
- 关键工具失败。
- 证据不足，需要 `needs_human`。
- 过期证据。
- 错误告警或噪声告警。

输入不得直接包含标准答案，例如不能把 `maxclients exhausted` 同时作为症状和根因标签提示。

每个 case 标注：

- Top-1 标准根因。
- 可接受的 Top-3 候选。
- 必要证据和可选证据。
- 必要工具、允许工具和禁止工具。
- 是否应 Replan。
- 是否应 `needs_human`。
- 期望报告状态。

### 8.4 指标

- Top-1 RCA Accuracy。
- Top-3 RCA Recall。
- 根因类别 Macro Precision/Recall/F1。
- 混淆矩阵。
- 工具选择 Precision/Recall/F1。
- 无效工具率。
- 工具执行成功率。
- 必要证据召回率。
- 结论证据支持率。
- Replan 触发 Precision/Recall。
- Replan 成功率。
- `needs_human` Precision/Recall/F1。
- 降级成功率。
- Trace 完整率。
- 报告结论一致率。

### 8.5 实现任务

1. 扩展现有 Hypothesis/Report 模型，避免创建平行 RCA 模型。
2. 统一 planner、evidence analyzer、replanner 和 report 的 RCA category。
3. 将 eval 从关键词命中迁移到结构化标签；关键词只保留兼容和辅助诊断。
4. 增加 Top-3 候选和证据链接。
5. 实现工具集合 Precision/Recall，而不是只有“包含预期工具”。
6. 实现无效工具判定，并允许标准上下文工具。
7. 实现 Replan 和 `needs_human` 二分类指标。
8. 输出混淆矩阵和类别切片。
9. 保留所有失败 case 的 Trace、Evidence 和 Report。
10. 用阶段 7 的受控故障结果补充 `controlled_fault` RCA 证据。

### 8.6 主要修改边界

- `app/models/hypothesis.py`
- `app/models/report.py`
- `app/agent/aiops/evidence_analyzer.py`
- `app/agent/aiops/replanner.py`
- `app/services/report_generator.py`
- `scripts/eval/eval_cases.py`
- `eval/cases.yaml`
- 对应 tests

### 8.7 验收标准

- 至少 48 个结构化 RCA case。
- 不再以关键词命中作为主要 RCA 指标。
- Top-1、Top-3、Macro-F1、工具 F1、Replan F1 和 needs-human F1 可复现。
- 每个预测都能链接证据。
- 每个错误分类都出现在混淆矩阵和失败列表。
- 至少一个 case 能证明系统在证据不足时拒绝强行给出 completed RCA。

### 8.8 面试输出

- Top-1/Top-3 RCA 和 Macro-F1。
- 一张根因混淆矩阵。
- 一个症状误导但最终定位正确的 case。
- 一个主动降级到 `needs_human` 的 case。

## 9. 阶段 5：安全与对抗评测

### 9.1 目标

量化系统是否能拦截危险动作，同时避免把正常只读诊断全部误拦。

### 9.2 数据集

至少 40 个 case：

| 类型 | 数量 |
| --- | ---: |
| 应拦截危险动作 | 15 |
| 应审批高风险动作 | 10 |
| 应允许只读或低风险动作 | 10 |
| Prompt/参数注入 | 5 |

覆盖：

- 未审计 SQL。
- 删除 Pod、重启数据库、危险 shell。
- 跨 Incident 审批 ID。
- 重复和并发审批。
- 绕过 dry-run。
- 缺少 rollback plan。
- Prompt injection 要求忽略策略。
- 工具参数中的命令或 SQL 注入。
- 敏感信息回显。

### 9.3 指标

- Forbidden action Recall/Precision/F1。
- Approval trigger Recall/Precision/F1。
- Safe action false block rate。
- Approval bypass rate。
- Unauthorized execution rate。
- Prompt injection success rate。
- Tool argument injection block rate。
- Sensitive data leakage rate。
- Rollback plan completeness。
- Rollback recommendation recall。
- Dry-run-before-execute rate。
- Concurrent approval consistency。

### 9.4 实现任务

1. 扩展现有 `eval/change_cases.yaml`，不新建平行安全引擎。
2. 同时测试正例和负例，防止“全部阻止”获得高分。
3. 为每次决策保存 policy、reason、matched rule 和执行边界。
4. 增加 Prompt injection 和参数注入 case。
5. 增加敏感信息检查，复用已有 redaction。
6. 增加并发审批和幂等测试。
7. 将安全指标加入统一 benchmark 和工作台现有评测区域。

### 9.5 验收标准

- 未授权写操作成功数为 0。
- Forbidden、approval 和 safe allow 都有 Precision/Recall。
- 安全动作误拦截率可见。
- 至少 40 个安全 case。
- 每个失败 case 可定位到规则和决策。
- 不自动执行生产变更。

### 9.6 面试输出

- 危险动作拦截率与安全动作误拦截率同时展示。
- 现场展示一次危险操作被拦截。
- 展示审批通过仍需 pre-check、dry-run 和人工记录。

## 10. 阶段 6：延迟、Token、成本和并发

### 10.1 目标

把当前 fixture 的毫秒耗时与真实模型、真实适配器和系统容量分开量化，回答面试中的性能与成本问题。

### 10.2 观测模型

复用 Trace 和 ToolCall，补齐：

- request start/end。
- 首 SSE 事件时间。
- 首 token 时间。
- retrieval、planner、executor、replanner、report 阶段时间。
- LLM provider、model、attempt、timeout、token usage。
- 每个工具 attempt 和 retry latency。
- 排队时间和并发槽等待时间。

### 10.3 延迟与成本实验

至少执行：

- 30 次真实 RAG 请求。
- 20 次真实 AIOps 诊断。
- 结果按 case 和阶段保留。

指标：

- First event latency。
- Time to first token。
- End-to-end P50/P95；只有样本量足够时才展示 P99。
- 分阶段 P50/P95。
- 工具 P50/P95。
- LLM 成功率、超时率和重试恢复率。
- Input/output/total tokens。
- 单次 RAG 成本。
- 单次成功诊断成本。
- 各故障类别平均成本。

价格不能硬编码在业务代码。保存带日期和来源说明的 price snapshot；价格未知时只展示 token，不虚构金额。

### 10.4 并发压测

只选择 Locust，避免同时维护 k6/JMeter。

分两类：

应用容量测试：

- 使用 deterministic/fake LLM，测 FastAPI、SSE、状态存储和读接口容量。
- 使用 1、5、10 的小型并发阶梯或等价 smoke，不追求 50/100 并发展示。
- 每档运行到获得足够请求样本即可，不硬性要求 3 分钟。

受限真实模型测试：

- 使用真实模型但限制并发和总请求，避免把 provider 限流误认为应用瓶颈。
- 默认只验证并发 1-2；只有预算和 provider 配额明确时才扩到 5。

场景：

- `/api/chat`。
- `/api/aiops` SSE。
- Incident/Trace/Report 读取。
- Alertmanager webhook 和 SQLite/MySQL 存储对比降为可选。

指标：

- RPS。
- P50/P95；只有样本量足够时才展示 P99。
- Error/timeout rate。
- SSE 活跃连接数。
- 排队时间。
- Incident 状态串扰率。
- 告警去重正确率。

### 10.5 实现任务

1. 扩展现有 Trace 模型，不创建第二套 tracing。
2. 统一 token usage 和成本计算。
3. 增加 performance evaluator 和 Markdown 汇总。
4. 增加一个 Locust 文件及场景配置。
5. 保存压测机器、时间、持续时长和服务配置。
6. 对已有 smoke 给出瓶颈解释，不强制计算“最大稳定并发”。
7. 资源采集作为辅助证据；不可用时不阻塞阶段验收。

### 10.6 验收标准

- fixture、local_live 和真实模型延迟分开。
- 至少 20 次真实 RAG 请求和 10 次真实 AIOps 请求形成延迟和 token 分布；外部模型不可用时准确标记缺口。
- 成本来源可追溯，未知价格不计算金额。
- Locust smoke 可以一条命令运行。
- 给出当前环境下的瓶颈解释，不声称生产容量或最大稳定并发。
- 没有状态串扰或未解释的数据写冲突。

### 10.7 面试输出

- 一张端到端耗时瀑布。
- 一张小型并发 smoke 与 P95/错误率结果。
- 单次 RAG 和诊断 token/成本。
- 清楚区分应用瓶颈和模型 provider 限流。

## 11. 阶段 7：受控真实故障实验

### 11.1 目标

使用真实本地 Redis、MySQL 和应用下游服务注入可恢复故障，生成 `controlled_fault` 级 RCA、延迟和恢复数据。Prometheus/Loki 证据后端故障为可选扩展。

### 11.2 秋招核心实验

核心只要求三条业务主链：

1. Redis maxclients/连接容量接近耗尽，完成 2-3 次。
2. MySQL 慢查询和连接池等待，完成 2-3 次。
3. 下游依赖延迟或 5xx，保留 3 次代表性实验。

Prometheus 或 Loki 暂时不可用导致证据降级，只在环境可直接完成时追加，不作为秋招工程完成的硬门槛。

Kubernetes OOM/CrashLoop 只有在有真实可控 K8s 环境时才升级为 controlled fault；否则继续标记为 offline fixture。

### 11.3 每次实验记录

- Experiment ID。
- 故障类型和注入参数。
- 注入开始和结束时间。
- 告警时间。
- 诊断开始时间。
- First useful diagnosis 时间。
- Top-1/Top-3 RCA。
- 工具和真实数据源。
- 证据完整性。
- 是否触发 Replan。
- 是否需要人工接管。
- 报告状态。
- 恢复时间。
- 注入前后关键指标。
- 清理和恢复验证。

### 11.4 安全要求

- 只允许在明确的 sandbox/local 环境运行。
- 每个注入步骤必须有 pre-check、超时、自动清理和最终健康检查。
- 默认 dry-run 能展示计划但不注入。
- 生产环境字符串或未知目标必须拒绝执行。
- 不实现通用任意 shell 故障注入 API。

### 11.5 实现任务

1. 建立最小 controlled-fault experiment schema。
2. 复用已有 sandbox compose 和 adapter。
3. 为 Redis、MySQL、下游依赖实现限定参数的注入器和恢复器；证据后端注入器为可选。
4. 将实验标签直接作为独立 ground truth。
5. 运行最少 7 次有效实验并汇总 RCA、诊断时间和恢复结果；不得通过重复相同参数凑数量。
6. 将结果纳入 benchmark，但与 offline case 分开。
7. 保存至少一个失败或降级实验，不删除坏结果。

### 11.6 验收标准

- Redis、MySQL 各至少 2 次有效实验，下游依赖至少 3 次有效实验。
- 每次有效实验均有原始证据和恢复检查。
- 无实验残留导致后续环境异常。
- RCA 与延迟指标使用 `controlled_fault` 标签。
- 失败实验有明确原因。
- Redis/MySQL 主链可以现场复跑。

### 11.7 面试输出

- 现场触发 Redis 或 MySQL 故障。
- 展示 Redis/MySQL/下游依赖真实证据；Prometheus/Loki 降级证据可选。
- 展示 Agent Plan、并行取证、RCA 和审批边界。
- 展示有效实验成功率和诊断延迟，同时保留至少一个 blocked 或失败实验。

## 12. 阶段 8：面试交付与最终门禁

### 12.1 目标

把所有结果收敛到现有工作台和一个面试入口，避免创建第二套展示系统。

### 12.2 唯一展示入口

后端继续使用现有 `/api/eval/*` 和 read model。前端在现有评测/系统区域增加 benchmark 模块，不新建独立 SPA。

最终 scorecard 展示：

- Baseline provenance。
- 知识资产质量。
- RAG 检索。
- 回答质量。
- Agent RCA。
- 安全性。
- 延迟、token、成本。
- 并发容量。
- Controlled fault。
- Production 数据状态。

每个模块显示：

- evidence level。
- run ID。
- 样本量。
- 核心指标。
- CI。
- 状态。
- 失败 case 数。
- 原始产物路径。

### 12.3 面试材料

只生成以下长期维护材料：

1. `interview_scorecard.md/json`：统一指标入口。
2. 一份五分钟演示脚本。
3. 一份简历指标建议。
4. 一份能力边界说明。

不要再生成十几篇重复的技术文章。

### 12.4 五分钟演示顺序

1. 打开 scorecard，展示 run ID、代码版本和证据等级。
2. 展示 20 份资产和 Milvus 一致性。
3. 展示 RAG 策略消融和一个失败 case。
4. 触发 Redis 或 MySQL controlled fault。
5. 展示 Plan、并行取证、RCA、Trace 和报告。
6. 展示危险动作审批和 dry-run 边界。
7. 展示延迟、token、成本和并发拐点。
8. 主动说明 production MTTD/MTTR 尚未积累，避免过度包装。

### 12.5 最终质量门禁

最终命令应串联：

- format check。
- Ruff。
- mypy。
- security。
- pytest 和 coverage。
- API contract。
- knowledge quality。
- RAG retrieval。
- answer quality。
- Agent RCA。
- security eval。
- performance smoke。
- controlled-fault summary readiness。
- reference/hygiene check。
- benchmark manifest。

耗时较长的真实模型、压测和故障实验可以作为显式 full gate，不应让每次普通单测都重复执行。

### 12.6 验收标准

- 一个 scorecard 能读取最新 run。
- 不读取过期或其他 commit 的模块结果。
- 面试页面显示证据等级和样本量。
- 至少展示一个失败或降级 case。
- 五分钟脚本可以按顺序完成。
- 所有简历指标都能从 JSON 原始产物反查。
- README 只保留简洁入口，不堆放重复指标表。

## 13. 长期轨道：生产运营指标

生产运营指标不能通过开发脚本一次性“完成”。秋招工程完成标准只要求建立真实采集能力，不要求伪造生产样本。

### 13.1 需要建设的采集字段

- Incident alert time。
- Diagnosis start time。
- First useful diagnosis time。
- Resolve time。
- 人工确认根因。
- AI 根因是否正确。
- 建议是否接受。
- 建议是否执行。
- 执行后是否恢复。
- 人工修改报告内容。
- 人工估算节省时间。
- 严重误导标记。

### 13.2 可以计算的长期指标

- MTTD。
- Time to First Useful Diagnosis。
- MTTR。
- RCA 人工确认率。
- 建议接受率。
- 建议执行后恢复率。
- 人工修改率。
- needs-human 升级率。
- 节省排查时间。
- 错误建议率。
- 月度可用性。

### 13.3 秋招展示边界

没有真实生产样本时：

- 显示 `production: not_enough_data`。
- 展示采集字段和计算公式。
- 使用 controlled fault 数据说明工程能力。
- 不把 controlled fault MTTD/MTTR 称为生产 MTTD/MTTR。

## 14. 推荐执行批次

| 批次 | 内容 | 依赖 | 完成标志 |
| --- | --- | --- | --- |
| A | 阶段 0/1 收尾 | 当前代码和 20 份资产 | 质量问题已审计，candidate baseline 稳定 |
| B | 阶段 2 | Milvus、企业排障 RAG 标注 | 80 条开发集、合理独立集和标准 IR 指标 |
| C | 阶段 3 | 模型账号、人工 rubric | 90 个确定性回答运行和 10-15 条人工核心评审 |
| D | 阶段 4 | 结构化 RCA 标签 | 30-36 个高质量 RCA case 和混淆矩阵 |
| E | 阶段 5 | 风控和安全变更链 | 40 个正反安全 case |
| F | 阶段 6 | Trace usage、Locust | 真实延迟、token、超时重试和小型容量 smoke |
| G | 阶段 7 | 本地 sandbox | Redis/MySQL 各 2-3 次、下游依赖 3 次有效 controlled fault |
| H | 阶段 8 | 所有 benchmark 模块 | scorecard、演示脚本和最终门禁 |
| 长期 | 生产数据积累 | 真实使用者 | production 指标达到有效样本量 |

## 15. 每阶段执行模板

每开始一个阶段，执行者必须按以下顺序：

1. 审计当前实现和最新 run。
2. 明确本阶段数据集、指标定义和证据等级。
3. 先写或更新 evaluator 测试。
4. 实现最小必要模型和服务改动。
5. 运行目标测试。
6. 运行本阶段真实评测。
7. 检查失败 case，而不是只看 summary。
8. 修复实现或修正错误标注。
9. 运行相关回归和完整 benchmark。
10. 更新 scorecard/read model。
11. 记录面试可讲结果和不能声称的边界。

## 16. 秋招工程完成标准

满足以下条件时，可以说本文的工程实施已完成：

- 阶段 0/1 已收尾，并能生成可追溯 baseline。
- 阶段 2 的企业排障 RAG 检索指标完成，独立集达到合理门槛并保留失败分类。
- 阶段 3 的确定性、Judge 和 10-15 条人工核心评审完成；外部 Judge 不可用或不可靠时准确标记。
- 阶段 4 的 30-36 条结构化 Top-1/Top-3 RCA 和工具/证据指标完成。
- 阶段 5 的安全 Precision/Recall 和误拦截指标完成。
- 阶段 6 的真实延迟、token、超时重试与 Locust smoke 完成，不要求生产容量结论。
- 阶段 7 的 Redis/MySQL 最小可信实验和下游依赖实验完成并安全恢复。
- 阶段 8 的 scorecard、五分钟演示、简历指标和最终门禁完成。
- Production 指标采集能力完成，真实样本不足时显示 `not_enough_data`。
- 所有数字能追溯到 run ID 和原始 case。
- 没有把 offline 或 controlled fault 结果包装为 production 结果。

official baseline 还要求：

- 工作区干净。
- 所有 required module 通过。
- 所有 artifact 与当前 commit、Prompt、依赖、配置和知识库一致。

如果执行过程未被授权创建 commit，则先完成 candidate baseline，并把 clean official baseline 明确列为发布动作，而不是伪造为已完成。

## 17. 最终简历指标规则

只有满足以下条件的指标才能写入简历：

1. 有当前 run ID。
2. 有明确样本量。
3. 有 evidence level。
4. 有失败 case 明细。
5. 有可复现命令。
6. 未过期。

推荐表达：

```text
基于 20 份多格式知识资产和 N 条人工标注检索 query，构建可追溯 RAG/Agent
评测体系，量化 Recall@K、nDCG、Top-1/Top-3 RCA、工具选择 F1、P95 延迟与
单次 token 成本；通过本地 Redis/MySQL/Prometheus/Loki/Milvus 受控故障实验
验证真实证据链和审批边界。
```

禁止表达：

```text
生产故障定位准确率 100%。
平均诊断时间只有几十毫秒。
支持 100 并发。
显著降低生产 MTTR。
```

除非未来确实获得对应的 production 或正式压测数据。
