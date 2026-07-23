# AutoOnCall 当前问题与后续改进计划

## 1. 文档目的

本文档记录截至 **2026 年 7 月 23 日** AutoOnCall 的实际执行进度、当前剩余问题、后续修复方案和最终验收口径。

当前项目已经完成从“知识解析、检索和引用不稳定”到“检索、引用和边界基本稳定”的阶段性改进。下一阶段的主线不再是扩大知识库，而是提高真实运行时回答的聚焦度、证据忠实度和问题覆盖完整性。

本轮完整评测使用：

- 评测模式：`runtime`
- 回答来源：`runtime`
- 检索：Milvus
- 生成模型：`qwen-max`
- Judge 模型：`qwen-max`
- Embedding 模型：`text-embedding-v4`
- 评测配置：`full`
- 重复次数：`1`
- 数据集：`eval/ragas_stage3_core_cases.yaml`
- 案例数：12
- 执行时间：2026-07-22 15:04:43 至 15:08:31（Asia/Shanghai）

对应报告：

- `logs/ragas_full_runtime_core_20260722_rerun.json`
- `logs/ragas_full_runtime_core_20260722_rerun.md`
- `logs/ragas_full_runtime_core_20260722_rerun_failed.json`

## 2. 当前执行进度

### 2.1 总体进度判断

当前计划整体约完成 **65%**，已经进入“运行时回答质量专项优化”阶段。

| 工作阶段 | 当前状态 | 说明 |
| --- | --- | --- |
| 知识库解析与多格式 Loader | 已完成 | Markdown、PDF、HTML、XLSX 已能形成可索引内容 |
| 文档切分与稳定 chunk identity | 已完成 | 引用能够稳定落到 `source_file + chunk_id` |
| Milvus 索引与 runtime 检索 | 已完成 | runtime 使用 Milvus，未出现静默 lexical fallback |
| 引用生成与引用校验 | 已完成 | 核心 full Judge 引用存在、支持、正确率均为 `1.00` |
| 拒答与知识边界 | 已完成 | 核心拒答案例 `2/2`，`invalid_input=0` |
| 扩展 37 案例检索优化 | 进行中 | 从 `31/37` 提升到 `33/37`，Recall 提升到 `0.9062` |
| 回答聚焦与实体覆盖 | 进行中 | Answer Coverage 已接入，但仍偏向模板覆盖而非问题相关性 |
| Redis Capacity 专项 | 未完成 | 仍是核心案例中失败维度最多的案例 |
| MySQL 慢查询专项 | 未完成 | 关键指标和判别链路仍可能在生成阶段丢失 |
| Runtime full Judge 最终验收 | 未完成 | 最新结果仍为 `2/12` |
| 固定版本连续 3 次稳定性验证 | 未执行 | 需在单次 full Judge 达标后执行 |

### 2.2 已经落地的代码改进

当前代码已经实现以下能力：

1. **Answer Coverage Matrix**
   - 在生成前识别证据、原因判断、处置边界、告警设计和历史/实时边界等子目标。
   - 将子目标绑定到具体检索 chunk。
   - 对回答遗漏的子目标进行检测和有限重试。

2. **引用修复与必要来源覆盖**
   - 对缺少 claim-level 引用的回答尝试补齐引用。
   - 多来源问题缺少必要来源时触发重新生成。
   - 正常模型生成持续失败时，可使用已有证据构建抽取式引用回答。

3. **运行时引用身份恢复**
   - 对具有可信来源但缺少历史 chunk ID 的旧数据生成稳定兼容 ID。
   - PDF、HTML、XLSX 和新增 Runbook 已统一进入可引用上下文。

4. **检索意图与上下文选择**
   - 已增加 Redis、MySQL、依赖 503、网络分阶段超时、线程池耗尽、消息队列积压、TLS 等专项意图。
   - 支持必要来源约束、标题覆盖、同源选择和来源级向量探测。
   - runtime 默认使用 Milvus，避免把本地 lexical 结果误当作真实 runtime 证据。

5. **回答安全与主题检查**
   - 支持显式证据不足表达。
   - 对明显偏离问题故障域的回答拒绝输出。
   - 历史工单和事故复盘不能直接表述为当前 incident-window 事实。

6. **评测口径改进**
   - Judge 指标区分 `available`、`not_run`、`unavailable` 和 `invalid_input`。
   - 指标平均值只使用真实可用值，不再把缺失结果折算成 0。
   - 报告记录测试集 hash、Prompt hash、知识资产 hash、运行模式和评测 profile。

