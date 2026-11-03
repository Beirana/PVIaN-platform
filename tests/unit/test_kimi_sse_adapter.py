from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from pvian_gateway.forwarder import TransparentForwarder
from pvian_gateway.kimi_sse_adapter import KIMI_PROFILE_ID, KIMI_PROFILE_VERSION, install
from pvian_gateway.profiles import ResolvedEndpoint


def _endpoint(version: str = KIMI_PROFILE_VERSION) -> ResolvedEndpoint:
    profile = SimpleNamespace(profile_id=KIMI_PROFILE_ID, profile_version=version)
    endpoint = SimpleNamespace(chat_completions_path="/chat/completions")
    return ResolvedEndpoint(profile=profile, endpoint=endpoint, base_url="https://upstream.test")


@pytest.mark.asyncio
async def test_kimi_sse_aggregation_changes_only_upstream_stream_flag() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        seen["accept"] = request.headers.get("accept")
        sse = (
            'data: {"id":"chat-1","model":"kimi/kimi-k3","choices":'
            '[{"index":0,"delta":{"role":"assistant","content":"A"}}]}\n\n'
        )
        sse += (
            'data: {"choices":[{"index":0,"delta":{"content":"B"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        )
        sse += "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse.encode()
        )

    install()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        forwarder = TransparentForwarder(client)
        original = {
            "model": "kimi/kimi-k3",
            "messages": [{"role": "user", "content": "x"}],
            "stream": False,
            "reasoning_effort": "max",
            "max_completion_tokens": 32768,
        }
        response = await forwarder.forward(
            _endpoint(),
            "provider-secret-not-persisted",
            json.dumps(original).encode(),
            {"content-type": "application/json"},
            timeout_seconds=600,
        )
        output = b"".join([chunk async for chunk in response.body])
        await response.close()

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["stream"] is True
    assert {key: value for key, value in body.items() if key != "stream"} == {
        key: value for key, value in original.items() if key != "stream"
    }
    assert seen["accept"] == "text/event-stream"
    aggregate = json.loads(output)
    assert aggregate["model"] == "kimi/kimi-k3"
    assert aggregate["choices"][0]["message"]["content"] == "AB"
    assert aggregate["choices"][0]["finish_reason"] == "stop"
    assert aggregate["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


@pytest.mark.asyncio
async def test_non_opted_profile_remains_transparent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"ok":true}'
        )

    install()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        forwarder = TransparentForwarder(client)
        response = await forwarder.forward(
            _endpoint("0.1.0-gateway5231.2"),
            "provider-secret-not-persisted",
            b'{"model":"kimi/kimi-k3","stream":false}',
            {"content-type": "application/json"},
        )
        body = b"".join([chunk async for chunk in response.body])
        await response.close()
    assert body == b'{"ok":true}'
