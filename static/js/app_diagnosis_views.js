// Diagnosis panels, approval/change flows, tool contracts, adapters, and eval views.
Object.assign(window.AutoOnCallApp.prototype, {
    renderPlanCards(plan) {
        const items = Array.isArray(plan) ? plan : [];
        if (this.planCount) this.planCount.textContent = String(items.length);
        if (!this.planList) return;
        if (items.length === 0) {
            this.planList.innerHTML = '<div class="empty-state">暂无结构化计划</div>';
            return;
        }
        this.planList.innerHTML = items.map((step, index) => `
            <article class="plan-card">
                <div class="plan-card-header">
                    <strong>${this.escapeHtml(step.purpose || step.expected_evidence || `步骤 ${index + 1}`)}</strong>
                    <span class="meta-pill ${this.statusTone(step.status)}">${this.escapeHtml(step.status || 'pending')}</span>
                </div>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(step.tool_name || 'manual_analysis')}</span>
                    <span class="source-pill">${this.escapeHtml(step.risk_level || 'low')}</span>
                    <span class="source-pill">${this.escapeHtml(step.step_id || `step-${index + 1}`)}</span>
                </div>
                ${step.expected_evidence ? `<p>${this.escapeHtml(step.expected_evidence)}</p>` : ''}
            </article>
        `).join('');
    }
,
    renderExecutionSteps(steps) {
        const items = Array.isArray(steps) ? steps : [];
        if (this.stepCount) this.stepCount.textContent = String(items.length);
        if (!this.stepList) return;
        if (items.length === 0) {
            this.stepList.innerHTML = '<div class="empty-state">暂无执行步骤</div>';
            return;
        }
        this.stepList.innerHTML = items.map((step) => `
            <article class="step-card">
                <div class="step-card-header">
                    <strong>${this.escapeHtml(step.event_type || 'step')} · ${this.escapeHtml(step.node_name || 'node')}</strong>
                    <span class="meta-pill ${this.statusTone(step.status)}">${this.escapeHtml(step.status || 'unknown')}</span>
                </div>
                <p>${this.escapeHtml(step.summary || '无摘要')}</p>
                <div class="source-strip">
                    ${this.sourcePill(step.data_source)}
                    <span class="source-pill">${this.escapeHtml(step.tool_name || step.step_id || '-')}</span>
                    <span class="source-pill">${this.escapeHtml(String(step.latency_ms ?? 0))} ms</span>
                    <span class="source-pill">${this.formatDateTime(step.created_at)}</span>
                </div>
            </article>
        `).join('');
    }
,
    renderToolCallTable(toolCalls) {
        const items = Array.isArray(toolCalls) ? toolCalls : [];
        if (this.toolCallCount) this.toolCallCount.textContent = String(items.length);
        if (!this.toolCallTable) return;
        if (items.length === 0) {
            this.toolCallTable.innerHTML = '<div class="empty-state">暂无工具调用</div>';
            return;
        }
        this.toolCallTable.innerHTML = `
            <table>
                <thead>
                    <tr>
                        <th>工具</th>
                        <th>来源</th>
                        <th>状态</th>
                        <th>耗时</th>
                        <th>摘要</th>
                    </tr>
                </thead>
                <tbody>
                    ${items.map((call) => `
                        <tr>
                            <td>${this.escapeHtml(call.tool_name || 'unknown')}</td>
                            <td>${this.sourcePill(call.data_source)}</td>
                            <td><span class="meta-pill ${this.statusTone(call.status)}">${this.escapeHtml(call.status || 'unknown')}</span></td>
                            <td>${this.escapeHtml(String(call.latency_ms ?? 0))} ms</td>
                            <td>
                                ${this.escapeHtml(call.output_summary || call.error_message || '无摘要')}
                                <br>
                                <span class="incident-summary">${this.escapeHtml(call.input_summary || '')}</span>
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
    }
,
    renderDependencySignals(signals, fallbackToolCalls = []) {
        const directSignals = Array.isArray(signals) ? signals : [];
        const items = directSignals.length > 0
            ? directSignals
            : this.extractDependencySignalsFromToolCalls(fallbackToolCalls);
        if (this.dependencySignalCount) this.dependencySignalCount.textContent = String(items.length);
        if (!this.dependencySignalList) return;
        if (items.length === 0) {
            this.dependencySignalList.innerHTML = '<div class="empty-state">暂无 Tracing 或 MQ 证据</div>';
            return;
        }
        this.dependencySignalList.innerHTML = items.map((call) => `
            <article class="dependency-signal-card">
                <div class="plan-card-header">
                    <strong>${this.escapeHtml(this.formatDependencySignalTitle(call))}</strong>
                    <span class="meta-pill ${this.statusTone(call.status)}">${this.escapeHtml(call.status || 'unknown')}</span>
                </div>
                <div class="source-strip">
                    ${this.sourcePill(call.data_source)}
                    <span class="source-pill">${this.escapeHtml(call.backend || call.domain || 'dependency')}</span>
                    ${call.stance ? `<span class="source-pill">${this.escapeHtml(call.stance)}</span>` : ''}
                    ${call.confidence !== undefined ? `<span class="source-pill">conf ${this.escapeHtml(this.formatConfidence(call.confidence))}</span>` : ''}
                    <span class="source-pill">${this.escapeHtml(String(call.latency_ms ?? 0))} ms</span>
                </div>
                <p>${this.escapeHtml(call.summary || call.output_summary || call.error_message || '无摘要')}</p>
                ${call.confidence_reason ? `<p>${this.escapeHtml(call.confidence_reason)}</p>` : ''}
            </article>
        `).join('');
    }
,
    extractDependencySignalsFromToolCalls() {
        return [];
    }
,
    formatDependencySignalTitle(signal) {
        return signal.tool_name || 'dependency signal';
    }
,
    renderEvidenceCards(evidence) {
        const items = Array.isArray(evidence) ? evidence : [];
        if (this.evidenceCount) this.evidenceCount.textContent = String(items.length);
        if (!this.evidenceList) return;
        if (items.length === 0) {
            this.evidenceList.innerHTML = '<div class="empty-state">暂无证据记录</div>';
            return;
        }
        this.evidenceList.innerHTML = items.map((item) => `
            <article class="evidence-card">
                <div class="evidence-card-header">
                    <strong>${this.escapeHtml(item.summary || item.fact || '证据')}</strong>
                    <span class="meta-pill">${this.formatConfidence(item.confidence)}</span>
                </div>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(item.evidence_type || 'unknown')}</span>
                    ${this.sourcePill(item.data_source)}
                    <span class="source-pill">${this.escapeHtml(item.stance || 'neutral')}</span>
                    <span class="source-pill">${this.escapeHtml(item.source_tool || 'unknown')}</span>
                </div>
                ${item.fact ? `<p><strong>事实：</strong>${this.escapeHtml(item.fact)}</p>` : ''}
                ${item.inference ? `<p><strong>推断：</strong>${this.escapeHtml(item.inference)}</p>` : ''}
                ${item.uncertainty ? `<p><strong>不确定：</strong>${this.escapeHtml(item.uncertainty)}</p>` : ''}
                ${item.next_step ? `<p><strong>下一步：</strong>${this.escapeHtml(item.next_step)}</p>` : ''}
            </article>
        `).join('');
    }
,
    renderConclusionView(chain) {
        const confidence = chain.confidence;
        if (this.confidenceBadge) {
            this.confidenceBadge.textContent = this.formatConfidence(confidence);
        }
        if (!this.conclusionView) return;
        const sourceSummary = chain.data_sources || {};
        const bySource = sourceSummary.by_source || {};
        const sourceBadges = Object.entries(bySource)
            .map(([source, count]) => this.sourcePill(source, { count }))
            .join('');
        this.conclusionView.innerHTML = `
            <section class="conclusion-block">
                <h3>可信度</h3>
                <p>${this.escapeHtml(chain.confidence_reason || '暂无置信度说明')}</p>
                <div class="source-strip">${sourceBadges || '<span class="source-pill">unknown=0</span>'}</div>
                ${sourceSummary.has_mock ? '<p>当前包含 Mock 数据，不能作为真实生产结论。</p>' : ''}
                ${sourceSummary.has_not_configured ? '<p>存在未配置适配器，相关证据不可用。</p>' : ''}
            </section>
            ${this.renderConclusionList('已确认事实', chain.confirmed_facts)}
            ${this.renderHypothesisRanking(chain.hypothesis_ranking || [], chain.selected_root_cause_id)}
            ${this.renderConclusionList('推断结论', chain.inferred_conclusions)}
            ${this.renderConclusionList('不确定项', chain.uncertainties)}
            ${this.renderConclusionList('下一步建议', chain.next_steps)}
            ${this.renderChangePlan(chain.change_plan || {})}
        `;
    }
,
    renderHypothesisRanking(items, selectedId) {
        const hypotheses = Array.isArray(items) ? items : [];
        if (hypotheses.length === 0) {
            return this.renderConclusionList('根因假设排序', []);
        }
        return `
            <section class="conclusion-block">
                <h3>根因假设排序</h3>
                ${hypotheses.slice(0, 5).map((item, index) => `
                    <article class="hypothesis-item">
                        <div class="hypothesis-title">
                            <strong>${index + 1}. ${this.escapeHtml(item.title || item.description || '未命名假设')}</strong>
                            <span class="meta-pill">${this.formatConfidence(item.confidence)}</span>
                            ${item.hypothesis_id === selectedId ? '<span class="meta-pill success">选中</span>' : ''}
                        </div>
                        <div class="source-strip">
                            <span class="source-pill">${this.escapeHtml(item.category || 'unknown')}</span>
                            <span class="source-pill">support=${this.escapeHtml(String((item.supporting_evidence_ids || []).length))}</span>
                            <span class="source-pill">refute=${this.escapeHtml(String((item.refuting_evidence_ids || []).length))}</span>
                        </div>
                        <p>${this.escapeHtml(item.confidence_reason || '暂无置信度原因')}</p>
                        ${this.renderTinyList('缺失证据', item.missing_evidence)}
                    </article>
                `).join('')}
            </section>
        `;
    }
,
    renderChangePlan(plan) {
        if (!plan || Object.keys(plan).length === 0) {
            return '';
        }
        return `
            <section class="conclusion-block">
                <h3>变更计划草案</h3>
                <p>${this.escapeHtml(plan.action || '未记录动作')}</p>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(plan.status || 'draft')}</span>
                    <span class="source-pill">${this.escapeHtml(plan.risk_level || 'medium')}</span>
                    <span class="source-pill">${this.escapeHtml(plan.change_plan_id || 'no-id')}</span>
                </div>
                ${this.renderTinyList('前置检查', plan.pre_checklist)}
                ${this.renderTinyList('人工执行', plan.execution_steps)}
                ${this.renderTinyList('回滚步骤', plan.rollback_steps)}
                ${this.renderTinyList('验证步骤', plan.verification_steps)}
            </section>
        `;
    }
,
    renderTinyList(title, values) {
        const items = Array.isArray(values) ? values.filter(Boolean) : [];
        if (items.length === 0) return '';
        return `
            <p><strong>${this.escapeHtml(title)}：</strong></p>
            <ul class="conclusion-list">${items.map((item) => `<li>${this.escapeHtml(item)}</li>`).join('')}</ul>
        `;
    }
,
    renderConclusionList(title, values) {
        const items = Array.isArray(values) ? values.filter(Boolean) : [];
        return `
            <section class="conclusion-block">
                <h3>${this.escapeHtml(title)}</h3>
                ${items.length > 0
                    ? `<ul class="conclusion-list">${items.map((item) => `<li>${this.escapeHtml(item)}</li>`).join('')}</ul>`
                    : '<p>暂无</p>'}
            </section>
        `;
    }
,
    renderTraceTimeline(trace) {
        const items = Array.isArray(trace.items) ? trace.items : [];
        if (this.traceCount) {
            this.traceCount.textContent = String(items.length);
        }
        if (!this.traceTimeline) return;
        if (items.length === 0) {
            this.traceTimeline.innerHTML = this.renderPanelState('empty', '暂无 Trace 事件', '当前 Incident 没有结构化事件流。');
            return;
        }
        this.traceTimeline.innerHTML = items.map((event) => `
            <article class="trace-event">
                <div class="trace-event-title">
                    ${this.escapeHtml(event.event_type || 'event')} · ${this.escapeHtml(event.status || 'unknown')}
                </div>
                <div class="trace-event-body">
                    ${this.escapeHtml(event.node_name || 'node')} · ${this.formatDateTime(event.created_at)}
                    <br>
                    ${this.escapeHtml(event.output_summary || event.error_message || '无摘要')}
                </div>
            </article>
        `).join('');
    }
,
    renderTraceTimelineError(error) {
        if (this.traceCount) this.traceCount.textContent = '0';
        if (this.traceTimeline) {
            this.traceTimeline.innerHTML = this.renderPanelState(
                'error',
                'Trace 加载失败',
                error?.message || '无法读取结构化事件流。'
            );
        }
    }
,
    renderReport(reportPayload) {
        const report = reportPayload.report || {};
        if (this.reportStatus) {
            this.reportStatus.textContent = report.status || '已加载';
        }
        this.renderDependencySignals(report.dependency_signals || [], report.tool_calls || []);
        if (!this.reportViewer) return;
        const markdown = reportPayload.markdown || reportPayload.report?.markdown || '';
        this.reportViewer.innerHTML = markdown
            ? this.renderMarkdown(markdown)
            : '<div class="empty-state">报告内容为空</div>';
        this.highlightCodeBlocks(this.reportViewer);
    }
,
    renderReportError(error) {
        if (this.reportStatus) this.reportStatus.textContent = '未生成';
        if (this.reportViewer) {
            this.reportViewer.innerHTML = this.renderPanelState(
                'error',
                '诊断报告加载失败',
                error?.message || '暂无诊断报告'
            );
        }
    }
,
    renderChangeExecutions(executions) {
        const items = Array.isArray(executions) ? executions : [];
        if (this.changeExecutionCount) {
            this.changeExecutionCount.textContent = String(items.length);
        }
        if (!this.changeExecutionList) return;
        if (items.length === 0) {
            this.changeExecutionList.innerHTML = this.renderPanelState('empty', '暂无执行记录', '当前没有安全变更执行记录。');
            return;
        }
        this.changeExecutionList.innerHTML = items.map((execution) => `
            <article class="change-execution-card" data-change-execution-id="${this.escapeHtml(execution.change_execution_id || '')}">
                <div class="change-execution-title">
                    <strong>${this.escapeHtml(execution.change_execution_id || 'change-execution')}</strong>
                    <span class="meta-pill ${this.statusTone(execution)}">${this.escapeHtml(execution.status_metadata?.label || execution.status || 'unknown')}</span>
                </div>
                <div class="source-strip">
                    <span class="source-pill">${this.escapeHtml(execution.mode || 'dry_run_only')}</span>
                    <span class="source-pill">plan=${this.escapeHtml(execution.change_plan_id || 'unknown')}</span>
                    <span class="source-pill">approval=${this.escapeHtml(execution.approval_id || 'unknown')}</span>
                    <span class="source-pill">${this.escapeHtml(this.formatDateTime(execution.updated_at || execution.created_at))}</span>
                </div>
                <div class="change-stage-list">
                    ${this.renderChangeStages(execution)}
                </div>
                ${this.renderChangeExecutionActions(execution)}
            </article>
        `).join('');
    }
,
    renderChangeExecutionError(error) {
        if (this.changeExecutionCount) this.changeExecutionCount.textContent = '0';
        if (this.changeExecutionList) {
            this.changeExecutionList.innerHTML = this.renderPanelState(
                'error',
                '执行记录加载失败',
                error?.message || '暂无执行记录'
            );
        }
    }
,
    renderChangeStages(execution) {
        const stages = Array.isArray(execution?.stages) && execution.stages.length > 0
            ? execution.stages
            : this.buildLegacyChangeStages(execution || {});
        return stages.map((stage) => {
            if (!stage || typeof stage !== 'object') return '';
            return this.renderChangeStage(
                stage.label || stage.key || 'Stage',
                { status: stage.status, reason: stage.reason || stage.recommendation },
                stage.status || 'pending',
                stage.reason || ''
            );
        }).join('');
    }
,
    buildLegacyChangeStages(execution) {
        return [
            {
                key: 'pre_check',
                label: 'Pre-check',
                status: execution.pre_check?.status || (execution.status === 'precheck_running' ? 'running' : 'pending'),
                reason: execution.pre_check?.reason || '未执行',
            },
            {
                key: 'dry_run',
                label: 'Dry-run',
                status: execution.dry_run?.status || (execution.status === 'dry_run_running' ? 'running' : 'pending'),
                reason: execution.dry_run?.reason || '未执行',
            },
            {
                key: 'execute',
                label: 'Execute',
                status: this.formatExecutionStageStatus(execution),
                reason: this.formatExecutionStageReason(execution),
            },
            {
                key: 'observe',
                label: 'Observe',
                status: execution.observation?.status || (execution.status === 'observing' ? 'running' : 'pending'),
                reason: execution.observation?.reason || execution.observation?.recommendation || '未执行',
            },
        ];
    }
,
    renderChangeStage(label, result, fallbackStatus = 'pending', fallbackReason = '') {
        const status = result?.status || fallbackStatus || 'pending';
        const reason = result?.reason || result?.recommendation || fallbackReason || '未执行';
        return `
            <div class="change-stage">
                <span>${this.escapeHtml(label)}</span>
                <strong class="${this.statusTone(status)}">${this.escapeHtml(status)}</strong>
                <p>${this.escapeHtml(reason)}</p>
            </div>
        `;
    }
,
    formatExecutionStageStatus(execution) {
        if (execution.status === 'waiting_manual_execution') return 'waiting_manual_execution';
        if (execution.status === 'sandbox_executing') return 'sandbox_executing';
        if (execution.status === 'manual_execution_recorded') return 'manual_execution_recorded';
        if (['dry_run_completed', 'sandbox_validated', 'closed', 'rollback_recommended', 'escalated'].includes(execution.status)) {
            return execution.manual_result && Object.keys(execution.manual_result).length > 0
                ? execution.status
                : (execution.status === 'sandbox_validated' ? 'passed' : 'skipped');
        }
        return 'pending';
    }
,
    formatExecutionStageReason(execution) {
        if (execution.status === 'waiting_manual_execution') {
            return 'dry-run 通过，等待人工提交执行结果。';
        }
        if (execution.status === 'sandbox_executing') {
            return '沙箱执行中，不调用生产写接口。';
        }
        if (execution.manual_result && Object.keys(execution.manual_result).length > 0) {
            return execution.manual_result.notes || `人工执行结果：${execution.manual_result.status || 'recorded'}`;
        }
        if (execution.status === 'dry_run_completed') {
            return 'dry-run 已完成，未执行生产变更。';
        }
        if (execution.status === 'sandbox_validated') {
            return '沙箱执行和观察通过，未调用生产写接口。';
        }
        if (execution.status === 'closed') {
            return '流程已关闭，未自动执行生产变更。';
        }
        if (execution.status === 'rollback_recommended') {
            return execution.rollback_result?.reason || '观察未通过，建议回滚或升级。';
        }
        if (execution.status === 'escalated') {
            return execution.rollback_result?.reason || '安全边界阻断，已升级处理。';
        }
        return '未执行';
    }
,
    renderChangeExecutionActions(execution) {
        if (execution.status !== 'waiting_manual_execution') return '';
        return `
            <textarea class="approval-comment" data-manual-notes="${this.escapeHtml(execution.change_execution_id || '')}" placeholder="记录人工执行窗口、执行人、指标观察或回滚原因"></textarea>
            <div class="approval-actions">
                <button class="action-btn primary" data-manual-result="succeeded" data-change-execution-id="${this.escapeHtml(execution.change_execution_id || '')}">记录成功</button>
                <button class="action-btn danger" data-manual-result="failed" data-change-execution-id="${this.escapeHtml(execution.change_execution_id || '')}">记录失败</button>
            </div>
        `;
    }
,
    async refreshApprovals() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/approvals/pending?include_approved_actions=true`);
            this.setDashboardItems('approvals', data.items);
            this.renderApprovals(this.dashboardState.approvals);
        } catch (error) {
            this.renderApprovalsError(error);
        }
    }
,
    renderApprovals(approvals) {
        const items = Array.isArray(approvals) ? approvals : [];
        const pendingItems = items.filter((approval) => approval.status === 'pending');
        const approvedActionItems = items.filter((approval) => this.approvalHasNextAction(approval));
        const historyItems = items.filter((approval) => (
            approval.status !== 'pending' && !this.approvalHasNextAction(approval)
        ));
        if (this.approvalCount) {
            this.approvalCount.textContent = `待审 ${pendingItems.length} · 推进 ${approvedActionItems.length}`;
        }
        if (!this.approvalList) return;
        this.approvalList.innerHTML = `
            <div class="response-workflow">
                ${this.renderApprovalStage(
                    '待我审批',
                    '需要人工判断是否允许后续诊断或变更动作。',
                    pendingItems,
                    '暂无待审批请求',
                    { showDecisionActions: true }
                )}
                ${this.renderApprovalStage(
                    '已批准待推进',
                    '审批已通过，等待更新诊断闭环或进入安全变更流程。',
                    approvedActionItems,
                    '暂无已批准待推进事项',
                    { showNextActions: true }
                )}
                ${historyItems.length > 0 ? this.renderApprovalStage(
                    '审批历史',
                    '已完成或已终止的审批记录，只保留状态追溯。',
                    historyItems,
                    '',
                    {}
                ) : ''}
            </div>
        `;
    }
,
    renderApprovalsError(error) {
        if (this.approvalCount) this.approvalCount.textContent = '0';
        if (this.approvalList) {
            this.approvalList.innerHTML = this.renderPanelState(
                'error',
                '审批记录加载失败',
                error?.message || '无法读取审批记录。'
            );
        }
    }
,
    approvalHasNextAction(approval) {
        const changePlan = approval?.change_plan || {};
        return approval?.status === 'approved' && (
            Boolean(approval?.approval_id) || Boolean(changePlan.change_plan_id)
        );
    }
,
    renderApprovalStage(title, description, approvals, emptyText, options = {}) {
        const items = Array.isArray(approvals) ? approvals : [];
        const body = items.length > 0
            ? items.map((approval) => this.renderApprovalItem(approval, options)).join('')
            : `<div class="empty-state">${this.escapeHtml(emptyText || '暂无记录')}</div>`;
        return `
            <section class="response-stage">
                <header class="response-stage-header">
                    <div>
                        <strong>${this.escapeHtml(title)}</strong>
                        <span>${this.escapeHtml(description)}</span>
                    </div>
                    <span class="meta-pill">${items.length}</span>
                </header>
                <div class="response-stage-body">${body}</div>
            </section>
        `;
    }
,
    renderApprovalItem(approval, options = {}) {
        const changePlan = approval.change_plan || {};
        const decisionReason = approval.decision_reason || '';
        const decidedBy = approval.decided_by || '未处理';
        const decidedAt = approval.decided_at ? this.formatDateTime(approval.decided_at) : '未处理';
        const showDecisionActions = Boolean(options.showDecisionActions) && approval.status === 'pending';
        const showNextActions = Boolean(options.showNextActions) && approval.status === 'approved';
        const canStartChange = showNextActions && changePlan.change_plan_id;
        return `
            <article class="approval-item">
                <div class="incident-title-row">
                    <strong>${this.escapeHtml(approval.action || '待确认动作')}</strong>
                    <span class="meta-pill ${this.statusTone(approval.status)}">${this.escapeHtml(approval.status || 'unknown')}</span>
                </div>
                <p class="approval-reason">${this.escapeHtml(approval.reason || approval.decision_reason || '无审批说明')}</p>
                <div class="meta-row">
                    <span class="meta-pill">${this.escapeHtml(approval.incident_id || 'unknown-incident')}</span>
                    <span class="meta-pill">${this.escapeHtml(approval.risk_level || 'unknown')}</span>
                    <span class="meta-pill">${this.escapeHtml(approval.tool_name || 'manual')}</span>
                    ${changePlan.change_plan_id ? `<span class="meta-pill">${this.escapeHtml(changePlan.change_plan_id)}</span>` : ''}
                    <span class="meta-pill">处理人：${this.escapeHtml(decidedBy)}</span>
                    <span class="meta-pill">处理时间：${this.escapeHtml(decidedAt)}</span>
                </div>
                ${decisionReason ? `<p class="approval-reason">审批意见：${this.escapeHtml(decisionReason)}</p>` : ''}
                ${showDecisionActions ? `
                    <textarea class="approval-comment" data-approval-reason="${this.escapeHtml(approval.approval_id)}" placeholder="填写审批意见，例如已确认变更窗口、缺少回滚方案"></textarea>
                    <div class="approval-actions">
                        <button class="action-btn primary" data-approval-decision="approve" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}">通过</button>
                        <button class="action-btn danger" data-approval-decision="reject" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}">拒绝</button>
                    </div>
                ` : ''}
                ${showNextActions ? `
                    <div class="response-next-actions">
                        <button class="action-btn primary" data-diagnosis-resume data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}">更新诊断闭环</button>
                        ${canStartChange ? `
                            <button class="action-btn" data-change-resume data-change-mode="dry_run_only" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}" data-change-plan-id="${this.escapeHtml(changePlan.change_plan_id)}">安全变更 dry-run</button>
                            <button hidden data-change-resume data-change-mode="sandbox" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}" data-change-plan-id="${this.escapeHtml(changePlan.change_plan_id)}">沙箱验证</button>
                            <button hidden data-change-resume data-change-mode="manual_record" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}" data-change-plan-id="${this.escapeHtml(changePlan.change_plan_id)}">记录人工变更</button>
                        ` : ''}
                    </div>
                ` : ''}
            </article>
        `;
    }
,
    async submitApprovalDecision(incidentId, approvalId, decision, reason = '') {
        if (!incidentId || !approvalId || !decision) return;
        const normalizedReason = (reason || '').trim() || (
            decision === 'approve'
                ? '前端审批通过，未填写额外意见'
                : '前端审批拒绝，未填写额外意见'
        );
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/approval`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    approval_id: approvalId,
                    decision,
                    decided_by: 'frontend-operator',
                    reason: normalizedReason
                })
            });
            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }
            const payload = await response.json();
            const resolvedApprovalId = payload.approval?.approval_id || approvalId;
            let diagnosisResumed = false;
            if (decision === 'approve') {
                diagnosisResumed = await this.resumeDiagnosisWorkflow(
                    incidentId,
                    resolvedApprovalId,
                    { silent: true }
                );
            }
            const notification = decision === 'approve'
                ? (
                    diagnosisResumed
                        ? '审批通过，诊断闭环已更新'
                        : '审批已通过，诊断闭环可手动更新'
                )
                : '审批状态已更新';
            this.showNotification(notification, diagnosisResumed || decision !== 'approve' ? 'success' : 'warning');
            this.selectedIncidentId = incidentId;
            await this.refreshIncidents();
            await this.refreshSelectedIncidentPanels();
        } catch (error) {
            this.showNotification('审批失败: ' + error.message, 'error');
        }
    }