7. **运行时可观测性**
   - 已记录检索后端、Milvus 查询耗时、检索命中及生成修复信息。

### 2.3 已完成的验证

- 与 RAG 回答、引用、检索、评测相关的针对性 pytest 已通过。
- Ruff 检查已通过。
- Milvus、真实生成模型和 Judge API 已成功完成一次完整 runtime full Judge。
- 37 案例 runtime id-smoke 已从：

| 运行 | 通过数 | ID Precision | ID Recall |
| --- | ---: | ---: | ---: |
| 改进前 | `31/37` | `0.7500` | `0.8125` |
| 当前最好结果 | `33/37` | `0.8281` | `0.9062` |

扩展集 Recall 已达到目标，但 Precision 和案例全通过仍未达标。

## 3. 当前总体结论

当前系统的**知识库检索、引用链路、拒答边界和基础 OnCall 可执行性已经基本稳定**，但真实运行时回答质量仍未达到验收标准。

当前主要矛盾不是“找不到知识”或“没有引用”，而是：

1. 生成回答过于模板化，加入了与问题无关的通用处置内容。
2. 回答没有始终围绕用户询问的故障对象、指标或判断目标展开。
3. 部分回答虽然引用正确，但引用支持的内容与生成结论不完全匹配。
4. Redis、MySQL、历史工单等多来源或高边界案例仍然存在信息覆盖不足。
5. 评测结果已经能够区分检索失败、输入无效和 Judge 指标不可用，但完整验收仍被回答质量指标阻塞。

## 4. 最新完整 Runtime Full Judge 结果

截至 2026 年 7 月 23 日，尚未生成新的 full Judge 报告；当前最新有效结果仍是 **2026 年 7 月 22 日 15:04 至 15:08** 的完整 runtime 运行。

| 指标 | 当前结果 | 目标 | 状态 |
| --- | ---: | ---: | --- |
| 全部案例通过 | `2/12` | `12/12` | 失败 |
| 核心案例通过率 | `16.67%` | `100%` | 失败 |
| Faithfulness | `0.8367` | `>= 0.85` | 失败 |
| Response Relevancy | `0.4314` | `>= 0.80` | 失败 |
| Judge OnCall Actionability | `0.90` | `>= 0.80` | 通过 |
| Answer Completeness | `0.90` | `>= 0.95` 或核心案例 `1.00` | 失败 |
| ID Context Precision | `1.00` | `>= 0.95` | 通过 |
| ID Context Recall | `1.00` | `>= 0.95` | 通过 |
| OnCall Actionability | `0.90` | `>= 0.90` | 通过 |
| Citation Existence | `1.00` | `1.00` | 通过 |
| Citation Support | `1.00` | `>= 0.95` | 通过 |
| Citation Correctness | `1.00` | `>= 0.95` | 通过 |
| Incident Boundary | `1.00` | `1.00` | 通过 |
| 拒答边界 | `2/2` | `2/2` | 通过 |
| invalid_input | `0` | `0` | 通过 |

## 5. 失败案例分布

### 5.1 仅 Response Relevancy 失败

- `stage3_cpu_evidence`
- `stage3_memory_oom`
- `stage3_disk_inode`
- `stage3_loki_ingestion`
- `stage3_deploy_history`

这类案例的共同问题是：回答能够提供证据和动作，但加入了过多通用的变更计划、审批、观察窗口或背景内容，导致答案没有足够聚焦于用户当前问题。

### 5.2 Response Relevancy 与 Faithfulness 失败

- `stage3_dependency_503`
- `stage3_k8s_backend`
- `stage3_ticket_history`

这类案例除回答偏离问题外，还出现了引用片段与回答结论之间的支持关系不够紧密。例如：

- 将应用配置、Secret 或发布判断扩展到当前检索片段没有明确支持的范围。
- 将 Kubernetes Pod、Service、EndpointSlice 的多个判断拼接为一条较长结论。
- 将历史工单中的事实与当前故障证据混合表达。

### 5.3 MySQL 慢查询案例

`stage3_mysql_slow_query` 失败：

- Response Relevancy
- Faithfulness
- OnCall Actionability

当前回答没有稳定保留用户明确询问的 `pool_waiting`、`active_connections`、慢 SQL 和 `EXPLAIN` 之间的关系，部分生成退化成“当前证据不足”或泛化排查建议。

### 5.4 Redis Capacity 案例

`stage3_redis_capacity` 是当前最严重的单项问题，失败：

