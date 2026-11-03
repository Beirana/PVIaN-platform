from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pvian_gateway.forwarder import TransparentForwarder
from pvian_gateway.profiles import ProfileError, ProviderProfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    PROJECT_ROOT / "config" / "provider-profiles" / "bailian-cn-beijing-glm-5.2.json"
)


def _profile_value() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def test_endpoint_upstream_timeout_is_optional_and_strictly_positive() -> None:
    baseline = ProviderProfile.from_dict(_profile_value())
    assert baseline.endpoints[0].upstream_timeout_seconds is None

    configured = _profile_value()
    configured["endpoints"][0]["upstream_timeout_seconds"] = 600
    profile = ProviderProfile.from_dict(configured)
    assert profile.endpoints[0].upstream_timeout_seconds == 600

    for invalid in (0, -1, True, 1.5, "600"):
        value = _profile_value()
        value["endpoints"][0]["upstream_timeout_seconds"] = invalid
        with pytest.raises(ProfileError, match="positive integer or omitted"):
            ProviderProfile.from_dict(value)


def test_forwarder_applies_request_timeout_without_mutating_payload() -> None:
    value = _profile_value()
    value["endpoints"][0]["upstream_timeout_seconds"] = 600
    profile = ProviderProfile.from_dict(value)
    endpoint_profile = profile.endpoints[0]
    endpoint = profile.resolve_endpoint(
        endpoint_profile,
        {
            profile.credential_environment_variable: "not-used-by-resolver",
            endpoint_profile.base_url_environment_variable: (
                f"https://{next(iter(endpoint_profile.allowed_hosts))}"
                f"{endpoint_profile.required_base_path}"
            ),
        },
    )
    payload = {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "synthetic timeout test"}],
        "stream": False,
        "temperature": 0,
        "reasoning_effort": "max",
        "max_completion_tokens": 32768,
    }
    exact_body = json.dumps(payload, separators=(",", ":")).encode()
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["body"] = request.content
        observed["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "model": "glm-5.2",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            forwarded = await TransparentForwarder(client).forward(
                endpoint,
                "provider-key-not-logged",
                exact_body,
                {"content-type": "application/json"},
                timeout_seconds=profile.endpoints[0].upstream_timeout_seconds,
            )
            async for _ in forwarded.body:
                pass
            await forwarded.close()

    asyncio.run(exercise())

    assert observed["body"] == exact_body
    assert set(observed["timeout"].values()) == {600.0}
