// Live AIOps request handling, SSE parsing, form templates, and message rendering.
Object.assign(window.AutoOnCallApp.prototype, {
    async sendAIOpsRequest(loadingMessageElement, incident = null, liveRunState = null) {
        const selectedIncident = incident || this.getSelectedAIOpsIncident();
        const response = await this.apiFetch(`${this.apiBaseUrl}/aiops`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: this.sessionId,
                incident: selectedIncident
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP错误: ${response.status}`);
        }

        let fullResponse = '';
        const aiopsRun = liveRunState || this.createAIOpsRunState();
        if (selectedIncident && !aiopsRun.incident) {
            aiopsRun.incident = selectedIncident;
            aiopsRun.incidentId = selectedIncident.incident_id || aiopsRun.incidentId;
        }
        this.activeAIOpsRun = aiopsRun;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        try {
            while (true) {
                const { done, value } = await reader.read();

                if (done) {
                    if (fullResponse) {
                        this.updateAIOpsMessage(
                            loadingMessageElement,
                            fullResponse,
                            this.buildAIOpsDetails(aiopsRun)
                        );
                    }
                    break;
                }

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    const parsedLine = this.parseSseLine(line);
                    if (parsedLine.ignored) {
                        continue;
                    }
                    if (parsedLine.done) {
                        this.updateAIOpsMessage(
                            loadingMessageElement,
                            fullResponse,
                            this.buildAIOpsDetails(aiopsRun)
                        );
                        await this.refreshAfterAIOpsRun(aiopsRun);
                        return;
                    }

                    const sseMessage = this.parseSseJson(parsedLine.rawData);
                    if (!sseMessage) {
                        aiopsRun.errors.push('无法解析诊断事件 JSON');
                        continue;
                    }

                    fullResponse = this.applyAIOpsEvent(sseMessage, aiopsRun, fullResponse);
                    if (sseMessage.type === 'error') {
                        throw new Error(sseMessage.data || sseMessage.message || '智能运维分析失败');
                    }
                    if (sseMessage.type === 'complete' || sseMessage.type === 'done') {
                        this.updateAIOpsMessage(
                            loadingMessageElement,
                            fullResponse,
                            this.buildAIOpsDetails(aiopsRun)
                        );
                        await this.refreshAfterAIOpsRun(aiopsRun);
                        return;
                    }
                    this.updateAIOpsStreamContent(loadingMessageElement, fullResponse);
                }
            }
        } finally {
            reader.releaseLock();
        }
    }
,
    async refreshAfterAIOpsRun(runState) {
        if (!runState || !runState.incidentId) return;
        this.selectedIncidentId = runState.incidentId;
        this.saveLastAIOpsRunState(runState, {
            status: runState.status || 'running',
            updated_at: this.currentIsoTime(),
            has_report: Boolean(runState.structuredReport)
        });
        await Promise.allSettled([
            this.refreshIncidents(),
            this.refreshAIOpsRuns(),
            this.refreshApprovals()
        ]);
        if (this.currentWorkbenchView !== 'chat') {
            await this.refreshSelectedIncidentPanels();
        }
        if (runState.sessionId) {
            await this.refreshAIOpsRunStatus(runState.sessionId, {
                runState,
                silent: true
            });
        }
    }
,
    parseSseLine(line) {
        const trimmed = line.trim();
        if (
            trimmed === '' ||
            trimmed.startsWith('id:') ||
            trimmed.startsWith('event:') ||
            trimmed.startsWith('retry:')
        ) {
            return { ignored: true };
        }
        if (!line.startsWith('data:')) {
            return { ignored: true };
        }

        const rawData = line.substring(5).trim();
        return {
            ignored: false,
            done: rawData === '[DONE]',
            rawData
        };
    }
,
    parseSseJson(rawData) {
        try {
            const message = JSON.parse(rawData);
            return message && typeof message.type === 'string' ? message : null;
        } catch {
            return null;
        }
    }
,
    applyAIOpsEvent(message, runState, currentText) {
        this.collectAIOpsEvent(message, runState);
        this.renderLiveAIOpsProgress(message, runState);
        return this.reduceAIOpsEvent(message, runState, currentText);
    }
,
    reduceAIOpsEvent(message, runState, currentText) {
        const reducers = {
            content: () => currentText + (message.data || ''),
            plan: () => currentText + `\n\n## 执行计划\n${message.message || '诊断计划已生成'}\n`,
            step_complete: () => currentText + `\n- ${message.message || '步骤执行完成'}\n`,
            status: () => currentText + `\n${message.message || '诊断流程更新'}\n`,
            approval_required: () => currentText + this.formatAIOpsApprovalEvent(message),
            report: () => currentText + `\n\n## 诊断报告\n\n${message.report || ''}\n`,
            complete: () => this.buildFinalAIOpsResponse(currentText, message, runState),
            done: () => currentText,
            error: () => currentText + `\n\n诊断异常：${message.message || message.data || '未知错误'}\n`
        };
        const reducer = reducers[message.type] || (() => currentText + `\n${message.message || message.data || ''}\n`);
        return reducer();
    }
,
    getSelectedAIOpsIncident() {
        return this.buildAIOpsIncidentFromForm();
    }
,
    buildAIOpsIncidentFromForm() {
        const title = this.readInputValue(this.aiOpsTitle);
        const serviceName = this.readInputValue(this.aiOpsServiceName);
        const symptom = this.readInputValue(this.aiOpsSymptom);
        const environment = this.readInputValue(this.aiOpsEnvironment);
        const incidentId = this.readInputValue(this.aiOpsIncidentId);
        const severity = this.aiOpsSeverity ? this.aiOpsSeverity.value : 'P2';
        const rawAlertText = this.readInputValue(this.aiOpsRawAlert);
        const hasUserInput = [title, serviceName, symptom, environment, incidentId, rawAlertText]
            .some((value) => value.length > 0);

        if (!hasUserInput) {
            return null;
        }

        const resolvedServiceName = serviceName || 'unknown-service';
        return {
            incident_id: incidentId || this.generateAIOpsIncidentId(resolvedServiceName),
            title: title || `${resolvedServiceName} 故障诊断`,
            service_name: resolvedServiceName,
            severity: severity || 'P2',
            symptom: symptom || title || '用户提交的故障诊断请求',
            environment: environment || 'unknown',
            raw_alert: this.parseRawAlertInput(rawAlertText)
        };
    }
,
    readInputValue(element) {
        return element && typeof element.value === 'string' ? element.value.trim() : '';
    }
,
    generateAIOpsIncidentId(serviceName) {
        const normalizedService = String(serviceName || 'incident')
            .toUpperCase()
            .replace(/[^A-Z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 24) || 'INCIDENT';
        return `INC-${normalizedService}-${Date.now().toString(36).toUpperCase()}`;
    }
,
    parseRawAlertInput(value) {
        const rawText = String(value || '').trim();
        if (!rawText) return {};
        try {
            const parsed = JSON.parse(rawText);
            return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
                ? parsed
                : { payload: parsed };
        } catch {
            return { description: rawText };
        }
    }
,
    lookupAIOpsTemplate(preset) {
        const selectedPreset = preset || (this.aiOpsPresetSelect ? this.aiOpsPresetSelect.value : 'default');
        if (selectedPreset === 'default') {
            return null;
        }
        const canonicalPreset = this.aiOpsDemoIncidentAliases[selectedPreset] || selectedPreset;
        const backendPreset = this.aiOpsDemoIncidents[canonicalPreset] || this.aiOpsDemoIncidents[selectedPreset];
        if (backendPreset) {
            return JSON.parse(JSON.stringify(backendPreset));
        }
        const presets = this.getFallbackAIOpsDemoIncidents();
        const fallbackPreset = presets[canonicalPreset] || presets[selectedPreset] || null;
        return fallbackPreset ? JSON.parse(JSON.stringify(fallbackPreset)) : null;
    }
,
    applyAIOpsTemplate(preset) {
        const template = this.lookupAIOpsTemplate(preset);
        if (!template) {
            this.setAIOpsFormStatus('手动输入', 'warning');
            return;
        }
        this.populateAIOpsIncidentForm(template);
        this.setAIOpsFormStatus('模板已填充', 'success');
    }
,
    populateAIOpsIncidentForm(incident) {
        if (!incident) return;
        this.setInputValue(this.aiOpsTitle, incident.title || '');
        this.setInputValue(this.aiOpsServiceName, incident.service_name || '');
        this.setInputValue(this.aiOpsEnvironment, incident.environment || '');
        this.setInputValue(this.aiOpsIncidentId, incident.incident_id || '');
        this.setInputValue(this.aiOpsSymptom, incident.symptom || '');
        if (this.aiOpsSeverity) {
            this.aiOpsSeverity.value = incident.severity || 'P2';
        }
        this.setInputValue(
            this.aiOpsRawAlert,
            incident.raw_alert && Object.keys(incident.raw_alert).length > 0
                ? JSON.stringify(incident.raw_alert, null, 2)
                : ''
        );
    }
,
    setInputValue(element, value) {
        if (element) {
            element.value = value;
        }
    }
,
    clearAIOpsForm() {
        if (this.diagnosisForm) {
            this.diagnosisForm.reset();
        }
        if (this.aiOpsPresetSelect) {
            this.aiOpsPresetSelect.value = 'default';
        }
        this.setInputValue(this.aiOpsTitle, '');
        this.setInputValue(this.aiOpsServiceName, '');
        this.setInputValue(this.aiOpsEnvironment, '');
        this.setInputValue(this.aiOpsIncidentId, '');
        this.setInputValue(this.aiOpsSymptom, '');
        this.setInputValue(this.aiOpsRawAlert, '');
        if (this.aiOpsSeverity) {
            this.aiOpsSeverity.value = 'P2';
        }
        this.clearLastAIOpsRunState();
        this.activeAIOpsRun = null;
        this.setAIOpsFormStatus('手动输入', 'warning');
    }
,
    setAIOpsFormStatus(text, tone = '') {
        if (!this.aiOpsFormStatus) return;
        this.aiOpsFormStatus.textContent = text;
        const directTone = ['success', 'warning', 'error'].includes(tone) ? tone : '';
        this.aiOpsFormStatus.className = directTone || (tone ? this.statusTone(tone) : '');
    }
,
    validateAIOpsIncident(incident) {
        if (!incident) {
            return '请先填写故障信息，或选择一个诊断模板';
        }
        if (!incident.title && !incident.symptom) {
            return '请填写故障标题或故障现象';
        }
        if (!incident.service_name || incident.service_name === 'unknown-service') {
            return '请填写影响服务';
        }
        return '';
    }
,
    beginLiveAIOpsRun(incident, runState) {
        if (!incident || !runState) return;
        runState.sessionId = this.sessionId;
        runState.incident = incident;
        runState.incidentId = incident.incident_id || runState.incidentId;
        runState.status = 'running';
        this.activeAIOpsRun = runState;
        this.selectedIncidentId = runState.incidentId;
        this.saveLastAIOpsRunState(runState, {
            status: 'running',
            incident,
            started_at: this.currentIsoTime()
        });
        this.upsertDashboardIncidentFromRun(runState, 'running', '诊断已提交，正在等待后端事件');
        this.upsertLiveExecutionStep(runState, {
            event_id: `start-${runState.incidentId}`,
            event_type: 'workflow_started',
            node_name: 'workflow',
            step_id: '',
            tool_name: '',
            status: 'running',
            summary: `已提交诊断：${incident.title || incident.symptom || runState.incidentId}`,
            data_source: 'frontend',
            latency_ms: 0,
            created_at: this.currentIsoTime()
        });
        this.renderPlanCards(runState.plan);
        this.renderExecutionSteps(runState.executionSteps);
        this.renderToolCallTable(runState.toolCalls);
        this.renderEvidenceCards(runState.evidence);
        this.renderDependencySignals([], runState.toolCalls);
        this.setResponseAttention(false);
    }
,
    renderLiveAIOpsProgress(message, runState) {
        if (!runState) return;
        const status = runState.status || (message && message.status) || 'running';
        const statusReason = (message && message.message) || '诊断流程运行中';
        this.upsertDashboardIncidentFromRun(runState, status, statusReason);
        this.renderPlanCards(runState.plan);
        this.renderExecutionSteps(runState.executionSteps);
        this.renderToolCallTable(runState.toolCalls);
        this.renderEvidenceCards(runState.evidence);
        this.renderDependencySignals([], runState.toolCalls);

        if (runState.pendingApproval) {
            this.setResponseAttention(true);
            if (!runState.approvalNotified) {
                runState.approvalNotified = true;
                this.showNotification('诊断已暂停：后续动作需要人工审批', 'warning');
            }
        }
    }
,
    upsertDashboardIncidentFromRun(runState, status, statusReason = '') {
        if (!runState || !runState.incidentId) return;
        const incident = runState.incident || {};
        const existing = this.dashboardState.incidents.find((item) => (
            item.incident_id === runState.incidentId
        ));
        const overview = {
            ...(existing || {}),
            incident_id: runState.incidentId,
            trace_id: runState.traceId || existing?.trace_id || '',
            status: status || existing?.status || 'running',
            status_metadata: runState.statusMetadata || existing?.status_metadata || null,
            status_reason: statusReason || existing?.status_reason || '',
            title: incident.title || existing?.title || runState.incidentId,
            service_name: incident.service_name || existing?.service_name || 'unknown-service',
            severity: incident.severity || existing?.severity || 'P2',
            environment: incident.environment || existing?.environment || 'unknown',
            summary: incident.symptom || existing?.summary || statusReason || '',
            root_cause: existing?.root_cause || '',
            approval_status: runState.pendingApproval?.status || existing?.approval_status || 'not_required',
            session_id: this.sessionId,
            trace_summary: {
                ...(existing?.trace_summary || {}),
                event_count: runState.eventCount,
                latest_event_type: runState.lastEventType || existing?.trace_summary?.latest_event_type || '',
                latest_event_status: status || existing?.trace_summary?.latest_event_status || ''
            },
            approval_summary: runState.pendingApproval
                ? {
                    total: 1,
                    by_status: { [runState.pendingApproval.status || 'pending']: 1 },
                    latest: runState.pendingApproval
                }
                : (existing?.approval_summary || { total: 0, by_status: {}, latest: null }),
            updated_at: this.currentIsoTime()
        };

        if (existing) {
            Object.assign(existing, overview);
        } else {
            this.dashboardState.incidents.unshift(overview);
        }
        this.renderIncidentList();
    }
,
    upsertLiveApproval(approval) {
        if (!approval || !approval.approval_id) return;
        const existingIndex = this.dashboardState.approvals.findIndex((item) => (
            item.approval_id === approval.approval_id
        ));
        if (existingIndex >= 0) {
            this.dashboardState.approvals[existingIndex] = {
                ...this.dashboardState.approvals[existingIndex],
                ...approval
            };
        } else {
            this.dashboardState.approvals.unshift(approval);
        }
        this.renderApprovals(this.dashboardState.approvals);
    }
,
    setResponseAttention(active) {
        const responseNav = document.querySelector('[data-workbench-view="response"]');
        if (responseNav) {
            responseNav.classList.toggle('has-attention', Boolean(active));
        }
    }
,
    getFallbackAIOpsDemoIncidents() {
        return {
            default: null,
            redis_maxclients: {
                incident_id: 'INC-REDIS-001',
                title: 'order-service Redis maxclients exhausted',
                service_name: 'order-service',
                severity: 'P1',
                symptom: 'Redis connection timeout，接口 5xx 上升，怀疑 maxclients 耗尽',
                environment: 'prod',
                raw_alert: {
                    alertname: 'RedisMaxClientsNearLimit',
                    dependency: 'redis-order',
                    connected_clients: 9800,
                    maxclients: 10000
                }
            },
            mysql_slow_query: {
                incident_id: 'INC-MYSQL-001',
                title: 'payment-service MySQL slow query latency',
                service_name: 'payment-service',
                severity: 'P2',
                symptom: '接口响应慢，日志出现 MySQL 慢查询和连接池等待',
                environment: 'prod',
                raw_alert: {
                    alertname: 'MySQLSlowQuerySpike',
                    dependency: 'mysql-payment',
                    slow_query_count: 42,
                    pool_waiting: 18
                }
            },
            pod_crashloop: {
                incident_id: 'INC-K8S-001',
                title: 'inventory-service Pod CrashLoopBackOff',
                service_name: 'inventory-service',
                severity: 'P1',
                symptom: 'Pod CrashLoopBackOff，重启次数快速增加',
                environment: 'prod',
                raw_alert: {
                    alertname: 'PodCrashLoopBackOff',
                    namespace: 'inventory',
                    pod: 'inventory-service-7f8d9c-abc12',
                    restarts: 12
                }
            },
            redpanda_lag: {
                incident_id: 'INC-RP-001',
                title: 'checkout-service Redpanda consumer lag',
                service_name: 'checkout-service',
                severity: 'P2',
                symptom: 'checkout-service 响应慢，订单消息积压，怀疑 Redpanda/Kafka topic 或 partition 异常',
                environment: 'prod',
                raw_alert: {
                    alertname: 'RedpandaConsumerLagHigh',
                    topic: 'redpanda-checkout',
                    consumer_group: 'checkout-service',
                    consumer_lag: 128400,
                    max_partition_lag: 79000
                }
            },
            forbidden_sql: {
                incident_id: 'INC-SQL-001',
                title: 'order-service forbidden unaudited SQL',
                service_name: 'order-service',
                severity: 'P1',
                symptom: '需要立即执行未审核 SQL 清理异常订单数据',
                environment: 'prod',
                raw_alert: {
                    requested_action: 'execute_sql',
                    sql: "DELETE FROM orders WHERE status = 'abnormal';",
                    audited: false,
                    reason: '业务方要求立刻清理异常订单'
                }
            }
        };
    }
,
    async loadAIOpsDemoIncidents() {
        if (!this.aiOpsPresetSelect) return;
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/aiops/demo/incidents`);
            if (!response.ok) return;
            const payload = await response.json();
            const items = Array.isArray(payload.items) ? payload.items : [];
            if (!items.length) return;

            this.aiOpsDemoIncidents = {};
            this.aiOpsDemoIncidentAliases = {};
            items.forEach((item) => {
                if (!item || !item.case_id || !item.incident) return;
                this.aiOpsDemoIncidents[item.case_id] = item.incident;
                (item.aliases || []).forEach((alias) => {
                    this.aiOpsDemoIncidentAliases[alias] = item.case_id;
                });
            });
            this.renderAIOpsPresetOptions(items);
            this.aiOpsDemoIncidentsLoaded = true;
        } catch (error) {
            console.warn('加载 AIOps 演示场景失败，使用前端兜底场景:', error);
        }
    }
,
    renderAIOpsPresetOptions(items) {
        if (!this.aiOpsPresetSelect) return;
        const currentValue = this.aiOpsPresetSelect.value || 'default';
        const canonicalCurrent = this.aiOpsDemoIncidentAliases[currentValue] || currentValue;
        this.aiOpsPresetSelect.innerHTML = '';

        const defaultOption = document.createElement('option');
        defaultOption.value = 'default';
        defaultOption.textContent = '手动输入';
        this.aiOpsPresetSelect.appendChild(defaultOption);

        items.forEach((item) => {
            if (!item || !item.case_id) return;
            const option = document.createElement('option');
            option.value = item.case_id;
            option.textContent = item.label || item.case_id;
            this.aiOpsPresetSelect.appendChild(option);
        });

        const hasCurrent = Array.from(this.aiOpsPresetSelect.options)
            .some((option) => option.value === canonicalCurrent);
        this.aiOpsPresetSelect.value = hasCurrent ? canonicalCurrent : 'default';
    }
,
    createAIOpsRunState() {
        return {
            sessionId: this.sessionId,
            incident: null,
            incidentId: '',
            traceId: '',
            eventCount: 0,
            lastEventType: '',
            plan: [],
            executionSteps: [],
            evidence: [],
            stepCount: 0,
            completedStepCount: 0,
            evidenceCount: 0,
            toolCalls: [],
            structuredReport: null,
            pendingApproval: null,
            approvalNotified: false,
            riskAssessment: null,
            traceSummary: null,
            status: '',
            statusMetadata: null,
            messages: [],
            errors: []
        };
    }
,
    collectAIOpsEvent(message, runState) {
        if (!message || typeof message !== 'object' || !runState) return;

        runState.eventCount += 1;
        runState.lastEventType = message.type || runState.lastEventType;
        runState.incidentId = message.incident_id || runState.incidentId;
        runState.traceId = message.trace_id || runState.traceId;
        runState.status = this.extractAIOpsStatus(message) || runState.status;
        if (message.message) {
            runState.messages.push(message.message);
        }
        if (runState.incidentId) {
            this.selectedIncidentId = runState.incidentId;
        }

        const traceEvent = message.trace_event || null;
        if (traceEvent) {
            runState.traceId = traceEvent.trace_id || runState.traceId;
        }

        const structuredReport = this.extractStructuredReport(message);
        if (structuredReport) {
            runState.structuredReport = structuredReport;
            runState.incidentId = structuredReport.incident_id || runState.incidentId;
            runState.traceId = structuredReport.trace_id || runState.traceId;
            runState.traceSummary = structuredReport.trace_summary || runState.traceSummary;
            runState.status = structuredReport.status || runState.status;
            if (Array.isArray(structuredReport.tool_calls)) {
                runState.toolCalls = structuredReport.tool_calls;
            }
            if (Array.isArray(structuredReport.evidence)) {
                runState.evidence = structuredReport.evidence;
                runState.evidenceCount = Math.max(runState.evidenceCount, structuredReport.evidence.length);
            }
        }

        if (message.pending_approval) {
            runState.pendingApproval = message.pending_approval;
        }
        if (message.risk_assessment) {
            runState.riskAssessment = message.risk_assessment;
        }
        if (Array.isArray(message.current_plan) && message.current_plan.length > 0) {
            runState.plan = this.normalizeAIOpsPlanItems(message.current_plan);
        } else if (Array.isArray(message.plan) && message.plan.length > 0) {
            runState.plan = this.normalizeAIOpsPlanItems(message.plan);
        }
        const liveStep = this.normalizeAIOpsLiveStep(message, runState);
        if (liveStep) {
            this.upsertLiveExecutionStep(runState, liveStep);
        }
        if (message.type === 'step_complete') {
            runState.completedStepCount += 1;
            runState.stepCount = runState.completedStepCount;
            if (Array.isArray(message.evidence)) {
                runState.evidence = message.evidence;
                runState.evidenceCount = Math.max(runState.evidenceCount, message.evidence.length);
            }
            if (Array.isArray(message.tool_call_records)) {
                runState.toolCalls = message.tool_call_records;
            }
            if (Array.isArray(message.errors)) {
                runState.errors = message.errors;
            }
            this.markAIOpsPlanProgress(runState, message);
        }
        if (message.type === 'approval_required' && message.pending_approval) {
            this.upsertLiveApproval(message.pending_approval);
        }
        if (message.type === 'error') {
            runState.errors.push(message.message || message.data || '诊断事件异常');
        }
        this.saveLastAIOpsRunState(runState, {
            status: runState.status || this.extractAIOpsStatus(message) || 'running',
            node_name: message.stage || '',
            updated_at: this.currentIsoTime(),
            pending_approval: runState.pendingApproval,
            has_report: Boolean(runState.structuredReport)
        });
    }
,
    normalizeAIOpsPlanItems(planItems) {
        const items = Array.isArray(planItems) ? planItems : [];
        return items.map((item, index) => {
            if (item && typeof item === 'object') {
                return {
                    ...item,
                    step_id: item.step_id || `step-${index + 1}`,
                    tool_name: item.tool_name || 'manual_analysis',
                    purpose: item.purpose || item.expected_evidence || item.summary || `步骤 ${index + 1}`,
                    expected_evidence: item.expected_evidence || item.purpose || '',
                    risk_level: item.risk_level || 'low',
                    status: item.status || 'pending'
                };
            }
            return {
                step_id: `step-${index + 1}`,
                tool_name: 'manual_analysis',
                purpose: String(item || `步骤 ${index + 1}`),
                expected_evidence: String(item || ''),
                risk_level: 'low',
                status: 'pending'
            };
        });
    }
,
    normalizeAIOpsLiveStep(message, runState) {
        if (!message || !runState) return null;
        if (message.type === 'step_complete') {
            const currentStep = message.current_step;
            const stepObject = currentStep && typeof currentStep === 'object' ? currentStep : {};
            const latestTool = this.getLatestToolCall(message.tool_call_records || runState.toolCalls);
            return {
                event_id: message.trace_event_id || `${message.type}-${runState.eventCount}`,
                event_type: 'step_complete',
                node_name: message.stage || 'executor',
                step_id: stepObject.step_id || latestTool.step_id || '',
                tool_name: stepObject.tool_name || latestTool.tool_name || '',
                status: latestTool.status || message.status || 'success',
                summary: message.result_preview || message.message || String(currentStep || '步骤执行完成'),
                data_source: latestTool.data_source || 'runtime',
                latency_ms: latestTool.latency_ms ?? 0,
                created_at: this.currentIsoTime()
            };
        }
        if (message.type === 'approval_required') {
            const approval = message.pending_approval || {};
            return {
                event_id: message.trace_event_id || `approval-${approval.approval_id || runState.eventCount}`,
                event_type: 'approval_request',
                node_name: message.stage || 'risk_controller',
                step_id: approval.step_id || '',
                tool_name: approval.tool_name || 'manual_approval',
                status: approval.status || 'pending',
                summary: approval.reason || message.message || '后续动作需要人工审批',
                data_source: 'approval',
                latency_ms: 0,
                created_at: this.currentIsoTime()
            };
        }
        if (message.trace_event) {
            return this.normalizeTraceEventAsExecutionStep(message.trace_event);
        }
        if (message.type === 'status' && message.message) {
            return {
                event_id: message.trace_event_id || `status-${runState.eventCount}`,
                event_type: 'status',
                node_name: message.stage || 'workflow',
                step_id: '',
                tool_name: '',
                status: message.status || 'running',
                summary: message.message,
                data_source: 'runtime',
                latency_ms: 0,
                created_at: this.currentIsoTime()
            };
        }
        return null;
    }
,
    normalizeTraceEventAsExecutionStep(traceEvent) {
        if (!traceEvent || typeof traceEvent !== 'object') return null;
        return {
            event_id: traceEvent.event_id || `trace-${Date.now()}`,
            event_type: traceEvent.event_type || 'node',
            node_name: traceEvent.node_name || 'workflow',
            step_id: traceEvent.step_id || '',
            tool_name: traceEvent.tool_name || '',
            status: traceEvent.status || 'running',
            summary: traceEvent.output_summary || traceEvent.error_message || traceEvent.input_summary || '节点已执行',
            data_source: traceEvent.metadata?.data_source || 'runtime',
            latency_ms: traceEvent.latency_ms ?? 0,
            created_at: traceEvent.created_at || this.currentIsoTime()
        };
    }
,
    getLatestToolCall(toolCalls) {
        const items = Array.isArray(toolCalls) ? toolCalls : [];
        return items.length > 0 ? items[items.length - 1] : {};
    }
,
    upsertLiveExecutionStep(runState, step) {
        if (!runState || !step) return;
        const key = step.event_id || `${step.event_type}-${step.step_id}-${step.summary}`;
        const existingIndex = runState.executionSteps.findIndex((item) => (
            (item.event_id || `${item.event_type}-${item.step_id}-${item.summary}`) === key
        ));
        if (existingIndex >= 0) {
            runState.executionSteps[existingIndex] = {
                ...runState.executionSteps[existingIndex],
                ...step
            };
        } else {
            runState.executionSteps.push(step);
        }
    }
,
    markAIOpsPlanProgress(runState, message) {
        if (!runState || !Array.isArray(runState.plan) || runState.plan.length === 0) return;
        const latestTool = this.getLatestToolCall(message.tool_call_records || runState.toolCalls);
        const currentStep = message.current_step && typeof message.current_step === 'object'
            ? message.current_step
            : {};
        const completedStepId = currentStep.step_id || latestTool.step_id || '';
        let matched = false;
        runState.plan = runState.plan.map((step, index) => {
            if (completedStepId && step.step_id === completedStepId) {
                matched = true;
                return { ...step, status: latestTool.status || 'completed' };
            }
            if (!completedStepId && index < runState.stepCount) {
                matched = true;
                return { ...step, status: step.status === 'pending' ? 'completed' : step.status };
            }
            return step;
        });
        if (!matched && runState.stepCount > 0 && runState.plan[runState.stepCount - 1]) {
            runState.plan[runState.stepCount - 1] = {
                ...runState.plan[runState.stepCount - 1],
                status: 'completed'
            };
        }
    }
,
    currentIsoTime() {
        return new Date().toISOString();
    }
,
    extractStructuredReport(message) {
        if (!message || typeof message !== 'object') return null;
        if (message.structured_report && typeof message.structured_report === 'object') {
            return message.structured_report;
        }
        if (message.diagnosis && message.diagnosis.structured_report) {
            return message.diagnosis.structured_report;
        }
        if (message.report && typeof message.report === 'object') {
            return message.report;
        }
        return null;
    }
,
    extractAIOpsStatus(message) {
        if (!message || typeof message !== 'object') return '';
        if (typeof message.status === 'string' && message.status) {
            return message.status;
        }
        const report = this.extractStructuredReport(message);
        if (report && typeof report.status === 'string') {
            return report.status;
        }
        if (message.diagnosis && typeof message.diagnosis.status === 'string') {
            return message.diagnosis.status;
        }
        return '';
    }
,
    formatAIOpsApprovalEvent(message) {
        const approval = message.pending_approval || {};
        const risk = message.risk_assessment || {};
        return [
            '',
            '',
            '## 人工审批',
            `- 动作：${approval.action || risk.action || '需要人工确认的后续动作'}`,
            `- 风险等级：${approval.risk_level || risk.risk_level || 'medium'}`,
            `- 状态：${approval.status || 'pending'}`,
            `- 原因：${approval.reason || risk.reason || '后续动作需要人工确认'}`
        ].join('\n');
    }
,
    buildFinalAIOpsResponse(currentText, message, runState) {
        let finalText = currentText || '';
        const report = this.extractStructuredReport(message) || runState.structuredReport;
        const finalMarkdown = (
            message.response ||
            (message.diagnosis && message.diagnosis.report) ||
            (report && report.markdown) ||
            ''
        ).trim();

        if (finalMarkdown && !this.isTextAlreadyIncluded(finalText, finalMarkdown)) {
            finalText += `\n\n${finalMarkdown}`;
        }

        const incidentId = runState.incidentId || (report && report.incident_id) || '';
        if (incidentId && !finalText.includes(`/api/incidents/${incidentId}`)) {
            finalText += [
                '',
                '',
                '## 诊断闭环索引',
                `- Incident：${incidentId}`,
                `- Trace：/api/incidents/${incidentId}/trace`,
                `- Report：/api/incidents/${incidentId}/report`,
                `- Approval：/api/incidents/${incidentId}/approval`,
                `- Changes：/api/incidents/${incidentId}/changes`
            ].join('\n');
        }
        return finalText;
    }
,
    buildAIOpsDetails(runState) {
        if (!runState) return [];

        const details = [];
        const report = runState.structuredReport;
        details.push(
            `事件概览：incident=${runState.incidentId || 'unknown'}；` +
            `trace=${runState.traceId || 'unknown'}；` +
            `状态=${runState.status || 'unknown'}；` +
            `SSE事件数=${runState.eventCount}；最近事件=${runState.lastEventType || 'unknown'}`
        );

        if (runState.plan.length > 0) {
            details.push(`执行计划：共 ${runState.plan.length} 个步骤；已完成 ${runState.stepCount} 个步骤`);
        }

        if (report) {
            details.push(
                `结构化报告：状态=${report.status || 'unknown'}；` +
                `服务=${report.service_name || 'unknown-service'}；` +
                `根因=${report.root_cause || '暂未形成明确根因'}；` +
                `置信度=${report.confidence ?? 'unknown'}`
            );
        }

        if (runState.traceSummary || (report && report.trace_summary)) {
            const traceSummary = runState.traceSummary || report.trace_summary;
            details.push(
                `Trace摘要：事件数=${traceSummary.event_count || 0}；` +
                `异常或阻断=${traceSummary.failed_or_blocked_count || 0}`
            );
        }

        if (runState.toolCalls.length > 0) {
            const toolSummary = runState.toolCalls
                .slice(0, 5)
                .map(call => `${call.tool_name || 'unknown'}:${call.status || 'unknown'}`)
                .join('，');
            details.push(`工具调用：${toolSummary}`);
        }

        if (runState.pendingApproval) {
            details.push(
                `人工审批：状态=${runState.pendingApproval.status || 'pending'}；` +
                `动作=${runState.pendingApproval.action || '待确认动作'}；` +
                `审批ID=${runState.pendingApproval.approval_id || 'unknown'}`
            );
        }

        if (runState.riskAssessment) {
            details.push(
                `风险评估：等级=${runState.riskAssessment.risk_level || 'unknown'}；` +
                `策略=${runState.riskAssessment.policy || 'unknown'}；` +
                `需要审批=${runState.riskAssessment.need_approval ? '是' : '否'}`
            );
        }

        if (runState.errors.length > 0) {
            details.push(`错误信息：${runState.errors.slice(0, 3).join('；')}`);
        }

        if (runState.incidentId) {
            details.push(
                `查询接口：/api/incidents/${runState.incidentId}；` +
                `/api/incidents/${runState.incidentId}/trace；` +
                `/api/incidents/${runState.incidentId}/report`
            );
        }
        return details;
    }
,
    isTextAlreadyIncluded(container, text) {
        if (!container || !text) return false;
        const compactContainer = container.replace(/\s+/g, ' ').trim();
        const compactText = text.replace(/\s+/g, ' ').trim();
        if (!compactText) return true;
        return compactContainer.includes(compactText.slice(0, Math.min(compactText.length, 200)));
    }

    // 更新智能运维流式内容（实时显示）
,
    updateAIOpsStreamContent(messageElement, content) {
        if (!messageElement) return;
        
        // 添加 aiops-message 类
        messageElement.classList.add('aiops-message');
        
        const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
        if (messageContentWrapper) {
            let messageContent = messageContentWrapper.querySelector('.message-content');
            if (!messageContent) {
                messageContent = document.createElement('div');
                messageContent.className = 'message-content';
                messageContentWrapper.appendChild(messageContent);
            }
            // 流式显示时使用纯文本
            messageContent.textContent = content;
            this.scrollToBottom();
        }
    }

    // 更新智能运维消息（带折叠详情）
,
    updateAIOpsMessage(messageElement, response, details) {        
        if (!messageElement) {
            // 如果没有传入消息元素，则创建新消息            return this.addAIOpsMessage(response, details);
        }

        // 添加aiops-message类
        messageElement.classList.add('aiops-message');

        // 获取消息内容包装器
        const messageContentWrapper = messageElement.querySelector('.message-content-wrapper');
        if (!messageContentWrapper) {
            console.error('未找到 message-content-wrapper');
            return;
        }

        // 清空现有内容（保留消息内容容器）
        const messageContent = messageContentWrapper.querySelector('.message-content');
        if (!messageContent) {
            console.error('未找到 message-content');
            return;
        }

        // 移除加载动画相关的类和内容
        messageContent.classList.remove('loading-message-content');
        messageContent.textContent = '';
        
        // 移除加载图标（如果存在）
        const loadingIcon = messageContent.querySelector('.loading-spinner-icon');
        if (loadingIcon) {
            loadingIcon.remove();
        }

        // 详情部分（可折叠）- 先显示
        if (details && details.length > 0) {
            // 检查是否已存在详情容器
            let detailsContainer = messageElement.querySelector('.aiops-details');
            if (!detailsContainer) {
                detailsContainer = document.createElement('div');
                detailsContainer.className = 'aiops-details';
                messageContentWrapper.insertBefore(detailsContainer, messageContent);
            } else {
                // 清空现有详情
                detailsContainer.innerHTML = '';
            }

            const detailsToggle = document.createElement('div');
            detailsToggle.className = 'details-toggle';
            detailsToggle.innerHTML = `
                <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>查看详细步骤 (${details.length}条)</span>
            `;

            const detailsContent = document.createElement('div');
            detailsContent.className = 'details-content';
            
            details.forEach((detail, index) => {
                const detailItem = document.createElement('div');
                detailItem.className = 'detail-item';
                detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${this.escapeHtml(detail)}`;
                detailsContent.appendChild(detailItem);
            });

            // 点击切换折叠状态
            detailsToggle.addEventListener('click', () => {
                detailsContent.classList.toggle('expanded');
                detailsToggle.classList.toggle('expanded');
            });

            detailsContainer.appendChild(detailsToggle);
            detailsContainer.appendChild(detailsContent);
        }

        // 更新主要响应内容（使用 Markdown 渲染）
        const renderedHtml = this.renderMarkdown(response);
        messageContent.innerHTML = renderedHtml;
        // 高亮代码块
        this.highlightCodeBlocks(messageContent);        
        // 保存到历史记录
        this.currentChatHistory.push({
            type: 'assistant',
            content: response,
            timestamp: new Date().toISOString()
        });
        
        this.scrollToBottom();
        return messageElement;
    }

    // 添加智能运维消息（带折叠详情）- 保留用于兼容性