- Answer Completeness
- Response Relevancy
- Faithfulness
- Judge OnCall Actionability
- OnCall Actionability

当前回答只覆盖了 `connected_clients` 和 `maxclients` 的基础检查，但没有完整回答该案例要求的：

- `effective_capacity`
- `blocked_clients`
- 连接所有者
- retry amplification
- 当前 incident-window 证据与历史复盘的区别
- 审批边界
- 验证条件
- 回滚条件

此外，回答将多个来源压缩成过短的通用结论，导致 Judge 无法确认每个来源对结论的独立贡献。

## 6. 已经通过的部分

### 6.1 Milvus 运行时检索

本次运行已成功连接并使用 Milvus：

- 地址：`127.0.0.1:19530`
- Collection：`biz`
- 向量维度：`1024`
- Collection 状态：已加载

本次没有出现 Milvus 不可用或 runtime 静默退回 lexical index 的问题。

### 6.2 引用链路

所有质量案例均具备有效引用：

- `source_file` 存在
- `chunk_id` 存在
- 引用存在率为 `100%`
- 引用支持率为 `100%`
- 引用正确率为 `100%`

因此，下一轮不应优先继续扩大引用修复逻辑，而应重点改进回答内容与来源绑定策略。

### 6.3 拒答与时间边界

以下边界已经通过：

- 知识库外问题能够拒答。
- 没有将静态 Runbook 直接表述为当前实时事故事实。
- 历史材料与当前事件边界的确定性检查通过。
- 没有检测到明显事实错误或严重幻觉。

## 7. 根因判断

### 7.1 Prompt 仍然存在“通用模板吸引力”

当前 Prompt 同时要求回答：

- 证据
- 原因判断
- 处置边界
- 审批
- dry-run
- 验证
- 回滚
- 历史/实时区分

即使用户只问其中一部分，模型也容易把所有通用要求都写出来，造成答案冗余和主题漂移。

### 7.2 Answer Coverage 只检查“是否出现”，没有充分检查“是否相关”

当前覆盖矩阵能够检查回答是否包含“证据、判断、边界”等词，但不能充分判断：

- 这些内容是否针对用户指定的故障对象。
- 每条结论是否只使用了必要的来源。
- 是否把通用安全模板误当成当前问题的回答。
- 是否真正覆盖用户点名的指标和实体。

### 7.3 多来源回答缺少来源职责隔离

对于官方文档、事故复盘、工单和 Runbook 混合问题，当前生成阶段虽然要求每个来源贡献结论，但模型仍可能：

- 使用同一个来源回答多个子问题。
- 把历史事实写成当前事实。
- 把官方机制、历史结果和当前诊断动作合并到同一句。
- 在一句话中绑定多个来源，降低 Judge 对支持关系的判断置信度。

### 7.4 生成上下文仍可能包含过多相邻语义片段

Milvus 的 ID Precision 和 Recall 已经通过，但“检索命中正确”不等于“生成上下文最适合回答”。

当前 Top-K 中可能同时存在：

- 首轮证据
- 原因判别
- 处置审批
- 快速决策摘要
- 历史复盘
- 文档元数据

生成模型容易将这些片段拼接成“完整运维模板”，而不是针对问题进行最小回答。

## 8. 后续修复与改进计划

后续工作按“先回答聚焦，再专项案例，最后完整验收”的顺序推进。不要继续用扩大知识库或增加 Top-K 掩盖生成阶段的问题。

### 第 1 步：重构显式子问题规划

在生成前先解析用户问题中的显式目标，只保留必要子目标：

1. 用户询问证据时，优先回答要检查的指标、日志和命令。
2. 用户询问原因时，优先回答区分方法和判据。
3. 用户询问处置时，才补充审批、验证和回滚。
4. 用户没有询问历史时，不主动加入历史工单或复盘内容。
5. 用户没有询问通用变更模板时，不输出执行人、观察时长、canary 比例等字段。

验收要求：

- 每个回答最多 2 至 3 条。
- 每条只表达一个主要结论。
- 每条结论最多绑定一个引用。
- 不出现与用户问题无直接关系的通用模板段落。

实现方向：

- 将当前基于关键词的 Coverage Matrix 改成“问题目标 + 必须实体 + 可选边界”的结构。
- `boundary` 不再因为出现 `maxclients`、`pool_waiting` 等技术词就自动成为必答项。
- 只有用户明确询问处置、变更、扩容、重启、回滚或安全边界时，才强制生成审批与回滚内容。
- 生成 Prompt 直接列出“必须回答”和“禁止主动扩展”两组要求。

