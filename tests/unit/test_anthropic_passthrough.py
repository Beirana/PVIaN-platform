from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pvian_gateway.accounting import AnthropicUsageCollector
from pvian_gateway.aliases import AliasRegistry
from pvian_gateway.forwarder import TransparentForwarder
from pvian_gateway.profiles import ProfileError, load_profiles


def _anthropic_profile_dir(tmp_path: Path, *, base_path: str = "") -> Path:
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "anthropic.json").write_text(
        json.dumps(
            {
                "schema_version": "pvi.provider-qualification-profile/v1",
                "profile_id": "provider-anthropic-test",
                "profile_version": "1.0.0",
                "lifecycle": "active",
                "provider": {
                    "provider_id": "provider-anthropic-test",
                    "display_name": "Anthropic test surface",
                    "credential_environment_variable": "ANTHROPIC_TEST_API_KEY",
                },
                "endpoints": [
                    {
                        "endpoint_id": "endpoint-anthropic-test",
                        "route_id_prefix": "anthropic-test",
                        "transport": "anthropic_native_messages",
                        "base_url_environment_variable": "ANTHROPIC_TEST_BASE_URL",
                        "allowed_hosts": ["anthropic.test"],
                        "required_base_path": base_path,
                        "messages_path": "/v1/messages",
                        "qualification_enabled": True,
                    }
                ],
                "model_selection": {
                    "targets": [
                        {
                            "logical_model_id": "claude_test",
                            "catalog_id_patterns": ["^claude-test$"],
                            "if_absent": "record_not_discovered_do_not_substitute",
                        }
                    ]
                },
                "qualification": {
                    "probe_set": ["message", "usage"],
                    "request_variants": [{"variant_id": "baseline"}],
                    "enablement": {"required_probes": ["message", "usage"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


def _anthropic_alias_file(tmp_path: Path) -> Path:
    path = tmp_path / "aliases.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "aliases": [
                    {
                        "alias": "pvian/anthropic-test/claude-test",
                        "profile_id": "provider-anthropic-test",
                        "profile_version": "1.0.0",
                        "endpoint_id": "endpoint-anthropic-test",
                        "route_id": "anthropic-test:claude-test",
                        "upstream_model": "claude-test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_anthropic_endpoint_parses_and_resolves_messages_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = load_profiles(_anthropic_profile_dir(tmp_path))
    profile = profiles["provider-anthropic-test"]
    endpoint_definition = profile.endpoints[0]

    assert endpoint_definition.messages_path == "/v1/messages"
    assert endpoint_definition.chat_completions_path == ""

    monkeypatch.setenv("ANTHROPIC_TEST_BASE_URL", "https://anthropic.test")
    endpoint = profile.resolve_endpoint(endpoint_definition)
    assert endpoint.messages_url() == "https://anthropic.test/v1/messages"

    monkeypatch.setenv("ANTHROPIC_TEST_BASE_URL", "https://api.anthropic.com")
    with pytest.raises(ProfileError):
        profile.resolve_endpoint(endpoint_definition)

    monkeypatch.setenv("ANTHROPIC_TEST_BASE_URL", "https://wrong-path.test/v2")
    with pytest.raises(ProfileError):
        profile.resolve_endpoint(endpoint_definition)


def test_alias_registry_accepts_anthropic_transport(tmp_path: Path) -> None:
    profiles = load_profiles(_anthropic_profile_dir(tmp_path))
    registry = AliasRegistry.from_file(_anthropic_alias_file(tmp_path))
    registry.validate_bindings(profiles)  # must not raise


def test_forwarder_forward_messages_pins_protocol_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["anthropic_version"] = request.headers.get("anthropic-version")
        captured["user_agent"] = request.headers.get("user-agent")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"type": "message", "model": "claude-test"})

    async def exercise() -> None:
        forwarder = TransparentForwarder(
            httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        forwarded = await forwarder.forward_messages(
            "https://anthropic.test/v1/messages",
            "secret-credential",
            b'{"model": "claude-test", "messages": []}',
            {},
            extra_headers={
                "anthropic-version": "2023-06-01",
                "user-agent": "pvian-gateway/anthropic-passthrough claude-cli/1.0",
            },
        )
        await forwarded.close()

    asyncio.run(exercise())
    assert captured["url"] == "https://anthropic.test/v1/messages"
    assert captured["authorization"] == "Bearer secret-credential"
    assert captured["anthropic_version"] == "2023-06-01"
    assert captured["user_agent"] == "pvian-gateway/anthropic-passthrough claude-cli/1.0"
    assert captured["body"] == {"model": "claude-test", "messages": []}


def test_anthropic_collector_non_streaming_usage() -> None:
    collector = AnthropicUsageCollector("application/json")
    collector.feed(
        json.dumps(
            {
                "type": "message",
                "model": "claude-test",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 15, "output_tokens": 4},
            }
        ).encode()
    )
    usage = collector.final_usage()
    assert usage is not None
    assert usage.prompt_tokens == 15
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 19
    assert usage.reasoning_tokens is None
    assert collector.reported_model == "claude-test"
    assert collector.finish_reason == "end_turn"
    assert collector.complete is True


def test_anthropic_collector_sse_events() -> None:
    collector = AnthropicUsageCollector("text/event-stream")
    events = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"model":"claude-test",'
        '"usage":{"input_tokens":12,"cache_read_input_tokens":4}}}\n\n'
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"x"}}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
        '"usage":{"output_tokens":35}}\n\n'
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n\n'
    )
    collector.feed(events.encode())
    usage = collector.final_usage()
    assert usage is not None
    assert usage.prompt_tokens == 12
    assert usage.cached_tokens == 4
    assert usage.completion_tokens == 35
    assert collector.finish_reason == "tool_use"
    assert collector.complete is True


def test_anthropic_collector_incomplete_stream_is_not_complete() -> None:
    collector = AnthropicUsageCollector("text/event-stream")
    collector.feed(
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"model":"claude-test","usage":{"input_tokens":3}}}\n\n'
    )
    assert collector.final_usage() is None
    assert collector.complete is False


def test_anthropic_collector_overflow_yields_no_usage() -> None:
    collector = AnthropicUsageCollector("text/event-stream")
    collector.feed(b"x" * (collector._MAX_BYTES + 1))
    assert collector.final_usage() is None
    assert collector.complete is False