,
    addAIOpsMessage(response, details) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant aiops-message';

        // 添加头像图标
        const messageAvatar = document.createElement('div');
        messageAvatar.className = 'message-avatar';
        messageAvatar.innerHTML = `
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="white"/>
            </svg>
        `;
        messageDiv.appendChild(messageAvatar);

        // 创建消息内容包装器
        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';

        // 详情部分（可折叠）- 先显示
        if (details && details.length > 0) {
            const detailsContainer = document.createElement('div');
            detailsContainer.className = 'aiops-details';

            const detailsToggle = document.createElement('div');
            detailsToggle.className = 'details-toggle';
            detailsToggle.innerHTML = `
                <svg class="toggle-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M9 18L15 12L9 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>查看详细步骤 (${details.length}条)</span>
            `;

            const detailsContent = document.createElement('div');
            detailsContent.className = 'details-content';
            
            details.forEach((detail, index) => {
                const detailItem = document.createElement('div');
                detailItem.className = 'detail-item';
                detailItem.innerHTML = `<strong>步骤 ${index + 1}:</strong> ${this.escapeHtml(detail)}`;
                detailsContent.appendChild(detailItem);
            });

            // 点击切换折叠状态
            detailsToggle.addEventListener('click', () => {
                detailsContent.classList.toggle('expanded');
                detailsToggle.classList.toggle('expanded');
            });

            detailsContainer.appendChild(detailsToggle);
            detailsContainer.appendChild(detailsContent);
            messageContentWrapper.appendChild(detailsContainer);
        }

        // 主要响应内容 - 后显示（使用Markdown渲染）
        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';
        messageContent.innerHTML = this.renderMarkdown(response);
        // 高亮代码块
        this.highlightCodeBlocks(messageContent);
        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);
        
        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            this.scrollToBottom();
        }

        return messageDiv;
    }

    // HTML转义
