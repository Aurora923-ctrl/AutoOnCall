"""Stop local AutoOnCall app processes started by the PyCharm launcher."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MANAGED_PROCESSES = [
    ("AutoOnCall API", ROOT / "server.pid", ("uvicorn", "app.main:app", "--port", "9900")),
    ("CLS MCP", ROOT / "mcp_cls.pid", ("mcp_servers", "cls_server.py")),
    ("Monitor MCP", ROOT / "mcp_monitor.pid", ("mcp_servers", "monitor_server.py")),
]


def main() -> int:
    args = parse_args()
    os.chdir(ROOT)
    print("=" * 72)
    print("AutoOnCall PyCharm One-Click Stop")
    print("=" * 72)
    stop_app_processes()
    if args.containers:
        compose_down(ROOT / "deploy" / "compose" / "vector-database.yml")
        compose_down(ROOT / "deploy" / "compose" / "interview-stack.yml")
    print("[DONE] Stop command completed.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop local AutoOnCall processes.")
    parser.add_argument(
        "--containers",
        action="store_true",
        help="Also stop Docker Compose containers.",
    )
    return parser.parse_args()


def stop_app_processes() -> None:
    if os.name != "nt":
        print("[WARN] Non-Windows process cleanup is not implemented in this helper.")
        return
    for name, pid_path, expected_tokens in MANAGED_PROCESSES:
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            print(f"[INFO] {name} has no valid launcher PID file")
            continue
        ps_command = (
            f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
            "if ($null -eq $p) { exit 2 }; "
            "$line = [string]$p.CommandLine; "
            + " ".join(
                f"if ($line -notlike '*{token}*') {{ exit 3 }};" for token in expected_tokens
            )
            + "Stop-Process -Id $p.ProcessId -Force; exit 0"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
            cwd=ROOT,
            check=False,
        )
        if completed.returncode == 0:
            print(f"[OK] Stopped {name} PID {pid}")
        elif completed.returncode == 3:
            print(f"[WARN] PID {pid} no longer matches {name}; refusing to stop it")
        else:
            print(f"[INFO] {name} PID {pid} is not running")
        pid_path.unlink(missing_ok=True)


def compose_down(compose_file: Path) -> None:
    print(f"[RUN] docker compose -f {compose_file.name} down")
    completed = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down"],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"docker compose -f {compose_file.name} down failed with exit code "
            f"{completed.returncode}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
