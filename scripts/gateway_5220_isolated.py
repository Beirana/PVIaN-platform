from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from jsonschema import Draft202012Validator

from pvian_gateway.accounting import UsageCollector, estimate, load_pricing, select_quote
from pvian_gateway.aliases import AliasRegistry
from pvian_gateway.profiles import ProbeRequestPolicy, load_profiles
from pvian_gateway.state import QualificationStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / ".codex_tmp" / "gateway-5220"
CAPABILITY_ARTIFACT = (
    PROJECT_ROOT
    / "data"
    / "evidence-artifacts"
    / "provider-capability-sweep-bailian-20260809-v2.json"
)
KIMI_ARTIFACT = (
    PROJECT_ROOT
    / "data"
    / "evidence-artifacts"
    / "provider-capability-targeted-kimi-k3-tool-required-20260809.json"
)
SMOKE_SCHEMA = PROJECT_ROOT / "contracts" / "schemas" / "gateway-5220-smoke.schema.json"
CAPABILITY_SCHEMA = PROJECT_ROOT / "contracts" / "schemas" / "capability-sweep.schema.json"
CLIENT_REGISTRY = PROJECT_ROOT / "config" / "gateway-client-keys.json"
SOURCE_PROFILE_DIR = PROJECT_ROOT / "config" / "provider-profiles"
TARGET_MODELS = (
    "qwen3.8-max",
    "kimi/kimi-k3",
    "glm-5.2",
    "qwen3.7-plus",
    "qwen3-vl-plus",
    "deepseek-v4-pro",
    "MiniMax/MiniMax-M3",
    "xiaomi/mimo-v2.5-pro",
)
PUBLIC_ALIASES = tuple(f"pvian/bailian-cn-beijing/{model}" for model in TARGET_MODELS)
SMOKE_PROMPT = "Reply briefly that the isolated gateway route is available."


