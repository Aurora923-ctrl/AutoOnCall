// Workbench navigation, API client helpers, incidents, alerts, and run history.
Object.assign(window.AutoOnCallApp.prototype, {
    toggleModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭模式下拉菜单
,
    closeModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 选择模式
,
    selectMode(mode) {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再切换模式', 'warning');
            return;
        }
        
        this.currentMode = mode;
        this.updateUI();
        
        const modeNames = {
            'quick': '快速',
            'stream': '流式'
        };
        
        this.showNotification(`已切换到${modeNames[mode]}模式`, 'info');
    }

    // 更新UI
,
    updateUI() {
        // 更新模式选择器显示
        if (this.currentModeText) {
            const modeNames = {
                'quick': '快速',
                'stream': '流式'
            };
            this.currentModeText.textContent = modeNames[this.currentMode] || '快速';
        }
        
        // 更新下拉菜单选中状态
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            const mode = item.getAttribute('data-mode');
            if (mode === this.currentMode) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
        
        // 更新发送按钮状态
        if (this.sendButton) {
            this.sendButton.disabled = this.isStreaming;
        }

        if (this.aiOpsSidebarBtn) {
            this.aiOpsSidebarBtn.disabled = this.isStreaming;
        }

        const aiOpsSubmitBtn = document.getElementById('aiOpsSubmitBtn');
        if (aiOpsSubmitBtn) {
            aiOpsSubmitBtn.disabled = this.isStreaming;
        }
        
        // 更新输入框状态
        if (this.messageInput) {
            this.messageInput.disabled = this.isStreaming;
            this.messageInput.placeholder = '问问 AutoOnCall';
        }

        this.updateWorkbenchNavState();
    }
,
    updateWorkbenchNavState() {
        if (!this.workbenchNavButtons) return;
        this.workbenchNavButtons.forEach((button) => {
            const view = button.getAttribute('data-workbench-view') || 'chat';
            button.classList.toggle('active', view === this.currentWorkbenchView);
        });
    }
,
    updateIncidentTabNavState() {
        if (this.incidentTabNav) {
            this.incidentTabNav.hidden = this.currentWorkbenchView !== 'incidents';
        }
        if (!this.incidentTabButtons) return;
        this.incidentTabButtons.forEach((button) => {
            const tab = button.getAttribute('data-incident-tab') || 'overview';
            const isActive = tab === this.currentIncidentTab;
            button.classList.toggle('active', isActive);
            button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        });
    }
,
    async loadInitialWorkbenchData() {
        await Promise.allSettled([
            this.refreshHealthStatus(),
            this.refreshIncidents(),
            this.refreshApprovals(),
            this.refreshEvalSummary(),
            this.refreshAdapterVerification(),
            this.refreshToolContracts()
        ]);
    }
,
    resolveWorkbenchTarget(view) {
        const targets = {
            aiops: { view: 'incidents', incidentTab: 'process' },
            trace: { view: 'incidents', incidentTab: 'evidence' },
            report: { view: 'incidents', incidentTab: 'report' },
            approvals: { view: 'response' },
            changes: { view: 'response' },
            eval: { view: 'system' },
            adapters: { view: 'system' },
            health: { view: 'system' }
        };
        return targets[view] || { view: view || 'chat' };
    }
,
    normalizeWorkbenchView(view) {
        return this.resolveWorkbenchTarget(view).view;
    }
,
    async setWorkbenchView(view) {
        const target = this.resolveWorkbenchTarget(view);
        this.currentWorkbenchView = target.view;
        if (target.incidentTab) {
            this.currentIncidentTab = target.incidentTab;
        }
        const isChatView = this.currentWorkbenchView === 'chat';

        if (this.mainContent) {
            this.mainContent.classList.toggle('workbench-active', !isChatView);
            this.mainContent.dataset.workbenchView = this.currentWorkbenchView;
        }
        if (this.workbenchPanel) {
            this.workbenchPanel.hidden = isChatView;
        }

        this.updateWorkbenchTitle();
        this.updateWorkbenchPanelVisibility();
        this.updateWorkbenchNavState();
        this.updateIncidentTabNavState();
        if (this.currentWorkbenchView === 'response') {
            this.setResponseAttention(false);
        }

        if (isChatView) {
            this.checkAndSetCentered();
            return;
        }

        await this.refreshWorkbenchData(this.currentWorkbenchView);
    }
,
    updateWorkbenchTitle() {
        if (!this.workbenchTitle) return;
        const titles = {
            incidents: '故障诊断中心',
            response: '处置中心',
            system: '环境就绪中心'
        };
        this.workbenchTitle.textContent = titles[this.currentWorkbenchView] || 'AutoOnCall 工作台';
    }
,
    setIncidentTab(tab) {
        const allowedTabs = ['overview', 'process', 'evidence', 'report', 'response'];
        this.currentIncidentTab = allowedTabs.includes(tab) ? tab : 'overview';
        if (this.currentWorkbenchView !== 'incidents') {
            this.currentWorkbenchView = 'incidents';
            if (this.mainContent) {
                this.mainContent.classList.add('workbench-active');
                this.mainContent.dataset.workbenchView = 'incidents';
            }
            if (this.workbenchPanel) {
                this.workbenchPanel.hidden = false;
            }
            this.updateWorkbenchTitle();
            this.updateWorkbenchNavState();
        }
        this.updateWorkbenchPanelVisibility();
        this.updateIncidentTabNavState();
    }
,
    updateWorkbenchPanelVisibility() {
        if (!this.workbenchPanel) return;
        const view = this.currentWorkbenchView;
        const incidentTabPanels = {
            overview: ['incidents', 'alerts', 'diagnosis-launch', 'run-history', 'detail', 'conclusion'],
            process: ['incidents', 'replay', 'plan', 'steps', 'tools'],
            evidence: ['incidents', 'dependencies', 'evidence', 'trace'],
            report: ['incidents', 'report'],
            response: ['incidents', 'approvals', 'changes', 'detail']
        };
        const visibility = {
            incidents: incidentTabPanels[this.currentIncidentTab] || incidentTabPanels.overview,
            response: ['approvals', 'changes', 'incidents', 'detail', 'trace'],
            system: ['health', 'adapters', 'tool-contracts', 'eval']
        };
        const visiblePanels = visibility[view] || ['incidents', 'detail'];
        this.workbenchPanel.querySelectorAll('[data-panel]').forEach((panel) => {
            const panelName = panel.getAttribute('data-panel');
            panel.classList.toggle('is-muted', !visiblePanels.includes(panelName));
        });
    }
,
    async refreshWorkbenchData(view) {
        const tasks = [this.refreshHealthStatus()];
        if (['incidents', 'response'].includes(view)) {
            tasks.push(this.refreshIncidents());
            tasks.push(this.refreshAlerts());
            tasks.push(this.refreshAIOpsRuns());
        }
        if (view === 'response') {
            tasks.push(this.refreshApprovals());
        }
        if (view === 'system') {
            tasks.push(this.refreshEvalSummary());
            tasks.push(this.refreshAdapterVerification());
            tasks.push(this.refreshToolContracts());
        }
        await Promise.allSettled(tasks);

        if (this.selectedIncidentId && ['incidents', 'response'].includes(view)) {
            await this.refreshSelectedIncidentPanels();
        }
    }
,
    renderAuthTokenState() {
        const token = (localStorage.getItem('autooncallApiToken') || '').trim();
        if (this.apiTokenInput) {
            this.apiTokenInput.value = token;
        }
        if (this.authStatusBadge) {
            this.authStatusBadge.textContent = token ? '已设置' : '未设置';
            this.authStatusBadge.className = token ? 'ready' : '';
        }
    }
,
    saveApiToken() {
        const token = (this.apiTokenInput?.value || '').trim();
        if (token) {
            localStorage.setItem(this.apiTokenStorageKey, token);
            this.showNotification('接口令牌已保存', 'success');
        } else {
            localStorage.removeItem(this.apiTokenStorageKey);
            this.showNotification('接口令牌已清除', 'info');
        }
        this.renderAuthTokenState();
        this.refreshWorkbenchData(this.currentWorkbenchView);
    }
,
    clearApiToken() {
        localStorage.removeItem(this.apiTokenStorageKey);
        if (this.apiTokenInput) {
            this.apiTokenInput.value = '';
        }
        this.renderAuthTokenState();
        this.showNotification('接口令牌已清除', 'info');
    }
,
    async apiGet(path) {
        const response = await this.apiFetch(path);
        if (!response.ok) {
            throw new Error(`HTTP错误: ${response.status}`);
        }
        return response.json();
    }
,
    async apiGetWithStatus(path) {
        const response = await this.apiFetch(path);
        try {
            const data = await response.json();
            return {
                ok: response.ok,
                status: response.status,
                data
            };
        } catch (error) {
            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }
            throw error;
        }
    }
