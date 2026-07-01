CREATE TABLE IF NOT EXISTS incident_demo_cases (
    case_id VARCHAR(64) PRIMARY KEY,
    service_name VARCHAR(128) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    symptom VARCHAR(255) NOT NULL,
    expected_root_cause VARCHAR(255) NOT NULL,
    expected_tools JSON NOT NULL,
    evidence_summary JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS incident_change_events (
    change_id VARCHAR(64) PRIMARY KEY,
    service_name VARCHAR(128) NOT NULL,
    version VARCHAR(64) NOT NULL,
    operator VARCHAR(64) NOT NULL,
    deployed_at TIMESTAMP NOT NULL,
    risk_level VARCHAR(16) NOT NULL,
    rollback_plan VARCHAR(255) NOT NULL,
    correlation_hint VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS slo_snapshots (
    snapshot_id VARCHAR(64) PRIMARY KEY,
    service_name VARCHAR(128) NOT NULL,
    window_minutes INT NOT NULL,
    availability DECIMAL(8, 5) NOT NULL,
    p95_latency_ms INT NOT NULL,
    error_rate DECIMAL(8, 5) NOT NULL,
    burn_rate DECIMAL(8, 3) NOT NULL,
    captured_at TIMESTAMP NOT NULL
);

INSERT INTO incident_demo_cases (
    case_id,
    service_name,
    severity,
    symptom,
    expected_root_cause,
    expected_tools,
    evidence_summary,
    status
)
VALUES
(
    'INC-REDIS-001',
    'order-service',
    'P1',
    '5xx spike and Redis connection timeout',
    'Redis connected_clients reached maxclients and application connection pool started timing out',
    JSON_ARRAY('query_metrics', 'query_logs', 'query_redis_status', 'search_runbook', 'search_history_ticket'),
    JSON_OBJECT(
        'metrics', 'P95 latency > 3000ms and 5xx rate > 8%',
        'logs', 'Redis connection timeout appears in order-service logs',
        'redis', 'connected_clients/maxclients ratio is above 0.98',
        'approval', 'Increasing maxclients or restarting service requires human approval'
    ),
    'ready_for_demo'
),
(
    'INC-MYSQL-001',
    'payment-service',
    'P2',
    'Checkout latency and MySQL slow query',
    'MySQL slow query increased connection pool wait time',
    JSON_ARRAY('query_metrics', 'query_logs', 'query_mysql_status', 'search_runbook'),
    JSON_OBJECT(
        'metrics', 'P95 latency over 2200ms',
        'logs', 'Slow query and pool waiting signals',
        'mysql', 'Slow_queries increased and Threads_connected is elevated'
    ),
    'ready_for_demo'
),
(
    'INC-K8S-001',
    'inventory-service',
    'P1',
    'Pod CrashLoopBackOff and restart count increasing',
    'Kubernetes pod crash loop reduced serving capacity',
    JSON_ARRAY('query_k8s_status', 'query_logs', 'query_metrics', 'search_runbook'),
    JSON_OBJECT(
        'k8s', 'CrashLoopBackOff with recent restart count spike',
        'logs', 'Startup failed after config reload',
        'risk', 'Delete pod is forbidden for the agent'
    ),
    'ready_for_demo'
)
ON DUPLICATE KEY UPDATE
    symptom = VALUES(symptom),
    expected_root_cause = VALUES(expected_root_cause),
    expected_tools = VALUES(expected_tools),
    evidence_summary = VALUES(evidence_summary),
    status = VALUES(status);

INSERT INTO incident_change_events (
    change_id,
    service_name,
    version,
    operator,
    deployed_at,
    risk_level,
    rollback_plan,
    correlation_hint
)
VALUES
(
    'CHG-10086',
    'order-service',
    '2026.06.27-1024',
    'release-bot',
    '2026-06-27 06:30:00',
    'medium',
    'Rollback to 2026.06.26-1810 through Argo CD after approval',
    'Redis timeout started 18 minutes after deployment; verify connection pool config diff'
),
(
    'CHG-10087',
    'payment-service',
    '2026.06.27-0910',
    'release-bot',
    '2026-06-27 04:10:00',
    'low',
    'Rollback to 2026.06.26-1712',
    'Slow query started after report query feature flag opened'
),
(
    'CHG-10088',
    'inventory-service',
    '2026.06.27-0730',
    'release-bot',
    '2026-06-27 03:30:00',
    'high',
    'Rollback image and restore previous ConfigMap',
    'CrashLoopBackOff follows ConfigMap refresh'
)
ON DUPLICATE KEY UPDATE
    version = VALUES(version),
    risk_level = VALUES(risk_level),
    rollback_plan = VALUES(rollback_plan),
    correlation_hint = VALUES(correlation_hint);

INSERT INTO slo_snapshots (
    snapshot_id,
    service_name,
    window_minutes,
    availability,
    p95_latency_ms,
    error_rate,
    burn_rate,
    captured_at
)
VALUES
('SLO-ORDER-001', 'order-service', 30, 0.94210, 3860, 0.08320, 14.500, '2026-06-27 06:48:00'),
('SLO-PAY-001', 'payment-service', 30, 0.98100, 2260, 0.02100, 4.200, '2026-06-27 04:28:00'),
('SLO-INV-001', 'inventory-service', 30, 0.95500, 1810, 0.05200, 9.700, '2026-06-27 03:48:00')
ON DUPLICATE KEY UPDATE
    availability = VALUES(availability),
    p95_latency_ms = VALUES(p95_latency_ms),
    error_rate = VALUES(error_rate),
    burn_rate = VALUES(burn_rate);

INSERT INTO aiops_dependency_snapshots (
    snapshot_id,
    service_name,
    dependency_type,
    dependency_name,
    symptom,
    observed_value,
    severity,
    source
)
VALUES
('DEP-REDIS-001', 'order-service', 'redis', 'redis-cluster-prod', 'connected_clients near maxclients', '9827/10000', 'P1', 'demo-seed'),
('DEP-MYSQL-001', 'payment-service', 'mysql', 'order-mysql', 'slow query and pool wait', 'Slow_queries=432', 'P2', 'demo-seed'),
('DEP-KAFKA-001', 'checkout-service', 'kafka', 'redpanda-orders', 'consumer lag increasing', 'lag=12842', 'P2', 'demo-seed')
ON DUPLICATE KEY UPDATE
    observed_value = VALUES(observed_value),
    severity = VALUES(severity),
    source = VALUES(source);

INSERT INTO aiops_remediation_audit (
    audit_id,
    incident_key,
    action_type,
    approval_required,
    decision_boundary,
    source
)
VALUES
('AUD-REDIS-001', 'INC-REDIS-001', 'increase_redis_maxclients', 1, 'Agent may suggest, human must approve and execute config change', 'demo-seed'),
('AUD-REDIS-002', 'INC-REDIS-001', 'restart_order_service', 1, 'Production restart requires approval and rollback window', 'demo-seed'),
('AUD-K8S-001', 'INC-K8S-001', 'delete_pod', 1, 'Forbidden for autonomous agent; route to human change process', 'demo-seed')
ON DUPLICATE KEY UPDATE
    approval_required = VALUES(approval_required),
    decision_boundary = VALUES(decision_boundary),
    source = VALUES(source);

INSERT INTO orders (user_id, status, amount, created_at)
VALUES
(20001, 'FAILED', 1299.00, '2026-06-27 06:43:00'),
(20002, 'FAILED', 859.00, '2026-06-27 06:44:00'),
(20003, 'TIMEOUT', 499.00, '2026-06-27 06:45:00')
ON DUPLICATE KEY UPDATE status = VALUES(status);

INSERT INTO payment_events (order_id, event_type, payload, created_at)
VALUES
(1, 'redis_timeout', JSON_OBJECT('pool_wait_ms', 1200, 'exception', 'RedisConnectionTimeout'), '2026-06-27 06:43:11'),
(2, 'mysql_slow_query', JSON_OBJECT('query_ms', 4200, 'sql_hash', '9f3a-pay-report'), '2026-06-27 04:23:12'),
(3, 'consumer_lag_detected', JSON_OBJECT('topic', 'order-service.events', 'lag', 12842), '2026-06-27 05:18:19');
