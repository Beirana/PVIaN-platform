from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from pvi.probe_runner.qualification import _qualify_model
from pvian_gateway.main import _usage_attempt
from pvian_gateway.profiles import ProfileError, load_profiles
from pvian_gateway.state import QualificationStateStore

ROOT = Path(__file__).parents[2]
PROFILE_DIR = ROOT / "config" / "provider-profiles"
REQUESTED = "xiaomi/mimo-v2.5-pro"
REPORTED = "mimo-v2.5-pro"


def _profiles():
    return load_profiles(PROFILE_DIR)


def test_only_exact_mimo_requested_reported_pair_is_explicitly_accepted() -> None:
    profiles = _profiles()
    mimo = profiles["provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro"]
    assert mimo.reported_model_decision(REQUESTED, REPORTED).match == "explicit_mapping"
    assert mimo.reported_model_decision(REQUESTED, REQUESTED).match == "exact"

    for rejected in ("other/mimo-v2.5-pro", "mimo-v2.5-pro-nearby", "MIMO-v2.5-pro", None):
        assert mimo.reported_model_decision(REQUESTED, rejected).match == "rejected"

    for profile in profiles.values():
        if profile.profile_id != mimo.profile_id:
            assert profile.reported_model_decision(REQUESTED, REPORTED).match == "rejected"


def test_mapping_cannot_escape_its_exact_catalog_target(tmp_path: Path) -> None:
    raw = json.loads((PROFILE_DIR / "bailian-cn-beijing-xiaomi-mimo-v2.5-pro.json").read_text())
    identity = raw["model_selection"]["targets"][0]["reported_model_identity"]
    identity["requested_model"] = "other/mimo-v2.5-pro"
    (tmp_path / "bad.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProfileError, match="outside its target"):
        load_profiles(tmp_path)


def test_qualifier_and_state_enable_only_the_versioned_mimo_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pvi.probe_runner.qualification as qualification

    profile = _profiles()["provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro"]
    supported = {"status": "observed_supported"}
    monkeypatch.setattr(
        qualification,
        "_run_baseline",
        lambda *_: {
            "message": supported,
            "stream": supported,
            "usage": {
                "prompt_tokens": 1,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
            "reported_model": REPORTED,
        },
    )
    monkeypatch.setattr(
        qualification,
        "_run_thinking",
        lambda *_: {"status": "observed_supported", "variants": [], "reported_model": REPORTED},
    )
    monkeypatch.setattr(
        qualification,
        "_run_tool_and_replay",
        lambda *_: {
            "native_tool_call": {
                "status": "observed_supported",
                "tool_call_count": 1,
                "call_id_sha256": None,
            },
            "assistant_replay": {"status": "observed_supported"},
            "tool_result_replay": {"status": "observed_supported"},
            "reported_model": REPORTED,
        },
    )
    result = _qualify_model(profile, None, object(), REQUESTED)
    assert result.qualification_status == "enabled"
    assert result.reported_model_identity["match"] == "explicit_mapping"

    store = QualificationStateStore(tmp_path / "state.json")
    record = store.record_qualification(
        route_id=f"bailian-cn-beijing:{REQUESTED}",
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        provider_id=profile.provider_id,
        endpoint_id=profile.endpoints[0].endpoint_id,
        requested_model=REQUESTED,
        reported_model=REPORTED,
        reported_model_identity=result.reported_model_identity,
        passed=True,
        capability_summary=result.capabilities,
        validity_hours=profile.policy.validity_hours,
        failure_threshold=profile.policy.consecutive_failure_threshold,
    )
    assert record["qualification_status"] == "enabled"
    assert store.callable_record(record["route_id"]) is not None
    store.validate_profile_bindings({profile.profile_id: replace(profile, lifecycle="active")})


def test_gateway_attempt_evidence_rejects_other_short_names() -> None:
    record = {
        "requested_model": REQUESTED,
        "reported_model": REPORTED,
        "reported_model_identity": {
            "schema_version": "pvian.reported-model-identity-decision/v1",
            "policy_id": "bailian-cn-beijing-xiaomi-mimo-v2-5-pro@1.0.0",
            "match": "explicit_mapping",
        },
        "provider_id": "provider-bailian",
    }
    accepted = _usage_attempt(
        "r1", None, "route", record, object(), "completed", reported_model=REPORTED
    )
    rejected = _usage_attempt(
        "r2", None, "route", record, object(), "completed", reported_model="other-short-name"
    )
    assert accepted["estimate"]["reason"] == "usage_unknown"
    assert rejected["estimate"]["reason"] == "reported_model_mismatch"
