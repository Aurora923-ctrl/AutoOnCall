"""Static frontend smoke tests for the AIOps demo loop."""

from pathlib import Path

import httpx
import pytest

from app.api.aiops import get_aiops_status_catalog, get_demo_incident, list_demo_incidents
from app.config import config
from app.main import app

STATIC_DIR = Path("static")
FRONTEND_JS_DIR = STATIC_DIR / "js"


def read_frontend_scripts() -> str:
    """Return the frontend source bundle used by static contract tests."""
    script_paths = [STATIC_DIR / "app.js", *sorted(FRONTEND_JS_DIR.glob("*.js"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in script_paths)


def test_static_aiops_page_consumes_structured_report_and_incident_links() -> None:
    script = read_frontend_scripts()
    page = Path("static/index.html").read_text(encoding="utf-8")
    style = Path("static/styles.css").read_text(encoding="utf-8")

    assert "collectAIOpsEvent" in script
    assert "parseSseLine" in script
    assert "extractAIOpsStatus" in script
    assert "this.apiBaseUrl = '/api'" in script
    assert "bootstrapAutoOnCallApp" in script
    assert "toggleMobileSidebar" in script
    assert "closeMobileSidebar" in script
    assert 'id="mobileMenuBtn"' in page
    assert 'id="sidebarBackdrop"' in page
    assert "mobile-sidebar-open" in style
    assert "translateX(-105%)" in style
    assert "overscroll-behavior-inline: contain" in style
    assert "document.readyState === 'loading'" in script
    assert "sanitizeRenderedHtml" in script
    assert "value.startsWith('javascript:')" in script
    assert "reduceAIOpsEvent" in script
    assert "parsedLine.done" in script
    assert "trimmed.startsWith('event:')" in script
    assert "trimmed.startsWith('id:')" in script
    assert "trimmed.startsWith('retry:')" in script
    assert "structured_report" in script
    assert "message.diagnosis.status" in script
    assert "structuredReport.status" in script
    assert "status_metadata" in script
    assert "resolveStatusMetadata" in script
    assert "statusMetadataFromCatalog" in script
    assert "loadAIOpsStatusCatalog" in script
    assert "/aiops/status-catalog" in script
    assert "approval_required" in script
    assert "buildAIOpsDetails" in script
    assert "Progress: ${this.formatAIOpsProgressSummary(runState.progress)}" in script
    assert "recoverAIOpsRunFromSnapshot" in script
    assert "eof_without_terminal" in script
    assert "stream_error" in script
    assert "!error.aiopsTerminalEvent" in script
    assert "诊断连接中断，已同步后端状态" in script
    assert "beginLiveAIOpsRun" in script
    assert "renderLiveAIOpsProgress" in script
    assert "extractAIOpsProgress" in script
    assert "formatAIOpsProgressSummary" in script
    assert "progress: () => currentText" in script
    assert "progress_cursor" in script
    assert "progress_events" in script
    assert "progressCursor" in script
    assert "progressEvents" in script
    assert "phase ${phase}" in script
    assert "autooncallAIOpsRun" in script
    assert "loadLastAIOpsRunState" in script
    assert "saveLastAIOpsRunState" in script
    assert "sessionStorage.getItem(this.aiOpsRunStorageKey)" in script
    assert "sessionStorage.setItem(this.aiOpsRunStorageKey" in script
    assert "localStorage.removeItem(this.aiOpsRunStorageKey)" in script
    assert "clearLastAIOpsRunState" in script
    assert "restoreLastAIOpsRun" in script
    assert "refreshAIOpsRunStatus" in script
    assert "refreshAIOpsRuns" in script
    assert "refreshAlerts" in script
    assert "warning_count" in script
    assert "告警 ${warnings}" in script
    assert "renderAlertList" in script
    assert "applyAlertToDiagnosisForm" in script
    assert "buildIncidentFromAlertEvent" in script
    assert "summarizeAlertRawPayload" in script
    assert "/alerts?limit=20" in script
    assert "/incidents?limit=5" in script
    assert "raw_payload" in script
    assert "renderAIOpsRunHistory" in script
    assert "buildAIOpsRunHistoryQuery" in script
    assert "applyAIOpsRunFilters" in script
    assert "clearAIOpsRunFilters" in script
    assert "renderAIOpsRunCompare" in script
    assert "renderAIOpsRunDelta" in script
    assert "openAIOpsRunHistory" in script
    assert "data-aiops-run-id" in script
    assert "service_name" in script
    assert "applyRecoveredAIOpsRunStatus" in script
    assert "/aiops/runs/${encodeURIComponent(sessionId)}" in script
    assert "normalizeAIOpsPlanItems" in script
    assert "normalizeAIOpsLiveStep" in script
    assert "upsertLiveExecutionStep" in script
    assert "markAIOpsPlanProgress" in script
    assert "setResponseAttention" in script
    assert "has-attention" in style
    assert "getSelectedAIOpsIncident" in script
    assert "buildAIOpsIncidentFromForm" in script
    assert "parseRawAlertInput" in script
    assert "applyAIOpsTemplate" in script
    assert "validateAIOpsIncident" in script
    assert "incident: selectedIncident" in script
    assert "incident" in script
    assert 'id="diagnosisForm"' in page
    assert 'id="alertList"' in page
    assert 'id="alertCount"' in page
    assert 'data-panel="alerts"' in page
    assert 'id="aiOpsRunHistoryList"' in page
    assert 'id="aiOpsRunHistoryCount"' in page
    assert 'id="aiOpsRunStatusFilter"' in page
    assert 'value="approval_approved"' in page
    assert 'value="approval_rejected"' in page
    assert 'id="aiOpsRunServiceFilter"' in page
    assert 'id="aiOpsRunCompare"' in page
    assert 'data-panel="run-history"' in page
    assert 'id="aiOpsTitle"' in page
    assert 'id="aiOpsServiceName"' in page
    assert 'id="aiOpsSeverity"' in page
    assert 'id="aiOpsEnvironment"' in page
    assert 'id="aiOpsIncidentId"' in page
    assert 'id="aiOpsSymptom"' in page
    assert 'id="aiOpsRawAlert"' in page
    assert "新建故障诊断" in page
    assert "手动输入" in page
    assert "redis_maxclients" in page
    assert "mysql_slow_query" in page
    assert "pod_crashloop" in page
    assert "forbidden_sql" in page
    assert "/incidents/${encodeURIComponent(incidentId)}/replay" in script
    assert "/api/incidents/${incidentId}/report" in script
    assert "/api/incidents/${incidentId}/changes" in script
    assert "/api/incidents/${runState.incidentId}/trace" in script
    assert "setWorkbenchView" in script
    assert "refreshSelectedIncidentPanels" in script
    assert "submitApprovalDecision" in script
    assert "resumeDiagnosisWorkflow" in script
    assert "startSafeChangeWorkflow" in script
    assert "submitManualChangeResult" in script
    assert "renderChangeStages" in script
    assert "renderIncidentReplay" in script
    assert "renderIncidentPanelsLoading" in script
    assert "renderPanelState" in script
    assert "renderIncidentPath" in script
    assert "renderReplayStageCard" in script
    assert "renderReplayTimelineItems" in script
    assert "renderReplayTimelineControls" in script
    assert "handleReplayFilterChange" in script
    assert "filterReplayTimeline" in script
    assert "renderReplayReplannerDecisions" in script
    assert "renderReplayReplannerDecisionCard" in script
    assert "buildLegacyChangeStages" in script
    assert "execution?.stages" in script
    assert "execution.status_metadata?.label" in script
    assert "parseSseEventsFromText" in script
    assert "approval-comment" in script
    assert "data-approval-reason" in script
    assert "data-diagnosis-resume" in script
    assert "data-change-resume" in script
    assert "data-manual-result" in script
    assert "reason: normalizedReason" in script
    assert "/diagnosis/resume" in script
    assert "approval_id: approvalId" in script
    assert "/changes/${encodeURIComponent(changePlanId)}/resume" in script
    assert "/changes/${encodeURIComponent(changeExecutionId)}/manual-result" in script
    assert "refreshEvalSummary" in script
    assert "/eval/ragas" in script
    assert "/eval/scorecard" in script
    assert "renderInterviewScorecard" in script
    assert "production_status" in script
    assert "ragasSummary" in script
    assert "renderRagasQualityPanel" in script
    assert "RAGAS 答案质量门禁" in script
    assert "RAGAS 门禁无失败用例" in script
    assert "ragas_id_precision" in script
    assert "ragas_relevancy" in script
    assert "eval_ragas_cases.py" in script
    assert "resolveEvalDashboard" in script
    assert "buildLegacyEvalDashboard" in script
    assert "formatEvalMetric" in script
    assert "apiGetWithStatus" in script
    assert "apiFetch(path, options = {})" in script
    assert "sessionStorage.getItem(this.apiTokenStorageKey)" in script
    assert "localStorage.removeItem(this.apiTokenStorageKey)" in script
    assert "readApiToken" in script
    assert "writeApiToken" in script
    assert "X-AutoOnCall-Token" in script
    assert "renderAuthTokenState" in script
    assert "saveApiToken" in script
    assert "clearApiToken" in script
    assert 'id="apiTokenInput"' in page
    assert 'id="apiTokenSaveBtn"' in page
    assert 'id="apiTokenClearBtn"' in page
    assert 'id="authStatusBadge"' in page
    assert script.count("fetch(") == 1
    assert "AUTOONCALL_STATIC_PREFIX" in page
    assert "window.location.protocol === 'file:' ? '.' : '/static'" in page
    assert "autooncallStylesheet" in page
    assert "appScript.src" in page
    assert "cdn.jsdelivr.net" not in page
    assert "renderBasicMarkdown" in script
    assert "/health/live" in script
    assert "/health/ready" in script
    assert "mergeHealthChecks" in script
    assert "readiness_http_status" in script
    assert "liveness_http_status" in script
    assert "health.capabilities" in script
    assert "RAG 能力" in script
    assert "AIOps 能力" in script
    assert "externalSystems.checks" in script
    assert "check.error_type || check.message" in script
    assert "ready=${status} · live=${liveStatus}" in script
    assert "AIOps 用例通过率" in script
    assert "禁止动作识别通过率" in script
    assert "RAG 引用通过率" in script
    assert "评测范围" in script
    assert "复现命令" in script
    assert "失败用例" in script
    assert "renderDiagnosisChain" in script
    assert "renderToolCallTable" in script
    assert "dependency_signals" in script
    assert "extractDependencySignalsFromToolCalls" in script
    assert "formatDependencySignalTitle" in script
    assert "refreshToolContracts" in script
    assert "renderToolContracts" in script
    assert "renderToolContractSummary" in script
    assert "/aiops/tools/contracts" in script
    assert "AIOps 工具能力" in script
    assert "compact-tool-contracts" in script
    assert 'data-panel="tool-contracts"' in page
    assert 'id="toolContractSummary"' in page
    assert 'id="toolContractCount"' in page
    assert "formatToolContractApproval" in script
    assert "renderEvidenceCards" in script
    assert "formatConfidence" in script
    assert "sourcePill" in script
    assert "sourceMetadata" in script
    assert "mcp_monitor" in script
    assert "Mixed" in script
    assert "Mock" in script
    assert "Real" in script
    assert "source-pill.real" in style
    assert "source-pill.mixed" in style
    assert "source-pill.unavailable" in style
    assert "buildRagMetadata" in script
    assert "renderRagSources" in script
    assert "引用来源" in script
    assert "拒答边界" in script
    assert "search_results" in script
    assert "retrieval_results" in script
    assert "no_answer_rejected" in script
    assert "/eval/summary" in script
    assert "/eval/adapter-verification" in script
    assert "payload.available === false" in script
    assert 'data-workbench-view="chat"' in page
    assert 'data-workbench-view="incidents"' in page
    assert 'data-workbench-view="response"' in page
    assert 'data-workbench-view="system"' in page
    assert 'data-workbench-view="aiops"' not in page
    assert 'data-workbench-view="trace"' not in page
    assert 'data-workbench-view="report"' not in page
    assert 'data-workbench-view="approvals"' not in page
    assert 'data-workbench-view="changes"' not in page
    assert 'data-workbench-view="eval"' not in page
    assert 'data-workbench-view="adapters"' not in page
    assert 'data-workbench-view="health"' not in page
    assert "知识问答" in page
    assert 'id="knowledgePanel"' in page
    assert 'id="knowledgeUploadBtn"' in page
    assert 'id="knowledgeStatusBadge"' in page
    assert 'id="knowledgeFileName"' in page
    assert 'id="knowledgeIndexStatus"' in page
    assert 'id="knowledgeChunkCount"' in page
    assert 'id="knowledgeUploadSummary"' in page
    assert "知识库" in page
    assert "上传文档" in page
    assert "故障诊断中心" in page
    assert "告警事件" in page
    assert "处置中心" in page
    assert "环境就绪中心" in page
    assert "服务就绪" in page
    assert "外部适配器" in page
    assert "诊断工具能力" in page
    assert "待办与推进" in page
    assert "执行记录" in page
    assert 'id="incidentTabNav"' in page
    assert 'data-incident-tab="overview"' in page
    assert 'data-incident-tab="process"' in page
    assert 'data-incident-tab="evidence"' in page
    assert 'data-incident-tab="report"' in page
    assert 'data-incident-tab="response"' in page
    assert 'id="incidentReplay"' in page
    assert 'id="replayStatus"' in page
    assert 'data-panel="replay"' in page
    assert "概览" in page
    assert "诊断过程" in page
    assert "诊断回放" in page
    assert "证据与依赖" in page
    assert "处置记录" in page
    assert "normalizeWorkbenchView" in script
    assert "resolveWorkbenchTarget" in script
    assert "aiops: { view: 'incidents', incidentTab: 'process' }" in script
    assert "trace: { view: 'incidents', incidentTab: 'evidence' }" in script
    assert "report: { view: 'incidents', incidentTab: 'report' }" in script
    assert "changes: { view: 'response' }" in script
    assert "health: { view: 'system' }" in script
    assert "setIncidentTab" in script
    assert (
        "overview: ['incidents', 'alerts', 'diagnosis-launch', 'run-history', 'detail', 'conclusion']"
        in script
    )
    assert "process: ['incidents', 'replay', 'plan', 'steps', 'tools']" in script
    assert "evidence: ['incidents', 'dependencies', 'evidence', 'trace']" in script
    assert "report: ['incidents', 'report']" in script
    assert "response: ['incidents', 'approvals', 'changes', 'detail']" in script
    assert "system: ['health', 'adapters', 'tool-contracts', 'eval']" in script
    assert "dataset.workbenchView" in script
    assert "currentIncidentTab" in script
    assert "updateIncidentTabNavState" in script
    assert "renderApprovalStage" in script
    assert "renderApprovalItem" in script
    assert "approvalHasNextAction" in script
    assert "待我审批" in script
    assert "已批准待推进" in script
    assert "审批历史" in script
    assert "待审 ${pendingItems.length} · 推进 ${approvedActionItems.length}" in script
    assert "暂无待审批请求" in script
    assert "暂无已批准待推进事项" in script
    assert "response-next-actions" in script
    assert 'data-change-mode="sandbox"' in script
    assert 'id="planList"' in page
    assert 'id="toolCallTable"' in page
    assert 'id="evidenceList"' in page
    assert 'id="conclusionView"' in page
    assert 'id="traceTimeline"' in page
    assert ".replay-stage-rail" in style
    assert ".replay-body-grid" in style
    assert ".replanner-decision-panel" in style
    assert ".replanner-decision-card" in style
    assert ".replay-eval-metric-list" in style
    assert ".replay-eval-metric" in style
    assert ".panel-state" in style
    assert ".incident-path-card" in style
    assert ".replay-filter-bar" in style
    assert "renderReplayEvaluationMetric" in script
    assert "formatReplayEvaluationValue" in script
    assert "evaluationMetricTone" in script
    assert "formatReplannerDecisionSource" in script
    assert "replannerDecisionSourceTone" in script
    assert "source=${this.escapeHtml(sourceLabel)}" in script
    assert "baseline=${this.escapeHtml(analysisLabel)}" in script
    assert "未通过指标" in script
    assert "没有匹配当前筛选条件的事件" in script
    assert 'id="reportViewer"' in page
    assert 'id="approvalList"' in page
    assert 'id="changeExecutionList"' in page
    assert 'id="aiOpsRunHistoryList"' in page
    assert 'id="aiOpsRunCompare"' in page
    assert 'id="evalSummary"' in page
    assert "alert-list" in style
    assert "alert-raw-summary" in style


def test_static_upload_flow_warns_when_indexing_fails() -> None:
    script = read_frontend_scripts()
    page = Path("static/index.html").read_text(encoding="utf-8")
    style = Path("static/styles.css").read_text(encoding="utf-8")

    assert 'id="knowledgeUploadBtn"' in page
    assert "knowledgeUploadBtn.addEventListener" in script
    assert "loadKnowledgeUploadState" in script
    assert "saveKnowledgeUploadState" in script
    assert "renderKnowledgeUploadState" in script
    assert "renderKnowledgeUploadProgress" in script
    assert "updateKnowledgeUploadResult(data.data)" in script
    assert "renderKnowledgeUploadError(file, error)" in script
    assert "autooncallKnowledgeUpload" in script
    assert "formatKnowledgeIndexStatus" in script
    assert "buildKnowledgeUploadSummary" in script
    assert "knowledge-panel" in style
    assert "knowledge-summary-grid" in style
    assert "data.data.indexing" in script
    assert "向量索引失败" in script
    assert "未生成可检索内容" in script
    assert "分块数量" in script
    assert "索引耗时" in script
    assert "覆盖已有文件" in script
    assert "showNotification(warningMessage, 'warning')" in script
    assert "loadUploadConstraints" in script
    assert "/upload/config" in script
    assert "data.allowed_extensions" in script
    assert "data.max_file_size" in script
    assert "文件大小不能超过${maxSizeMb}MB" in script


@pytest.mark.asyncio
async def test_demo_incident_curl_uses_central_api_base_url(monkeypatch) -> None:
    monkeypatch.setattr(config, "api_base_url", "http://ops.example/autooncall/")

    payload = await get_demo_incident("redis-maxclients")

    assert payload["curl"].startswith(
        "curl -N -X POST http://ops.example/autooncall/api/aiops/demo/"
    )
    assert payload["case_id"] == "redis_maxclients"
    assert "127.0.0.1:9900" not in payload["curl"]


@pytest.mark.asyncio
async def test_demo_incident_catalog_is_frontend_source_of_truth() -> None:
    payload = await list_demo_incidents()
    case_ids = [item["case_id"] for item in payload["items"]]

    assert payload["count"] == 4
    assert case_ids == [
        "redis_maxclients",
        "mysql_slow_query",
        "pod_crashloop",
        "forbidden_sql",
    ]
    assert payload["items"][3]["incident"]["raw_alert"]["requested_action"] == "execute_sql"
    redis_alert = payload["items"][0]["incident"]["raw_alert"]
    assert redis_alert["connected_clients"] == 9940
    assert redis_alert["requested_action"] == "apply_config_change"
    mysql_alert = payload["items"][1]["incident"]["raw_alert"]
    assert mysql_alert["sql_digest"] == "9f3a-pay-report"
    assert mysql_alert["feature_flag"] == "PAYMENT_REPORT_ENABLED=true"
    assert "redis-maxclients" in payload["items"][0]["aliases"]


@pytest.mark.asyncio
async def test_aiops_status_catalog_exposes_backend_lifecycle_metadata() -> None:
    payload = await get_aiops_status_catalog()
    items = {item["status"]: item for item in payload["items"]}

    assert payload["count"] == len(payload["items"])
    assert items["waiting_approval"]["label"] == "等待人工审批"
    assert items["approval_rejected"]["tone"] == "error"
    assert items["change_validated"]["label"] == "变更已校验"


@pytest.mark.asyncio
async def test_static_workbench_assets_are_served_by_fastapi() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        index_response = await client.get("/")
        script_response = await client.get("/static/app.js")
        core_script_response = await client.get("/static/js/app_core.js")
        style_response = await client.get("/static/styles.css")

    assert index_response.status_code == 200
    assert script_response.status_code == 200
    assert core_script_response.status_code == 200
    assert style_response.status_code == 200
    assert "AutoOnCall" in index_response.text
    assert 'id="aiOpsPresetSelect"' in index_response.text
    assert 'id="workbenchPanel"' in index_response.text
    assert 'id="incidentTabNav"' in index_response.text
    assert 'data-incident-tab="overview"' in index_response.text
    assert 'data-workbench-view="response"' in index_response.text
    assert 'data-workbench-view="system"' in index_response.text
    assert 'id="incidentList"' in index_response.text
    assert 'id="alertList"' in index_response.text
    assert 'id="aiOpsRunHistoryList"' in index_response.text
    assert 'id="aiOpsRunCompare"' in index_response.text
    assert 'id="changeExecutionList"' in index_response.text
    assert 'id="evalSummary"' in index_response.text
    assert (
        '<main class="main-content workbench-active" data-workbench-view="incidents">'
        in index_response.text
    )
    assert "AUTOONCALL_SCRIPT_FILES" in script_response.text
    assert "class AutoOnCallApp" in core_script_response.text
    assert "this.currentWorkbenchView = 'incidents'" in core_script_response.text
    assert ".workbench" in style_response.text
