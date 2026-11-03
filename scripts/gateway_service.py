#!/usr/bin/env python3
"""PVIaN gateway service manager — single entry point for start/stop/restart/status.

This script wraps the launcher (scripts/launch_gateway.py) and provides
operational subcommands so day-to-day gateway management no longer relies on
hand-rolled netstat + taskkill sequences (gateway-infra-runbook §6).

USAGE
-----
  python scripts/gateway_service.py start     # start if not running
  python scripts/gateway_service.py stop      # stop the running gateway
  python scripts/gateway_service.py restart   # stop then start
  python scripts/gateway_service.py status    # report health + TTL + key

All commands detect the gateway by checking the configured port (default 5200)
and the launcher's PID file. The gateway is always started via launch_gateway.py
so the provider-key preflight and end-to-end self-check run on every start.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "scripts" / "launch_gateway.py"
ENV_FILE = PROJECT_ROOT / ".env"
VAR_DIR = PROJECT_ROOT / "var" / "gateway"
PID_FILE = VAR_DIR / "gateway.pid"
STATE_FILE = VAR_DIR / "qualification-state-flash-0731.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5200


def _log(msg: str) -> None:
    print(f"[gateway_service] {msg}", flush=True)


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


def _load_env() -> dict[str, str]:
    """Load .env values (without overriding existing os.environ) for reading config."""
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    if not ENV_FILE.exists():
        return {}
    return dotenv_values(ENV_FILE)


def _configured_host_port() -> tuple[str, int]:
    env = _load_env()
    # os.environ takes precedence over .env (Task Scheduler may inject these).
    host = os.environ.get("PVI_GATEWAY_HOST") or env.get("PVI_GATEWAY_HOST") or DEFAULT_HOST
    port = int(os.environ.get("PVI_GATEWAY_PORT") or env.get("PVI_GATEWAY_PORT") or DEFAULT_PORT)
    return host, port


def _state_file_path() -> Path:
    env = _load_env()
    raw = os.environ.get("PVI_GATEWAY_STATE_FILE") or env.get("PVI_GATEWAY_STATE_FILE")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p
    return STATE_FILE


# ---------------------------------------------------------------------------
# port / process helpers
# ---------------------------------------------------------------------------


def _port_in_use(host: str, port: int) -> int | None:
    """Return the PID listening on host:port, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) != 0:
                return None  # nothing listening
    except OSError:
        return None
    # Port is open — find the owning PID via netstat.
    r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10)
    try:
        text = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = r.stdout.decode("mbcs", errors="replace")
    for line in text.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            return int(parts[-1])
    return None


