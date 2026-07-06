# AutoOnCall 校招竞争力改进计划

> 生成时间：2026-07-06  
> 输入依据：`文档/校招竞争力对标与改进清单.md`、当前仓库 README / 评测 / 沙箱 / MCP 现状，以及文档中点名的 HolmesGPT、RCA Agent、Causely、MCP servers、ShopSimulator、DB-GPT 等项目。

## 1. 总目标

AutoOnCall 不需要推倒重做。下一阶段最有效的路线，是把它从“完整的 AIOps Demo”继续打磨成：

> 面向 OnCall 故障诊断的 SRE Agent 原型：具备真实或可复现观测数据、证据驱动 RCA、安全边界、Trace / Approval / Report 闭环，以及可复现评测指标。

计划聚焦校招展示，不追求企业平台化大而全。每个改动都要能回答面试官的三个问题：

- 这个项目解决什么真实问题？
- Agent 的结论为什么可信？
- 如果模型判断错了，系统如何限制风险？

## 2. 对标项目转化成 AutoOnCall 行动项

| 对标来源 | 可借鉴点 | AutoOnCall 落地方式 |
| --- | --- | --- |
| HolmesGPT | 生产事故调查、多数据源取证、只读访问、RBAC、工具输出预算 | 固化 Redis / MySQL / K8s 三个端到端场景；强化 Prometheus、Loki、Redis、MySQL、K8s、工单、发布历史的数据源标注；补工具输出预算和原始证据 artifact |
| Microsoft RCA Agent 论文 | RCA Agent 需要动态采集日志、指标、数据库等诊断信息 | 禁止仅凭 incident 文本强判根因；根因必须由工具证据支撑；工具失败形成 unknown evidence |
| Causely 论文 | 拓扑、依赖、因果关系可减少工具调用、token 和诊断时间 | 做轻量 Causely-lite：服务拓扑、最近变更、依赖影响面、历史工单参与假设排序 |
| ShopSimulator / LexRAG / NyayaAI | 场景模拟、可量化评测、多轮成功率、引用准确性 | 扩展 eval case，不只看 pass/fail，输出 RCA 命中、工具命中、反证识别、拒答、报告完整率 |
| DB-GPT | 多源数据分析、沙箱执行、报告输出 | 诊断报告升级为面试可展示的决策报告；保留 sandbox / dry-run 边界，不包装成生产自动修复 |
| MCP servers | 标准工具协议、受控访问工具和数据源 | 优先 MCP 化 Prometheus 和 Loki，Tool Registry 同时兼容本地工具和 MCP 工具 |

## 3. 当前仓库基础判断

已有基础已经很好：

- `README.md` 已经把 Alert / Incident -> Plan -> Tool -> Evidence -> Replan -> Report 的主线讲清楚。
- `deploy/sandbox.md` 和 `deploy/compose/full-stack-compose.yml` 已经覆盖 Redis、MySQL、Prometheus、Alertmanager、Grafana、Loki、K8s mock、Tempo、Jaeger、CMDB、工单和发布历史。
- `scripts/eval/eval_cases.py` 已经有较完整的指标分组，包括工具命中、根因命中、风险策略、报告、RAG 和 Trace 完整性。
- `scripts/sandbox/verify_full_stack_adapters.py` 已经能验证工具是否消费真实 adapter source。
- `mcp_servers/README.md` 已明确当前 MCP 服务更偏 mock/fallback，不是主生产入口。
- `app/services/context_budget.py` 已开始做上下文预算，但主要是字符级 prompt 截断，还没有覆盖工具大输出 artifact 化。

主要差距不是“没有功能”，而是：

- 端到端演示需要更稳定、更像面试作品包。
- 评测 case 需要更多反证、冲突、失败和无依据场景。
- 报告需要明确展示 supporting / refuting / unknown evidence。
- K8s、Trace、MCP 工具层还可以进一步从 mock/fallback 走向可复现真实链路。
- 工具大输出、token 预算和 artifact path 需要更像生产系统。

