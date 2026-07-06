// Conversation history, persisted AIOps run state, and knowledge upload state.
Object.assign(window.AutoOnCallApp.prototype, {
    toggleToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭工具菜单
,
    closeToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 新建对话
,
    newChat() {
        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成后再新建对话', 'warning');
            return;
        }

        this.setWorkbenchView('chat');
        
        // 如果当前有对话内容，且不是从历史记录加载的，才保存为新的历史对话
        // 如果是从历史记录加载的，只需要更新该历史记录
        if (this.currentChatHistory.length > 0) {
            if (this.isCurrentChatFromHistory) {
                // 当前对话是从历史记录加载的，更新该历史记录
                this.updateCurrentChatHistory();
            } else {
                // 当前对话是新对话，保存为新的历史对话
                this.saveCurrentChat();
            }
        }
        
        // 停止所有进行中的操作
        this.isStreaming = false;
        
        // 清空输入框
        if (this.messageInput) {
            this.messageInput.value = '';
        }
        
        // 清空当前对话历史
        this.currentChatHistory = [];
        
        // 重置标记
        this.isCurrentChatFromHistory = false;
        
        // 清空聊天记录
        if (this.chatMessages) {
            this.chatMessages.innerHTML = '';
        }
        
        // 生成新的会话ID
        this.sessionId = this.generateSessionId();
        
        // 重置模式为快速
        this.currentMode = 'quick';
        this.updateUI();
        
        // 重新设置居中样式（确保对话框居中显示）
        this.checkAndSetCentered();
        
        // 确保容器有过渡动画
        if (this.chatContainer) {
            this.chatContainer.style.transition = 'all 0.5s ease';
        }
        
        // 更新历史对话列表
        this.renderChatHistory();
    }
    
    // 保存当前对话到历史记录（新建）
,
    saveCurrentChat() {
        if (this.currentChatHistory.length === 0) {
            return;
        }
        
        // 检查是否已存在相同ID的历史记录
        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex !== -1) {
            // 如果已存在，更新而不是新建
            this.updateCurrentChatHistory();
            return;
        }
        
        // 获取对话标题（使用第一条用户消息的前30个字符）
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        const title = firstUserMessage ? 
            (firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : '')) : 
            '新对话';
        
        const chatHistory = {
            id: this.sessionId,
            title: title,
            messages: [...this.currentChatHistory],
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString()
        };
        
        // 添加到历史记录列表的开头
        this.chatHistories.unshift(chatHistory);
        
        // 限制历史记录数量（最多保存50条）
        if (this.chatHistories.length > 50) {
            this.chatHistories = this.chatHistories.slice(0, 50);
        }
        
        // 保存到localStorage
        this.saveChatHistories();
    }
    
    // 更新当前对话的历史记录
,
    updateCurrentChatHistory() {
        if (this.currentChatHistory.length === 0) {
            return;
        }
        
        const existingIndex = this.chatHistories.findIndex(h => h.id === this.sessionId);
        if (existingIndex === -1) {
            // 如果不存在，调用保存方法
            this.saveCurrentChat();
            return;
        }
        
        // 更新现有的历史记录
        const history = this.chatHistories[existingIndex];
        history.messages = [...this.currentChatHistory];
        history.updatedAt = new Date().toISOString();
        
        // 如果标题需要更新（第一条消息改变了）
        const firstUserMessage = this.currentChatHistory.find(msg => msg.type === 'user');
        if (firstUserMessage) {
            const newTitle = firstUserMessage.content.substring(0, 30) + (firstUserMessage.content.length > 30 ? '...' : '');
            if (history.title !== newTitle) {
                history.title = newTitle;
            }
        }
        
        // 保存到localStorage
        this.saveChatHistories();
    }
    
    // 加载历史对话列表
,
    loadChatHistories() {
        try {
            const stored = localStorage.getItem('chatHistories');
            return stored ? JSON.parse(stored) : [];
        } catch (e) {
            console.error('加载历史对话失败:', e);
            return [];
        }
    }
    
    // 保存历史对话列表到localStorage
,
    saveChatHistories() {
        try {
            localStorage.setItem('chatHistories', JSON.stringify(this.chatHistories));
        } catch (e) {
            console.error('保存历史对话失败:', e);
        }
    }
,
    loadKnowledgeUploadState() {
        try {
            const stored = localStorage.getItem('autooncallKnowledgeUpload');
            return stored ? JSON.parse(stored) : null;
        } catch (e) {
            console.error('加载知识库状态失败:', e);
            return null;
        }
    }
