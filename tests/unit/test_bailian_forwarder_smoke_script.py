from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pvian_gateway.profiles import load_profiles

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "smoke_bailian_candidate_forwarder.py"


def _module():
    spec = importlib.util.spec_from_file_location("bailian_forwarder_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_smoke_requires_both_explicit_network_and_cost_flags() -> None:
    module = _module()
    for allow_network, confirm_cost in ((False, False), (True, False), (False, True)):
        with pytest.raises(SystemExit, match="requires"):
            module._require_live_authorization(
                SimpleNamespace(allow_network=allow_network, confirm_cost=confirm_cost)
            )
    module._require_live_authorization(
        SimpleNamespace(allow_network=True, confirm_cost=True)
    )


def test_smoke_budget_is_exactly_one_call_for_each_of_eight_profiles() -> None:
    module = _module()
    assert len(module.TARGET_PROFILE_IDS) == 8
    assert len(set(module.TARGET_PROFILE_IDS)) == 8


def test_smoke_request_uses_profile_policy_without_retry_or_fallback() -> None:
    module = _module()
    profiles = load_profiles(ROOT / "config" / "provider-profiles")
    profile = profiles["provider-bailian-cn-beijing-kimi-k3"]
    body = json.loads(module._request_body(profile, "kimi/kimi-k3"))

    assert body["model"] == "kimi/kimi-k3"
    assert body["stream"] is False
    assert body["max_completion_tokens"] == 128
    assert body["reasoning_effort"] == "max"
    assert "temperature" not in body
    assert "seed" not in body
    assert "fallback" not in body
    assert "retry" not in body