## 4. 执行路线总览

| 阶段 | 时间建议 | 目标 | 退出标准 |
| --- | --- | --- | --- |
| P0 | 1-2 周 | 把校招主线打稳：3 个 demo、证据驱动 RCA、评测 summary、报告样例 | 10 分钟能稳定演示 Redis / MySQL / K8s 任一主线；3 份报告可直接打开；评测输出指标化 summary |
| P1 | 2-4 周 | 拉开工程深度：MCP 化、输出预算、拓扑/变更关联、K8s/Trace 加强 | 至少 2 个真实 MCP server 跑通；大日志不进 prompt；报告体现变更/拓扑影响 |
| P2 | 4 周后 | 锦上添花：checkpoint、RBAC、工作台体验、一键 demo | 审批可恢复；角色边界清晰；演示脚本一键生成数据、报告和评测结果 |

## 5. P0：最优先改进

### P0-1 固化 3 个稳定端到端故障场景

借鉴：HolmesGPT、ShopSimulator。

建议场景：

| 场景 | 输入 | 必须覆盖的数据源 | 预期结论 |
| --- | --- | --- | --- |
| Redis maxclients | `order-service Redis connection timeout，5xx 上升` | Alertmanager、Prometheus、Loki、Redis、Runbook、历史工单 | Redis 连接接近 maxclients，应用连接池释放异常或突增 |
| MySQL 慢查询 | `payment-service 响应慢，出现慢 SQL 和连接池等待` | Prometheus、Loki、MySQL、Runbook、发布历史 | 慢查询 / 锁等待 / 连接池等待导致延迟 |
| K8s CrashLoop / OOM | `inventory-service Pod CrashLoopBackOff / OOMKilled` | K8s、Loki、Prometheus、Runbook、CMDB | Pod 异常重启导致可用实例减少 |

建议落地：

- 为三个场景补一个统一 demo 入口，例如 `scripts/demo/run_aiops_demos.py` 或扩展现有 `scripts/sandbox/simulate_mysql_redis_aiops.py`。
- 生成固定输出：`logs/demo_reports/redis_maxclients.md`、`logs/demo_reports/mysql_slow_query.md`、`logs/demo_reports/k8s_crashloop.md`。
- 每份报告必须写明数据来源：`prometheus`、`loki`、`redis_info`、`mysql`、`kubernetes`、`mock`、`not_configured`。

验收标准：

- 任一场景 5 分钟内能从输入跑到报告。
- 每个场景至少包含 4 类数据源。
- 关闭 mock fallback 后，缺失数据源表现为结构化失败，不静默回退成 mock。
- README 或面试讲解文档中有 10 分钟演示脚本。

### P0-2 强化证据驱动根因判断

借鉴：HolmesGPT、Microsoft RCA Agent。

核心规则：

- 根因不能只来自用户输入或告警标题。
- 每个根因必须关联 supporting evidence。
- 正常指标、无异常日志、空慢查询、Pod Running 等要形成 refuting evidence。
- 工具失败、超时、未配置要形成 unknown evidence。
- supporting 和 refuting 冲突时降低置信度，并在报告中提示人工确认。

建议落地文件：

- `app/agent/aiops/evidence_analyzer.py`
- `app/services/report_generator.py`
- `app/services/report_markdown.py`
- `tests/test_evidence_analyzer.py`
- `tests/test_report_generator.py`

验收标准：

- 报告包含“支持证据 / 反驳证据 / 不确定证据 / 置信度原因”四块。
- Redis 正常但日志出现 timeout 时，报告不能强判为 Redis maxclients。
- MySQL 正常但 K8s 有 OOMKilled 时，根因排序应把 K8s 放在 MySQL 前面。
- 工具失败时报告能生成，但状态和置信度要反映不确定性。

### P0-3 扩展 RCA 评测集和指标化 summary

