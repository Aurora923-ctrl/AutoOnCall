// API client and auth-token helpers for the AutoOnCall workbench.
Object.assign(window.AutoOnCallApp.prototype, {
    async apiGet(path) {
        const response = await this.apiFetch(path);
        return this.readJsonResponse(response);
    }
,
    async apiGetWithStatus(path, options = {}) {
        const response = await this.apiFetch(path, options);
        const data = await this.readResponsePayload(response);
        return {
            ok: response.ok,
            status: response.status,
            data
        };
    }
,
    authHeaders(headers = {}) {
        const normalizedHeaders = { ...headers };
        const token = this.readApiToken();
        if (token) {
            normalizedHeaders['X-AutoOnCall-Token'] = token;
        }
        return normalizedHeaders;
    }
,
    readApiToken() {
        try {
            const sessionToken = (sessionStorage.getItem(this.apiTokenStorageKey) || '').trim();
            if (sessionToken) return sessionToken;
            const legacyToken = (localStorage.getItem(this.apiTokenStorageKey) || '').trim();
            if (legacyToken) {
                sessionStorage.setItem(this.apiTokenStorageKey, legacyToken);
                localStorage.removeItem(this.apiTokenStorageKey);
                return legacyToken;
            }
        } catch (error) {
            console.warn('读取接口令牌失败:', error);
        }
        return '';
    }
,
    writeApiToken(token) {
        try {
            sessionStorage.setItem(this.apiTokenStorageKey, token);
            localStorage.removeItem(this.apiTokenStorageKey);
        } catch (error) {
            console.warn('保存接口令牌失败:', error);
        }
    }
,
    clearApiTokenStorage() {
        try {
            sessionStorage.removeItem(this.apiTokenStorageKey);
            localStorage.removeItem(this.apiTokenStorageKey);
        } catch (error) {
            console.warn('清除接口令牌失败:', error);
        }
    }
,
    async apiFetch(path, options = {}) {
        const {
            timeoutMs = 30000,
            requestKey = '',
            signal: externalSignal,
            ...fetchOptions
        } = options;
        const controller = new AbortController();
        const previousController = requestKey ? this.requestControllers.get(requestKey) : null;
        if (previousController) {
            previousController.abort();
        }
        if (requestKey) {
            this.requestControllers.set(requestKey, controller);
        }

        let timeoutId = null;
        let timedOut = false;
        const abortFromExternalSignal = () => controller.abort(externalSignal?.reason);
        if (externalSignal) {
            if (externalSignal.aborted) {
                abortFromExternalSignal();
            } else {
                externalSignal.addEventListener('abort', abortFromExternalSignal, { once: true });
            }
        }
        if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
            timeoutId = setTimeout(() => {
                timedOut = true;
                controller.abort();
            }, timeoutMs);
        }

        try {
            const response = await fetch(path, {
                ...fetchOptions,
                headers: this.authHeaders(fetchOptions.headers || {}),
                signal: controller.signal
            });
            if ([401, 403].includes(response.status)) {
                this.renderAuthFailure(response.status);
            }
            return response;
        } catch (error) {
            if (error?.name === 'AbortError') {
                const message = timedOut ? '请求超时，请稍后重试' : '请求已取消';
                const abortError = new Error(message);
                abortError.name = 'AbortError';
                abortError.timedOut = timedOut;
                throw abortError;
            }
            throw new Error('网络连接失败，请检查服务状态后重试', { cause: error });
        } finally {
            if (timeoutId) clearTimeout(timeoutId);
            if (externalSignal) {
                externalSignal.removeEventListener('abort', abortFromExternalSignal);
            }
            if (requestKey && this.requestControllers.get(requestKey) === controller) {
                this.requestControllers.delete(requestKey);
            }
        }
    }
    ,
    async readResponsePayload(response) {
        if (response.status === 204) return null;
        const text = await response.text();
        if (!text.trim()) return null;
        const contentType = (response.headers.get('content-type') || '').toLowerCase();
        if (contentType.includes('json')) {
            try {
                return JSON.parse(text);
            } catch {
                if (response.ok) {
                    throw new Error('服务返回了无效的 JSON 响应');
                }
            }
        }
        return text;
    }
    ,
    async readJsonResponse(response) {
        const payload = await this.readResponsePayload(response);
        if (!response.ok) {
            throw this.buildApiError(response, payload);
        }
        if (payload === null) return null;
        if (typeof payload === 'string') {
            throw new Error('服务返回了非 JSON 响应');
        }
        return payload;
    }
    ,
    buildApiError(response, payload) {
        const statusMessages = {
            401: '认证失败，请检查接口令牌',
            403: '当前令牌没有执行此操作的权限',
            409: '操作状态已变化，请刷新后重试',
            422: '请求字段校验失败',
            500: '服务内部错误，请稍后重试'
        };
        const detail = typeof payload === 'string'
            ? payload.trim()
            : payload?.detail || payload?.message || payload?.error?.message || '';
        const error = new Error(detail || statusMessages[response.status] || `HTTP 错误: ${response.status}`);
        error.status = response.status;
        error.payload = payload;
        return error;
    }
    ,
    renderAuthFailure(status) {
        if (this.authStatusBadge) {
            this.authStatusBadge.textContent = status === 401 ? '令牌无效' : '权限不足';
            this.authStatusBadge.className = 'error';
        }
    }
    ,
    cancelRequest(requestKey) {
        const controller = this.requestControllers.get(requestKey);
        if (controller) controller.abort();
    }
    ,
    async runExclusiveAction(actionKey, action, buttons = []) {
        if (!actionKey || this.pendingActions.has(actionKey)) return false;
        this.pendingActions.add(actionKey);
        const elements = buttons.filter(Boolean);
        elements.forEach((button) => {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        });
        try {
            await action();
            return true;
        } finally {
            this.pendingActions.delete(actionKey);
            elements.forEach((button) => {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            });
        }
    }
    ,
    createSseParser(onEvent) {
        let buffer = '';
        let eventName = '';
        let eventId = '';
        let dataLines = [];

        const dispatch = () => {
            if (dataLines.length === 0) {
                eventName = '';
                return false;
            }
            const data = dataLines.join('\n');
            const event = { event: eventName || 'message', id: eventId, data };
            dataLines = [];
            eventName = '';
            return onEvent(event) === true;
        };

        const processLine = (rawLine) => {
            const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
            if (line === '') return dispatch();
            if (line.startsWith(':')) return false;
            const separator = line.indexOf(':');
            const field = separator >= 0 ? line.slice(0, separator) : line;
            let value = separator >= 0 ? line.slice(separator + 1) : '';
            if (value.startsWith(' ')) value = value.slice(1);
            if (field === 'data') dataLines.push(value);
            if (field === 'event') eventName = value;
            if (field === 'id' && !value.includes('\0')) eventId = value;
            return false;
        };

        return {
            push(chunk) {
                buffer += chunk;
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (processLine(line)) return true;
                }
                return false;
            },
            finish() {
                if (buffer) processLine(buffer);
                buffer = '';
                return dispatch();
            }
        };
    }
});
