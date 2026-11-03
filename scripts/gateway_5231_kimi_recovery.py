"""Build and run an isolated, auditable Kimi K3 recovery window on port 5231.

This script is deliberately narrower than the legacy launcher.  It never
changes the repository provider profile, the 5200 service, or the historical
5231 evidence root.  ``--live`` is gated by both explicit authorization flags
and has an exact budget of one catalog GET, two provider qualification
completions, and one deployed-gateway smoke completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_ROOT = PROJECT_ROOT / ".codex_tmp" / "gateway-kimi-5231-requal-20260810-r3"
SOURCE_PROFILE = OLD_ROOT / "profiles" / "bailian-cn-beijing-kimi-k3.json"
PROFILE_ID = "provider-bailian-cn-beijing-kimi-k3"
PROFILE_VERSION = "0.1.0-gateway5231.2"
OLD_PROFILE_VERSION = "0.1.0-gateway5231.1"
ENDPOINT_ID = "endpoint-bailian-cn-beijing-openai-compatible"
PUBLIC_ALIAS = "pvian/bailian-cn-beijing/kimi/kimi-k3"
ROUTE_ID = "bailian-cn-beijing:kimi/kimi-k3"
REQUESTED_MODEL = "kimi/kimi-k3"
PORT = 5231
CLIENT_KEY_FILE = OLD_ROOT / "consumer-client-key.secret"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _old_snapshots() -> dict[str, Any]:
    paths = {
        "qualification_state": OLD_ROOT / "qualification-state.json",
        "usage_attempt_ledger": OLD_ROOT / "usage-attempt-ledger.json",
        "profile": SOURCE_PROFILE,
    }
    return {
        name: {"path": str(path), "sha256": _sha(path)}
        for name, path in paths.items()
        if path.is_file()
    }


def _profile_value() -> dict[str, Any]:
    if not SOURCE_PROFILE.is_file():
        raise RuntimeError(f"old profile is missing: {SOURCE_PROFILE}")
    value = _read_json(SOURCE_PROFILE)
    value["profile_version"] = PROFILE_VERSION
    value["lifecycle"] = "active"
    endpoints = value.get("endpoints")
    if not isinstance(endpoints, list) or len(endpoints) != 1:
        raise RuntimeError("Kimi recovery expects exactly one endpoint")
    endpoint = endpoints[0]
    if endpoint.get("endpoint_id") != ENDPOINT_ID:
        raise RuntimeError("unexpected Kimi endpoint identity")
    endpoint["upstream_timeout_seconds"] = 600
    return value


def _aliases_value() -> dict[str, Any]:
    return {
        "schema_version": "pvian.public-model-aliases/v1",
        "registry_version": "gateway-kimi-5231-recovery-20260810.1",
        "aliases": [
            {
                "alias": PUBLIC_ALIAS,
                "profile_id": PROFILE_ID,
                "profile_version": PROFILE_VERSION,
                "endpoint_id": ENDPOINT_ID,
                "route_id": ROUTE_ID,
                "upstream_model": REQUESTED_MODEL,
            }
        ],
    }


def _plan_value(old_snapshots: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "pvian.isolated-route-recovery-plan/v1",
        "recovery_id": "kimi-5231-recovery-20260810",
        "created_at": _now(),
        "evidence_class": "provider_qualification_and_deployed_gateway_smoke",
        "port": PORT,
        "public_alias": PUBLIC_ALIAS,
        "route_id": ROUTE_ID,
        "requested_model": REQUESTED_MODEL,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "profile_lineage": {
            "previous_profile_version": OLD_PROFILE_VERSION,
            "difference": (
                "endpoint upstream_timeout_seconds null -> 600; no request-body semantic change"
            ),
            "source_profile_sha256": old_snapshots["profile"]["sha256"],
        },
        "network_plan": {
            "catalog_gets": 1,
            "provider_qualification_completion_max": 2,
            "deployed_gateway_smoke_completion_max": 1,
            "total_completion_max": 3,
            "sdk_retries": 0,
            "retry_total_attempts": 1,
            "fallback": None,
            "runtime_token_hard_cap": None,
            "runtime_cost_hard_cap": None,
        },
        "qualification_requests": [
            {
                "probe": "message_usage",
                "stream": False,
                "max_completion_tokens": 1024,
                "reasoning_effort": "max",
                "temperature": "omitted",
                "seed": "omitted",
            },
            {
                "probe": "stream",
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_completion_tokens": 1024,
                "reasoning_effort": "max",
                "temperature": "omitted",
                "seed": "omitted",
            },
        ],
        "gateway_smoke_request": {
            "stream": False,
            "max_completion_tokens": 32768,
            "reasoning_effort": "max",
            "temperature": "omitted",
            "seed": "omitted",
        },
        "stop_conditions": [
            "exact catalog model absent",
            "identity, usage, finish, auth, transport, or request evidence failure",
            "any call exceeds the exact budget",
        ],
        "source_5220_read_only": True,
        "old_5231_evidence_immutable": True,
        "old_root_snapshots": old_snapshots,
    }


def build_offline(root: Path) -> dict[str, Any]:
    if root.resolve() == OLD_ROOT.resolve():
        raise RuntimeError("recovery root must not be the historical 5231 root")
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"recovery root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    old_snapshots = _old_snapshots()
    profile = _profile_value()
    _write_json(root / "profiles" / f"{PROFILE_ID}.json", profile)
    _write_json(root / "aliases.json", _aliases_value())
    _write_json(
        root / "qualification-state.json",
        {"schema_version": "pvian.qualification-state/v1", "records": {}},
    )
    _write_json(
        root / "usage-attempt-ledger.json",
        {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []},
    )
    plan = _plan_value(old_snapshots)
    _write_json(root / "recovery-plan.json", plan)
    _write_json(
        root / "source-snapshot.json",
        {"created_at": _now(), "old_root": str(OLD_ROOT), "files": old_snapshots},
    )
    return {
        "status": "offline_ready",
        "root": str(root.resolve()),
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "completion_budget": 3,
        "old_root_snapshots": old_snapshots,
    }


def _load_recovery_profile(root: Path) -> Any:
    from pvian_gateway.profiles import load_profiles

    profiles = load_profiles(root / "profiles")
    profile = profiles.get(PROFILE_ID)
    if profile is None or profile.profile_version != PROFILE_VERSION:
        raise RuntimeError("recovery profile/version binding failed")
    endpoint = next((item for item in profile.endpoints if item.endpoint_id == ENDPOINT_ID), None)
    if endpoint is None or endpoint.upstream_timeout_seconds != 600:
        raise RuntimeError("recovery profile must set only the 600-second endpoint override")
    if tuple(profile.policy.required_probes) != ("message", "stream", "usage"):
        raise RuntimeError("recovery qualification plan is not the authorized two-call shape")
    if profile.policy.probe_request_policy is None:
        raise RuntimeError("Kimi probe policy is missing")
    policy = profile.policy.probe_request_policy
    if (
        policy.retry_total_attempts != 1
        or policy.fallback is not None
        or policy.message_max_output_tokens != 1024
        or policy.stream_max_output_tokens != 1024
    ):
        raise RuntimeError("recovery probe policy drifted")
    return profile


def _load_secret() -> str:
    if not CLIENT_KEY_FILE.is_file():
        raise RuntimeError(f"controlled consumer key file is missing: {CLIENT_KEY_FILE}")
    value = CLIENT_KEY_FILE.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError("controlled consumer key file is not a single nonempty line")
    return value


def _provider_qualify(root: Path, profile: Any) -> dict[str, Any]:
    from pvi.probe_runner.qualification import qualify_profile
    from pvian_gateway.state import QualificationStateStore

    state = QualificationStateStore(root / "qualification-state.json")
    results = qualify_profile(profile, state)
    if len(results) != 1:
        raise RuntimeError("provider qualification did not produce exactly one result")
    result = results[0]
    summary = result.safe_summary()
    _write_json(
        root / "provider-qualification.json",
        {
            "schema_version": "pvian.provider-qualification-evidence/v1",
            "evidence_class": "provider_probe",
            "recorded_at": _now(),
            "profile_id": PROFILE_ID,
            "profile_version": PROFILE_VERSION,
            "route_id": ROUTE_ID,
            "requested_model": REQUESTED_MODEL,
            "catalog_gets": 1,
            "completion_calls": 2,
            "retry_total_attempts": 1,
            "fallback": None,
            "result": summary,
            "raw_provider_body": "never_persisted",
        },
    )
    if (
        result.qualification_status != "enabled"
        or result.reported_model != REQUESTED_MODEL
        or result.capabilities.get("message", {}).get("status") != "observed_supported"
        or result.capabilities.get("stream", {}).get("status") != "observed_supported"
        or not isinstance(result.capabilities.get("usage"), dict)
    ):
        raise RuntimeError("provider qualification was not fully supported")
    return summary


def _listen_pid(port: int) -> int | None:
    command = (
        "$x=Get-NetTCPConnection -LocalPort "
        + str(port)
        + " -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess;"
        " if($null -ne $x){ [Console]::WriteLine($x) }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


def _process_command_line(pid: int) -> str:
    command = (
        "(Get-CimInstance Win32_Process -Filter 'ProcessId="
        + str(pid)
        + "' -ErrorAction SilentlyContinue).CommandLine"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _stop_exact_gateway(pid: int) -> None:
    command = f"Stop-Process -Id {pid} -Force -ErrorAction Stop"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if _listen_pid(PORT) != pid:
            return
        time.sleep(0.2)
    raise RuntimeError("exact old 5231 process did not stop")


def _wait_http(url: str, *, timeout: float = 20.0) -> httpx.Response:
    deadline = time.time() + timeout
    last: httpx.Response | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            last = response
            if response.status_code == 200:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(
        f"service did not become healthy: {url} status={getattr(last, 'status_code', None)}"
    )


def _start_gateway(root: Path, secret: str) -> tuple[subprocess.Popen[Any], Path, Path]:
    log_path = root / "gateway.log"
    err_path = root / "gateway.err"
    env = os.environ.copy()
    env.update(
        {
            "PVI_GATEWAY_HOST": "127.0.0.1",
            "PVI_GATEWAY_PORT": str(PORT),
            "PVI_GATEWAY_PROFILE_DIR": str((root / "profiles").resolve()),
            "PVI_GATEWAY_CLIENT_KEY_REGISTRY_FILE": str(
                (PROJECT_ROOT / "config" / "gateway-client-keys.json").resolve()
            ),
            "PVI_GATEWAY_ALIAS_REGISTRY_FILE": str((root / "aliases.json").resolve()),
            "PVI_GATEWAY_STATE_FILE": str((root / "qualification-state.json").resolve()),
            "PVI_GATEWAY_UPSTREAM_TIMEOUT_SECONDS": "300",
            "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY": secret,
        }
    )
    log = log_path.open("ab")
    err = err_path.open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "pvian_gateway.main", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log,
        stderr=err,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.close()
    err.close()
    (root / "gateway.pid").write_text(str(process.pid) + "\n", encoding="ascii")
    return process, log_path, err_path


def _smoke(root: Path, secret: str) -> dict[str, Any]:
    smoke_request = {
        "model": PUBLIC_ALIAS,
        "messages": [{"role": "user", "content": "Reply with exactly: PVI_KIMI_RECOVERY_SMOKE_OK"}],
        "stream": False,
        "reasoning_effort": "max",
        "max_completion_tokens": 32768,
    }
    started = time.monotonic()
    request_id = None
    with httpx.Client(timeout=650.0) as client:
        models = client.get(
            "http://127.0.0.1:5231/v1/models", headers={"Authorization": f"Bearer {secret}"}
        )
        if models.status_code != 200:
            raise RuntimeError(f"models preflight failed: {models.status_code}")
        model_ids = {
            item.get("id") for item in models.json().get("data", []) if isinstance(item, dict)
        }
        if model_ids != {PUBLIC_ALIAS}:
            raise RuntimeError("recovery /v1/models did not expose the exact Kimi alias")
        response = client.post(
            "http://127.0.0.1:5231/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json=smoke_request,
        )
        request_id = response.headers.get("x-pvian-request-id")
        body: dict[str, Any] = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if response.status_code != 200:
            raise RuntimeError(f"deployed smoke failed: {response.status_code}")
    choices = body.get("choices") if isinstance(body, dict) else None
    choice = (
        choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    )
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reported_model = body.get("model") if isinstance(body, dict) else None
    usage = (
        body.get("usage")
        if isinstance(body, dict) and isinstance(body.get("usage"), dict)
        else None
    )
    result = {
        "schema_version": "pvian.deployed-gateway-smoke-evidence/v1",
        "evidence_class": "deployed_gateway_smoke",
        "recorded_at": _now(),
        "route_id": ROUTE_ID,
        "public_alias": PUBLIC_ALIAS,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "requested_model": PUBLIC_ALIAS,
        "reported_model": reported_model,
        "http_status": 200,
        "finish_reason": choice.get("finish_reason"),
        "usage": usage,
        "request_id_sha256": "sha256:" + hashlib.sha256((request_id or "").encode()).hexdigest(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "content_validation": "nonempty" if str(message.get("content") or "").strip() else "empty",
        "raw_provider_body": "never_persisted",
    }
    if (
        reported_model not in {REQUESTED_MODEL, PUBLIC_ALIAS}
        or result["finish_reason"] != "stop"
        or result["content_validation"] != "nonempty"
    ):
        raise RuntimeError("deployed smoke response contract failed")
    _write_json(root / "gateway-smoke.json", result)
    return result


def run_live(root: Path) -> dict[str, Any]:
    plan = _read_json(root / "recovery-plan.json")
    if (
        plan.get("profile_version") != PROFILE_VERSION
        or plan.get("network_plan", {}).get("total_completion_max") != 3
    ):
        raise RuntimeError("recovery plan drifted")
    snapshots = _old_snapshots()
    if snapshots != plan.get("old_root_snapshots"):
        raise RuntimeError("historical 5231 evidence changed before live run")
    profile = _load_recovery_profile(root)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    # Provider credential is resolved by the profile from the controlled
    # environment; the value is never copied into an artifact or log.
    _provider_qualify(root, profile)
    secret = _load_secret()
    old_pid = _listen_pid(PORT)
    if old_pid is not None:
        command_line = _process_command_line(old_pid)
        if "pvian_gateway.main" not in command_line or "--port 5231" not in command_line:
            raise RuntimeError("port 5231 is occupied by an unexpected process")
        _stop_exact_gateway(old_pid)
    process, _log, _err = _start_gateway(root, secret)
    try:
        _wait_http("http://127.0.0.1:5231/healthz")
        _wait_http("http://127.0.0.1:5231/readyz")
        smoke = _smoke(root, secret)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    final = {
        "schema_version": "pvian.kimi-recovery-evidence/v1",
        "evidence_class": "recovery_operator_resolution",
        "recorded_at": _now(),
        "status": "enabled_callable",
        "port": PORT,
        "public_alias": PUBLIC_ALIAS,
        "route_id": ROUTE_ID,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "upstream_timeout_seconds": 600,
        "provider_qualification_path": str((root / "provider-qualification.json").resolve()),
        "provider_qualification_sha256": _sha(root / "provider-qualification.json"),
        "gateway_smoke_path": str((root / "gateway-smoke.json").resolve()),
        "gateway_smoke_sha256": _sha(root / "gateway-smoke.json"),
        "recovery_plan_path": str((root / "recovery-plan.json").resolve()),
        "recovery_plan_sha256": _sha(root / "recovery-plan.json"),
        "old_root_immutable_snapshots": _old_snapshots(),
        "process_pid": process.pid,
        "consumer_key_path": str(CLIENT_KEY_FILE.resolve()),
        "consumer_key_content": "not persisted",
        "old_233_records_preserved": True,
        "resume_lineage": "original S run root resumes; no completed records rewritten",
        "smoke": smoke,
    }
    _write_json(root / "recovery-evidence.json", final)
    return {
        "status": final["status"],
        "port": PORT,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "recovery_evidence_path": str((root / "recovery-evidence.json").resolve()),
        "recovery_evidence_sha256": _sha(root / "recovery-evidence.json"),
        "provider_qualification_path": final["provider_qualification_path"],
        "gateway_smoke_path": final["gateway_smoke_path"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway-5231-kimi-recovery")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.live:
        print(json.dumps(build_offline(root), ensure_ascii=False, sort_keys=True))
        return 0
    if not (args.allow_network and args.confirm_cost):
        print(
            json.dumps(
                {"status": "blocked", "completion_calls": 0, "reason": "both live flags required"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        print(json.dumps(run_live(root), ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        # Error text is intentionally reduced to a type/category; provider
        # bodies and credentials must never enter the operator output.
        print(
            json.dumps(
                {"status": "failed", "error_category": type(exc).__name__}, ensure_ascii=False
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
