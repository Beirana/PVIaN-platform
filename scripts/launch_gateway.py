#!/usr/bin/env python3
"""Unified gateway launcher — the single, safe way to start the PVIaN gateway.

WHY THIS EXISTS
---------------
Two independent infrastructure failures were traced to *how* the gateway was
started, not to the gateway code or the upstream provider:

  1. S-SB-002 Gate C: deterministic 502 upstream_transport_error on one
     high-reasoning-load call. Root cause: gateway upstream timeout (60s) was
     tighter than the call's ~143-200s generation time. Fixed by raising the
     default to 300s (config.py). But the gateway only sees the new default if
     it is started *after* the change.

  2. S/P branch 401 invalid_client_api_key: the gateway reads the provider API
     key from os.environ (profiles.py:121), and main.py:524 does
     load_dotenv(..., override=False). With override=False, an *empty-string*
     env var already present in the launching shell is NOT overwritten by the
     .env value — so a shell that set PVI_PROBE_BAILIAN_API_KEY="" silently
     starves the gateway of its credential, and every upstream call returns 401.
     The gateway logs nothing useful because, to it, the key is "present"
     (just empty). The .env key itself is valid (verified: direct upstream call
     returns 200).

This launcher eliminates both failure modes by:

  - Loading .env with override=True, so .env values win over stale/empty shell
    env vars (the exact opposite of the gateway's own override=False default,
    which is safe for production but fragile for launching).
  - Verifying the provider key actually reaches the upstream (a direct
    chat/completions probe) BEFORE declaring the gateway usable. The existing
    launchers only checked alias visibility (/v1/models), which passes even
    when the key is wrong.
  - Starting the gateway via the main() entry point (so load_dotenv runs inside
    the gateway too), and confirming the effective upstream timeout.

It is deliberately free of any experiment/P-branch/S-branch logic — those
launchers should call this one (or copy its verification steps) instead of
re-implementing gateway startup.

USAGE
-----
  python scripts/launch_gateway.py
  python scripts/launch_gateway.py --port 5200 --state-file var/gateway/state.json
  python scripts/launch_gateway.py --no-keep  # start, verify, then stop (smoke)

By default it starts the gateway and keeps it in the foreground (Ctrl+C stops
it cleanly). The gateway listens on 127.0.0.1 only; it is never the launcher's
job to expose it publicly.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def _log(msg: str) -> None:
    print(f"[launch_gateway] {msg}", flush=True)


def load_env_with_override(env_file: Path) -> dict[str, str]:
    """Load .env with override=True and return the provider variables.

    This is the fix for the 401 root cause: override=True forces .env values to
    win over empty/stale shell env vars. Returns the resolved provider vars so
    the launcher can probe the upstream directly with the SAME key the gateway
    will use.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        raise SystemExit("python-dotenv not installed in this environment") from None

    # dotenv_values reads the file; we then apply with override into os.environ.
    values = dotenv_values(env_file)
    applied = {}
    provider_vars = [
        "PVI_PROBE_BAILIAN_API_KEY",
        "PVI_PROBE_BAILIAN_OPENAI_BASE_URL",
        "PVI_PROBE_BAILIAN_REGION",
        "PVI_PROBE_BAILIAN_OPENAI_CHAT_PATH",
        "PVI_PROBE_BAILIAN_OPENAI_TRANSPORT",
        # Gateway client key — loaded here so main() can reuse the stable .env
        # value instead of generating a fresh random key on every launch.
        "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY",
    ]
    for key in provider_vars:
        val = values.get(key)
        if val is None:
            continue
        # override=True semantics: .env wins over whatever is in the shell.
        os.environ[key] = val
        applied[key] = key  # don't log values

    # Surface whether the critical key is now non-empty (without leaking it).
    key = os.environ.get("PVI_PROBE_BAILIAN_API_KEY", "")
    base = os.environ.get("PVI_PROBE_BAILIAN_OPENAI_BASE_URL", "")
    _log(f"loaded {len(applied)} provider vars from .env (override=True)")
    key_status = "set, len=" + str(len(key)) if key else "EMPTY — this would cause 401"
    _log(f"  PVI_PROBE_BAILIAN_API_KEY: {key_status}")
    _log(f"  PVI_PROBE_BAILIAN_OPENAI_BASE_URL: {base or 'EMPTY'}")
    if not key:
        raise SystemExit(
            "provider key is empty after loading .env — refusing to start (would cause 401)"
        )
    return values


