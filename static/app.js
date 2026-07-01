// AutoOnCall 前端应用
class AutoOnCallApp {
    constructor() {
        this.apiBaseUrl = '/api';
        this.currentMode = 'quick'; // 'quick' 或 'stream'
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.currentChatHistory = []; // 当前对话的消息历史
        this.chatHistories = this.loadChatHistories(); // 所有历史对话
        this.knowledgeUploadState = this.loadKnowledgeUploadState();
        this.isCurrentChatFromHistory = false; // 标记当前对话是否是从历史记录加载的
        this.currentWorkbenchView = 'incidents';
        this.currentIncidentTab = 'overview';
        this.apiTokenStorageKey = 'autooncallApiToken';
        this.selectedIncidentId = '';
        this.aiOpsDemoIncidents = {};
        this.aiOpsDemoIncidentAliases = {};
        this.aiOpsDemoIncidentsLoaded = false;
        this.aiOpsStatusCatalog = [];
        this.aiOpsRunStorageKey = 'autooncallAIOpsRun';
        this.lastAIOpsRunState = this.loadLastAIOpsRunState();
        this.activeAIOpsRun = null;
        this.dashboardState = {
            incidents: [],
            alerts: [],
            aiopsRuns: [],
            aiopsRunFilters: {
                status: '',
                serviceName: ''
            },
            approvals: [],
            changeExecutions: [],
            health: null,
            evalSummary: null,
            adapterVerification: null,
            toolContracts: [],
            toolContractsError: ''
        };
        
        this.initializeElements();
        this.bindEvents();
        this.updateUI();
        this.initMarkdown();
        this.checkAndSetCentered();
        this.renderChatHistory();
        this.renderKnowledgeUploadState(this.knowledgeUploadState);
        this.renderAuthTokenState();
        this.setWorkbenchView(this.currentWorkbenchView);
        this.loadAIOpsStatusCatalog();
        this.loadAIOpsDemoIncidents();
        this.restoreLastAIOpsRun();
    }

    // 初始化Markdown配置
    initMarkdown() {
        // 等待 marked 库加载完成
        const checkMarked = () => {
            if (typeof marked !== 'undefined') {
                try {
                    // 配置marked选项
                    marked.setOptions({
                        breaks: true,  // 支持GFM换行
                        gfm: true,     // 启用GitHub风格的Markdown
                        headerIds: false,
                        mangle: false
                    });

                    // 配置代码高亮
                    if (typeof hljs !== 'undefined') {
                        marked.setOptions({
                            highlight: function(code, lang) {
                                if (lang && hljs.getLanguage(lang)) {
                                    try {
                                        return hljs.highlight(code, { language: lang }).value;
                                    } catch (err) {
                                        console.error('代码高亮失败:', err);
                                    }
                                }
                                return code;
                            }
                        });
                    }                } catch (e) {
                    console.error('Markdown 配置失败:', e);
                }
            } else {
                // 如果 marked 还没加载，等待一段时间后重试
                setTimeout(checkMarked, 100);
            }
        };
        checkMarked();
    }

    // 安全地渲染 Markdown
    renderMarkdown(content) {
        if (!content) return '';
        
        // 检查 marked 是否可用
        if (typeof marked === 'undefined') {
            console.warn('marked 库未加载，使用纯文本显示');
            return this.escapeHtml(content);
        }
        
        try {
            const html = marked.parse(content);
            return this.sanitizeRenderedHtml(html);
        } catch (e) {
            console.error('Markdown 渲染失败:', e);
            return this.escapeHtml(content);
        }
    }

    sanitizeRenderedHtml(html) {
        const container = document.createElement('div');
        container.innerHTML = html || '';

        container.querySelectorAll('script, style, iframe, object, embed, link, meta').forEach((node) => {
            node.remove();
        });

        container.querySelectorAll('*').forEach((node) => {
            Array.from(node.attributes).forEach((attr) => {
                const name = attr.name.toLowerCase();
                const value = (attr.value || '').trim().toLowerCase();
                if (
                    name.startsWith('on') ||
                    name === 'style' ||
                    ((name === 'href' || name === 'src' || name === 'xlink:href') &&
                        (value.startsWith('javascript:') || value.startsWith('data:text/html')))
                ) {
                    node.removeAttribute(attr.name);
                }
            });
        });

        return container.innerHTML;
    }

    // 高亮代码块
    highlightCodeBlocks(container) {
        if (typeof hljs !== 'undefined' && container) {
            try {
                container.querySelectorAll('pre code').forEach((block) => {
                    if (!block.classList.contains('hljs')) {
                        hljs.highlightElement(block);
                    }
                });
            } catch (e) {
                console.error('代码高亮失败:', e);
            }
        }
    }

    // 初始化DOM元素
    initializeElements() {
        // 侧边栏元素
        this.sidebar = document.querySelector('.sidebar');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.aiOpsSidebarBtn = document.getElementById('aiOpsSidebarBtn');
        this.aiOpsPresetSelect = document.getElementById('aiOpsPresetSelect');
        this.diagnosisForm = document.getElementById('diagnosisForm');
        this.aiOpsFormStatus = document.getElementById('aiOpsFormStatus');
        this.aiOpsTitle = document.getElementById('aiOpsTitle');
        this.aiOpsServiceName = document.getElementById('aiOpsServiceName');
        this.aiOpsSeverity = document.getElementById('aiOpsSeverity');
        this.aiOpsEnvironment = document.getElementById('aiOpsEnvironment');
        this.aiOpsIncidentId = document.getElementById('aiOpsIncidentId');
        this.aiOpsSymptom = document.getElementById('aiOpsSymptom');
        this.aiOpsRawAlert = document.getElementById('aiOpsRawAlert');
        this.aiOpsClearFormBtn = document.getElementById('aiOpsClearFormBtn');
        
        // 输入区域元素
        this.messageInput = document.getElementById('messageInput');
        this.sendButton = document.getElementById('sendButton');
        this.toolsBtn = document.getElementById('toolsBtn');
        this.toolsMenu = document.getElementById('toolsMenu');
        this.uploadFileItem = document.getElementById('uploadFileItem');
        this.knowledgeUploadBtn = document.getElementById('knowledgeUploadBtn');
        this.knowledgeStatusBadge = document.getElementById('knowledgeStatusBadge');
        this.knowledgeFileName = document.getElementById('knowledgeFileName');
        this.knowledgeIndexStatus = document.getElementById('knowledgeIndexStatus');
        this.knowledgeChunkCount = document.getElementById('knowledgeChunkCount');
        this.knowledgeUploadSummary = document.getElementById('knowledgeUploadSummary');
        this.modeSelectorBtn = document.getElementById('modeSelectorBtn');
        this.modeDropdown = document.getElementById('modeDropdown');
        this.currentModeText = document.getElementById('currentModeText');
        this.fileInput = document.getElementById('fileInput');
        
        // 聊天区域元素
        this.chatMessages = document.getElementById('chatMessages');
        this.loadingOverlay = document.getElementById('loadingOverlay');
        this.chatContainer = document.querySelector('.chat-container');
        this.welcomeGreeting = document.getElementById('welcomeGreeting');
        this.chatHistoryList = document.getElementById('chatHistoryList');

        // 工作台元素
        this.mainContent = document.querySelector('.main-content');
        this.workbenchPanel = document.getElementById('workbenchPanel');
        this.workbenchTitle = document.getElementById('workbenchTitle');
        this.workbenchNavButtons = document.querySelectorAll('[data-workbench-view]');
        this.incidentTabNav = document.getElementById('incidentTabNav');
        this.incidentTabButtons = document.querySelectorAll('[data-incident-tab]');
        this.refreshWorkbenchBtn = document.getElementById('refreshWorkbenchBtn');
        this.healthStatusPill = document.getElementById('healthStatusPill');
        this.healthMode = document.getElementById('healthMode');
        this.healthSummary = document.getElementById('healthSummary');
        this.incidentList = document.getElementById('incidentList');
        this.incidentCount = document.getElementById('incidentCount');
        this.alertList = document.getElementById('alertList');
        this.alertCount = document.getElementById('alertCount');
        this.aiOpsRunHistoryList = document.getElementById('aiOpsRunHistoryList');
        this.aiOpsRunHistoryCount = document.getElementById('aiOpsRunHistoryCount');
        this.aiOpsRunStatusFilter = document.getElementById('aiOpsRunStatusFilter');
        this.aiOpsRunServiceFilter = document.getElementById('aiOpsRunServiceFilter');
        this.aiOpsRunFilterBtn = document.getElementById('aiOpsRunFilterBtn');
        this.aiOpsRunClearFilterBtn = document.getElementById('aiOpsRunClearFilterBtn');
        this.aiOpsRunCompare = document.getElementById('aiOpsRunCompare');
        this.selectedIncidentBadge = document.getElementById('selectedIncidentBadge');
        this.incidentDetail = document.getElementById('incidentDetail');
        this.planCount = document.getElementById('planCount');
        this.planList = document.getElementById('planList');
        this.stepCount = document.getElementById('stepCount');
        this.stepList = document.getElementById('stepList');
        this.toolCallCount = document.getElementById('toolCallCount');
        this.toolCallTable = document.getElementById('toolCallTable');
        this.dependencySignalCount = document.getElementById('dependencySignalCount');
        this.dependencySignalList = document.getElementById('dependencySignalList');
        this.evidenceCount = document.getElementById('evidenceCount');
        this.evidenceList = document.getElementById('evidenceList');
        this.confidenceBadge = document.getElementById('confidenceBadge');
        this.conclusionView = document.getElementById('conclusionView');
        this.traceTimeline = document.getElementById('traceTimeline');
        this.traceCount = document.getElementById('traceCount');
        this.reportViewer = document.getElementById('reportViewer');
        this.reportStatus = document.getElementById('reportStatus');
        this.approvalList = document.getElementById('approvalList');
        this.approvalCount = document.getElementById('approvalCount');
        this.changeExecutionList = document.getElementById('changeExecutionList');
        this.changeExecutionCount = document.getElementById('changeExecutionCount');
        this.evalSummary = document.getElementById('evalSummary');
        this.evalStatus = document.getElementById('evalStatus');
        this.adapterVerification = document.getElementById('adapterVerification');
        this.adapterVerifyStatus = document.getElementById('adapterVerifyStatus');
        this.toolContractSummary = document.getElementById('toolContractSummary');
        this.toolContractCount = document.getElementById('toolContractCount');
        this.apiTokenInput = document.getElementById('apiTokenInput');
        this.apiTokenSaveBtn = document.getElementById('apiTokenSaveBtn');
        this.apiTokenClearBtn = document.getElementById('apiTokenClearBtn');
        this.authStatusBadge = document.getElementById('authStatusBadge');
        
        // 初始化时检查是否需要居中
        this.checkAndSetCentered();
    }