,
    saveKnowledgeUploadState(payload) {
        this.knowledgeUploadState = payload || null;
        try {
            if (this.knowledgeUploadState) {
                localStorage.setItem('autooncallKnowledgeUpload', JSON.stringify(this.knowledgeUploadState));
            } else {
                localStorage.removeItem('autooncallKnowledgeUpload');
            }
        } catch (e) {
            console.error('保存知识库状态失败:', e);
        }
    }
,
    loadLastAIOpsRunState() {
        try {
            const stored = sessionStorage.getItem(this.aiOpsRunStorageKey);
            const legacyStored = localStorage.getItem(this.aiOpsRunStorageKey);
            if (!stored && legacyStored) {
                sessionStorage.setItem(this.aiOpsRunStorageKey, legacyStored);
            }
            if (legacyStored) {
                localStorage.removeItem(this.aiOpsRunStorageKey);
            }
            const activeStored = stored || legacyStored;
            if (!activeStored) return null;
            const payload = JSON.parse(activeStored);
            if (!payload || typeof payload !== 'object') return null;
            return payload.session_id || payload.diagnosis_run_id ? payload : null;
        } catch (e) {
            console.error('加载最近诊断任务失败:', e);
            this.clearLastAIOpsRunState();
            return null;
        }
    }
,
    saveLastAIOpsRunState(runState, extra = {}) {
        const sessionId = extra.session_id || extra.sessionId || runState?.sessionId || this.sessionId;
        if (!sessionId) return;

        const previous = this.lastAIOpsRunState || {};
        const incident = extra.incident || runState?.incident || previous.incident || null;
        const incidentId = (
            extra.incident_id ||
            runState?.incidentId ||
            incident?.incident_id ||
            previous.incident_id ||
            ''
        );
        const now = this.currentIsoTime();
        const payload = {
            ...previous,
            diagnosis_run_id: sessionId,
            session_id: sessionId,
            incident_id: incidentId,
            trace_id: extra.trace_id || runState?.traceId || previous.trace_id || '',
            status: extra.status || runState?.status || previous.status || 'running',
            status_metadata: extra.status_metadata || runState?.statusMetadata || previous.status_metadata || null,
            node_name: extra.node_name || previous.node_name || '',
            incident,
            started_at: extra.started_at || previous.started_at || now,
            updated_at: extra.updated_at || now,
            last_event_type: extra.last_event_type || runState?.lastEventType || previous.last_event_type || '',
            pending_approval: extra.pending_approval || runState?.pendingApproval || previous.pending_approval || null,
            has_report: extra.has_report ?? Boolean(runState?.structuredReport || previous.has_report),
            plan: Array.isArray(runState?.plan) ? runState.plan : previous.plan || [],
            execution_steps: Array.isArray(runState?.executionSteps)
                ? runState.executionSteps
                : previous.execution_steps || [],
            tool_call_records: Array.isArray(runState?.toolCalls)
                ? runState.toolCalls
                : previous.tool_call_records || [],
            gathered_evidence: Array.isArray(runState?.evidence)
                ? runState.evidence
                : previous.gathered_evidence || [],
            structured_report: runState?.structuredReport || previous.structured_report || null,
            error_message: extra.error_message || previous.error_message || ''
        };

        this.lastAIOpsRunState = payload;
        try {
            sessionStorage.setItem(this.aiOpsRunStorageKey, JSON.stringify(payload));
            localStorage.removeItem(this.aiOpsRunStorageKey);
        } catch (e) {
            console.error('保存最近诊断任务失败:', e);
        }
        this.upsertAIOpsRunHistoryItem(this.buildAIOpsRunHistoryItem(payload));
    }
,
    clearLastAIOpsRunState() {
        this.lastAIOpsRunState = null;
        try {
            sessionStorage.removeItem(this.aiOpsRunStorageKey);
            localStorage.removeItem(this.aiOpsRunStorageKey);
        } catch (e) {
            console.error('清理最近诊断任务失败:', e);
        }
    }