,
    authHeaders(headers = {}) {
        const normalizedHeaders = { ...headers };
        const token = (localStorage.getItem('autooncallApiToken') || '').trim();
        if (token) {
            normalizedHeaders['X-AutoOnCall-Token'] = token;
        }
        return normalizedHeaders;
    }
,
    async apiFetch(path, options = {}) {
        return fetch(path, {
            ...options,
            headers: this.authHeaders(options.headers || {})
        });
    }
,
    async refreshHealthStatus() {
        try {
            const [liveResult, readyResult] = await Promise.all([
                this.apiGetWithStatus('/health/live'),
                this.apiGetWithStatus('/health/ready')
            ]);
            const live = liveResult.data.data || liveResult.data;
            const ready = readyResult.data.data || readyResult.data;
            const health = this.mergeHealthChecks(live, ready, liveResult.status, readyResult.status);
            this.dashboardState.health = health;
            this.renderHealthSummary(health);
        } catch (error) {
            this.renderHealthError(error);
        }
    }
,
    mergeHealthChecks(live, ready, liveHttpStatus, readyHttpStatus) {
        const readyChecks = ready.checks || {};
        const liveChecks = live.checks || {};
        return {
            ...ready,
            live_status: live.status || 'unknown',
            liveness_http_status: liveHttpStatus,
            readiness_http_status: readyHttpStatus,
            checks: {
                ...readyChecks,
                liveness: liveChecks.process || {}
            }
        };
    }
