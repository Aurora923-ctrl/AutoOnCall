# AutoOnCall 项目量化指标与秋招验收记录

## 1. 文档定位

本文只保留秋招有用的信息：

- 项目做成了什么。
- 有哪些可复核的量化结果。
- 指标来自什么证据等级和样本规模。
- 简历与面试应该如何表述。
- 哪些结论不能对外声称。

历史实施计划、阶段任务清单、重复验收标准、Codex 执行提示词和已完成的待办均已删除。

状态日期：2026-07-12（Asia/Shanghai）。

## 2. 项目一句话介绍

AutoOnCall 是一个基于 FastAPI、RAG 和 Plan-Execute-Replan Agent 的 AIOps 智能诊断系统。
系统接收告警后规划排查步骤，并行读取 Prometheus、Loki、Redis、MySQL、CMDB、部署历史
和工单等证据，输出结构化根因分析；证据不足时触发 Replan 或 `needs_human`，高风险处置
受审批、pre-check、dry-run 和回滚边界约束。

## 3. 已完成的核心工作

1. 建立多格式知识资产解析、切分、质量检查和 Milvus 索引链路。
2. 建立 RAG 检索、回答质量、Agent RCA、安全策略和性能评测体系。
3. 将 RCA 从关键词命中升级为结构化 Top-1/Top-3 根因、证据引用和工具选择评测。
4. 接入 7 类真实观测或运维数据源，并实现 Trace、报告和诊断结论的一致性检查。
5. 完成本地 Redis、MySQL 和下游依赖受控故障注入、诊断及恢复验证。
6. 建立危险动作审批、参数注入拦截、dry-run 和未授权执行防护。
7. 建立可追溯 benchmark：记录 commit、run ID、样本量、证据等级、置信区间和失败 case。
8. 将最终指标接入统一 scorecard、评测 API、工作台和五分钟面试演示链路。

## 4. 证据等级

| 等级 | 含义 | 可对外声称的范围 |
| --- | --- | --- |
| `official` | 干净 commit 上生成的正式 benchmark | 可用于证明工程基线和结果可追溯性 |
| `offline_fixture` | 固定离线标注集或确定性模拟数据 | 可证明离线评测结果，不能称生产准确率 |
| `simulated_review` | 模拟 reviewer 导入结果 | 只证明评审和统计流程，不代表独立真人盲评 |
| `local_live` | 本地服务、真实模型或真实适配器运行 | 可证明本地真实链路，不代表生产容量 |
| `controlled_fault` | 本地可恢复故障注入实验 | 可证明受控环境诊断能力，不能称生产 MTTD/MTTR |
| `production` | 真实生产运行数据 | 当前样本不足，状态为 `not_enough_data` |

## 5. 核心量化指标总览

| 能力 | 样本规模 | 核心结果 | 证据等级 |
| --- | ---: | --- | --- |
| 工程基线 | 8 个 required module | `8/8` 通过，scorecard `passed` | `official` |
| 自动化测试 | 542 个 pytest 用例 | `542 passed`，覆盖率 `83.33%` | `official` |
| 知识资产 | 20 份资产、209 chunks | 解析、切分、index-ready 均 `20/20` | `local_live` |
| RAG 回归集 | 80 case | `80/80`；Recall@3 `1.00`，MRR `0.8776`，nDCG@3 `0.8889` | `offline_fixture` |
| 企业冻结检索集 | 12 case | `11/12`；9 条企业排障正例 `9/9`；MRR `0.94`，nDCG@3 `0.96` | `offline_fixture` |
| 回答质量 | 12 case × 3，共 36 次 | 稳定率、行动性、grounding 均 `1.00`；事实错误和严重幻觉 `0` | `offline_fixture` |
| Agent RCA | 48 case | Top-1、Top-3、Macro-F1 均 `1.00`；工具选择 F1 `0.9583` | `offline_fixture` |
| 安全评测 | 43 case | Forbidden、Approval、Safe Allow F1 均 `1.00`；未授权执行 `0` | `offline_fixture` |
| 真实模型性能 | RAG 20、AIOps 10 | 30/30 成功；RAG P95 `5.82s`，AIOps P95 `63.26s` | `local_live` |
| 故障注入恢复 | Redis 2、MySQL 2、下游 3 | `7/7` 注入和恢复成功 | `controlled_fault` |
| 真实适配器 | 7 类 source | 7 类全部通过；Redis/MySQL 主链各 `3/3` | `local_live` |
| 端到端故障 RCA | Redis 1、MySQL 1 | `2/2` Top-1 正确并完成恢复 | `controlled_fault` |

## 6. 指标明细

### 6.1 工程质量与可追溯基线

Official benchmark：

- Run ID：`20260712T074629Z-81f1f98c-0d69abdc`
- Commit：`81f1f98ccbc4588f8470ad82d3fb82ad849c431f`
- Required modules：`8/8`
- Scorecard：`passed`
- 工作区状态：`git_dirty=false`
- pytest：`542 passed`
- 应用覆盖率：`83.33%`
- Ruff / Black：通过
- Scorecard 可用模块：`7/10`
- 失败模块：`0`
- 可选缺失或数据不足模块：`3`

