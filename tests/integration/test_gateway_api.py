from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pvian_gateway.aliases import AliasRegistryError
from pvian_gateway.config import GatewaySettings
from pvian_gateway.main import create_app
from pvian_gateway.state import QualificationStateError, QualificationStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_KEY = "gateway-client-test-key-0123456789abcdef"
REVOKED_CLIENT_KEY = "gateway-client-revoked-key-0123456789ab"
BAILIAN_BASE_URL = "https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
BAILIAN_CHAT_URL = f"{BAILIAN_BASE_URL}/chat/completions"
ROUTE_ID = "bailian-cn-beijing:deepseek-v4-pro"
ALIAS = "pvian/bailian-cn-beijing/deepseek-v4-pro"


def _set_bailian_credentials(monkeypatch: pytest.MonkeyPatch, *, key: str = "test-secret") -> None:
    monkeypatch.setenv("PVI_PROBE_BAILIAN_OPENAI_BASE_URL", BAILIAN_BASE_URL)
    monkeypatch.setenv("PVI_PROBE_BAILIAN_API_KEY", key)


def _configure_gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", CLIENT_KEY)
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PREVIOUS", REVOKED_CLIENT_KEY)


def _client_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    return {"authorization": f"Bearer {CLIENT_KEY}", **(extra or {})}


class OneChunkAsyncStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content

    async def aclose(self) -> None:
        return None


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
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )


