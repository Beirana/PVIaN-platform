from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_bailian_profile_keeps_mutable_model_settings_out_of_env() -> None:
    profile_path = PROJECT_ROOT / "config" / "provider-profiles" / "bailian-cn-beijing.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile["schema_version"] == "pvi.provider-qualification-profile/v1"
    assert profile["provider"]["credential_environment_variable"] == ("PVI_PROBE_BAILIAN_API_KEY")

    openai_endpoint = profile["endpoints"][0]
    assert openai_endpoint["transport"] == "openai_compatible_chat_completions"
    assert openai_endpoint["catalog_discovery"]["operation"] == "openai_models_list"
    assert openai_endpoint["catalog_discovery"]["on_unavailable"] == (
        "record_inconclusive_do_not_fallback"
    )

    target = profile["model_selection"]["targets"][0]
    assert target["logical_model_id"] == "deepseek_v4_pro"
    assert target["if_absent"] == "record_not_discovered_do_not_substitute"

    variants = profile["qualification"]["request_variants"]
    assert {item["variant_id"] for item in variants} == {
        "baseline",
        "enable_thinking_true",
        "reasoning_effort_high",
        "reasoning_effort_max",
    }
    baseline = next(item for item in variants if item["variant_id"] == "baseline")
    assert baseline["request_overrides"] == {"enable_thinking": False}

    env_template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PVI_PROBE_BAILIAN_OPENAI_MODEL=" not in env_template
    assert "PVI_PROBE_BAILIAN_OPENAI_ENABLE_THINKING=" not in env_template
    assert "PVI_PROBE_BAILIAN_OPENAI_REASONING_EFFORT=" not in env_template


def test_bailian_flash_0731_profile_is_active_and_exact() -> None:
    profile_path = (
        PROJECT_ROOT
        / "config"
        / "provider-profiles"
        / "bailian-cn-beijing-deepseek-v4-flash-0731.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile["lifecycle"] == "active"
    assert profile["provider"]["provider_id"] == "provider-bailian"
    assert profile["provider"]["region"] == "cn-beijing"
    assert profile["model_selection"]["targets"] == [
        {
            "logical_model_id": "deepseek_v4_flash_0731",
            "catalog_id_patterns": ["^deepseek-v4-flash-0731$"],
            "source": {
                "kind": "official_documentation",
                "url": "https://help.aliyun.com/zh/model-studio/deepseek-api-by-vanchin",
            },
            "if_absent": "record_not_discovered_do_not_substitute",
        }
    ]