### 第 2 步：建立实体和指标级回答契约

针对核心案例，为每个问题生成必须保留的实体集合，例如：

- CPU：`CPU`、进程/线程、线程栈、profiling、流量或慢查询判据。
- Memory：`OOMKilled`、working set/RSS、heap、GC、restart。
- Disk：容量、inode、大目录/大文件、只读检查。
- MySQL：`pool_waiting`、`active_connections`、慢 SQL、`EXPLAIN`。
- Redis：`connected_clients`、`maxclients`、`effective_capacity`、`blocked_clients`。
- Kubernetes：Pod 状态、Events、Service selector、EndpointSlice。
- Loki：`discarded_samples_total`、`discarded_bytes_total`、reason、用户可见症状。
- 历史工单：工单 ID、版本/部署记录、历史与当前 incident-window 区分。

验收要求：

- 用户问题中的关键实体至少有一个直接相关结论。
- 关键实体不能只出现在引用上下文中，必须出现在答案中。
- 未被问题要求的相邻实体不应扩展成独立结论。

实现方向：

- 为每个 query 生成 `required_answer_entities`。
- 生成后检查关键实体是否真正出现在回答 claim 中。
- 缺失关键实体时只针对缺失实体重试，不重新展开整套通用模板。
- 增加 `off_topic_entities` 诊断，识别配置、Secret、canary 等无关扩展。

### 第 3 步：落实来源职责隔离

对多来源问题采用显式职责映射：

| 来源类型 | 允许承担的职责 |
| --- | --- |
| 官方文档 | 机制、限制、参数含义、命令 |
| Runbook | 当前排查步骤、判断路径、处置边界 |
| 事故复盘 | 历史现象、历史根因、历史验证 |
| 工单 | 历史版本、发布、审批和处置记录 |
| 当前工具结果 | 当前 incident-window 事实 |

生成时禁止把不同职责合并成无来源区分的大段叙述。

实现方向：

- 在 generation evidence 中为每个 chunk 标注 `source_role`。
- 多来源 Prompt 明确要求“一条 claim 只承担一种来源职责”。
- 禁止一条 claim 同时绑定多个 `[证据 N]`。
- 对历史来源自动加入“历史记录/历史复盘”限定语。
- 当前没有实时工具证据时，不允许出现“当前已经”“当前为”等确定性表述。

### 第 4 步：修复 Redis Capacity 专项

Redis Capacity 必须成为下一轮的专项回归案例，回答至少要覆盖：

1. 当前 `connected_clients` 与 `effective_capacity` 的比较。
2. `maxclients`、文件描述符余量和 `blocked_clients`。
3. 连接所有者、连接池、重试放大或单一 release。
4. 官方限制与历史复盘的职责区别。
5. 当前证据不足时明确写出缺失项。
6. 限流、降重试、连接池调整或扩容必须经过审批。
7. 变更后的验证条件和回滚条件。

验收要求：

- `stage3_redis_capacity` 单案例 full Judge 全部通过。
- Answer Completeness `1.00`。
- Judge OnCall Actionability `>=0.80`。
- Faithfulness `>=0.85`。

实施顺序：

1. 固定官方 Redis 文档负责容量公式、客户端指标和变更限制。
2. 固定 Redis Postmortem 负责历史 retry amplification、连接来源和事故因果。
3. 回答中明确“历史复盘不能替代当前 INFO clients”。
4. 单独增加 Redis 完整性确定性检查。
5. 先运行单案例 full Judge，达标后再回归 12 案例。

### 第 5 步：修复 MySQL 慢查询专项

MySQL 案例必须保留：

- `pool_waiting`
- `active_connections`
- 慢 SQL
- `EXPLAIN`
- 锁等待或连接池耗尽的区分
- 发布版本与回滚边界

禁止只输出“当前证据不足”而不说明下一步应该检查什么。

实施方向：

- 第一条必须回答 `pool_waiting` 与 `active_connections` 的关系。
- 第二条必须说明通过慢 SQL、`EXPLAIN`、锁等待或连接持有时间区分原因。
- 只有问题要求变更时，才加入索引、连接池或回滚审批边界。
- “当前证据不足”后必须跟随具体的下一步只读取证动作。

### 第 6 步：增加生成后的确定性质量门

在提交给 Judge 之前增加以下确定性检查：

