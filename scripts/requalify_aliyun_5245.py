#!/usr/bin/env python3
"""Renew the isolated 5245 gateway's qualification state without touching it.

The 5245 aliyun-token-plan gateway runs from a frozen overlay whose records
bind to suffixed profile versions (``-gateway5245.1``). Renewal therefore
cannot write the overlay state directly with the qualifier: this script

  1. copies the current overlay state into a temporary state file,
  2. runs ``pvi-probe qualify --due`` against the copy (only routes inside
     the 24h renewal window / expired / degraded are refreshed; budget
     membership is enforced fail-closed before any upstream call),
  3. rewrites the refreshed records' profile_version back to the overlay
     suffix and validates bindings against the overlay profiles,
  4. atomically swaps the overlay state file with ``os.replace``.

The running gateway re-reads the state file on every request, so the renewed
TTLs go live on the next call — the process is never restarted or stopped
(operator constraint 2026-08-20: the 5245 service must not be touched).

USAGE
-----
  python scripts/requalify_aliyun_5245.py            # scheduled renewal
  python scripts/requalify_aliyun_5245.py --dry-run  # show due routes only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
BUDGETS_FILE = PROJECT_ROOT / "config" / "requalification-budgets.json"
OVERLAY_ROOT = PROJECT_ROOT / ".codex_tmp" / "gateway-5245-aliyun-token-plan"
OVERLAY_STATE = OVERLAY_ROOT / "qualification-state.json"
OVERLAY_PROFILE_DIR = OVERLAY_ROOT / "profiles"
TEMP_ROOT = PROJECT_ROOT / ".codex_tmp" / "aliyun-5245-renewal"
GATEWAY_SETTINGS_ENV = "PVI_GATEWAY_STATE_FILE"
OVERLAY_VERSION_SUFFIX = "-gateway5245.1"
PROFILE_STEMS = (
    "aliyun-token-plan-qwen3.8-max",
    "aliyun-token-plan-qwen3.7-max",
    "aliyun-token-plan-qwen3.6-flash",
)


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[requalify_5245 {ts}] {msg}", flush=True)


def _profile_id(stem: str) -> str:
    # Config file stems map onto provider profile ids by swapping dots for
    # dashes in the model segment, e.g. aliyun-token-plan-qwen3.8-max ->
    # provider-aliyun-token-plan-qwen3-8-max.
    model = stem.removeprefix("aliyun-token-plan-")
    return f"provider-aliyun-token-plan-{model.replace('.', '-')}"


def _load_budgets() -> set[str]:
    if not BUDGETS_FILE.exists():
        return set()
    try:
        data = json.loads(BUDGETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {b["profile_id"] for b in data.get("budgets", [])}


def _run_qualify(temp_state: Path) -> tuple[int, dict]:
    env = os.environ.copy()
    env[GATEWAY_SETTINGS_ENV] = str(temp_state.resolve())
    # Scope the run to the aliyun profiles only: the temp state holds just the
    # overlay's own records, so an unscoped --due would treat every OTHER
    # production route as unknown-and-due and requalify them redundantly
    # (observed once on 2026-08-21; qualify only sees the temp state).
    cmd = [
        sys.executable, "-m", "pvi.probe_runner.cli",
        "qualify", "--due",
        *[f"--profile-id={_profile_id(stem)}" for stem in PROFILE_STEMS],
        "--allow-network", "--confirm-cost",
    ]
    _log(f"running: {' '.join(cmd)}")
    _log(f"  {GATEWAY_SETTINGS_ENV}={temp_state}")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    _log(f"  exit code: {proc.returncode}")
    if proc.stderr.strip():
        _log(f"  stderr: {proc.stderr.strip()[:500]}")
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _log(f"  WARNING: could not parse stdout as JSON (first 300 chars): {proc.stdout[:300]}")
        parsed = {}
    return proc.returncode, parsed


def _apply_overlay_suffix(state_path: Path) -> None:
    """Rewrite renewed aliyun records to bind to the overlay profile versions."""
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    records = payload.get("records", {})
    if isinstance(records, list):
        raise SystemExit("unexpected state shape: records is a list")
    changed = 0
    for record in records.values():
        if record.get("profile_id") in {_profile_id(s) for s in PROFILE_STEMS}:
            version = record.get("profile_version", "")
            if not version.endswith(OVERLAY_VERSION_SUFFIX):
                record["profile_version"] = version + OVERLAY_VERSION_SUFFIX
                changed += 1
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    state_path.write_text(text, encoding="utf-8")
    _log(f"suffix rewrite: {changed} record(s) bound to overlay versions")


def _validate_overlay_state(state_path: Path) -> None:
    from pvian_gateway.profiles import load_profiles
    from pvian_gateway.state import QualificationStateStore

    profiles = load_profiles(OVERLAY_PROFILE_DIR)
    store = QualificationStateStore(state_path)
    store.validate_profile_bindings(profiles)
    enabled = [r for r in store.list_records() if r["qualification_status"] == "enabled"]
    callable_routes = [r["route_id"] for r in enabled if store.callable_record(r["route_id"])]
    _log(f"validation: {len(enabled)} enabled, {len(callable_routes)} callable")
    if not callable_routes:
        raise SystemExit("overlay state has no callable routes after renewal — refusing swap")


def renew(dry_run: bool = False) -> int:
    if not OVERLAY_STATE.exists():
        _log(f"overlay state missing ({OVERLAY_STATE}) — 5245 not deployed; nothing to renew.")
        return 0

    from pvian_gateway.state import QualificationStateStore

    overlay_store = QualificationStateStore(OVERLAY_STATE)
    due = overlay_store.due_records()
    _log(f"overlay due routes (24h window): {len(due)}")
    if not due:
        _log("no overlay routes due. Done.")
        return 0
    for route in due:
        _log(f"  {route.get('route_id')} (profile={route.get('profile_id')})")

    budgets = _load_budgets()
    ungated = [r for r in due if r.get("profile_id") not in budgets]
    if ungated:
        _log(f"ABORT: {len(ungated)} overlay route(s) have no frozen budget — fail-closed.")
        return 1
    if dry_run:
        _log("--dry-run: stopping before upstream calls.")
        return 0

    if TEMP_ROOT.exists():
        shutil.rmtree(TEMP_ROOT)
    TEMP_ROOT.mkdir(parents=True)
    temp_state = TEMP_ROOT / "qualification-state.json"
    shutil.copyfile(OVERLAY_STATE, temp_state)

    rc, parsed = _run_qualify(temp_state)
    results = parsed.get("results", [])
    all_enabled = bool(results) and all(
        r.get("qualification_status") == "enabled" for r in results
    )
    if not (rc == 0 or (rc == 2 and not results) or (rc == 2 and all_enabled)):
        _log(f"overlay requalification FAILED (rc={rc}) — overlay state left untouched.")
        return 1

    _apply_overlay_suffix(temp_state)
    _validate_overlay_state(temp_state)
    os.replace(temp_state, OVERLAY_STATE)
    _log(f"overlay state atomically swapped: {OVERLAY_STATE} (gateway picks it up per request).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Renew the isolated 5245 gateway state in place.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show due overlay routes and budgets without calling upstream.")
    args = ap.parse_args(argv)
    return renew(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
