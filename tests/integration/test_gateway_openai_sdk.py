from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pvian_gateway.config import GatewaySettings
from pvian_gateway.main import create_app
from pvian_gateway.state import QualificationStateStore

openai = pytest.importorskip("openai")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_KEY = "gateway-sdk-client-key-0123456789abcdef"
ALIAS = "pvian/bailian-cn-beijing/deepseek-v4-pro"


def _qualified_state(path: Path) -> None:
    QualificationStateStore(path).record_qualification(
        route_id="bailian-cn-beijing:deepseek-v4-pro",
        profile_id="provider-bailian-cn-beijing",
        profile_version="0.2.1",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={"thinking": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )


def test_openai_python_sdk_calls_public_alias_for_message_stream_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", CLIENT_KEY)
    monkeypatch.setenv(
        "PVI_PROBE_BAILIAN_OPENAI_BASE_URL",
        "https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("PVI_PROBE_BAILIAN_API_KEY", "sdk-test-provider-key")
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    captured: list[dict[str, object]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'data: {"id":"stream-1","object":"chat.completion.chunk",'
                    b'"model":"deepseek-v4-pro","choices":[{"index":0,'
                    b'"delta":{"reasoning_content":"synthetic"}}]}\n\n'
                    b'data: {"id":"stream-1","object":"chat.completion.chunk",'
                    b'"model":"deepseek-v4-pro","choices":[{"index":0,'
                    b'"delta":{"content":"ok"},"finish_reason":"stop"}],'
                    b'"usage":{"total_tokens":2}}\n\n'
                    b"data: [DONE]\n\n"
                ),
            )
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "synthetic",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "synthetic", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=upstream_client,
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
            ) as gateway_client:
                client = openai.AsyncOpenAI(
                    api_key=CLIENT_KEY,
                    base_url="http://gateway.test/v1",
                    http_client=gateway_client,
                    max_retries=0,
                )
                models = await client.models.list()
                assert models.data[0].id == ALIAS
                completion = await client.chat.completions.create(
                    model=ALIAS,
                    messages=[{"role": "user", "content": "synthetic"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "synthetic",
                                "description": "synthetic test only",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                    tool_choice="auto",
                    extra_body={"enable_thinking": True, "reasoning_effort": "high"},
                )
                assert completion.model == "deepseek-v4-pro"
                stream = await client.chat.completions.create(
                    model=ALIAS,
                    messages=[{"role": "user", "content": "synthetic"}],
                    stream=True,
                    extra_body={"enable_thinking": True},
                )
                chunks = [chunk async for chunk in stream]
                assert chunks[-1].choices[0].finish_reason == "stop"
                replay = await client.chat.completions.create(
                    model=ALIAS,
                    messages=[
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "synthetic", "arguments": "{}"},
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
                    ],
                    extra_body={"enable_thinking": True},
                )
                assert replay.model == "deepseek-v4-pro"
        await upstream_client.aclose()

    asyncio.run(exercise())

    assert len(captured) == 3
    assert all(request["model"] == "deepseek-v4-pro" for request in captured)
    assert captured[0]["tools"]
    assert captured[0]["tool_choice"] == "auto"
    assert captured[0]["enable_thinking"] is True
    assert captured[0]["reasoning_effort"] == "high"
    assert captured[2]["messages"][0]["tool_calls"][0]["id"] == "call-1"
    assert captured[2]["messages"][1]["tool_call_id"] == "call-1"


def test_health_and_readiness_are_the_only_unauthenticated_gateways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", CLIENT_KEY)
    state_file = tmp_path / "gateway-state.json"
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )
    with TestClient(app) as test_client:
        assert test_client.get("/healthz").status_code == 200
        assert test_client.get("/readyz").status_code == 200
        assert test_client.get("/v1/models").status_code == 401
        assert test_client.get("/openapi.json").status_code == 401
        assert test_client.get("/docs", follow_redirects=False).status_code == 401