借鉴：ShopSimulator、LexRAG、NyayaAI。

当前 `scripts/eval/eval_cases.py` 已经有较多指标，下一步重点是补高质量 case，而不是只加指标名。

新增 case 类型：

- 5 个 contradiction case：用户怀疑 Redis / MySQL / K8s，但工具证据反驳该假设。
- 3 个 tool failure case：Prometheus、Loki、MySQL 任一失败后仍能降级报告。
- 3 个 no trusted source case：Runbook / RAG 无可信来源时拒答。
- 2 个 forbidden action case：危险 SQL、删除 Pod、危险 shell 被阻断。

建议新增或强化指标：

- RCA 根因命中率。
- 工具选择召回率。
- 无关工具率。
- 反证识别率。
- unknown evidence 覆盖率。
- 风险策略命中率。
- 报告完整率。
- RAG 引用准确率。
- 无可信资料拒答率。

验收标准：

- `make eval` 输出 `logs/eval_summary.json` 和 `logs/eval_summary.md`。
- Markdown summary 不只显示通过数量，还显示分类指标。
- AIOps case 总数建议提升到 25 个左右。
- contradiction case 至少 5 个，并能在 summary 中单独展示通过率。

### P0-4 生成可展示诊断报告样例

借鉴：DB-GPT、HolmesGPT findings。

报告结构建议：

- Incident 基本信息。
- 诊断时间线。
- 工具调用表。
- 证据矩阵。
- 根因假设排序。
- 影响面分析。
- 风险动作与审批状态。
- 下一步建议。
- 数据来源说明。
- 置信度和限制说明。

验收标准：

- `logs/demo_reports/` 中稳定生成 Redis、MySQL、K8s 三份 Markdown。
- 每份报告都能回答：看了哪些数据、哪些证据支持、哪些证据反驳、为什么是这个置信度。
- 报告不把 mock/fallback 伪装成真实生产数据。

## 6. P1：显著拉开差距

### P1-1 MCP 化 Prometheus 和 Loki

借鉴：HolmesGPT、MCP servers。

当前 `mcp_servers/` 是 mock/fallback 演示服务。下一步不要一次性 MCP 化所有工具，先做两个最能体现 SRE Agent 的真实工具：

- `mcp_servers/prometheus_server.py`
- `mcp_servers/loki_server.py`

建议工具：

- `prometheus_query`
- `prometheus_range_query`
- `loki_query_range`
- `loki_label_values`

验收标准：

- 至少 Prometheus 和 Loki 两个 MCP server 能连接本地沙箱。
- Tool Registry 可以统一调用本地 adapter 工具和 MCP 工具。
- MCP 工具契约写清输入、输出、权限、超时、裁剪策略和安全边界。
- 文档明确：MCP 是工具协议化，不等于绕过 Tool Registry 风控。

### P1-2 增加工具输出压缩和 artifact 机制

借鉴：HolmesGPT 的 large payload / output budgeting 思路。

建议能力：

- 日志查询默认限制时间窗口、行数和返回字段。
- 大输出先落到 `logs/artifacts/`，prompt 只拿摘要。
- ToolCallRecord 记录 `raw_artifact_path`、`output_size_bytes`、`truncated`、`truncation_reason`。
- 每次诊断记录工具耗时、输出大小和估算 token / 字符预算。

建议落地文件：

- `app/services/context_budget.py`
- `app/agent/aiops/executor.py`
- `app/tools/logs_tool.py`
- `app/tools/tracing_tool.py`
- `app/models/trace.py`
- `tests/test_context_budget.py`

验收标准：

- 超大 Loki 日志不会直接进入 LLM prompt。
- 报告能展示“原始数据路径 + 摘要 + 裁剪原因”。
- 评测中有一个大日志 case，验证不会撑爆 prompt。

### P1-3 做轻量 Causely-lite：拓扑、变更、历史工单参与 RCA

借鉴：Causely、HolmesGPT 的 ticket / change / service context integration。

