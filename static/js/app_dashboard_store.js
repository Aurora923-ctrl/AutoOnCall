// Small dashboard-state helpers. Rendering still lives in feature modules.
Object.assign(window.AutoOnCallApp.prototype, {
    setDashboardState(key, value) {
        this.dashboardState[key] = value;
        return value;
    }
,
    setDashboardItems(key, items) {
        return this.setDashboardState(key, Array.isArray(items) ? items : []);
    }
,
    setDashboardError(key, error) {
        return this.setDashboardState(key, error?.message || String(error || ''));
    }
});
