from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pvian_gateway.aliases import AliasRegistry
from pvian_gateway.profiles import load_profiles
from pvian_gateway.state import QualificationStateStore

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / "contracts" / "schemas" / "gateway-5220-smoke.schema.json").read_text(
        encoding="utf-8"
    )
)


def _module() -> Any:
    path = ROOT / "scripts" / "gateway_5220_isolated.py"
    spec = importlib.util.spec_from_file_location("test_gateway_5220_isolated_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_compiles_eight_active_profiles_aliases_and_bound_state(tmp_path: Path) -> None:
    module = _module()
    source_before = {
        path.name: path.read_bytes()
        for path in (ROOT / "config" / "provider-profiles").glob("*.json")
    }
    root = tmp_path / "gateway-5220"
    # The overlay state derives its TTL from the frozen 2026-08-09 evidence
    # artifacts; pin the clock inside that window so the test stays deterministic.
    frozen_now = datetime(2026, 8, 10, tzinfo=UTC)
    manifest = module.build_overlay(root, now=frozen_now)
    profiles = load_profiles(root / "profiles")
    assert len(profiles) == 8
    assert all(profile.lifecycle == "active" for profile in profiles.values())
    assert all(profile.profile_version.endswith("-gateway5220.1") for profile in profiles.values())
    assert all(
        profile.policy.required_probes == ("message", "stream", "usage")
        for profile in profiles.values()
    )
    aliases = AliasRegistry.from_file(root / "aliases.json")
    aliases.validate_bindings(profiles)
    assert {item.alias for item in aliases.aliases} == set(module.PUBLIC_ALIASES)
    assert all("flash" not in item.alias and "yunwu" not in item.alias for item in aliases.aliases)
    store = QualificationStateStore(root / "qualification-state.json")
    store.validate_profile_bindings(profiles)
    records = store.list_records()
    assert len(records) == 8
    assert all(item["qualification_status"] == "enabled" for item in records)
    assert all(
        store.callable_record(item["route_id"], now=frozen_now) is not None for item in records
    )
    mimo = next(item for item in records if item["requested_model"] == "xiaomi/mimo-v2.5-pro")
    assert mimo["reported_model"] == "mimo-v2.5-pro"
    assert mimo["reported_model_identity"]["match"] == "explicit_mapping"
    assert set(mimo["capabilities"]) == {"message", "stream", "usage"}
    ledger = json.loads((root / "usage-attempt-ledger.json").read_text(encoding="utf-8"))
    assert ledger == {"schema_version": "pvian.usage-attempt-ledger/v1", "attempts": []}
    assert manifest["port"] == 5220
    assert source_before == {
        path.name: path.read_bytes()
        for path in (ROOT / "config" / "provider-profiles").glob("*.json")
    }

    kimi_profile = next(
        profile for profile in profiles.values() if profile.declares_upstream_model("kimi/kimi-k3")
    )
    kimi_request = module._smoke_request(
        kimi_profile,
        "pvian/bailian-cn-beijing/kimi/kimi-k3",
        "kimi/kimi-k3",
    )
    assert kimi_request["reasoning_effort"] == "max"
    assert kimi_request[kimi_profile.policy.probe_request_policy.max_output_parameter] >= 2048
    assert module.validate_existing_overlay(root, now=frozen_now) == manifest


def test_build_refuses_to_overwrite_an_existing_isolated_root(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "gateway-5220"
    root.mkdir()
    marker = root / "owned.txt"
    marker.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(module.IsolatedGatewayError, match="refusing overwrite"):
        module.build_overlay(root)
    assert marker.read_text(encoding="utf-8") == "do not overwrite"


def test_profile_version_drift_fails_before_creating_overlay_root(tmp_path: Path) -> None:
    module = _module()
    capability = json.loads(module.CAPABILITY_ARTIFACT.read_text(encoding="utf-8"))
    capability["results"][0]["profile_version"] = "drifted-version"
    artifact_path = tmp_path / "drifted-capability.json"
    artifact_path.write_text(json.dumps(capability), encoding="utf-8")
    root = tmp_path / "gateway-5220"
    with pytest.raises(module.IsolatedGatewayError, match="binding drifted"):
        module.build_overlay(root, artifact_path, module.KIMI_ARTIFACT)
    assert not root.exists()


def test_live_mode_requires_both_network_and_cost_flags_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(sys, "argv", ["gateway_5220_isolated.py", "--live"])
    with pytest.raises(SystemExit, match="requires --allow-network and --confirm-cost"):
        module.main()


def test_spawn_uses_python_module_5220_create_no_window_and_no_secret_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 522099

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.sys, "platform", "win32")
    process = module._spawn_gateway(tmp_path, {"CONTROLLED_SECRET": "not-in-argv"})
    assert process.pid == 522099
    assert captured["command"] == [
        sys.executable,
        "-m",
        "pvian_gateway.main",
        "--host",
        "127.0.0.1",
        "--port",
        "5220",
    ]
    assert captured["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert "not-in-argv" not in " ".join(captured["command"])


def test_exact_process_stop_never_targets_an_unrelated_pid(tmp_path: Path) -> None:
    module = _module()
    pid_path = tmp_path / "gateway.pid"
    pid_path.write_text("5200", encoding="ascii")

    class FakeProcess:
        pid = 5220
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int) -> int:
            return 0

    process = FakeProcess()
    module._stop_exact_process(process, pid_path)
    assert process.terminated is True
    assert pid_path.read_text(encoding="ascii") == "5200"


def test_smoke_schema_rejects_prompt_output_raw_body_and_auth_fields() -> None:
    artifact = _synthetic_smoke_artifact()
    Draft202012Validator(SCHEMA).validate(artifact)
    for forbidden in ("prompt", "output", "raw_body", "authorization"):
        unsafe = copy.deepcopy(artifact)
        unsafe["results"][0][forbidden] = "sensitive"
        assert list(Draft202012Validator(SCHEMA).iter_errors(unsafe))


def _synthetic_smoke_artifact() -> dict[str, Any]:
    module = _module()
    results = []
    for alias, model in zip(module.PUBLIC_ALIASES, module.TARGET_MODELS, strict=True):
        results.append(
            {
                "alias": alias,
                "requested_model": model,
                "reported_model": "mimo-v2.5-pro" if model.startswith("xiaomi/") else model,
                "reported_model_identity": {
                    "schema_version": "pvian.reported-model-identity-decision/v1",
                    "policy_id": "test-policy",
                    "match": "explicit_mapping" if model.startswith("xiaomi/") else "exact",
                },
                "http_status": 200,
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 1,
                    "cached_tokens": 0,
                    "reasoning_tokens": None,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                "cost": {"status": "unknown", "reason": "pricing_missing"},
                "gateway_request_id_sha256": "sha256:" + "a" * 64,
                "upstream_request_id_sha256": "sha256:" + "b" * 64,
                "safe_headers": {
                    "x-pvian-route-id": f"bailian-cn-beijing:{model}",
                    "x-pvian-profile-id": "profile",
                    "x-pvian-profile-version": "1",
                    "x-pvian-endpoint-id": "endpoint",
                    "x-pvian-provider-id": "provider-bailian",
                    "x-pvian-upstream-model": model,
                    "x-pvian-public-model": alias,
                    "x-pvian-alias-facade": "semantic-model-mapping/v1",
                },
                "error_category": None,
                "status": "observed_supported",
            }
        )
    return {
        "schema_version": "pvian.gateway-5220-smoke/v1",
        "evidence_class": "isolated_gateway_smoke",
        "port": 5220,
        "models_http_status": 200,
        "completion_calls_attempted": 8,
        "retry_total_attempts": 1,
        "fallback": None,
        "completed_at": "2026-08-09T14:00:00Z",
        "results": results,
    }
