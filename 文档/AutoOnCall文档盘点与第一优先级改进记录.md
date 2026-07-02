# AutoOnCall 文档盘点与第一优先级改进记录

本文记录一次围绕“校招生第一优先级：整理稳定面试演示链路”的文档审阅和改进。目标不是重写所有文档，而是让项目在面试中有一条稳定、可信、可复现的讲解主线。

## 1. 审阅范围

本次盘点覆盖仓库内全部 Markdown 文档：

| 分组 | 文档数量 | 范围 |
| --- | ---: | --- |
| 仓库入口与运维文档 | 5 | `README.md`、`AGENTS.md`、`deploy/*.md`、`mcp_servers/README.md` |
| 面试与分析文档 | 4 | 项目分析书、从零上手与面试讲解、校招生评估建议、提示词索引 |
| 技术长文 | 13 | `文档/技术长文/*.md` |
| Runbook 知识库 | 5 | `aiops-docs/*.md` |

本次使用子 agent 分组审阅，每组逐篇阅读并给出是否需要修改、如何服务 10 分钟演示链路的建议。主 agent 负责汇总判断和实际改文档。

## 2. 第一优先级目标

第一优先级不是新增功能，而是完成一条稳定面试主线：

```text
RAG 可信问答
-> demo Incident 诊断
-> Plan-Execute-Replan
-> 工具调用和 Evidence
-> Trace / Report
-> 风险审批或 forbidden
-> 离线评测说明
```

完成标准：

- 10 分钟内能讲完，不需要临场翻代码找入口。
- README 是外部入口，面试讲解文档是主手册。
- 固定 demo case 使用同一套名称：`redis_maxclients`、`mysql_slow_query`、`pod_crashloop`、`forbidden_sql`。
- RAG 至少有一个正例和一个拒答负例。
- AIOps 至少能展示 Plan、ToolCall、Evidence、Trace、Report。
- mock / fallback / sandbox / 真实适配器边界有固定说法。
- 离线评测只说“稳定回归”，不说成线上准确率。

## 3. 已完成改进

| 文件 | 改动 |
| --- | --- |
| `README.md` | 新增并强化“面试演示路径（10 分钟）”，补 RAG 三问、固定 AIOps demo case、质量验证、双终端启动方式、演示前检查清单 |
| `文档/AutoOnCall项目从零上手与面试讲解.md` | 将“推荐演示路径”扩展成 10 分钟脚本、RAG 演示问题、固定 demo case、前端观察点、验证命令和面试讲法 |
| `文档/AutoOnCall校招生项目评估与改进建议.md` | 在第一优先级中补充“完成标准”，避免建议停留在口号 |
| `文档/AutoOnCall项目分析书.md` | 增加推荐演示入口，指向 README 和面试讲解文档，统一 demo case 与边界说法 |
| `文档/提示词/技术长文提示词索引.md` | 增加统一 demo case 和边界表达要求，防止后续长文口径发散 |
| `deploy/sandbox.md` | 增加面试快速路径说明，明确 sandbox 是进阶适配器演示，不是 10 分钟主线 |
| `deploy/production.md` | 增加生产化附录定位，避免把本地 demo 说成生产平台 |
| `mcp_servers/README.md` | 明确 MCP 不属于 10 分钟主线，只用于解释 mock/fallback |

## 4. 逐组审阅结论

### 4.1 仓库入口与运维文档

| 文档 | 当前作用 | 结论 |
| --- | --- | --- |
| `README.md` | 项目主入口、快速开始、能力说明 | 已重点改进，应作为唯一外部主剧本 |
| `AGENTS.md` | 协作者规范 | 不作为面试主线材料，可后续补文档维护原则 |
| `deploy/sandbox.md` | full-stack 本地沙箱 | 已补面试快速路径；作为进阶真实适配器演示 |
| `deploy/production.md` | 生产配置边界 | 已标注为生产化附录；适合追问，不适合主线 |
| `mcp_servers/README.md` | MCP mock/fallback 说明 | 已标注不依赖于 10 分钟主线 |

### 4.2 面试与分析文档

| 文档 | 当前作用 | 结论 |
| --- | --- | --- |
| `文档/AutoOnCall项目从零上手与面试讲解.md` | 面试主手册 | 已重点改进，作为 10 分钟演示执行稿 |
| `文档/AutoOnCall项目分析书.md` | 项目可信度和评分说明 | 已补演示入口，不扩成操作手册 |
| `文档/AutoOnCall校招生项目评估与改进建议.md` | 改进路线图 | 已补完成标准；后续继续指导第二优先级 |
| `文档/提示词/技术长文提示词索引.md` | 技术长文生成约束 | 已统一 demo 口径和边界说法 |

