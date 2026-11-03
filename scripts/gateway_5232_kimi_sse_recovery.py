"""Build and run an isolated Kimi nonstream-to-SSE recovery gateway on 5232.

Only the Kimi ``0.1.0-gateway5231.3`` overlay opts into the process-local
adapter.  The consumer body remains ``stream=false`` and is rebuilt directly
from the frozen S schedule/package; the adapter changes only the upstream
transport body to ``stream=true`` and aggregates the complete SSE response.
The live mode is bounded to one exact completion and never writes S records.
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / ".codex_tmp" / "gateway-kimi-5231-recovery-20260810"
ROOT_DEFAULT = PROJECT_ROOT / ".codex_tmp" / "gateway-kimi-5232-sse-20260810"
PORT = 5232
PROFILE_ID = "provider-bailian-cn-beijing-kimi-k3"
PROFILE_VERSION = "0.1.0-gateway5231.3"
PUBLIC_ALIAS = "pvian/bailian-cn-beijing/kimi/kimi-k3"
ROUTE_ID = "bailian-cn-beijing:kimi/kimi-k3"
UPSTREAM_MODEL = "kimi/kimi-k3"
TARGET_CALL_ID = "s-sb-002-S30-H-CYBER-004-E0I0-r1-p0"
CLIENT_KEY_FILE = SOURCE_ROOT / "consumer-client-key.secret"
S_EXPERIMENT_ROOT = Path(
    r"C:\path\to\consumer-experiments"
)
S_RUNTIME = S_EXPERIMENT_ROOT / (
    "docs/design/s_branch_sandbagging/mvp_30plus30_v0_3_0_factorial_r1/runtime"
)
S_SCHEDULE = S_RUNTIME.parent / "condition_schedule_720.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _profile() -> dict[str, Any]:
    source = _load_json(SOURCE_ROOT / "profiles" / f"{PROFILE_ID}.json")
    source["profile_version"] = PROFILE_VERSION
    source["lifecycle"] = "active"
    endpoint = source["endpoints"][0]
    endpoint["upstream_timeout_seconds"] = 600
    endpoint["internal_stream_aggregation"] = "sse_to_nonstream_v1"
    return source


def _aliases() -> dict[str, Any]:
    return {
        "schema_version": "pvian.public-model-aliases/v1",
        "registry_version": "gateway-kimi-5232-sse-20260810.1",
        "aliases": [
            {
                "alias": PUBLIC_ALIAS,
                "profile_id": PROFILE_ID,
                "profile_version": PROFILE_VERSION,
                "endpoint_id": "endpoint-bailian-cn-beijing-openai-compatible",
                "route_id": ROUTE_ID,
                "upstream_model": UPSTREAM_MODEL,
            }
        ],
    }


def _make_state() -> dict[str, Any]:
    value = _load_json(SOURCE_ROOT / "qualification-state.json")
    record = value["records"][ROUTE_ID]
    record = dict(record)
    record["profile_version"] = PROFILE_VERSION
    record["request_count"] = 0
    record["known_usage_attempts"] = 0
    record["consecutive_failures"] = 0
    record["last_success_at"] = None
    record["last_failure_at"] = None
    record["last_failure_category"] = None
    record["last_observed_at"] = None
    record["usage_totals"] = {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    record["revalidation_due"] = False
    record["operational_status"] = "nominal"
    return {"schema_version": value["schema_version"], "records": {ROUTE_ID: record}}


def _load_s_modules() -> tuple[Any, Any]:
    runtime = str(S_RUNTIME)
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    import s_factorial_model_extension as model_extension
    import s_factorial_runner as runner

    return runner, model_extension


def _exact_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    runner, model_extension = _load_s_modules()
    calls = runner.load_frozen_schedule(S_SCHEDULE)
    call = next((item for item in calls if item["logical_call_id"] == TARGET_CALL_ID), None)
    if call is None:
        raise RuntimeError("target_call_missing_from_frozen_schedule")
    items = {
        item["package_item_id"]: item
        for item in runner.load_frozen_package(model_extension.REPO_ROOT)
    }
    item = items.get(call["item_id"])
    if item is None:
        raise RuntimeError("target_item_missing_from_frozen_package")
    for key in ("question_hash", "choices_hash", "selection_hash"):
        if item[key] != call[key]:
            raise RuntimeError("frozen_request_asset_hash_drift")
    system = runner.read_factor_block(S_RUNTIME.parent / "prompt_common_system.txt")
    e1 = runner.read_factor_block(S_RUNTIME.parent / "factor_e1_block.txt")
    i1 = runner.read_factor_block(S_RUNTIME.parent / "factor_i1_block.txt")
    question = runner.render_question_block(item["question"], item["choices"])
    messages = runner.compose_prompt(system, e1, i1, question, call["E"], call["I"])
    spec = model_extension.MODEL_SPECS["kimi_k3"]
    parameters = dict(spec["parameters"])
    if parameters != {
        "stream": False,
        "reasoning_effort": "max",
        "max_completion_tokens": 32768,
    }:
        raise RuntimeError("kimi_formal_parameter_contract_drift")
    consumer_body = {
        "model": spec["model_alias"],
        "messages": [
            {"role": "system", "content": messages["system"]},
            {"role": "user", "content": messages["user"]},
        ],
        **parameters,
    }
    upstream_body = dict(consumer_body)
    upstream_body["model"] = UPSTREAM_MODEL
    upstream_stream_body = dict(upstream_body)
    upstream_stream_body["stream"] = True
    diff = {
        key: {"before": upstream_body.get(key), "after": upstream_stream_body.get(key)}
        for key in set(upstream_body) | set(upstream_stream_body)
        if upstream_body.get(key) != upstream_stream_body.get(key)
    }
    if set(diff) != {"stream"}:
        raise RuntimeError("adapter_body_diff_is_not_stream_only")
    metadata = {
        "logical_call_id": TARGET_CALL_ID,
        "item_id": call["item_id"],
        "question_hash": call["question_hash"],
        "choices_hash": call["choices_hash"],
        "selection_hash": call["selection_hash"],
        "prompt_render_hash": runner.render_prompt_hash(messages),
        "schedule_sha256": runner.sha256_path(S_SCHEDULE),
        "package_sha256": runner.sha256_path(
            model_extension.REPO_ROOT / runner.FROZEN_PACKAGE_RELATIVE
        ),
        "consumer_body_sha256": runner.digest(consumer_body),
        "upstream_nonstream_body_sha256": runner.digest(upstream_body),
        "upstream_stream_body_sha256": runner.digest(upstream_stream_body),
        "body_diff": {"stream": {"before": False, "after": True}},
    }
    return consumer_body, metadata


def build_offline(root: Path) -> dict[str, Any]:
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"recovery root must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    consumer_body, payload_metadata = _exact_payload()
    _write_json(root / "profiles" / f"{PROFILE_ID}.json", _profile())
    _write_json(root / "aliases.json", _aliases())
    _write_json(root / "qualification-state.json", _make_state())
    _write_json(
        root / "usage-attempt-ledger.json",
        {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []},
    )
    plan = {
        "schema_version": "pvian.kimi-sse-aggregation-plan/v1",
        "created_at": _now(),
        "port": PORT,
        "public_alias": PUBLIC_ALIAS,
        "route_id": ROUTE_ID,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "adapter_mode": "sse_to_nonstream_v1",
        "consumer_parameters": {
            "stream": False,
            "reasoning_effort": "max",
            "max_completion_tokens": 32768,
        },
        "upstream_mutation": {"stream": {"before": False, "after": True}},
        "completion_budget": 1,
        "retry_total_attempts": 1,
        "fallback": None,
        "runtime_token_hard_cap": None,
        "runtime_cost_hard_cap": None,
        "exact_payload_metadata": payload_metadata,
        "s_records_write": False,
        "source_5231_read_only": True,
        "stop_on_any_failure": True,
    }
    _write_json(root / "sse-aggregation-plan.json", plan)
    return {
        "status": "offline_ready",
        "root": str(root.resolve()),
        "profile_version": PROFILE_VERSION,
        "port": PORT,
        "completion_budget": 1,
        "target_logical_call_id": TARGET_CALL_ID,
        "payload_metadata": payload_metadata,
        "consumer_payload_values_loaded_in_memory": True,
        "consumer_payload_persisted": False,
        "consumer_body_field_count": len(consumer_body),
    }


def _secret() -> str:
    value = CLIENT_KEY_FILE.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise RuntimeError("consumer_secret_file_invalid")
    return value


def _start(root: Path, secret: str) -> subprocess.Popen[Any]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str((PROJECT_ROOT / "src").resolve()),
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
    log_path = root / "gateway.log"
    err_path = root / "gateway.err"
    with log_path.open("ab") as log, err_path.open("ab") as err:
        process = subprocess.Popen(
            [
                sys.executable,
                "scripts/kimi_sse_gateway_entrypoint.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    (root / "gateway.pid").write_text(str(process.pid) + "\n", encoding="ascii")
    return process


def _wait(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("isolated_gateway_health_timeout")


def _probe(root: Path, secret: str) -> dict[str, Any]:
    body, payload_metadata = _exact_payload()
    started = time.monotonic()
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "x-pvian-logical-call-id": TARGET_CALL_ID,
    }
    with httpx.Client(timeout=700.0) as client:
        models = client.get(
            f"http://127.0.0.1:{PORT}/v1/models", headers={"Authorization": f"Bearer {secret}"}
        )
        if models.status_code != 200:
            raise RuntimeError("models_preflight_failed")
        ids = {item.get("id") for item in models.json().get("data", []) if isinstance(item, dict)}
        if ids != {PUBLIC_ALIAS}:
            raise RuntimeError("exact_alias_preflight_failed")
        response = client.post(
            f"http://127.0.0.1:{PORT}/v1/chat/completions",
            headers=headers,
            json=body,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    request_id = response.headers.get("x-pvian-request-id")
    response_body: dict[str, Any] = {}
    if response.headers.get("content-type", "").startswith("application/json"):
        try:
            value = response.json()
            if isinstance(value, dict):
                response_body = value
        except ValueError:
            pass
    choices = response_body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    usage = response_body.get("usage")
    if not isinstance(message, dict):
        message = {}
    result = {
        "schema_version": "pvian.kimi-sse-aggregation-evidence/v1",
        "evidence_class": "deployed_gateway_sse_aggregation_probe",
        "recorded_at": _now(),
        "status": "success" if response.status_code == 200 else "failed",
        "route_id": ROUTE_ID,
        "public_alias": PUBLIC_ALIAS,
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "adapter_mode": "sse_to_nonstream_v1",
        "sse_termination": response.headers.get("x-pvian-sse-termination"),
        "logical_call_id": TARGET_CALL_ID,
        "http_status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "front_door_classification": (
            "completed_before_300s"
            if response.status_code == 200 and elapsed_ms < 300000
            else "failed_or_after_300s"
        ),
        "requested_model": PUBLIC_ALIAS,
        "reported_model": response_body.get("model"),
        "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        "usage": usage if isinstance(usage, dict) else None,
        "content_length": len(message.get("content") or "")
        if isinstance(message.get("content"), str)
        else 0,
        "content_sha256": (
            "sha256:" + hashlib.sha256(message["content"].encode("utf-8")).hexdigest()
            if isinstance(message.get("content"), str)
            else None
        ),
        "message_field_names": sorted(message),
        "gateway_request_id_sha256": (
            "sha256:" + hashlib.sha256(request_id.encode()).hexdigest() if request_id else None
        ),
        "payload_metadata": payload_metadata,
        "raw_body_persisted": False,
        "s_records_written": False,
    }
    if (
        response.status_code != 200
        or result["reported_model"] != UPSTREAM_MODEL
        or result["finish_reason"] != "stop"
        or not isinstance(usage, dict)
        or result["content_length"] == 0
    ):
        _write_json(root / "sse-aggregation-evidence.json", result)
        raise RuntimeError("aggregated_response_contract_failed")
    _write_json(root / "sse-aggregation-evidence.json", result)
    return result


def run_live(root: Path) -> dict[str, Any]:
    plan = _load_json(root / "sse-aggregation-plan.json")
    if plan.get("completion_budget") != 1 or plan.get("upstream_mutation") != {
        "stream": {"before": False, "after": True}
    }:
        raise RuntimeError("aggregation_plan_drifted")
    secret = _secret()
    process = _start(root, secret)
    try:
        _wait(f"http://127.0.0.1:{PORT}/healthz")
        _wait(f"http://127.0.0.1:{PORT}/readyz")
        result = _probe(root, secret)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    evidence_path = root / "sse-aggregation-evidence.json"
    return {
        "status": result["status"],
        "base_url": f"http://127.0.0.1:{PORT}",
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "evidence_path": str(evidence_path.resolve()),
        "evidence_sha256": _sha(evidence_path),
        "process_pid": process.pid,
        "service_kept_running": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway-5232-kimi-sse-recovery")
    parser.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.live:
        try:
            print(json.dumps(build_offline(root), ensure_ascii=False, sort_keys=True))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error_category": type(exc).__name__}))
            return 1
    if not (args.allow_network and args.confirm_cost):
        print(json.dumps({"status": "blocked", "completion_calls": 0}))
        return 2
    try:
        print(json.dumps(run_live(root), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_category": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
