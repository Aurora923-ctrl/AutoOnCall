// Knowledge chat, RAG metadata, notifications, and file upload.
Object.assign(window.AutoOnCallApp.prototype, {
    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    }

    // 发送消息
,
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
,
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

            const data = await this.readJsonResponse(response);
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
,
    async sendStreamMessage(message) {
        let assistantMessageElement = null;
        try {
            const response = await this.apiFetch(`${this.apiBaseUrl}/chat_stream`, {
                method: 'POST',
                timeoutMs: 0,
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
            assistantMessageElement = this.addMessage('assistant', '', true);
            let fullResponse = '';
            let streamRagMetadata = null;

            // 处理流式响应
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let terminalReceived = false;

            const handleSseEvent = ({ data: rawData }) => {
                if (rawData === '[DONE]') {
                    terminalReceived = true;
                    this.handleStreamComplete(
                        assistantMessageElement,
                        fullResponse,
                        streamRagMetadata
                    );
                    return true;
                }

                try {
                    const sseMessage = JSON.parse(rawData);
                    if (sseMessage && typeof sseMessage.type === 'string') {
                        if (sseMessage.type === 'content') {
                            const content = sseMessage.data || '';
                            fullResponse += content;

                            if (assistantMessageElement) {
                                const messageContent = assistantMessageElement.querySelector('.message-content');
                                messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                this.highlightCodeBlocks(messageContent);
                                this.scrollToBottom();
                            }
                        } else if (sseMessage.type === 'replace_content') {
                            fullResponse = sseMessage.data || '';
                            if (assistantMessageElement) {
                                const messageContent = assistantMessageElement.querySelector('.message-content');
                                messageContent.innerHTML = this.renderMarkdown(fullResponse);
                                this.highlightCodeBlocks(messageContent);
                                this.scrollToBottom();
                            }
                        } else if (sseMessage.type === 'search_results') {
                            streamRagMetadata = this.buildRagMetadata({ retrieval: sseMessage.data });
                            this.renderRagSources(assistantMessageElement, streamRagMetadata);
                        } else if (sseMessage.type === 'done') {
                            terminalReceived = true;
                            const doneMetadata = this.buildRagMetadata(sseMessage.data || {});
                            streamRagMetadata = this.mergeRagMetadata(streamRagMetadata, doneMetadata);
                            fullResponse = (sseMessage.data && sseMessage.data.answer) || fullResponse;
                            this.handleStreamComplete(
                                assistantMessageElement,
                                fullResponse,
                                streamRagMetadata
                            );
                            return true;
                        } else if (sseMessage.type === 'error') {
                            terminalReceived = true;
                            console.error('[SSE调试] 收到错误事件');
                            this.handleStreamFailure(
                                assistantMessageElement,
                                sseMessage.data || '未知错误'
                            );
                            return true;
                        }
                    } else {
                        fullResponse += rawData;
                        if (assistantMessageElement) {
                            const messageContent = assistantMessageElement.querySelector('.message-content');
                            messageContent.innerHTML = this.renderMarkdown(fullResponse);
                            this.highlightCodeBlocks(messageContent);
                            this.scrollToBottom();
                        }
                    }
                } catch (e) {
                    fullResponse += rawData === '' ? '\n' : rawData;
                    if (assistantMessageElement) {
                        const messageContent = assistantMessageElement.querySelector('.message-content');
                        messageContent.innerHTML = this.renderMarkdown(fullResponse);
                        this.highlightCodeBlocks(messageContent);
                        this.scrollToBottom();
                    }
                }
                return false;
            };
            const parser = this.createSseParser(handleSseEvent);

            try {
                while (true) {
                    const { done, value } = await reader.read();
                    
                    if (done) {
                        if (parser.push(decoder.decode()) || parser.finish()) {
                            return;
                        }
                        if (!terminalReceived) {
                            this.handleStreamFailure(
                                assistantMessageElement,
                                '流式响应在完成事件前中断，请重试'
                            );
                        }
                        return;
                    }

                    if (parser.push(decoder.decode(value, { stream: true }))) {
                        return;
                    }
                }
            } finally {
                reader.releaseLock();
            }
        } catch (error) {
            if (assistantMessageElement) {
                console.error('流式响应读取失败:', error);
                this.handleStreamFailure(
                    assistantMessageElement,
                    error?.message || '流式响应读取失败，请重试'
                );
                return;
            }
            throw error;
        }
    }
,
    handleStreamFailure(assistantMessageElement, message) {
        if (!assistantMessageElement) return;
        assistantMessageElement.classList.remove('streaming');
        const messageContent = assistantMessageElement.querySelector('.message-content');
        if (messageContent) {
            messageContent.innerHTML = this.renderMarkdown('错误: ' + message);
        }
        this.renderRagSources(assistantMessageElement, null);
    }

    // 添加消息到聊天界面
,
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
,
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
,
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
,
    scrollToBottom() {
        if (this.chatMessages) {
            this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
        }
    }

    // 处理流式传输完成
,
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
,
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
,
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
,
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
,
    renderRagSourceItem(item, rejected = false) {
        const score = this.formatRetrievalScore(item.score);
        const preview = item.content_preview || item.content || '';
        const citationLabel = item.citation_index ? `证据 ${item.citation_index}` : '';
        return `
            <details class="rag-source-item${rejected ? ' rejected' : ''}">
                <summary>
                    ${citationLabel ? `<span>${this.escapeHtml(citationLabel)}</span>` : ''}
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
,
    formatRetrievalScore(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return 'unknown';
        return numeric.toFixed(4);
    }

    // 显示通知
,
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
,
    handleFileSelect(event) {
        const file = event.target.files[0];
        if (file) {
            // 验证文件格式
            if (!this.validateFileType(file)) {
                this.showNotification(`只支持上传 ${this.formatAllowedFileTypes()} 格式的文件`, 'error');
                this.fileInput.value = '';
                return;
            }
            this.uploadFile(file);
        }
    }

    // 验证文件类型
,
    validateFileType(file) {
        const fileName = file.name.toLowerCase();
        const allowedExtensions = this.uploadConstraints?.allowedExtensions || ['.txt', '.md', '.markdown'];
        return allowedExtensions.some(ext => fileName.endsWith(ext));
    }

,
    async loadUploadConstraints() {
        try {
            const payload = await this.apiGet(`${this.apiBaseUrl}/upload/config`);
            const data = payload.data || {};
            const allowedExtensions = Array.isArray(data.allowed_extensions)
                ? data.allowed_extensions
                    .map((ext) => String(ext || '').trim().toLowerCase())
                    .filter(Boolean)
                    .map((ext) => ext.startsWith('.') ? ext : `.${ext}`)
                : [];
            const maxSize = Number(data.max_file_size);
            const maxSizeMb = Number(data.max_file_size_mb);
            this.uploadConstraints = {
                allowedExtensions: allowedExtensions.length
                    ? allowedExtensions
                    : this.uploadConstraints.allowedExtensions,
                maxSize: Number.isFinite(maxSize) && maxSize > 0
                    ? maxSize
                    : this.uploadConstraints.maxSize,
                maxSizeMb: Number.isFinite(maxSizeMb) && maxSizeMb > 0
                    ? maxSizeMb
                    : this.uploadConstraints.maxSizeMb
            };
        } catch (error) {
            console.warn('读取上传配置失败，使用本地默认值:', error);
        }
    }

,
    formatAllowedFileTypes() {
        const allowedExtensions = this.uploadConstraints?.allowedExtensions || ['.txt', '.md', '.markdown'];
        return allowedExtensions.map((ext) => ext.replace(/^\./, '').toUpperCase()).join('、');
    }

    // 上传文件到知识库
,
    async uploadFile(file) {
        // 再次验证文件类型（双重保险）
        if (!this.validateFileType(file)) {
            this.showNotification(`只支持上传 ${this.formatAllowedFileTypes()} 格式的文件`, 'error');
            return;
        }

        // 验证文件大小（与后端 MAX_FILE_SIZE 保持一致）
        const maxSize = this.uploadConstraints?.maxSize || (10 * 1024 * 1024);
        const maxSizeMb = this.uploadConstraints?.maxSizeMb || Math.ceil(maxSize / 1024 / 1024);
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
                timeoutMs: 0,
                body: formData
            });

            if (!response.ok) {
                throw new Error(`HTTP错误: ${response.status}`);
            }

            const data = await this.readJsonResponse(response);

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
,
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
,
    formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    // 发送智能运维请求（SSE 流式模式）
});