,
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // 触发智能运维（点击智能运维按钮时直接调用）
,
    async triggerAIOps() {
        if (this.isStreaming) {
            this.showNotification('请等待当前操作完成', 'warning');
            return;
        }

        const incident = this.getSelectedAIOpsIncident();
        const validationMessage = this.validateAIOpsIncident(incident);
        if (validationMessage) {
            this.setAIOpsFormStatus('待补充', 'warning');
            this.showNotification(validationMessage, 'warning');
            return;
        }
        
        this.isStreaming = true;
        this.sessionId = this.generateSessionId();
        this.currentIncidentTab = 'process';
        this.setAIOpsFormStatus('诊断运行中', 'warning');
        this.updateUI();
        await this.setWorkbenchView('incidents');
        const liveRunState = this.createAIOpsRunState();
        this.beginLiveAIOpsRun(incident, liveRunState);

        // 保留诊断过程消息，供用户回到知识问答时查看完整流式记录。
        const loadingMessage = this.addLoadingMessage(`分析中：${incident.title}`);
        this.currentAIOpsMessage = loadingMessage;

        try {
            await this.sendAIOpsRequest(loadingMessage, incident, liveRunState);
            await this.setWorkbenchView('incidents');
            this.setIncidentTab('process');
            this.setAIOpsFormStatus(
                this.formatRecoveredAIOpsStatusLabel(liveRunState.status || 'completed'),
                this.statusTone(liveRunState.status || 'completed')
            );
        } catch (error) {
            console.error('智能运维分析失败:', error);
            liveRunState.status = 'failed';
            liveRunState.errors.push(error.message || '智能运维分析失败');
            this.saveLastAIOpsRunState(liveRunState, {
                status: 'failed',
                error_message: error.message || '智能运维分析失败',
                updated_at: this.currentIsoTime()
            });
            this.setAIOpsFormStatus('诊断失败', 'error');
            this.showNotification('智能运维分析失败: ' + error.message, 'error');
            // 更新消息为错误信息
            if (loadingMessage) {
                const messageContent = loadingMessage.querySelector('.message-content');
                if (messageContent) {
                    messageContent.textContent = '抱歉，智能运维分析时出现错误：' + error.message;
                }
            }
        } finally {
            this.isStreaming = false;
            this.currentAIOpsMessage = null;
            this.updateUI();
        }
    }

    // 显示/隐藏加载遮罩层
