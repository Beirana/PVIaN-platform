from __future__ import annotations

import json
from pathlib import Path

from pvian_gateway.aliases import AliasRegistry, alias_matches_record
from pvian_gateway.profiles import load_profiles

ROOT = Path(__file__).parents[2]
PROFILE_DIR = ROOT / "config" / "provider-profiles"
ALIAS_FILE = ROOT / "config" / "public-model-aliases.json"

CANDIDATE_MODELS = (
    "qwen3.8-max",
    "kimi/kimi-k3",
    "glm-5.2",
    "qwen3.7-plus",
    "qwen3-vl-plus",
    "MiniMax/MiniMax-M3",
    "xiaomi/mimo-v2.5-pro",
)


def _raw_profiles() -> dict[str, dict[str, object]]:
    values = {}
    for path in PROFILE_DIR.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        values[value["profile_id"]] = value
    return values


def test_eight_models_have_unique_exact_bailian_profiles_and_no_nearby_substitution() -> None:
    profiles = load_profiles(PROFILE_DIR)
    models = (*CANDIDATE_MODELS, "deepseek-v4-pro")

    for model in models:
        matches = [
            profile
            for profile in profiles.values()
            if profile.provider_id == "provider-bailian"
            and profile.declares_upstream_model(model)
        ]
        assert len(matches) == 1, model
        profile = matches[0]
        assert profile.region == "cn-beijing"
        target = next(item for item in profile.targets if item.declares(model))
        assert target.select_from_catalog({model, f"{model}-preview"}) == model
        assert target.select_from_catalog({f"{model}-preview"}) is None
        endpoint = next(item for item in profile.endpoints if item.qualification_enabled)
        assert endpoint.route_id_prefix == "bailian-cn-beijing"


def test_candidate_profiles_freeze_network_no_retry_no_fallback_and_redaction() -> None:
    profiles = load_profiles(PROFILE_DIR)
    raw_profiles = _raw_profiles()

    # Safety constraints (network flags, no retry, no fallback, redaction) must
    # hold for every Bailian third-party model profile regardless of lifecycle.
    # All 7 models are now active after per-model probe adaptation, but the
    # safety invariants are still verified here.
    for model in CANDIDATE_MODELS:
        profile = next(item for item in profiles.values() if item.declares_upstream_model(model))
        raw = raw_profiles[profile.profile_id]
        qualification = raw["qualification"]
        request_policy = qualification["probe_request_policy"]
        result_rules = qualification["result_rules"]

        assert qualification["required_network_flags"] == ["allow_network", "confirm_cost"]
        assert request_policy["retry_total_attempts"] == 1
        assert request_policy["fallback"] is None
        assert result_rules["raw_content"] == "never_persist"
        assert result_rules["unsupported_parameter"].endswith("do_not_retry_with_fallback")


def test_eight_exact_aliases_are_preregistered_but_candidates_fail_closed_without_state() -> None:
    profiles = load_profiles(PROFILE_DIR)
    aliases = AliasRegistry.from_file(ALIAS_FILE)
    aliases.validate_bindings(profiles)

    for model in (*CANDIDATE_MODELS, "deepseek-v4-pro"):
        public_alias = f"pvian/bailian-cn-beijing/{model}"
        alias = aliases.get(public_alias)
        assert alias is not None
        assert alias.upstream_model == model
        assert alias.route_id == f"bailian-cn-beijing:{model}"
        if model in CANDIDATE_MODELS:
            assert alias_matches_record(alias, None) is False
    serialized = ALIAS_FILE.read_text(encoding="utf-8")
    assert "cn-beepseek" not in serialized


def test_qwen_vl_text_qualification_includes_tool_probes() -> None:
    """qwen3-vl-plus supports tool calling (verified via direct API test on
    2026-08-11: returns finish_reason=tool_calls with correct function name
    and arguments). Tool probes are included in both probe_set and
    required_probes; tool_choice_mode is auto (not none)."""
    raw = _raw_profiles()["provider-bailian-cn-beijing-qwen3-vl-plus"]
    qualification = raw["qualification"]
    tool_probes = {"native_tool_call", "assistant_replay", "tool_result_replay"}

    assert tool_probes <= set(qualification["probe_set"])
    assert tool_probes <= set(qualification["enablement"]["required_probes"])
    assert qualification["probe_request_policy"]["tool_choice_mode"] == "auto"