class IsolatedGatewayError(RuntimeError):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and optionally live-test gateway port 5220."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--capability-artifact", type=Path, default=CAPABILITY_ARTIFACT)
    parser.add_argument("--kimi-artifact", type=Path, default=KIMI_ARTIFACT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IsolatedGatewayError("cannot read required safe evidence/config JSON") from exc
    if not isinstance(value, dict):
        raise IsolatedGatewayError("required JSON root must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _validate_evidence(
    capability_path: Path, kimi_path: Path
) -> tuple[dict[str, dict[str, Any]], datetime]:
    capability = _read_json(capability_path)
    capability_schema = _read_json(CAPABILITY_SCHEMA)
    if list(Draft202012Validator(capability_schema).iter_errors(capability)):
        raise IsolatedGatewayError("capability artifact failed its contract schema")
    if (
        capability.get("schema_version") != "pvian.capability-sweep/v1"
        or capability.get("sweep_revision") != "v2"
        or capability.get("global_stop") is not None
    ):
        raise IsolatedGatewayError("capability artifact is not a completed v2 sweep")
    results = capability.get("results")
    if not isinstance(results, list) or len(results) != 8:
        raise IsolatedGatewayError("capability artifact must contain exactly eight models")
    by_model: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            raise IsolatedGatewayError("invalid capability model result")
        model = result.get("requested_model")
        if model not in TARGET_MODELS or model in by_model:
            raise IsolatedGatewayError("capability artifact model set is not exact")
        observations = result.get("observations")
        if result.get("catalog_discovered") is not True or not isinstance(observations, list):
            raise IsolatedGatewayError("model lacks catalog/basic capability evidence")
        probes = {
            item.get("probe"): item
            for item in observations
            if isinstance(item, dict) and item.get("probe") in {"message_usage", "stream"}
        }
        if set(probes) != {"message_usage", "stream"}:
            raise IsolatedGatewayError("model lacks exact message and stream observations")
        for probe in probes.values():
            identity = probe.get("reported_model_identity")
            if (
                probe.get("status") != "observed_supported"
                or not isinstance(probe.get("usage"), dict)
                or not isinstance(probe.get("provider_request_id_sha256"), str)
                or not isinstance(identity, dict)
                or identity.get("match") not in {"exact", "explicit_mapping"}
            ):
                raise IsolatedGatewayError("basic capability evidence is not qualification-grade")
        result["_basic_probes"] = probes
        by_model[model] = result
    if set(by_model) != set(TARGET_MODELS):
        raise IsolatedGatewayError("capability artifact model set is incomplete")

    kimi = _read_json(kimi_path)
    validation = kimi.get("validation")
    if (
        kimi.get("schema_version") != "pvian.targeted-capability-probe/v1"
        or kimi.get("requested_model") != "kimi/kimi-k3"
        or kimi.get("completion_calls_attempted") != 2
        or not isinstance(validation, dict)
        or validation.get("passed") is not True
        or validation.get("native_tool_call_valid") is not True
        or validation.get("replay_output_nonempty") is not True
    ):
        raise IsolatedGatewayError("targeted Kimi evidence is not accepted")
    completed = capability.get("completed_at")
    if not isinstance(completed, str):
        raise IsolatedGatewayError("capability evidence timestamp is missing")
    try:
        observed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IsolatedGatewayError("capability evidence timestamp is invalid") from exc
    if observed_at.tzinfo != UTC:
        raise IsolatedGatewayError("capability evidence timestamp must be UTC")
    return by_model, observed_at


def _source_profile_documents() -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(SOURCE_PROFILE_DIR.glob("*.json")):
        value = _read_json(path)
        profile_id = value.get("profile_id")
        if isinstance(profile_id, str):
            result[profile_id] = (path, value)
    return result


def build_overlay(
    root: Path,
    capability_path: Path = CAPABILITY_ARTIFACT,
    kimi_path: Path = KIMI_ARTIFACT,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    if root.exists():
        raise IsolatedGatewayError("isolated gateway root already exists; refusing overwrite")
    basic, observed_at = _validate_evidence(capability_path, kimi_path)
    source = _source_profile_documents()
    source_profiles = load_profiles(SOURCE_PROFILE_DIR)
    for model, evidence in basic.items():
        profile = source_profiles.get(evidence["profile_id"])
        if (
            profile is None
            or profile.profile_version != evidence.get("profile_version")
            or not profile.declares_upstream_model(model)
        ):
            raise IsolatedGatewayError("capability evidence/source profile binding drifted")
    root.mkdir(parents=True)
    profile_dir = root / "profiles"
    profile_dir.mkdir()
    aliases: list[dict[str, str]] = []
    overlay_versions: dict[str, str] = {}

    for model in TARGET_MODELS:
        evidence = basic[model]
        profile_id = evidence["profile_id"]
        if profile_id not in source:
            raise IsolatedGatewayError("evidence profile is missing from source repository")
        source_path, source_value = source[profile_id]
        overlay = json.loads(json.dumps(source_value))
        source_version = str(overlay["profile_version"])
        overlay_version = f"{source_version}-gateway5220.1"
        overlay["profile_version"] = overlay_version
        overlay["lifecycle"] = "active"
        qualification = overlay["qualification"]
        probe_set = qualification["probe_set"]
        if "usage" not in probe_set:
            probe_set.append("usage")
        qualification["enablement"]["required_probes"] = ["message", "stream", "usage"]
        output_path = profile_dir / source_path.name
        output_path.write_text(
            json.dumps(overlay, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        overlay_versions[profile_id] = overlay_version
        endpoint = next(
            item
            for item in overlay["endpoints"]
            if item.get("qualification_enabled") is True
            and item.get("transport") == "openai_compatible_chat_completions"
        )
        aliases.append(
            {
                "alias": f"pvian/bailian-cn-beijing/{model}",
                "profile_id": profile_id,
                "profile_version": overlay_version,
                "endpoint_id": endpoint["endpoint_id"],
                "route_id": f"{endpoint['route_id_prefix']}:{model}",
                "upstream_model": model,
            }
        )

    alias_path = root / "aliases.json"
    alias_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "registry_version": "gateway-5220.1",
                "aliases": aliases,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    profiles = load_profiles(profile_dir)
    AliasRegistry.from_file(alias_path).validate_bindings(profiles)
    state_path = root / "qualification-state.json"
    store = QualificationStateStore(state_path)
    for model in TARGET_MODELS:
        evidence = basic[model]
        profile = profiles[evidence["profile_id"]]
        message = evidence["_basic_probes"]["message_usage"]
        endpoint = next(item for item in profile.endpoints if item.qualification_enabled)
        decision = profile.reported_model_decision(model, message["reported_model"])
        if not decision.accepted:
            raise IsolatedGatewayError("overlay identity decision rejected qualification evidence")
        store.record_qualification(
            route_id=f"{endpoint.route_id_prefix}:{model}",
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            provider_id=profile.provider_id,
            endpoint_id=endpoint.endpoint_id,
            requested_model=model,
            reported_model=message["reported_model"],
            reported_model_identity=decision.as_dict(),
            passed=True,
            capability_summary={
                "message": {"status": "observed_supported"},
                "stream": {"status": "observed_supported"},
                "usage": message["usage"],
            },
            validity_hours=profile.policy.validity_hours,
            failure_threshold=profile.policy.consecutive_failure_threshold,
            observed_at=observed_at,
        )
    store.validate_profile_bindings(profiles)
    enabled = [
        item for item in store.list_records() if item["qualification_status"] == "enabled"
    ]
    expired = any(store.callable_record(item["route_id"], now=now) is None for item in enabled)
    if len(enabled) != 8 or expired:
        raise IsolatedGatewayError("isolated state did not produce eight enabled bindings")
    (root / "usage-attempt-ledger.json").write_text(
        json.dumps(
            {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "pvian.gateway-5220-overlay/v1",
        "port": 5220,
        "source_capability_artifact_sha256": _sha256(capability_path),
        "source_kimi_artifact_sha256": _sha256(kimi_path),
        "profile_versions": overlay_versions,
        "aliases": list(PUBLIC_ALIASES),
        "state_file": "qualification-state.json",
        "ledger_file": "usage-attempt-ledger.json",
        "log_file": "gateway.log",
        "pid_file": "gateway.pid",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def validate_existing_overlay(
    root: Path,
    capability_path: Path = CAPABILITY_ARTIFACT,
    kimi_path: Path = KIMI_ARTIFACT,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "manifest.json")
    if (
        manifest.get("schema_version") != "pvian.gateway-5220-overlay/v1"
        or manifest.get("port") != 5220
        or manifest.get("aliases") != list(PUBLIC_ALIASES)
        or manifest.get("source_capability_artifact_sha256") != _sha256(capability_path)
        or manifest.get("source_kimi_artifact_sha256") != _sha256(kimi_path)
    ):
        raise IsolatedGatewayError("existing isolated overlay provenance is invalid")
    if any((root / name).exists() for name in ("gateway.log", "gateway.pid", "gateway-smoke.json")):
        raise IsolatedGatewayError("existing isolated overlay has prior live artifacts")
    profiles = load_profiles(root / "profiles")
    versions = manifest.get("profile_versions")
    if (
        len(profiles) != 8
        or not isinstance(versions, dict)
        or any(
            profile.lifecycle != "active"
            or profile.profile_version != versions.get(profile.profile_id)
            or profile.policy.required_probes != ("message", "stream", "usage")
            for profile in profiles.values()
        )
    ):
        raise IsolatedGatewayError("existing isolated profile overlay is invalid")
    aliases = AliasRegistry.from_file(root / "aliases.json")
    aliases.validate_bindings(profiles)
    if {item.alias for item in aliases.aliases} != set(PUBLIC_ALIASES):
        raise IsolatedGatewayError("existing isolated alias set is invalid")
    store = QualificationStateStore(root / "qualification-state.json")
    store.validate_profile_bindings(profiles)
    records = store.list_records()
    expired = any(store.callable_record(item["route_id"], now=now) is None for item in records)
    if len(records) != 8 or expired:
        raise IsolatedGatewayError("existing isolated state is expired or invalid")
    ledger = _read_json(root / "usage-attempt-ledger.json")
    if ledger != {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []}:
        raise IsolatedGatewayError("existing isolated pre-live ledger is not empty")
    return manifest


def _active_client_key(environ: dict[str, str]) -> str:
    registry = _read_json(CLIENT_REGISTRY)
    for item in registry.get("keys", []):
        if isinstance(item, dict) and item.get("status") == "active":
            name = item.get("secret_environment_variable")
            if isinstance(name, str) and environ.get(name):
                return environ[name]
    raise IsolatedGatewayError("controlled active gateway client key is unavailable")


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        return handle.connect_ex(("127.0.0.1", port)) != 0


def _stop_exact_process(process: subprocess.Popen[bytes], pid_path: Path) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if pid_path.exists() and pid_path.read_text(encoding="ascii").strip() == str(process.pid):
        pid_path.unlink()


def _safe_request_hash(headers: httpx.Headers, name: str) -> str | None:
    value = headers.get(name)
    if not value or len(value) > 1024:
        return None
    return "sha256:" + hashlib.sha256(f"{name}:{value}".encode()).hexdigest()


def _smoke(root: Path, client_key: str) -> dict[str, Any]:
    quotes = load_pricing(PROJECT_ROOT / "config" / "pricing")
    profiles = load_profiles(root / "profiles")
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=300, follow_redirects=False) as client:
        models_response = client.get(
            "http://127.0.0.1:5220/v1/models",
            headers={"authorization": f"Bearer {client_key}"},
        )
        try:
            model_value = models_response.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            model_value = {}
        ids = {
            item.get("id")
            for item in model_value.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        } if isinstance(model_value, dict) else set()
        if models_response.status_code != 200 or ids != set(PUBLIC_ALIASES):
            raise IsolatedGatewayError("isolated /v1/models did not expose the exact eight aliases")
        for alias, model in zip(PUBLIC_ALIASES, TARGET_MODELS, strict=True):
            profile = next(
                item for item in profiles.values() if item.declares_upstream_model(model)
            )
            request_value = _smoke_request(profile, alias, model)
            try:
                response = client.post(
                    "http://127.0.0.1:5220/v1/chat/completions",
                    headers={"authorization": f"Bearer {client_key}"},
                    json=request_value,
                )
            except httpx.HTTPError:
                decision = profile.reported_model_decision(model, None)
                results.append(
                    {
                        "alias": alias,
                        "requested_model": model,
                        "reported_model": None,
                        "reported_model_identity": decision.as_dict(),
                        "http_status": None,
                        "finish_reason": None,
                        "usage": None,
                        "cost": {"status": "unknown", "reason": "usage_unknown"},
                        "gateway_request_id_sha256": None,
                        "upstream_request_id_sha256": None,
                        "safe_headers": _empty_safe_headers(),
                        "error_category": "transport_error",
                        "status": "inconclusive",
                    }
                )
                continue
            collector = UsageCollector(response.headers.get("content-type"))
            collector.feed(response.content)
            usage = collector.final_usage() if response.status_code == 200 else None
            decision = profile.reported_model_decision(model, collector.reported_model)
            quote = select_quote(quotes, "bailian", "cn-beijing", model)
            result = {
                "alias": alias,
                "requested_model": model,
                "reported_model": collector.reported_model,
                "reported_model_identity": decision.as_dict(),
                "http_status": response.status_code,
                "finish_reason": collector.finish_reason,
                "usage": usage.as_dict() if usage else None,
                "cost": estimate(usage, quote),
                "gateway_request_id_sha256": _safe_request_hash(
                    response.headers, "x-pvian-request-id"
                ),
                "upstream_request_id_sha256": next(
                    (
                        _safe_request_hash(response.headers, name)
                        for name in (
                            "x-request-id",
                            "request-id",
                            "x-dashscope-request-id",
                            "x-acs-request-id",
                        )
                        if response.headers.get(name)
                    ),
                    None,
                ),
                "safe_headers": {
                    name: response.headers.get(name)
                    for name in (
                        "x-pvian-route-id",
                        "x-pvian-profile-id",
                        "x-pvian-profile-version",
                        "x-pvian-endpoint-id",
                        "x-pvian-provider-id",
                        "x-pvian-upstream-model",
                        "x-pvian-public-model",
                        "x-pvian-alias-facade",
                    )
                },
                "error_category": (
                    None
                    if response.status_code == 200
                    and collector.finish_reason == "stop"
                    and usage is not None
                    and decision.accepted
                    else "http_error"
                    if response.status_code != 200
                    else "evidence_incomplete"
                ),
                "status": "observed_supported"
                if response.status_code == 200
                and collector.finish_reason == "stop"
                and usage is not None
                and decision.accepted
                else "inconclusive",
            }
            results.append(result)
    artifact = {
        "schema_version": "pvian.gateway-5220-smoke/v1",
        "evidence_class": "isolated_gateway_smoke",
        "port": 5220,
        "models_http_status": models_response.status_code,
        "completion_calls_attempted": len(results),
        "retry_total_attempts": 1,
        "fallback": None,
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "results": results,
    }
    schema = _read_json(SMOKE_SCHEMA)
    errors = list(Draft202012Validator(schema).iter_errors(artifact))
    if errors:
        raise IsolatedGatewayError("safe gateway smoke artifact failed schema validation")
    (root / "gateway-smoke.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def _smoke_request(profile: Any, alias: str, model: str) -> dict[str, Any]:
    policy = profile.policy.probe_request_policy or ProbeRequestPolicy()
    cap = max(policy.message_max_output_tokens, 2048 if model == "kimi/kimi-k3" else 1024)
    request_value: dict[str, Any] = {
        "model": alias,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
        "stream": False,
        policy.max_output_parameter: cap,
    }
    if policy.temperature is not None:
        request_value["temperature"] = policy.temperature
    if policy.seed is not None:
        request_value["seed"] = policy.seed
    if model == "kimi/kimi-k3":
        variant = next(
            (
                item
                for item in profile.request_variants
                if item.get("variant_id") == "reasoning_effort_max"
            ),
            None,
        )
        overrides = variant.get("request_overrides") if isinstance(variant, dict) else None
        if not isinstance(overrides, dict) or overrides.get("reasoning_effort") != "max":
            raise IsolatedGatewayError("Kimi overlay lacks frozen reasoning_effort=max variant")
        request_value["reasoning_effort"] = "max"
    return request_value


def _empty_safe_headers() -> dict[str, None]:
    return {
        name: None
        for name in (
            "x-pvian-route-id",
            "x-pvian-profile-id",
            "x-pvian-profile-version",
            "x-pvian-endpoint-id",
            "x-pvian-provider-id",
            "x-pvian-upstream-model",
            "x-pvian-public-model",
            "x-pvian-alias-facade",
        )
    }


def _start_and_smoke(root: Path) -> dict[str, Any]:
    if not _port_is_free(5220):
        raise IsolatedGatewayError("port 5220 is already occupied; refusing to disturb it")
    environ = dict(os.environ)
    client_key = _active_client_key(environ)
    environ.update(
        {
            "PVI_GATEWAY_HOST": "127.0.0.1",
            "PVI_GATEWAY_PORT": "5220",
            "PVI_GATEWAY_PROFILE_DIR": str((root / "profiles").resolve()),
            "PVI_GATEWAY_CLIENT_KEY_REGISTRY_FILE": str(CLIENT_REGISTRY.resolve()),
            "PVI_GATEWAY_ALIAS_REGISTRY_FILE": str((root / "aliases.json").resolve()),
            "PVI_GATEWAY_STATE_FILE": str((root / "qualification-state.json").resolve()),
            "PVI_GATEWAY_UPSTREAM_TIMEOUT_SECONDS": "300",
        }
    )
    pid_path = root / "gateway.pid"
    process = _spawn_gateway(root, environ)
    pid_path.write_text(str(process.pid), encoding="ascii")
    try:
        deadline = time.monotonic() + 30
        with httpx.Client(timeout=2) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise IsolatedGatewayError("isolated gateway exited during startup")
                try:
                    if client.get("http://127.0.0.1:5220/readyz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            else:
                raise IsolatedGatewayError("isolated gateway readiness timed out")
        artifact = _smoke(root, client_key)
        if len(artifact["results"]) != 8 or any(
            item["status"] != "observed_supported" for item in artifact["results"]
        ):
            raise IsolatedGatewayError("one or more isolated gateway smokes were inconclusive")
        return artifact
    except Exception:
        _stop_exact_process(process, pid_path)
        raise


def _spawn_gateway(root: Path, environ: dict[str, str]) -> subprocess.Popen[bytes]:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with (root / "gateway.log").open("xb") as log_handle:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pvian_gateway.main",
                "--host",
                "127.0.0.1",
                "--port",
                "5220",
            ],
            cwd=PROJECT_ROOT,
            env=environ,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )


def main() -> int:
    arguments = _arguments()
    if arguments.live and (not arguments.allow_network or not arguments.confirm_cost):
        raise SystemExit("live gateway start/smoke requires --allow-network and --confirm-cost")
    if not arguments.live and (arguments.allow_network or arguments.confirm_cost):
        raise SystemExit("live flags require --live")
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        capability_path = arguments.capability_artifact.resolve()
        kimi_path = arguments.kimi_artifact.resolve()
        manifest = (
            validate_existing_overlay(arguments.root, capability_path, kimi_path)
            if arguments.live and arguments.root.exists()
            else build_overlay(arguments.root, capability_path, kimi_path)
        )
        artifact = _start_and_smoke(arguments.root.resolve()) if arguments.live else None
    except IsolatedGatewayError as exc:
        raise SystemExit(str(exc)) from None
    print(
        json.dumps(
            {
                "status": "live_ready" if artifact is not None else "overlay_built",
                "port": manifest["port"],
                "aliases": len(manifest["aliases"]),
                "completion_calls_attempted": (
                    artifact["completion_calls_attempted"] if artifact else 0
                ),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
