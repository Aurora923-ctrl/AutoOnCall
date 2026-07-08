// API client and auth-token helpers for the AutoOnCall workbench.
Object.assign(window.AutoOnCallApp.prototype, {
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
        return fetch(path, {
            ...options,
            headers: this.authHeaders(options.headers || {})
        });
    }
});