,
    async resumeDiagnosisWorkflow(incidentId, approvalId, options = {}) {
        if (!incidentId || !approvalId) return false;
        const silent = Boolean(options.silent);
        const openReport = Boolean(options.openReport);
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/diagnosis/resume`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    approval_id: approvalId
                })
            });
            const responseText = await response.text();
            const events = this.parseSseEventsFromText(responseText);
            const terminalEvent = events[events.length - 1] || {};
            if (!response.ok || terminalEvent.type === 'error') {
                throw new Error(terminalEvent.message || `HTTP错误: ${response.status}`);
            }
            this.selectedIncidentId = incidentId;
            if (openReport) {
                await this.setWorkbenchView('report');
            } else if (!silent) {
                await this.refreshIncidents();
                await this.refreshSelectedIncidentPanels();
            }
            if (!silent) {
                this.showNotification(`诊断闭环已更新：${terminalEvent.status || 'complete'}`, 'success');
            }
            return true;
        } catch (error) {
            if (!silent) {
                this.showNotification('诊断闭环更新失败: ' + error.message, 'error');
            }
            return false;
        }
    }
,
    async startSafeChangeWorkflow(incidentId, changePlanId, approvalId, mode = 'dry_run_only') {
        if (!incidentId || !changePlanId || !approvalId) return;
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/changes/${encodeURIComponent(changePlanId)}/resume`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    approval_id: approvalId,
                    mode: mode || 'dry_run_only',
                    operator: 'frontend-operator'
                })
            });
            const responseText = await response.text();
            const events = this.parseSseEventsFromText(responseText);
            const terminalEvent = events[events.length - 1] || {};
            if (!response.ok || terminalEvent.type === 'error') {
                throw new Error(terminalEvent.message || `HTTP错误: ${response.status}`);
            }
            this.showNotification(`安全变更流程已更新：${terminalEvent.status || 'complete'}`, 'success');
            this.selectedIncidentId = incidentId;
            await this.setWorkbenchView('changes');
        } catch (error) {
            this.showNotification('安全变更启动失败: ' + error.message, 'error');
        }
    }