### 4.3 技术长文

技术长文整体内容扎实，不建议为了第一优先级逐篇大改。它们更适合作为面试追问材料。

| 文档 | 面试用途 | 是否本次改 |
| --- | --- | --- |
| 第一篇-项目总览与架构设计 | 开场定位和全局架构 | 不改，已由 README/分析书承接演示入口 |
| 第二篇-代码结构与工程分层 | 被问“代码怎么读”时使用 | 不改 |
| 第三篇-告警接入与故障生命周期 | 被问 Alertmanager 接入时使用 | 不改 |
| 第四篇-智能运维诊断主链路 | AIOps 主链路深挖 | 不改 |
| 第五篇-计划执行复盘机制 | Planner/Executor/Replanner 深挖 | 不改 |
| 第六篇-证据轨迹与报告沉淀机制 | 解释 Evidence/Trace/Report | 不改 |
| 第七篇-人工审批与安全变更链路 | 解释风险审批和 dry-run | 不改 |
| 第八篇-知识库检索增强问答链路 | 解释 RAG 引用和拒答 | 不改 |
| 第九篇-外部系统适配器与工具层设计 | 解释工具和适配器 | 不改 |
| 第十篇-接口权限与健康检查设计 | 解释 RBAC/health | 不改 |
| 第十一篇-存储与状态模型设计 | 解释状态持久化 | 不改 |
| 第十二篇-测试体系与工程化质量保障 | 解释测试/eval 体系 | 不改 |
| 第十三篇-校招面试总复盘 | 面试总控稿 | 后续可把演示脚本前置，但本次先由面试讲解文档承接 |

后续如果继续打磨技术长文，建议只在每篇开头或结尾加一句“这篇在 10 分钟演示里承担什么角色”，不要重写正文。

### 4.4 Runbook 知识库

| Runbook | 面试适配度 | 结论 |
| --- | --- | --- |
| `aiops-docs/slow_response.md` | 很高 | 最适合 RAG + 多源诊断演示 |
| `aiops-docs/cpu_high_usage.md` | 高 | 适合 RAG 正例和简单排障说明 |
| `aiops-docs/service_unavailable.md` | 高但范围大 | 适合作为高严重度场景，演示时需固定分支 |
| `aiops-docs/disk_high_usage.md` | 中高 | 适合展示“只诊断不误删”的安全边界 |
| `aiops-docs/memory_high_usage.md` | 中 | 偏 JVM，可作为后续补充，不建议第一批主 demo |

本次没有修改 Runbook。后续可给 `slow_response.md`、`cpu_high_usage.md`、`service_unavailable.md` 各补一组固定样例，包括告警输入、关键日志、关键指标和预期结论。

## 5. 当前推荐演示顺序

### 5.1 主线

```text
README 打开项目定位
-> 启动 FastAPI / 上传 Runbook
-> RAG 正例：CPU 使用率过高怎么排查？
-> RAG 负例：公司年假怎么申请？
-> AIOps 主 demo：redis_maxclients
-> 看 Plan、ToolCall、Evidence、Trace、Report
-> 安全边界短 demo：forbidden_sql
-> 展示 test/eval 命令和边界说法
```

### 5.2 推荐讲法

> 我这条演示不追求把所有组件都跑一遍，而是证明这不是一个普通聊天框：知识回答有可信来源和拒答，故障诊断通过工具取证形成 Evidence，报告可追溯，风险动作会审批或禁止，离线评测用于防止核心链路漂移。

### 5.3 不建议主线展开的内容

- SSO/OIDC。
- 多租户权限。
- 完整 Kubernetes / Helm 部署。
- Tempo / Jaeger / Redpanda / Grafana 全组件演示。
- 自动生产修复。
- 数据库迁移平台。
- 复杂多 Agent 概念。

这些内容可以作为追问材料，而不是校招生 10 分钟主线。

## 6. 后续建议

第一优先级已经完成到“文档入口可演示”的程度。后续可以按以下顺序继续：

1. 给 README 演示路径配一组截图或录屏。
2. 给 `slow_response.md`、`cpu_high_usage.md`、`service_unavailable.md` 补固定样例。
3. 把第十三篇技术长文的“本地演示脚本”前置并同步 README demo case。
4. 增加 5-8 个高质量 eval case，进入第二优先级。
5. 轻量拆分 1-2 个大文件，进入工程质量优先级。