,
    async restoreLastAIOpsRun() {
        const saved = this.lastAIOpsRunState || this.loadLastAIOpsRunState();
        if (!saved || !(saved.session_id || saved.diagnosis_run_id)) return;

        const runState = this.createAIOpsRunState();
        runState.sessionId = saved.session_id || saved.diagnosis_run_id;
        runState.incident = saved.incident || null;
        runState.incidentId = saved.incident_id || saved.incident?.incident_id || '';
        runState.traceId = saved.trace_id || '';
        runState.status = saved.status || '';
        runState.statusMetadata = saved.status_metadata || null;
        runState.pendingApproval = saved.pending_approval || null;
        runState.plan = this.normalizeAIOpsPlanItems(saved.plan || []);
        runState.executionSteps = Array.isArray(saved.execution_steps) ? saved.execution_steps : [];
        runState.toolCalls = Array.isArray(saved.tool_call_records) ? saved.tool_call_records : [];
        runState.evidence = Array.isArray(saved.gathered_evidence) ? saved.gathered_evidence : [];
        runState.structuredReport = saved.structured_report || null;
        this.sessionId = runState.sessionId;
        this.activeAIOpsRun = runState;

        if (runState.incidentId) {
            this.selectedIncidentId = runState.incidentId;
            this.upsertDashboardIncidentFromRun(
                runState,
                runState.status || 'running',
                '已从本地恢复最近一次诊断任务，正在同步后端状态'
            );
        }

        await this.refreshAIOpsRunStatus(runState.sessionId, {
            runState,
            fromRestore: true
        });
    }
,
    async refreshAIOpsRunStatus(sessionId, options = {}) {
        if (!sessionId) return null;
        try {
            const response = await this.apiFetch(
                `${this.apiBaseUrl}/aiops/runs/${encodeURIComponent(sessionId)}`
            );
            if (response.status === 404) {
                if (options.fromRestore) {
                    this.setAIOpsFormStatus('最近诊断待同步', 'warning');
                }
                return null;
            }
            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            const payload = await response.json();
            const runState = this.applyRecoveredAIOpsRunStatus(payload, options.runState, options);
            if (runState?.incidentId) {
                this.selectedIncidentId = runState.incidentId;
                await Promise.allSettled([
                    this.refreshIncidents(),
                    this.refreshApprovals()
                ]);
                if (this.currentWorkbenchView !== 'chat') {
                    await this.refreshSelectedIncidentPanels();
                }
            }
            return payload;
        } catch (error) {
            console.warn('同步最近诊断任务失败:', error);
            if (options.fromRestore) {
                this.setAIOpsFormStatus('最近诊断待同步', 'warning');
            }
            return null;
        }
    }
,
    applyRecoveredAIOpsRunStatus(payload, runState = null, options = {}) {
        if (!payload || typeof payload !== 'object') return null;

        const target = runState || this.createAIOpsRunState();
        const report = payload.report || payload.structured_report || null;
        target.sessionId = payload.session_id || payload.diagnosis_run_id || target.sessionId || this.sessionId;
        target.incident = payload.incident || target.incident || null;
        target.incidentId = payload.incident_id || target.incident?.incident_id || target.incidentId || '';
        target.traceId = payload.trace_id || target.traceId || '';
        target.status = payload.status || target.status || '';
        target.statusMetadata = payload.status_metadata || target.statusMetadata || null;
        target.plan = this.normalizeAIOpsPlanItems(
            Array.isArray(payload.current_plan) && payload.current_plan.length
                ? payload.current_plan
                : payload.plan || target.plan
        );
        target.executionSteps = this.normalizeRecoveredExecutionSteps(payload);
        target.toolCalls = this.resolveRecoveredToolCalls(payload, report, target);
        target.evidence = this.resolveRecoveredEvidence(payload, report, target);
        target.evidenceCount = target.evidence.length;
        target.structuredReport = report || target.structuredReport;
        target.pendingApproval = payload.pending_approval || (
            payload.approval_summary?.status === 'pending'
                ? payload.approval_summary.latest
                : null
        );
        target.riskAssessment = payload.risk_assessment || report?.risk_summary || target.riskAssessment;
        target.traceSummary = payload.trace_summary || target.traceSummary;
        target.eventCount = payload.trace_summary?.event_count || target.executionSteps.length || target.eventCount;
        target.lastEventType = payload.trace_summary?.latest_event_type || target.lastEventType;
        target.errors = Array.isArray(payload.errors) ? payload.errors : target.errors;
        this.sessionId = target.sessionId;
        this.activeAIOpsRun = target;

        if (target.incidentId) {
            this.selectedIncidentId = target.incidentId;
            this.upsertDashboardIncidentFromRun(
                target,
                target.status || 'running',
                this.formatRecoveredAIOpsStatusReason(payload)
            );
        }

        this.renderPlanCards(target.plan);
        this.renderExecutionSteps(target.executionSteps);
        this.renderToolCallTable(target.toolCalls);
        this.renderEvidenceCards(target.evidence);
        this.renderDependencySignals(report?.dependency_signals || [], target.toolCalls);
        this.setResponseAttention(Boolean(target.pendingApproval));
        this.saveLastAIOpsRunState(target, {
            status: target.status,
            status_metadata: target.statusMetadata,
            node_name: payload.node_name || '',
            started_at: payload.started_at,
            updated_at: payload.updated_at,
            has_report: Boolean(payload.has_report || report),
            pending_approval: target.pendingApproval
        });

        if (options.fromRestore) {
            this.setAIOpsFormStatus(this.formatRecoveredAIOpsStatusLabel(target), this.statusTone(target));
        }
        return target;
    }