    // 绑定事件监听器
    bindEvents() {
        // 新建对话
        if (this.newChatBtn) {
            this.newChatBtn.addEventListener('click', () => this.newChat());
        }
        
        // AI Ops按钮
        if (this.aiOpsSidebarBtn) {
            this.aiOpsSidebarBtn.addEventListener('click', () => this.triggerAIOps());
        }

        if (this.apiTokenSaveBtn) {
            this.apiTokenSaveBtn.addEventListener('click', () => this.saveApiToken());
        }

        if (this.apiTokenClearBtn) {
            this.apiTokenClearBtn.addEventListener('click', () => this.clearApiToken());
        }

        if (this.apiTokenInput) {
            this.apiTokenInput.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    this.saveApiToken();
                }
            });
        }

        if (this.diagnosisForm) {
            this.diagnosisForm.addEventListener('submit', (event) => {
                event.preventDefault();
                this.triggerAIOps();
            });
        }

        if (this.aiOpsPresetSelect) {
            this.aiOpsPresetSelect.addEventListener('change', () => {
                this.applyAIOpsTemplate(this.aiOpsPresetSelect.value);
            });
        }

        if (this.aiOpsClearFormBtn) {
            this.aiOpsClearFormBtn.addEventListener('click', () => this.clearAIOpsForm());
        }
        
        // 模式选择下拉菜单
        if (this.modeSelectorBtn) {
            this.modeSelectorBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleModeDropdown();
            });
        }
        
        // 下拉菜单项点击
        const dropdownItems = document.querySelectorAll('.dropdown-item');
        dropdownItems.forEach(item => {
            item.addEventListener('click', (e) => {
                const mode = item.getAttribute('data-mode');
                this.selectMode(mode);
                this.closeModeDropdown();
            });
        });
        
        // 点击外部关闭下拉菜单
        document.addEventListener('click', (e) => {
            if (!this.modeSelectorBtn.contains(e.target) && 
                !this.modeDropdown.contains(e.target)) {
                this.closeModeDropdown();
            }
        });
        
        // 发送消息
        if (this.sendButton) {
            this.sendButton.addEventListener('click', () => this.sendMessage());
        }
        
        if (this.messageInput) {
            this.messageInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });
        }
        
        // 工具按钮和菜单
        if (this.toolsBtn) {
            this.toolsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleToolsMenu();
            });
        }
        
        // 工具菜单项点击事件
        if (this.uploadFileItem) {
            this.uploadFileItem.addEventListener('click', () => {
                if (this.fileInput) {
                    this.fileInput.click();
                }
                this.closeToolsMenu();
            });
        }

        if (this.knowledgeUploadBtn) {
            this.knowledgeUploadBtn.addEventListener('click', () => {
                if (this.fileInput) {
                    this.fileInput.click();
                }
            });
        }
        
        // 点击外部关闭工具菜单
        document.addEventListener('click', (e) => {
            if (this.toolsBtn && this.toolsMenu && 
                !this.toolsBtn.contains(e.target) && 
                !this.toolsMenu.contains(e.target)) {
                this.closeToolsMenu();
            }
        });
        
        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
        }

        if (this.workbenchNavButtons) {
            this.workbenchNavButtons.forEach((button) => {
                button.addEventListener('click', () => {
                    const view = button.getAttribute('data-workbench-view') || 'chat';
                    this.setWorkbenchView(view);
                });
            });
        }

        if (this.incidentTabButtons) {
            this.incidentTabButtons.forEach((button) => {
                button.addEventListener('click', () => {
                    this.setIncidentTab(button.getAttribute('data-incident-tab') || 'overview');
                });
            });
        }

        if (this.refreshWorkbenchBtn) {
            this.refreshWorkbenchBtn.addEventListener('click', () => {
                this.refreshWorkbenchData(this.currentWorkbenchView);
            });
        }

        if (this.incidentList) {
            this.incidentList.addEventListener('click', (event) => {
                const item = event.target.closest('[data-incident-id]');
                if (item) {
                    this.selectIncident(item.getAttribute('data-incident-id'));
                }
            });
        }

        if (this.alertList) {
            this.alertList.addEventListener('click', (event) => {
                const item = event.target.closest('[data-alert-fingerprint]');
                if (item) {
                    this.applyAlertToDiagnosisForm(item.getAttribute('data-alert-fingerprint'));
                }
            });
        }

        if (this.aiOpsRunHistoryList) {
            this.aiOpsRunHistoryList.addEventListener('click', (event) => {
                const item = event.target.closest('[data-aiops-run-id]');
                if (item) {
                    this.openAIOpsRunHistory(item.getAttribute('data-aiops-run-id'));
                }
            });
        }

        if (this.aiOpsRunFilterBtn) {
            this.aiOpsRunFilterBtn.addEventListener('click', () => this.applyAIOpsRunFilters());
        }

        if (this.aiOpsRunClearFilterBtn) {
            this.aiOpsRunClearFilterBtn.addEventListener('click', () => this.clearAIOpsRunFilters());
        }

        if (this.aiOpsRunStatusFilter) {
            this.aiOpsRunStatusFilter.addEventListener('change', () => this.applyAIOpsRunFilters());
        }

        if (this.aiOpsRunServiceFilter) {
            this.aiOpsRunServiceFilter.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    this.applyAIOpsRunFilters();
                }
            });
        }

        if (this.approvalList) {
            this.approvalList.addEventListener('click', (event) => {
                const diagnosisResumeButton = event.target.closest('[data-diagnosis-resume]');
                if (diagnosisResumeButton) {
                    this.resumeDiagnosisWorkflow(
                        diagnosisResumeButton.getAttribute('data-incident-id'),
                        diagnosisResumeButton.getAttribute('data-approval-id'),
                        { openReport: true }
                    );
                    return;
                }
                const resumeButton = event.target.closest('[data-change-resume]');
                if (resumeButton) {
                    this.startSafeChangeWorkflow(
                        resumeButton.getAttribute('data-incident-id'),
                        resumeButton.getAttribute('data-change-plan-id'),
                        resumeButton.getAttribute('data-approval-id'),
                        resumeButton.getAttribute('data-change-mode')
                    );
                    return;
                }
                const button = event.target.closest('[data-approval-decision]');
                if (button) {
                    const approvalItem = button.closest('.approval-item');
                    const reasonInput = approvalItem ? approvalItem.querySelector('[data-approval-reason]') : null;
                    this.submitApprovalDecision(
                        button.getAttribute('data-incident-id'),
                        button.getAttribute('data-approval-id'),
                        button.getAttribute('data-approval-decision'),
                        reasonInput ? reasonInput.value : ''
                    );
                }
            });
        }

        if (this.changeExecutionList) {
            this.changeExecutionList.addEventListener('click', (event) => {
                const resultButton = event.target.closest('[data-manual-result]');
                if (!resultButton) return;
                const executionItem = resultButton.closest('.change-execution-card');
                const notesInput = executionItem ? executionItem.querySelector('[data-manual-notes]') : null;
                this.submitManualChangeResult(
                    resultButton.getAttribute('data-change-execution-id'),
                    resultButton.getAttribute('data-manual-result'),
                    notesInput ? notesInput.value : ''
                );
            });
        }
    }

    // 切换工具菜单显示/隐藏
    toggleToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭工具菜单
    closeToolsMenu() {
        if (this.toolsMenu && this.toolsBtn) {
            const wrapper = this.toolsBtn.closest('.tools-btn-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 新建对话
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
    saveChatHistories() {
        try {
            localStorage.setItem('chatHistories', JSON.stringify(this.chatHistories));
        } catch (e) {
            console.error('保存历史对话失败:', e);
        }
    }

    loadKnowledgeUploadState() {
        try {
            const stored = localStorage.getItem('autooncallKnowledgeUpload');
            return stored ? JSON.parse(stored) : null;
        } catch (e) {
            console.error('加载知识库状态失败:', e);
            return null;
        }
    }

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

    loadLastAIOpsRunState() {
        try {
            const stored = localStorage.getItem('autooncallAIOpsRun');
            if (!stored) return null;
            const payload = JSON.parse(stored);
            if (!payload || typeof payload !== 'object') return null;
            return payload.session_id || payload.diagnosis_run_id ? payload : null;
        } catch (e) {
            console.error('加载最近诊断任务失败:', e);
            return null;
        }
    }

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
            localStorage.setItem(this.aiOpsRunStorageKey, JSON.stringify(payload));
        } catch (e) {
            console.error('保存最近诊断任务失败:', e);
        }
        this.upsertAIOpsRunHistoryItem(this.buildAIOpsRunHistoryItem(payload));
    }

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

    safeJsonPreview(value, limit = 500) {
        try {
            return JSON.stringify(value).slice(0, limit);
        } catch {
            return String(value).slice(0, limit);
        }
    }

    resolveRecoveredToolCalls(payload, report, target) {
        if (Array.isArray(payload?.tool_call_records) && payload.tool_call_records.length) {
            return payload.tool_call_records;
        }
        if (Array.isArray(report?.tool_calls) && report.tool_calls.length) {
            return report.tool_calls;
        }
        return Array.isArray(target.toolCalls) ? target.toolCalls : [];
    }

    resolveRecoveredEvidence(payload, report, target) {
        if (Array.isArray(payload?.gathered_evidence) && payload.gathered_evidence.length) {
            return payload.gathered_evidence;
        }
        if (Array.isArray(report?.evidence) && report.evidence.length) {
            return report.evidence;
        }
        return Array.isArray(target.evidence) ? target.evidence : [];
    }

    formatRecoveredAIOpsStatusReason(payload) {
        const nodeName = payload?.node_name ? `节点 ${payload.node_name}` : '诊断流程';
        const updatedAt = payload?.updated_at ? `，同步时间 ${payload.updated_at}` : '';
        return `${nodeName} 状态：${this.formatRecoveredAIOpsStatusLabel(payload)}${updatedAt}`;
    }

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

    updateKnowledgeUploadResult(payload) {
        const savedPayload = {
            ...(payload || {}),
            updated_at: new Date().toISOString()
        };
        this.saveKnowledgeUploadState(savedPayload);
        this.renderKnowledgeUploadState(savedPayload);
    }

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
    toggleModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.toggle('active');
            }
        }
    }

    // 关闭模式下拉菜单
    closeModeDropdown() {
        if (this.modeSelectorBtn && this.modeDropdown) {
            const wrapper = this.modeSelectorBtn.closest('.mode-selector-wrapper');
            if (wrapper) {
                wrapper.classList.remove('active');
            }
        }
    }

    // 选择模式
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

    updateWorkbenchNavState() {
        if (!this.workbenchNavButtons) return;
        this.workbenchNavButtons.forEach((button) => {
            const view = button.getAttribute('data-workbench-view') || 'chat';
            button.classList.toggle('active', view === this.currentWorkbenchView);
        });
    }

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

    normalizeWorkbenchView(view) {
        return this.resolveWorkbenchTarget(view).view;
    }

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

    updateWorkbenchTitle() {
        if (!this.workbenchTitle) return;
        const titles = {
            incidents: '故障诊断中心',
            response: '处置中心',
            system: '环境就绪中心'
        };
        this.workbenchTitle.textContent = titles[this.currentWorkbenchView] || 'AutoOnCall 工作台';
    }

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

    updateWorkbenchPanelVisibility() {
        if (!this.workbenchPanel) return;
        const view = this.currentWorkbenchView;
        const incidentTabPanels = {
            overview: ['incidents', 'alerts', 'diagnosis-launch', 'run-history', 'detail', 'conclusion'],
            process: ['incidents', 'plan', 'steps', 'tools'],
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

    clearApiToken() {
        localStorage.removeItem(this.apiTokenStorageKey);
        if (this.apiTokenInput) {
            this.apiTokenInput.value = '';
        }
        this.renderAuthTokenState();
        this.showNotification('接口令牌已清除', 'info');
    }

    async apiGet(path) {
        const response = await this.apiFetch(path);
        if (!response.ok) {
            throw new Error(`HTTP错误: ${response.status}`);
        }
        return response.json();
    }

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

    authHeaders(headers = {}) {
        const normalizedHeaders = { ...headers };
        const token = (localStorage.getItem('autooncallApiToken') || '').trim();
        if (token) {
            normalizedHeaders['X-AutoOnCall-Token'] = token;
        }
        return normalizedHeaders;
    }

    async apiFetch(path, options = {}) {
        return fetch(path, {
            ...options,
            headers: this.authHeaders(options.headers || {})
        });
    }

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

    renderHealthError(error) {
        if (this.healthStatusPill) {
            this.healthStatusPill.textContent = '状态不可用';
            this.healthStatusPill.className = 'status-pill error';
        }
        if (this.healthSummary) {
            this.healthSummary.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
        }
    }

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

    async applyAIOpsRunFilters() {
        this.dashboardState.aiopsRunFilters = {
            status: this.aiOpsRunStatusFilter ? this.aiOpsRunStatusFilter.value : '',
            serviceName: this.aiOpsRunServiceFilter ? this.aiOpsRunServiceFilter.value.trim() : ''
        };
        await this.refreshAIOpsRuns();
    }

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

    formatAIOpsRunCounters(run) {
        const completed = run.completed_step_count ?? 0;
        const tools = run.tool_call_count ?? 0;
        const evidence = run.evidence_count ?? 0;
        const warnings = run.warning_count ?? 0;
        return `步骤 ${completed} · 工具 ${tools} · 证据 ${evidence} · 告警 ${warnings}`;
    }

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

    async openAIOpsRunHistory(sessionId) {
        if (!sessionId) return;
        this.currentIncidentTab = 'process';
        await this.setWorkbenchView('incidents');
        await this.refreshAIOpsRunStatus(sessionId, { fromHistory: true });
        this.setIncidentTab('process');
    }

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

    async selectIncident(incidentId) {
        if (!incidentId) return;
        this.selectedIncidentId = incidentId;
        this.renderIncidentList();
        this.renderAIOpsRunCompare();
        await this.refreshSelectedIncidentPanels();
    }

    async refreshSelectedIncidentPanels() {
        if (!this.selectedIncidentId) {
            this.renderEmptyIncidentPanels();
            return;
        }
        const incidentId = this.selectedIncidentId;
        const [detail, trace, report, approvals, changes] = await Promise.allSettled([
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/trace`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/report`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/approval`),
            this.apiGet(`${this.apiBaseUrl}/incidents/${encodeURIComponent(incidentId)}/changes`)
        ]);

        if (detail.status === 'fulfilled') {
            this.renderIncidentDetail(detail.value);
        }
        if (trace.status === 'fulfilled') {
            this.renderTraceTimeline(trace.value);
        }
        if (report.status === 'fulfilled') {
            this.renderReport(report.value);
        } else {
            this.renderReportError(report.reason);
        }
        if (approvals.status === 'fulfilled') {
            this.renderApprovals(approvals.value.items || []);
        }
        if (changes.status === 'fulfilled') {
            this.dashboardState.changeExecutions = Array.isArray(changes.value.items) ? changes.value.items : [];
            this.renderChangeExecutions(this.dashboardState.changeExecutions);
        } else {
            this.renderChangeExecutionError(changes.reason);
        }
    }

    renderEmptyIncidentPanels() {
        if (this.selectedIncidentBadge) this.selectedIncidentBadge.textContent = '未选择';
        if (this.incidentDetail) this.incidentDetail.innerHTML = '<div class="empty-state">选择一个故障事件查看详情</div>';
        if (this.traceTimeline) this.traceTimeline.innerHTML = '<div class="empty-state">暂无 Trace 事件</div>';
        if (this.reportViewer) this.reportViewer.innerHTML = '<div class="empty-state">暂无诊断报告</div>';
        if (this.approvalList) this.approvalList.innerHTML = '<div class="empty-state">暂无审批记录</div>';
        if (this.changeExecutionList) this.changeExecutionList.innerHTML = '<div class="empty-state">暂无执行记录</div>';
        this.renderDiagnosisChain({});
        this.renderDependencySignals([]);
        if (this.traceCount) this.traceCount.textContent = '0';
        if (this.reportStatus) this.reportStatus.textContent = '未加载';
        if (this.approvalCount) this.approvalCount.textContent = '0';
        if (this.changeExecutionCount) this.changeExecutionCount.textContent = '0';
    }

    renderIncidentDetail(incident) {
        if (this.selectedIncidentBadge) {
            this.selectedIncidentBadge.textContent = incident.incident_id || 'unknown';
        }
        if (!this.incidentDetail) return;
        const traceSummary = incident.trace_summary || {};
        const approvalSummary = incident.approval_summary || {};
        const chain = incident.diagnosis_chain || {};
        this.incidentDetail.innerHTML = `
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

    renderDiagnosisChain(chain) {
        const safeChain = chain || {};
        this.renderPlanCards(safeChain.plan || []);
        this.renderExecutionSteps(safeChain.steps || []);
        this.renderToolCallTable(safeChain.tool_calls || []);
        this.renderDependencySignals(safeChain.dependency_signals || [], safeChain.tool_calls || []);
        this.renderEvidenceCards(safeChain.evidence || []);
        this.renderConclusionView(safeChain);
    }

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

    extractDependencySignalsFromToolCalls(toolCalls) {
        return (Array.isArray(toolCalls) ? toolCalls : [])
            .filter((call) => ['query_traces', 'query_message_queue_status'].includes(call.tool_name))
            .map((call) => ({
                ...call,
                domain: call.tool_name === 'query_traces' ? 'tracing' : 'message_queue',
                backend: call.data_source || (call.tool_name === 'query_traces' ? 'jaeger/tempo' : 'redpanda/kafka'),
                summary: call.output_summary || call.error_message || ''
            }));
    }

    formatDependencySignalTitle(signal) {
        const domain = signal.domain === 'message_queue' ? 'Redpanda / Kafka' : 'Jaeger / Tempo';
        return `${domain} · ${signal.tool_name || 'dependency signal'}`;
    }

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

    renderTinyList(title, values) {
        const items = Array.isArray(values) ? values.filter(Boolean) : [];
        if (items.length === 0) return '';
        return `
            <p><strong>${this.escapeHtml(title)}：</strong></p>
            <ul class="conclusion-list">${items.map((item) => `<li>${this.escapeHtml(item)}</li>`).join('')}</ul>
        `;
    }

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

    renderTraceTimeline(trace) {
        const items = Array.isArray(trace.items) ? trace.items : [];
        if (this.traceCount) {
            this.traceCount.textContent = String(items.length);
        }
        if (!this.traceTimeline) return;
        if (items.length === 0) {
            this.traceTimeline.innerHTML = '<div class="empty-state">暂无 Trace 事件</div>';
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

    renderReportError(error) {
        if (this.reportStatus) this.reportStatus.textContent = '未生成';
        if (this.reportViewer) {
            this.reportViewer.innerHTML = `<div class="empty-state">${this.escapeHtml(error?.message || '暂无诊断报告')}</div>`;
        }
    }

    renderChangeExecutions(executions) {
        const items = Array.isArray(executions) ? executions : [];
        if (this.changeExecutionCount) {
            this.changeExecutionCount.textContent = String(items.length);
        }
        if (!this.changeExecutionList) return;
        if (items.length === 0) {
            this.changeExecutionList.innerHTML = '<div class="empty-state">暂无执行记录</div>';
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

    renderChangeExecutionError(error) {
        if (this.changeExecutionCount) this.changeExecutionCount.textContent = '0';
        if (this.changeExecutionList) {
            this.changeExecutionList.innerHTML = `<div class="empty-state">${this.escapeHtml(error?.message || '暂无执行记录')}</div>`;
        }
    }

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

    async refreshApprovals() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/approvals/pending`);
            this.dashboardState.approvals = Array.isArray(data.items) ? data.items : [];
            this.renderApprovals(this.dashboardState.approvals);
        } catch (error) {
            if (this.approvalList) {
                this.approvalList.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }

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

    approvalHasNextAction(approval) {
        const changePlan = approval?.change_plan || {};
        return approval?.status === 'approved' && (
            Boolean(approval?.approval_id) || Boolean(changePlan.change_plan_id)
        );
    }

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
                            <button class="action-btn" data-change-resume data-change-mode="sandbox" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}" data-change-plan-id="${this.escapeHtml(changePlan.change_plan_id)}">沙箱验证</button>
                            <button class="action-btn" data-change-resume data-change-mode="manual_record" data-incident-id="${this.escapeHtml(approval.incident_id)}" data-approval-id="${this.escapeHtml(approval.approval_id)}" data-change-plan-id="${this.escapeHtml(changePlan.change_plan_id)}">记录人工变更</button>
                        ` : ''}
                    </div>
                ` : ''}
            </article>
        `;
    }

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

    async refreshEvalSummary() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/eval/summary`);
            this.dashboardState.evalSummary = data;
            this.renderEvalSummary(data);
        } catch (error) {
            if (this.evalStatus) this.evalStatus.textContent = '不可用';
            if (this.evalSummary) {
                this.evalSummary.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message)}</div>`;
            }
        }
    }

    async refreshAdapterVerification() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/eval/adapter-verification`);
            this.dashboardState.adapterVerification = data;
            this.renderAdapterVerification(data);
        } catch (error) {
            this.dashboardState.adapterVerification = null;
            if (this.adapterVerifyStatus) this.adapterVerifyStatus.textContent = '未生成';
            if (this.adapterVerification) {
                this.adapterVerification.innerHTML = `<div class="empty-state">${this.escapeHtml(error.message || '暂无适配器验收报告，请运行 make sandbox-verify')}</div>`;
            }
        }
    }

    async refreshToolContracts() {
        try {
            const data = await this.apiGet(`${this.apiBaseUrl}/aiops/tools/contracts`);
            this.dashboardState.toolContracts = Array.isArray(data.items) ? data.items : [];
            this.dashboardState.toolContractsError = '';
            this.renderToolContracts(data);
        } catch (error) {
            this.dashboardState.toolContracts = [];
            this.dashboardState.toolContractsError = error.message || '工具契约不可用';
            this.renderToolContracts(null);
        }
    }

    renderToolContracts(payload) {
        if (payload && Array.isArray(payload.items)) {
            this.dashboardState.toolContracts = payload.items;
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

    formatToolContractApproval(contract) {
        if (contract.read_only === false) {
            return '需要审批';
        }
        if (contract.risk_level === 'high') {
            return '高风险需复核';
        }
        return '无需审批';
    }

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
                    <p>由 scripts/verify_full_stack_adapters.py 生成。</p>
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

    renderEvalSummary(payload) {
        const available = Boolean(payload && payload.available);
        if (this.evalStatus) {
            this.evalStatus.textContent = available ? '已加载' : '未生成';
        }
        if (!this.evalSummary) return;
        if (!available) {
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
        `;
    }

    resolveEvalDashboard(payload) {
        const dashboard = payload?.dashboard;
        if (dashboard && Array.isArray(dashboard.metrics)) {
            return dashboard;
        }
        return this.buildLegacyEvalDashboard(payload || {});
    }

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
                { key: 'rag_citation_pass_rate', label: 'RAG 引用通过率', value: metrics.rag_recall_at_k ?? rag.recall_at_k, value_type: 'percent', description: '回答引用来源是否覆盖期望 Runbook 文档。' },
                { key: 'p95_latency_ms', label: 'p95 延迟', value: metrics.p95_latency_ms ?? summary.p95_latency_ms, value_type: 'duration_ms', description: '离线评测单 case 执行耗时的 p95。' }
            ]
        };
    }

    formatEvalMetric(metric) {
        const value = metric?.value;
        if (value === null || value === undefined || value === '') return '-';
        if (metric.value_type === 'percent') return this.formatPercent(value);
        if (metric.value_type === 'duration_ms') return `${this.formatNumber(value)} ms`;
        if (metric.value_type === 'integer') return this.formatInteger(value);
        return String(value);
    }

    statusValue(statusOrItem) {
        if (statusOrItem && typeof statusOrItem === 'object') {
            return statusOrItem.status || statusOrItem.status_metadata?.status || 'unknown';
        }
        return statusOrItem || 'unknown';
    }

    resolveStatusMetadata(statusOrItem) {
        if (!statusOrItem || typeof statusOrItem !== 'object') {
            return null;
        }
        return statusOrItem.status_metadata || statusOrItem.statusMetadata || null;
    }

    statusMetadataFromCatalog(status) {
        const value = status || 'unknown';
        return (this.aiOpsStatusCatalog || []).find((item) => item?.status === value) || null;
    }

    statusTone(statusOrItem) {
        const metadata = this.resolveStatusMetadata(statusOrItem);
        if (metadata?.tone) {
            return metadata.tone === 'neutral' ? '' : metadata.tone;
        }
        const status = this.statusValue(statusOrItem);
        const catalogMetadata = this.statusMetadataFromCatalog(status);
        if (catalogMetadata?.tone) {
            return catalogMetadata.tone === 'neutral' ? '' : catalogMetadata.tone;
        }
        if (['healthy', 'completed', 'success', 'approved', 'approval_approved', 'approval_resumed', 'closed', 'dry_run_completed', 'sandbox_validated', 'change_validated', 'passed', 'manual_execution_recorded', 'available', 'configured'].includes(status)) return 'success';
        if (['waiting_approval', 'pending', 'degraded', 'running', 'waiting', 'empty', 'unknown', 'waiting_manual_execution', 'dry_run_running', 'precheck_running', 'sandbox_executing', 'observing'].includes(status)) return 'warning';
        if (['failed', 'error', 'rejected', 'approval_rejected', 'blocked', 'forbidden', 'unhealthy', 'unavailable', 'not_configured', 'precheck_failed', 'dry_run_failed', 'rollback_recommended', 'escalated'].includes(status)) return 'error';
        return '';
    }

    riskTone(riskLevel) {
        if (riskLevel === 'low') return 'success';
        if (riskLevel === 'medium') return 'warning';
        if (riskLevel === 'high') return 'error';
        return '';
    }

    sourcePill(source, options = {}) {
        const value = String(source || 'unknown').trim() || 'unknown';
        const metadata = this.sourceMetadata(value);
        const text = options.count !== undefined
            ? `${value}=${String(options.count)}`
            : value;
        const label = metadata.label ? ` · ${metadata.label}` : '';
        const classNames = [
            'source-pill',
            metadata.tone,
            this.sourceClassName(value)
        ].filter(Boolean).join(' ');

        return `<span class="${this.escapeHtml(classNames)}" title="${this.escapeHtml(metadata.title || value)}">${this.escapeHtml(text + label)}</span>`;
    }

    sourceClassName(source) {
        const normalized = String(source || 'unknown')
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, '-')
            .replace(/^-+|-+$/g, '');
        return normalized ? `source-${normalized}` : 'source-unknown';
    }

    sourceMetadata(source) {
        const value = String(source || 'unknown').trim().toLowerCase();
        const realSources = new Set([
            'alertmanager',
            'cmdb',
            'deploy_history',
            'jaeger',
            'kafka',
            'kubernetes',
            'log_gateway',
            'loki',
            'mcp_cls',
            'mcp_monitor',
            'milvus',
            'mysql',
            'prometheus',
            'rag',
            'redis',
            'redpanda',
            'session_snapshot',
            'tempo',
            'ticket_api',
            'trace_store'
        ]);

        if (value.includes('mixed')) {
            return { tone: 'mixed', label: 'Mixed', title: '包含真实适配器和模拟/降级数据' };
        }
        if (value.includes('mock') || value.includes('fallback') || value.includes('synthetic')) {
            return { tone: 'mock', label: 'Mock', title: '模拟或降级数据，不能代表生产事实' };
        }
        if (
            value === 'unknown'
            || value === 'not_configured'
            || value === 'unavailable'
            || value.includes('not_configured')
            || value.includes('unavailable')
        ) {
            return { tone: 'unavailable', label: 'Unavailable', title: '数据源未知、未配置或不可用' };
        }
        if (value.includes('failed') || value.includes('error')) {
            return { tone: 'error', label: 'Failed', title: '数据源调用失败' };
        }
        if (realSources.has(value) || value.startsWith('mcp_')) {
            return { tone: 'real', label: 'Real', title: '来自已配置的真实适配器或持久化数据' };
        }
        return { tone: '', label: '', title: source };
    }

    formatPercent(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return `${Math.round(numeric * 1000) / 10}%`;
    }

    formatNumber(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return String(Math.round(numeric * 100) / 100);
    }

    formatInteger(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return String(Math.round(numeric));
    }

    formatConfidence(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return `${Math.round(numeric * 100)}%`;
    }

    formatDateTime(value) {
        if (!value) return '-';
        try {
            return new Date(value).toLocaleString();
        } catch {
            return value;
        }
    }

    // 生成随机会话ID
    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    }

    // 发送消息
    async sendMessage() {
        let message = '';
        if (this.messageInput) {
            message = this.messageInput.value.trim();
        }
        
        if (!message) {
            this.showNotification('请输入消息内容', 'warning');
            return;
        }

        if (this.isStreaming) {
            this.showNotification('请等待当前对话完成', 'warning');
            return;
        }

        // 显示用户消息
        this.addMessage('user', message);
        
        // 清空输入框
        if (this.messageInput) {
            this.messageInput.value = '';
        }

        // 设置发送状态
        this.isStreaming = true;
        this.updateUI();

        try {
            if (this.currentMode === 'quick') {
                await this.sendQuickMessage(message);
            } else if (this.currentMode === 'stream') {
                await this.sendStreamMessage(message);
            }
        } catch (error) {
            console.error('发送消息失败:', error);
            this.addMessage('assistant', '抱歉，发送消息时出现错误：' + error.message);
        } finally {
            this.isStreaming = false;
            this.updateUI();
            
            // 如果当前对话是从历史记录加载的，更新历史记录
            if (this.isCurrentChatFromHistory && this.currentChatHistory.length > 0) {
                this.updateCurrentChatHistory();
                this.renderChatHistory(); // 更新历史对话列表显示
            }
        }
    }

    // 发送快速消息（普通对话）
    async sendQuickMessage(message) {
        // 添加等待提示消息
        const loadingMessage = this.addLoadingMessage('正在思考...');
        
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            const data = await response.json();            
            // 移除等待提示消息
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }
            
            // 统一响应格式：检查 data.code 或 data.message 判断请求是否成功
            if (data.code === 200 || data.message === 'success') {
                // data.data 是 ChatResponse 对象
                const chatResponse = data.data;
                
                if (chatResponse && chatResponse.success) {
                    // 成功：添加实际响应消息（即使 answer 为空也显示）
                    const answer = chatResponse.answer || '（无回复内容）';
                    this.addMessage('assistant', answer, false, true, this.buildRagMetadata(chatResponse));
                } else if (chatResponse && chatResponse.errorMessage) {
                    // 业务错误
                    throw new Error(chatResponse.errorMessage);
                } else {
                    // 兜底：尝试显示任何可用内容
                    const fallbackAnswer = chatResponse?.answer || chatResponse?.errorMessage || '服务返回了空内容';
                    this.addMessage('assistant', fallbackAnswer, false, true, this.buildRagMetadata(chatResponse));
                }
            } else {
                // HTTP 成功但业务失败
                throw new Error(data.message || '请求失败');
            }
        } catch (error) {
            // 出错时也要移除等待提示消息
            if (loadingMessage && loadingMessage.parentNode) {
                loadingMessage.parentNode.removeChild(loadingMessage);
            }
            throw error;
        }
    }

    // 发送流式消息
    async sendStreamMessage(message) {
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/chat_stream`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    Id: this.sessionId,
                    Question: message
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }
            
            // 创建助手消息元素
            const assistantMessageElement = this.addMessage('assistant', '', true);
            let fullResponse = '';
            let streamRagMetadata = null;

            // 处理流式响应
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let currentEvent = '';

            try {
                while (true) {
                    const { done, value } = await reader.read();
                    
                    if (done) {
                        // 流结束，使用统一的处理方法
                        this.handleStreamComplete(assistantMessageElement, fullResponse, streamRagMetadata);
                        break;
                    }

                    // 解码数据并添加到缓冲区
                    buffer += decoder.decode(value, { stream: true });
                    
                    // 按行分割处理
                    const lines = buffer.split('\n');
                    // 保留最后一行（可能不完整）
                    buffer = lines.pop() || '';
                    
                    for (const line of lines) {
                        if (line.trim() === '') continue;
                        
                        // 解析SSE格式
                        if (line.startsWith('id:')) {
                            continue;
                        } else if (line.startsWith('event:')) {
                            // 兼容 "event:message" 和 "event: message" 两种格式
                            currentEvent = line.substring(6).trim();
                            // 注意：后端统一使用 "message" 事件名，真正的类型在 data 的 JSON 中
                            continue;
                        } else if (line.startsWith('data:')) {
                            // 兼容 "data:xxx" 和 "data: xxx" 两种格式
                            const rawData = line.substring(5).trim();
                            
                            // 兼容旧格式 [DONE] 标记
                            if (rawData === '[DONE]') {
                                // 流结束标记，将内容转换为Markdown渲染
                                this.handleStreamComplete(assistantMessageElement, fullResponse, streamRagMetadata);
                                return;
                            }
                            
                            // 处理 SSE 数据
                            try {
                                // 尝试解析为 SseMessage 格式的 JSON
                                const sseMessage = JSON.parse(rawData);
                                
                                if (sseMessage && typeof sseMessage.type === 'string') {
                                    if (sseMessage.type === 'content') {
                                        const content = sseMessage.data || '';
                                        fullResponse += content;
                                        
                                        // 实时渲染 Markdown
                                        if (assistantMessageElement) {
                                            const messageContent = assistantMessageElement.querySelector('.message-content');
                                            messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                            // 高亮代码块
                                            this.highlightCodeBlocks(messageContent);
                                            this.scrollToBottom();
                                        }
                                    } else if (sseMessage.type === 'search_results') {
                                        streamRagMetadata = this.buildRagMetadata({ retrieval: sseMessage.data });
                                        this.renderRagSources(assistantMessageElement, streamRagMetadata);
                                    } else if (sseMessage.type === 'done') {
                                        const doneMetadata = this.buildRagMetadata(sseMessage.data || {});
                                        streamRagMetadata = this.mergeRagMetadata(streamRagMetadata, doneMetadata);
                                        fullResponse = (sseMessage.data && sseMessage.data.answer) || fullResponse;
                                        this.handleStreamComplete(assistantMessageElement, fullResponse, streamRagMetadata);
                                        return;
                                    } else if (sseMessage.type === 'error') {
                                        console.error('[SSE调试] 收到错误:', sseMessage.data);
                                        if (assistantMessageElement) {
                                            const messageContent = assistantMessageElement.querySelector('.message-content');
                                            messageContent.innerHTML = this.renderMarkdown('错误: ' + (sseMessage.data || '未知错误'));
                                        }
                                        return;
                                    }
                                } else {
                                    // 不是标准 SseMessage 格式，尝试兼容处理
                                    fullResponse += rawData;
                                    if (assistantMessageElement) {
                                        const messageContent = assistantMessageElement.querySelector('.message-content');
                                        messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                        this.highlightCodeBlocks(messageContent);
                                        this.scrollToBottom();
                                    }
                                }
                            } catch (e) {
                                // JSON 解析失败，尝试兼容旧格式
                                if (rawData === '') {
                                    fullResponse += '\n';
                                } else {
                                    fullResponse += rawData;
                                }
                                
                                if (assistantMessageElement) {
                                    const messageContent = assistantMessageElement.querySelector('.message-content');
                                    messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                    this.highlightCodeBlocks(messageContent);
                                    this.scrollToBottom();
                                }
                            }
                        }
                    }
                }
            } finally {
                reader.releaseLock();
            }
        } catch (error) {
            throw error;
        }
    }

    // 添加消息到聊天界面
    addMessage(type, content, isStreaming = false, saveToHistory = true, metadata = null) {
        // 检查是否是第一条消息，如果是则移除居中样式
        const isFirstMessage = this.chatMessages && this.chatMessages.querySelectorAll('.message').length === 0;
        
        // 保存消息到当前对话历史（如果不是流式消息且需要保存）
        if (!isStreaming && saveToHistory && content) {
            this.currentChatHistory.push({
                type: type,
                content: content,
                metadata: metadata,
                timestamp: new Date().toISOString()
            });
        }
        
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}${isStreaming ? ' streaming' : ''}`;

        // 如果是assistant消息，添加头像图标
        if (type === 'assistant') {
            const messageAvatar = document.createElement('div');
            messageAvatar.className = 'message-avatar';
            messageAvatar.innerHTML = `
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="white"/>
                </svg>
            `;
            messageDiv.appendChild(messageAvatar);
        }

        // 创建消息内容包装器
        const messageContentWrapper = document.createElement('div');
        messageContentWrapper.className = 'message-content-wrapper';

        const messageContent = document.createElement('div');
        messageContent.className = 'message-content';
        
        // 如果是assistant消息且不是流式消息，使用Markdown渲染
        if (type === 'assistant' && !isStreaming) {
            messageContent.innerHTML = this.renderMarkdown(content);
            // 高亮代码块
            this.highlightCodeBlocks(messageContent);
        } else {
            // 用户消息或流式消息使用纯文本
            messageContent.textContent = content;
        }

        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);
        if (type === 'assistant' && metadata) {
            this.renderRagSources(messageDiv, metadata);
        }

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            
            // 如果是第一条消息，移除居中样式并添加动画
            if (isFirstMessage && this.chatContainer) {
                this.chatContainer.classList.remove('centered');
                // 添加动画类
                this.chatContainer.style.transition = 'all 0.5s ease';
            }
            
            this.scrollToBottom();
        }

        return messageDiv;
    }

    // 添加带加载动画的消息
    addLoadingMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message assistant';

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

        const messageContent = document.createElement('div');
        messageContent.className = 'message-content loading-message-content';
        
        // 创建文本和动画容器
        const textSpan = document.createElement('span');
        textSpan.textContent = content;
        
        // 创建旋转动画图标
        const loadingIcon = document.createElement('span');
        loadingIcon.className = 'loading-spinner-icon';
        loadingIcon.innerHTML = `
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z" fill="currentColor" opacity="0.2"/>
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10c1.54 0 3-.36 4.28-1l-1.5-2.6C13.64 19.62 12.84 20 12 20c-4.41 0-8-3.59-8-8s3.59-8 8-8c.84 0 1.64.38 2.18 1l1.5-2.6C13 2.36 12.54 2 12 2z" fill="currentColor"/>
            </svg>
        `;
        
        messageContent.appendChild(textSpan);
        messageContent.appendChild(loadingIcon);
        messageContentWrapper.appendChild(messageContent);
        messageDiv.appendChild(messageContentWrapper);

        if (this.chatMessages) {
            this.chatMessages.appendChild(messageDiv);
            
            // 如果是第一条消息，移除居中样式
            const isFirstMessage = this.chatMessages.querySelectorAll('.message').length === 1;
            if (isFirstMessage && this.chatContainer) {
                this.chatContainer.classList.remove('centered');
                this.chatContainer.style.transition = 'all 0.5s ease';
            }
            
            this.scrollToBottom();
        }

        return messageDiv;
    }
    
    // 检查并设置居中样式
    checkAndSetCentered() {
        if (this.chatMessages && this.chatContainer) {
            const hasMessages = this.chatMessages.querySelectorAll('.message').length > 0;
            if (!hasMessages) {
                this.chatContainer.classList.add('centered');
            } else {
                this.chatContainer.classList.remove('centered');
            }
        }
    }

    // 滚动到底部
    scrollToBottom() {
        if (this.chatMessages) {
            this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
        }
    }

    // 处理流式传输完成
    handleStreamComplete(assistantMessageElement, fullResponse, metadata = null) {
        if (assistantMessageElement) {
            assistantMessageElement.classList.remove('streaming');
            const messageContent = assistantMessageElement.querySelector('.message-content');
            if (messageContent) {
                messageContent.innerHTML = this.renderMarkdown(fullResponse);
                // 高亮代码块
                this.highlightCodeBlocks(messageContent);
            }
            this.renderRagSources(assistantMessageElement, metadata);
        }
        // 保存流式消息到历史记录
        if (fullResponse) {
            this.currentChatHistory.push({
                type: 'assistant',
                content: fullResponse,
                metadata: metadata,
                timestamp: new Date().toISOString()
            });
            // 如果当前对话是从历史记录加载的，更新历史记录
            if (this.isCurrentChatFromHistory) {
                this.updateCurrentChatHistory();
                this.renderChatHistory();
            }
        }
    }

    buildRagMetadata(payload) {
        if (!payload || typeof payload !== 'object') return null;
        const retrieval = payload.retrieval || payload;
        const citations = Array.isArray(payload.citations) && payload.citations.length > 0
            ? payload.citations
            : (Array.isArray(retrieval.retrieval_results) ? retrieval.retrieval_results : []);
        const rejected = Array.isArray(retrieval.rejected_results) ? retrieval.rejected_results : [];
        if (!retrieval.status && citations.length === 0 && rejected.length === 0 && payload.no_answer !== true && payload.noAnswer !== true) {
            return null;
        }
        return {
            citations,
            rejectedResults: rejected,
            retrieval,
            noAnswer: Boolean(payload.no_answer || payload.noAnswer || retrieval.no_answer_rejected),
            answerPolicy: payload.answer_policy || payload.answerPolicy || retrieval.answer_policy || ''
        };
    }

    mergeRagMetadata(base, next) {
        if (!base) return next || null;
        if (!next) return base;
        return {
            citations: next.citations && next.citations.length > 0 ? next.citations : base.citations,
            rejectedResults: next.rejectedResults && next.rejectedResults.length > 0 ? next.rejectedResults : base.rejectedResults,
            retrieval: next.retrieval && next.retrieval.status ? next.retrieval : base.retrieval,
            noAnswer: Boolean(base.noAnswer || next.noAnswer),
            answerPolicy: next.answerPolicy || base.answerPolicy || ''
        };
    }

    renderRagSources(messageElement, metadata) {
        if (!messageElement || !metadata) return;
        const wrapper = messageElement.querySelector('.message-content-wrapper');
        if (!wrapper) return;

        const existing = wrapper.querySelector('.rag-sources');
        if (existing) {
            existing.remove();
        }

        const citations = Array.isArray(metadata.citations) ? metadata.citations : [];
        const rejected = Array.isArray(metadata.rejectedResults) ? metadata.rejectedResults : [];
        const retrieval = metadata.retrieval || {};
        if (citations.length === 0 && rejected.length === 0 && !metadata.noAnswer) {
            return;
        }

        const sources = document.createElement('section');
        sources.className = `rag-sources${metadata.noAnswer ? ' no-answer' : ''}`;
        const citationHtml = citations.length > 0
            ? citations.map((item) => this.renderRagSourceItem(item, false)).join('')
            : '<div class="empty-state">本轮回答没有可用引用来源</div>';
        const rejectedHtml = rejected.length > 0
            ? `
                <details class="rag-rejected">
                    <summary>查看被拒绝的候选片段 (${rejected.length})</summary>
                    ${rejected.map((item) => this.renderRagSourceItem(item, true)).join('')}
                </details>
            `
            : '';

        sources.innerHTML = `
            <div class="rag-sources-header">
                <strong>${metadata.noAnswer ? '拒答边界' : '引用来源'}</strong>
                <span>${this.escapeHtml(retrieval.status || metadata.answerPolicy || 'rag')}</span>
            </div>
            ${retrieval.summary ? `<p class="rag-summary">${this.escapeHtml(retrieval.summary)}</p>` : ''}
            <div class="rag-source-list">${citationHtml}</div>
            ${rejectedHtml}
        `;
        wrapper.appendChild(sources);
    }

    renderRagSourceItem(item, rejected = false) {
        const score = this.formatRetrievalScore(item.score);
        const preview = item.content_preview || item.content || '';
        return `
            <details class="rag-source-item${rejected ? ' rejected' : ''}">
                <summary>
                    <span>${this.escapeHtml(item.source_file || '未知来源')}</span>
                    <span>${this.escapeHtml(item.chunk_id || 'unknown-chunk')}</span>
                    <span>score=${this.escapeHtml(score)}</span>
                </summary>
                ${item.heading_path ? `<p><strong>标题：</strong>${this.escapeHtml(item.heading_path)}</p>` : ''}
                ${item.retrieval_reason ? `<p><strong>检索说明：</strong>${this.escapeHtml(item.retrieval_reason)}</p>` : ''}
                ${preview ? `<p>${this.escapeHtml(preview)}</p>` : ''}
            </details>
        `;
    }

    formatRetrievalScore(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return 'unknown';
        return numeric.toFixed(4);
    }

    // 显示通知
    showNotification(message, type = 'info') {
        // 创建通知元素
        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.textContent = message;
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 20px;
            border-radius: 8px;
            color: white;
            font-weight: 500;
            z-index: 10000;
            animation: slideIn 0.3s ease;
            max-width: 300px;
        `;

        // 根据类型设置颜色（Google Material Design配色）
        const colors = {
            info: '#1a73e8',
            success: '#34a853',
            warning: '#fbbc04',
            error: '#ea4335'
        };
        notification.style.backgroundColor = colors[type] || colors.info;

        // 添加到页面
        document.body.appendChild(notification);

        // 3秒后自动移除
        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => {
                if (notification.parentNode) {
                    notification.parentNode.removeChild(notification);
                }
            }, 300);
        }, 3000);
    }

    // 处理文件选择
    handleFileSelect(event) {
        const file = event.target.files[0];
        if (file) {
            // 验证文件格式
            if (!this.validateFileType(file)) {
                this.showNotification('只支持上传 TXT 或 Markdown (.md) 格式的文件', 'error');
                this.fileInput.value = '';
                return;
            }
            this.uploadFile(file);
        }
    }

    // 验证文件类型
    validateFileType(file) {
        const fileName = file.name.toLowerCase();
        const allowedExtensions = ['.txt', '.md', '.markdown'];
        return allowedExtensions.some(ext => fileName.endsWith(ext));
    }

    // 上传文件到知识库
    async uploadFile(file) {
        // 再次验证文件类型（双重保险）
        if (!this.validateFileType(file)) {
            this.showNotification('只支持上传 TXT 或 Markdown (.md) 格式的文件', 'error');
            return;
        }

        // 验证文件大小（与后端 MAX_FILE_SIZE 保持一致）
        const maxSizeMb = 10;
        const maxSize = maxSizeMb * 1024 * 1024;
        if (file.size > maxSize) {
            this.showNotification(`文件大小不能超过${maxSizeMb}MB`, 'error');
            return;
        }

        // 锁定前端并显示上传遮罩层
        this.isStreaming = true;
        this.updateUI();
        this.renderKnowledgeUploadProgress(file);
        this.showUploadOverlay(true, file.name);

        try {
            // 创建 FormData
            const formData = new FormData();
            formData.append('file', file);

            // 发送上传请求
            const response = await this.apiFetch(`${this.apiBaseUrl}/upload`, {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            const data = await response.json();

            const acceptedUploadStatus = data.code === 200
                || data.code === 207
                || ['success', 'partial_success'].includes(data.message);
            if (acceptedUploadStatus && data.data) {
                const indexing = data.data.indexing || {};
                const uploadSummary = this.formatUploadIndexingMessage(data.data);
                this.updateKnowledgeUploadResult(data.data);
                if (indexing.status === 'failed') {
                    const errorMessage = indexing.error_message || '未知错误';
                    const warningMessage = `${uploadSummary}\n\n向量索引失败原因：${errorMessage}`;
                    this.addMessage('assistant', warningMessage, false, true);
                    this.showNotification(warningMessage, 'warning');
                } else if (indexing.status === 'empty') {
                    const warningMessage = `${uploadSummary}\n\n索引结果：未生成可检索内容。${indexing.message || ''}`;
                    this.addMessage('assistant', warningMessage, false, true);
                    this.showNotification(warningMessage, 'warning');
                } else {
                    // 在聊天界面显示上传成功消息
                    const successMessage = `${uploadSummary}\n\n索引结果：可用于后续 RAG 检索。`;
                    this.addMessage('assistant', successMessage, false, true);
                }
            } else {
                throw new Error(data.message || '上传失败');
            }
        } catch (error) {
            console.error('文件上传失败:', error);
            this.renderKnowledgeUploadError(file, error);
            this.showNotification('文件上传失败: ' + error.message, 'error');
        } finally {
            // 清空文件输入
            if (this.fileInput) {
                this.fileInput.value = '';
            }
            // 解锁前端
            this.isStreaming = false;
            this.showUploadOverlay(false);
            this.updateUI();
        }
    }

    formatUploadIndexingMessage(payload) {
        const indexing = payload.indexing || {};
        const lines = [
            `知识库文件：${payload.filename || 'unknown'}`,
            `上传大小：${this.formatFileSize(payload.size || 0)}`,
            `覆盖已有文件：${payload.overwritten ? '是' : '否'}`,
            `索引状态：${indexing.status || 'unknown'}`,
            `分块数量：${indexing.chunk_count ?? 0}`,
            `索引耗时：${indexing.duration_ms ?? 0} ms`
        ];
        if (indexing.message) {
            lines.push(`说明：${indexing.message}`);
        }
        return lines.join('\n');
    }

    // 格式化文件大小
    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    // 发送智能运维请求（SSE 流式模式）
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

    parseSseJson(rawData) {
        try {
            const message = JSON.parse(rawData);
            return message && typeof message.type === 'string' ? message : null;
        } catch {
            return null;
        }
    }

    applyAIOpsEvent(message, runState, currentText) {
        this.collectAIOpsEvent(message, runState);
        this.renderLiveAIOpsProgress(message, runState);
        return this.reduceAIOpsEvent(message, runState, currentText);
    }

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

    getSelectedAIOpsIncident() {
        return this.buildAIOpsIncidentFromForm();
    }

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

    readInputValue(element) {
        return element && typeof element.value === 'string' ? element.value.trim() : '';
    }

    generateAIOpsIncidentId(serviceName) {
        const normalizedService = String(serviceName || 'incident')
            .toUpperCase()
            .replace(/[^A-Z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 24) || 'INCIDENT';
        return `INC-${normalizedService}-${Date.now().toString(36).toUpperCase()}`;
    }

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

    applyAIOpsTemplate(preset) {
        const template = this.lookupAIOpsTemplate(preset);
        if (!template) {
            this.setAIOpsFormStatus('手动输入', 'warning');
            return;
        }
        this.populateAIOpsIncidentForm(template);
        this.setAIOpsFormStatus('模板已填充', 'success');
    }

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

    setInputValue(element, value) {
        if (element) {
            element.value = value;
        }
    }

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
        this.setAIOpsFormStatus('手动输入', 'warning');
    }

    setAIOpsFormStatus(text, tone = '') {
        if (!this.aiOpsFormStatus) return;
        this.aiOpsFormStatus.textContent = text;
        const directTone = ['success', 'warning', 'error'].includes(tone) ? tone : '';
        this.aiOpsFormStatus.className = directTone || (tone ? this.statusTone(tone) : '');
    }

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

    setResponseAttention(active) {
        const responseNav = document.querySelector('[data-workbench-view="response"]');
        if (responseNav) {
            responseNav.classList.toggle('has-attention', Boolean(active));
        }
    }

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

    getLatestToolCall(toolCalls) {
        const items = Array.isArray(toolCalls) ? toolCalls : [];
        return items.length > 0 ? items[items.length - 1] : {};
    }

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

    currentIsoTime() {
        return new Date().toISOString();
    }

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

    isTextAlreadyIncluded(container, text) {
        if (!container || !text) return false;
        const compactContainer = container.replace(/\s+/g, ' ').trim();
        const compactText = text.replace(/\s+/g, ' ').trim();
        if (!compactText) return true;
        return compactContainer.includes(compactText.slice(0, Math.min(compactText.length, 200)));
    }

    // 更新智能运维流式内容（实时显示）
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
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // 触发智能运维（点击智能运维按钮时直接调用）
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
}

// 添加CSS动画
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(100%);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(100%);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// 初始化应用
function bootstrapAutoOnCallApp() {
    new AutoOnCallApp();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapAutoOnCallApp);
} else {
    bootstrapAutoOnCallApp();
}