,
    async submitManualChangeResult(changeExecutionId, status, notes = '') {
        if (!changeExecutionId || !status) return;
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/changes/${encodeURIComponent(changeExecutionId)}/manual-result`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    status,
                    operator: 'frontend-operator',
                    notes: (notes || '').trim() || `前端记录人工执行结果：${status}`,
                    observed_metrics: {
                        frontend_manual_recorded: true
                    }
                })
            });
            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }
            this.showNotification('人工执行结果已记录', 'success');
            await this.refreshSelectedIncidentPanels();
        } catch (error) {
            this.showNotification('人工结果记录失败: ' + error.message, 'error');
        }
    }
,
    parseSseEventsFromText(text) {
        const events = [];
        let dataLines = [];
        for (const rawLine of String(text || '').split(/\r?\n/)) {
            const line = rawLine.trimEnd();
            if (!line) {
                if (dataLines.length > 0) {
                    const parsed = this.parseSseJson(dataLines.join('\n'));
                    if (parsed) events.push(parsed);
                    dataLines = [];
                }
                continue;
            }
            if (line.startsWith('data:')) {
                dataLines.push(line.substring(5).trimStart());
            }
        }
        if (dataLines.length > 0) {
            const parsed = this.parseSseJson(dataLines.join('\n'));
            if (parsed) events.push(parsed);
        }
        return events;
    }
,
    async refreshEvalSummary() {
        try {
            const [evalResult, scorecardResult, ragasResult] = await Promise.allSettled([
                this.apiGet(`${this.apiBaseUrl}/eval/summary`),
                this.apiGet(`${this.apiBaseUrl}/eval/scorecard`),
                this.apiGet(`${this.apiBaseUrl}/eval/ragas`)
            ]);
            if (evalResult.status === 'fulfilled') {
                this.setDashboardState('evalSummary', evalResult.value);
            } else {
                this.setDashboardState('evalSummary', {
                    available: false,
                    message: evalResult.reason?.message || '离线评测摘要不可用'
                });
            }
            if (scorecardResult.status === 'fulfilled') {
                this.setDashboardState('interviewScorecard', scorecardResult.value);
            } else {
                this.setDashboardState('interviewScorecard', {
                    available: false,
                    message: scorecardResult.reason?.message || '面试 scorecard 不可用'
                });
            }
            if (ragasResult.status === 'fulfilled') {
                this.setDashboardState('ragasSummary', ragasResult.value);
            } else {
                this.setDashboardState('ragasSummary', {
                    available: false,
                    message: ragasResult.reason?.message || 'RAGAS 质量摘要不可用'
                });
            }
            this.renderEvalSummary(
                this.dashboardState.evalSummary,
                this.dashboardState.ragasSummary,
                this.dashboardState.interviewScorecard
            );
        } catch (error) {
            if (this.evalStatus) this.evalStatus.textContent = '不可用';
            if (this.evalSummary) {
                this.evalSummary.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }
,
    async refreshAdapterVerification() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/eval/adapter-verification`);
            this.setDashboardState('adapterVerification', data);
            this.renderAdapterVerification(data);
        } catch (error) {
            this.setDashboardState('adapterVerification', null);
            if (this.adapterVerifyStatus) this.adapterVerifyStatus.textContent = '未生成';
            if (this.adapterVerification) {
                this.adapterVerification.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message || '暂无适配器验收报告，请运行 make sandbox-verify')}</div>`;
            }
        }
    }
,
    async refreshToolContracts() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/aiops/tools/contracts`);
            this.setDashboardItems('toolContracts', data.items);
            this.setDashboardState('toolContractsError', '');
            this.renderToolContracts(data);
        } catch (error) {
            this.setDashboardItems('toolContracts', []);
            this.setDashboardState('toolContractsError', error.message || '工具契约不可用');
            this.renderToolContracts(null);
        }
    }
