// Incident overview and replay rendering.
Object.assign(window.AutoOnCallApp.prototype, {
    renderIncidentPanelsLoading(incidentId) {
        if (this.selectedIncidentBadge) this.selectedIncidentBadge.textContent = `${incidentId} · 加载中`;
        if (this.incidentDetail) {
            this.incidentDetail.innerHTML = this.renderPanelState(
                'loading',
                '正在加载事件详情',
                '同步拉取 Incident 概览、Trace、审批和报告。'
            );
        }
        if (this.incidentReplay) {
            this.incidentReplay.innerHTML = this.renderPanelState(
                'loading',
                '正在加载诊断回放',
                '准备展示从告警到报告的诊断链路。'
            );
        }
        if (this.traceTimeline) {
            this.traceTimeline.innerHTML = this.renderPanelState('loading', '正在加载 Trace', '读取结构化事件流。');
        }
        if (this.reportViewer) {
            this.reportViewer.innerHTML = this.renderPanelState('loading', '正在加载报告', '读取最终诊断报告。');
        }
        if (this.approvalList) {
            this.approvalList.innerHTML = this.renderPanelState('loading', '正在加载审批记录', '读取人工确认与风险动作。');
        }
        if (this.changeExecutionList) {
            this.changeExecutionList.innerHTML = this.renderPanelState('loading', '正在加载执行记录', '读取安全变更闭环。');
        }
        this.renderDiagnosisChainLoading();
        if (this.replayStatus) {
            this.replayStatus.textContent = '加载中';
            this.replayStatus.className = 'warning';
        }
        if (this.traceCount) this.traceCount.textContent = '…';
        if (this.reportStatus) this.reportStatus.textContent = '加载中';
        if (this.approvalCount) this.approvalCount.textContent = '…';
        if (this.changeExecutionCount) this.changeExecutionCount.textContent = '…';
    }
,
    renderEmptyIncidentPanels() {
        if (this.selectedIncidentBadge) this.selectedIncidentBadge.textContent = '未选择';
        this.dashboardState.incidentReplay = null;
        this.resetReplayFilters();
        if (this.incidentDetail) {
            this.incidentDetail.innerHTML = this.renderPanelState('empty', '尚未选择故障事件', '从左侧事件列表选择一条 Incident。');
        }
        if (this.incidentReplay) {
            this.incidentReplay.innerHTML = this.renderPanelState('empty', '暂无诊断回放', '选择 Incident 后展示 Planner、Executor、Replanner、审批和报告链路。');
        }
        if (this.traceTimeline) this.traceTimeline.innerHTML = this.renderPanelState('empty', '暂无 Trace 事件', '尚未加载结构化事件流。');
        if (this.reportViewer) this.reportViewer.innerHTML = this.renderPanelState('empty', '暂无诊断报告', '当前没有可展示的报告。');
        if (this.approvalList) this.approvalList.innerHTML = this.renderPanelState('empty', '暂无审批记录', '当前没有人工审批动作。');
        if (this.changeExecutionList) this.changeExecutionList.innerHTML = this.renderPanelState('empty', '暂无执行记录', '当前没有安全变更执行记录。');
        this.renderDiagnosisChain({});
        this.renderDependencySignals([]);
        if (this.traceCount) this.traceCount.textContent = '0';
        if (this.replayStatus) this.replayStatus.textContent = '未加载';
        if (this.reportStatus) this.reportStatus.textContent = '未加载';
        if (this.approvalCount) this.approvalCount.textContent = '0';
        if (this.changeExecutionCount) this.changeExecutionCount.textContent = '0';
    }
,
    renderIncidentDetail(incident) {
        if (this.selectedIncidentBadge) {
            this.selectedIncidentBadge.textContent = incident.incident_id || 'unknown';
        }
        if (!this.incidentDetail) return;
        const traceSummary = incident.trace_summary || {};
        const approvalSummary = incident.approval_summary || {};
        const chain = incident.diagnosis_chain || {};
        this.incidentDetail.innerHTML = `
            ${this.renderIncidentPath(incident)}
            <div class="detail-grid">
                <div class="detail-field">
                    <span>服务</span>
                    <strong>${this.escapeHtml(incident.service_name || 'unknown-service')}</strong>
                </div>
                <div class="detail-field">
                    <span>严重级别</span>
                    <strong>${this.escapeHtml(incident.severity || 'unknown')}</strong>
                </div>
                <div class="detail-field">
                    <span>状态</span>
                    <strong>${this.escapeHtml(incident.status || 'unknown')}</strong>
                </div>
                <div class="detail-field">
                    <span>状态原因</span>
                    <strong>${this.escapeHtml(incident.status_reason || '未记录')}</strong>
                </div>
                <div class="detail-field">
                    <span>审批</span>
                    <strong>${this.escapeHtml(incident.approval_status || 'not_required')}</strong>
                </div>
                <div class="detail-field">
                    <span>Session</span>
                    <strong>${this.escapeHtml(incident.session_id || '未记录')}</strong>
                </div>
                <div class="detail-field">
                    <span>Trace 事件</span>
                    <strong>${traceSummary.event_count || 0}</strong>
                </div>
                <div class="detail-field">
                    <span>审批记录</span>
                    <strong>${approvalSummary.total || 0}</strong>
                </div>
                <div class="detail-field">
                    <span>根因</span>
                    <strong>${this.escapeHtml(incident.root_cause || '暂未形成明确根因')}</strong>
                </div>
                <div class="detail-field">
                    <span>更新时间</span>
                    <strong>${this.formatDateTime(incident.updated_at)}</strong>
                </div>
            </div>
        `;
        this.renderDiagnosisChain(chain);
    }
,
    renderIncidentPath(incident) {
        const statusTone = this.statusTone(incident);
        const pathItems = [
            'Incidents',
            incident.environment || 'unknown-env',
            incident.service_name || 'unknown-service',
            incident.incident_id || 'unknown-incident'
        ];
        return `
            <div class="incident-path-card">
                <nav class="incident-path" aria-label="Incident path">
                    ${pathItems.map((item, index) => `
                        ${index > 0 ? '<span class="incident-path-separator">/</span>' : ''}
                        <span>${this.escapeHtml(item)}</span>
                    `).join('')}
                </nav>
                <div class="incident-context-strip">
                    <span class="meta-pill ${statusTone}">${this.escapeHtml(incident.status_metadata?.label || incident.status || 'unknown')}</span>
                    <span class="source-pill">severity=${this.escapeHtml(incident.severity || 'unknown')}</span>
                    <span class="source-pill">trace=${this.escapeHtml(incident.trace_id || 'none')}</span>
                    <span class="source-pill">updated=${this.escapeHtml(this.formatDateTime(incident.updated_at))}</span>
                </div>
            </div>
        `;
    }
,
    renderIncidentDetailError(error) {
        if (this.selectedIncidentBadge) this.selectedIncidentBadge.textContent = '加载失败';
        if (this.incidentDetail) {
            this.incidentDetail.innerHTML = this.renderPanelState(
                'error',
                '事件详情加载失败',
                error?.message || '无法读取当前 Incident 概览。'
            );
        }
        this.renderDiagnosisChain({});
    }
,
    renderPanelState(type, title, detail = '') {
        const safeType = ['empty', 'loading', 'error', 'success', 'warning'].includes(type)
            ? type
            : 'empty';
        return `
            <div class="panel-state ${safeType}" role="status">
                <strong>${this.escapeHtml(title || '暂无数据')}</strong>
                ${detail ? `<span>${this.escapeHtml(detail)}</span>` : ''}
            </div>
        `;
    }
,
    renderDiagnosisChainLoading() {
        if (this.planCount) this.planCount.textContent = '…';
        if (this.stepCount) this.stepCount.textContent = '…';
        if (this.toolCallCount) this.toolCallCount.textContent = '…';
        if (this.dependencySignalCount) this.dependencySignalCount.textContent = '…';
        if (this.evidenceCount) this.evidenceCount.textContent = '…';
        if (this.confidenceBadge) this.confidenceBadge.textContent = '加载中';
        if (this.planList) this.planList.innerHTML = this.renderPanelState('loading', '正在加载计划', '读取 Planner 结构化步骤。');
        if (this.stepList) this.stepList.innerHTML = this.renderPanelState('loading', '正在加载执行步骤', '读取 Executor 节点和工具调用。');
        if (this.toolCallTable) this.toolCallTable.innerHTML = this.renderPanelState('loading', '正在加载工具调用', '读取 ToolExecutionResult。');
        if (this.dependencySignalList) this.dependencySignalList.innerHTML = this.renderPanelState('loading', '正在加载依赖证据', '读取 Tracing 和 MQ 信号。');
        if (this.evidenceList) this.evidenceList.innerHTML = this.renderPanelState('loading', '正在加载证据', '读取 Evidence 列表。');
        if (this.conclusionView) this.conclusionView.innerHTML = this.renderPanelState('loading', '正在加载结论', '读取根因、置信度和建议动作。');
    }
,
    renderDiagnosisChain(chain) {
        const safeChain = chain || {};
        this.renderPlanCards(safeChain.plan || []);
        this.renderExecutionSteps(safeChain.steps || []);
        this.renderToolCallTable(safeChain.tool_calls || []);
        this.renderDependencySignals(safeChain.dependency_signals || [], safeChain.tool_calls || []);
        this.renderEvidenceCards(safeChain.evidence || []);
        this.renderConclusionView(safeChain);
    }
,
    renderIncidentReplay(replay) {
        if (!this.incidentReplay) return;
        const payload = replay || {};
        const metrics = payload.metrics || {};
        const stages = Array.isArray(payload.stages) ? payload.stages : [];
        const timeline = Array.isArray(payload.timeline) ? payload.timeline : [];
        const replayFilters = this.normalizedReplayFilters();
        const filteredTimeline = this.filterReplayTimeline(timeline, replayFilters);
        const replannerDecisions = Array.isArray(payload.replanner_decisions)
            ? payload.replanner_decisions
            : [];
        const evidenceQuality = payload.evidence_quality || {};
        const tooling = payload.tooling || {};
        const approvalFlow = payload.approval_flow || {};
        const changeFlow = payload.change_flow || {};
        const reportSummary = payload.report_summary || {};
        const evaluation = payload.evaluation || {};

        if (this.replayStatus) {
            const count = metrics.trace_event_count ?? timeline.length;
            this.replayStatus.textContent = `${count} 个事件`;
            this.replayStatus.className = this.statusTone(payload);
        }

        this.incidentReplay.innerHTML = `
            <div class="replay-summary-bar">
                <div>
                    <span>服务</span>
                    <strong>${this.escapeHtml(payload.service_name || 'unknown-service')}</strong>
                </div>
                <div>
                    <span>状态</span>
                    <strong>${this.escapeHtml(payload.status_metadata?.label || payload.status || 'unknown')}</strong>
                </div>
                <div>
                    <span>根因</span>
                    <strong>${this.escapeHtml(payload.root_cause || reportSummary.root_cause || '暂未确认')}</strong>
                </div>
                <div>
                    <span>置信度</span>
                    <strong>${this.formatConfidence(metrics.confidence ?? reportSummary.confidence)}</strong>
                </div>
            </div>
            <div class="replay-stage-rail">
                ${stages.map((stage) => this.renderReplayStageCard(stage)).join('') || '<div class="empty-state">暂无阶段数据</div>'}
            </div>
            ${this.renderReplayReplannerDecisions(replannerDecisions)}
            <div class="replay-body-grid">
                <section class="replay-timeline-panel">
                    <div class="replay-panel-heading">
                        <strong>生命周期时间线</strong>
                        <span>${filteredTimeline.length}/${timeline.length} events</span>
                    </div>
                    ${this.renderReplayTimelineControls(timeline, filteredTimeline, replayFilters)}
                    <div class="replay-timeline-list">
                        ${this.renderReplayTimelineItems(filteredTimeline, timeline.length)}
                    </div>
                </section>
                <aside class="replay-insight-panel">
                    ${this.renderReplayMetricGrid(metrics)}
                    ${this.renderReplayEvidenceQuality(evidenceQuality)}
                    ${this.renderReplayTooling(tooling)}
                    ${this.renderReplayApprovalFlow(approvalFlow)}
                    ${this.renderReplayChangeFlow(changeFlow)}
                    ${this.renderReplayReportSummary(reportSummary)}
                    ${this.renderReplayEvaluation(evaluation)}
                </aside>
            </div>
        `;
    }
,
    renderIncidentReplayError(error) {
        if (this.replayStatus) {
            this.replayStatus.textContent = '未加载';
            this.replayStatus.className = 'warning';
        }
        if (!this.incidentReplay) return;
        this.incidentReplay.innerHTML = this.renderPanelState(
            'error',
            '诊断回放加载失败',
            error?.message || '暂无诊断回放'
        );
    }
,
    renderReplayStageCard(stage) {
        const item = stage || {};
        const tone = this.statusTone(item.status);
        return `
            <article class="replay-stage-card ${tone}">
                <div class="replay-stage-title">
                    <strong>${this.escapeHtml(item.label || item.key || '阶段')}</strong>
                    <span class="meta-pill ${tone}">${this.escapeHtml(item.status || 'unknown')}</span>
                </div>
                <p>${this.escapeHtml(item.summary || '暂无摘要')}</p>
                <div class="source-strip">
                    <span class="source-pill">events=${this.escapeHtml(String(item.event_count ?? 0))}</span>
                    <span class="source-pill">failed=${this.escapeHtml(String(item.failed_event_count ?? 0))}</span>
                    ${item.updated_at ? `<span class="source-pill">${this.formatDateTime(item.updated_at)}</span>` : ''}
                </div>
            </article>
        `;
    }
,
    renderReplayTimelineControls(timeline, filteredTimeline, filters) {
        const items = Array.isArray(timeline) ? timeline : [];
        const stageOptions = this.replayFilterOptions(items, 'stage', 'stage_label');
        const statusOptions = this.replayFilterOptions(items, 'status');
        const safeFilters = filters || this.normalizedReplayFilters();
        return `
            <div class="replay-filter-bar">
                <label>
                    <span>阶段</span>
                    <select data-replay-filter="stage">
                        ${this.renderReplayFilterOptions(stageOptions, safeFilters.stage)}
                    </select>
                </label>
                <label>
                    <span>状态</span>
                    <select data-replay-filter="status">
                        ${this.renderReplayFilterOptions(statusOptions, safeFilters.status)}
                    </select>
                </label>
                <label class="replay-filter-search">
                    <span>关键词</span>
                    <input type="search" data-replay-filter="query" maxlength="80" value="${this.escapeHtml(safeFilters.query)}" placeholder="tool / node / summary">
                </label>
                <button type="button" class="action-btn" data-replay-filter-action="apply">筛选</button>
                <button type="button" class="action-btn" data-replay-filter-action="clear">清空</button>
                <span class="replay-filter-count">${filteredTimeline.length}/${items.length}</span>
            </div>
        `;
    }
,
    renderReplayFilterOptions(options, currentValue) {
        const current = currentValue || 'all';
        return [
            { value: 'all', label: '全部' },
            ...options
        ].map((option) => `
            <option value="${this.escapeHtml(option.value)}" ${option.value === current ? 'selected' : ''}>
                ${this.escapeHtml(option.label)}
            </option>
        `).join('');
    }
,
    replayFilterOptions(items, valueKey, labelKey = valueKey) {
        const byValue = new Map();
        items.forEach((item) => {
            const value = String(item?.[valueKey] || '').trim();
            if (!value) return;
            const label = String(item?.[labelKey] || value).trim() || value;
            if (!byValue.has(value)) byValue.set(value, label);
        });
        return Array.from(byValue.entries())
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([value, label]) => ({ value, label }));
    }
,
    renderReplayTimelineItems(timeline, totalCount = 0) {
        const items = Array.isArray(timeline) ? timeline : [];
        if (items.length === 0) {
            const title = totalCount > 0 ? '没有匹配当前筛选条件的事件' : '暂无回放事件';
            const detail = totalCount > 0 ? '调整阶段、状态或关键词后重试。' : '当前 Incident 还没有结构化 Trace。';
            return this.renderPanelState('empty', title, detail);
        }
        return items.map((event) => {
            const tone = this.statusTone(event.status);
            return `
                <article class="replay-timeline-item ${tone}">
                    <div class="replay-timeline-marker">${this.escapeHtml(event.stage_label || event.stage || '事件')}</div>
                    <div class="replay-timeline-content">
                        <div class="replay-timeline-title">
                            <strong>${this.escapeHtml(event.event_type || 'event')} · ${this.escapeHtml(event.node_name || 'workflow')}</strong>
                            <span class="meta-pill ${tone}">${this.escapeHtml(event.status || 'unknown')}</span>
                        </div>
                        <p>${this.escapeHtml(event.summary || '无摘要')}</p>
                        <div class="source-strip">
                            ${event.tool_name ? `<span class="source-pill">${this.escapeHtml(event.tool_name)}</span>` : ''}
                            ${event.step_id ? `<span class="source-pill">${this.escapeHtml(event.step_id)}</span>` : ''}
                            ${event.data_source ? this.sourcePill(event.data_source) : ''}
                            <span class="source-pill">${this.escapeHtml(String(event.latency_ms ?? 0))} ms</span>
                            <span class="source-pill">${this.formatDateTime(event.created_at)}</span>
                        </div>
                    </div>
                </article>
            `;
        }).join('');
    }
,
    handleReplayFilterChange(event, options = {}) {
        const target = event.target.closest('[data-replay-filter]');
        if (!target) return;
        const key = target.getAttribute('data-replay-filter');
        if (!['stage', 'status', 'query'].includes(key)) return;
        this.dashboardState.replayFilters = {
            ...this.normalizedReplayFilters(),
            [key]: String(target.value || '').trim()
        };
        if (options.render) {
            this.renderIncidentReplay(this.dashboardState.incidentReplay);
        }
    }
,
    resetReplayFilters() {
        this.dashboardState.replayFilters = {
            stage: 'all',
            status: 'all',
            query: ''
        };
    }
,
    normalizedReplayFilters() {
        const filters = this.dashboardState.replayFilters || {};
        return {
            stage: filters.stage || 'all',
            status: filters.status || 'all',
            query: filters.query || ''
        };
    }
,
    filterReplayTimeline(timeline, filters) {
        const items = Array.isArray(timeline) ? timeline : [];
        const safeFilters = filters || this.normalizedReplayFilters();
        const query = String(safeFilters.query || '').trim().toLowerCase();
        return items.filter((item) => {
            if (safeFilters.stage !== 'all' && item.stage !== safeFilters.stage) return false;
            if (safeFilters.status !== 'all' && item.status !== safeFilters.status) return false;
            if (!query) return true;
            return this.replayTimelineSearchText(item).includes(query);
        });
    }
,
    replayTimelineSearchText(item) {
        const event = item || {};
        return [
            event.stage,
            event.stage_label,
            event.status,
            event.event_type,
            event.node_name,
            event.tool_name,
            event.step_id,
            event.data_source,
            event.summary,
            event.input_summary,
            event.output_summary,
            event.error_message
        ].filter(Boolean).join(' ').toLowerCase();
    }
,
    renderReplayReplannerDecisions(decisions) {
        const items = Array.isArray(decisions) ? decisions : [];
        if (items.length === 0) {
            return `
                <section class="replanner-decision-panel">
                    <div class="replay-panel-heading">
                        <strong>Replanner 决策</strong>
                        <span>0 decisions</span>
                    </div>
                    <div class="empty-state">暂无 Replanner 决策记录</div>
                </section>
            `;
        }
        return `
            <section class="replanner-decision-panel">
                <div class="replay-panel-heading">
                    <strong>Replanner 决策</strong>
                    <span>${items.length} decisions</span>
                </div>
                <div class="replanner-decision-list">
                    ${items.map((item) => this.renderReplayReplannerDecisionCard(item)).join('')}
                </div>
            </section>
        `;
    }
,
    renderReplayReplannerDecisionCard(decision) {
        const item = decision || {};
        const tone = this.replannerDecisionTone(item);
        const missingEvidence = Array.isArray(item.missing_evidence) ? item.missing_evidence : [];
        const newSteps = Array.isArray(item.new_steps) ? item.new_steps : [];
        const conflicts = Array.isArray(item.conflicts) ? item.conflicts : [];
        const confidenceReasons = Array.isArray(item.confidence_reasons)
            ? item.confidence_reasons
            : [];
        const profile = item.evidence_profile || {};
        const dataSources = profile.by_data_source || {};
        return `
            <article class="replanner-decision-card ${tone}">
                <div class="replay-stage-title">
                    <strong>${this.escapeHtml(item.decision_label || item.decision || 'Replanner')}</strong>
                    <span class="meta-pill ${tone}">${item.evidence_sufficient ? '证据充足' : '证据不足'}</span>
                </div>
                <p>${this.escapeHtml(item.reason || item.summary || '暂无决策原因')}</p>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(item.decision || 'unknown')}</span>
                    <span class="source-pill">${this.escapeHtml(item.source_quality || 'unknown')}</span>
                    <span class="source-pill">avg=${this.formatConfidence(item.average_evidence_confidence)}</span>
                    <span class="source-pill">${this.formatDateTime(item.created_at)}</span>
                </div>
                ${this.renderReplayCountStrip('数据源', dataSources)}
                ${this.renderReplayMiniList('缺失证据', missingEvidence)}
                ${this.renderReplayMiniList('新增步骤', newSteps.map((step) => (
                    `${step.tool_name || 'manual_analysis'}：${step.purpose || step.expected_evidence || step.step_id || '待执行'}`
                )))}
                ${this.renderReplayMiniList('冲突', conflicts)}
                ${this.renderReplayMiniList('置信原因', confidenceReasons)}
            </article>
        `;
    }
,
    replannerDecisionTone(decision) {
        const value = decision?.decision || '';
        if (['generate_report', 'continue_investigation'].includes(value)) return 'success';
        if (['add_steps', 'retry_failed_tool', 'request_approval'].includes(value)) return 'warning';
        if (value === 'escalate_to_human') return 'error';
        return this.statusTone(decision?.status || 'unknown');
    }
,
    renderReplayMetricGrid(metrics) {
        const safeMetrics = metrics || {};
        const metricItems = [
            ['计划步骤', safeMetrics.plan_step_count ?? 0],
            ['工具调用', safeMetrics.tool_call_count ?? 0],
            ['证据数量', safeMetrics.evidence_count ?? 0],
            ['审批记录', safeMetrics.approval_count ?? 0],
            ['异常事件', safeMetrics.failed_event_count ?? 0],
            ['P95 耗时', `${safeMetrics.p95_latency_ms ?? 0} ms`]
        ];
        return `
            <section class="replay-insight-block">
                <h3>回放指标</h3>
                <div class="replay-metric-grid">
                    ${metricItems.map(([label, value]) => `
                        <div class="metric-tile">
                            <strong>${this.escapeHtml(String(value))}</strong>
                            <span>${this.escapeHtml(label)}</span>
                        </div>
                    `).join('')}
                </div>
            </section>
        `;
    }
,
    renderReplayEvidenceQuality(evidenceQuality) {
        const quality = evidenceQuality || {};
        return `
            <section class="replay-insight-block">
                <h3>证据质量</h3>
                <div class="source-strip">
                    <span class="source-pill">avg=${this.formatConfidence(quality.average_confidence)}</span>
                    <span class="source-pill">high=${this.escapeHtml(String(quality.high_confidence_count ?? 0))}</span>
                    <span class="source-pill">low=${this.escapeHtml(String(quality.low_confidence_count ?? 0))}</span>
                    ${quality.has_mock ? '<span class="source-pill warning">mock</span>' : ''}
                    ${quality.has_not_configured ? '<span class="source-pill error">not_configured</span>' : ''}
                </div>
                ${this.renderReplayCountStrip('来源', quality.by_source)}
                ${this.renderReplayCountStrip('类型', quality.by_type)}
            </section>
        `;
    }
,
    renderReplayTooling(tooling) {
        const safeTooling = tooling || {};
        return `
            <section class="replay-insight-block">
                <h3>工具调用</h3>
                <div class="source-strip">
                    <span class="source-pill">total=${this.escapeHtml(String(safeTooling.total ?? 0))}</span>
                    <span class="source-pill">failure=${this.escapeHtml(String(safeTooling.failure_count ?? 0))}</span>
                </div>
                ${this.renderReplayCountStrip('工具', safeTooling.by_tool)}
                ${this.renderReplayCountStrip('状态', safeTooling.by_status)}
            </section>
        `;
    }
,
    renderReplayApprovalFlow(approvalFlow) {
        const flow = approvalFlow || {};
        const summary = flow.summary || {};
        const beforeAfter = flow.before_after || {};
        return `
            <section class="replay-insight-block">
                <h3>审批前后</h3>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(summary.status || 'not_required')}</span>
                    <span class="source-pill">total=${this.escapeHtml(String(summary.total ?? 0))}</span>
                </div>
                <p><strong>前：</strong>${this.escapeHtml(beforeAfter.before || '未触发审批')}</p>
                <p><strong>后：</strong>${this.escapeHtml(beforeAfter.after || '暂无后续状态')}</p>
            </section>
        `;
    }
,
    renderReplayChangeFlow(changeFlow) {
        const flow = changeFlow || {};
        return `
            <section class="replay-insight-block">
                <h3>安全变更</h3>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(flow.status || 'not_started')}</span>
                    <span class="source-pill">total=${this.escapeHtml(String(flow.total ?? 0))}</span>
                </div>
                <p>${this.escapeHtml(flow.latest?.status || '未启动安全变更流程')}</p>
            </section>
        `;
    }
,
    renderReplayReportSummary(reportSummary) {
        const report = reportSummary || {};
        return `
            <section class="replay-insight-block">
                <h3>报告摘要</h3>
                <p>${this.escapeHtml(report.root_cause || report.summary || '报告未生成')}</p>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(report.status || (report.available ? 'available' : 'unavailable'))}</span>
                    <span class="source-pill">conf=${this.formatConfidence(report.confidence)}</span>
                </div>
            </section>
        `;
    }
,
    renderReplayEvaluation(evaluation) {
        const evalPayload = evaluation || {};
        const metrics = Array.isArray(evalPayload.metrics) ? evalPayload.metrics : [];
        const failedMetrics = Array.isArray(evalPayload.failed_metrics)
            ? evalPayload.failed_metrics
            : [];
        const statusTone = this.statusTone(evalPayload.status || 'unknown');
        return `
            <section class="replay-insight-block">
                <h3>评测结果</h3>
                <div class="source-strip">
                    <span class="source-pill ${statusTone}">${this.escapeHtml(evalPayload.status || 'not_linked')}</span>
                    ${evalPayload.source ? `<span class="source-pill">${this.escapeHtml(evalPayload.source)}</span>` : ''}
                    ${evalPayload.case_id ? `<span class="source-pill">case=${this.escapeHtml(evalPayload.case_id)}</span>` : ''}
                </div>
                <p>${this.escapeHtml(evalPayload.summary || evalPayload.message || '单次评测暂未绑定')}</p>
                ${metrics.length ? `
                    <div class="replay-eval-metric-list">
                        ${metrics.map((metric) => this.renderReplayEvaluationMetric(metric)).join('')}
                    </div>
                ` : ''}
                ${this.renderReplayMiniList('未通过指标', failedMetrics)}
            </section>
        `;
    }
,
    renderReplayEvaluationMetric(metric) {
        const item = metric || {};
        const tone = this.evaluationMetricTone(item);
        return `
            <article class="replay-eval-metric ${tone}">
                <div>
                    <strong>${this.escapeHtml(item.label || item.key || '指标')}</strong>
                    <span>${this.escapeHtml(item.description || '')}</span>
                </div>
                <b>${this.escapeHtml(this.formatReplayEvaluationValue(item))}</b>
            </article>
        `;
    }
,
    evaluationMetricTone(metric) {
        const status = metric?.status || 'unknown';
        if (['passed', 'success'].includes(status)) return 'success';
        if (['failed', 'error'].includes(status)) return 'error';
        if (['warning', 'unknown'].includes(status)) return 'warning';
        return '';
    }
,
    formatReplayEvaluationValue(metric) {
        const value = metric?.value;
        if (value === null || value === undefined || value === '') return '未绑定';
        if (metric.value_type === 'boolean') {
            const normalized = String(value).toLowerCase();
            if (['true', 'passed', 'success', '1'].includes(normalized)) return '通过';
            if (['false', 'failed', 'error', '0'].includes(normalized)) return '未通过';
            return value ? '通过' : '未通过';
        }
        if (metric.value_type === 'unknown') return '未绑定';
        if (metric.value_type === 'percent') return this.formatPercent(value);
        if (metric.value_type === 'duration_ms') return `${this.formatNumber(value)} ms`;
        if (metric.value_type === 'integer') return this.formatInteger(value);
        return String(value);
    }
,
    renderReplayCountStrip(title, counts) {
        const entries = Object.entries(counts || {});
        if (entries.length === 0) return '';
        return `
            <p><strong>${this.escapeHtml(title)}：</strong></p>
            <div class="source-strip">
                ${entries.slice(0, 8).map(([key, value]) => `
                    <span class="source-pill">${this.escapeHtml(key)}=${this.escapeHtml(String(value))}</span>
                `).join('')}
            </div>
        `;
    }
,
    renderReplayMiniList(title, values) {
        const items = Array.isArray(values) ? values.filter(Boolean) : [];
        if (items.length === 0) return '';
        return `
            <div class="replay-mini-list">
                <strong>${this.escapeHtml(title)}</strong>
                <ul>
                    ${items.slice(0, 5).map((item) => `<li>${this.escapeHtml(item)}</li>`).join('')}
                </ul>
            </div>
        `;
    }
});
