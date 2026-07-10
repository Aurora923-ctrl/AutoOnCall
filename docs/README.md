# 文档导航

仓库中的项目文档和静态文档资产统一存放在 `docs/`，运行期和文档引用均使用稳定的 ASCII 路径。

- `project/`：校招项目总文档、技术文章提示词索引和架构图资产。
- `interview/`：五分钟演示脚本与负例边界。
- `articles/`：十三篇项目设计与实现技术长文。
- `golden-chains/`：Redis、MySQL 与 K8s 黄金链路。
- `knowledge-base/`：由 `make upload` 和离线 RAG 评测使用的 11 个稳定英文名资产。

运行日志、评测生成摘要和临时调试报告应保留在被忽略的 `logs/`、`tmp_debug*` 路径中，不属于源码文档。