,
    renderHealthSummary(health) {
        const status = health.status || 'unknown';
        const mode = health.mode || 'unknown';
        const externalSystems = health.checks?.external_systems || {};
        const mockEnabled = externalSystems.mock_fallback_enabled;
        const milvusStatus = health.checks?.milvus?.status || health.milvus?.status || 'unknown';
        const externalStatus = externalSystems.status || 'unknown';
        const liveStatus = health.live_status || 'unknown';
        const readinessHttpStatus = health.readiness_http_status || 'unknown';
        const livenessHttpStatus = health.liveness_http_status || 'unknown';
        const capabilities = health.capabilities || {};
        const ragCapability = capabilities.rag || {};
        const aiopsCapability = capabilities.aiops || {};

        if (this.healthStatusPill) {
            this.healthStatusPill.textContent = `ready=${status} · live=${liveStatus}`;
            this.healthStatusPill.className = `status-pill ${status === 'healthy' ? 'healthy' : 'degraded'}`;
        }
        if (this.healthMode) {
            this.healthMode.textContent = `${mockEnabled ? 'Mock fallback on' : 'Strict mode'} · ${mode}`;
        }
        if (this.healthSummary) {
            this.healthSummary.innerHTML = `
                <div class="detail-grid">
                    <div class="detail-field">
                        <span>服务</span>
                        <strong>${this.escapeHtml(health.service || 'AutoOnCall')}</strong>
                    </div>
                    <div class="detail-field">
                        <span>版本</span>
                        <strong>${this.escapeHtml(health.version || 'unknown')}</strong>
                    </div>
                    <div class="detail-field">
                        <span>运行模式</span>
                        <strong>${this.escapeHtml(mode)}</strong>
                    </div>
                    <div class="detail-field">
                        <span>Liveness</span>
                        <strong>${this.escapeHtml(String(livenessHttpStatus))} / ${this.escapeHtml(liveStatus)}</strong>
                    </div>
                    <div class="detail-field">
                        <span>Readiness</span>
                        <strong>${this.escapeHtml(String(readinessHttpStatus))} / ${this.escapeHtml(status)}</strong>
                    </div>
                    <div class="detail-field">
                        <span>Milvus</span>
                        <strong>${this.escapeHtml(milvusStatus)}</strong>
                    </div>
                    <div class="detail-field">
                        <span>RAG 能力</span>
                        <strong>${this.escapeHtml(ragCapability.status || 'unknown')}</strong>
                    </div>
                    <div class="detail-field">
                        <span>AIOps 能力</span>
                        <strong>${this.escapeHtml(aiopsCapability.status || 'unknown')}</strong>
                    </div>
                    <div class="detail-field">
                        <span>外部系统</span>
                        <strong>${this.escapeHtml(externalStatus)}</strong>
                    </div>
                    <div class="detail-field">
                        <span>Mock 回退</span>
                        <strong>${mockEnabled ? 'enabled' : 'disabled'}</strong>
                    </div>
                </div>
            `;
        }
        this.renderAdapterVerification(this.dashboardState.adapterVerification);
    }
