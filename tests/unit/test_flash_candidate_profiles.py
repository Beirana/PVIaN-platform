from __future__ import annotations

import json
from pathlib import Path

from pvian_gateway.profiles import load_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FLASH_MODEL = "deepseek-v4-flash-0731"


def test_flash_profiles_are_exact_and_leave_pro_profiles_unchanged() -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")

    pairs = (
        (
            profiles["provider-yunwu-openai-compatible"],
            profiles["provider-yunwu-deepseek-v4-flash-0731"],
        ),
        (
            profiles["provider-bailian-cn-beijing"],
            profiles["provider-bailian-cn-beijing-deepseek-v4-flash-0731"],
        ),
    )
    for index, (pro_profile, flash_profile) in enumerate(pairs):
        assert pro_profile.declares_upstream_model("deepseek-v4-pro") is True
        assert pro_profile.declares_upstream_model(FLASH_MODEL) is False
        assert flash_profile.lifecycle == ("candidate" if index == 0 else "active")
        assert flash_profile.declares_upstream_model(FLASH_MODEL) is True
        assert flash_profile.declares_upstream_model("deepseek-v4-pro") is False
        assert flash_profile.targets[0].select_from_catalog(
            {FLASH_MODEL, f"{FLASH_MODEL}-preview"}
        ) == FLASH_MODEL
        assert flash_profile.targets[0].select_from_catalog({f"{FLASH_MODEL}-preview"}) is None
        expected_endpoint = next(
            item for item in pro_profile.endpoints if item.qualification_enabled
        )
        assert flash_profile.endpoints == (expected_endpoint,)
        assert flash_profile.probe_set == pro_profile.probe_set
        assert flash_profile.request_variants == pro_profile.request_variants
        assert flash_profile.policy == pro_profile.policy


def test_only_bailian_flash_has_the_exact_public_alias() -> None:
    aliases = json.loads(
        (PROJECT_ROOT / "config" / "public-model-aliases.json").read_text(encoding="utf-8")
    )

    flash_aliases = [
        item for item in aliases["aliases"] if item["upstream_model"] == FLASH_MODEL
    ]
    assert flash_aliases == [
        {
            "alias": "pvian/bailian-cn-beijing/deepseek-v4-flash-0731",
            "profile_id": "provider-bailian-cn-beijing-deepseek-v4-flash-0731",
            "profile_version": "0.1.0",
            "endpoint_id": "endpoint-bailian-cn-beijing-openai-compatible",
            "route_id": "bailian-cn-beijing:deepseek-v4-flash-0731",
            "upstream_model": FLASH_MODEL,
        }
    ]
    assert all("yunwu" not in item["alias"] for item in flash_aliases)


YUNWU_FLASH_MODEL = "deepseek-v4-flash"


def test_yunwu_flash_candidate_matches_only_the_catalog_name() -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    profile = profiles["provider-yunwu-deepseek-v4-flash"]

    assert profile.lifecycle == "candidate"
    assert profile.declares_upstream_model(YUNWU_FLASH_MODEL) is True
    assert profile.declares_upstream_model(FLASH_MODEL) is False
    assert profile.declares_upstream_model("deepseek-v4-pro") is False
    assert profile.targets[0].select_from_catalog(
        {YUNWU_FLASH_MODEL, f"{YUNWU_FLASH_MODEL}-preview"}
    ) == YUNWU_FLASH_MODEL
    assert profile.targets[0].select_from_catalog(
        {f"{YUNWU_FLASH_MODEL}-preview"}
    ) is None
