"""One-click local launcher for PyCharm on Windows.

This script starts the interview-focused AutoOnCall local demo stack:
- Core AIOps adapters from deploy/compose/interview-stack.yml
- Milvus/RAG is optional; start it separately with `make up && make upload`
- CLS and Monitor MCP servers
- FastAPI, which also serves the static frontend at the configured local API URL

Create a PyCharm Python run configuration pointing to this file, set the working
directory to the project root, then click Run.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import (  # noqa: E402
    LOCAL_DEMO_API_URL,
    LOCAL_FULL_STACK_ENV,
)

LOG_DIR = ROOT / "logs"
API_URL = LOCAL_DEMO_API_URL
LIVE_URL = f"{API_URL}/health/live"
READY_URL = f"{API_URL}/health/ready"

MCP_PROCESSES = [
    ("CLS MCP", ROOT / "mcp_servers" / "cls_server.py", ROOT / "mcp_cls.log"),
    ("Monitor MCP", ROOT / "mcp_servers" / "monitor_server.py", ROOT / "mcp_monitor.log"),
]


def main() -> int:
    args = parse_args()
    ensure_project_root()
    LOG_DIR.mkdir(exist_ok=True)

    print_header()
    print_project_summary()
    ensure_docker_available()
    python_cmd = resolve_python(args.python)
    print(f"[OK] Python: {python_cmd}")

    if not args.skip_install:
        install_dependencies(python_cmd)

    if not args.skip_docker:
        compose_up(
            "interview Docker stack",
            ROOT / "deploy" / "compose" / "interview-stack.yml",
        )

    start_mcp_servers(python_cmd, restart=args.restart_processes)
    start_api(python_cmd, restart=args.restart_processes)

    wait_for_http(LIVE_URL, timeout_seconds=args.live_timeout, label="FastAPI liveness")
    ready = wait_for_http(
        READY_URL, timeout_seconds=args.ready_timeout, label="readiness", required=False
    )
    if not ready:
        print(
            "[WARN] Readiness has not fully passed yet. The UI is available; RAG may still be warming up."
        )

    if args.upload_docs:
        upload_docs()

    print()
    print("[DONE] AutoOnCall local stack is running.")
    print(f"       Frontend: {API_URL}")
    print(f"       API docs: {API_URL}/docs")
    print("       Stop app processes: python scripts/dev/pycharm_one_click_stop.py")
    print("       Stop containers: docker compose -f deploy/compose/interview-stack.yml down")

    if args.open_browser:
        webbrowser.open(API_URL)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start AutoOnCall locally from PyCharm.")
    parser.add_argument(
        "--python", default="", help="Python executable to use. Defaults to current/venv."
    )
    parser.add_argument("--skip-install", action="store_true", help="Skip pip editable install.")
    parser.add_argument(
        "--skip-docker", action="store_true", help="Do not start Docker Compose stacks."
    )
    parser.add_argument(
        "--restart-processes", action="store_true", help="Restart MCP/API if already running."
    )
    parser.add_argument(
        "--upload-docs", action="store_true", help="Upload aiops-docs/*.md after startup."
    )
    parser.add_argument("--no-open-browser", dest="open_browser", action="store_false")
    parser.add_argument("--live-timeout", type=int, default=90)
    parser.add_argument("--ready-timeout", type=int, default=120)
    parser.set_defaults(open_browser=True)
    return parser.parse_args()


def ensure_project_root() -> None:
    required = [ROOT / "app" / "main.py", ROOT / "pyproject.toml", ROOT / "static" / "index.html"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Not an AutoOnCall project root. Missing: {', '.join(missing)}")
    os.chdir(ROOT)


def print_header() -> None:
    print("=" * 72)
    print("AutoOnCall PyCharm One-Click Launcher")
    print("=" * 72)


def print_project_summary() -> None:
    print("[INFO] Project analysis:")
    print("       FastAPI entry: app.main:app")
    print("       Frontend: static/index.html served by FastAPI /")
    print("       Docker stack: deploy/compose/interview-stack.yml (core AIOps only)")
    print("       Optional RAG: make up && make upload")
    print("       Local MCP servers: mcp_servers/cls_server.py, monitor_server.py")


def resolve_python(explicit: str) -> str:
    if explicit:
        return explicit
    if is_project_venv_python(sys.executable):
        return sys.executable
    candidates = [
        ROOT / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def ensure_docker_available() -> None:
    run(["docker", "info"], label="check Docker Desktop", quiet=True)


def install_dependencies(python_cmd: str) -> None:
    if python_module_available(python_cmd, "pip"):
        print("[RUN] Installing project dependencies with pip -e .")
        run([python_cmd, "-m", "pip", "install", "-e", "."], label="install dependencies")
        return

    if command_available("uv"):
        print("[RUN] pip is unavailable; installing project dependencies with uv pip")
        run(
            ["uv", "pip", "install", "--python", python_cmd, "-e", "."],
            label="install dependencies",
        )
        return

    print("[RUN] pip is unavailable; trying ensurepip")
    run([python_cmd, "-m", "ensurepip", "--upgrade"], label="bootstrap pip")
    run([python_cmd, "-m", "pip", "install", "-e", "."], label="install dependencies")


def compose_up(label: str, compose_file: Path) -> None:
    print(f"[RUN] Starting containers: {label}")
    command = ["docker", "compose"]
    command.extend(["-f", str(compose_file), "up", "-d", "--remove-orphans"])
    run(command, label=f"docker compose {label}")


def start_mcp_servers(python_cmd: str, *, restart: bool) -> None:
    for name, script_path, log_path in MCP_PROCESSES:
        if is_process_running(script_path.name):
            if not restart:
                print(f"[OK] {name} already running")
                continue
            stop_processes_matching(script_path.name)
        start_background_process(
            name=name,
            command=[python_cmd, str(script_path)],
            log_path=log_path,
            env=runtime_env(),
        )


def start_api(python_cmd: str, *, restart: bool) -> None:
    if http_ok(LIVE_URL):
        if not restart:
            print("[OK] FastAPI already running")
            return
        stop_processes_matching("uvicorn")

    start_background_process(
        name="AutoOnCall API",
        command=[
            python_cmd,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "9900",
        ],
        log_path=ROOT / "server.log",
        env=runtime_env(),
    )


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    for key, value in LOCAL_FULL_STACK_ENV.items():
        env.setdefault(key, value)
    return env


def start_background_process(
    *,
    name: str,
    command: list[str],
    log_path: Path,
    env: dict[str, str],
) -> None:
    print(f"[RUN] Starting {name}; log={relative(log_path)}")
    log_file = log_path.open("a", encoding="utf-8")
    subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )
    time.sleep(2)


def wait_for_http(
    url: str,
    *,
    timeout_seconds: int,
    label: str,
    required: bool = True,
) -> bool:
    print(f"[WAIT] {label}: {url}")
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if http_ok(url):
            print(f"[OK] {label} ready")
            return True
        print(f"      waiting... {attempt}")
        time.sleep(2)
    if required:
        raise SystemExit(f"{label} did not become ready in {timeout_seconds}s")
    return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def upload_docs() -> None:
    docs = sorted((ROOT / "aiops-docs").glob("*.md"))
    if not docs:
        print("[WARN] No aiops-docs/*.md files found.")
        return
    print(f"[RUN] Uploading {len(docs)} aiops-docs files")
    for path in docs:
        run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"{API_URL}/api/upload",
                "-F",
                f"file=@{path}",
            ],
            label=f"upload {path.name}",
        )


def run(
    command: list[str],
    *,
    label: str,
    quiet: bool = False,
    env: dict[str, str] | None = None,
) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
            env=env,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"{label} failed: command not found: {command[0]}") from exc
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def is_project_venv_python(python_cmd: str) -> bool:
    try:
        python_path = Path(python_cmd).resolve()
        venv_path = (ROOT / "venv").resolve()
        return python_path.is_relative_to(venv_path)
    except (OSError, ValueError):
        return False


def python_module_available(python_cmd: str, module_name: str) -> bool:
    completed = subprocess.run(
        [python_cmd, "-m", module_name, "--version"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def command_available(command: str) -> bool:
    completed = subprocess.run(
        ["where", command] if os.name == "nt" else ["which", command],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def is_process_running(token: str) -> bool:
    if os.name != "nt":
        return False
    ps_command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.CommandLine -like '*" + token + "*' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool((completed.stdout or "").strip())


def stop_processes_matching(token: str) -> None:
    if os.name != "nt":
        return
    ps_command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{token}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
        check=False,
    )


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
