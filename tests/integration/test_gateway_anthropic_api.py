from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pvian_gateway.config import GatewaySettings
from pvian_gateway.main import create_app
from pvian_gateway.state import QualificationStateStore

CLIENT_KEY = "gateway-client-test-key-0123456789abcdef"
ANTHROPIC_ALIAS = "pvian/anthropic-test/claude-test"
ANTHROPIC_ROUTE = "anthropic-test:claude-test"
OPENAI_ALIAS = "pvian/openai-test/claude-test"
OPENAI_ROUTE = "openai-test:claude-test"


def _settings(tmp_path: Path) -> GatewaySettings:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "anthropic.json").write_text(
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
                        "required_base_path": "",
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
    (profile_dir / "openai.json").write_text(
        json.dumps(
            {
                "schema_version": "pvi.provider-qualification-profile/v1",
                "profile_id": "provider-openai-test",
                "profile_version": "1.0.0",
                "lifecycle": "active",
                "provider": {
                    "provider_id": "provider-openai-test",
                    "display_name": "OpenAI test surface",
                    "credential_environment_variable": "OPENAI_TEST_API_KEY",
                },
                "endpoints": [
                    {
                        "endpoint_id": "endpoint-openai-test",
                        "route_id_prefix": "openai-test",
                        "transport": "openai_compatible_chat_completions",
                        "base_url_environment_variable": "OPENAI_TEST_BASE_URL",
                        "allowed_hosts": ["openai.test"],
                        "required_base_path": "/v1",
                        "chat_completions_path": "/chat/completions",
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
                    "probe_set": ["message"],
                    "request_variants": [{"variant_id": "baseline"}],
                    "enablement": {"required_probes": ["message"]},
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
                        "alias": ANTHROPIC_ALIAS,
                        "profile_id": "provider-anthropic-test",
                        "profile_version": "1.0.0",
                        "endpoint_id": "endpoint-anthropic-test",
                        "route_id": ANTHROPIC_ROUTE,
                        "upstream_model": "claude-test",
                    },
                    {
                        "alias": OPENAI_ALIAS,
                        "profile_id": "provider-openai-test",
                        "profile_version": "1.0.0",
                        "endpoint_id": "endpoint-openai-test",
                        "route_id": OPENAI_ROUTE,
                        "upstream_model": "claude-test",
                    },
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
                        "key_id": "test",
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


def _qualify(state_file: Path, *, anthropic: bool = True, passed: bool = True) -> None:
    QualificationStateStore(state_file).record_qualification(
        route_id=ANTHROPIC_ROUTE if anthropic else OPENAI_ROUTE,
        profile_id="provider-anthropic-test" if anthropic else "provider-openai-test",
        profile_version="1.0.0",
        provider_id="provider-anthropic-test" if anthropic else "provider-openai-test",
        endpoint_id="endpoint-anthropic-test" if anthropic else "endpoint-openai-test",
        requested_model="claude-test",
        reported_model="claude-test",
        passed=passed,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )


def _mock_client(captured: dict[str, object]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["anthropic_version"] = request.headers.get("anthropic-version")
        captured["user_agent"] = request.headers.get("user-agent")
        captured["body"] = json.loads(request.content)
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 42})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "model": "claude-test",
                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                },
            )
        return httpx.Response(
            200,
            json={
                "type": "message",
                "model": "claude-test",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 15, "output_tokens": 4},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    qualify_anthropic: bool = True,
    qualify_openai: bool = True,
) -> tuple[GatewaySettings, dict[str, object]]:
    settings = _settings(tmp_path)
    if qualify_anthropic:
        _qualify(settings.state_file, anthropic=True)
    if qualify_openai:
        _qualify(settings.state_file, anthropic=False)
    monkeypatch.setenv("PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY", CLIENT_KEY)
    monkeypatch.setenv("ANTHROPIC_TEST_API_KEY", "anthropic-secret")
    monkeypatch.setenv("ANTHROPIC_TEST_BASE_URL", "https://anthropic.test")
    monkeypatch.setenv("OPENAI_TEST_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_TEST_BASE_URL", "https://openai.test/v1")
    captured: dict[str, object] = {}
    return settings, captured


def _headers() -> dict[str, str]:
    return {"authorization": f"Bearer {CLIENT_KEY}"}


def test_messages_passes_through_with_model_facade_and_pinned_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": ANTHROPIC_ALIAS,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-test"
    assert captured["url"] == "https://anthropic.test/v1/messages"
    assert captured["authorization"] == "Bearer anthropic-secret"
    assert captured["anthropic_version"] == "2023-06-01"
    assert "pvian-gateway/anthropic-passthrough" in str(captured["user_agent"])
    # the only body mutation is the model facade rewrite
    assert captured["body"] == {
        "model": "claude-test",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert response.headers["x-pvian-api-surface"] == "anthropic_native_messages"
    assert response.headers["x-pvian-route-id"] == ANTHROPIC_ROUTE
    assert response.headers["x-pvian-public-model"] == ANTHROPIC_ALIAS


def test_messages_requires_client_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={"model": ANTHROPIC_ALIAS, "max_tokens": 8, "messages": []},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "client_api_key_required"


def test_messages_unknown_alias_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers=_headers(),
            json={"model": "pvian/missing/model", "max_tokens": 8, "messages": []},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_messages_unqualified_alias_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch, qualify_anthropic=False)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": ANTHROPIC_ALIAS,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "alias_not_callable"


def test_surfaces_reject_each_others_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        anthropic_on_openai = client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json={"model": ANTHROPIC_ALIAS, "messages": [{"role": "user", "content": "hi"}]},
        )
        openai_on_anthropic = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": OPENAI_ALIAS,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert anthropic_on_openai.status_code == 409
    assert anthropic_on_openai.json()["error"]["code"] == "alias_not_callable"
    assert openai_on_anthropic.status_code == 409
    assert openai_on_anthropic.json()["error"]["code"] == "alias_not_callable"


def test_messages_ledger_records_anthropic_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": ANTHROPIC_ALIAS,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
    ledger_path = settings.state_file.with_name("usage-attempt-ledger.json")
    attempts = json.loads(ledger_path.read_text(encoding="utf-8"))["attempts"]
    final = attempts[-1]
    assert final["route_id"] == ANTHROPIC_ROUTE
    assert final["status"] == "completed"
    assert final["usage"]["prompt_tokens"] == 15
    assert final["usage"]["completion_tokens"] == 4
    assert final["estimate"]["status"] == "unknown"
    assert final["estimate"]["reason"] == "pricing_missing"


def test_count_tokens_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages/count_tokens",
            headers=_headers(),
            json={
                "model": ANTHROPIC_ALIAS,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.json() == {"input_tokens": 42}
    assert captured["url"] == "https://anthropic.test/v1/messages/count_tokens"
    assert captured["body"]["model"] == "claude-test"


def test_models_listing_includes_anthropic_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, captured = _prepare(tmp_path, monkeypatch)
    app = create_app(settings, client=_mock_client(captured))
    with TestClient(app) as client:
        response = client.get("/v1/models", headers=_headers())
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert ANTHROPIC_ALIAS in ids
    assert OPENAI_ALIAS in ids
