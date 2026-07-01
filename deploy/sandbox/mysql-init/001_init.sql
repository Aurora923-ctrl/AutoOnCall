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

INSERT INTO orders (user_id, status, amount)
VALUES
    (10001, 'CREATED', 89.90),
    (10002, 'PAID', 199.00),
    (10003, 'FAILED', 39.90),
    (10004, 'PAID', 459.00);

INSERT INTO payment_events (order_id, event_type, payload)
VALUES
    (1, 'payment_requested', JSON_OBJECT('channel', 'card')),
    (2, 'payment_succeeded', JSON_OBJECT('channel', 'wallet')),
    (3, 'payment_failed', JSON_OBJECT('reason', 'timeout'));

GRANT SELECT, SHOW VIEW ON autooncall.* TO 'autooncall'@'%';
GRANT PROCESS ON *.* TO 'autooncall'@'%';
FLUSH PRIVILEGES;