def _flash_candidate_settings(tmp_path: Path, *, lifecycle: str = "candidate") -> GatewaySettings:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "synthetic-flash.json").write_text(
        json.dumps(
            {
                "schema_version": "pvi.provider-qualification-profile/v1",
                "profile_id": "synthetic-flash-profile",
                "profile_version": "1.0.0",
                "lifecycle": lifecycle,
                "provider": {
                    "provider_id": "synthetic-provider",
                    "display_name": "Synthetic provider",
                    "credential_environment_variable": "SYNTHETIC_FLASH_API_KEY",
                },
                "endpoints": [
                    {
                        "endpoint_id": "synthetic-openai",
                        "route_id_prefix": "synthetic",
                        "transport": "openai_compatible_chat_completions",
                        "base_url_environment_variable": "SYNTHETIC_FLASH_BASE_URL",
                        "allowed_hosts": ["synthetic.test"],
                        "required_base_path": "/v1",
                        "chat_completions_path": "/chat/completions",
                        "qualification_enabled": True,
                    }
                ],
                "model_selection": {
                    "targets": [
                        {
                            "logical_model_id": "deepseek_v4_flash_0731",
                            "catalog_id_patterns": ["^deepseek-v4-flash-0731$"],
                            "if_absent": "record_not_discovered_do_not_substitute",
                        }
                    ]
                },
                "qualification": {
                    "probe_set": ["message", "stream"],
                    "request_variants": [{"variant_id": "baseline"}],
                    "enablement": {"required_probes": ["message", "stream"]},
                },
            }
        ),
        encoding="utf-8",
    )
    alias_registry = tmp_path / "aliases.json"
    alias_registry.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "aliases": [
                    {
                        "alias": "pvian/synthetic/deepseek-v4-flash-0731",
                        "profile_id": "synthetic-flash-profile",
                        "profile_version": "1.0.0",
                        "endpoint_id": "synthetic-openai",
                        "route_id": "synthetic:deepseek-v4-flash-0731",
                        "upstream_model": "deepseek-v4-flash-0731",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    key_registry = tmp_path / "client-keys.json"
    key_registry.write_text(
        json.dumps(
            {
                "schema_version": "pvian.gateway-client-key-registry/v1",
                "keys": [
                    {
                        "key_id": "synthetic",
                        "status": "active",
                        "secret_environment_variable": "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return GatewaySettings(
        profile_dir=profile_dir,
        alias_registry_file=alias_registry,
        client_key_registry_file=key_registry,
        state_file=tmp_path / "qualification-state.json",
    )


def _qualified_flash_candidate(state_file: Path, *, passed: bool = True) -> None:
    QualificationStateStore(state_file).record_qualification(
        route_id="synthetic:deepseek-v4-flash-0731",
        profile_id="synthetic-flash-profile",
        profile_version="1.0.0",
        provider_id="synthetic-provider",
        endpoint_id="synthetic-openai",
        requested_model="deepseek-v4-flash-0731",
        reported_model="deepseek-v4-flash-0731",
        passed=passed,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )


def test_gateway_forwards_exact_body_only_for_qualified_explicit_route(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch)
    captured: dict[str, object] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["auth"] = request.headers.get("authorization")
        captured["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "upstream-1"},
            content=b'data: {"id":"synthetic"}\n\ndata: [DONE]\n\n',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=client,
    )
    raw_body = b'{"model":"deepseek-v4-pro","stream":true,"messages":[]}'
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            content=raw_body,
            headers=_client_headers(
                {
                    "content-type": "application/json",
                    "x-pvian-route-id": ROUTE_ID,
                }
            ),
        )

    assert response.status_code == 200
    assert response.content == b'data: {"id":"synthetic"}\n\ndata: [DONE]\n\n'
    assert response.headers["x-pvian-route-id"] == ROUTE_ID
    assert captured == {
        "url": BAILIAN_CHAT_URL,
        "body": raw_body,
        "auth": "Bearer test-secret",
        "accept_encoding": "identity",
    }


def test_gateway_keeps_json_decodable_when_upstream_ignores_identity(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch)
    expected = {"model": "deepseek-v4-pro", "choices": [{"message": {"content": "ok"}}]}
    captured: dict[str, str | None] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["accept_encoding"] = request.headers.get("accept-encoding")
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=OneChunkAsyncStream(gzip.compress(json.dumps(expected).encode("utf-8"))),
        )

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": []},
            headers=_client_headers({"x-pvian-route-id": ROUTE_ID}),
        )

    assert captured["accept_encoding"] == "identity"
    assert response.headers["content-encoding"] == "gzip"
    assert response.json() == expected


def test_gateway_alias_keeps_gzip_json_decodable(tmp_path: Path, monkeypatch) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch, key="test-provider-secret")
    expected = {"model": "deepseek-v4-pro", "choices": [{"message": {"content": "ok"}}]}

    def upstream(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["model"] == "deepseek-v4-pro"
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=OneChunkAsyncStream(gzip.compress(json.dumps(expected).encode("utf-8"))),
        )

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": ALIAS, "messages": []},
            headers=_client_headers(),
        )

    assert response.headers["content-encoding"] == "gzip"
    assert response.json() == expected


def test_gateway_rejects_unqualified_route_without_upstream_call(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    _set_bailian_credentials(monkeypatch)
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
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": []},
            headers=_client_headers({"x-pvian-route-id": ROUTE_ID}),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "route_not_callable"


def test_gateway_rejects_missing_invalid_and_revoked_client_keys_before_upstream(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch)
    upstream_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    body = {"model": "deepseek-v4-pro", "messages": []}
    with TestClient(app) as test_client:
        missing = test_client.post("/v1/chat/completions", json=body)
        invalid = test_client.post(
            "/v1/chat/completions", json=body, headers={"authorization": "Bearer invalid"}
        )
        revoked = test_client.post(
            "/v1/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {REVOKED_CLIENT_KEY}"},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"]["code"] == "client_api_key_required"
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_client_api_key"
    assert revoked.status_code == 403
    assert revoked.json()["error"]["code"] == "client_api_key_revoked"
    assert upstream_calls == 0


def test_gateway_alias_facade_lists_callable_alias_and_preserves_request_fields(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch)
    captured: dict[str, object] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "usage": {"total_tokens": 3},
                "choices": [{"message": {"reasoning_content": "synthetic", "tool_calls": []}}],
            },
        )

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = ALIAS
    request_body = {
        "model": alias,
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "function": {"arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        "tools": [{"type": "function", "function": {"name": "synthetic"}}],
        "tool_choice": "auto",
        "enable_thinking": True,
        "reasoning_effort": "high",
    }
    with TestClient(app) as test_client:
        models = test_client.get("/v1/models", headers=_client_headers())
        response = test_client.post(
            "/v1/chat/completions", json=request_body, headers=_client_headers()
        )

    assert models.status_code == 200
    assert models.json()["object"] == "list"
    assert models.json()["data"][0]["id"] == alias
    assert models.json()["data"][0]["pvian"]["route_id"] == ROUTE_ID
    assert response.status_code == 200
    assert response.headers["x-pvian-public-model"] == alias
    assert response.headers["x-pvian-alias-facade"] == "semantic-model-mapping/v1"
    assert response.json()["model"] == "deepseek-v4-pro"
    assert captured["body"] == {**request_body, "model": "deepseek-v4-pro"}


def test_direct_explicit_route_and_alias_match_at_field_level(tmp_path: Path, monkeypatch) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch, key="test-provider-secret")
    forwarded: list[dict[str, object]] = []
    response_body = {
        "model": "deepseek-v4-pro",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        "choices": [
            {
                "message": {
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
    }

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "synthetic-1"},
            content=json.dumps(response_body, separators=(",", ":")).encode("utf-8"),
        )

    exact_body = {
        "model": "deepseek-v4-pro",
        "messages": [
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
            {"role": "tool", "tool_call_id": "call-1", "content": "synthetic result"},
        ],
        "tools": [{"type": "function", "function": {"name": "synthetic"}}],
        "tool_choice": "auto",
        "enable_thinking": True,
        "reasoning_effort": "high",
    }
    direct_client = httpx.Client(transport=httpx.MockTransport(upstream))
    direct = direct_client.post(
        BAILIAN_CHAT_URL,
        content=json.dumps(exact_body, separators=(",", ":")).encode("utf-8"),
    )
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as test_client:
        explicit = test_client.post(
            "/v1/chat/completions",
            content=json.dumps(exact_body, separators=(",", ":")).encode("utf-8"),
            headers=_client_headers(
                {
                    "content-type": "application/json",
                    "x-pvian-route-id": ROUTE_ID,
                }
            ),
        )
        alias = test_client.post(
            "/v1/chat/completions",
            json={**exact_body, "model": ALIAS},
            headers=_client_headers(),
        )

    assert direct.json() == explicit.json() == alias.json() == response_body
    assert direct.headers["x-request-id"] == explicit.headers["x-request-id"] == alias.headers[
        "x-request-id"
    ]
    assert forwarded == [exact_body, exact_body, exact_body]
    assert explicit.headers["x-pvian-route-id"] == ROUTE_ID
    assert alias.headers["x-pvian-public-model"] == ALIAS


def _ambiguous_alias_settings(tmp_path: Path) -> GatewaySettings:
    """Two profiles/endpoints each exposing the same upstream model under a
    distinct route_id, plus a public-model-alias registry binding both routes.
    A bare upstream-model request is therefore ambiguous (no unique alias)."""

    profile_dir = tmp_path / "profiles-ambiguous"
    profile_dir.mkdir()
    for _index, prefix in enumerate(("alpha", "beta")):
        (profile_dir / f"{prefix}.json").write_text(
            json.dumps(
                {
                    "schema_version": "pvi.provider-qualification-profile/v1",
                    "profile_id": f"provider-ambiguous-{prefix}",
                    "profile_version": "1.0.0",
                    "lifecycle": "active",
                    "provider": {
                        "provider_id": f"provider-ambiguous-{prefix}",
                        "display_name": f"Synthetic ambiguous {prefix}",
                        "credential_environment_variable": f"AMBIGUOUS_{prefix.upper()}_API_KEY",
                    },
                    "endpoints": [
                        {
                            "endpoint_id": f"endpoint-ambiguous-{prefix}",
                            "route_id_prefix": prefix,
                            "transport": "openai_compatible_chat_completions",
                            "base_url_environment_variable": f"AMBIGUOUS_{prefix.upper()}_BASE_URL",
                            "allowed_hosts": [f"{prefix}.test"],
                            "required_base_path": "/v1",
                            "chat_completions_path": "/chat/completions",
                            "qualification_enabled": True,
                        }
                    ],
                    "model_selection": {
                        "targets": [
                            {
                                "logical_model_id": "deepseek_v4_pro",
                                "catalog_id_patterns": ["^deepseek-v4-pro$"],
                                "if_absent": "record_not_discovered_do_not_substitute",
                            }
                        ]
                    },
                    "qualification": {
                        "probe_set": ["message", "stream"],
                        "request_variants": [{"variant_id": "baseline"}],
                        "enablement": {"required_probes": ["message", "stream"]},
                    },
                }
            ),
            encoding="utf-8",
        )
    alias_registry = tmp_path / "aliases-ambiguous.json"
    alias_registry.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "aliases": [
                    {
                        "alias": "pvian/alpha/deepseek-v4-pro",
                        "profile_id": "provider-ambiguous-alpha",
                        "profile_version": "1.0.0",
                        "endpoint_id": "endpoint-ambiguous-alpha",
                        "route_id": "alpha:deepseek-v4-pro",
                        "upstream_model": "deepseek-v4-pro",
                    },
                    {
                        "alias": "pvian/beta/deepseek-v4-pro",
                        "profile_id": "provider-ambiguous-beta",
                        "profile_version": "1.0.0",
                        "endpoint_id": "endpoint-ambiguous-beta",
                        "route_id": "beta:deepseek-v4-pro",
                        "upstream_model": "deepseek-v4-pro",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return GatewaySettings(
        profile_dir=profile_dir,
        alias_registry_file=alias_registry,
        state_file=tmp_path / "ambiguous-state.json",
    )


def test_gateway_rejects_ambiguous_bare_model_without_upstream_call(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    settings = _ambiguous_alias_settings(tmp_path)
    app = create_app(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not call upstream"))
        ),
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": []},
            headers=_client_headers(),
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_is_ambiguous"


def test_gateway_omits_expired_or_profile_drifted_aliases(tmp_path: Path, monkeypatch) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    state = QualificationStateStore(state_file)
    state.record_qualification(
        route_id=ROUTE_ID,
        profile_id="provider-bailian-cn-beijing",
        profile_version="0.2.0",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={},
        validity_hours=168,
        failure_threshold=5,
        observed_at=datetime.now(UTC),
    )
    _set_bailian_credentials(monkeypatch)
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not call upstream"))
        ),
    )
    with pytest.raises(QualificationStateError):
        with TestClient(app):
            pass

    state.record_qualification(
        route_id=ROUTE_ID,
        profile_id="provider-bailian-cn-beijing",
        profile_version="0.2.1",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={},
        validity_hours=168,
        failure_threshold=5,
        observed_at=datetime.now(UTC) - timedelta(hours=169),
    )
    with TestClient(app) as test_client:
        expired = test_client.get("/v1/models", headers=_client_headers())
    assert expired.json()["data"] == []


def test_gateway_hides_alias_and_rejects_route_when_provider_credential_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    monkeypatch.setenv("PVI_PROBE_BAILIAN_OPENAI_BASE_URL", BAILIAN_BASE_URL)
    monkeypatch.delenv("PVI_PROBE_BAILIAN_API_KEY", raising=False)
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("must not call upstream"))
        ),
    )
    alias = ALIAS
    with TestClient(app) as test_client:
        models = test_client.get("/v1/models", headers=_client_headers())
        alias_response = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )
        route_response = test_client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-pro", "messages": []},
            headers=_client_headers({"x-pvian-route-id": ROUTE_ID}),
        )

    assert models.json()["data"] == []
    assert alias_response.status_code == 409
    assert alias_response.json()["error"]["code"] == "alias_not_callable"
    assert route_response.status_code == 409
    assert route_response.json()["error"]["code"] == "route_not_callable"