,
    renderHealthError(error) {
        if (this.healthStatusPill) {
            this.healthStatusPill.textContent = '状态不可用';
            this.healthStatusPill.className = 'status-pill error';
        }
        if (this.healthSummary) {
            this.healthSummary.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
        }
    }
,
    async loadAIOpsStatusCatalog() {
        if (!this.aiOpsRunStatusFilter) return;
        try {
            const payload = await this.apiGet(`${this.apiBaseUrl}/aiops/status-catalog`);
            const items = Array.isArray(payload.items) ? payload.items : [];
            if (!items.length) return;
            this.aiOpsStatusCatalog = items;
            this.renderAIOpsRunStatusFilter(items);
        } catch (error) {
            console.warn('加载 AIOps 状态目录失败，使用静态兜底选项:', error);
        }
    }
,
    renderAIOpsRunStatusFilter(items) {
        if (!this.aiOpsRunStatusFilter) return;
        const currentValue = this.aiOpsRunStatusFilter.value || '';
        this.aiOpsRunStatusFilter.innerHTML = '';

        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = '全部';
        this.aiOpsRunStatusFilter.appendChild(allOption);

        items.forEach((item) => {
            const status = item?.status || '';
            if (!status) return;
            const option = document.createElement('option');
            option.value = status;
            option.textContent = item.label || status;
            this.aiOpsRunStatusFilter.appendChild(option);
        });

        const hasCurrent = Array.from(this.aiOpsRunStatusFilter.options)
            .some((option) => option.value === currentValue);
        this.aiOpsRunStatusFilter.value = hasCurrent ? currentValue : '';
    }