建议能力：

- 查询服务 owner、依赖、上游、下游、SLO。
- 查询最近部署记录。
- 查询历史相似 incident。
- 把“实时事实”和“历史背景”在 Evidence 中区分。
- 假设排序时考虑依赖影响面和最近变更，但不能让历史背景单独决定根因。

建议落地文件：

- `config/service_topology.yaml`
- `app/services/service_topology.py`
- `app/tools/context_tool.py`
- `app/tools/alert_tool.py`
- `deploy/full-stack/mock-cmdb/`
- `deploy/full-stack/mock-deploy-history/`
- `deploy/full-stack/mock-ticketing/`

验收标准：

- Redis / MySQL / K8s 至少 1 个场景能关联最近变更或历史工单。
- 报告明确区分实时证据、历史背景和推断。
- 拓扑信息能帮助减少无关工具调用，至少在 eval summary 中体现无关工具率下降。

### P1-4 强化 K8s、Loki、Tempo 场景

借鉴：HolmesGPT 的 Kubernetes、Loki、Tempo 数据源。

建议分阶段：

1. 保留 K8s mock API，但让证据形态接近真实 K8s Pod / Event / ContainerStatus。
2. Loki 接收并查询真实容器日志。
3. Tempo / Jaeger 加入高延迟 Trace 场景。
4. 构造“下游依赖慢 span -> 上游 P95 升高”的排查链路。

验收标准：

- K8s 场景报告不只依赖 mock 工具摘要，而能展示 Pod、Event、重启次数、OOMKilled 等结构化证据。
- 高延迟场景能通过 Trace 指到慢 span 或异常依赖。
- `make sandbox-verify` 能覆盖 `kubernetes`、`loki`、`jaeger` 数据源。

## 7. P2：锦上添花

### P2-1 LangGraph 持久化 checkpoint 和审批恢复

借鉴：LangGraph 课程中的 checkpoint / human-in-the-loop 思路。

验收标准：

- 审批等待中的诊断在服务重启后可以恢复。
- SQLite 或 MySQL 中能看到 checkpoint/session snapshot。
- `tests/test_aiops_session_snapshot_store.py` 覆盖恢复边界。

### P2-2 RBAC / Admin Token

建议角色：

- viewer：只看报告、Trace、评测。
- operator：发起诊断。
- approver：审批高风险动作。
- admin：配置工具、数据源和系统参数。

验收标准：

- API 权限表写入 README 或部署文档。
- 审批接口必须校验 approver/admin。
- demo 环境有安全默认 token，不提交真实密钥。

### P2-3 Incident 工作台体验

重点不是炫酷前端，而是让诊断链路一眼可见。

建议补齐：

- Incident 列表。
- Trace 时间线。
- Evidence 矩阵。
- 工具调用详情。
- 审批中心。
- 评测 summary。
- 报告预览。

验收标准：

- 面试时不需要切多个接口或翻日志。
- 前端能清楚显示数据源是 real adapter、mock 还是 not_configured。

### P2-4 一键 demo 脚本

目标是面试前快速恢复到可演示状态。

建议命令：

```bash
make sandbox-up
make sandbox-verify
make sandbox-demo
make eval
```

后续可新增：

```bash
make demo-redis
make demo-mysql
make demo-k8s
make demo-reports
```

验收标准：

- 一键生成 demo 数据、诊断报告和评测 summary。
- README 中有 5-10 分钟演示路径。
- 失败时能明确指出是 Docker、依赖服务、配置还是工具适配器问题。

## 8. 推荐任务拆分

