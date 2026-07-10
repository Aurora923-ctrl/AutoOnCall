# AutoOnCall Python Makefile
# 用于自动化项目初始化、Docker 管理、文档向量化和本地质量验证

# ============================================================
# 配置变量
# ============================================================
SERVER_URL = http://localhost:9900
UPLOAD_API = $(SERVER_URL)/api/upload
HEALTH_LIVE_API = $(SERVER_URL)/health/live
HEALTH_READY_API = $(SERVER_URL)/health/ready
DOCS_DIR = aiops-docs
DOCS_GLOBS = $(DOCS_DIR)/*.md $(DOCS_DIR)/*.markdown $(DOCS_DIR)/*.pdf $(DOCS_DIR)/*.html $(DOCS_DIR)/*.htm $(DOCS_DIR)/*.csv $(DOCS_DIR)/*.xlsx
MILVUS_CONTAINER = milvus-standalone
ifeq ($(OS),Windows_NT)
PYTHON ?= .venv/Scripts/python.exe
else
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '.venv/bin/python'; elif [ -x venv/bin/python ]; then printf 'venv/bin/python'; else printf 'python3'; fi)
endif
MYPY_PYTHON_VERSION ?= $(shell $(PYTHON) -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

# 颜色输出
GREEN = \033[0;32m
YELLOW = \033[0;33m
RED = \033[0;31m
CYAN = \033[0;36m
NC = \033[0m

.PHONY: help init bootstrap verify verify-local hygiene-check start stop restart check upload clean up down status wait \
        install install-dev dev run seed-demo demo demo-reports interview-demo interview-demo-all interview-summary interview-ragas test test-quick eval eval-rag eval-ragas eval-change eval-replanner export-bad-cases format format-check lint fix type-check \
        api-contract-verify security pre-commit-install pre-commit check-all coverage docs shell \
        ipython watch add add-dev remove list-docs test-upload sync logs \
        start-cls stop-cls start-monitor stop-monitor start-api stop-api status-mcp \
        interview-up interview-down interview-status sandbox-verify sandbox-demo

# ============================================================
# 默认目标：显示帮助信息
# ============================================================
help:
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)  AutoOnCall - Makefile 命令$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@echo "$(CYAN)【一键操作】$(NC)"
	@echo "  $(YELLOW)make init$(NC)         - 🚀 一键初始化（Docker → 服务 → 上传文档）"
	@echo "  $(YELLOW)make bootstrap$(NC)    - 📦 安装项目和开发工具"
	@echo "  $(YELLOW)make seed-demo$(NC)    - 🎬 生成诊断回放工作台样例数据"
	@echo "  $(YELLOW)make demo$(NC)         - 🎬 生成样例数据并启动本地演示服务"
	@echo "  $(YELLOW)make interview-demo-all$(NC) - 生成面试演示包和统一 eval 摘要"
	@echo "  $(YELLOW)make verify-local$(NC) - ✅ 快速测试 + AIOps/RAG/安全变更评测"
	@echo "  $(YELLOW)make hygiene-check$(NC) - 🧼 检查本地生成产物"
	@echo ""
	@echo "$(CYAN)【Docker 管理】$(NC)"
	@echo "  $(YELLOW)make up$(NC)           - 🐳 启动 Milvus 容器"
	@echo "  $(YELLOW)make down$(NC)         - 🛑 停止 Milvus 容器"
	@echo "  $(YELLOW)make status$(NC)       - 📊 查看容器状态"
	@echo "  $(YELLOW)make interview-up$(NC) - 启动校招核心 Docker 栈"
	@echo "  $(YELLOW)make sandbox-verify$(NC) - 验证核心适配器是否被工具链消费"
	@echo ""
	@echo "$(CYAN)【服务管理】$(NC)"
	@echo "  $(YELLOW)make start$(NC)        - 🚀 启动所有服务（MCP + FastAPI）"
	@echo "  $(YELLOW)make stop$(NC)         - 🛑 停止所有服务（MCP + FastAPI）"
	@echo "  $(YELLOW)make restart$(NC)      - 🔄 重启所有服务"
	@echo "  $(YELLOW)make check$(NC)        - 🔍 检查 FastAPI 服务状态"
	@echo "  $(YELLOW)make status-mcp$(NC)   - 📊 查看 MCP 服务状态"
	@echo ""
	@echo "$(CYAN)【MCP 服务管理】$(NC)"
	@echo "  $(YELLOW)make start-cls$(NC)     - 📋 启动 CLS MCP 服务"
	@echo "  $(YELLOW)make stop-cls$(NC)      - 🛑 停止 CLS MCP 服务"
	@echo "  $(YELLOW)make start-monitor$(NC) - 📊 启动 Monitor MCP 服务"
	@echo "  $(YELLOW)make stop-monitor$(NC)  - 🛑 停止 Monitor MCP 服务"
	@echo "  $(YELLOW)make start-api$(NC)     - 🚀 启动 FastAPI 服务"
	@echo "  $(YELLOW)make stop-api$(NC)      - 🛑 停止 FastAPI 服务"
	@echo ""
	@echo "$(CYAN)【开发模式】$(NC)"
	@echo "  $(YELLOW)make dev$(NC)          - 🔧 开发模式运行（前台，热重载）"
	@echo "  $(YELLOW)make run$(NC)          - 🏭 生产模式运行（前台）"
	@echo ""
	@echo "$(CYAN)【文档管理】$(NC)"
	@echo "  $(YELLOW)make upload$(NC)       - 📤 上传 aiops-docs 目录下的文档"
	@echo "  $(YELLOW)make list-docs$(NC)    - 📚 列出可上传的文档"
	@echo "  $(YELLOW)make test-upload$(NC)  - 🧪 测试上传单个文件"
	@echo ""
	@echo "$(CYAN)【依赖管理】$(NC)"
	@echo "  $(YELLOW)make install$(NC)      - 📦 安装生产依赖"
	@echo "  $(YELLOW)make install-dev$(NC)  - 📦 安装开发依赖"
	@echo "  $(YELLOW)make sync$(NC)         - 🔄 同步依赖"
	@echo "  $(YELLOW)make add PKG=xxx$(NC)  - ➕ 添加依赖包"
	@echo ""
	@echo "$(CYAN)【代码质量】$(NC)"
	@echo "  $(YELLOW)make format$(NC)       - 🎨 格式化代码"
	@echo "  $(YELLOW)make format-check$(NC) - 🎨 检查格式（不修改文件）"
	@echo "  $(YELLOW)make lint$(NC)         - 🔍 代码检查"
	@echo "  $(YELLOW)make type-check$(NC)   - 🔍 类型检查"
	@echo "  $(YELLOW)make security$(NC)     - 🔒 安全检查"
	@echo "  $(YELLOW)make fix$(NC)          - 🔧 自动修复问题"
	@echo "  $(YELLOW)make test$(NC)         - 🧪 运行测试"
	@echo "  $(YELLOW)make eval$(NC)         - 🧪 运行 AIOps 离线评测"
	@echo "  $(YELLOW)make eval-rag$(NC)     - 🧪 运行 RAG 检索离线评测"
	@echo "  $(YELLOW)make eval-ragas$(NC)   - 🧪 运行 RAGAS 质量评测（手动/面试）"
	@echo "  $(YELLOW)make eval-change$(NC)  - 🧪 运行安全变更离线评测"
	@echo "  $(YELLOW)make eval-replanner$(NC) - 🧪 运行 Replanner LLM 决策评测"
	@echo "  $(YELLOW)make api-contract-verify$(NC) - 离线验证 API/SSE/ToolContract 契约"
	@echo "  $(YELLOW)make verify$(NC)       - ✅ 运行只验证门禁（不修改源码）"
	@echo "  $(YELLOW)make check-all$(NC)    - ✅ 兼容入口，等同 make verify"
	@echo ""
	@echo "$(CYAN)【其他】$(NC)"
	@echo "  $(YELLOW)make clean$(NC)        - 🧹 清理临时文件"
	@echo "  $(YELLOW)make shell$(NC)        - 🐍 启动 Python Shell"
	@echo "  $(YELLOW)make coverage$(NC)     - 📊 查看测试覆盖率"
	@echo "  $(YELLOW)make logs$(NC)         - 📜 查看服务日志"
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)使用示例:$(NC)"
	@echo "  1. 一键初始化: $(YELLOW)make init$(NC)"
	@echo "  2. 启动服务:   $(YELLOW)make start$(NC) (自动启动 CLS + Monitor MCP + FastAPI)"
	@echo "  3. 检查状态:   $(YELLOW)make status-mcp$(NC)"
	@echo "  4. 停止服务:   $(YELLOW)make stop$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"

# ============================================================
# 一键初始化
# ============================================================
init:
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)🚀 开始一键初始化 AutoOnCall...$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@echo "$(YELLOW)步骤 1/4: 启动 Docker 容器（Milvus 向量数据库）$(NC)"
	@$(MAKE) up
	@echo ""
	@echo "$(YELLOW)步骤 2/4: 启动 FastAPI 服务$(NC)"
	@$(MAKE) start
	@echo ""
	@echo "$(YELLOW)步骤 3/4: 等待服务就绪$(NC)"
	@$(MAKE) wait
	@echo ""
	@echo "$(YELLOW)步骤 4/4: 上传文档到向量数据库$(NC)"
	@$(MAKE) upload
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)✅ 初始化完成！所有文档已成功向量化存储到数据库$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@echo "$(GREEN)🌐 服务访问地址:$(NC)"
	@echo "   API 服务: $(SERVER_URL)"
	@echo "   API 文档: $(SERVER_URL)/docs"
	@echo "   MinIO: http://localhost:9001 (admin/minioadmin)"
	@echo ""
	@echo "$(YELLOW)💡 提示: 服务正在后台运行$(NC)"
	@echo "   查看日志: $(YELLOW)tail -f server.log$(NC)"
	@echo "   停止服务: $(YELLOW)make stop$(NC)"

# ============================================================
# Docker 管理
# ============================================================

# 启动 Docker 容器（使用 docker compose）
up:
	@echo "$(YELLOW)🐳 检查 Docker 容器状态...$(NC)"
	@if ! docker info > /dev/null 2>&1; then \
		echo "$(YELLOW)⚠️  Docker 未运行，尝试启动 Colima...$(NC)"; \
		colima start 2>/dev/null || (echo "$(RED)❌ 无法启动 Docker，请手动启动$(NC)" && exit 1); \
		sleep 3; \
	fi
	@if docker ps --format '{{.Names}}' | grep -q "^$(MILVUS_CONTAINER)$$"; then \
		echo "$(GREEN)✅ Milvus 容器已经在运行中$(NC)"; \
		docker ps --filter "name=milvus" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | head -10; \
	else \
		echo "$(YELLOW)🚀 启动 Milvus 相关容器...$(NC)"; \
		docker compose -f deploy/compose/vector-database.yml up -d; \
		echo "$(YELLOW)⏳ 等待容器启动...$(NC)"; \
		sleep 5; \
		if docker ps --format '{{.Names}}' | grep -q "^$(MILVUS_CONTAINER)$$"; then \
			echo "$(GREEN)✅ Docker 容器启动成功！$(NC)"; \
			echo ""; \
			echo "$(GREEN)📋 运行中的容器:$(NC)"; \
			docker ps --filter "name=milvus" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | head -10; \
			echo ""; \
			echo "$(GREEN)🌐 服务访问地址:$(NC)"; \
			echo "   Milvus: localhost:19530"; \
			echo "   MinIO: http://localhost:9001 (admin/minioadmin)"; \
		else \
			echo "$(RED)❌ 容器启动失败$(NC)"; \
			exit 1; \
		fi; \
	fi

# 停止 Docker 容器
down:
	@echo "$(YELLOW)🛑 停止 Docker 容器...$(NC)"
	@if docker ps --format '{{.Names}}' | grep -q "milvus"; then \
		docker compose -f deploy/compose/vector-database.yml down; \
		echo "$(GREEN)✅ Docker 容器已停止$(NC)"; \
	else \
		echo "$(YELLOW)⚠️  没有运行中的 Milvus 容器$(NC)"; \
	fi

# 查看容器状态
status:
	@echo "$(YELLOW)📊 Docker 容器状态:$(NC)"
	@echo ""
	@if docker ps -a --format '{{.Names}}' | grep -q "milvus"; then \
		docker ps -a --filter "name=milvus" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"; \
		echo ""; \
		running=$$(docker ps --filter "name=milvus" --format '{{.Names}}' | wc -l | tr -d ' '); \
		total=$$(docker ps -a --filter "name=milvus" --format '{{.Names}}' | wc -l | tr -d ' '); \
		echo "$(GREEN)运行中: $$running / $$total$(NC)"; \
	else \
		echo "$(YELLOW)⚠️  没有找到 Milvus 相关容器$(NC)"; \
		echo "$(YELLOW)提示: 请先创建 Milvus 容器$(NC)"; \
	fi

interview-up:  ## Start interview-focused local Docker stack
	@echo "$(YELLOW)启动校招核心 Docker 栈...$(NC)"
	docker compose -f deploy/compose/interview-stack.yml up -d --remove-orphans
	$(PYTHON) scripts/sandbox/seed_live_incident_evidence.py
	@echo "$(GREEN)校招核心栈已启动: Redis 16379, MySQL 13306, metrics-exporter 19108, Prometheus 19090, Loki 13100$(NC)"
	@echo "$(YELLOW)RAG/Milvus 是加分项，需单独运行 make up && make upload。$(NC)"

interview-down:  ## Stop interview-focused local Docker stack
	@echo "$(YELLOW)停止校招核心 Docker 栈...$(NC)"
	docker compose -f deploy/compose/interview-stack.yml down
	@echo "$(GREEN)校招核心栈已停止$(NC)"

interview-status:  ## Show interview-focused Docker stack containers
	@echo "$(YELLOW)校招核心 Docker 栈状态:$(NC)"
	docker compose -f deploy/compose/interview-stack.yml ps

sandbox-verify:  ## Verify ToolRegistry consumes interview adapter sources
	@echo "$(YELLOW)验证 AIOps 校招核心适配器数据源...$(NC)"
	$(PYTHON) scripts/sandbox/seed_live_incident_evidence.py
	$(PYTHON) scripts/sandbox/verify_full_stack_adapters.py

sandbox-demo:  ## Run deterministic AIOps scenarios against interview adapters
	@echo "$(YELLOW)运行 Redis/MySQL/Prometheus 真实数据流演示...$(NC)"
	$(PYTHON) scripts/sandbox/simulate_mysql_redis_aiops.py

demo-reports:  ## Generate deterministic Redis/MySQL/K8s interview demo reports
	@echo "$(YELLOW)📝 生成 AIOps 面试演示报告...$(NC)"
	$(PYTHON) scripts/demo/generate_demo_reports.py

interview-demo:  ## Build fixed interview demo reports and eval summary package
	@echo "$(YELLOW)📝 生成 AutoOnCall 面试演示包...$(NC)"
	$(PYTHON) scripts/demo/run_interview_demo.py

interview-demo-all:  ## Build the complete interview demo package and rollup summary
	@echo "$(YELLOW)Building complete AutoOnCall interview demo package...$(NC)"
	$(PYTHON) scripts/demo/run_interview_demo.py
	$(PYTHON) scripts/eval/build_interview_summary.py
	@echo "$(GREEN)Interview artifacts ready: logs/interview_demo/README.md and logs/interview_eval_summary.md$(NC)"

interview-summary:  ## Build one interview-facing eval summary from current artifacts
	$(PYTHON) scripts/eval/build_interview_summary.py

interview-ragas:  ## Refresh optional RAGAS quality report for interview demos
	@echo "$(YELLOW)馃И Refreshing interview RAGAS quality snapshot...$(NC)"
	$(PYTHON) scripts/eval/eval_ragas_cases.py --cases eval/rag_cases.yaml --docs-dir aiops-docs --summary-json logs/ragas_eval_summary.json --summary-md logs/ragas_eval_summary.md
	$(PYTHON) scripts/eval/build_interview_summary.py --ragas-summary logs/ragas_eval_summary.json

# ============================================================
# MCP 服务管理
# ============================================================

# 启动 CLS MCP 服务
start-cls:
	@echo "$(YELLOW)📋 启动 CLS MCP 服务...$(NC)"
	@if pgrep -f "mcp_servers/cls_server.py" > /dev/null 2>&1; then \
		echo "$(GREEN)✅ CLS MCP 服务已经在运行中$(NC)"; \
	else \
		echo "$(YELLOW)📦 正在启动 CLS MCP 服务（后台运行）...$(NC)"; \
		nohup $(PYTHON) mcp_servers/cls_server.py > mcp_cls.log 2>&1 & \
		echo $$! > mcp_cls.pid; \
		sleep 2; \
		if pgrep -f "mcp_servers/cls_server.py" > /dev/null 2>&1; then \
			echo "$(GREEN)✅ CLS MCP 服务启动成功$(NC)"; \
			echo "$(YELLOW)   PID: $$(cat mcp_cls.pid)$(NC)"; \
			echo "$(YELLOW)   URL: http://127.0.0.1:8003/mcp$(NC)"; \
			echo "$(YELLOW)   日志: mcp_cls.log$(NC)"; \
		else \
			echo "$(RED)❌ CLS MCP 服务启动失败$(NC)"; \
			echo "$(YELLOW)请检查日志: tail -f mcp_cls.log$(NC)"; \
		fi; \
	fi

# 启动 Monitor MCP 服务
start-monitor:
	@echo "$(YELLOW)📊 启动 Monitor MCP 服务...$(NC)"
	@if pgrep -f "mcp_servers/monitor_server.py" > /dev/null 2>&1; then \
		echo "$(GREEN)✅ Monitor MCP 服务已经在运行中$(NC)"; \
	else \
		echo "$(YELLOW)📦 正在启动 Monitor MCP 服务（后台运行）...$(NC)"; \
		nohup $(PYTHON) mcp_servers/monitor_server.py > mcp_monitor.log 2>&1 & \
		echo $$! > mcp_monitor.pid; \
		sleep 2; \
		if pgrep -f "mcp_servers/monitor_server.py" > /dev/null 2>&1; then \
			echo "$(GREEN)✅ Monitor MCP 服务启动成功$(NC)"; \
			echo "$(YELLOW)   PID: $$(cat mcp_monitor.pid)$(NC)"; \
			echo "$(YELLOW)   URL: http://127.0.0.1:8004/mcp$(NC)"; \
			echo "$(YELLOW)   日志: mcp_monitor.log$(NC)"; \
		else \
			echo "$(RED)❌ Monitor MCP 服务启动失败$(NC)"; \
			echo "$(YELLOW)请检查日志: tail -f mcp_monitor.log$(NC)"; \
		fi; \
	fi

# 停止 Monitor MCP 服务
stop-monitor:
	@echo "$(YELLOW)🛑 停止 Monitor MCP 服务...$(NC)"
	@if [ -f mcp_monitor.pid ]; then \
		pid=$$(cat mcp_monitor.pid); \
		if ps -p $$pid > /dev/null 2>&1; then \
			kill $$pid; \
			echo "$(GREEN)✅ Monitor MCP 服务已停止 (PID: $$pid)$(NC)"; \
		else \
			echo "$(YELLOW)⚠️  进程不存在 (PID: $$pid)$(NC)"; \
		fi; \
		rm -f mcp_monitor.pid; \
	else \
		echo "$(YELLOW)⚠️  未找到 mcp_monitor.pid 文件$(NC)"; \
		pkill -f "mcp_servers/monitor_server.py" 2>/dev/null && \
			echo "$(GREEN)✅ 已停止所有 Monitor MCP 进程$(NC)" || \
			echo "$(YELLOW)⚠️  没有运行中的 Monitor MCP 进程$(NC)"; \
	fi

# 检查 MCP 服务状态
status-mcp:
	@echo "$(YELLOW)📊 MCP 服务状态:$(NC)"
	@echo ""
	@echo "$(CYAN)CLS MCP 服务:$(NC)"
	@if pgrep -f "mcp_servers/cls_server.py" > /dev/null 2>&1; then \
		pid=$$(pgrep -f "mcp_servers/cls_server.py"); \
		echo "  状态: $(GREEN)运行中$(NC)"; \
		echo "  PID: $$pid"; \
		echo "  URL: http://127.0.0.1:8003/mcp"; \
		curl -s http://127.0.0.1:8003/mcp > /dev/null 2>&1 && \
			echo "  连接: $(GREEN)✅ 正常$(NC)" || \
			echo "  连接: $(RED)❌ 无法连接$(NC)"; \
	else \
		echo "  状态: $(RED)未运行$(NC)"; \
	fi
	@echo ""
	@echo "$(CYAN)Monitor MCP 服务:$(NC)"
	@if pgrep -f "mcp_servers/monitor_server.py" > /dev/null 2>&1; then \
		pid=$$(pgrep -f "mcp_servers/monitor_server.py"); \
		echo "  状态: $(GREEN)运行中$(NC)"; \
		echo "  PID: $$pid"; \
		echo "  URL: http://127.0.0.1:8004/mcp"; \
		curl -s http://127.0.0.1:8004/mcp > /dev/null 2>&1 && \
			echo "  连接: $(GREEN)✅ 正常$(NC)" || \
			echo "  连接: $(RED)❌ 无法连接$(NC)"; \
	else \
		echo "  状态: $(RED)未运行$(NC)"; \
	fi
	@echo ""
	@echo "$(CYAN)Math MCP 服务:$(NC)"
	@echo "  状态: $(YELLOW)已移除（示例服务）$(NC)"

# ============================================================
# FastAPI 服务管理
# ============================================================

# 启动所有服务（MCP + FastAPI）
start:
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)🚀 启动所有服务$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@$(MAKE) start-cls
	@sleep 1
	@echo ""
	@$(MAKE) start-monitor
	@sleep 1
	@echo ""
	@$(MAKE) start-api
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)✅ 所有服务启动完成！$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"

# 启动 FastAPI 服务
start-api:
	@echo "$(YELLOW)🚀 启动 FastAPI 服务...$(NC)"
	@if curl -s -f $(HEALTH_LIVE_API) > /dev/null 2>&1; then \
		echo "$(GREEN)✅ FastAPI 服务已经在运行中 ($(SERVER_URL))$(NC)"; \
	else \
		echo "$(YELLOW)📦 正在启动 FastAPI 服务（后台运行）...$(NC)"; \
		nohup $(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 9900 > server.log 2>&1 & \
		echo $$! > server.pid; \
		echo "$(GREEN)✅ FastAPI 服务启动命令已执行$(NC)"; \
		echo "$(YELLOW)   PID: $$(cat server.pid)$(NC)"; \
		echo "$(YELLOW)   URL: $(SERVER_URL)$(NC)"; \
		echo "$(YELLOW)   日志: server.log$(NC)"; \
	fi

# 停止所有服务（FastAPI + MCP）
stop:
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)🛑 停止所有服务$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@$(MAKE) stop-api
	@echo ""
	@$(MAKE) stop-cls
	@echo ""
	@$(MAKE) stop-monitor
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)✅ 所有服务已停止！$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"

# 停止 CLS MCP 服务
stop-cls:
	@echo "$(YELLOW)🛑 停止 CLS MCP 服务...$(NC)"
	@if [ -f mcp_cls.pid ]; then \
		pid=$$(cat mcp_cls.pid); \
		if ps -p $$pid > /dev/null 2>&1; then \
			kill $$pid; \
			echo "$(GREEN)✅ CLS MCP 服务已停止 (PID: $$pid)$(NC)"; \
		else \
			echo "$(YELLOW)⚠️  进程不存在 (PID: $$pid)$(NC)"; \
		fi; \
		rm -f mcp_cls.pid; \
	else \
		echo "$(YELLOW)⚠️  未找到 mcp_cls.pid 文件$(NC)"; \
		pkill -f "mcp_servers/cls_server.py" 2>/dev/null && \
			echo "$(GREEN)✅ 已停止所有 CLS MCP 进程$(NC)" || \
			echo "$(YELLOW)⚠️  没有运行中的 CLS MCP 进程$(NC)"; \
	fi

# 停止 FastAPI 服务
stop-api:
	@echo "$(YELLOW)🛑 停止 FastAPI 服务...$(NC)"
	@if [ -f server.pid ]; then \
		pid=$$(cat server.pid); \
		if ps -p $$pid > /dev/null 2>&1; then \
			kill $$pid; \
			echo "$(GREEN)✅ FastAPI 服务已停止 (PID: $$pid)$(NC)"; \
		else \
			echo "$(YELLOW)⚠️  进程不存在 (PID: $$pid)$(NC)"; \
		fi; \
		rm -f server.pid; \
	else \
		echo "$(YELLOW)⚠️  未找到 server.pid 文件$(NC)"; \
		pkill -f "uvicorn app.main:app" 2>/dev/null && \
			echo "$(GREEN)✅ 已停止所有 uvicorn 进程$(NC)" || \
			echo "$(YELLOW)⚠️  没有运行中的 uvicorn 进程$(NC)"; \
	fi

# 重启所有服务
restart:
	@echo "$(YELLOW)🔄 重启所有服务...$(NC)"
	@echo ""
	@$(MAKE) stop
	@sleep 2
	@$(MAKE) start
	@$(MAKE) wait
	@echo ""
	@echo "$(GREEN)✅ 所有服务重启完成！$(NC)"

# 等待服务就绪（最多 60 秒）
wait:
	@echo "$(YELLOW)⏳ 等待服务器就绪...$(NC)"
	@max_attempts=60; \
	attempt=0; \
	while [ $$attempt -lt $$max_attempts ]; do \
		if curl -s -f $(HEALTH_READY_API) > /dev/null 2>&1; then \
			echo ""; \
			echo "$(GREEN)✅ 服务器已就绪！($(SERVER_URL))$(NC)"; \
			exit 0; \
		fi; \
		attempt=$$((attempt + 1)); \
		printf "\r$(YELLOW)   等待中... [$$attempt/$$max_attempts]$(NC)"; \
		sleep 1; \
	done; \
	echo ""; \
	echo "$(RED)❌ 服务器启动超时！$(NC)"; \
	echo "$(YELLOW)请检查日志: tail -f server.log$(NC)"; \
	exit 1

# 检查服务状态
check:
	@echo "$(YELLOW)🔍 检查服务器状态...$(NC)"
	@if curl -s -f $(HEALTH_LIVE_API) > /dev/null 2>&1; then \
		echo "$(GREEN)✅ 服务器运行正常 ($(SERVER_URL))$(NC)"; \
		echo ""; \
		echo "$(CYAN)Liveness 响应:$(NC)"; \
		curl -s $(HEALTH_LIVE_API) | $(PYTHON) -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))" 2>/dev/null || curl -s $(HEALTH_LIVE_API); \
		echo ""; \
		echo "$(CYAN)Readiness 响应:$(NC)"; \
		curl -s $(HEALTH_READY_API) | $(PYTHON) -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))" 2>/dev/null || curl -s $(HEALTH_READY_API); \
	else \
		echo "$(RED)❌ 服务器未运行或无法连接！$(NC)"; \
		echo "$(YELLOW)请先启动服务: make start$(NC)"; \
		exit 1; \
	fi

# 开发模式运行（前台，热重载）
dev:
	@echo "$(YELLOW)🔧 启动开发服务器（热重载）...$(NC)"
	$(PYTHON) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 9900

# 生产模式运行（前台）
run:
	@echo "$(YELLOW)🏭 启动生产服务器...$(NC)"
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 9900

# 生成本地面试演示数据
seed-demo:
	@echo "$(YELLOW)🎬 生成 AIOps 诊断回放工作台样例数据...$(NC)"
	$(PYTHON) scripts/data/seed_demo_data.py
	@echo "$(GREEN)✅ 样例数据已写入 data/aiops_state.db，评测摘要已写入 logs/eval_summary.json$(NC)"

# 一键演示：生成样例数据并前台启动服务
demo: seed-demo
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)🎬 AutoOnCall 面试演示已准备好$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(YELLOW)打开: $(SERVER_URL)$(NC)"
	$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 9900

# ============================================================
# 文档管理
# ============================================================

# 上传所有文档
upload:
	@echo "$(YELLOW)📤 开始上传 $(DOCS_DIR) 目录下的文档...$(NC)"
	@if [ ! -d "$(DOCS_DIR)" ]; then \
		echo "$(RED)❌ 目录 $(DOCS_DIR) 不存在！$(NC)"; \
		exit 1; \
	fi
	@count=0; \
	success=0; \
	failed=0; \
	for file in $(DOCS_GLOBS); do \
		if [ -f "$$file" ]; then \
			count=$$((count + 1)); \
			filename=$$(basename "$$file"); \
			echo "$(YELLOW)  [$$count] 上传文件: $$filename$(NC)"; \
			response=$$(curl -s -w "\n%{http_code}" -X POST $(UPLOAD_API) \
				-F "file=@$$file" \
				-H "Accept: application/json"); \
			http_code=$$(echo "$$response" | tail -n1); \
			body=$$(echo "$$response" | sed '$$d'); \
			indexing_info=$$(printf '%s' "$$body" | $(PYTHON) -c "import json,sys; payload=json.load(sys.stdin); indexing=(payload.get('data') or {}).get('indexing') or {}; print(f\"{indexing.get('status') or 'unknown'}\t{indexing.get('error_message') or indexing.get('message') or ''}\")" 2>/dev/null || printf 'unknown\tunable to parse response'); \
			indexing_status=$$(printf '%s' "$$indexing_info" | cut -f1); \
			indexing_message=$$(printf '%s' "$$indexing_info" | cut -f2-); \
				if { [ "$$http_code" = "200" ] || [ "$$http_code" = "207" ]; } && [ "$$indexing_status" = "success" ]; then \
					echo "$(GREEN)      ✅ 成功: $$filename$(NC)"; \
					success=$$((success + 1)); \
				elif [ "$$http_code" = "200" ] || [ "$$http_code" = "207" ]; then \
					echo "$(RED)      ❌ 索引未成功: $$filename (status=$$indexing_status)$(NC)"; \
					if [ -n "$$indexing_message" ]; then echo "         $$indexing_message"; fi; \
					failed=$$((failed + 1)); \
			else \
				echo "$(RED)      ❌ 失败: $$filename (HTTP $$http_code)$(NC)"; \
				echo "$$body" | head -n 3; \
				failed=$$((failed + 1)); \
			fi; \
			sleep 1; \
		fi; \
	done; \
	echo ""; \
	echo "$(GREEN)📊 上传统计:$(NC)"; \
	echo "   总计: $$count 个文件"; \
	echo "   $(GREEN)成功: $$success$(NC)"; \
	if [ $$failed -gt 0 ]; then \
		echo "   $(RED)失败: $$failed$(NC)"; \
		echo "$(RED)❌ 文档上传或索引存在失败，请检查 Milvus、DashScope Key 或 server.log$(NC)"; \
		exit 1; \
	fi

# 列出文档
list-docs:
	@echo "$(YELLOW)📚 $(DOCS_DIR) 目录下的文档:$(NC)"
	@if [ -d "$(DOCS_DIR)" ]; then \
		ls -lh $(DOCS_GLOBS) 2>/dev/null || echo "$(RED)没有找到可上传文件$(NC)"; \
	else \
		echo "$(RED)目录 $(DOCS_DIR) 不存在$(NC)"; \
	fi

# 测试上传单个文件
test-upload:
	@echo "$(YELLOW)🧪 测试上传单个文件...$(NC)"
	@first_file=$$(ls $(DOCS_GLOBS) 2>/dev/null | head -n1); \
	if [ -n "$$first_file" ]; then \
		echo "$(YELLOW)上传文件: $$first_file$(NC)"; \
		curl -X POST $(UPLOAD_API) \
			-F "file=@$$first_file" \
			-H "Accept: application/json" | $(PYTHON) -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=False))" 2>/dev/null || \
			curl -X POST $(UPLOAD_API) -F "file=@$$first_file"; \
	else \
		echo "$(RED)测试文件不存在$(NC)"; \
	fi

# ============================================================
# 依赖管理
# ============================================================

bootstrap:  ## 安装项目和开发工具，作为新环境的第一步
	@echo "$(YELLOW)📦 准备 AutoOnCall 开发环境...$(NC)"
	@$(MAKE) install-dev
	@echo "$(GREEN)✅ 开发环境依赖已安装$(NC)"
	@echo "$(YELLOW)下一步可运行: make dev 或 make verify-local$(NC)"

install:  ## 安装依赖（生产环境）
	@echo "$(YELLOW)📦 安装依赖...$(NC)"
	$(PYTHON) -m pip install -r requirements.txt 2>/dev/null || $(PYTHON) -m pip install -e .
	@echo "$(GREEN)✅ 依赖安装完成$(NC)"

install-dev:  ## 安装开发依赖
	@echo "$(YELLOW)📦 安装开发依赖...$(NC)"
	$(PYTHON) -m pip install -e ".[dev]" 2>/dev/null || $(PYTHON) -m pip install -e .
	@echo "$(GREEN)✅ 开发依赖安装完成$(NC)"

sync:  ## 同步依赖
	@echo "$(YELLOW)🔄 同步依赖...$(NC)"
	$(PYTHON) -m pip install -e . --upgrade
	@echo "$(GREEN)✅ 依赖同步完成$(NC)"

add:  ## 添加依赖包 (用法: make add PKG=package_name)
	@echo "$(YELLOW)📦 添加依赖: $(PKG)...$(NC)"
	$(PYTHON) -m pip install $(PKG)

add-dev:  ## 添加开发依赖 (用法: make add-dev PKG=package_name)
	@echo "$(YELLOW)📦 添加开发依赖: $(PKG)...$(NC)"
	$(PYTHON) -m pip install $(PKG)

remove:  ## 移除依赖包 (用法: make remove PKG=package_name)
	@echo "$(YELLOW)🗑️  移除依赖: $(PKG)...$(NC)"
	$(PYTHON) -m pip uninstall $(PKG)

# ============================================================
# 代码质量
# ============================================================

format:  ## 格式化代码
	@echo "$(YELLOW)🎨 格式化代码...$(NC)"
	$(PYTHON) -m ruff check --select I --fix app/ 2>/dev/null || true
	$(PYTHON) -m ruff format app/ 2>/dev/null || $(PYTHON) -m black app/
	@echo "$(GREEN)✅ 格式化完成$(NC)"

format-check:  ## 检查格式（不修改文件）
	@echo "$(YELLOW)🎨 检查代码格式（不修改文件）...$(NC)"
	$(PYTHON) -m ruff format --check app/
	@echo "$(GREEN)✅ 格式检查通过$(NC)"

lint:  ## 代码检查
	@echo "$(YELLOW)🔍 代码检查...$(NC)"
	$(PYTHON) -m ruff check app/
	@echo "$(GREEN)✅ 检查完成$(NC)"

fix:  ## 自动修复代码问题
	@echo "$(YELLOW)🔧 自动修复代码问题...$(NC)"
	$(PYTHON) -m ruff check --fix app/ 2>/dev/null || true
	$(PYTHON) -m ruff format app/ 2>/dev/null || $(PYTHON) -m black app/
	@echo "$(GREEN)✅ 修复完成$(NC)"

type-check:  ## 类型检查
	@echo "$(YELLOW)🔍 类型检查...$(NC)"
	$(PYTHON) -m mypy app/ --ignore-missing-imports --python-version $(MYPY_PYTHON_VERSION)
	@echo "$(GREEN)✅ 类型检查完成$(NC)"

security:  ## 安全检查
	@echo "$(YELLOW)🔒 安全检查...$(NC)"
	$(PYTHON) -m bandit -r app/ -ll
	@echo "$(GREEN)✅ 安全检查完成$(NC)"

test:  ## 运行测试
	@echo "$(YELLOW)🧪 运行测试...$(NC)"
	$(PYTHON) -m pytest tests/ -v --cov=app --cov-report=term-missing --cov-report=html

test-quick:  ## 快速测试
	@echo "$(YELLOW)⚡ 快速测试...$(NC)"
	$(PYTHON) -m pytest tests/ -v

test-integrations:  ## Run Docker/live integration tests
	$(PYTHON) -m pytest tests/ -m integration -v


eval:  ## 运行 AIOps 离线评测
	@echo "$(YELLOW)🧪 运行 AIOps 离线评测...$(NC)"
	$(PYTHON) scripts/eval/eval_cases.py --cases eval/cases.yaml --env-file deploy/sandbox.env --report-path logs/eval_reports.db --summary-json logs/eval_summary.json --summary-md logs/eval_summary.md

eval-rag:  ## 运行 RAG 检索离线评测
	@echo "$(YELLOW)🧪 运行 RAG 检索离线评测...$(NC)"
	$(PYTHON) scripts/eval/eval_rag_cases.py --cases eval/rag_cases.yaml --docs-dir aiops-docs --summary-json logs/rag_eval_summary.json --summary-md logs/rag_eval_summary.md

eval-ragas:  ## Run optional RAGAS quality evaluation for RAG answers
	@echo "$(YELLOW)🧪 Running optional RAGAS quality evaluation...$(NC)"
	$(PYTHON) scripts/eval/eval_ragas_cases.py --cases eval/rag_cases.yaml --docs-dir aiops-docs --summary-json logs/ragas_eval_summary.json --summary-md logs/ragas_eval_summary.md

eval-change:  ## 运行安全变更离线评测
	@echo "$(YELLOW)🧪 运行安全变更离线评测...$(NC)"
	$(PYTHON) scripts/eval/eval_change_cases.py --cases eval/change_cases.yaml --summary-json logs/change_eval_summary.json --summary-md logs/change_eval_summary.md

eval-replanner:  ## 运行 Replanner LLM 决策离线评测
	@echo "$(YELLOW)🧪 运行 Replanner LLM 决策离线评测...$(NC)"
	$(PYTHON) scripts/eval/eval_replanner_cases.py --cases eval/replanner_cases.yaml --summary-json logs/replanner_eval_summary.json --summary-md logs/replanner_eval_summary.md

export-bad-cases:  ## Export high-value feedback into reviewable eval backlog drafts
	@echo "$(YELLOW)Exporting bad cases into reviewable eval backlog drafts...$(NC)"
	$(PYTHON) scripts/eval/export_bad_cases.py

api-contract-verify:  ## Offline API/SSE/ToolContract compatibility verification
	@echo "$(YELLOW)Verifying API/SSE/ToolContract compatibility offline...$(NC)"
	$(PYTHON) scripts/eval/verify_api_contracts.py

hygiene-check:  ## 检查本地生成产物
	@echo "$(YELLOW)🧼 检查本地生成产物...$(NC)"
	$(PYTHON) scripts/maintenance/hygiene_check.py

verify-local:  ## 面试前本地快速质量验证
	@echo "$(YELLOW)✅ 运行 AutoOnCall 本地快速验证...$(NC)"
	@$(MAKE) test-quick
	@$(MAKE) eval
	@$(MAKE) eval-rag
	@$(MAKE) eval-change
	@$(MAKE) eval-replanner
	@echo "$(GREEN)✅ 本地快速验证完成$(NC)"

verify:  ## 运行只验证门禁（不修改源码）
	@echo "$(YELLOW)🚀 运行 AutoOnCall 交付门禁（不修改源码）...$(NC)"
	@$(MAKE) format-check
	@$(MAKE) lint
	@$(MAKE) type-check
	@$(MAKE) security
	@$(MAKE) test-quick
	@$(MAKE) eval
	@$(MAKE) eval-rag
	@$(MAKE) eval-change
	@$(MAKE) eval-replanner
	@$(MAKE) hygiene-check
	@echo "$(GREEN)✅ 交付门禁通过！$(NC)"

check-all:  ## 兼容入口：等同 make verify
	@$(MAKE) verify

pre-commit-install:  ## 安装 pre-commit hooks
	@echo "$(YELLOW)🔗 安装 pre-commit hooks...$(NC)"
	$(PYTHON) -m pre_commit install
	$(PYTHON) -m pre_commit install --hook-type commit-msg
	@echo "$(GREEN)✅ Pre-commit hooks 安装完成$(NC)"

pre-commit:  ## 运行 pre-commit 检查
	@echo "$(YELLOW)🔍 运行 pre-commit 检查...$(NC)"
	$(PYTHON) -m pre_commit run --all-files

coverage:  ## 查看测试覆盖率报告
	@echo "$(YELLOW)📊 生成覆盖率报告...$(NC)"
	$(PYTHON) -m pytest tests/ --cov=app --cov-report=html --cov-report=term
	@echo "$(GREEN)✅ 覆盖率报告已生成: htmlcov/index.html$(NC)"
	@open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html 2>/dev/null || echo "请手动打开 htmlcov/index.html"

# ============================================================
# 其他工具
# ============================================================

clean:  ## 清理临时文件
	@echo "$(YELLOW)🧹 清理临时文件...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage
	rm -f server.pid server.log
	rm -f mcp_cls.pid mcp_cls.log
	rm -f mcp_monitor.pid mcp_monitor.log
	rm -rf uploads/*.tmp 2>/dev/null || true
	@echo "$(GREEN)✅ 清理完成$(NC)"

shell:  ## 启动 Python shell
	@echo "$(YELLOW)🐍 启动 Python shell...$(NC)"
	$(PYTHON) -i -c "import sys; sys.path.insert(0, '.'); from app.config import config; print('环境已加载，config 对象可用')"

ipython:  ## 启动 IPython shell
	@echo "$(YELLOW)🐍 启动 IPython shell...$(NC)"
	$(PYTHON) -m IPython

docs:  ## 打开 API 文档
	@echo "$(YELLOW)📚 API 文档地址: $(SERVER_URL)/docs$(NC)"
	@open $(SERVER_URL)/docs 2>/dev/null || xdg-open $(SERVER_URL)/docs 2>/dev/null || echo "请手动打开 $(SERVER_URL)/docs"

watch:  ## 监视文件变化并自动运行测试
	@echo "$(YELLOW)👀 监视文件变化...$(NC)"
	$(PYTHON) -m pytest_watch -- -v

logs:  ## 查看服务日志
	@echo "$(YELLOW)📜 查看服务日志...$(NC)"
	@if [ -f server.log ]; then \
		tail -f server.log; \
	else \
		echo "$(RED)日志文件不存在$(NC)"; \
		echo "$(YELLOW)提示: 使用 make start 启动服务后会生成日志$(NC)"; \
	fi
