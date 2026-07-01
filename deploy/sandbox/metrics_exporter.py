"""Tiny Prometheus exporter for the local AutoOnCall AIOps sandbox."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic


STARTED_AT = monotonic()
SERVICES = {
    "order-service": {
        "qps": 1480.0,
        "error_rate": 0.082,
        "p95_ms": 3250.0,
        "cpu": 74.2,
        "memory": 635_000_000,
    },
    "payment-service": {
        "qps": 640.0,
        "error_rate": 0.027,
        "p95_ms": 1840.0,
        "cpu": 66.5,
        "memory": 512_000_000,
    },
    "checkout-service": {
        "qps": 920.0,
        "error_rate": 0.041,
        "p95_ms": 2460.0,
        "cpu": 69.4,
        "memory": 588_000_000,
    },
}


def render_metrics() -> str:
    """Return deterministic metrics matching AutoOnCall's sandbox PromQL templates."""
    elapsed = max(monotonic() - STARTED_AT, 1.0)
    lines = [
        "# HELP autooncall_http_qps Current request throughput by service.",
        "# TYPE autooncall_http_qps gauge",
        "# HELP autooncall_http_5xx_rate Current 5xx ratio by service.",
        "# TYPE autooncall_http_5xx_rate gauge",
        "# HELP autooncall_p95_latency_ms Current p95 latency in milliseconds.",
        "# TYPE autooncall_p95_latency_ms gauge",
        "# HELP autooncall_cpu_usage_percent Current CPU usage percentage.",
        "# TYPE autooncall_cpu_usage_percent gauge",
        "# HELP autooncall_memory_working_set_bytes Current memory working set.",
        "# TYPE autooncall_memory_working_set_bytes gauge",
        "# HELP http_requests_total Cumulative HTTP requests for default PromQL compatibility.",
        "# TYPE http_requests_total counter",
        "# HELP http_request_duration_seconds_bucket Latency histogram buckets.",
        "# TYPE http_request_duration_seconds_bucket counter",
    ]
    for service, values in SERVICES.items():
        qps = values["qps"]
        error_rate = values["error_rate"]
        total = int(qps * elapsed)
        errors = int(total * error_rate)
        success = max(total - errors, 0)
        labels = f'service="{service}"'
        lines.extend(
            [
                f"autooncall_http_qps{{{labels}}} {qps}",
                f"autooncall_http_5xx_rate{{{labels}}} {error_rate}",
                f"autooncall_p95_latency_ms{{{labels}}} {values['p95_ms']}",
                f"autooncall_cpu_usage_percent{{{labels}}} {values['cpu']}",
                f"autooncall_memory_working_set_bytes{{{labels}}} {values['memory']}",
                f'http_requests_total{{{labels},status="200"}} {success}',
                f'http_requests_total{{{labels},status="500"}} {errors}',
            ]
        )
        for bucket, ratio in [(0.1, 0.20), (0.5, 0.55), (1.0, 0.74), (2.5, 0.92), (5.0, 1.0)]:
            lines.append(
                f'http_request_duration_seconds_bucket{{{labels},le="{bucket}"}} '
                f"{int(total * ratio)}"
            )
        lines.append(f'http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {total}')
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/"}:
            self.send_response(404)
            self.end_headers()
            return
        payload = render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9108), MetricsHandler).serve_forever()
