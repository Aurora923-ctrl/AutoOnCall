"""Emit deterministic business logs into the local Loki sandbox."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOKI_PUSH_URL = os.getenv("LOKI_PUSH_URL", "http://loki:3100/loki/api/v1/push")
PUSH_INTERVAL_SECONDS = int(os.getenv("LOKI_PUSH_INTERVAL_SECONDS", "30"))
READY_FILE = Path("/tmp/autooncall-loki-emitter.ready")

LOG_TEMPLATES = [
    {
        "service": "order-service",
        "level": "ERROR",
        "endpoint": "POST /api/orders",
        "incident_id": "INC-REDIS-001",
        "message": (
            "ERROR order-service Redis connection timeout while creating order; "
            "endpoint=POST /api/orders redis_instance=redis-order-cache "
            "checkout_success_rate=0.918"
        ),
    },
    {
        "service": "order-service",
        "level": "WARN",
        "endpoint": "POST /api/orders",
        "incident_id": "INC-REDIS-001",
        "message": (
            "WARN order-service dependency timeout budget nearly exhausted; "
            "redis_connected_clients=9940 maxclients=10000 backlog=1860"
        ),
    },
    {
        "service": "payment-service",
        "level": "ERROR",
        "endpoint": "POST /api/payments",
        "incident_id": "INC-MYSQL-001",
        "message": (
            "ERROR payment-service slow query timeout digest=9f3a-pay-report avg_ms=2280 "
            "pool_waiting=6 active_connections=188/200 feature_flag=PAYMENT_REPORT_ENABLED "
            "endpoint=POST /api/payments"
        ),
    },
    {
        "service": "inventory-service",
        "level": "ERROR",
        "endpoint": "POST /api/inventory/reservations",
        "incident_id": "INC-K8S-001",
        "message": (
            "ERROR inventory-service startup validation failed; "
            "config=RESERVATION_BATCH_SIZE value=5000 reason=memory_limit_too_low"
        ),
    },
    {
        "service": "inventory-service",
        "level": "WARN",
        "endpoint": "POST /api/inventory/reservations",
        "incident_id": "INC-K8S-001",
        "message": (
            "WARN inventory-service readiness probe failed with 503; "
            "pod=inventory-service-6d7c9b-crash1 reservation_backlog=2380"
        ),
    },
]


def build_payload() -> dict[str, Any]:
    now_ns = time.time_ns()
    streams = []
    for index, template in enumerate(LOG_TEMPLATES):
        timestamp_ns = str(now_ns - index * 1_000_000_000)
        streams.append(
            {
                "stream": {
                    "service": template["service"],
                    "level": template["level"],
                    "endpoint": template["endpoint"],
                    "incident_id": template["incident_id"],
                },
                "values": [[timestamp_ns, template["message"]]],
            }
        )
    return {"streams": streams}


def push_once() -> None:
    body = json.dumps(build_payload()).encode("utf-8")
    request = urllib.request.Request(
        LOKI_PUSH_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 300:
            raise RuntimeError(f"Loki push failed with status={response.status}")


def main() -> None:
    while True:
        try:
            push_once()
            READY_FILE.touch()
            print("pushed business log fixtures to Loki", flush=True)
            time.sleep(PUSH_INTERVAL_SECONDS)
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            READY_FILE.unlink(missing_ok=True)
            print(f"Loki log push failed: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