def _read_pid_file() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid_file(pid: int) -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _clear_pid_file() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def _kill_tree(pid: int) -> None:
    """Kill a process and its children (uvicorn worker may be a child)."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)


# ---------------------------------------------------------------------------
# health checks
# ---------------------------------------------------------------------------


def _http_get(
    url: str, headers: dict[str, str] | None = None, timeout: float = 3
) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _healthz(host: str, port: int) -> bool:
    code, _ = _http_get(f"http://{host}:{port}/healthz")
    return code == 200


def _readyz(host: str, port: int) -> bool:
    code, _ = _http_get(f"http://{host}:{port}/readyz")
    return code == 200


def _models_ok(host: str, port: int, client_key: str) -> tuple[bool, list[str]]:
    """Check Bearer-authed /v1/models; return (ok, alias_ids)."""
    code, body = _http_get(
        f"http://{host}:{port}/v1/models",
        headers={"Authorization": f"Bearer {client_key}"},
        timeout=5,
    )
    if code != 200:
        return False, []
    try:
        data = json.loads(body)
        ids = [m["id"] for m in data.get("data", [])]
        return True, ids
    except (json.JSONDecodeError, KeyError):
        return False, []


def _state_ttl_info() -> dict[str, object]:
    """Read state file and report per-route valid_until + remaining hours."""
    path = _state_file_path()
    if not path.exists():
        return {"state_file": str(path), "exists": False, "routes": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state_file": str(path), "exists": False, "routes": []}
    routes = []
    now = time.time()
    for route_id, record in sorted(payload.get("records", {}).items()):
        valid_until = record.get("valid_until")
        remaining = None
        if isinstance(valid_until, str):
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                remaining = round((dt.timestamp() - now) / 3600, 1)
            except (ValueError, OSError):
                pass
        routes.append(
            {
                "route_id": route_id,
                "validity_hours": record.get("validity_hours"),
                "valid_until": valid_until,
                "remaining_hours": remaining,
                "status": record.get("qualification_status"),
            }
        )
    return {"state_file": str(path), "exists": True, "routes": routes}


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    host, port = _configured_host_port()
    existing = _port_in_use(host, port)
    if existing is not None:
        _log(f"gateway already running on {host}:{port} (PID {existing})")
        return 0

    _log(f"starting gateway via launcher on {host}:{port} ...")
    # Detach: run launcher in a new process group so it survives this script.
    pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    python = str(pythonw) if pythonw.exists() else sys.executable
    cmd = [python, str(LAUNCHER), "--host", host, "--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # CREATE_NEW_PROCESS_GROUP on Windows so Ctrl+C here doesn't kill it.
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    )
    _write_pid_file(proc.pid)
    _log(f"launcher started (launcher PID {proc.pid}). Waiting for gateway ready...")

    # Wait for /readyz (launcher does its own self-check, but we confirm the port).
    deadline = time.perf_counter() + 90
    while time.perf_counter() < deadline:
        if _readyz(host, port):
            _log(f"gateway is ready on {host}:{port}")
            return 0
        time.sleep(1)
    _log(f"WARNING: gateway did not become ready within 90s on {host}:{port}.")
    _log("Check launcher logs or run 'python scripts/gateway_service.py status'.")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    host, port = _configured_host_port()
    pid = _port_in_use(host, port)
    if pid is None:
        _log(f"no gateway listening on {host}:{port}")
        _clear_pid_file()
        return 0
    _log(f"stopping gateway on {host}:{port} (PID {pid}) ...")
    _kill_tree(pid)
    # Wait for port to free.
    for _ in range(10):
        time.sleep(1)
        if _port_in_use(host, port) is None:
            _log("gateway stopped.")
            _clear_pid_file()
            return 0
    _log("WARNING: port still bound after taskkill; attempting backstop cleanup.")
    # Backstop: re-scan and kill.
    residual = _port_in_use(host, port)
    if residual is not None:
        _kill_tree(residual)
        time.sleep(2)
    _clear_pid_file()
    return 0 if _port_in_use(host, port) is None else 1


def cmd_restart(args: argparse.Namespace) -> int:
    rc = cmd_stop(args)
    if rc != 0:
        _log("stop failed; aborting restart.")
        return rc
    time.sleep(2)
    return cmd_start(args)


def cmd_status(args: argparse.Namespace) -> int:
    host, port = _configured_host_port()
    env = _load_env()
    client_key = os.environ.get("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY") or env.get(
        "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", ""
    )

    pid = _port_in_use(host, port)
    running = pid is not None

    print("=" * 60)
    print(f"PVIaN Gateway Status  ({host}:{port})")
    print("=" * 60)
    print(f"  process     : {'RUNNING (PID ' + str(pid) + ')' if running else 'NOT RUNNING'}")

    if running:
        h = _healthz(host, port)
        r = _readyz(host, port)
        print(f"  /healthz    : {'200 OK' if h else 'FAIL'}")
        print(f"  /readyz     : {'200 OK' if r else 'FAIL'}")
        if client_key:
            ok, aliases = _models_ok(host, port, client_key)
            if ok:
                print(f"  /v1/models  : {len(aliases)} callable alias(es)")
                for a in aliases:
                    print(f"                 - {a}")
            else:
                print("  /v1/models  : FAIL (Bearer auth or upstream error)")
        else:
            print("  /v1/models  : skipped (no client key in .env)")
    else:
        print("  (health checks skipped — gateway not running)")

    # TTL info (from state file, independent of running state).
    ttl = _state_ttl_info()
    print(f"  state file  : {ttl['state_file']}")
    if ttl["exists"]:
        for route in ttl["routes"]:
            rem = route["remaining_hours"]
            rem_str = f"{rem}h" if rem is not None else "?"
            print(
                f"                 {route['route_id']}: "
                f"{route['status']}, TTL {route['validity_hours']}h, "
                f"remaining {rem_str}, until {route['valid_until']}"
            )
    else:
        print("                 (state file not found)")

    # client key presence (not the value).
    print(f"  client key  : {'present in .env (stable)' if client_key else 'MISSING from .env'}")

    # Ledger size.
    ledger = VAR_DIR / "usage-attempt-ledger.json"
    if ledger.exists():
        size_mb = ledger.stat().st_size / (1024 * 1024)
        print(f"  ledger      : {size_mb:.1f} MB ({ledger.name})")

    print("=" * 60)
    return 0 if running else 1


# ---------------------------------------------------------------------------
# requalify (wraps requalify_due.py)
# ---------------------------------------------------------------------------

REQUALIFY_SCRIPT = PROJECT_ROOT / "scripts" / "requalify_due.py"


def cmd_requalify(args: argparse.Namespace) -> int:
    """Run the requalification scheduler (requalify_due.py)."""
    cmd: list[str] = [sys.executable, str(REQUALIFY_SCRIPT)]
    if args.dry_run:
        cmd.append("--dry-run")
    _log(f"running requalify_due.py{' (--dry-run)' if args.dry_run else ''} ...")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return proc.returncode


# ---------------------------------------------------------------------------
# client-key management
# ---------------------------------------------------------------------------

CLIENT_KEY_ENV = "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY"


def _read_env_value(key: str) -> str | None:
    """Read a value from .env (not os.environ, to see the persisted file value)."""
    env = _load_env()
    return env.get(key)


def _update_env_value(key: str, value: str) -> None:
    """Update a single key=value line in .env, preserving everything else."""
    if not ENV_FILE.exists():
        raise SystemExit(f".env not found at {ENV_FILE}")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_client_key(args: argparse.Namespace) -> int:
    if args.key_action == "show":
        value = _read_env_value(CLIENT_KEY_ENV)
        if value:
            print(value)
            return 0
        _log(f"{CLIENT_KEY_ENV} is not set in .env")
        return 1

    if args.key_action == "rotate":
        import secrets

        new_key = secrets.token_urlsafe(48)
        _update_env_value(CLIENT_KEY_ENV, new_key)
        _log(f"rotated {CLIENT_KEY_ENV} in .env.")
        _log("Restart the gateway to activate the new key:")
        _log(f"  python {Path(__file__).name} restart")
        _log("Downstream consumers must update their PVIAN_CLIENT_KEY.")
        return 0

    _log(f"unknown client-key action: {args.key_action}")
    return 2


# ---------------------------------------------------------------------------
# ledger archive (audit 🟡-7: usage-attempt-ledger growth management)
# ---------------------------------------------------------------------------

LEDGER_FILE = VAR_DIR / "usage-attempt-ledger.json"


def cmd_ledger(args: argparse.Namespace) -> int:
    if args.ledger_action == "archive":
        if not LEDGER_FILE.exists():
            _log(f"ledger file not found: {LEDGER_FILE}")
            return 1
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d")
        archive_name = LEDGER_FILE.with_name(f"usage-attempt-ledger-{stamp}.json")
        if archive_name.exists():
            _log(
                f"archive already exists: {archive_name} (remove it first or use a different date)"
            )
            return 1
        # Archive (rename) then create a fresh empty ledger so the gateway
        # can continue appending without interruption.
        LEDGER_FILE.rename(archive_name)
        LEDGER_FILE.write_text(
            '{"schema_version":"pvian.usage-attempt-ledger/v1","attempts":[]}', encoding="utf-8"
        )
        size_mb = archive_name.stat().st_size / (1024 * 1024)
        _log(f"archived {LEDGER_FILE.name} -> {archive_name.name} ({size_mb:.1f} MB)")
        _log(f"created fresh empty ledger at {LEDGER_FILE}")
        _log("Note: the gateway reads/writes the ledger per-request with a cross-process")
        _log("lock, so this operation is safe while the gateway is running.")
        return 0

    if args.ledger_action == "size":
        if LEDGER_FILE.exists():
            size_mb = LEDGER_FILE.stat().st_size / (1024 * 1024)
            print(f"{size_mb:.1f} MB ({LEDGER_FILE.name})")
            return 0
        _log("ledger file not found")
        return 1

    _log(f"unknown ledger action: {args.ledger_action}")
    return 2


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway_service",
        description="PVIaN gateway service manager.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Start the gateway if not already running.")
    sub.add_parser("stop", help="Stop the running gateway.")
    sub.add_parser("restart", help="Stop then start.")
    sub.add_parser("status", help="Report gateway health, TTL, and key status.")

    rq = sub.add_parser("requalify", help="Run the requalification scheduler (requalify_due.py).")
    rq.add_argument(
        "--dry-run", action="store_true", help="Show due routes without calling upstream."
    )

    ck = sub.add_parser("client-key", help="Manage the gateway client key.")
    ck_sub = ck.add_subparsers(dest="key_action", required=True)
    ck_sub.add_parser("show", help="Print the stable client key from .env.")
    ck_rotate = ck_sub.add_parser(
        "rotate", help="Generate a new key, update .env, prompt to restart."
    )
    ck_rotate.description = (
        "Generates a new key and writes it to .env. Restart the gateway to activate."
    )

    lg = sub.add_parser("ledger", help="Manage the usage-attempt-ledger file.")
    lg_sub = lg.add_subparsers(dest="ledger_action", required=True)
    lg_sub.add_parser("archive", help="Archive the current ledger and start a fresh one.")
    lg_sub.add_parser("size", help="Print the current ledger size.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "requalify": cmd_requalify,
        "client-key": cmd_client_key,
        "ledger": cmd_ledger,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