def verify_provider_key_reaches_upstream() -> None:
    """Probe the upstream directly with the provider key. Fails fast on 401.

    This is the step the existing launchers are missing: they only check that
    the alias is *visible* via the gateway, which passes even with a bad key.
    A 200 here proves the key the gateway will use actually works end-to-end.
    """
    key = os.environ["PVI_PROBE_BAILIAN_API_KEY"]
    base = os.environ["PVI_PROBE_BAILIAN_OPENAI_BASE_URL"].rstrip("/")
    chat_path = os.environ.get("PVI_PROBE_BAILIAN_OPENAI_CHAT_PATH", "/chat/completions")
    url = base + chat_path
    body = json.dumps(
        {
            "model": "deepseek-v4-flash-0731",
            "messages": [{"role": "user", "content": "reply with the single word: OK"}],
            "max_tokens": 16,
            "stream": False,
            "enable_thinking": False,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    _log(f"probing upstream directly: POST {url}")
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=60) as r:
            elapsed = time.perf_counter() - t0
            _log(f"  upstream probe -> {r.status} in {elapsed:.2f}s (key is valid)")
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        body_head = e.read()[:300].decode("utf-8", "replace")
        raise SystemExit(
            f"upstream probe FAILED: HTTP {e.code} in {elapsed:.2f}s\n"
            f"  body: {body_head}\n"
            f"  -> the provider key is rejected by the upstream. The gateway would "
            f"return 401 invalid_client_api_key on every call. Aborting start."
        ) from e
    except Exception as e:
        elapsed = time.perf_counter() - t0
        raise SystemExit(
            f"upstream probe FAILED: {type(e).__name__}: {e} in {elapsed:.2f}s\n"
            f"  -> cannot reach upstream. Check network/TUN/proxy. Aborting start."
        ) from e


def generate_client_key() -> str:
    """One-time gateway client key (base64url, no padding), as the P-branch launchers do."""
    return secrets.token_urlsafe(48)


def start_gateway(
    host: str, port: int, state_file: str, client_key: str, timeout: int
) -> subprocess.Popen:
    """Start pvian-gateway via the main() entry point (so its load_dotenv runs).

    We pass the timeout explicitly via env var too, so it is unambiguous
    regardless of any config.py default.
    """
    env = os.environ.copy()
    env["PVI_GATEWAY_HOST"] = host
    env["PVI_GATEWAY_PORT"] = str(port)
    env["PVI_GATEWAY_STATE_FILE"] = str(Path(state_file).resolve())
    env["PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY"] = client_key
    env["PVI_GATEWAY_UPSTREAM_TIMEOUT_SECONDS"] = str(timeout)

    # Use the installed entry point (pvian-gateway.exe) so the call goes through
    # main(), which runs load_dotenv before create_app/uvicorn.run. The module-
    # level `app = create_app()` at main.py:538 is only reached when uvicorn
    # imports the app directly (bypassing main) — we deliberately do NOT do that.
    exe = PROJECT_ROOT / ".venv" / "Scripts" / "pvian-gateway.exe"
    if not exe.exists():
        raise SystemExit(
            f"pvian-gateway.exe not found at {exe}. Install the package "
            f"(`uv pip install -e .`) or run via the venv."
        )
    cmd = [str(exe), "--host", host, "--port", str(port)]
    _log(f"starting gateway: {' '.join(cmd)}")
    _log(f"  upstream_timeout_seconds={timeout}")
    _log(f"  state_file={state_file}")
    proc = subprocess.Popen(cmd, env=env, cwd=str(PROJECT_ROOT))
    _log(f"  gateway PID={proc.pid}")
    return proc


def stop_gateway(proc: subprocess.Popen, host: str, port: int) -> None:
    """Stop the gateway AND any uvicorn worker it spawned.

    `pvian-gateway.exe` launches a separate uvicorn server worker process, so
    terminating just the exe leaves an orphan listener on the port. We kill the
    process tree (exe + children) and then, as a backstop, kill whatever is
    actually listening on the port if anything still is.
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # Backstop: find and kill any process still listening on the port.
    # On Windows, pvian-gateway.exe's uvicorn worker has a different PID than
    # the exe itself, so Popen's child tracking can miss it.
    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) != 0:
                return  # port already free, nothing to clean up
    except Exception:
        pass  # if the check itself fails, proceed to the forceful cleanup

    _log(f"port {port} still bound after process exit; killing residual listener.")
    try:
        if os.name == "nt":
            # Find the PID owning the port, then taskkill its tree.
            # netstat on Windows may output in the system OEM codepage (GBK on
            # zh-CN), not UTF-8 — decode defensively.
            r = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                timeout=10,
            )
            try:
                text = r.stdout.decode("utf-8")
            except UnicodeDecodeError:
                text = r.stdout.decode("mbcs", errors="replace")
            for line in text.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=10
                    )
                    _log(f"  killed residual listener PID={pid}")
        else:
            r = subprocess.run(
                ["fuser", "-k", f"{port}/tcp"],
                capture_output=True,
                timeout=10,
            )
    except Exception as e:
        _log(f"  residual cleanup warning: {e}")


def wait_ready(host: str, port: int, timeout_s: float = 60.0) -> bool:
    url = f"http://{host}:{port}/readyz"
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    _log("gateway /readyz OK")
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def verify_gateway_end_to_end(host: str, port: int, client_key: str, alias: str) -> None:
    """Send one trivial chat/completions through the gateway to prove the full
    path works (client-key auth -> gateway -> provider key -> upstream -> 200).

    This catches the case where the upstream probe passed but the gateway still
    can't reach upstream (e.g. its own env differs from this launcher's).
    """
    url = f"http://{host}:{port}/v1/chat/completions"
    body = json.dumps(
        {
            "model": alias,
            "messages": [{"role": "user", "content": "reply with the single word: OK"}],
            "max_tokens": 16,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {client_key}",
            "Content-Type": "application/json",
            "x-pvian-logical-call-id": "gateway-launcher-self-check",
        },
    )
    _log(f"gateway end-to-end probe: POST {url} (alias={alias})")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            elapsed = time.perf_counter() - t0
            _log(f"  gateway probe -> {r.status} in {elapsed:.2f}s (full path OK)")
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        body_head = e.read()[:300].decode("utf-8", "replace")
        raise SystemExit(
            f"gateway end-to-end probe FAILED: HTTP {e.code} in {elapsed:.2f}s\n"
            f"  body: {body_head}\n  -> upstream direct probe passed but the gateway "
            f"failed. This points to a gateway-side env mismatch (the gateway process "
            f"did not get the same key). Aborting."
        ) from e


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Unified PVIaN gateway launcher (safe, self-checking)."
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5200)
    ap.add_argument(
        "--state-file",
        default=str(PROJECT_ROOT / "var" / "gateway" / "qualification-state-flash-0731.json"),
    )
    ap.add_argument(
        "--alias",
        default="pvian/bailian-cn-beijing/deepseek-v4-flash-0731",
        help="public alias for the end-to-end self-check probe",
    )
    ap.add_argument(
        "--upstream-timeout",
        type=int,
        default=300,
        help="gateway upstream timeout in seconds (default 300)",
    )
    ap.add_argument(
        "--no-keep",
        action="store_true",
        help="start, verify, then shut down immediately (smoke test)",
    )
    args = ap.parse_args()

    if not ENV_FILE.exists():
        raise SystemExit(f".env not found at {ENV_FILE}")

    _log("=== step 1/4: load .env (override=True) ===")
    load_env_with_override(ENV_FILE)

    _log("=== step 2/4: verify provider key reaches upstream ===")
    verify_provider_key_reaches_upstream()

    _log("=== step 3/4: start gateway ===")
    # Reuse the stable client key from .env (loaded by load_env_with_override)
    # so it survives restarts. Fall back to a one-time generated key only when
    # .env has no usable value, and warn so the operator can persist it.
    client_key = os.environ.get("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", "")
    if not client_key or len(client_key.encode("utf-8")) < 32:
        client_key = generate_client_key()
        _log(
            "WARNING: .env has no stable PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY; "
            "generated a one-time key. Persist it in .env to keep it stable across restarts."
        )
    else:
        _log("using stable client key from .env (PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY)")
    proc = start_gateway(args.host, args.port, args.state_file, client_key, args.upstream_timeout)

    try:
        if not wait_ready(args.host, args.port):
            raise SystemExit(f"gateway did not become ready on {args.host}:{args.port}")
        _log("=== step 4/4: end-to-end self-check through gateway ===")
        verify_gateway_end_to_end(args.host, args.port, client_key, args.alias)

        _log("=" * 60)
        _log("GATEWAY READY AND VERIFIED")
        _log(f"  base url : http://{args.host}:{args.port}")
        _log(f"  client key (give this to the consumer): {client_key}")
        stable = bool(
            os.environ.get("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", "")
            and len(os.environ["PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY"].encode("utf-8")) >= 32
        )
        if stable:
            _log("  (this key is persisted in .env and stable across restarts)")
        else:
            _log("  (one-time key — persist in .env to stabilize)")
        _log(f"  alias    : {args.alias}")
        _log("=" * 60)

        if args.no_keep:
            _log("--no-keep: shutting down after successful self-check.")
            stop_gateway(proc, args.host, args.port)
            return 0

        _log("gateway running in foreground. Ctrl+C to stop.")
        # Keep alive until interrupted.
        while proc.poll() is None:
            time.sleep(1.0)
        rc = proc.returncode
        _log(f"gateway exited with code {rc}")
        return rc
    except KeyboardInterrupt:
        _log("interrupted — stopping gateway.")
        stop_gateway(proc, args.host, args.port)
        return 0
    except BaseException:
        _log("launcher error — stopping gateway.")
        stop_gateway(proc, args.host, args.port)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