,
    normalizeRecoveredExecutionSteps(payload) {
        const steps = [];
        const pastSteps = Array.isArray(payload?.past_steps) ? payload.past_steps : [];
        pastSteps.forEach((item, index) => {
            const step = item?.step && typeof item.step === 'object' ? item.step : {};
            const result = item?.result ?? item?.value ?? '';
            steps.push({
                event_id: `recovered-step-${index + 1}`,
                event_type: 'step_complete',
                node_name: 'executor',
                step_id: step.step_id || '',
                tool_name: step.tool_name || '',
                status: result?.status || 'success',
                summary: this.summarizeRecoveredStepValue(result) || step.purpose || `已恢复步骤 ${index + 1}`,
                data_source: result?.data_source || 'session_snapshot',
                latency_ms: result?.latency_ms ?? 0,
                created_at: payload.updated_at || this.currentIsoTime()
            });
        });

        const latestTrace = payload?.trace_summary?.latest || null;
        if (latestTrace && !steps.some((step) => step.event_id === latestTrace.event_id)) {
            const latestStep = this.normalizeTraceEventAsExecutionStep(latestTrace);
            if (latestStep) steps.push(latestStep);
        }

        if (steps.length) return steps;
        return Array.isArray(payload?.execution_steps) ? payload.execution_steps : [];
    }
,
    summarizeRecoveredStepValue(value) {
        if (!value) return '';
        if (typeof value === 'string') return value.slice(0, 500);
        if (typeof value === 'object') {
            return (
                value.output_summary ||
                value.summary ||
                value.message ||
                value.error_message ||
                this.safeJsonPreview(value, 500)
            );
        }
        return String(value).slice(0, 500);
    }
,
    safeJsonPreview(value, limit = 500) {
        try {
            return JSON.stringify(value).slice(0, limit);
        } catch {
            return String(value).slice(0, limit);
        }
    }
,
    resolveRecoveredToolCalls(payload, report, target) {
        if (Array.isArray(payload?.tool_call_records) && payload.tool_call_records.length) {
            return payload.tool_call_records;
        }
        if (Array.isArray(report?.tool_calls) && report.tool_calls.length) {
            return report.tool_calls;
        }
        return Array.isArray(target.toolCalls) ? target.toolCalls : [];
    }
,
    resolveRecoveredEvidence(payload, report, target) {
        if (Array.isArray(payload?.gathered_evidence) && payload.gathered_evidence.length) {
            return payload.gathered_evidence;
        }
        if (Array.isArray(report?.evidence) && report.evidence.length) {
            return report.evidence;
        }
        return Array.isArray(target.evidence) ? target.evidence : [];
    }
,
    formatRecoveredAIOpsStatusReason(payload) {
        const nodeName = payload?.node_name ? `节点 ${payload.node_name}` : '诊断流程';
        const updatedAt = payload?.updated_at ? `，同步时间 ${payload.updated_at}` : '';
        return `${nodeName} 状态：${this.formatRecoveredAIOpsStatusLabel(payload)}${updatedAt}`;
    }
,
    formatRecoveredAIOpsStatusLabel(statusOrItem) {
        const metadata = this.resolveStatusMetadata(statusOrItem);
        if (metadata?.label) {
            return metadata.label;
        }
        const status = this.statusValue(statusOrItem);
        const catalogMetadata = this.statusMetadataFromCatalog(status);
        if (catalogMetadata?.label) {
            return catalogMetadata.label;
        }
        const labels = {
            running: '诊断运行中',
            completed: '诊断已完成',
            waiting_approval: '等待人工审批',
            approval_approved: '审批已通过',
            approval_rejected: '审批已拒绝',
            approval_resumed: '诊断闭环已恢复',
            blocked: '已阻断',
            escalated: '已升级',
            failed: '诊断失败'
        };
        return labels[status] || status || '已恢复最近诊断';
    }
