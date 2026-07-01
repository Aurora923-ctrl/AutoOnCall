"""Optional Playwright smoke test for the static AutoOnCall workbench."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request

import pytest


def test_static_workbench_renders_in_browser(tmp_path) -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    port = _unused_port()
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_http(f"http://127.0.0.1:{port}/health/live")
        errors: list[str] = []
        with playwright_api.sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Playwright browser is not installed: {exc}")
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.locator("#aiOpsPresetSelect").wait_for(state="visible", timeout=5000)
            page.locator("#aiOpsPresetSelect").select_option("redis_maxclients")
            assert page.locator("#aiOpsServiceName").input_value() == "order-service"
            assert "Redis" in page.locator("#aiOpsTitle").input_value()
            page.locator('[data-workbench-view="chat"]').click()
            page.locator("#knowledgeUploadBtn").wait_for(state="visible", timeout=5000)
            page.locator('[data-workbench-view="incidents"]').click()
            page.locator('[data-incident-tab="process"]').click()
            page.locator("#planList").wait_for(state="visible", timeout=5000)
            page.locator('[data-workbench-view="response"]').click()
            page.locator("#workbenchPanel").wait_for(state="visible", timeout=5000)
            page.locator('[data-workbench-view="system"]').click()
            page.locator("#toolContractSummary").wait_for(state="visible", timeout=5000)
            page.screenshot(path=str(tmp_path / "autooncall-workbench.png"), full_page=False)
            browser.close()

        relevant_errors = [
            error
            for error in errors
            if "favicon" not in error.lower() and "failed to load resource" not in error.lower()
        ]
        assert not relevant_errors
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")
