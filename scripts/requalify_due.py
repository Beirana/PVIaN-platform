#!/usr/bin/env python3
"""Daily automatic requalification scheduler for the PVIaN gateway.

Runs ``pvi-probe qualify --due --allow-network --confirm-cost`` to requalify
routes whose qualification TTL is expiring. The gateway's state store reads
the state file directly from disk on every request (state.py:281-292), so a
successful requalification here makes the alias callable again WITHOUT
restarting the gateway — hot reload is free.

This script is designed to run as a Windows Task Scheduler daily job (04:00
local time). It is NOT run in CI.

USAGE
-----
  python scripts/requalify_due.py            # normal scheduled run
  python scripts/requalify_due.py --dry-run  # show due routes without calling upstream

KEY DESIGN POINTS (per workorder task D):
  - qualify subcommand has no --state-file flag; the state path is injected
    via the PVI_GATEWAY_STATE_FILE environment variable so the requalifier
    and the gateway read/write the SAME file (task A.0 unification).
  - The qualify CLI exit code is ambiguous: 0 = all enabled, 2 = no due
    routes OR some failed. This script parses stdout JSON (mode + results)
    to distinguish the two cases.
  - Alerting is debounced: a single day's upstream outage should not spam.
    Consecutive failures are counted in a small state file; alerts fire only
    after 2 consecutive days of failure.
  - Frozen per-model cost budgets (config/requalification-budgets.json) gate
    each route; if a budget is exceeded the route is skipped and logged.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
STATE_FILE_DEFAULT = PROJECT_ROOT / "var" / "gateway" / "qualification-state-flash-0731.json"
BUDGETS_FILE = PROJECT_ROOT / "config" / "requalification-budgets.json"
LOG_DIR = PROJECT_ROOT / "var" / "gateway"
FAILURE_TRACKER = LOG_DIR / "requalify-consecutive-failures.json"
GATEWAY_SETTINGS_ENV = "PVI_GATEWAY_STATE_FILE"

ALERT_THRESHOLD = 2  # consecutive days of failure before alerting


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[requalify_due {ts}] {msg}", flush=True)


def _load_env_state_file() -> Path:
    """Resolve the state file path the same way the gateway does."""
    # os.environ first (Task Scheduler may inject it), then .env, then default.
    raw = os.environ.get(GATEWAY_SETTINGS_ENV)
    if not raw and ENV_FILE.exists():
        try:
            from dotenv import dotenv_values
            raw = dotenv_values(ENV_FILE).get(GATEWAY_SETTINGS_ENV)
        except ImportError:
            pass
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p
    return STATE_FILE_DEFAULT


def _query_due_routes(state_path: Path) -> list[dict]:
    """Return routes that are due for requalification (expired or degraded)."""
    try:
        from pvian_gateway.state import QualificationStateStore
    except ImportError:
        _log("ERROR: cannot import pvian_gateway.state — package not installed?")
        return []
    store = QualificationStateStore(state_path)
    return store.due_records()


def _load_budgets() -> dict[str, dict]:
    """Return {profile_id: budget_dict} from the frozen budgets file."""
    if not BUDGETS_FILE.exists():
        return {}
    try:
        data = json.loads(BUDGETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {b["profile_id"]: b for b in data.get("budgets", [])}


def _route_within_budget(route: dict, budgets: dict[str, dict]) -> bool:
    """Check whether a due route's profile has a frozen budget."""
    profile_id = route.get("profile_id", "")
    if profile_id not in budgets:
        _log(f"  WARNING: no frozen budget for {profile_id} — skipping"
             f" (add to {BUDGETS_FILE.name}).")
        return False
    return True


def _run_qualify(state_path: Path) -> tuple[int, dict]:
    """Run pvi-probe qualify --due and parse stdout JSON.

    Returns (exit_code, parsed_json_or_empty).
    """
    env = os.environ.copy()
    # CRITICAL: inject the state file path via env var, not --state-file
    # (the qualify subcommand does not accept --state-file; only repair-state does).
    env[GATEWAY_SETTINGS_ENV] = str(state_path.resolve())

    cmd = [
        sys.executable, "-m", "pvi.probe_runner.cli",
        "qualify", "--due",
        "--allow-network", "--confirm-cost",
    ]
    _log(f"running: {' '.join(cmd)}")
    _log(f"  {GATEWAY_SETTINGS_ENV}={state_path}")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    _log(f"  exit code: {proc.returncode}")
    if proc.stderr.strip():
        _log(f"  stderr: {proc.stderr.strip()[:500]}")
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _log(f"  WARNING: could not parse stdout as JSON (first 300 chars): {proc.stdout[:300]}")
        parsed = {}
    return proc.returncode, parsed


def _read_failure_count() -> int:
    if not FAILURE_TRACKER.exists():
        return 0
    try:
        return int(json.loads(FAILURE_TRACKER.read_text()).get("count", 0))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0