,
    showLoadingOverlay(show) {
        if (this.loadingOverlay) {
            if (show) {
                this.loadingOverlay.style.display = 'flex';
                // 更新文字为智能运维
                const loadingText = this.loadingOverlay.querySelector('.loading-text');
                const loadingSubtext = this.loadingOverlay.querySelector('.loading-subtext');
                if (loadingText) loadingText.textContent = '智能运维分析中，请稍候...';
                if (loadingSubtext) loadingSubtext.textContent = '后端正在处理，请耐心等待';
                // 防止页面滚动
                document.body.style.overflow = 'hidden';
            } else {
                this.loadingOverlay.style.display = 'none';
                // 恢复页面滚动
                document.body.style.overflow = '';
            }
        }
    }

    // 显示/隐藏上传遮罩层
,
    showUploadOverlay(show, fileName = '') {
        if (this.loadingOverlay) {
            if (show) {
                this.loadingOverlay.style.display = 'flex';
                // 更新文字为上传中
                const loadingText = this.loadingOverlay.querySelector('.loading-text');
                const loadingSubtext = this.loadingOverlay.querySelector('.loading-subtext');
                if (loadingText) loadingText.textContent = '正在上传文件...';
                if (loadingSubtext) loadingSubtext.textContent = fileName ? `上传: ${fileName}` : '请稍候';
                // 防止页面滚动
                document.body.style.overflow = 'hidden';
            } else {
                this.loadingOverlay.style.display = 'none';
                // 恢复页面滚动
                document.body.style.overflow = '';
            }
        }
    }
});