,
    async refreshAIOpsRuns() {
        try {
            const query = this.buildAIOpsRunHistoryQuery();
            const data = await this.apiGet(`${this.apiBaseUrl}/aiops/runs?${query}`);
            this.dashboardState.aiopsRuns = Array.isArray(data.items) ? data.items : [];
            this.renderAIOpsRunHistory();
        } catch (error) {
            if (this.aiOpsRunHistoryList) {
                this.aiOpsRunHistoryList.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }
,
    buildAIOpsRunHistoryQuery() {
        const filters = this.dashboardState.aiopsRunFilters || {};
        const params = new URLSearchParams({ limit: '20' });
        if (filters.status) {
            params.set('status', filters.status);
        }
        if (filters.serviceName) {
            params.set('service_name', filters.serviceName);
        }
        return params.toString();
    }
,
    async applyAIOpsRunFilters() {
        this.dashboardState.aiopsRunFilters = {
            status: this.aiOpsRunStatusFilter ? this.aiOpsRunStatusFilter.value : '',
            serviceName: this.aiOpsRunServiceFilter ? this.aiOpsRunServiceFilter.value.trim() : ''
        };
        await this.refreshAIOpsRuns();
    }
,
    async clearAIOpsRunFilters() {
        if (this.aiOpsRunStatusFilter) {
            this.aiOpsRunStatusFilter.value = '';
        }
        if (this.aiOpsRunServiceFilter) {
            this.aiOpsRunServiceFilter.value = '';
        }
        this.dashboardState.aiopsRunFilters = {
            status: '',
            serviceName: ''
        };
        await this.refreshAIOpsRuns();
    }
,
    renderAIOpsRunHistory() {
        const runs = Array.isArray(this.dashboardState.aiopsRuns)
            ? this.dashboardState.aiopsRuns
            : [];
        if (this.aiOpsRunHistoryCount) {
            this.aiOpsRunHistoryCount.textContent = String(runs.length);
        }
        if (!this.aiOpsRunHistoryList) return;
        if (runs.length === 0) {
            this.aiOpsRunHistoryList.innerHTML = '<div class="empty-state">暂无诊断运行记录</div>';
            this.renderAIOpsRunCompare();
            return;
        }

        this.aiOpsRunHistoryList.innerHTML = runs.map((run) => {
            const sessionId = run.session_id || run.diagnosis_run_id || '';
            const isActive = sessionId && sessionId === this.activeAIOpsRun?.sessionId;
            const status = run.status || 'unknown';
            const title = run.title || run.incident_id || sessionId || '诊断任务';
            const summary = run.summary || this.formatAIOpsRunCounters(run);
            return `
                <article class="aiops-run-item ${isActive ? 'active' : ''}" data-aiops-run-id="${this.escapeHtml(sessionId)}">
                    <div class="incident-title-row">
                        <strong>${this.escapeHtml(title)}</strong>
                        <span class="meta-pill ${this.statusTone(run)}">${this.escapeHtml(this.formatRecoveredAIOpsStatusLabel(run))}</span>
                    </div>
                    <p class="incident-summary">${this.escapeHtml(summary)}</p>
                    <div class="meta-row">
                        <span class="meta-pill">${this.escapeHtml(run.service_name || 'unknown-service')}</span>
                        <span class="meta-pill">${this.escapeHtml(run.severity || 'unknown')}</span>
                        <span class="meta-pill">${this.escapeHtml(this.formatDateTime(run.updated_at))}</span>
                        <span class="meta-pill">${this.escapeHtml(this.formatAIOpsRunCounters(run))}</span>
                    </div>
                </article>
            `;
        }).join('');
        this.renderAIOpsRunCompare();
    }
,
    formatAIOpsRunCounters(run) {
        const completed = run.completed_step_count ?? 0;
        const tools = run.tool_call_count ?? 0;
        const evidence = run.evidence_count ?? 0;
        const warnings = run.warning_count ?? 0;
        return `步骤 ${completed} · 工具 ${tools} · 证据 ${evidence} · 告警 ${warnings}`;
    }
,
    renderAIOpsRunCompare() {
        if (!this.aiOpsRunCompare) return;
        const runs = Array.isArray(this.dashboardState.aiopsRuns)
            ? [...this.dashboardState.aiopsRuns]
            : [];
        if (runs.length === 0) {
            this.aiOpsRunCompare.innerHTML = '<div class="empty-state">暂无可对比的诊断运行</div>';
            return;
        }

        const incidentId = (
            this.selectedIncidentId ||
            this.activeAIOpsRun?.incidentId ||
            runs[0]?.incident_id ||
            ''
        );
        const comparableRuns = runs
            .filter((run) => run.incident_id === incidentId)
            .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));

        if (comparableRuns.length < 2) {
            this.aiOpsRunCompare.innerHTML = `
                <div class="empty-state">当前故障事件暂无两次以上诊断运行可对比</div>
            `;
            return;
        }

        const [latest, previous] = comparableRuns;
        this.aiOpsRunCompare.innerHTML = `
            <div class="run-compare-heading">
                <strong>同事件运行对比</strong>
                <span>${this.escapeHtml(incidentId)}</span>
            </div>
            <div class="run-compare-grid">
                ${this.renderAIOpsRunCompareCard('最近一次', latest)}
                ${this.renderAIOpsRunCompareCard('上一次', previous)}
            </div>
            <div class="run-compare-delta">
                ${this.renderAIOpsRunDelta('状态', this.formatRecoveredAIOpsStatusLabel(previous), this.formatRecoveredAIOpsStatusLabel(latest))}
                ${this.renderAIOpsRunDelta('完成步骤', previous.completed_step_count ?? 0, latest.completed_step_count ?? 0)}
                ${this.renderAIOpsRunDelta('工具调用', previous.tool_call_count ?? 0, latest.tool_call_count ?? 0)}
                ${this.renderAIOpsRunDelta('证据数量', previous.evidence_count ?? 0, latest.evidence_count ?? 0)}
                ${this.renderAIOpsRunDelta('报告', previous.has_report ? '已生成' : '未生成', latest.has_report ? '已生成' : '未生成')}
            </div>
        `;
    }
