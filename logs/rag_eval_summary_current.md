# AutoOnCall RAG 离线评测摘要

## 运行记录
- 生成时间：2026-07-07T12:38:47.720897+00:00
- case 文件：`eval\rag_cases.yaml`
- 文档目录：`aiops-docs`
- 总耗时：88.10 ms
- 评测边界：offline deterministic RAG retrieval regression; local multi-source docs (Markdown/PDF/HTML/CSV/XLSX) and lexical scoring are used, not live LLM or Milvus
- Git commit：`1ba4c75244c78c6f3cf5edc632960bf65bdf5b90`
- Python：`3.11.3`
- RAG top_k：3

## 核心指标
- RAG case：30/30 (100%)
- recall@1：100%
- recall@3：100%
- strict recall@3：100%
- MRR：1.00
- citation coverage：100%
- confusion case pass：100%
- no-answer rejection：100%

> 以上指标只代表离线固定 case 的检索回归结果，不代表线上问答准确率。

## 失败定位
- 无失败 case。

## Case 明细
| Case | 类型 | 结果 | 期望来源 | 实际来源 | Top score | 失败指标 |
| --- | --- | --- | --- | --- | ---: | --- |
| cpu_high_usage_alert | positive | PASS | cpu_high_usage.md | cpu_high_usage.md, cpu_high_usage.md, cpu_high_usage.md | 2.78 | - |
| cpu_dead_loop | positive | PASS | cpu_high_usage.md | cpu_high_usage.md | 5.07 | - |
| cpu_slow_sql_relation | confusion | PASS | cpu_high_usage.md | cpu_high_usage.md, slow_response.md | 2.79 | - |
| cpu_high_but_root_cause_slow_query | confusion | PASS | slow_response.md | slow_response.md, cpu_high_usage.md, slow_response.md | 4.98 | - |
| memory_oom | positive | PASS | memory_high_usage.md | memory_high_usage.md, memory_high_usage.md, memory_high_usage.md | 4.00 | - |
| memory_jvm_gc | positive | PASS | memory_high_usage.md | memory_high_usage.md, memory_high_usage.md, memory_high_usage.md | 5.58 | - |
| disk_no_space | positive | PASS | disk_high_usage.md | disk_high_usage.md, service_unavailable.md, disk_high_usage.md | 2.69 | - |
| disk_docker_images | positive | PASS | disk_high_usage.md | disk_high_usage.md, disk_high_usage.md, disk_high_usage.md | 5.09 | - |
| disk_high_but_container_logs | confusion | PASS | disk_high_usage.md | disk_high_usage.md, disk_high_usage.md, disk_high_usage.md | 5.03 | - |
| disk_inode_and_large_files | positive | PASS | disk_high_usage.md | disk_high_usage.md, disk_high_usage.md | 3.89 | - |
| service_503_unavailable | positive | PASS | service_unavailable.md | service_unavailable.md, service_unavailable.md, service_unavailable.md | 4.64 | - |
| service_dependency_redis_down | confusion | PASS | service_unavailable.md | service_unavailable.md, service_unavailable.md | 4.00 | - |
| service_503_but_dependency_timeout | confusion | PASS | service_unavailable.md | service_unavailable.md, service_unavailable.md, disk_high_usage.md | 3.86 | - |
| service_config_error | positive | PASS | service_unavailable.md | service_unavailable.md | 4.32 | - |
| slow_response_sql | positive | PASS | slow_response.md | slow_response.md, slow_response.md, cpu_high_usage.md | 5.52 | - |
| slow_response_external_api | positive | PASS | slow_response.md | slow_response.md, mysql_slow_query_postmortem.pdf, redis_postmortem.pdf | 3.44 | - |
| slow_response_cache_penetration | positive | PASS | slow_response.md | slow_response.md | 5.33 | - |
| reject_resume_question | negative | PASS | - | - | 0.00 | - |
| reject_reimbursement_policy | negative | PASS | - | - | 0.00 | - |
| reject_frontend_color | negative | PASS | - | - | 0.00 | - |
| reject_stock_investment | negative | PASS | - | - | 0.00 | - |
| reject_meeting_room_booking | negative | PASS | - | - | 0.00 | - |
| pdf_postmortem_loader_metadata | positive | PASS | redis_postmortem.pdf | redis_postmortem.pdf, redis_capacity_wiki.html, tickets.csv | 4.00 | - |
| html_wiki_loader_heading | positive | PASS | payment_wiki.html | payment_wiki.html, mysql_slow_query_postmortem.pdf, tickets.csv | 5.67 | - |
| table_ticket_loader_row_citation | positive | PASS | tickets.csv | tickets.csv, tickets.xlsx, redis_capacity_wiki.html | 3.50 | - |
| xlsx_deploy_history_row_citation | positive | PASS | tickets.xlsx | tickets.xlsx, tickets.xlsx, tickets.xlsx | 2.00 | - |
| redis_capacity_wiki_runtime_boundary | positive | PASS | redis_capacity_wiki.html | redis_capacity_wiki.html, redis_capacity_wiki.html, redis_postmortem.pdf | 3.33 | - |
| mysql_pdf_postmortem_pool_waiting | positive | PASS | mysql_slow_query_postmortem.pdf | mysql_slow_query_postmortem.pdf, payment_wiki.html, tickets.csv | 5.33 | - |
| redis_ticket_retry_loop_history | positive | PASS | tickets.csv | tickets.csv, tickets.xlsx, tickets.xlsx | 4.16 | - |
| mysql_xlsx_rc4_remediation_history | positive | PASS | tickets.xlsx | tickets.xlsx, mysql_slow_query_postmortem.pdf, payment_wiki.html | 4.43 | - |