,
    renderKnowledgeUploadState(payload) {
        const data = payload || {};
        const indexing = data.indexing || {};
        const status = indexing.status || data.status || 'idle';
        const filename = data.filename || '未上传';
        const chunkCount = indexing.chunk_count ?? data.chunk_count ?? 0;
        const statusText = this.formatKnowledgeIndexStatus(status);

        if (this.knowledgeStatusBadge) {
            this.knowledgeStatusBadge.textContent = statusText;
            this.knowledgeStatusBadge.className = `meta-pill ${this.statusTone(status)}`;
        }
        if (this.knowledgeFileName) {
            this.knowledgeFileName.textContent = filename;
        }
        if (this.knowledgeIndexStatus) {
            this.knowledgeIndexStatus.textContent = statusText;
        }
        if (this.knowledgeChunkCount) {
            this.knowledgeChunkCount.textContent = String(chunkCount);
        }
        if (this.knowledgeUploadSummary) {
            this.knowledgeUploadSummary.textContent = this.buildKnowledgeUploadSummary(data);
            this.knowledgeUploadSummary.className = `knowledge-upload-summary ${this.statusTone(status)}`;
        }
    }
,
    renderKnowledgeUploadProgress(file) {
        const payload = {
            filename: file?.name || 'unknown',
            size: file?.size || 0,
            status: 'running',
            indexing: {
                status: 'running',
                chunk_count: 0,
                duration_ms: 0
            }
        };
        this.renderKnowledgeUploadState(payload);
    }
,
    updateKnowledgeUploadResult(payload) {
        const savedPayload = {
            ...(payload || {}),
            updated_at: new Date().toISOString()
        };
        this.saveKnowledgeUploadState(savedPayload);
        this.renderKnowledgeUploadState(savedPayload);
    }
,
    renderKnowledgeUploadError(file, error) {
        const payload = {
            filename: file?.name || 'unknown',
            size: file?.size || 0,
            status: 'failed',
            error_message: error?.message || '上传失败',
            updated_at: new Date().toISOString(),
            indexing: {
                status: 'failed',
                chunk_count: 0,
                duration_ms: 0,
                error_message: error?.message || '上传失败'
            }
        };
        this.saveKnowledgeUploadState(payload);
        this.renderKnowledgeUploadState(payload);
    }
,
    formatKnowledgeIndexStatus(status) {
        const names = {
            idle: '未建立',
            running: '索引中',
            success: '可检索',
            empty: '无有效内容',
            failed: '索引失败'
        };
        return names[status] || status || '未知';
    }
,
    buildKnowledgeUploadSummary(payload) {
        if (!payload || !payload.filename) {
            return '暂无知识库上传记录';
        }
        const indexing = payload.indexing || {};
        const status = indexing.status || payload.status || 'unknown';
        if (status === 'running') {
            return `${payload.filename} 正在写入知识库。`;
        }
        if (status === 'failed') {
            return `${payload.filename} 索引失败：${indexing.error_message || payload.error_message || '未知错误'}`;
        }
        if (status === 'empty') {
            return `${payload.filename} 未生成可检索内容。${indexing.message || ''}`.trim();
        }
        const chunkCount = indexing.chunk_count ?? 0;
        const duration = indexing.duration_ms ?? 0;
        return `${payload.filename} 已可检索，分块 ${chunkCount} 个，耗时 ${duration} ms。`;
    }
    
    // 渲染历史对话列表
,
    renderChatHistory() {
        if (!this.chatHistoryList) {
            return;
        }
        
        this.chatHistoryList.innerHTML = '';
        
        if (this.chatHistories.length === 0) {
            return;
        }
        
        this.chatHistories.forEach((history, index) => {
            const historyItem = document.createElement('div');
            historyItem.className = 'history-item';
            historyItem.dataset.historyId = history.id;
            
            historyItem.innerHTML = `
                <div class="history-item-content">
                    <span class="history-item-title">${this.escapeHtml(history.title)}</span>
                </div>
                <button class="history-item-delete" data-history-id="${history.id}" title="删除">
                    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M18 6L6 18M6 6L18 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                    </svg>
                </button>
            `;
            
            // 点击历史项加载对话
            historyItem.addEventListener('click', (e) => {
                if (!e.target.closest('.history-item-delete')) {
                    this.loadChatHistory(history.id);
                }
            });
            
            // 删除历史对话
            const deleteBtn = historyItem.querySelector('.history-item-delete');
            deleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.deleteChatHistory(history.id);
            });
            
            this.chatHistoryList.appendChild(historyItem);
        });
    }
    
    // 加载历史对话
