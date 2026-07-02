# Repository Guidelines

## 项目结构与模块组织

本仓库是一个 Python 3.11 FastAPI 应用，用于 RAG 对话和 AIOps 智能诊断。

- `app/main.py` 是应用入口。
- `app/api/` 存放聊天、文件上传、AIOps、健康检查等路由。
- `app/services/` 存放 RAG、向量、Embedding、文档处理等业务服务。
- `app/services/aiops_read_models/` 存放 AIOps 运行、Incident 概览、Replay 和评测相关读模型，`app/services/read_models.py` 仅保留兼容导出。
- `app/agent/aiops/` 实现 Plan-Execute-Replan 诊断流程。
- `app/models/` 定义 Pydantic 数据模型。
- `app/core/`、`app/tools/`、`app/utils/` 存放基础设施、Agent 工具和日志辅助代码。
- `app/integrations/` 存放 Prometheus、日志网关、Kubernetes、Redis、MySQL、工单系统等外部系统适配器。
- `mcp_servers/` 存放 MCP 服务脚本。
- `static/` 存放前端工作台页面；`static/app.js` 只负责按顺序加载分片，业务脚本放在 `static/js/`。
- `aiops-docs/` 存放写入 Milvus 的 Markdown 知识库文档。
- `deploy/` 存放生产部署与配置示例。

不要提交虚拟环境、日志、覆盖率报告、临时上传文件或生成产物。

## 构建、测试与开发命令

- `pip install -e ".[dev]"` 安装项目和开发工具。
- `make install-dev` 在支持 GNU Make 的环境中执行同类安装。
- `make dev` 以热重载方式在 `9900` 端口启动 FastAPI。
- `make run` 以非热重载方式启动 FastAPI。
- `make up` 使用 `deploy/compose/vector-database.yml` 启动 Milvus。
- `make start` 启动 MCP 服务和 FastAPI。
- `.\scripts\dev\start-windows.bat`、`.\scripts\dev\stop-windows.bat` 用于 Windows 下启动和停止服务。
- `make upload` 将 `aiops-docs/*.md` 上传到正在运行的 API。

## 编码风格与命名约定

使用 4 空格缩进，并在有助于理解时添加类型注解。沿用现有命名风格：文件名小写，函数和变量使用 `snake_case`，类使用 `PascalCase`，Pydantic 模型放在 `app/models/`。

格式化和导入整理使用 Ruff、Black、isort，行宽为 100：

- `make format` 格式化 `app/`。
- `make lint` 执行 Ruff 检查。
- `make fix` 应用 Ruff 自动修复并格式化。
- `make type-check` 对 `app/` 运行 mypy。

## 测试指南

pytest 配置期望存在顶层 `tests/` 目录。测试文件命名为 `test_*.py` 或 `*_test.py`，测试类命名为 `Test*`，测试函数命名为 `test_*`。异步测试使用 `pytest-asyncio`。

当前检出版本已包含仓库级测试，覆盖 AIOps 模型、工具注册表、证据分析、审批、Trace、报告和离线评测等核心链路。修改 API、服务或 Agent 行为时，请在 `tests/` 下补充或更新对应测试。使用 `make test` 运行覆盖率测试，或使用 `make test-quick` 快速验证。

## 提交与 Pull Request 规范

当前检出版本无法读取 Git 历史，因此请使用清晰的祈使句提交标题，例如 `Add AIOps upload validation` 或 `Fix Milvus connection retry`。每个提交应聚焦一个变更。

PR 应包含变更摘要、影响的路由或服务、配置变更和验证命令。有关联 issue 时请链接；涉及 UI 或接口行为变化时，请附截图或 API 示例。

## 安全与配置提示

不要提交 `.env`、API Key、日志、上传文件或 Milvus 数据卷。运行时配置包含 DashScope 和 Milvus 相关设置；新增环境变量时，请同步更新 `README.md`，并在 `app/config.py` 中保留安全默认值。