| 任务 ID | 优先级 | 任务 | 主要交付物 | 验收 |
| --- | --- | --- | --- | --- |
| AOC-P0-01 | P0 | Redis / MySQL / K8s 三个 demo case 固化 | demo 脚本、报告样例、README 演示脚本 | 3 个场景 5 分钟内可跑通 |
| AOC-P0-02 | P0 | Evidence supporting/refuting/unknown 完整化 | Evidence Analyzer、报告模板、测试 | 反证 case 不误判 |
| AOC-P0-03 | P0 | RCA 评测扩展 | `eval/cases.yaml`、summary JSON/MD | AIOps case 约 25 个，contradiction >= 5 |
| AOC-P0-04 | P0 | 报告样例升级 | `logs/demo_reports/*.md`、报告字段 | 每份报告包含证据矩阵和数据来源 |
| AOC-P1-01 | P1 | Prometheus / Loki MCP 化 | MCP server、Tool Registry 接入、文档 | 2 个真实 MCP server 跑通 |
| AOC-P1-02 | P1 | 工具输出预算和 artifact | artifact path、输出大小、裁剪原因 | 大日志不进 prompt |
| AOC-P1-03 | P1 | Causely-lite 拓扑与变更关联 | 拓扑/发布/工单证据、假设排序 | 报告区分实时事实和历史背景 |
| AOC-P1-04 | P1 | K8s / Trace 场景加强 | K8s evidence、Trace evidence、sandbox verify | K8s 和 Trace 可复现取证 |
| AOC-P2-01 | P2 | checkpoint 审批恢复 | 持久化 checkpoint、恢复测试 | 重启后审批可恢复 |
| AOC-P2-02 | P2 | RBAC / Token 边界 | 角色权限、接口校验、文档 | 审批接口权限清晰 |
| AOC-P2-03 | P2 | 工作台可视化 | Evidence 矩阵、Trace、报告预览 | 面试不用翻接口 |

## 9. 质量门禁

每完成一个 P0/P1 任务，至少执行：

```bash
make test-quick
make eval
make eval-rag
make eval-change
```

涉及沙箱或适配器时追加：

```bash
make sandbox-verify
make sandbox-demo
```

涉及格式和类型时追加：

```bash
make lint
make type-check
```

如果某个命令依赖本机 Docker、Milvus、外部 API 或不可用服务，文档中必须标注“未运行原因”，不能把未验证项写成已通过。

## 10. 简历升级条件

只有满足以下条件后，才建议在简历中使用“参考 HolmesGPT 工程实践”这种更强表述：

- 3 个端到端场景可复现。
- 报告能展示 supporting / refuting / unknown evidence。
- eval summary 有 RCA、工具、风险、RAG、报告、Trace 等分类指标。
- mock / sandbox / real adapter 边界写清楚。
- 至少 Prometheus / Loki 或等价两类工具完成 MCP 化。
- 不宣传线上准确率，不宣传自动修复生产故障。

推荐升级表述：

```text
参考 HolmesGPT 等 SRE Agent 的工程实践，将 Prometheus、Loki、Redis、MySQL、Kubernetes 等观测数据接入 Tool Registry，并在本地 full-stack sandbox 中构造 Redis、MySQL、K8s 等可复现故障场景；通过 Evidence Analyzer、Risk Controller、Trace、Approval、Report 和离线评测集验证 RCA 命中、工具命中、风险策略、拒答和报告完整性。
```

## 11. 不建议优先投入

- 不做多 Agent 群聊式重构。
- 不做自动生产修复。
- 不做复杂 React 重写。
- 不为了数量堆随机工具。
- 不做模型微调，除非已有稳定数据集和明确收益。
- 不把 mock/fallback 包装成真实生产接入。
- 不在简历里写“线上准确率 100%”。

## 12. 参考来源

- [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
- [Exploring LLM-based Agents for Root Cause Analysis](https://arxiv.org/abs/2403.04123)
- [Causely: A Causal Intelligence Layer for Enterprise AI](https://arxiv.org/abs/2605.18327)
- [Model Context Protocol servers](https://github.com/modelcontextprotocol/servers)
- [ShopSimulator](https://github.com/ShopAgent-Team/ShopSimulator)
- [DB-GPT](https://github.com/eosphoros-ai/DB-GPT)