,
    renderAIOpsRunCompareCard(label, run) {
        return `
            <article class="run-compare-card">
                <div class="incident-title-row">
                    <strong>${this.escapeHtml(label)}</strong>
                    <span class="meta-pill ${this.statusTone(run)}">${this.escapeHtml(this.formatRecoveredAIOpsStatusLabel(run))}</span>
                </div>
                <p class="incident-summary">${this.escapeHtml(run.summary || run.title || run.session_id || '')}</p>
                <div class="meta-row">
                    <span class="meta-pill">${this.escapeHtml(this.formatDateTime(run.updated_at))}</span>
                    <span class="meta-pill">${this.escapeHtml(this.formatAIOpsRunCounters(run))}</span>
                </div>
            </article>
        `;
    }
,
    renderAIOpsRunDelta(label, previousValue, latestValue) {
        const previousText = String(previousValue);
        const latestText = String(latestValue);
        const changed = previousText !== latestText;
        return `
            <div class="run-compare-delta-item ${changed ? 'changed' : ''}">
                <span>${this.escapeHtml(label)}</span>
                <strong>${this.escapeHtml(previousText)} → ${this.escapeHtml(latestText)}</strong>
            </div>
        `;
    }
,
    upsertAIOpsRunHistoryItem(item) {
        if (!item || !(item.session_id || item.diagnosis_run_id)) return;
        const sessionId = item.session_id || item.diagnosis_run_id;
        const current = Array.isArray(this.dashboardState.aiopsRuns)
            ? this.dashboardState.aiopsRuns
            : [];
        const existingIndex = current.findIndex((run) => (
            (run.session_id || run.diagnosis_run_id) === sessionId
        ));
        if (existingIndex >= 0) {
            current[existingIndex] = {
                ...current[existingIndex],
                ...item
            };
        } else {
            current.unshift(item);
        }
        current.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
        this.dashboardState.aiopsRuns = current.slice(0, 20);
        this.renderAIOpsRunHistory();
    }
,
    buildAIOpsRunHistoryItem(payload) {
        const incident = payload.incident || {};
        const sessionId = payload.session_id || payload.diagnosis_run_id || '';
        const plan = Array.isArray(payload.plan) ? payload.plan : [];
        const executionSteps = Array.isArray(payload.execution_steps) ? payload.execution_steps : [];
        const toolCalls = Array.isArray(payload.tool_call_records) ? payload.tool_call_records : [];
        const evidence = Array.isArray(payload.gathered_evidence) ? payload.gathered_evidence : [];
        return {
            diagnosis_run_id: sessionId,
            session_id: sessionId,
            incident_id: payload.incident_id || incident.incident_id || '',
            trace_id: payload.trace_id || '',
            status: payload.status || 'running',
            node_name: payload.node_name || '',
            title: incident.title || payload.incident_id || sessionId,
            service_name: incident.service_name || 'unknown-service',
            severity: incident.severity || 'unknown',
            environment: incident.environment || 'unknown',
            summary: incident.symptom || payload.error_message || '',
            started_at: payload.started_at || payload.created_at || '',
            updated_at: payload.updated_at || this.currentIsoTime(),
            approval_status: payload.pending_approval?.status || 'not_required',
            has_pending_approval: Boolean(payload.pending_approval),
            has_report: Boolean(payload.has_report || payload.structured_report),
            plan_step_count: plan.length,
            completed_step_count: executionSteps.length,
            evidence_count: evidence.length,
            tool_call_count: toolCalls.length,
            error_count: payload.error_message ? 1 : 0,
            warning_count: Array.isArray(payload.warnings) ? payload.warnings.length : 0,
            links: {
                run: sessionId ? `/api/aiops/runs/${sessionId}` : ''
            }
        };
    }
,
    async openAIOpsRunHistory(sessionId) {
        if (!sessionId) return;
        this.currentIncidentTab = 'process';
        await this.setWorkbenchView('incidents');
        await this.refreshAIOpsRunStatus(sessionId, { fromHistory: true });
        this.setIncidentTab('process');
    }