def _write_failure_count(count: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    FAILURE_TRACKER.write_text(json.dumps({"count": count}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily PVIaN gateway requalification scheduler.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show due routes and budgets without calling upstream.")
    args = ap.parse_args(argv)

    rc_production = _production_pass(args)
    rc_overlay = _overlay_renewal_pass(args.dry_run)
    rc_anthropic = _anthropic_renewal_pass(args.dry_run)
    if rc_production != 0 or rc_overlay != 0 or rc_anthropic != 0:
        return 1
    return 0


def _production_pass(args: argparse.Namespace) -> int:
    state_path = _load_env_state_file()
    _log(f"state file: {state_path}")

    due = _query_due_routes(state_path)
    _log(f"due routes: {len(due)}")
    if not due:
        _log("no routes due for requalification. Done.")
        _write_failure_count(0)  # reset debounce on success
        return 0

    budgets = _load_budgets()
    for route in due:
        route_id = route.get("route_id", "?")
        profile_id = route.get("profile_id", "?")
        within = _route_within_budget(route, budgets)
        _log(f"  {route_id} (profile={profile_id}): budget_ok={within}")

    if args.dry_run:
        _log("--dry-run: stopping before upstream calls.")
        return 0

    # Gate: if ANY due route has no budget, abort the whole run (fail-closed).
    ungated = [r for r in due if not _route_within_budget(r, budgets)]
    if ungated:
        _log(f"ABORT: {len(ungated)} route(s) have no frozen budget — fail-closed. "
             f"Add them to {BUDGETS_FILE.name} or remove the route from state.")
        return 1

    rc, parsed = _run_qualify(state_path)

    # Parse the JSON output to disambiguate exit code 2.
    results = parsed.get("results", [])
    mode = parsed.get("mode", "")
    all_enabled = results and all(r.get("qualification_status") == "enabled" for r in results)

    if rc == 0 or (rc == 2 and not results):
        # 0 = all enabled; 2 with empty results = nothing was due (race).
        _log(f"requalification OK (mode={mode}, {len(results)} route(s) rechecked).")
        _write_failure_count(0)
        return 0

    if rc == 2 and results and not all_enabled:
        failed = [r for r in results if r.get("qualification_status") != "enabled"]
        _log(f"requalification PARTIAL FAILURE: {len(failed)} route(s) not enabled:")
        for r in failed:
            _log(f"  {r.get('route_id')}: {r.get('qualification_status')}")

    consecutive = _read_failure_count() + 1
    _write_failure_count(consecutive)
    if consecutive >= ALERT_THRESHOLD:
        _log(f"ALERT: {consecutive} consecutive day(s) of requalification failure. "
             f"Check upstream provider status and gateway state.")
    else:
        _log(f"failure {consecutive}/{ALERT_THRESHOLD} — debounced (will alert if it persists).")
    return 1 if not all_enabled else 0


def _overlay_renewal_pass(dry_run: bool) -> int:
    """Renew the isolated 5245 gateway state (atomic swap, no process touch).

    Runs after the production pass; a failure here flips the overall exit
    code but never undoes the production result. See
    scripts/requalify_aliyun_5245.py for the renewal contract.
    """
    import importlib.util

    script = PROJECT_ROOT / "scripts" / "requalify_aliyun_5245.py"
    spec = importlib.util.spec_from_file_location("requalify_aliyun_5245", script)
    if spec is None or spec.loader is None:
        _log("overlay renewal: cannot load requalify_aliyun_5245.py — skipping.")
        return 1
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return int(module.renew(dry_run=dry_run))
    except SystemExit as exc:
        _log(f"overlay renewal: FAILED ({exc}).")
        return 1
    except Exception as exc:  # noqa: BLE001 - scheduler must not crash silently
        _log(f"overlay renewal: ERROR {type(exc).__name__}: {exc}")
        return 1


ANTHROPIC_PROFILE_IDS = {"provider-aigocode-anthropic-claude-sonnet-5"}


def _anthropic_renewal_pass(dry_run: bool) -> int:
    """Renew anthropic_native_messages routes via the wire-format prober.

    The OpenAI qualifier skips this transport, so due anthropic routes are
    renewed here with scripts/qualify_anthropic_surface.py writing directly
    into the production state (hot-reloaded by the gateway per request).
    Budget membership is enforced fail-closed like the production pass.
    """
    state_path = _load_env_state_file()
    due = [r for r in _query_due_routes(state_path) if r.get("profile_id") in ANTHROPIC_PROFILE_IDS]
    if not due:
        return 0
    _log(f"anthropic renewal: {len(due)} route(s) due")
    budgets = _load_budgets()
    ungated = [r for r in due if r.get("profile_id") not in budgets]
    if ungated:
        _log("ABORT: anthropic route(s) have no frozen budget — fail-closed.")
        return 1
    if dry_run:
        _log("--dry-run: stopping before anthropic upstream calls.")
        return 0
    script = PROJECT_ROOT / "scripts" / "qualify_anthropic_surface.py"
    cmd = [
        sys.executable, str(script),
        "--state-file", str(state_path),
        "--allow-network", "--confirm-cost",
    ]
    _log(f"running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"anthropic renewal: ERROR {type(exc).__name__}: {exc}")
        return 1
    _log(f"  exit code: {proc.returncode}")
    if proc.stderr.strip():
        _log(f"  stderr: {proc.stderr.strip()[:500]}")
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
