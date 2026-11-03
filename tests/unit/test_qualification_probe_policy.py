from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pvi.probe_runner.qualification import (
    _base_request_parameters,
    qualification_completion_call_count,
)
from pvian_gateway.profiles import ProbeRequestPolicy, ProfileError, load_profiles


def _profile_value(policy: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "pvi.provider-qualification-profile/v1",
        "profile_id": "synthetic",
        "profile_version": "1.0.0",
        "lifecycle": "candidate",
        "provider": {
            "provider_id": "synthetic",
            "display_name": "Synthetic",
            "credential_environment_variable": "SYNTHETIC_KEY",
        },
        "endpoints": [
            {
                "endpoint_id": "synthetic-endpoint",
                "route_id_prefix": "synthetic",
                "transport": "openai_compatible_chat_completions",
                "base_url_environment_variable": "SYNTHETIC_URL",
                "allowed_hosts": ["synthetic.invalid"],
                "required_base_path": "/v1",
                "chat_completions_path": "/chat/completions",
                "qualification_enabled": True,
            }
        ],
        "model_selection": {
            "targets": [
                {
                    "logical_model_id": "model",
                    "catalog_id_patterns": ["^model$"],
                    "if_absent": "stop",
                }
            ]
        },
        "qualification": {
            "probe_set": ["message", "thinking", "usage"],
            "request_variants": [
                {"variant_id": "thinking", "request_overrides": {"enable_thinking": True}}
            ],
            "probe_request_policy": policy,
            "enablement": {
                "required_probes": ["message", "thinking", "usage"],
                "validity_hours": 24,
                "consecutive_failure_threshold": 5,
            },
        },
    }


def _load(tmp_path: Path, policy: dict[str, object]):
    (tmp_path / "profile.json").write_text(
        json.dumps(_profile_value(policy)), encoding="utf-8"
    )
    return load_profiles(tmp_path)["synthetic"]


def test_null_temperature_and_seed_are_omitted_and_glm_output_field_is_frozen(
    tmp_path: Path,
) -> None:
    profile = _load(
        tmp_path,
        {
            "temperature": None,
            "seed": None,
            "max_output_parameter": "max_completion_tokens",
            "max_output_tokens": {"message": 32, "thinking": 256},
            "tool_choice_mode": "none",
            "timeout_seconds": 180,
            "retry_total_attempts": 1,
            "fallback": None,
        },
    )
    policy = profile.policy.probe_request_policy
    assert policy is not None
    assert _base_request_parameters(policy, "message") == {"max_completion_tokens": 32}
    assert _base_request_parameters(policy, "thinking") == {"max_completion_tokens": 256}
    assert qualification_completion_call_count(profile) == 2


def test_seed_is_sent_only_when_explicitly_frozen() -> None:
    policy = ProbeRequestPolicy(seed=20260807)
    assert _base_request_parameters(policy, "message") == {
        "max_tokens": 24,
        "temperature": 0,
        "seed": 20260807,
    }


@pytest.mark.parametrize(
    "policy",
    [
        {"retry_total_attempts": 2},
        {"fallback": "another-model"},
        {"max_output_parameter": "vendor_magic_tokens"},
    ],
)
def test_profile_rejects_retry_fallback_or_unknown_output_parameter(
    tmp_path: Path, policy: dict[str, object]
) -> None:
    with pytest.raises(ProfileError):
        _load(tmp_path, policy)


def test_call_budget_counts_each_probe_family_once() -> None:
    profile = SimpleNamespace(
        policy=SimpleNamespace(
            required_probes=(
                "message",
                "usage",
                "stream",
                "thinking",
                "native_tool_call",
                "assistant_replay",
                "tool_result_replay",
            )
        ),
        request_variants=({"variant_id": "a"}, {"variant_id": "b"}),
    )
    assert qualification_completion_call_count(profile) == 6