def test_gateway_never_leaks_client_key_sentinel_to_gateway_state_or_responses(
    tmp_path: Path, monkeypatch
) -> None:
    client_sentinel = "gateway-client-sentinel-0123456789abcdef"
    provider_sentinel = "provider-key-sentinel-0123456789abcdef"
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", client_sentinel)
    monkeypatch.setenv(
        "PVI_GATEWAY_CLIENT_KEY_INTERNAL_PREVIOUS",
        "gateway-old-sentinel-0123456789abcdef",
    )
    monkeypatch.setenv("PVI_PROBE_BAILIAN_OPENAI_BASE_URL", BAILIAN_BASE_URL)
    monkeypatch.setenv("PVI_PROBE_BAILIAN_API_KEY", provider_sentinel)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    captured: dict[str, str | None] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"model": "deepseek-v4-pro", "choices": []})

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    headers = {"authorization": f"Bearer {client_sentinel}"}
    with TestClient(app) as test_client:
        models = test_client.get("/v1/models", headers=headers)
        routes = test_client.get("/v1/gateway/routes", headers=headers)
        capabilities = test_client.get("/v1/gateway/capabilities", headers=headers)
        chat = test_client.post(
            "/v1/chat/completions",
            json={"model": ALIAS, "messages": []},
            headers=headers,
        )
        missing = test_client.get("/v1/models")

    visible = "\n".join(
        [
            models.text,
            routes.text,
            capabilities.text,
            chat.text,
            missing.text,
            state_file.read_text(),
        ]
    )
    assert client_sentinel not in visible
    assert provider_sentinel not in visible
    assert captured["authorization"] == f"Bearer {provider_sentinel}"
    assert client_sentinel not in str(captured["authorization"])