- 回答是否包含问题中的核心实体。
- 是否出现与问题无关的通用模板词汇。
- 是否有连续多条回答都使用同一个泛化句式。
- 是否把多个来源绑定到同一句。
- 是否存在“当前证据不足”但同时没有给出下一步取证动作。
- 历史案例是否明确标记为历史，而不是当前事实。

该检查只用于诊断和失败原因分类，不替代 Judge。

新增建议状态：

- `answer_entity_missing`
- `answer_topic_drift`
- `generic_boundary_overexpansion`
- `multi_source_claim_mixed`
- `history_presented_as_current`
- `evidence_gap_without_next_step`

对于可修复问题执行一次定向重试；第二次仍失败时保留最聚焦且引用有效的版本，不再无限重试。

### 第 7 步：提高扩展 37 案例上下文 Precision

当前 Recall 已达到 `0.9062`，下一步应减少无关上下文，而不是继续增加召回：

- 每个来源设置最大 chunk 数。
- 优先选择覆盖不同显式子问题的 chunk。
- 排除文档元数据、Scope、Owner 等低回答价值片段。
- 当前事故问题降低历史 PDF/XLSX 的默认权重。
- 历史问题提高 PDF/XLSX 权重。
- 保留 required source，但不强制把其所有相邻标题片段送入生成。

目标：

- Runtime id-smoke：`37/37`
- ID Context Precision：`>=0.90`
- ID Context Recall：`>=0.90`

### 第 8 步：重新执行固定验收流程

代码和 Prompt 调整后，按以下顺序重新执行：

1. 重建 Milvus 索引。
2. 验证知识库资产 hash 和索引 identity。
3. 运行 37 案例 runtime id-smoke。
4. 运行 12 案例 runtime full Judge。
5. 对固定 Prompt 和固定知识库重复运行 3 次。
6. 比较每次的案例级失败集合，而不仅比较平均分。
7. 更新人工复核绑定的 `source_run`、`case_set_sha256`、知识库 hash 和 Prompt hash。

在单次 `12/12` 之前，不执行三次完整 Judge，以免重复消耗模型和 Judge 调用。单次达标后再进行 3 次稳定性验证。

## 9. 下一轮验收标准

### 核心 12 案例

- 全部通过：`12/12`
- Faithfulness：`>=0.85`
- Response Relevancy：`>=0.80`
- Answer Completeness：`>=0.95`
- Judge OnCall Actionability：`>=0.80`
- ID Context Precision：`>=0.95`
- ID Context Recall：`>=0.95`
- Citation Correctness：`>=0.95`
- Incident Boundary：`100%`
- 拒答边界：`100%`
- `invalid_input`：`0`

### 扩展 37 案例

- Runtime id-smoke：`37/37`
- Citation existence：`100%`
- Citation support：`>=0.95`
- Context Recall：`>=0.90`
- Context Precision：`>=0.90`
- OnCall Actionability：`>=0.90`

## 10. 当前不应优先处理的事项

在回答质量达标前，暂不优先投入以下工作：

- 继续扩写已经达到可用质量的官方快照。
- 继续处理官方文档之间的近重复 chunk。
- 继续增加与当前 12 个核心案例无关的知识资产。
- 用平均分掩盖单案例失败。
- 将 id-smoke、offline、fixed-context 和 runtime full Judge 的结果直接混合比较。

## 11. 下一步执行顺序

下一轮实际开发建议严格按以下顺序：

1. 调整 Answer Coverage，只保留用户显式子问题。
2. 增加 required entity 和 off-topic entity 检查。
3. 修复 Redis Capacity 单案例。
4. 修复 MySQL 慢查询单案例。
5. 回归 CPU、Memory、Disk、Loki 等 Relevancy 失败案例。
6. 运行核心 12 案例 id-smoke，确认检索和引用没有回归。
7. 运行核心 12 案例 full Judge 一次。
8. 运行扩展 37 案例 id-smoke。
9. 单次 full Judge 达到 `12/12` 后，固定知识库、Prompt 和配置重复 3 次。
10. 更新人工复核绑定和最终基线报告。

## 12. 结论

当前系统已经完成从“检索和引用不稳定”到“检索、引用和边界基本稳定”的阶段性改进。

下一阶段的核心目标不是继续扩大召回，而是让模型：

> 只回答用户实际问的问题，只使用支撑该问题所需的证据，并清楚区分当前事实、历史材料、机制说明和受控处置边界。

只有完成回答聚焦、实体覆盖、来源职责隔离和 Redis/MySQL 专项修复后，才适合再次进行最终的 runtime full Judge 验收。
