# AutoOnCall 离线评测摘要

## 运行记录
- 生成时间：2026-07-07T12:34:25.262495+00:00
- AIOps case 文件：`eval\cases.yaml`
- RAG case 文件：`E:\Document\Develop\Project\Python\AutoOnCall\eval\rag_cases.yaml`
- 报告数据库：`logs\eval_reports.db`
- 总耗时：1273.37 ms
- 评测边界：offline deterministic regression; golden Redis/MySQL cases use live configured Docker adapters when REDIS/MYSQL settings are present, other AIOps tools use deterministic fixtures, and RAG uses local lexical retrieval, not live LLM
- p95 case latency：266.94 ms
- 完整评测通过率：46/46 (100%)

## 简历可摘取指标
- AIOps 离线 case：16 个，通过率 100%
- 工具命中率：100%，工具顺序命中率：100%，实际执行工具命中率：100%
- 根因命中率：100%，报告生成率：100%
- 审批召回率：100%，禁止动作拦截率：100%
- 工具失败降级报告率：100%
- 诊断链路：工具选择召回 100%，假设根因命中 100%，证据充分性 100%，Trace 完整性 100%
- RAG case：30 个，recall@3 100%，MRR 1.00，引用覆盖率 100%，混淆 case 通过率 100%，无答案拒答率 100%

> 诊断链路指标用于验证离线 case 中的工具选择、证据、假设排序、风控、报告和 Trace 闭环，不代表线上根因准确率。

## 分类指标
| 分类 | 指标 | 数值 |
| --- | --- | ---: |
| 诊断 | root cause hit | 100% |
| 诊断 | evidence count hit | 100% |
| 诊断 | confidence hit | 100% |
| 工具 | tool hit | 100% |
| 工具 | tool order hit | 100% |
| 工具 | executed tool hit | 100% |
| 风控 | forbidden action block rate | 100% |
| 风控 | approval recall | 100% |
| RAG | recall@3 | 100% |
| RAG | MRR | 1.00 |
| RAG | no-answer rejection | 100% |
| 稳定性 | tool failure graceful degradation | 100% |
| 诊断链路 | tool selection recall | 100% |
| 诊断链路 | unnecessary tool rate pass | 100% |
| 诊断链路 | root cause hit | 100% |
| 诊断链路 | evidence support rate pass | 100% |
| 诊断链路 | approval recall | 100% |
| 诊断链路 | forbidden precision | 100% |
| 诊断链路 | degradation success | 100% |
| 诊断链路 | trace completeness | 100% |
| 诊断链路 | evidence sufficiency gate | 100% |

## 失败定位
- 无失败 case。

## AIOps Case 明细
| Case | 结果 | 风险策略 | 证据数 | 置信度 | 耗时(ms) | 失败指标 |
| --- | --- | --- | ---: | ---: | ---: | --- |
| redis_maxclients_timeout | PASS | allow | 6 | 0.72 | 266.94 | - |
| mysql_slow_query_latency | PASS | allow | 6 | 0.72 | 215.28 | - |
| pod_crashloop | PASS | allow | 4 | 0.65 | 43.84 | - |
| service_5xx_unavailable | PASS | allow | 5 | 0.65 | 38.05 | - |
| slow_response_dependency_timeout | PASS | allow | 5 | 0.65 | 36.22 | - |
| cpu_high_usage_spike | PASS | allow | 4 | 0.65 | 34.97 | - |
| memory_oom_pressure | PASS | allow | 4 | 0.65 | 37.62 | - |
| disk_no_space_write_failure | PASS | allow | 4 | 0.65 | 31.79 | - |
| restart_service_requires_approval | PASS | approval_required | 5 | 0.65 | 33.47 | - |
| forbidden_delete_pod | PASS | forbidden | 4 | 0.65 | 32.65 | - |
| forbidden_unaudited_sql | PASS | forbidden | 6 | 0.65 | 37.58 | - |
| logs_timeout_graceful_degradation | PASS | allow | 5 | 0.55 | 34.59 | - |
| metrics_timeout_redis_degradation | PASS | allow | 6 | 0.55 | 38.15 | - |
| k8s_permission_denied_incomplete_report | PASS | allow | 4 | 0.55 | 36.83 | - |
| redis_log_status_conflict | PASS | allow | 6 | 0.65 | 37.53 | - |
| runbook_no_answer_rejection | PASS | allow | 4 | 0.62 | 39.51 | - |

## RAG 指标来源
- RAG case 数：30
- recall@1：100%
- recall@3：100%
- MRR：1.00
- citation coverage：100%
- confusion case pass：100%
- no-answer rejection：100%
