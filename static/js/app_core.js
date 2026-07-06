// AutoOnCall 前端应用核心
class AutoOnCallApp {
    constructor() {
        this.apiBaseUrl = '/api';
        this.currentMode = 'quick'; // 'quick' 或 'stream'
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.currentChatHistory = []; // 当前对话的消息历史
        this.chatHistories = this.loadChatHistories(); // 所有历史对话
        this.knowledgeUploadState = this.loadKnowledgeUploadState();
        this.uploadConstraints = {
            allowedExtensions: ['.txt', '.md', '.markdown'],
            maxSizeMb: 10,
            maxSize: 10 * 1024 * 1024
        };
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
            incidentReplay: null,
            replayFilters: {
                stage: 'all',
                status: 'all',
                query: ''
            },
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
        this.loadUploadConstraints();
        this.setWorkbenchView(this.currentWorkbenchView);
        this.loadAIOpsStatusCatalog();
        this.loadAIOpsDemoIncidents();
        this.restoreLastAIOpsRun();
    }

    // 初始化Markdown配置
}

window.AutoOnCallApp = AutoOnCallApp;

// Core AutoOnCall application shell, DOM wiring, and markdown rendering.
Object.assign(window.AutoOnCallApp.prototype, {
    initMarkdown() {
        if (typeof marked === 'undefined') return;
        try {
            marked.setOptions({
                breaks: true,
                gfm: true,
                headerIds: false,
                mangle: false
            });

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
            }
        } catch (e) {
            console.error('Markdown 配置失败:', e);
        }
    }

    // 安全地渲染 Markdown
,
    renderMarkdown(content) {
        if (!content) return '';
        
        try {
            const html = typeof marked !== 'undefined'
                ? marked.parse(content)
                : this.renderBasicMarkdown(content);
            return this.sanitizeRenderedHtml(html);
        } catch (e) {
            console.error('Markdown 渲染失败:', e);
            return this.escapeHtml(content);
        }
    }
,
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

    // 无外部依赖的简版 Markdown 渲染，保障离线演示可读性
,
    renderBasicMarkdown(content) {
        const lines = String(content || '').replace(/\r\n/g, '\n').split('\n');
        const blocks = [];
        let paragraph = [];
        let listItems = [];
        let inCode = false;
        let codeLanguage = '';
        let codeLines = [];

        const flushParagraph = () => {
            if (!paragraph.length) return;
            blocks.push(`<p>${this.renderInlineMarkdown(paragraph.join(' '))}</p>`);
            paragraph = [];
        };
        const flushList = () => {
            if (!listItems.length) return;
            blocks.push(`<ul>${listItems.map((item) => `<li>${this.renderInlineMarkdown(item)}</li>`).join('')}</ul>`);
            listItems = [];
        };
        const flushCode = () => {
            const languageClass = codeLanguage ? ` class="language-${this.escapeHtml(codeLanguage)}"` : '';
            blocks.push(`<pre><code${languageClass}>${this.escapeHtml(codeLines.join('\n'))}</code></pre>`);
            codeLanguage = '';
            codeLines = [];
        };

        lines.forEach((line) => {
            const codeFence = line.match(/^```(\w+)?\s*$/);
            if (codeFence) {
                if (inCode) {
                    flushCode();
                    inCode = false;
                } else {
                    flushParagraph();
                    flushList();
                    inCode = true;
                    codeLanguage = codeFence[1] || '';
                }
                return;
            }
            if (inCode) {
                codeLines.push(line);
                return;
            }
            if (!line.trim()) {
                flushParagraph();
                flushList();
                return;
            }
            const heading = line.match(/^(#{1,4})\s+(.+)$/);
            if (heading) {
                flushParagraph();
                flushList();
                const level = heading[1].length;
                blocks.push(`<h${level}>${this.renderInlineMarkdown(heading[2])}</h${level}>`);
                return;
            }
            const list = line.match(/^\s*[-*]\s+(.+)$/);
            if (list) {
                flushParagraph();
                listItems.push(list[1]);
                return;
            }
            paragraph.push(line.trim());
        });

        if (inCode) flushCode();
        flushParagraph();
        flushList();
        return blocks.join('');
    }
,
    renderInlineMarkdown(text) {
        return this.escapeHtml(text)
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\*([^*]+)\*/g, '<em>$1</em>');
    }

    // 高亮代码块
,
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
,
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
        this.incidentReplay = document.getElementById('incidentReplay');
        this.replayStatus = document.getElementById('replayStatus');
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
,
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
            if (this.modeSelectorBtn && this.modeDropdown &&
                !this.modeSelectorBtn.contains(e.target) &&
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

        if (this.incidentReplay) {
            this.incidentReplay.addEventListener('change', (event) => {
                this.handleReplayFilterChange(event, { render: true });
            });
            this.incidentReplay.addEventListener('input', (event) => {
                this.handleReplayFilterChange(event, { render: false });
            });
            this.incidentReplay.addEventListener('keydown', (event) => {
                if (event.key !== 'Enter') return;
                const target = event.target.closest('[data-replay-filter]');
                if (!target) return;
                event.preventDefault();
                this.handleReplayFilterChange(event, { render: true });
            });
            this.incidentReplay.addEventListener('click', (event) => {
                const action = event.target.closest('[data-replay-filter-action]');
                if (!action) return;
                const type = action.getAttribute('data-replay-filter-action');
                if (type === 'clear') {
                    this.resetReplayFilters();
                    this.renderIncidentReplay(this.dashboardState.incidentReplay);
                }
                if (type === 'apply') {
                    this.renderIncidentReplay(this.dashboardState.incidentReplay);
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
});