def test_gateway_alias_preserves_sse_and_upstream_429_without_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    state_file = tmp_path / "gateway-state.json"
    _qualified_state(state_file)
    _set_bailian_credentials(monkeypatch)
    requests: list[dict[str, object]] = []
    sse = (
        b'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"reasoning_content":"a"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"b"},"finish_reason":"stop"}],'
        b'"usage":{"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        if len(requests) == 2:
            return httpx.Response(
                429,
                headers={"content-type": "application/json", "retry-after": "1"},
                content=b'{"error":{"code":"synthetic_rate_limited"}}',
            )
        return httpx.Response(
            503,
            headers={"content-type": "application/json", "x-request-id": "synthetic-503"},
            content=b'{"error":{"code":"synthetic_unavailable"}}',
        )

    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = ALIAS
    with TestClient(app) as test_client:
        streamed = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "stream": True, "messages": []},
            headers=_client_headers(),
        )
        limited = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )
        unavailable = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )

    assert streamed.status_code == 200
    assert streamed.content == sse
    assert b"reasoning_content" in streamed.content
    assert b"usage" in streamed.content
    assert b"[DONE]" in streamed.content
    assert limited.status_code == 429
    assert limited.content == b'{"error":{"code":"synthetic_rate_limited"}}'
    assert limited.headers["retry-after"] == "1"
    assert unavailable.status_code == 503
    assert unavailable.content == b'{"error":{"code":"synthetic_unavailable"}}'
    assert unavailable.headers["x-request-id"] == "synthetic-503"
    assert len(requests) == 3
    assert all(item["model"] == "deepseek-v4-pro" for item in requests)


