from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pvian_gateway.aliases import AliasRegistry
from pvian_gateway.profiles import load_profiles

ROOT = Path(__file__).parents[2]
ARTIFACT_PATH = (
    ROOT
    / "data"
    / "evidence-artifacts"
    / "gateway-forwarder-smoke-bailian-candidates-20260809.json"
)
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "gateway-forwarder-smoke.schema.json"
EVIDENCE_PATH = (
    ROOT / "data" / "evidence" / "evidence-bailian-gateway-forwarder-smoke-20260809.json"
)
PROFILE_DIR = ROOT / "config" / "provider-profiles"
ALIAS_PATH = ROOT / "config" / "public-model-aliases.json"

EXACT_MODELS = [
    "qwen3.8-max",
    "kimi/kimi-k3",
    "glm-5.2",
    "qwen3.7-plus",
    "qwen3-vl-plus",
    "deepseek-v4-pro",
    "MiniMax/MiniMax-M3",
    "xiaomi/mimo-v2.5-pro",
]


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


def _validate_profile_tuples(artifact: dict[str, object]) -> None:
    profiles = load_profiles(PROFILE_DIR)
    aliases = AliasRegistry.from_file(ALIAS_PATH)
    aliases.validate_bindings(profiles)

    attempts = artifact["attempts"]
    assert isinstance(attempts, list)
    for attempt in attempts:
        assert isinstance(attempt, dict)
        profile = profiles[attempt["profile_id"]]
        if attempt["profile_version"] != profile.profile_version:
            assert (
                attempt["profile_id"],
                attempt["profile_version"],
                profile.profile_version,
            ) == (
                "provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro",
                "0.1.0",
                "0.2.0",
            )
        assert profile.declares_upstream_model(attempt["requested_model"])
        alias = aliases.get(f"pvian/bailian-cn-beijing/{attempt['requested_model']}")
        assert alias is not None
        assert alias.profile_id == attempt["profile_id"]
        if attempt["profile_version"] == profile.profile_version:
            assert alias.profile_version == attempt["profile_version"]
        else:
            assert alias.profile_version == profile.profile_version
        assert alias.route_id == attempt["route_id"]
        assert alias.upstream_model == attempt["requested_model"]


def test_smoke_artifact_is_strict_sanitized_complete_and_hash_bound() -> None:
    artifact = _load(ARTIFACT_PATH)
    schema = _load(SCHEMA_PATH)
    Draft202012Validator(schema).validate(artifact)

    assert artifact["catalog"]["target_models_present"] == EXACT_MODELS
    assert [item["requested_model"] for item in artifact["attempts"]] == EXACT_MODELS
    assert [item["ordinal"] for item in artifact["attempts"]] == list(range(1, 9))
    assert artifact["transport_attempts"] == artifact["completion_calls_attempted"] == 8
    assert artifact["retry_attempts"] == 0
    assert all(item["attempt_count"] == 1 for item in artifact["attempts"])
    assert all(item["http_status"] == 200 for item in artifact["attempts"])
    assert sum(Decimal(item["cost"]["amount"]) for item in artifact["attempts"]) == Decimal(
        artifact["estimated_list_cost_cny"]
    )

    artifact_keys = _keys(artifact)
    for forbidden in (
        "prompt",
        "response_content",
        "private_reasoning",
        "raw_body",
        "remote_error_text",
        "credential",
    ):
        assert forbidden not in artifact_keys
    request_ids = [artifact["catalog"]["provider_request_id_sha256"]] + [
        item["provider_request_id_sha256"] for item in artifact["attempts"]
    ]
    assert all(value.startswith("sha256:") and len(value) == 71 for value in request_ids)

    evidence = _load(EVIDENCE_PATH)
    assert evidence["sha256"] == hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest()
    _validate_profile_tuples(artifact)


@pytest.mark.parametrize(
    "forbidden",
    ["prompt", "response_content", "private_reasoning", "raw_body", "remote_error_text"],
)
def test_smoke_schema_rejects_sensitive_or_unallowlisted_fields(forbidden: str) -> None:
    artifact = _load(ARTIFACT_PATH)
    artifact["attempts"][0][forbidden] = "must-not-persist"
    errors = list(Draft202012Validator(_load(SCHEMA_PATH)).iter_errors(artifact))
    assert errors


def test_profile_tuple_drift_is_detected_before_evidence_can_be_trusted() -> None:
    artifact = copy.deepcopy(_load(ARTIFACT_PATH))
    artifact["attempts"][5]["profile_version"] = "0.4.0"
    with pytest.raises(AssertionError):
        _validate_profile_tuples(artifact)


def test_capability_profiles_preserve_observed_vs_effective_semantics(repository) -> None:
    target_models = {
        "qwen3.8-max",
        "kimi/kimi-k3",
        "glm-5.2",
        "qwen3.7-plus",
        "qwen3-vl-plus",
        "deepseek-v4-pro",
        "MiniMax/MiniMax-M3",
        "xiaomi/mimo-v2.5-pro",
    }

    profiles = [
        profile
        for profile in repository.profiles.values()
        if profile.subject.model_requested in target_models
        and profile.subject.provider_id == "provider-bailian"
    ]
    assert len(profiles) == 8
    for profile in profiles:
        for capability in (
            "messages",
            "usage_context",
            "streaming",
            "reasoning",
            "tool_calling",
        ):
            assert profile.model_api_capabilities[capability]["observed"] == "supported"
            assert profile.model_api_capabilities[capability]["effective"] == "unknown"
        structured = profile.model_api_capabilities["structured_output"]
        assert structured["observed"] == (
            "unsupported"
            if profile.subject.model_requested == "MiniMax/MiniMax-M3"
            else "supported"
        )
        assert structured["effective"] == "unknown"

    mimo = next(item for item in profiles if item.subject.model_requested.startswith("xiaomi/"))
    assert mimo.subject.model_reported == "mimo-v2.5-pro"
    kimi = next(item for item in profiles if item.subject.model_requested.startswith("kimi/"))
    assert kimi.subject.model_reported == "kimi/kimi-k3"
