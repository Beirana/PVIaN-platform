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
CLIENT_REGISTRY = PROJECT_ROOT / "config" / "gateway-client-keys.json"
ISOLATED_DIR = PROJECT_ROOT / "var" / "gateway-isolated"
PORT = 5231
OVERLAY_VERSION_SUFFIX = "-gateway5231.1"

# (profile source file stem, exact model, isolated qualification state file)
UNIFIED_BATCH = (
    ("deepseek-official-v4-pro", "deepseek-v4-pro", "deepseek-official-state.json"),
    ("deepseek-official-v4-flash", "deepseek-v4-flash", "deepseek-official-state.json"),
    ("moonshot-official-kimi-k3", "kimi-k3", "moonshot-official-state.json"),
    (
        "openrouter-dots-3-note-free",
        "dots-studio/dots-3-note-preview:free",
        "openrouter-state.json",
    ),
)
CREDENTIAL_VARS = (
    "PVI_PROBE_DEEPSEEK_API_KEY",
    "PVI_PROBE_DEEPSEEK_OPENAI_BASE_URL",
    "PVI_PROBE_MOONSHOT_API_KEY",
    "PVI_PROBE_MOONSHOT_OPENAI_BASE_URL",
    "PVI_PROBE_OPENROUTER_API_KEY",
    "PVI_PROBE_OPENROUTER_OPENAI_BASE_URL",
    "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY",
)
SMOKE_PROMPT = "Reply briefly that the isolated gateway route is available."


class IsolatedGatewayError(RuntimeError):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and smoke the unified multi-provider isolated gateway on 5231."
    )
    parser.add_argument(
        "--root", type=Path, default=PROJECT_ROOT / ".codex_tmp" / "gateway-5231-unified-batch"
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    return parser.parse_args()


def _require_live_authorization(arguments: argparse.Namespace) -> None:
    if not arguments.allow_network or not arguments.confirm_cost:
        raise SystemExit("live smoke requires --allow-network and --confirm-cost")


def build_overlay(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if root.exists():
        raise IsolatedGatewayError("isolated gateway root already exists; refusing overwrite")

    sources = []
    for stem, model, state_name in UNIFIED_BATCH:
        raw = json.loads((SOURCE_PROFILE_DIR / f"{stem}.json").read_text(encoding="utf-8"))
        raw["lifecycle"] = "active"
        raw["profile_version"] = raw["profile_version"] + OVERLAY_VERSION_SUFFIX
        state = json.loads((ISOLATED_DIR / state_name).read_text(encoding="utf-8"))
        records = state.get("records", {})
        if isinstance(records, list):
            records = {item["route_id"]: item for item in records}
        record = records.get(f"{raw['endpoints'][0]['route_id_prefix']}:{model}")
        if record is None or record.get("qualification_status") != "enabled":
            raise IsolatedGatewayError(f"isolated qualification state missing for {model}")
        sources.append((raw, model, record))

    root.mkdir(parents=True)
    profile_dir = root / "profiles"
    profile_dir.mkdir()
    aliases: list[dict[str, str]] = []
    versions: dict[str, str] = {}
    for raw, model, _ in sources:
        (profile_dir / f"{raw['profile_id']}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        versions[raw["profile_id"]] = raw["profile_version"]
        if "deepseek" in raw["profile_id"]:
            namespace = "deepseek-official"
        elif "moonshot" in raw["profile_id"]:
            namespace = "moonshot-official"
        else:
            namespace = "openrouter"
        aliases.append(
            {
                "alias": f"pvian/{namespace}/{model}",
                "profile_id": raw["profile_id"],
                "profile_version": raw["profile_version"],
                "endpoint_id": raw["endpoints"][0]["endpoint_id"],
                "route_id": f"{raw['endpoints'][0]['route_id_prefix']}:{model}",
                "upstream_model": model,
            }
        )

    alias_path = root / "aliases.json"
    alias_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "registry_version": "gateway-5231.1",
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
    for raw, model, record in sources:
        observed_at = datetime.fromisoformat(record["qualified_at"].replace("Z", "+00:00"))
        store.record_qualification(
            route_id=f"{raw['endpoints'][0]['route_id_prefix']}:{model}",
            profile_id=raw["profile_id"],
            profile_version=raw["profile_version"],
            provider_id=raw["provider"]["provider_id"],
            endpoint_id=raw["endpoints"][0]["endpoint_id"],
            requested_model=model,
            reported_model=record["reported_model"],
            reported_model_identity=record["reported_model_identity"],
            passed=True,
            capability_summary=record.get("capabilities", {}),
            validity_hours=raw["qualification"]["enablement"]["validity_hours"],
            failure_threshold=raw["qualification"]["enablement"]["consecutive_failure_threshold"],
            observed_at=observed_at,
        )
    store.validate_profile_bindings(profiles)
    enabled = [item for item in store.list_records() if item["qualification_status"] == "enabled"]
    if len(enabled) != len(UNIFIED_BATCH) or any(
        store.callable_record(item["route_id"]) is None for item in enabled
    ):
        raise IsolatedGatewayError("isolated state did not produce enabled bindings")

    expected_aliases = [item["alias"] for item in aliases]
    manifest = {
        "schema_version": "pvian.gateway-5231-overlay/v1",
        "port": PORT,
        "aliases": expected_aliases,
        "profile_versions": versions,
        "source_state_files": sorted({name for _, _, name in UNIFIED_BATCH}),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _child_env(root: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    env = {key: value for key, value in os.environ.items()}
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    for name in CREDENTIAL_VARS:
        value = env.get(name, "").strip() or (file_values.get(name) or "").strip()
        if not value:
            raise IsolatedGatewayError(f"required credential env var is missing: {name}")
        env[name] = value
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
            if client.get("/healthz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1.0)
    raise IsolatedGatewayError("isolated gateway did not become ready in time")


def _smoke(client: httpx.Client, bearer: str, expected_aliases: list[str]) -> list[dict[str, Any]]:
    if client.get("/readyz").status_code != 200:
        raise IsolatedGatewayError("isolated gateway /readyz failed")
    models = client.get("/v1/models", headers={"authorization": f"Bearer {bearer}"})
    if models.status_code != 200:
        raise IsolatedGatewayError("isolated gateway /v1/models rejected the client key")
    discovered = {item["id"] for item in models.json().get("data", [])}
    if discovered != set(expected_aliases):
        raise IsolatedGatewayError(f"isolated /v1/models mismatch: {sorted(discovered)}")

    results: list[dict[str, Any]] = []
    for alias in expected_aliases:
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {bearer}"},
            json={
                "model": alias,
                "messages": [{"role": "user", "content": SMOKE_PROMPT}],
                "max_tokens": 64,
                "stream": False,
            },
            timeout=300.0,
        )
        if response.status_code != 200:
            raise IsolatedGatewayError(f"alias {alias} completion returned {response.status_code}")
        payload = response.json()
        reported = payload.get("model")
        usage = payload.get("usage")
        finish = (payload.get("choices") or [{}])[0].get("finish_reason")
        if not isinstance(reported, str) or not isinstance(usage, dict):
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
    log_handle = (root / "gateway.log").open("wb")
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
        env=_child_env(root),
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
            results = _smoke(client, bearer, manifest["aliases"])
        artifact = {
            "schema_version": "pvian.gateway-5231-smoke/v1",
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
