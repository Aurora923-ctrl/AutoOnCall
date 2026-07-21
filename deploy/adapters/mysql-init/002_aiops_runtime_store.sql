CREATE TABLE IF NOT EXISTS alert_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    fingerprint VARCHAR(128) NOT NULL UNIQUE,
    incident_id VARCHAR(128) NOT NULL,
    source VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    service_name VARCHAR(128) NOT NULL,
    severity VARCHAR(32) NOT NULL,
    environment VARCHAR(64) NOT NULL,
    starts_at VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_alert_events_incident (incident_id, updated_at),
    INDEX idx_alert_events_status (status, updated_at),
    INDEX idx_alert_events_service (service_name, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trace_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_id VARCHAR(128) NOT NULL UNIQUE,
    trace_id VARCHAR(128) NOT NULL,
    incident_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    node_name VARCHAR(128) NOT NULL,
    step_id VARCHAR(128),
    tool_name VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_trace_events_incident (incident_id, created_at),
    INDEX idx_trace_events_trace (trace_id, created_at),
    INDEX idx_trace_events_type (event_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS approval_requests (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    approval_id VARCHAR(128) NOT NULL UNIQUE,
    incident_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    risk_level VARCHAR(32) NOT NULL,
    action VARCHAR(1000) NOT NULL,
    idempotency_key VARCHAR(128),
    pending_idempotency_key VARCHAR(128)
        GENERATED ALWAYS AS (
            CASE WHEN status = 'pending' THEN idempotency_key ELSE NULL END
    ) STORED,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    decided_at VARCHAR(64),
    payload LONGTEXT NOT NULL,
    UNIQUE KEY uniq_pending_approval_idempotency (pending_idempotency_key),
    INDEX idx_approval_requests_incident (incident_id, created_at),
    INDEX idx_approval_requests_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS change_executions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    change_execution_id VARCHAR(128) NOT NULL UNIQUE,
    change_plan_id VARCHAR(128) NOT NULL,
    approval_id VARCHAR(128) NOT NULL,
    incident_id VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    mode VARCHAR(32) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    UNIQUE KEY uniq_change_executions_scope (incident_id, change_plan_id, approval_id),
    INDEX idx_change_executions_incident (incident_id, created_at),
    INDEX idx_change_executions_plan (change_plan_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS aiops_sessions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL UNIQUE,
    incident_id VARCHAR(128) NOT NULL,
    trace_id VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    node_name VARCHAR(128) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_aiops_sessions_incident (incident_id, updated_at),
    INDEX idx_aiops_sessions_trace (trace_id, updated_at),
    INDEX idx_aiops_sessions_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS a2a_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id VARCHAR(128) NOT NULL UNIQUE,
    message_id VARCHAR(256) NOT NULL,
    request_fingerprint VARCHAR(64) NOT NULL,
    owner_id VARCHAR(128) NOT NULL DEFAULT '',
    skill VARCHAR(128) NOT NULL,
    incident_id VARCHAR(128) NOT NULL,
    state VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_a2a_tasks_message (message_id, updated_at),
    INDEX idx_a2a_tasks_incident (incident_id, updated_at),
    INDEX idx_a2a_tasks_owner (owner_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS incident_states (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    incident_id VARCHAR(128) NOT NULL UNIQUE,
    status VARCHAR(64) NOT NULL,
    service_name VARCHAR(128) NOT NULL,
    severity VARCHAR(32) NOT NULL,
    environment VARCHAR(80) NOT NULL,
    trace_id VARCHAR(128),
    session_id VARCHAR(128),
    approval_status VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_incident_states_status (status, updated_at),
    INDEX idx_incident_states_service (service_name, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS diagnosis_reports (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_id VARCHAR(128) NOT NULL UNIQUE,
    incident_id VARCHAR(128) NOT NULL,
    trace_id VARCHAR(128) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    payload LONGTEXT NOT NULL,
    INDEX idx_diagnosis_reports_incident (incident_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version BIGINT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
ON autooncall.* TO 'autooncall'@'%';

FLUSH PRIVILEGES;
