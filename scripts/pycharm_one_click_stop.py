"""Stop local AutoOnCall app processes started by the PyCharm launcher."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROCESS_TOKENS = [
    "uvicorn",
    "app.main:app",
    "mcp_servers\\cls_server.py",
    "mcp_servers/cls_server.py",
    "mcp_servers\\monitor_server.py",
    "mcp_servers/monitor_server.py",
]


def main() -> int:
    args = parse_args()
    os.chdir(ROOT)
    print("=" * 72)
    print("AutoOnCall PyCharm One-Click Stop")
    print("=" * 72)
    stop_app_processes()
    if args.containers:
        compose_down(ROOT / "vector-database.yml")
        compose_down(ROOT / "deploy" / "full-stack-compose.yml")
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
    for token in PROCESS_TOKENS:
        ps_command = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{token}*' }} | "
            "ForEach-Object { "
            "Write-Host ('Stopping PID ' + $_.ProcessId + ' ' + $_.CommandLine); "
            "Stop-Process -Id $_.ProcessId -Force "
            "}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
            cwd=ROOT,
            check=False,
        )


def compose_down(compose_file: Path) -> None:
    print(f"[RUN] docker compose -f {compose_file.name} down")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down"],
        cwd=ROOT,
        check=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
