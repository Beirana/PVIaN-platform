from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from pvian_gateway.aliases import AliasRegistry
from pvian_gateway.profiles import load_profiles
from pvian_gateway.state import QualificationStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROFILE_DIR = PROJECT_ROOT / "config" / "provider-profiles"
SOURCE_STATE_FILE = PROJECT_ROOT / "var" / "gateway-isolated" / "deepseek-official-state.json"
CLIENT_REGISTRY = PROJECT_ROOT / "config" / "gateway-client-keys.json"
PORT = 5230
OVERLAY_VERSION_SUFFIX = "-gateway5230.1"
TARGET_MODELS = ("deepseek-v4-pro", "deepseek-v4-flash")
PUBLIC_ALIASES = tuple(f"pvian/deepseek-official/{model}" for model in TARGET_MODELS)
SMOKE_PROMPT = "Reply briefly that the isolated gateway route is available."


class IsolatedGatewayError(RuntimeError):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and smoke an isolated DeepSeek-official gateway overlay on 5230."
    )
    parser.add_argument(
        "--root", type=Path, default=PROJECT_ROOT / ".codex_tmp" / "gateway-5230-deepseek"
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument(
        "--keep-running", action="store_true",
        help="Keep the isolated child alive after a fully successful smoke.",
    )
    return parser.parse_args()


def _require_live_authorization(arguments: argparse.Namespace) -> None:
    if not arguments.allow_network or not arguments.confirm_cost:
        raise SystemExit("live smoke requires --allow-network and --confirm-cost")


def build_overlay(root: Path, now: datetime | None = None) -> dict[str, Any]:
    root = root.resolve()
    if root.exists():
        raise IsolatedGatewayError("isolated gateway root already exists; refusing overwrite")

    source_profiles = {
        model: _overlay_profile_document(model)
        for model in TARGET_MODELS
    }
    source_state = json.loads(SOURCE_STATE_FILE.read_text(encoding="utf-8"))
    records = source_state.get("records", {})
    if isinstance(records, list):
        records = {item["route_id"]: item for item in records}

    root.mkdir(parents=True)
    profile_dir = root / "profiles"
    profile_dir.mkdir()
    versions: dict[str, str] = {}
    aliases: list[dict[str, str]] = []
    for model in TARGET_MODELS:
        record = records.get(f"deepseek-official:{model}")
        if record is None or record.get("qualification_status") != "enabled":
            raise IsolatedGatewayError(f"isolated qualification state missing for {model}")
        document = source_profiles[model]
        overlay_version = document["profile_version"]
        versions[document["profile_id"]] = overlay_version
        (profile_dir / f"deepseek-official-{model}.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        aliases.append(
            {
                "alias": f"pvian/deepseek-official/{model}",
                "profile_id": document["profile_id"],
                "profile_version": overlay_version,
                "endpoint_id": "endpoint-deepseek-official-openai-compatible",
                "route_id": f"deepseek-official:{model}",
                "upstream_model": model,
            }
        )

    alias_path = root / "aliases.json"
    alias_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "registry_version": "gateway-5230.1",
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
        record = records[f"deepseek-official:{model}"]
        document = source_profiles[model]
        observed_at = datetime.fromisoformat(record["qualified_at"].replace("Z", "+00:00"))
        store.record_qualification(
            route_id=f"deepseek-official:{model}",
            profile_id=document["profile_id"],
            profile_version=document["profile_version"],
            provider_id=document["provider"]["provider_id"],
            endpoint_id="endpoint-deepseek-official-openai-compatible",
            requested_model=model,
            reported_model=record["reported_model"],
            reported_model_identity=record["reported_model_identity"],
            passed=True,
            capability_summary=record.get("capabilities", {}),
            validity_hours=document["qualification"]["enablement"]["validity_hours"],
            failure_threshold=document["qualification"]["enablement"]["consecutive_failure_threshold"],
            observed_at=observed_at,
        )
    store.validate_profile_bindings(profiles)
    enabled = [item for item in store.list_records() if item["qualification_status"] == "enabled"]
    expired = any(store.callable_record(item["route_id"], now=now) is None for item in enabled)
    if len(enabled) != len(TARGET_MODELS) or expired:
        raise IsolatedGatewayError("isolated state did not produce enabled bindings")

    manifest = {
        "schema_version": "pvian.gateway-5230-overlay/v1",
        "port": PORT,
        "aliases": list(PUBLIC_ALIASES),
        "profile_versions": versions,
        "source_state_file": str(SOURCE_STATE_FILE),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _overlay_profile_document(model: str) -> dict[str, Any]:
    stem = "deepseek-official-v4-pro" if model.endswith("pro") else "deepseek-official-v4-flash"
    raw = json.loads((SOURCE_PROFILE_DIR / f"{stem}.json").read_text(encoding="utf-8"))
    raw["lifecycle"] = "active"
    raw["profile_version"] = raw["profile_version"] + OVERLAY_VERSION_SUFFIX
    return raw


def _child_env() -> dict[str, str]:
    from dotenv import dotenv_values

    env = {key: value for key, value in os.environ.items()}
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    for name in (
        "PVI_PROBE_DEEPSEEK_API_KEY",
        "PVI_PROBE_DEEPSEEK_OPENAI_BASE_URL",
        "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY",
    ):
        value = env.get(name, "").strip() or (file_values.get(name) or "").strip()
        if not value:
            raise IsolatedGatewayError(f"required credential env var is missing: {name}")
        env[name] = value
    root = (PROJECT_ROOT / ".codex_tmp" / "gateway-5230-deepseek").resolve()
    env.update(
        {
            "PVI_GATEWAY_HOST": "127.0.0.1",
            "PVI_GATEWAY_PORT": str(PORT),
            "PVI_GATEWAY_PROFILE_DIR": str(root / "profiles"),
            "PVI_GATEWAY_ALIAS_REGISTRY_FILE": str(root / "aliases.json"),
            "PVI_GATEWAY_STATE_FILE": str(root / "qualification-state.json"),
            "PVI_GATEWAY_CLIENT_KEY_REGISTRY_FILE": str(CLIENT_REGISTRY),
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
        }
    )
    return env


def _wait_ready(client: httpx.Client, process: subprocess.Popen, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise IsolatedGatewayError("isolated gateway child exited during startup")
        try:
            response = client.get("/healthz")
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1.0)
    raise IsolatedGatewayError("isolated gateway did not become ready in time")


def _smoke(client: httpx.Client, bearer: str) -> list[dict[str, Any]]:
    ready = client.get("/readyz")
    if ready.status_code != 200:
        raise IsolatedGatewayError("isolated gateway /readyz failed")
    models = client.get("/v1/models", headers={"authorization": f"Bearer {bearer}"})
    if models.status_code != 200:
        raise IsolatedGatewayError("isolated gateway /v1/models rejected the client key")
    discovered = {item["id"] for item in models.json().get("data", [])}
    if discovered != set(PUBLIC_ALIASES):
        raise IsolatedGatewayError(f"isolated /v1/models mismatch: {sorted(discovered)}")

    results: list[dict[str, Any]] = []
    for alias in PUBLIC_ALIASES:
        body = {
            "model": alias,
            "messages": [{"role": "user", "content": SMOKE_PROMPT}],
            "max_tokens": 64,
            "stream": False,
        }
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {bearer}"},
            json=body,
            timeout=300.0,
        )
        if response.status_code != 200:
            raise IsolatedGatewayError(f"alias {alias} completion returned {response.status_code}")
        payload = response.json()
        reported = payload.get("model")
        usage = payload.get("usage")
        finish = (payload.get("choices") or [{}])[0].get("finish_reason")
        if reported not in TARGET_MODELS or not isinstance(usage, dict):
            raise IsolatedGatewayError(f"alias {alias} returned incomplete evidence")
        results.append(
            {
                "alias": alias,
                "reported_model": reported,
                "finish_reason": finish,
                "usage": usage,
                "http_status": response.status_code,
            }
        )
    return results


def main() -> int:
    arguments = _arguments()
    _require_live_authorization(arguments)
    root = arguments.root.resolve()
    manifest = build_overlay(root)
    log_path = root / "gateway.log"
    log_handle = log_path.open("wb")
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "uvicorn",
            "pvian_gateway.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    (root / "gateway.pid").write_text(str(process.pid) + "\n", encoding="utf-8")
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30.0) as client:
            _wait_ready(client, process)
            from dotenv import dotenv_values

            bearer = dotenv_values(PROJECT_ROOT / ".env").get(
                "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", ""
            ).strip()
            if not bearer:
                raise IsolatedGatewayError("client key missing for smoke")
            results = _smoke(client, bearer)
        artifact = {
            "schema_version": "pvian.gateway-5230-smoke/v1",
            "evidence_class": "deployed_gateway_smoke",
            "port": PORT,
            "child_pid": process.pid,
            "manifest": manifest,
            "completion_calls": len(results),
            "results": results,
            "smoked_at": datetime.now(UTC).isoformat(),
        }
        (root / "gateway-smoke.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"status": "smoke_ok", "results": results}, ensure_ascii=False))
        return 0
    except Exception:
        process.terminate()
        process.wait(timeout=30)
        raise
    finally:
        if not arguments.keep_running:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