def test_gateway_writes_one_local_usage_attempt_per_completed_or_interrupted_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    profile_path = (
        PROJECT_ROOT
        / "config"
        / "provider-profiles"
        / "bailian-cn-beijing-deepseek-v4-flash-0731.json"
    )
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["lifecycle"] = "active"
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "bailian.json").write_text(json.dumps(profile), encoding="utf-8")
    alias_registry = tmp_path / "aliases.json"
    alias_registry.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "aliases": [
                    {
                        "alias": "pvian/bailian-cn-beijing/deepseek-v4-flash-0731",
                        "profile_id": "provider-bailian-cn-beijing-deepseek-v4-flash-0731",
                        "profile_version": "0.1.0",
                        "endpoint_id": "endpoint-bailian-cn-beijing-openai-compatible",
                        "route_id": "bailian-cn-beijing:deepseek-v4-flash-0731",
                        "upstream_model": "deepseek-v4-flash-0731",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_file = tmp_path / "gateway-state.json"
    QualificationStateStore(state_file).record_qualification(
        route_id="bailian-cn-beijing:deepseek-v4-flash-0731",
        profile_id="provider-bailian-cn-beijing-deepseek-v4-flash-0731",
        profile_version="0.1.0",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-flash-0731",
        reported_model="deepseek-v4-flash-0731",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )
    monkeypatch.setenv(
        "PVI_PROBE_BAILIAN_OPENAI_BASE_URL",
        "https://llm-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("PVI_PROBE_BAILIAN_API_KEY", "synthetic-provider-key")
    json_body = (
        b'{"model":"deepseek-v4-flash-0731","usage":{"prompt_tokens":10,'
        b'"prompt_tokens_details":{"cached_tokens":2},"completion_tokens":4,'
        b'"completion_tokens_details":{"reasoning_tokens":3},"total_tokens":14}}'
    )
    sse = (
        b'data: {"model":"deepseek-v4-flash-0731"}\n\n'
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    missing_done = b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    responses = [
        httpx.Response(200, headers={"content-type": "application/json"}, content=json_body),
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse),
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=missing_done),
    ]

    def upstream(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    app = create_app(
        GatewaySettings(
            profile_dir=profile_dir,
            alias_registry_file=alias_registry,
            state_file=state_file,
        ),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/bailian-cn-beijing/deepseek-v4-flash-0731"
    body = {"model": alias, "messages": []}
    with TestClient(app) as test_client:
        models = test_client.get("/v1/models", headers=_client_headers())
        normal = test_client.post("/v1/chat/completions", json=body, headers=_client_headers())
        streamed = test_client.post(
            "/v1/chat/completions", json={**body, "stream": True}, headers=_client_headers()
        )
        test_client.post(
            "/v1/chat/completions", json={**body, "stream": True}, headers=_client_headers()
        )

    ledger_path = tmp_path / "usage-attempt-ledger.json"
    attempts = json.loads(ledger_path.read_text(encoding="utf-8"))["attempts"]
    assert models.json()["data"][0]["id"] == alias
    assert normal.headers["x-pvian-cost"] == "unknown"
    assert normal.headers["x-pvian-public-model"] == alias
    assert streamed.content == sse
    assert len(attempts) == 3
    assert [item["status"] for item in attempts] == ["completed", "completed", "stream_interrupted"]
    assert attempts[0]["requested_model"] == "deepseek-v4-flash-0731"
    assert attempts[0]["reported_model"] == "deepseek-v4-flash-0731"
    assert attempts[0]["provider"] == "bailian"
    assert attempts[0]["cost_status"] == "estimated"
    assert attempts[0]["estimated_list_cost_cny"] == "0.0000164"
    assert attempts[0]["pricing_version"] == "2026-08-03"
    assert attempts[0]["usage"] == {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    assert attempts[2]["usage"] is None
    assert attempts[2]["cost_status"] == "unknown"
    assert attempts[2]["estimated_list_cost_cny"] is None
    assert attempts[2]["pricing_version"] == "2026-08-03"
    assert attempts[2]["estimate"] == {"status": "unknown", "reason": "usage_unknown"}

    def models_for_state(path: Path) -> list[object]:
        state_app = create_app(
            GatewaySettings(
                profile_dir=profile_dir,
                alias_registry_file=alias_registry,
                state_file=path,
            ),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: pytest.fail("must not call upstream"))
            ),
        )
        with TestClient(state_app) as test_client:
            return test_client.get("/v1/models", headers=_client_headers()).json()["data"]

    state = QualificationStateStore(state_file)
    state.record_qualification(
        route_id="bailian-cn-beijing:deepseek-v4-flash-0731",
        profile_id="provider-bailian-cn-beijing-deepseek-v4-flash-0731",
        profile_version="0.1.0",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-flash-0731",
        reported_model="deepseek-v4-flash-0731",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
        observed_at=datetime.now(UTC) - timedelta(hours=169),
    )
    assert models_for_state(state_file) == []
    assert models_for_state(tmp_path / "missing-state.json") == []

    state.record_qualification(
        route_id="bailian-cn-beijing:deepseek-v4-flash-0731",
        profile_id="provider-bailian-cn-beijing-deepseek-v4-flash-0731",
        profile_version="0.1.0",
        provider_id="provider-bailian",
        endpoint_id="endpoint-bailian-cn-beijing-openai-compatible",
        requested_model="deepseek-v4-flash-0731",
        reported_model=None,
        passed=False,
        capability_summary={"message": {"status": "inconclusive"}},
        validity_hours=168,
        failure_threshold=5,
    )
    assert models_for_state(state_file) == []


def test_flash_candidate_is_hidden_and_rejected_without_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path)
    _qualified_flash_candidate(settings.state_file, passed=False)
    upstream_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200)

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as test_client:
        models = test_client.get("/v1/models", headers=_client_headers())
        response = test_client.post(
            "/v1/chat/completions",
            json={"model": "pvian/synthetic/deepseek-v4-flash-0731", "messages": []},
            headers=_client_headers(),
        )

    assert models.json()["data"] == []
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "alias_not_callable"
    assert upstream_calls == 0


def test_flash_candidate_preserves_native_payloads_errors_and_safe_trace_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    monkeypatch.setenv("SYNTHETIC_FLASH_BASE_URL", "https://synthetic.test/v1")
    monkeypatch.setenv("SYNTHETIC_FLASH_API_KEY", "synthetic-provider-key")
    settings = _flash_candidate_settings(tmp_path, lifecycle="active")
    _qualified_flash_candidate(settings.state_file)
    received_headers: list[dict[str, str]] = []
    received_bodies: list[dict[str, object]] = []
    sse = (
        b'data: {"choices":[{"delta":{"reasoning_content":"synthetic"}}]}\n\n'
        b'data: {"usage":{"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        received_headers.append(dict(request.headers))
        received_bodies.append(json.loads(request.content))
        if len(received_bodies) == 1:
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-pvian-request-id": "forged-upstream-id",
                },
                content=sse,
            )
        if len(received_bodies) == 2:
            return httpx.Response(
                429,
                headers={"content-type": "application/json", "retry-after": "1"},
                content=b'{"error":{"code":"rate_limited"}}',
            )
        return httpx.Response(
            503,
            headers={"content-type": "application/json"},
            content=b'{"error":{"code":"unavailable"}}',
        )

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/synthetic/deepseek-v4-flash-0731"
    request_body = {
        "model": alias,
        "stream": True,
        "enable_thinking": True,
        "messages": [{"role": "user", "content": "not logged"}],
    }
    with TestClient(app) as test_client:
        streamed = test_client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=_client_headers(
                {
                    "x-pvian-logical-call-id": "formal-round-1",
                    "x-request-id": "consumer-request-id",
                }
            ),
        )
        limited = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )
        unavailable = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )
        invalid_logical_id = test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers({"x-pvian-logical-call-id": "bad\nvalue"}),
        )

    assert streamed.status_code == 200
    assert streamed.content == sse
    assert b"reasoning_content" in streamed.content
    assert b"usage" in streamed.content
    assert streamed.headers["x-pvian-logical-call-id"] == "formal-round-1"
    assert streamed.headers["x-pvian-request-id"] != "forged-upstream-id"
    assert len(streamed.headers["x-pvian-request-id"]) == 32
    assert streamed.headers["x-pvian-route-id"] == "synthetic:deepseek-v4-flash-0731"
    assert streamed.headers["x-pvian-provider-id"] == "synthetic-provider"
    assert streamed.headers["x-pvian-upstream-model"] == "deepseek-v4-flash-0731"
    assert streamed.headers["x-pvian-cost"] == "unknown"
    assert limited.status_code == 429
    assert limited.content == b'{"error":{"code":"rate_limited"}}'
    assert limited.headers["retry-after"] == "1"
    assert unavailable.status_code == 503
    assert unavailable.content == b'{"error":{"code":"unavailable"}}'
    assert invalid_logical_id.status_code == 422
    assert len(received_bodies) == 3
    assert all(
        header not in headers
        for headers in received_headers
        for header in (
            "x-pvian-logical-call-id",
            "x-pvian-request-id",
            "x-pvian-route-id",
            "x-request-id",
        )
    )
    assert received_bodies[0] == {**request_body, "model": "deepseek-v4-flash-0731"}


