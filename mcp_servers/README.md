# MCP Servers

`mcp_servers/` 保留两类本地演示 MCP 服务，用于没有真实外部系统时给 AIOps Agent 提供可控的 mock/fallback 工具数据。

当前主诊断链路以 `app/tools/registry.py` 的 Tool Registry 为准；真实数据优先通过 `app/integrations/` 中的 Alertmanager、Prometheus、Loki/日志网关、Kubernetes、Redis、MySQL、CMDB、发布历史和工单适配器读取。MCP 服务不是生产主入口。

10 分钟面试主线不依赖 MCP。只有在面试官追问“外部系统没配置时怎么降级”时，再用本页解释 MCP mock/fallback；如果要展示适配器真实取证，请优先看 `deploy/sandbox.md` 并关闭 mock fallback。

## 服务

| 服务 | 文件 | 默认地址 | 用途 |
| --- | --- | --- | --- |
| CLS mock | `cls_server.py` | `http://127.0.0.1:8003/mcp` | 本地日志查询、日志模式分析 |
| Monitor mock | `monitor_server.py` | `http://127.0.0.1:8004/mcp` | 本地指标、服务信息、历史工单 mock |

## 启动

通过 Makefile：

```bash
make start-cls
make start-monitor
make status-mcp
```

或随 FastAPI 一起启动：

```bash
make start
```

手动启动：

```bash
python mcp_servers/cls_server.py
python mcp_servers/monitor_server.py
```

## 配置

默认配置在 `app/config.py`：

```text
MCP_CLS_TRANSPORT=streamable-http
MCP_CLS_URL=http://localhost:8003/mcp
MCP_MONITOR_TRANSPORT=streamable-http
MCP_MONITOR_URL=http://localhost:8004/mcp
```

## 边界

- 这些服务返回本地演示数据，不代表真实生产日志或监控。
- 严格验收或生产化演示应优先使用 `deploy/compose/full-stack-compose.yml` 和真实适配器，并设置 `AIOPS_MOCK_FALLBACK_ENABLED=false`。
- MCP 服务适合解释 fallback 机制，不适合宣传成已经接入生产系统。
