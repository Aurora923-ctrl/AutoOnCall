// AutoOnCall frontend script loader.
// Keep this file small; feature code lives under /static/js/.
window.AUTOONCALL_SCRIPT_FILES = [
    'app_core.js',
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

    function loadNext(index) {
        if (index >= files.length) return;
        const script = document.createElement('script');
        script.src = `${prefix}/js/${files[index]}`;
        script.onload = () => loadNext(index + 1);
        script.onerror = () => {
            console.error(`AutoOnCall frontend script failed to load: ${files[index]}`);
        };
        document.body.appendChild(script);
    }

    loadNext(0);
})();
