CREATE TABLE IF NOT EXISTS orders (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_user_id (user_id),
    KEY idx_status (status)
);

CREATE TABLE IF NOT EXISTS payment_events (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id BIGINT NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload JSON NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_order_id (order_id)
);

CREATE TABLE IF NOT EXISTS inventory_reservations (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    sku_id BIGINT NOT NULL,
    order_id BIGINT NOT NULL,
    reserved_qty INT NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_sku_status (sku_id, status)
);

CREATE TABLE IF NOT EXISTS incident_business_snapshots (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_id VARCHAR(64) NOT NULL,
    service_name VARCHAR(128) NOT NULL,
    endpoint VARCHAR(128) NOT NULL,
    business_metric VARCHAR(64) NOT NULL,
    metric_value DECIMAL(12, 4) NOT NULL,
    observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_incident_service (incident_id, service_name)
);

CREATE TABLE IF NOT EXISTS aiops_service_catalog (
    service_name VARCHAR(128) PRIMARY KEY,
    owner VARCHAR(128) NOT NULL,
    tier VARCHAR(32) NOT NULL,
    namespace VARCHAR(128) NOT NULL,
    business_domain VARCHAR(128) NOT NULL,
    payload JSON NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS aiops_deploy_history (
    service_name VARCHAR(128) PRIMARY KEY,
    current_version VARCHAR(64) NOT NULL,
    payload JSON NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS aiops_history_tickets (
    ticket_id VARCHAR(128) PRIMARY KEY,
    service_name VARCHAR(128) NOT NULL,
    title VARCHAR(255) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    root_cause TEXT,
    resolution TEXT,
    customer_impact TEXT,
    labels_text VARCHAR(512),
    payload JSON NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_ticket_service_updated (service_name, updated_at),
    FULLTEXT KEY ft_ticket_text (title, root_cause, resolution, customer_impact, labels_text)
);

CREATE TABLE IF NOT EXISTS aiops_incident_evidence (
    incident_key VARCHAR(128) PRIMARY KEY,
    service_name VARCHAR(128) NOT NULL,
    dependency_type VARCHAR(32) NOT NULL,
    dependency_name VARCHAR(128) NOT NULL,
    symptom VARCHAR(255) NOT NULL,
    observed_value VARCHAR(255) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    expected_root_cause VARCHAR(255) NOT NULL,
    evidence_summary JSON NOT NULL,
    source VARCHAR(64) NOT NULL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_incident_evidence_service_dependency (service_name, dependency_type, observed_at)
);

CREATE TABLE IF NOT EXISTS aiops_remediation_audit (
    audit_id VARCHAR(128) PRIMARY KEY,
    incident_key VARCHAR(128) NOT NULL,
    action_type VARCHAR(128) NOT NULL,
    approval_required BOOLEAN NOT NULL,
    decision_boundary VARCHAR(255) NOT NULL,
    source VARCHAR(64) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_remediation_incident_created (incident_key, created_at)
);

INSERT INTO orders (id, user_id, status, amount)
VALUES
    (1, 10001, 'CREATED', 89.90),
    (2, 10002, 'PAID', 199.00),
    (3, 10003, 'FAILED', 39.90),
    (4, 10004, 'PAID', 459.00)
ON DUPLICATE KEY UPDATE
    user_id = VALUES(user_id),
    status = VALUES(status),
    amount = VALUES(amount);

INSERT INTO payment_events (id, order_id, event_type, payload)
VALUES
    (1, 1, 'payment_requested', JSON_OBJECT('channel', 'card')),
    (2, 2, 'payment_succeeded', JSON_OBJECT('channel', 'wallet')),
    (3, 3, 'payment_failed', JSON_OBJECT('reason', 'timeout')),
    (4, 2, 'mysql_slow_query', JSON_OBJECT('query_ms', 920, 'sql_hash', '9f3a-pay-report'))
ON DUPLICATE KEY UPDATE
    order_id = VALUES(order_id),
    event_type = VALUES(event_type),
    payload = VALUES(payload);

INSERT INTO inventory_reservations (id, sku_id, order_id, reserved_qty, status)
VALUES
    (1, 30001, 1, 1, 'RESERVED'),
    (2, 30002, 2, 2, 'CONFIRMED'),
    (3, 30003, 3, 1, 'RELEASE_PENDING')
ON DUPLICATE KEY UPDATE
    sku_id = VALUES(sku_id),
    order_id = VALUES(order_id),
    reserved_qty = VALUES(reserved_qty),
    status = VALUES(status);

INSERT INTO incident_business_snapshots (
    id,
    incident_id,
    service_name,
    endpoint,
    business_metric,
    metric_value
)
VALUES
    (1, 'INC-REDIS-001', 'order-service', 'POST /api/orders', 'checkout_success_rate', 0.9180),
    (2, 'INC-REDIS-001', 'order-service', 'POST /api/orders', 'order_create_p95_ms', 3250.0000),
    (3, 'INC-MYSQL-001', 'payment-service', 'POST /api/payments', 'payment_pending_orders', 420.0000),
    (4, 'INC-K8S-001', 'inventory-service', 'POST /api/inventory/reservations', 'reservation_backlog', 2380.0000)
ON DUPLICATE KEY UPDATE
    incident_id = VALUES(incident_id),
    service_name = VALUES(service_name),
    endpoint = VALUES(endpoint),
    business_metric = VALUES(business_metric),
    metric_value = VALUES(metric_value);

INSERT INTO aiops_incident_evidence (
    incident_key,
    service_name,
    dependency_type,
    dependency_name,
    symptom,
    observed_value,
    severity,
    expected_root_cause,
    evidence_summary,
    source,
    observed_at
)
VALUES
(
    'INC-REDIS-001',
    'order-service',
    'redis',
    'redis-cluster-prod',
    'Redis connection timeout with 5xx increase',
    'connected_clients=9940,maxclients=10000,blocked_clients=37',
    'P1',
    'Redis connected_clients reached maxclients and application connection pool timed out',
    JSON_OBJECT(
        'metrics', 'P95 latency > 3000ms and 5xx rate > 8%',
        'logs', 'Redis connection timeout appears in order-service logs',
        'redis', 'connected_clients/maxclients ratio is above 0.99',
        'approval', 'Increasing maxclients or restarting service requires human approval'
    ),
    'live-mysql-seed',
    '2026-07-06 10:00:00'
),
(
    'INC-MYSQL-001',
    'payment-service',
    'mysql',
    'payment-mysql',
    'MySQL slow query and connection pool waiting',
    'slow_queries=18,pool_waiting=6,active_connections=188/200',
    'P2',
    'MySQL slow query increased connection pool wait time',
    JSON_OBJECT(
        'metrics', 'P95 latency over 2200ms',
        'logs', 'Slow query and pool waiting signals',
        'mysql', 'Slow_queries increased and Threads_connected is elevated',
        'approval', 'SQL, index, and config changes require human approval'
    ),
    'live-mysql-seed',
    '2026-07-06 10:05:00'
)
ON DUPLICATE KEY UPDATE
    service_name = VALUES(service_name),
    dependency_type = VALUES(dependency_type),
    dependency_name = VALUES(dependency_name),
    symptom = VALUES(symptom),
    observed_value = VALUES(observed_value),
    severity = VALUES(severity),
    expected_root_cause = VALUES(expected_root_cause),
    evidence_summary = VALUES(evidence_summary),
    source = VALUES(source),
    observed_at = VALUES(observed_at);

INSERT INTO aiops_remediation_audit (
    audit_id,
    incident_key,
    action_type,
    approval_required,
    decision_boundary,
    source
)
VALUES
(
    'AUD-REDIS-001',
    'INC-REDIS-001',
    'increase_redis_maxclients',
    TRUE,
    'Agent may suggest, human must approve and execute Redis config change',
    'live-mysql-seed'
),
(
    'AUD-MYSQL-001',
    'INC-MYSQL-001',
    'add_mysql_index_or_disable_report',
    TRUE,
    'Agent may suggest, DBA/application owner must approve SQL, index, or config changes',
    'live-mysql-seed'
)
ON DUPLICATE KEY UPDATE
    action_type = VALUES(action_type),
    approval_required = VALUES(approval_required),
    decision_boundary = VALUES(decision_boundary),
    source = VALUES(source);

INSERT INTO aiops_service_catalog (
    service_name,
    owner,
    tier,
    namespace,
    business_domain,
    payload
)
VALUES
(
    'order-service',
    'payments-oncall',
    'critical',
    'orders',
    'checkout',
    JSON_OBJECT(
        'service_name', 'order-service',
        'owner', 'payments-oncall',
        'tier', 'critical',
        'environment', 'prod',
        'namespace', 'orders',
        'runtime', 'kubernetes',
        'business_context', JSON_OBJECT(
            'domain', 'checkout',
            'description', 'Creates customer orders and coordinates payment and inventory reservation.',
            'critical_user_journey', 'submit cart -> create order -> reserve inventory -> request payment',
            'peak_window', '20:00-23:00 Asia/Shanghai',
            'impact_when_degraded', 'Customers cannot submit new orders; checkout conversion drops immediately.'
        ),
        'slo', JSON_OBJECT('availability', '99.95%', 'p95_latency_ms', 900, 'error_rate', '1%'),
        'traffic_profile', JSON_OBJECT('normal_qps', 900, 'peak_qps', 1500),
        'critical_endpoints', JSON_ARRAY(
            JSON_OBJECT(
                'path', 'POST /api/orders',
                'business_action', 'create order',
                'slo_p95_ms', 900,
                'downstream', JSON_ARRAY('redis-order-cache', 'mysql-orders', 'inventory-service')
            ),
            JSON_OBJECT(
                'path', 'GET /api/orders/{id}',
                'business_action', 'query order detail',
                'slo_p95_ms', 300,
                'downstream', JSON_ARRAY('redis-order-cache', 'mysql-orders')
            )
        ),
        'dependencies', JSON_ARRAY(
            JSON_OBJECT(
                'name', 'redis-order-cache',
                'type', 'redis',
                'endpoint', 'redis:6379',
                'role', 'order idempotency, cart snapshot and hot order cache',
                'failure_mode', 'connection timeout raises 503 on order creation'
            ),
            JSON_OBJECT(
                'name', 'mysql-orders',
                'type', 'mysql',
                'endpoint', 'mysql:3306',
                'role', 'order transaction store',
                'failure_mode', 'slow insert or connection wait increases checkout latency'
            )
        ),
        'runbooks', JSON_ARRAY('docs/knowledge-base/slow_response.md', 'docs/knowledge-base/service_unavailable.md')
    )
),
(
    'payment-service',
    'payments-oncall',
    'critical',
    'payments',
    'payment',
    JSON_OBJECT(
        'service_name', 'payment-service',
        'owner', 'payments-oncall',
        'tier', 'critical',
        'environment', 'prod',
        'namespace', 'payments',
        'runtime', 'kubernetes',
        'business_context', JSON_OBJECT(
            'domain', 'payment',
            'description', 'Authorizes and records customer payment attempts for orders.',
            'critical_user_journey', 'create payment -> call channel -> write payment event -> notify order',
            'peak_window', '20:00-23:00 Asia/Shanghai',
            'impact_when_degraded', 'Orders stay in pending payment and customers may retry payment.'
        ),
        'dependencies', JSON_ARRAY(
            JSON_OBJECT(
                'name', 'mysql-payments',
                'type', 'mysql',
                'endpoint', 'mysql:3306',
                'role', 'payment events and channel callback ledger',
                'failure_mode', 'slow reports contend with payment writes and raise pool wait'
            ),
            JSON_OBJECT(
                'name', 'redis-payment-cache',
                'type', 'redis',
                'endpoint', 'redis:6379',
                'role', 'payment idempotency token cache',
                'failure_mode', 'cache timeout may duplicate channel status polling'
            )
        ),
        'critical_endpoints', JSON_ARRAY(
            JSON_OBJECT(
                'path', 'POST /api/payments',
                'business_action', 'request payment',
                'slo_p95_ms', 800,
                'downstream', JSON_ARRAY('mysql-payments', 'redis-payment-cache')
            ),
            JSON_OBJECT(
                'path', 'POST /api/payments/callback',
                'business_action', 'receive channel callback',
                'slo_p95_ms', 500,
                'downstream', JSON_ARRAY('mysql-payments')
            )
        ),
        'slo', JSON_OBJECT('availability', '99.9%', 'p95_latency_ms', 800, 'error_rate', '1%'),
        'runbooks', JSON_ARRAY('docs/knowledge-base/slow_response.md', 'docs/knowledge-base/service_unavailable.md')
    )
),
(
    'inventory-service',
    'supply-chain-oncall',
    'critical',
    'inventory',
    'inventory',
    JSON_OBJECT(
        'service_name', 'inventory-service',
        'owner', 'supply-chain-oncall',
        'tier', 'critical',
        'environment', 'prod',
        'namespace', 'inventory',
        'runtime', 'kubernetes',
        'business_context', JSON_OBJECT(
            'domain', 'inventory',
            'description', 'Reserves stock during checkout and releases stock when payment fails.',
            'critical_user_journey', 'order created -> reserve stock -> confirm or release stock',
            'peak_window', '20:00-23:00 Asia/Shanghai',
            'impact_when_degraded', 'New orders cannot reserve stock; order-service may return 503.'
        ),
        'dependencies', JSON_ARRAY(
            JSON_OBJECT(
                'name', 'mysql-inventory',
                'type', 'mysql',
                'endpoint', 'mysql:3306',
                'role', 'sku stock and reservation ledger',
                'failure_mode', 'connection errors block stock reservation'
            )
        ),
        'critical_endpoints', JSON_ARRAY(
            JSON_OBJECT(
                'path', 'POST /api/inventory/reservations',
                'business_action', 'reserve stock',
                'slo_p95_ms', 600,
                'downstream', JSON_ARRAY('mysql-inventory')
            ),
            JSON_OBJECT(
                'path', 'POST /api/inventory/releases',
                'business_action', 'release stock',
                'slo_p95_ms', 600,
                'downstream', JSON_ARRAY('mysql-inventory')
            )
        ),
        'slo', JSON_OBJECT('availability', '99.95%', 'p95_latency_ms', 600, 'error_rate', '1%'),
        'runbooks', JSON_ARRAY('docs/knowledge-base/service_unavailable.md', 'docs/knowledge-base/memory_high_usage.md')
    )
)
ON DUPLICATE KEY UPDATE
    owner = VALUES(owner),
    tier = VALUES(tier),
    namespace = VALUES(namespace),
    business_domain = VALUES(business_domain),
    payload = VALUES(payload);

INSERT INTO aiops_deploy_history (service_name, current_version, payload)
VALUES
(
    'order-service',
    '2026.06.27-1024',
    JSON_OBJECT(
        'service_name', 'order-service',
        'current_version', '2026.06.27-1024',
        'recent_deployments', JSON_ARRAY(
            JSON_OBJECT(
                'version', '2026.06.27-1024',
                'deployed_at', '2026-06-27T06:30:00Z',
                'operator', 'release-bot',
                'status', 'succeeded',
                'change_id', 'CHG-10086',
                'risk', 'medium',
                'summary', 'Reduced Redis connection acquire timeout and changed checkout retry policy.',
                'business_reason', 'Protect checkout throughput during peak traffic by failing fast on cache wait.',
                'related_config', JSON_ARRAY('ORDER_REDIS_POOL_MAX=280', 'ORDER_REDIS_ACQUIRE_TIMEOUT_MS=120'),
                'verification', JSON_ARRAY('canary 10%', 'order creation smoke test', 'Redis timeout dashboard')
            ),
            JSON_OBJECT(
                'version', '2026.06.26-1810',
                'deployed_at', '2026-06-26T10:10:00Z',
                'operator', 'release-bot',
                'status', 'succeeded',
                'change_id', 'CHG-10021',
                'risk', 'low',
                'summary', 'Added order detail cache metric labels.',
                'business_reason', 'Improve checkout observability without changing request behavior.',
                'verification', JSON_ARRAY('metric label cardinality check', 'dashboard refresh')
            )
        )
    )
),
(
    'payment-service',
    '2026.06.27-0910',
    JSON_OBJECT(
        'service_name', 'payment-service',
        'current_version', '2026.06.27-0910',
        'recent_deployments', JSON_ARRAY(
            JSON_OBJECT(
                'version', '2026.06.27-0910',
                'deployed_at', '2026-06-27T04:10:00Z',
                'operator', 'release-bot',
                'status', 'succeeded',
                'change_id', 'CHG-10087',
                'risk', 'medium',
                'summary', 'Enabled payment reconciliation report with a new date-range query.',
                'business_reason', 'Expose same-day channel mismatch report to payment operations.',
                'related_config', JSON_ARRAY('PAYMENT_REPORT_ENABLED=true'),
                'verification', JSON_ARRAY('report smoke test', 'payment callback regression', 'slow query dashboard')
            )
        )
    )
),
(
    'inventory-service',
    '2026.06.27-0730',
    JSON_OBJECT(
        'service_name', 'inventory-service',
        'current_version', '2026.06.27-0730',
        'recent_deployments', JSON_ARRAY(
            JSON_OBJECT(
                'version', '2026.06.27-0730',
                'deployed_at', '2026-06-27T03:30:00Z',
                'operator', 'release-bot',
                'status', 'succeeded',
                'change_id', 'CHG-10088',
                'risk', 'high',
                'summary', 'Refreshed inventory reservation ConfigMap and memory limit.',
                'business_reason', 'Support larger flash-sale SKU reservation batches.',
                'related_config', JSON_ARRAY('RESERVATION_BATCH_SIZE=5000', 'JVM_XMX=384m'),
                'verification', JSON_ARRAY('pod rollout status', 'reservation API smoke test'),
                'note', 'ConfigMap refresh shortly before CrashLoopBackOff'
            )
        )
    )
)
ON DUPLICATE KEY UPDATE
    current_version = VALUES(current_version),
    payload = VALUES(payload);

INSERT INTO aiops_history_tickets (
    ticket_id,
    service_name,
    title,
    severity,
    root_cause,
    resolution,
    customer_impact,
    labels_text,
    payload
)
VALUES
(
    'INC-REDIS-001',
    'order-service',
    'Redis maxclients exhausted',
    'P1',
    'Redis connected_clients reached maxclients',
    'Reduced retry storm, recycled abnormal clients after approval, and raised maxclients during the maintenance window.',
    'Checkout order creation returned 503 for part of peak traffic.',
    'redis maxclients checkout timeout',
    JSON_OBJECT(
        'ticket_id', 'INC-REDIS-001',
        'service_name', 'order-service',
        'title', 'Redis maxclients exhausted',
        'severity', 'P1',
        'root_cause', 'Redis connected_clients reached maxclients',
        'resolution', 'Reduced retry storm, recycled abnormal clients after approval, and raised maxclients during the maintenance window.',
        'customer_impact', 'Checkout order creation returned 503 for part of peak traffic.',
        'business_impact', 'Order conversion dropped and customer support received duplicate submit complaints.',
        'impacted_endpoints', JSON_ARRAY('POST /api/orders', 'GET /api/orders/{id}'),
        'evidence', JSON_ARRAY(
            'order-service logs showed Redis connection timeout on POST /api/orders',
            'Redis connected_clients was 9940/10000 and blocked_clients increased',
            'Prometheus showed P95 latency above 3s and 5xx above 8%'
        ),
        'timeline', JSON_ARRAY(
            JSON_OBJECT('time', '20:05', 'event', '5xx alert fired for order-service'),
            JSON_OBJECT('time', '20:07', 'event', 'Redis maxclients near-limit evidence collected'),
            JSON_OBJECT('time', '20:16', 'event', 'Approved mitigation started')
        ),
        'prevention', JSON_ARRAY(
            'Add early warning at 75% Redis client usage',
            'Cap application connection pool per pod',
            'Add checkout retry budget dashboard'
        ),
        'labels', JSON_ARRAY('redis', 'maxclients', 'checkout', 'timeout'),
        'status', 'resolved'
    )
),
(
    'INC-MYSQL-001',
    'payment-service',
    'MySQL slow query latency',
    'P2',
    'Slow query increased pool wait time',
    'Added a covering index after approval and disabled the expensive payment report feature flag.',
    'Payment submit response slowed down; some users retried payment.',
    'mysql slow-query payment pool-wait',
    JSON_OBJECT(
        'ticket_id', 'INC-MYSQL-001',
        'service_name', 'payment-service',
        'title', 'MySQL slow query latency',
        'severity', 'P2',
        'root_cause', 'Slow query increased pool wait time',
        'resolution', 'Added a covering index after approval and disabled the expensive payment report feature flag.',
        'customer_impact', 'Payment submit response slowed down; some users retried payment.',
        'business_impact', 'Pending-payment orders increased and channel callback delay grew.',
        'impacted_endpoints', JSON_ARRAY('POST /api/payments', 'POST /api/payments/callback'),
        'evidence', JSON_ARRAY(
            'Payment logs showed slow query digest 9f3a-pay-report',
            'MySQL Slow_queries and Threads_connected increased together',
            'Recent deploy CHG-10087 enabled a reconciliation report'
        ),
        'timeline', JSON_ARRAY(
            JSON_OBJECT('time', '14:22', 'event', 'P95 latency exceeded payment SLO'),
            JSON_OBJECT('time', '14:27', 'event', 'Slow query digest identified'),
            JSON_OBJECT('time', '14:42', 'event', 'Feature flag disabled after approval')
        ),
        'prevention', JSON_ARRAY(
            'Require EXPLAIN review for payment report SQL',
            'Add pool waiting alert',
            'Run slow-query replay in staging before release'
        ),
        'labels', JSON_ARRAY('mysql', 'slow-query', 'payment', 'pool-wait'),
        'status', 'resolved'
    )
),
(
    'INC-K8S-001',
    'inventory-service',
    'Pod CrashLoopBackOff after ConfigMap refresh',
    'P1',
    'ConfigMap value caused process startup failure and repeated restarts',
    'Rolled back the ConfigMap through change process and added startup config validation in CI.',
    'Order-service could not reserve stock for affected SKUs.',
    'kubernetes crashloop configmap inventory',
    JSON_OBJECT(
        'ticket_id', 'INC-K8S-001',
        'service_name', 'inventory-service',
        'title', 'Pod CrashLoopBackOff after ConfigMap refresh',
        'severity', 'P1',
        'root_cause', 'ConfigMap value caused process startup failure and repeated restarts',
        'resolution', 'Rolled back the ConfigMap through change process and added startup config validation in CI.',
        'customer_impact', 'Order-service could not reserve stock for affected SKUs.',
        'business_impact', 'Flash-sale checkout capacity dropped and stock reservation backlog grew.',
        'impacted_endpoints', JSON_ARRAY('POST /api/inventory/reservations'),
        'evidence', JSON_ARRAY(
            'Kubernetes events showed CrashLoopBackOff and readiness probe failures',
            'Deploy history showed CHG-10088 ConfigMap refresh shortly before the alert',
            'Startup logs showed invalid reservation batch configuration'
        ),
        'timeline', JSON_ARRAY(
            JSON_OBJECT('time', '11:30', 'event', 'ConfigMap refresh deployed'),
            JSON_OBJECT('time', '11:36', 'event', 'CrashLoopBackOff warning started'),
            JSON_OBJECT('time', '11:49', 'event', 'Rollback completed after approval')
        ),
        'prevention', JSON_ARRAY(
            'Validate ConfigMap values in CI',
            'Add canary rollout for inventory config changes',
            'Alert on readiness failure ratio before full outage'
        ),
        'labels', JSON_ARRAY('kubernetes', 'crashloop', 'configmap', 'inventory'),
        'status', 'resolved'
    )
)
ON DUPLICATE KEY UPDATE
    service_name = VALUES(service_name),
    title = VALUES(title),
    severity = VALUES(severity),
    root_cause = VALUES(root_cause),
    resolution = VALUES(resolution),
    customer_impact = VALUES(customer_impact),
    labels_text = VALUES(labels_text),
    payload = VALUES(payload);

GRANT SELECT, SHOW VIEW ON autooncall.* TO 'autooncall'@'%';
GRANT PROCESS ON *.* TO 'autooncall'@'%';
FLUSH PRIVILEGES;
