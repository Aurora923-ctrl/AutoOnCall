// Shared frontend formatting helpers.
Object.assign(window.AutoOnCallApp.prototype, {
    statusValue(statusOrItem) {
        if (statusOrItem && typeof statusOrItem === 'object') {
            return statusOrItem.status || statusOrItem.status_metadata?.status || 'unknown';
        }
        return statusOrItem || 'unknown';
    }
,
    resolveStatusMetadata(statusOrItem) {
        if (!statusOrItem || typeof statusOrItem !== 'object') {
            return null;
        }
        return statusOrItem.status_metadata || statusOrItem.statusMetadata || null;
    }
,
    statusMetadataFromCatalog(status) {
        const value = status || 'unknown';
        return (this.aiOpsStatusCatalog || []).find((item) => item?.status === value) || null;
    }
,
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
        if (['healthy', 'completed', 'success', 'approved', 'approval_approved', 'approval_resumed', 'closed', 'dry_run_completed', 'sandbox_validated', 'change_validated', 'passed', 'linked', 'manual_execution_recorded', 'available', 'configured'].includes(status)) return 'success';
        if (['waiting_approval', 'pending', 'degraded', 'running', 'waiting', 'empty', 'unknown', 'heuristic', 'not_linked', 'waiting_manual_execution', 'dry_run_running', 'precheck_running', 'sandbox_executing', 'observing'].includes(status)) return 'warning';
        if (['failed', 'error', 'rejected', 'approval_rejected', 'blocked', 'forbidden', 'unhealthy', 'unavailable', 'not_configured', 'precheck_failed', 'dry_run_failed', 'rollback_recommended', 'escalated'].includes(status)) return 'error';
        return '';
    }
,
    riskTone(riskLevel) {
        if (riskLevel === 'low') return 'success';
        if (riskLevel === 'medium') return 'warning';
        if (riskLevel === 'high') return 'error';
        return '';
    }
,
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
,
    sourceClassName(source) {
        const normalized = String(source || 'unknown')
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, '-')
            .replace(/^-+|-+$/g, '');
        return normalized ? `source-${normalized}` : 'source-unknown';
    }
,
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
,
    formatPercent(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return `${Math.round(numeric * 1000) / 10}%`;
    }
,
    formatNumber(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return String(Math.round(numeric * 100) / 100);
    }
,
    formatInteger(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return String(Math.round(numeric));
    }
,
    formatConfidence(value) {
        const numeric = typeof value === 'number' ? value : Number(value);
        if (!Number.isFinite(numeric)) return '-';
        return `${Math.round(numeric * 100)}%`;
    }
,
    formatDateTime(value) {
        if (!value) return '-';
        try {
            return new Date(value).toLocaleString();
        } catch {
            return value;
        }
    }

    // 生成随机会话ID
});
