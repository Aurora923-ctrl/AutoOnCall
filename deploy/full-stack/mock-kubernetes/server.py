"""Tiny read-only Kubernetes API mock for the full-stack sandbox."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MOCK_DIR = Path(__file__).resolve().parent
RESOURCES_PATH = MOCK_DIR / "resources.json"
DEFAULT_NAMESPACE = os.getenv("MOCK_KUBERNETES_NAMESPACE", "default")


def load_resources() -> dict[str, list[dict[str, Any]]]:
    with RESOURCES_PATH.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return {
        "pods": list(payload.get("pods", [])),
        "events": list(payload.get("events", [])),
    }


def filter_pods(
    pods: list[dict[str, Any]],
    namespace: str,
    label_selector: str,
) -> list[dict[str, Any]]:
    namespace_pods = [
        item for item in pods if item.get("metadata", {}).get("namespace", DEFAULT_NAMESPACE) == namespace
    ]
    if not label_selector:
        return namespace_pods

    label_pairs = [
        tuple(part.split("=", 1))
        for part in label_selector.split(",")
        if "=" in part and part.split("=", 1)[0].strip()
    ]
    if not label_pairs:
        return namespace_pods

    matched: list[dict[str, Any]] = []
    for pod in namespace_pods:
        labels = pod.get("metadata", {}).get("labels", {})
        if all(str(labels.get(key.strip())) == value.strip() for key, value in label_pairs):
            matched.append(pod)
    return matched


def filter_events(
    events: list[dict[str, Any]],
    namespace: str,
    field_selector: str,
) -> list[dict[str, Any]]:
    namespace_events = [
        item
        for item in events
        if item.get("involvedObject", {}).get("namespace", DEFAULT_NAMESPACE) == namespace
    ]
    if "involvedObject.kind=Pod" not in field_selector:
        return namespace_events
    return [
        item
        for item in namespace_events
        if item.get("involvedObject", {}).get("kind") == "Pod"
    ]


class KubernetesMockHandler(BaseHTTPRequestHandler):
    server_version = "AutoOnCallKubernetesMock/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in {"/readyz", "/healthz"}:
            self._write_json({"status": "ok"})
            return

        prefix = "/api/v1/namespaces/"
        if parsed.path.startswith(prefix):
            resources = load_resources()
            remainder = parsed.path[len(prefix) :]
            namespace, _, resource_name = remainder.partition("/")
            if resource_name == "pods":
                pods = filter_pods(
                    resources["pods"],
                    namespace,
                    query.get("labelSelector", [""])[0],
                )
                self._write_json({"kind": "PodList", "apiVersion": "v1", "items": pods})
                return
            if resource_name == "events":
                events = filter_events(
                    resources["events"],
                    namespace,
                    query.get("fieldSelector", [""])[0],
                )
                self._write_json({"kind": "EventList", "apiVersion": "v1", "items": events})
                return

        self._write_json(
            {
                "kind": "Status",
                "apiVersion": "v1",
                "status": "Failure",
                "reason": "NotFound",
            },
            status=404,
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _write_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = int(os.getenv("MOCK_KUBERNETES_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), KubernetesMockHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