,
    renderToolContracts(payload) {
        if (payload && Array.isArray(payload.items)) {
            this.setDashboardItems('toolContracts', payload.items);
        }
        const contracts = Array.isArray(this.dashboardState.toolContracts)
            ? this.dashboardState.toolContracts
            : [];
        if (this.toolContractCount) {
            this.toolContractCount.textContent = this.dashboardState.toolContractsError
                ? '不可用'
                : `${contracts.length} 个工具`;
        }
        if (this.toolContractSummary) {
            this.toolContractSummary.innerHTML = this.renderToolContractSummary();
        }
    }
,
    renderToolContractSummary() {
        const contracts = Array.isArray(this.dashboardState.toolContracts)
            ? this.dashboardState.toolContracts
            : [];
        if (this.dashboardState.toolContractsError) {
            return `
                <div class="empty-state">${this.escapeHtml(this.dashboardState.toolContractsError)}</div>
            `;
        }
        if (contracts.length === 0) {
            return '<div class="empty-state">暂无诊断工具契约</div>';
        }

        const readOnlyCount = contracts.filter((contract) => contract.read_only === true).length;
        const elevatedRiskCount = contracts.filter((contract) => ['medium', 'high'].includes(contract.risk_level)).length;
        const sources = Array.from(new Set(
            contracts.flatMap((contract) => Array.isArray(contract.data_sources) ? contract.data_sources : [])
        )).sort();
        const visibleContracts = contracts.slice(0, 8);
        const hiddenCount = Math.max(0, contracts.length - visibleContracts.length);

        return `
            <div class="metric-grid">
                <div class="metric-tile">
                    <span>工具总数</span>
                    <strong>${this.escapeHtml(String(contracts.length))}</strong>
                    <p>来自后端工具契约接口。</p>
                </div>
                <div class="metric-tile">
                    <span>只读工具</span>
                    <strong>${this.escapeHtml(String(readOnlyCount))}</strong>
                    <p>只做查询或诊断，不直接变更系统。</p>
                </div>
                <div class="metric-tile">
                    <span>中高风险</span>
                    <strong>${this.escapeHtml(String(elevatedRiskCount))}</strong>
                    <p>需要审批或人工复核。</p>
                </div>
                <div class="metric-tile">
                    <span>数据源覆盖</span>
                    <strong>${this.escapeHtml(String(sources.length))}</strong>
                    <p>${this.escapeHtml(sources.slice(0, 4).join(', ') || '未声明数据源')}</p>
                </div>
            </div>
            <div class="eval-detail-panel tool-contract-summary">
                <div>
                    <span>AIOps 工具能力</span>
                    <p>共 ${this.escapeHtml(String(contracts.length))} 个工具，${this.escapeHtml(String(readOnlyCount))} 个只读，${this.escapeHtml(String(elevatedRiskCount))} 个中高风险。</p>
                    <div class="source-strip">
                        ${sources.slice(0, 8).map((source) => this.sourcePill(source)).join('') || '<span class="source-pill">未声明数据源</span>'}
                    </div>
                </div>
                <div class="tool-call-table compact-tool-contracts">
                    <table>
                        <thead>
                            <tr>
                                <th>工具</th>
                                <th>风险</th>
                                <th>边界</th>
                                <th>数据源</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${visibleContracts.map((contract) => `
                                <tr>
                                    <td>${this.escapeHtml(contract.name || 'unknown')}</td>
                                    <td><span class="meta-pill ${this.riskTone(contract.risk_level)}">${this.escapeHtml(contract.risk_level || 'unknown')}</span></td>
                                    <td>${this.escapeHtml(this.formatToolContractApproval(contract))}</td>
                                    <td>${this.escapeHtml((contract.data_sources || []).slice(0, 3).join(', ') || '未声明')}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
                ${hiddenCount ? `<p>另有 ${this.escapeHtml(String(hiddenCount))} 个工具已隐藏在摘要之外，完整契约仍来自后端接口。</p>` : ''}
            </div>
        `;
    }
,
    formatToolContractApproval(contract) {
        if (contract.read_only === false) {
            return '需要审批';
        }
        if (contract.risk_level === 'high') {
            return '高风险需复核';
        }
        return '无需审批';
    }
,
    renderAdapterVerification(payload) {
        if (!this.adapterVerification && !this.adapterVerifyStatus) return;
        const externalSystems = this.dashboardState.health?.checks?.external_systems || {};
        const externalChecks = externalSystems.checks || {};
        const externalEntries = Object.entries(externalChecks);
        const externalStatus = externalSystems.status || 'unknown';
        const mockFallbackEnabled = Boolean(externalSystems.mock_fallback_enabled);
        const configuredCount = externalEntries.filter(([, check]) => check.configured === true).length;
        const unavailableCount = externalEntries.filter(([, check]) => {
            const statusText = check.status || (check.configured ? 'configured' : 'not_configured');
            return ['unhealthy', 'error', 'not_configured', 'unavailable'].includes(statusText);
        }).length;
        const externalRows = externalEntries
            .map(([name, check]) => {
                const statusText = check.status || (check.configured ? 'configured' : 'not_configured');
                const detailText = check.error_type || check.message || '';
                const suffix = detailText ? ` · ${detailText}` : '';
                return `
                    <article class="adapter-check-item">
                        <div class="incident-title-row">
                            <strong>${this.escapeHtml(name)}</strong>
                            <span class="meta-pill ${this.statusTone(statusText)}">${this.escapeHtml(statusText)}</span>
                        </div>
                        <p>${this.escapeHtml(suffix || '后端健康检查已返回该适配器状态')}</p>
                    </article>
                `;
            })
            .join('');
        const readinessOverview = `
            <div class="metric-grid">
                <div class="metric-tile">
                    <span>外部系统状态</span>
                    <strong>${this.escapeHtml(externalStatus)}</strong>
                    <p>来自 /health/ready 的 external_systems 检查。</p>
                </div>
                <div class="metric-tile">
                    <span>已配置适配器</span>
                    <strong>${this.escapeHtml(String(configuredCount))}</strong>
                    <p>共 ${this.escapeHtml(String(externalEntries.length))} 个外部系统检查项。</p>
                </div>
                <div class="metric-tile">
                    <span>异常或未配置</span>
                    <strong>${this.escapeHtml(String(unavailableCount))}</strong>
                    <p>用于判断诊断证据是否可能降级。</p>
                </div>
                <div class="metric-tile">
                    <span>Mock 回退</span>
                    <strong>${mockFallbackEnabled ? 'enabled' : 'disabled'}</strong>
                    <p>开启时诊断可演示，但不能代表生产真实结论。</p>
                </div>
            </div>
            <div class="eval-detail-panel">
                <div>
                    <span>健康检查明细</span>
                    <div class="adapter-check-list">
                        ${externalRows || '<div class="empty-state">暂无外部系统健康检查明细</div>'}
                    </div>
                </div>
            </div>
        `;
        if (!payload) {
            if (this.adapterVerifyStatus) {
                this.adapterVerifyStatus.textContent = externalStatus === 'unknown' ? '未生成' : externalStatus;
                this.adapterVerifyStatus.className = this.statusTone(externalStatus);
            }
            if (this.adapterVerification) {
                this.adapterVerification.innerHTML = `
                    ${readinessOverview}
                    <div class="empty-state">暂无适配器验收报告，请运行 make sandbox-verify</div>
                `;
            }
            return;
        }
        if (payload.available === false) {
            if (this.adapterVerifyStatus) {
                this.adapterVerifyStatus.textContent = '未生成';
                this.adapterVerifyStatus.className = this.statusTone('empty');
            }
            if (this.adapterVerification) {
                this.adapterVerification.innerHTML = `
                    ${readinessOverview}
                    <div class="empty-state">${this.escapeHtml(payload.message || '暂无适配器验收报告，请运行 make sandbox-verify')}</div>
                `;
            }
            return;
        }
        const status = payload.status || 'unknown';
        const checks = Array.isArray(payload.checks) ? payload.checks : [];
        if (this.adapterVerifyStatus) {
            this.adapterVerifyStatus.textContent = status;
            this.adapterVerifyStatus.className = status === 'passed' ? 'success' : 'error';
        }
        if (!this.adapterVerification) return;
        const sourceBadges = (payload.data_sources || [])
            .map((source) => this.sourcePill(source))
            .join('');
        const failedBadges = (payload.failed_tools || []).length > 0
            ? payload.failed_tools.map((tool) => `<span class="source-pill failed">${this.escapeHtml(tool)}</span>`).join('')
            : '<span class="source-pill success">无失败工具</span>';
        this.adapterVerification.innerHTML = `
            ${readinessOverview}
            <div class="metric-grid">
                <div class="metric-tile">
                    <span>验收状态</span>
                    <strong>${this.escapeHtml(status)}</strong>
                    <p>${this.escapeHtml(payload.summary || '')}</p>
                </div>
                <div class="metric-tile">
                    <span>真实数据源</span>
                    <strong>${this.escapeHtml(String((payload.data_sources || []).length))}</strong>
                    <p>mock_fallback_detected=${payload.mock_fallback_detected ? 'true' : 'false'}</p>
                </div>
                <div class="metric-tile">
                    <span>执行耗时</span>
                    <strong>${this.escapeHtml(String(payload.duration_ms || 0))} ms</strong>
                    <p>由 scripts/sandbox/verify_full_stack_adapters.py 生成。</p>
                </div>
                <div class="metric-tile">
                    <span>失败工具</span>
                    <strong>${this.escapeHtml(String((payload.failed_tools || []).length))}</strong>
                    <p>${this.escapeHtml((payload.failed_tools || []).join(', ') || '全部通过')}</p>
                </div>
            </div>
            <div class="eval-detail-panel">
                <div>
                    <span>已接入数据源</span>
                    <div class="source-strip">${sourceBadges || '<span class="source-pill">无</span>'}</div>
                </div>
                <div>
                    <span>失败工具</span>
                    <div class="source-strip">${failedBadges}</div>
                </div>
                <div>
                    <span>检查明细</span>
                    <div class="adapter-check-list">
                        ${checks.map((item) => `
                            <article class="adapter-check-item">
                                <div class="incident-title-row">
                                    <strong>${this.escapeHtml(item.tool_name || 'unknown')}</strong>
                                    <span class="meta-pill ${item.passed ? 'success' : 'error'}">${item.passed ? 'PASS' : 'FAIL'}</span>
                                </div>
                                <div class="source-strip">
                                    ${this.sourcePill(item.observed_source)}
                                    <span class="source-pill">expected=${this.escapeHtml(item.expected_source || '')}</span>
                                    <span class="source-pill">${this.escapeHtml(String(item.latency_ms ?? 0))} ms</span>
                                </div>
                                <p>${this.escapeHtml(item.summary || item.error_message || '无摘要')}</p>
                            </article>
                        `).join('')}
                    </div>
                </div>
            </div>
        `;
    }
,
    renderEvalSummary(
        payload,
        ragasPayload = this.dashboardState.ragasSummary,
        scorecardPayload = this.dashboardState.interviewScorecard
    ) {
        const available = Boolean(payload && payload.available);
        const ragasAvailable = Boolean(ragasPayload && ragasPayload.available);
        const scorecardAvailable = Boolean(scorecardPayload && scorecardPayload.available);
        if (this.evalStatus) {
            this.evalStatus.textContent = available || ragasAvailable || scorecardAvailable ? '已加载' : '未生成';
        }
        if (!this.evalSummary) return;
        if (!available && !ragasAvailable && !scorecardAvailable) {
            this.evalSummary.innerHTML = `<div class="empty-state">${this.escapeHtml(payload?.message || '暂无评测摘要')}</div>`;
            return;
        }
        const dashboard = this.resolveEvalDashboard(payload);
        const metrics = Array.isArray(dashboard.metrics) ? dashboard.metrics : [];
        const failedCases = Array.isArray(payload.failed_cases) ? payload.failed_cases : [];
        const artifacts = dashboard.artifacts || {};
        const artifactRows = Object.entries(artifacts)
            .map(([name, path]) => `
                <span class="source-pill">${this.escapeHtml(name)}=${this.escapeHtml(String(path))}</span>
            `)
            .join('');
        const failedCaseRows = failedCases.length > 0
            ? failedCases.map((caseId) => `<span class="source-pill error">${this.escapeHtml(String(caseId))}</span>`).join('')
            : '<span class="source-pill success">无失败用例</span>';
        this.evalSummary.innerHTML = `
            ${this.renderInterviewScorecard(scorecardPayload)}
            <div class="metric-grid">
                ${metrics.map((metric) => `
                    <div class="metric-tile">
                        <span>${this.escapeHtml(metric.label || metric.key || '指标')}</span>
                        <strong>${this.escapeHtml(this.formatEvalMetric(metric))}</strong>
                        <p>${this.escapeHtml(metric.description || '')}</p>
                    </div>
                `).join('')}
            </div>
            <div class="eval-detail-panel">
                <div>
                    <span>评测范围</span>
                    <p>${this.escapeHtml(dashboard.scope || payload.run?.evaluation_scope || '未声明')}</p>
                </div>
                <div>
                    <span>生成时间</span>
                    <p>${this.escapeHtml(this.formatDateTime(dashboard.generated_at || payload.run?.ended_at || payload.run?.started_at))}</p>
                </div>
                <div>
                    <span>复现命令</span>
                    <code>${this.escapeHtml(dashboard.command || payload.run?.command || '未记录')}</code>
                </div>
                <div>
                    <span>评测产物</span>
                    <div class="source-strip">${artifactRows || '<span class="source-pill">未记录</span>'}</div>
                </div>
                <div>
                    <span>失败用例</span>
                    <div class="source-strip">${failedCaseRows}</div>
                </div>
            </div>
            ${this.renderRagasQualityPanel(ragasPayload)}
        `;
    }
,
    renderInterviewScorecard(payload) {
        if (!payload || payload.available === false) {
            return `
                <div class="eval-detail-panel interview-scorecard-panel">
                    <div>
                        <span>面试 Scorecard</span>
                        <p>${this.escapeHtml(payload?.message || '暂无同一 run 的 scorecard，请运行 make interview-summary')}</p>
                    </div>
                </div>
            `;
        }
        const run = payload.run || {};
        const summary = payload.summary || {};
        const modules = Array.isArray(payload.modules) ? payload.modules : [];
        const moduleRows = modules.map((item) => {
            const failed = Array.isArray(item.failed_cases) ? item.failed_cases : [];
            const failureText = failed.length > 0
                ? (failed[0].id || failed[0].key || String(failed[0]))
                : '-';
            const metric = Array.isArray(item.metrics) && item.metrics.length > 0
                ? item.metrics[0]
                : null;
            const ratio = metric?.numerator !== undefined && metric?.denominator !== undefined
                ? `${metric.numerator}/${metric.denominator}`
                : '';
            const ci = metric?.confidence_interval || {};
            const ciText = ci.lower !== undefined && ci.upper !== undefined
                ? `${this.formatPercent(ci.lower)}-${this.formatPercent(ci.upper)}`
                : '-';
            const metricText = metric
                ? `${metric.key}=${this.formatEvalMetric({
                    value: metric.value,
                    value_type: String(metric.key || '').includes('latency') ? 'duration_ms' : 'percent'
                })}${ratio ? ` (${ratio})` : ''}`
                : '-';
            return `
                <tr>
                    <td>${this.escapeHtml(item.label || item.key || '模块')}</td>
                    <td><span class="meta-pill">${this.escapeHtml(item.evidence_level || 'unknown')}</span></td>
                    <td>${this.escapeHtml(String(item.sample_count ?? 0))}</td>
                    <td><span class="meta-pill ${this.statusTone(item.status)}">${this.escapeHtml(item.status || 'missing')}</span></td>
                    <td>${this.escapeHtml(metricText)}</td>
                    <td>${this.escapeHtml(ciText)}</td>
                    <td>${this.escapeHtml(String(item.failed_case_count ?? 0))}</td>
                    <td><code>${this.escapeHtml(item.artifact_path || '-')}</code></td>
                    <td>${this.escapeHtml(failureText)}</td>
                </tr>
            `;
        }).join('');
        return `
            <div class="eval-detail-panel interview-scorecard-panel">
                <div>
                    <span>面试 Scorecard</span>
                    <div class="source-strip">
                        <span class="source-pill ${this.statusTone(summary.status)}">${this.escapeHtml(summary.status || 'unknown')}</span>
                        <span class="source-pill">run=${this.escapeHtml(run.run_id || 'missing')}</span>
                        <span class="source-pill">baseline=${this.escapeHtml(summary.baseline_status || 'missing')}</span>
                        <span class="source-pill warning">production=${this.escapeHtml(summary.production_status || 'not_enough_data')}</span>
                        <span class="source-pill">commit=${this.escapeHtml(run.environment?.git_commit || 'missing')}</span>
                    </div>
                    <p>${this.escapeHtml(summary.production_boundary || 'Production 指标仅在真实生产样本充分时展示。')}</p>
                </div>
                <div class="tool-call-table compact-tool-contracts scorecard-table">
                    <table>
                        <thead>
                            <tr>
                                <th>模块</th>
                                <th>证据</th>
                                <th>样本</th>
                                <th>状态</th>
                                <th>核心指标</th>
                                <th>95% CI</th>
                                <th>失败</th>
                                <th>原始产物</th>
                                <th>失败 Case</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${moduleRows || '<tr><td colspan="9">暂无 scorecard 模块</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    }
,
    renderRagasQualityPanel(payload) {
        if (!payload || payload.available === false) {
            return `
                <div class="eval-detail-panel ragas-quality-panel">
                    <div>
                        <span>RAGAS 答案质量门禁</span>
                        <p>${this.escapeHtml(payload?.message || '暂无 RAGAS 质量摘要，请运行 eval_ragas_cases.py')}</p>
                    </div>
                </div>
            `;
        }
        const dashboard = this.resolveEvalDashboard(payload);
        const metrics = Array.isArray(dashboard.metrics) ? dashboard.metrics : [];
        const summary = payload.summary || {};
        const run = payload.run || {};
        const failedCases = Array.isArray(payload.failed_cases) ? payload.failed_cases : [];
        const visibleCases = Array.isArray(payload.case_scores)
            ? payload.case_scores.slice(0, 6)
            : [];
        const metricKeys = new Set([
            'ragas_pass_rate',
            'ragas_core_pass_rate',
            'ragas_id_recall',
            'ragas_id_precision',
            'ragas_actionability',
            'ragas_refusal_boundary',
            'ragas_faithfulness',
            'ragas_relevancy'
        ]);
        const visibleMetrics = metrics.filter((metric) => metricKeys.has(metric.key));
        const failedCaseRows = failedCases.length > 0
            ? failedCases.map((item) => `
                <article class="adapter-check-item">
                    <div class="incident-title-row">
                        <strong>${this.escapeHtml(item.id || 'unknown')}</strong>
                        <span class="meta-pill error">${this.escapeHtml(item.suggested_backlog_category || 'quality_gate')}</span>
                    </div>
                    <p>${this.escapeHtml((item.failed_metrics || []).join(', ') || 'unknown failure')}</p>
                </article>
            `).join('')
            : '<div class="empty-state">RAGAS 门禁无失败用例</div>';
        const caseRows = visibleCases.map((item) => `
            <tr>
                <td>${this.escapeHtml(item.id || 'unknown')}</td>
                <td><span class="meta-pill ${item.passed ? 'success' : 'error'}">${item.passed ? 'PASS' : 'FAIL'}</span></td>
                <td>${this.escapeHtml((item.tags || []).join(', ') || item.case_type || 'positive')}</td>
                <td>${this.escapeHtml((item.failed_metrics || []).join(', ') || '-')}</td>
            </tr>
        `).join('');
        return `
            <div class="eval-detail-panel ragas-quality-panel">
                <div>
                    <span>RAGAS 答案质量门禁</span>
                    <div class="source-strip">
                        <span class="source-pill ${this.statusTone(summary.status)}">${this.escapeHtml(summary.status || 'unknown')}</span>
                        <span class="source-pill">profile=${this.escapeHtml(dashboard.profile || run.metric_profile || 'unknown')}</span>
                        <span class="source-pill">answer=${this.escapeHtml(dashboard.answer_source || run.answer_source || 'unknown')}</span>
                        <span class="source-pill">judge=${this.escapeHtml(dashboard.judge_model || run.judge_model || 'not_required')}</span>
                    </div>
                    <p>把 RAGAS ID context、citation guard、拒答边界和 OnCall actionability 组合成固定用例质量回归。</p>
                </div>
                <div class="metric-grid">
                    ${visibleMetrics.map((metric) => `
                        <div class="metric-tile">
                            <span>${this.escapeHtml(metric.label || metric.key || '指标')}</span>
                            <strong>${this.escapeHtml(this.formatEvalMetric(metric))}</strong>
                            <p>${this.escapeHtml(metric.description || '')}</p>
                        </div>
                    `).join('')}
                </div>
                <div>
                    <span>失败门禁与改进队列</span>
                    <div class="adapter-check-list">${failedCaseRows}</div>
                </div>
                <div class="tool-call-table compact-tool-contracts">
                    <table>
                        <thead>
                            <tr>
                                <th>Case</th>
                                <th>结果</th>
                                <th>标签</th>
                                <th>未通过指标</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${caseRows || '<tr><td colspan="4">暂无 RAGAS case 明细</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        `;
    }
,
    resolveEvalDashboard(payload) {
        const dashboard = payload?.dashboard;
        if (dashboard && Array.isArray(dashboard.metrics)) {
            return dashboard;
        }
        return this.buildLegacyEvalDashboard(payload || {});
    }
,
    buildLegacyEvalDashboard(payload) {
        const summary = payload.summary || {};
        const metrics = payload.resume_metrics || {};
        const rag = payload.rag || {};
        return {
            generated_at: payload.run?.ended_at || payload.run?.started_at,
            scope: payload.run?.evaluation_scope || '',
            command: payload.run?.command || '',
            artifacts: payload.run?.artifacts || {},
            metrics: [
                { key: 'total_cases', label: '总用例数', value: summary.overall_case_count, value_type: 'integer', description: 'AIOps 与 RAG 离线评测用例总数。' },
                { key: 'overall_pass_rate', label: '总通过率', value: summary.overall_pass_rate, value_type: 'percent', description: '全部离线用例的通过比例。' },
                { key: 'aiops_pass_rate', label: 'AIOps 用例通过率', value: metrics.aiops_pass_rate ?? summary.pass_rate, value_type: 'percent', description: '故障诊断、工具、风险和报告链路的离线通过率。' },
                { key: 'rag_pass_rate', label: 'RAG 用例通过率', value: rag.pass_rate, value_type: 'percent', description: 'Runbook 检索评测用例的通过比例。' },
                { key: 'root_cause_hit_rate', label: '根因识别通过率', value: metrics.root_cause_hit_rate, value_type: 'percent', description: '报告根因是否命中评测集期望关键词。' },
                { key: 'tool_hit_rate', label: '工具选择通过率', value: metrics.tool_hit_rate, value_type: 'percent', description: 'Planner 是否选择了期望诊断工具。' },
                { key: 'approval_recall', label: '审批触发通过率', value: metrics.approval_recall, value_type: 'percent', description: '需要人工确认的动作是否进入审批链路。' },
                { key: 'forbidden_action_block_rate', label: '禁止动作识别通过率', value: metrics.forbidden_action_block_rate, value_type: 'percent', description: '危险动作是否被风险控制层阻断。' },
                { key: 'rag_retrieval_citation_metadata_rate', label: 'RAG 检索引用元数据率', value: metrics.rag_citation_coverage_rate ?? rag.citation_coverage_rate, value_type: 'percent', description: '相关检索结果是否具备 source_file + chunk_id；不代表生成答案实际引用。' },
                { key: 'p95_latency_ms', label: 'p95 延迟', value: metrics.p95_latency_ms ?? summary.p95_latency_ms, value_type: 'duration_ms', description: '离线评测单 case 执行耗时的 p95。' }
            ]
        };
    }
,
    formatEvalMetric(metric) {
        const value = metric?.value;
        if (value === null || value === undefined || value === '') return '-';
        if (metric.value_type === 'percent') return this.formatPercent(value);
        if (metric.value_type === 'duration_ms') return `${this.formatNumber(value)} ms`;
        if (metric.value_type === 'integer') return this.formatInteger(value);
        return String(value);
    }
});