,
    async loadChatHistory(historyId) {
        const history = this.chatHistories.find(h => h.id === historyId);
        if (!history) {
            return;
        }
        
        // 如果当前有对话内容，且不是同一个对话，先保存
        if (this.currentChatHistory.length > 0 && this.sessionId !== historyId) {
            if (this.isCurrentChatFromHistory) {
                // 如果当前对话也是从历史记录加载的，更新它
                this.updateCurrentChatHistory();
            } else {
                // 如果当前对话是新对话，保存为新历史
                this.saveCurrentChat();
            }
        }
        
        try {
            // 从后端获取会话历史
            const response = await this.apiFetch(`/api/chat/session/${historyId}`);
            if (response.ok) {
                const data = await response.json();
                const backendHistory = data.history || [];
                
                // 更新会话ID
                this.sessionId = history.id;
                this.isCurrentChatFromHistory = true;
                
                // 清空并重新渲染消息
                if (this.chatMessages) {
                    this.chatMessages.innerHTML = '';
                    
                    // 如果后端有历史记录，使用后端的
                    if (backendHistory.length > 0) {
                        this.currentChatHistory = [];
                        backendHistory.forEach(msg => {
                            // 后端返回格式: {role: "user|assistant", content: "...", timestamp: "..."}
                            const messageType = msg.role === 'user' ? 'user' : 'assistant';
                            this.addMessage(messageType, msg.content, false, false, msg.metadata || null);
                        });
                    } else {
                        // 否则使用localStorage的历史记录
                        this.currentChatHistory = [...history.messages];
                        history.messages.forEach(msg => {
                            this.addMessage(msg.type, msg.content, false, false, msg.metadata || null);
                        });
                    }
                }
            } else {
                // 如果后端请求失败，使用localStorage的历史记录
                console.warn('从后端加载历史失败，使用本地缓存');
                this.sessionId = history.id;
                this.currentChatHistory = [...history.messages];
                this.isCurrentChatFromHistory = true;
                
                if (this.chatMessages) {
                    this.chatMessages.innerHTML = '';
                    history.messages.forEach(msg => {
                        this.addMessage(msg.type, msg.content, false, false, msg.metadata || null);
                    });
                }
            }
        } catch (error) {
            console.error('加载会话历史失败:', error);
            // 出错时使用localStorage的历史记录
            this.sessionId = history.id;
            this.currentChatHistory = [...history.messages];
            this.isCurrentChatFromHistory = true;
            
            if (this.chatMessages) {
                this.chatMessages.innerHTML = '';
                history.messages.forEach(msg => {
                    this.addMessage(msg.type, msg.content, false, false, msg.metadata || null);
                });
            }
        }
        
        // 更新UI
        this.checkAndSetCentered();
        this.renderChatHistory();
    }
    
    // 删除历史对话
,
    async deleteChatHistory(historyId) {
        try {
            // 调用后端API清空会话
            const response = await this.apiFetch('/api/chat/clear', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    session_id: historyId
                })
            });

            if (!response.ok) {
                throw new Error('清空会话失败');
            }

            const result = await response.json();
            
            if (result.status === 'success') {
                // 从本地存储中删除
                this.chatHistories = this.chatHistories.filter(h => h.id !== historyId);
                this.saveChatHistories();
                this.renderChatHistory();
                
                // 如果删除的是当前对话，清空当前对话
                if (this.sessionId === historyId) {
                    this.currentChatHistory = [];
                    if (this.chatMessages) {
                        this.chatMessages.innerHTML = '';
                    }
                    this.sessionId = this.generateSessionId();
                    this.checkAndSetCentered();
                }
                
                this.showNotification('会话已清空', 'success');
            } else {
                throw new Error(result.message || '清空会话失败');
            }
        } catch (error) {
            console.error('删除历史对话失败:', error);
            this.showNotification('删除失败: ' + error.message, 'error');
        }
    }

    // 切换模式下拉菜单
});