,
    async refreshIncidents() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/incidents?limit=50`);
            this.dashboardState.incidents = Array.isArray(data.items) ? data.items : [];
            if (!this.selectedIncidentId && this.dashboardState.incidents.length > 0) {
                this.selectedIncidentId = this.dashboardState.incidents[0].incident_id;
            }
            this.renderIncidentList();
            if (this.selectedIncidentId) {
                await this.refreshSelectedIncidentPanels();
            } else {
                this.renderEmptyIncidentPanels();
            }
        } catch (error) {
            if (this.incidentList) {
                this.incidentList.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }
,
    renderIncidentList() {
        if (this.incidentCount) {
            this.incidentCount.textContent = String(this.dashboardState.incidents.length);
        }
        if (!this.incidentList) return;
        if (this.dashboardState.incidents.length === 0) {
            this.incidentList.innerHTML = '<div class="empty-state">暂无故障事件</div>';
            return;
        }

        this.incidentList.innerHTML = this.dashboardState.incidents.map((incident) => {
            const isActive = incident.incident_id === this.selectedIncidentId;
            const summary = incident.summary || incident.root_cause || '暂无摘要';
            return `
                <article class="incident-item ${isActive ? 'active' : ''}" data-incident-id="${this.escapeHtml(incident.incident_id)}">
                    <div class="incident-title-row">
                        <strong>${this.escapeHtml(incident.title || incident.incident_id)}</strong>
                        <span class="meta-pill ${this.statusTone(incident)}">${this.escapeHtml(this.formatRecoveredAIOpsStatusLabel(incident))}</span>
                    </div>
                    <p class="incident-summary">${this.escapeHtml(summary)}</p>
                    <div class="meta-row">
                        <span class="meta-pill">${this.escapeHtml(incident.service_name || 'unknown-service')}</span>
                        <span class="meta-pill">${this.escapeHtml(incident.severity || 'unknown')}</span>
                        <span class="meta-pill">${this.escapeHtml(incident.approval_status || 'not_required')}</span>
                    </div>
                </article>
            `;
        }).join('');
    }
,
    async refreshAlerts() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/alerts?limit=20`);
            this.dashboardState.alerts = Array.isArray(data.items) ? data.items : [];
            this.renderAlertList();
        } catch (error) {
            if (this.alertList) {
                this.alertList.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }
,
    renderAlertList() {
        if (this.alertCount) {
            this.alertCount.textContent = String(this.dashboardState.alerts.length);
        }
        if (!this.alertList) return;
        if (this.dashboardState.alerts.length === 0) {
            this.alertList.innerHTML = '<div class="empty-state">暂无接入告警</div>';
            return;
        }

        this.alertList.innerHTML = this.dashboardState.alerts.map((alert) => {
            const rawSummary = this.summarizeAlertRawPayload(alert);
            return `
                <article class="alert-item" data-alert-fingerprint="${this.escapeHtml(alert.fingerprint || '')}">
                    <div class="incident-title-row">
                        <strong>${this.escapeHtml(alert.alertname || 'UnknownAlert')}</strong>
                        <span class="meta-pill ${this.statusTone(alert.status)}">${this.escapeHtml(alert.status || 'unknown')}</span>
                    </div>
                    <p class="incident-summary">${this.escapeHtml(alert.summary || alert.description || '暂无告警摘要')}</p>
                    <p class="alert-raw-summary">${this.escapeHtml(rawSummary)}</p>
                    <div class="meta-row">
                        <span class="meta-pill">${this.escapeHtml(alert.service_name || 'unknown-service')}</span>
                        <span class="meta-pill">${this.escapeHtml(alert.severity || 'unknown')}</span>
                        <span class="meta-pill">${this.escapeHtml(alert.source || 'alertmanager')}</span>
                    </div>
                </article>
            `;
        }).join('');
    }
,
    summarizeAlertRawPayload(alert) {
        const rawPayload = alert?.raw_payload || {};
        const rawAlert = rawPayload.alert && typeof rawPayload.alert === 'object'
            ? rawPayload.alert
            : {};
        const rawKeys = Object.keys(rawAlert).slice(0, 4);
        const labelKeys = Object.keys(alert?.labels || {}).slice(0, 4);
        const parts = [];
        if (rawKeys.length) {
            parts.push(`raw: ${rawKeys.join(', ')}`);
        }
        if (labelKeys.length) {
            parts.push(`labels: ${labelKeys.join(', ')}`);
        }
        if (alert?.fingerprint) {
            parts.push(`fingerprint=${alert.fingerprint}`);
        }
        return parts.join(' · ') || '已保存原始告警 payload';
    }
,
    applyAlertToDiagnosisForm(fingerprint) {
        if (!fingerprint) return;
        const alert = this.dashboardState.alerts.find((item) => item.fingerprint === fingerprint);
        if (!alert) return;
        const incident = this.buildIncidentFromAlertEvent(alert);
        this.populateAIOpsIncidentForm(incident);
        this.setAIOpsFormStatus('已载入告警', 'success');
        if (alert.incident_id) {
            this.selectedIncidentId = alert.incident_id;
            this.renderIncidentList();
            this.refreshSelectedIncidentPanels().catch((error) => {
                console.warn('刷新告警关联事件详情失败:', error);
            });
        }
    }
,
    buildIncidentFromAlertEvent(alert) {
        const symptom = [alert.summary, alert.description]
            .filter((item, index, items) => item && items.indexOf(item) === index)
            .join('；');
        return {
            incident_id: alert.incident_id || this.generateAIOpsIncidentId(alert.service_name),
            title: `${alert.service_name || 'unknown-service'} ${alert.alertname || 'UnknownAlert'}`,
            service_name: alert.service_name || 'unknown-service',
            severity: alert.severity || 'P2',
            environment: alert.environment || 'unknown',
            symptom: symptom || `${alert.alertname || 'UnknownAlert'} alert from ${alert.source || 'alertmanager'}`,
            raw_alert: {
                source: alert.source || 'alertmanager',
                fingerprint: alert.fingerprint || '',
                status: alert.status || '',
                alertname: alert.alertname || '',
                labels: alert.labels || {},
                annotations: alert.annotations || {},
                starts_at: alert.starts_at || '',
                ends_at: alert.ends_at || '',
                generator_url: alert.generator_url || '',
                raw_payload: alert.raw_payload || {}
            }
        };
    }
,
    async selectIncident(incidentId) {
        if (!incidentId) return;
        if (this.selectedIncidentId !== incidentId) {
            this.resetReplayFilters();
        }
        this.selectedIncidentId = incidentId;
        this.renderIncidentList();
        this.renderAIOpsRunCompare();
        await this.refreshSelectedIncidentPanels();
    }
,
    async refreshSelectedIncidentPanels() {
        if (!this.selectedIncidentId) {
            this.renderEmptyIncidentPanels();
            return;
        }
        const incidentId = this.selectedIncidentId;
        this.renderIncidentPanelsLoading(incidentId);
        const [detail, replay, trace, report, approvals, changes] = await Promise.allSettled([
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/replay`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/trace`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/report`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/approval`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/changes`)
        ]);

        if (incidentId !== this.selectedIncidentId) {
            return;
        }

        if (detail.status === 'fulfilled') {
            this.renderIncidentDetail(detail.value);
        } else {
            this.renderIncidentDetailError(detail.reason);
        }
        if (replay.status === 'fulfilled') {
            this.dashboardState.incidentReplay = replay.value;
            this.renderIncidentReplay(replay.value);
        } else {
            this.dashboardState.incidentReplay = null;
            this.renderIncidentReplayError(replay.reason);
        }
        if (trace.status === 'fulfilled') {
            this.renderTraceTimeline(trace.value);
        } else {
            this.renderTraceTimelineError(trace.reason);
        }
        if (report.status === 'fulfilled') {
            this.renderReport(report.value);
        } else {
            this.renderReportError(report.reason);
        }
        if (approvals.status === 'fulfilled') {
            this.renderApprovals(approvals.value.items || []);
        } else {
            this.renderApprovalsError(approvals.reason);
        }
        if (changes.status === 'fulfilled') {
            this.dashboardState.changeExecutions = Array.isArray(changes.value.items) ? changes.value.items : [];
            this.renderChangeExecutions(this.dashboardState.changeExecutions);
        } else {
            this.renderChangeExecutionError(changes.reason);
        }
    }
});