def test_gateway_rejects_invalid_alias_binding_during_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path)
    aliases = json.loads(settings.alias_registry_file.read_text(encoding="utf-8"))
    aliases["aliases"][0]["route_id"] = "wrong:deepseek-v4-flash-0731"
    settings.alias_registry_file.write_text(json.dumps(aliases), encoding="utf-8")
    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)),
    )

    with pytest.raises(AliasRegistryError, match="route does not match"):
        with TestClient(app):
            pass


# ---------------------------------------------------------------------------
# D: State usage aggregation vs attempt ledger for non-streaming / complete SSE / interrupted SSE
# ---------------------------------------------------------------------------


def test_gateway_aggregates_usage_into_state_for_non_streaming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: Non-streaming 200 with valid usage must update state usage_totals."""

    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path, lifecycle="active")
    _qualified_flash_candidate(settings.state_file)
    monkeypatch.setenv("SYNTHETIC_FLASH_BASE_URL", "https://synthetic.test/v1")
    monkeypatch.setenv("SYNTHETIC_FLASH_API_KEY", "synthetic-key")
    json_body = (
        b'{"model":"deepseek-v4-flash-0731","usage":{"prompt_tokens":10,'
        b'"prompt_tokens_details":{"cached_tokens":2},"completion_tokens":4,'
        b'"completion_tokens_details":{"reasoning_tokens":3},"total_tokens":14}}'
    )

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=json_body
        )

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/synthetic/deepseek-v4-flash-0731"
    with TestClient(app) as test_client:
        test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )

    record = QualificationStateStore(settings.state_file).get(
        "synthetic:deepseek-v4-flash-0731"
    )
    assert record is not None
    assert record["request_count"] == 1
    assert record["usage_totals"]["prompt_tokens"] == 10
    assert record["usage_totals"]["cached_tokens"] == 2
    assert record["usage_totals"]["reasoning_tokens"] == 3
    assert record["usage_totals"]["completion_tokens"] == 4
    assert record["usage_totals"]["total_tokens"] == 14
    # D: known_usage_attempts=1 confirms this is a known-usage attempt.
    assert record["known_usage_attempts"] == 1


def test_gateway_aggregates_usage_into_state_for_complete_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: Complete SSE with [DONE] and valid usage must update state usage_totals."""

    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path, lifecycle="active")
    _qualified_flash_candidate(settings.state_file)
    monkeypatch.setenv("SYNTHETIC_FLASH_BASE_URL", "https://synthetic.test/v1")
    monkeypatch.setenv("SYNTHETIC_FLASH_API_KEY", "synthetic-key")
    sse = (
        b'data: {"model":"deepseek-v4-flash-0731"}\n\n'
        b'data: {"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":2},'
        b'"completion_tokens":4,"completion_tokens_details":{"reasoning_tokens":3},'
        b'"total_tokens":14}}\n\n'
        b"data: [DONE]\n\n"
    )

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/synthetic/deepseek-v4-flash-0731"
    with TestClient(app) as test_client:
        test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "stream": True, "messages": []},
            headers=_client_headers(),
        )

    record = QualificationStateStore(settings.state_file).get(
        "synthetic:deepseek-v4-flash-0731"
    )
    assert record is not None
    assert record["request_count"] == 1
    assert record["usage_totals"]["prompt_tokens"] == 10
    assert record["usage_totals"]["cached_tokens"] == 2
    assert record["usage_totals"]["reasoning_tokens"] == 3
    # D: known_usage_attempts=1 confirms this is a known-usage attempt.
    assert record["known_usage_attempts"] == 1


