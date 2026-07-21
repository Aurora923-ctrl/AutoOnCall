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
    if (window.autoOnCallApp) {
        return window.autoOnCallApp;
    }
    window.autoOnCallApp = new AutoOnCallApp();
    window.dispatchEvent(new CustomEvent('autooncall:ready', {
        detail: { app: window.autoOnCallApp }
    }));
    return window.autoOnCallApp;
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapAutoOnCallApp, { once: true });
} else {
    bootstrapAutoOnCallApp();
}