统一产物：

`logs/benchmarks/20260712T074629Z-81f1f98c-0d69abdc/interview_scorecard.json`

### 6.2 知识资产质量

| 指标 | 结果 | 说明 |
| --- | ---: | --- |
| 资产数量 | `20` | Markdown 14、PDF 2、HTML 2、CSV 1、XLSX 1 |
| 解析成功率 | `20/20 = 1.00` | 95% Wilson CI `[0.8389, 1.0000]` |
| 切分成功率 | `20/20 = 1.00` | 95% Wilson CI `[0.8389, 1.0000]` |
| Index-ready | `20/20 = 1.00` | 95% Wilson CI `[0.8389, 1.0000]` |
| Chunk 数量 | `209` | 平均长度 `902.34` 字符 |
| Chunk P50 / P95 / Max | `762 / 1596 / 1858` | 字符 |
| 空 Chunk | `0/209` | Wilson upper `0.0180` |
| 元数据缺失 | `0/209` | Wilson upper `0.0180` |
| 超长 Chunk | `4/209 = 1.91%` | 保留为 watch metric |
| 完全重复 Chunk | `0/209` | 无完全重复 |
| 近似重复 Chunk | `8/209 = 3.83%` | 保留为 watch metric |
| Milvus CRUD 一致性 | `passed` | 写入、读取、删除和清理通过 |

### 6.3 RAG 检索

80 条正式回归集：

| 指标 | 结果 |
| --- | ---: |
| 总体通过 | `80/80` |
| 非拒答 / 拒答 | `64 / 16` |
| Recall@1 / @3 / @5 | `0.6641 / 1.0000 / 1.0000` |
| Precision@1 / @3 / @5 | `0.7812 / 0.4166 / 0.2500` |
| MRR | `0.8776` |
| MAP@1 / @3 / @5 | `0.7812 / 0.8763 / 0.8763` |
| nDCG@1 / @3 / @5 | `0.7188 / 0.8889 / 0.8889` |
| Strict multi-source@3 / @5 | `1.00 / 1.00` |
| 引用覆盖率 | `1.00` |
| 拒答 Precision / Recall / F1 | `1.00 / 1.00 / 1.00` |
| 离线延迟 P50 / P95 / P99 | `12.27 / 18.38 / 20.10ms` |

独立企业排障冻结集：

- 总体 `11/12`。
- 9 条企业排障正例 `9/9`。
- Recall@3 `1.00`。
- MRR `0.94`。
- nDCG@3 `0.96`。
- 唯一失败为通用旅行域外拒答，不影响企业排障主链，但作为失败 case 保留。

### 6.4 回答质量

样本为 12 个核心 case，每个运行 3 次，共 36 个 answer-run。

| 指标 | 结果 |
| --- | ---: |
| 确定性通过率 | `12/12 = 1.00` |
| 三次运行稳定率 | `1.00` |
| 稳定性标准差 | `0` |
| Incident ID recall / precision | `1.00 / 0.70` |
| OnCall actionability | `1.00` |
| 引用存在 / 支持 / grounding | `1.00 / 1.00 / 1.00` |
| 引用正确率 | `0.70` |
| 拒答边界 | `1.00` |
| Incident 边界 / 混淆消解 | `1.00 / 1.00` |
| 事实错误率 | `0` |
| 严重幻觉率 | `0` |

36/36 模拟评审已导入，自动评测与模拟评审一致率为 `1.00`。该结果仅说明评审模板、
导入和统计链路可用，不能写成“真人盲评一致率 100%”。

### 6.5 Agent RCA

样本为 48 条结构化根因分析 case。

| 指标 | 结果 |
| --- | ---: |
| 总体通过 | `48/48` |
| Top-1 Accuracy | `1.00` |
| Top-3 Recall | `1.00` |
| Macro Precision / Recall / F1 | `1.00 / 1.00 / 1.00` |
| 必要证据召回率 | `1.00` |
| 结论证据支持率 | `1.00` |
| Replan F1 | `1.00` |
| `needs_human` F1 | `1.00` |
| Trace 完整率 | `1.00` |
| 报告结论一致率 | `1.00` |
| 工具选择 F1 | `0.9583` |
| 无效工具率 | `0` |

这些结果证明固定离线数据集上的结构化 RCA 能力，不代表生产故障定位准确率。

### 6.6 安全与审批

样本为 43 条危险动作、审批动作、安全只读动作和注入攻击正反例。

| 指标 | 结果 |
| --- | ---: |
| 总体通过 | `43/43` |
| Forbidden action F1 | `1.00` |
| Approval trigger F1 | `1.00` |
| Safe Allow F1 | `1.00` |
| 安全动作误拦截率 | `0` |
| 审批绕过率 | `0` |
| 未授权执行率 | `0` |
| 敏感信息泄漏率 | `0` |

结论仅适用于离线安全评测，不代表已有真实生产变更执行记录。

### 6.7 真实模型延迟

Run ID：`stage6-20260712T072656Z`。

| 场景 | 请求数 | 成功率 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: |
| RAG | `20` | `20/20` | `3414.33ms` | `5816.70ms` |
| AIOps | `10` | `10/10` | `49123.07ms` | `63262.73ms` |

