// AutoOnCall frontend script loader.
// Keep this file small; feature code lives under /static/js/.
window.AUTOONCALL_SCRIPT_FILES = [
    'app_core.js',
    'app_api_client.js',
    'app_dashboard_store.js',
    'app_state.js',
    'app_workbench.js',
    'app_replay.js',
    'app_diagnosis_views.js',
    'app_formatters.js',
    'app_chat.js',
    'app_aiops_live.js',
    'app_bootstrap.js'
];

(function loadAutoOnCallScripts() {
    const prefix = window.AUTOONCALL_STATIC_PREFIX || '/static';
    const files = window.AUTOONCALL_SCRIPT_FILES || [];
    const loadState = window.AUTOONCALL_LOAD_STATE = {
        status: 'loading',
        loaded: [],
        failed: ''
    };

    function showLoadFailure(file) {
        loadState.status = 'failed';
        loadState.failed = file;
        document.documentElement.classList.add('autooncall-load-failed');

        const existing = document.getElementById('autooncallLoadError');
        if (existing) return;

        const errorPanel = document.createElement('div');
        errorPanel.id = 'autooncallLoadError';
        errorPanel.setAttribute('role', 'alert');
        errorPanel.style.cssText = [
            'position:fixed',
            'inset:0',
            'z-index:2147483647',
            'display:grid',
            'place-items:center',
            'padding:24px',
            'background:#f8fafc',
            'color:#172033',
            'font:16px/1.5 system-ui,sans-serif'
        ].join(';');

        const message = document.createElement('div');
        message.style.cssText = [
            'max-width:560px',
            'padding:24px',
            'border:1px solid #d7dde8',
            'border-radius:8px',
            'background:#fff',
            'box-shadow:0 12px 36px rgba(23,32,51,.12)'
        ].join(';');

        const title = document.createElement('strong');
        title.textContent = 'AutoOnCall 前端加载失败';
        const detail = document.createElement('p');
        detail.textContent = `脚本 ${file} 未能加载。页面已停止初始化，请刷新后重试。`;
        message.append(title, detail);
        errorPanel.appendChild(message);
        document.body.appendChild(errorPanel);
        if (typeof window.CustomEvent === 'function') {
            window.dispatchEvent(new window.CustomEvent('autooncall:load-error', {
                detail: { file }
            }));
        }
    }

    function loadNext(index) {
        if (index >= files.length) {
            loadState.status = 'loaded';
            if (typeof window.CustomEvent === 'function') {
                window.dispatchEvent(new window.CustomEvent('autooncall:scripts-loaded', {
                    detail: { files: [...loadState.loaded] }
                }));
            }
            return;
        }
        const script = document.createElement('script');
        script.src = `${prefix}/js/${files[index]}`;
        script.onload = () => {
            loadState.loaded.push(files[index]);
            loadNext(index + 1);
        };
        script.onerror = () => {
            console.error(`AutoOnCall frontend script failed to load: ${files[index]}`);
            showLoadFailure(files[index]);
        };
        document.body.appendChild(script);
    }

    loadNext(0);
})();
