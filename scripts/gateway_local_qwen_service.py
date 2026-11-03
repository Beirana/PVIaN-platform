from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway_isolated_local_qwen import PORT, _child_env, build_overlay  # noqa: E402

ROOT = PROJECT_ROOT / "var" / "gateway-local-qwen"
PID_FILE = ROOT / "gateway.pid"


class ServiceError(RuntimeError):
    pass


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    call = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    return str(pid) in call.stdout


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, timeout=30, check=False,
    )


def _health(client: httpx.Client) -> int | None:
    try:
        response = client.get("/healthz", timeout=5.0)
        return response.status_code
    except httpx.TransportError:
        return None


def _wait_ready(process: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ServiceError("local-qwen gateway child exited during startup")
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=5.0) as client:
                if _health(client) == 200:
                    return
        except httpx.TransportError:
            pass
        time.sleep(1.0)
    raise ServiceError("local-qwen gateway did not become ready in time")


def start() -> int:
    if not (ROOT / "qualification-state.json").exists():
        if ROOT.exists() and any(ROOT.iterdir()):
            raise ServiceError(
                "overlay root exists without a qualification state; inspect manually"
            )
        if ROOT.exists():
            ROOT.rmdir()
        manifest = build_overlay(ROOT)
        print("[local-qwen] overlay built:", manifest["aliases"])
    pid = _read_pid()
    with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=5.0) as client:
        already = _pid_alive(pid) and _health(client) == 200
    if already:
        print(f"[local-qwen] already running on 127.0.0.1:{PORT} (PID {pid})")
        return 0
    if pid is not None and not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    log_handle = (ROOT / "gateway.log").open("ab")
    try:
        process = subprocess.Popen(
            [
                str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                "-m", "uvicorn", "pvian_gateway.main:app",
                "--host", "127.0.0.1", "--port", str(PORT),
            ],
            cwd=str(PROJECT_ROOT),
            env=_child_env(ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        log_handle.close()
    PID_FILE.write_text(str(process.pid) + "\n", encoding="utf-8")
    _wait_ready(process)
    print(f"[local-qwen] gateway running on 127.0.0.1:{PORT} (PID {process.pid})")
    return 0


def stop() -> int:
    pid = _read_pid()
    if pid is None or not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print("[local-qwen] not running")
        return 0
    print(f"[local-qwen] stopping PID {pid} ...")
    _kill_tree(pid)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.5)
    PID_FILE.unlink(missing_ok=True)
    print("[local-qwen] stopped")
    return 0


def status() -> int:
    pid = _read_pid()
    alive = _pid_alive(pid)
    print("=" * 60)
    print(f"Local-Qwen Gateway Status  (127.0.0.1:{PORT})")
    print("=" * 60)
    state = f'RUNNING (PID {pid})' if alive else 'NOT RUNNING'
    print(f'  process     : {state}')
    if not alive:
        return 1
    with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=10.0) as client:
        health = _health(client)
        print(f"  /healthz    : {health or 'unreachable'}")
        if health != 200:
            return 1
        ready = client.get("/readyz").status_code
        print(f"  /readyz     : {ready}")
        from dotenv import dotenv_values

        bearer = dotenv_values(PROJECT_ROOT / ".env").get(
            "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", ""
        ).strip()
        models = client.get("/v1/models", headers={"authorization": f"Bearer {bearer}"})
        data = models.json().get("data", [])
        print(f"  /v1/models  : {len(data)} callable alias(es)")
        for item in data:
            print(f"                 - {item['id']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the dedicated local-qwen gateway on 5239.")
    parser.add_argument("command", choices=["start", "stop", "restart", "status"])
    arguments = parser.parse_args()
    if arguments.command == "start":
        return start()
    if arguments.command == "stop":
        return stop()
    if arguments.command == "restart":
        stop()
        time.sleep(2.0)
        return start()
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