当前 Trace 未暴露模型供应商的 token usage，因此 Token 为 `not_observed`，金额为
`not_run`。不能虚构单次成本，也不能根据这 30 次请求声称生产容量或最大稳定并发。

### 6.8 受控故障与真实数据源

故障注入与恢复：

- Redis `2/2`。
- MySQL `2/2`。
- 下游依赖 `3/3`。
- 合计 `7/7`，均完成 cleanup verification。

真实适配器：

- Prometheus、Loki、Redis、MySQL、CMDB、部署历史和工单共 7 类 source 全部通过。
- Redis 诊断主链 `3/3`。
- MySQL 诊断主链 `3/3`。

同一时间窗口的端到端受控故障：

| 故障 | Top-1 | First useful diagnosis | 告警至诊断 | 恢复 |
| --- | --- | ---: | ---: | ---: |
| Redis maxclients | `redis_maxclients` | `3.373s` | `3.474s` | `0.325s` |
| MySQL 慢查询 | `mysql_slow_query` | `0.577s` | `0.678s` | `2.603s` |

端到端样本仅 `2/2`，可用于展示本地受控实验链路，不能称为生产 MTTD、MTTR 或生产
RCA 准确率。

## 7. 最值得写进简历的指标

建议优先选择 3 至 4 条，不要把所有数字堆进一段经历。

### 版本 A：偏 AI Agent / AIOps

> 设计并实现基于 FastAPI、RAG 与 Plan-Execute-Replan 的 AIOps 诊断 Agent，接入
> Prometheus、Loki、Redis、MySQL、CMDB 等 7 类数据源；在 48 条结构化 RCA 测试集上
> 实现 Top-1/Top-3 与 Macro-F1 均为 1.00，工具选择 F1 达 0.9583，并支持证据不足时
> Replan 和 `needs_human` 降级。

### 版本 B：偏 RAG 与评测工程

> 围绕 20 份多格式运维知识资产构建可追溯 RAG 评测体系，完成 209 个 Chunk 的质量治理；
> 在 80 条回归集上 Recall@3 达 1.00、MRR 达 0.8776、nDCG@3 达 0.8889，并通过 run ID、
> 置信区间和失败 case 实现指标追溯。

### 版本 C：偏工程可靠性与安全

> 构建覆盖 542 个 pytest 用例的质量门禁，应用覆盖率 83.33%；设计 43 条高风险动作与
> 注入攻击正反例，Forbidden、Approval 和 Safe Allow F1 均为 1.00，未授权执行和审批
> 绕过为 0。

### 版本 D：偏真实故障实验

> 搭建 Redis、MySQL 和下游依赖的本地受控故障实验链路，完成 7/7 次故障注入与恢复验证；
> 在 Redis maxclients 与 MySQL 慢查询端到端实验中均正确输出 Top-1 根因，并保留诊断、
> Trace、证据和恢复记录。

## 8. 面试时建议主动解释的边界

1. `48/48` 和各项 `1.00` 来自固定离线结构化评测集，不是生产准确率。
2. 回答质量的 reviewer 是 `simulated_review`，不是独立真人盲评。
3. Redis/MySQL 延迟来自两条本地受控故障样本，不是生产 MTTD/MTTR。
4. 真实模型只运行了 20 次 RAG 和 10 次 AIOps，足以展示延迟分布，但不足以证明生产容量。
5. 当前没有可靠 token usage，未计算金额，避免用估算值包装成本指标。
6. 生产采集字段已经建立，但真实样本不足，状态仍为 `production: not_enough_data`。

这种边界说明不会削弱项目，反而能证明对离线评测、受控实验和生产指标的区别有工程判断。

## 9. 不应使用的表述

禁止写：

```text
生产故障定位准确率 100%。
显著降低生产 MTTR。
支持 100 并发。
真人盲评一致率 100%。
单次诊断成本为某个金额。
```

除非未来获得对应的真实生产、独立人工评审、正式压测或供应商 Token 数据。

## 10. 面试展示顺序

1. 展示 official scorecard 的 run ID、commit 和证据等级。
2. 用 20 份资产和 209 chunks 说明知识治理。
3. 展示 RAG Recall@3、MRR、nDCG 和保留的失败 case。
4. 展示告警进入后的 Plan、并行取证、结构化 RCA、Trace 和报告。
5. 复跑 Redis 或 MySQL 受控故障，展示诊断与恢复。
6. 展示危险动作审批、dry-run 和未授权执行拦截。
7. 用真实模型 P50/P95 解释性能瓶颈。
8. 主动说明 production 指标仍为 `not_enough_data`。

## 11. 结论

AutoOnCall 当前已经完成秋招所需的核心工程闭环：知识治理、RAG 检索、回答质量、
结构化 Agent RCA、真实数据源接入、安全边界、真实模型延迟、受控故障实验和统一
benchmark 交付。

项目最有价值的部分不是某个孤立的 100% 指标，而是这些指标均带有样本规模、证据等级、
run ID、失败 case 和能力边界，可以在面试中复跑和解释。