def test_gateway_does_not_aggregate_usage_for_interrupted_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: SSE without [DONE] must still record success but not aggregate usage."""

    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path, lifecycle="active")
    _qualified_flash_candidate(settings.state_file)
    monkeypatch.setenv("SYNTHETIC_FLASH_BASE_URL", "https://synthetic.test/v1")
    monkeypatch.setenv("SYNTHETIC_FLASH_API_KEY", "synthetic-key")
    # SSE without [DONE] — collector.complete will be False.
    sse = (
        b'data: {"model":"deepseek-v4-flash-0731"}\n\n'
        b'data: {"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}\n\n'
    )

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse
        )

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/synthetic/deepseek-v4-flash-0731"
    with TestClient(app) as test_client:
        test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "stream": True, "messages": []},
            headers=_client_headers(),
        )

    record = QualificationStateStore(settings.state_file).get(
        "synthetic:deepseek-v4-flash-0731"
    )
    assert record is not None
    assert record["request_count"] == 1
    # Usage totals must remain at zero (unknown, not zero-consumption).
    assert record["usage_totals"]["prompt_tokens"] == 0
    assert record["usage_totals"]["total_tokens"] == 0
    # D: known_usage_attempts=0 makes this machine-readable: not zero-consumption.
    assert record["known_usage_attempts"] == 0
    assert record["request_count"] > record["known_usage_attempts"]

    ledger_path = settings.state_file.parent / "usage-attempt-ledger.json"
    attempts = json.loads(ledger_path.read_text(encoding="utf-8"))["attempts"]
    assert attempts[0]["status"] == "stream_interrupted"
    assert attempts[0]["usage"] is None


def test_gateway_no_double_counting_across_ledger_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: A single non-streaming request must appear once in ledger and once in state."""

    _configure_gateway_auth(monkeypatch)
    settings = _flash_candidate_settings(tmp_path, lifecycle="active")
    _qualified_flash_candidate(settings.state_file)
    monkeypatch.setenv("SYNTHETIC_FLASH_BASE_URL", "https://synthetic.test/v1")
    monkeypatch.setenv("SYNTHETIC_FLASH_API_KEY", "synthetic-key")
    json_body = (
        b'{"model":"deepseek-v4-flash-0731","usage":{"prompt_tokens":10,'
        b'"completion_tokens":4,"total_tokens":14}}'
    )

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=json_body
        )

    app = create_app(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    alias = "pvian/synthetic/deepseek-v4-flash-0731"
    with TestClient(app) as test_client:
        test_client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": []},
            headers=_client_headers(),
        )

    ledger_path = settings.state_file.parent / "usage-attempt-ledger.json"
    attempts = json.loads(ledger_path.read_text(encoding="utf-8"))["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["usage"]["prompt_tokens"] == 10

    record = QualificationStateStore(settings.state_file).get(
        "synthetic:deepseek-v4-flash-0731"
    )
    assert record is not None
    assert record["request_count"] == 1
    assert record["usage_totals"]["prompt_tokens"] == 10


# ---------------------------------------------------------------------------
# Pricing snapshots endpoint (GET /v1/gateway/pricing/snapshots)
# ---------------------------------------------------------------------------


def _pricing_snapshots_app(tmp_path: Path, monkeypatch) -> tuple[object, Path]:
    """Build a gateway app with a temporary pricing dir for hot-reload tests.

    Uses the real provider profiles and alias registry from the repo, but
    redirects ``app.state.pricing_dir`` to a tmp dir so we can mutate pricing
    files without touching committed data.
    """
    _configure_gateway_auth(monkeypatch)
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    app = create_app(
        GatewaySettings(
            profile_dir=PROJECT_ROOT / "config" / "provider-profiles",
            alias_registry_file=PROJECT_ROOT / "config" / "public-model-aliases.json",
            state_file=tmp_path / "state.json",
        )
    )
    return app, pricing_dir


def _write_pricing_file(directory: Path, model: str, *, pricing_version: str = "v1") -> None:
    directory.joinpath(f"{model}.json").write_text(
        json.dumps(
            {
                "schema_version": "pvian.local-pricing/v2",
                "pricing_version": pricing_version,
                "effective_at": None,
                "captured_at": "2026-08-09T00:00:00Z",
                "source": {
                    "kind": "official_documentation",
                    "url": f"https://help.aliyun.com/zh/model-studio/{model}",
                    "price_basis": "advertised_list_price",
                    "discounts_included": False,
                    "captured_via": "official_page_field_capture",
                    "content_sha256": None,
                    "content_digest_status": "unavailable_dynamic_page_not_archived",
                },
                "provider": "bailian",
                "profile_id": f"provider-bailian-cn-beijing-{model.replace('.', '-')}",
                "region": "cn-beijing",
                "model": model,
                "currency": "CNY",
                "unit": "per_1m_tokens",
                "tiers": [
                    {
                        "input_tokens_gt": 0,
                        "input_tokens_lte": 1048576,
                        "rates": {"input": 8, "cached_input": 2, "output": 28},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_pricing_snapshots_list(tmp_path: Path, monkeypatch) -> None:
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    with TestClient(app) as test_client:
        # Point the app's pricing dir to our tmp dir and trigger initial load.
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        resp = test_client.get(
            "/v1/gateway/pricing/snapshots", headers=_client_headers()
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        snap = items[0]
        assert snap["schema_version"] == "pvian.pricing-snapshot-response/v1"
        assert len(snap["snapshot_digest"]) == 64
        assert snap["provider"] == "bailian"
        assert snap["region"] == "cn-beijing"
        assert snap["rates"]["prompt"] == 8.0
        assert snap["rates"]["cached_prompt"] == 2.0
        assert snap["rates"]["completion"] == 28.0
        assert snap["source_urls"] == ["https://help.aliyun.com/zh/model-studio/glm-5.2"]


def test_pricing_snapshots_include_output_limits(tmp_path: Path, monkeypatch) -> None:
    """The endpoint injects advertised token limits from config/model-token-limits.json."""

    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    with TestClient(app) as test_client:
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        resp = test_client.get(
            "/v1/gateway/pricing/snapshots", headers=_client_headers()
        )
        assert resp.status_code == 200
        snap = resp.json()["items"][0]
        limits = snap["output_limits"]
        assert limits is not None
        assert limits["context_window"] == 1048576
        assert limits["max_output_tokens"] == 131072
        assert limits["max_thinking_chain_tokens"] == 131072
        assert limits["output_parameter"] == "max_tokens"
        assert limits["confirmation"] == "confirmed"
        # Digest must still be present and 64 hex chars.
        assert len(snap["snapshot_digest"]) == 64


def test_pricing_snapshots_filter_by_provider_region(tmp_path: Path, monkeypatch) -> None:
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    _write_pricing_file(pricing_dir, "qwen3.8-max")
    with TestClient(app) as test_client:
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        resp = test_client.get(
            "/v1/gateway/pricing/snapshots",
            params={"provider": "bailian", "model": "qwen3.8-max"},
            headers=_client_headers(),
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["model"] == "qwen3.8-max"


def test_pricing_snapshots_filter_by_alias(tmp_path: Path, monkeypatch) -> None:
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    with TestClient(app) as test_client:
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        resp = test_client.get(
            "/v1/gateway/pricing/snapshots",
            params={"model_alias": "pvian/bailian-cn-beijing/glm-5.2"},
            headers=_client_headers(),
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["public_alias"] == "pvian/bailian-cn-beijing/glm-5.2"
        assert items[0]["route"] == "bailian-cn-beijing:glm-5.2"


def test_pricing_snapshots_unknown_alias_404(tmp_path: Path, monkeypatch) -> None:
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    with TestClient(app) as test_client:
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        resp = test_client.get(
            "/v1/gateway/pricing/snapshots",
            params={"model_alias": "pvian/nonexistent/model"},
            headers=_client_headers(),
        )
        assert resp.status_code == 404


def test_pricing_snapshots_requires_auth(tmp_path: Path, monkeypatch) -> None:
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2")
    with TestClient(app) as test_client:
        resp = test_client.get("/v1/gateway/pricing/snapshots")
        assert resp.status_code == 401


def test_pricing_snapshots_hot_reload(tmp_path: Path, monkeypatch) -> None:
    """Editing a pricing file is visible on the next request without restart."""
    app, pricing_dir = _pricing_snapshots_app(tmp_path, monkeypatch)
    _write_pricing_file(pricing_dir, "glm-5.2", pricing_version="2026-08-09")
    with TestClient(app) as test_client:
        test_client.app.state.pricing_dir = pricing_dir
        from pvian_gateway.accounting import load_pricing
        from pvian_gateway.main import _pricing_dir_mtime

        test_client.app.state.pricing = load_pricing(pricing_dir)
        test_client.app.state.pricing_mtime = _pricing_dir_mtime(pricing_dir)

        # First request: original pricing_version.
        resp1 = test_client.get(
            "/v1/gateway/pricing/snapshots", headers=_client_headers()
        )
        snap1 = resp1.json()["items"][0]
        assert snap1["pricing_version"] == "2026-08-09"

        # Rewrite the pricing file with a new pricing_version.
        _write_pricing_file(pricing_dir, "glm-5.2", pricing_version="2026-09-01")

        # Second request: hot-reloaded pricing is visible without restart.
        resp2 = test_client.get(
            "/v1/gateway/pricing/snapshots", headers=_client_headers()
        )
        snap2 = resp2.json()["items"][0]
        assert snap2["pricing_version"] == "2026-09-01"
        assert snap1["snapshot_id"] != snap2["snapshot_id"]
