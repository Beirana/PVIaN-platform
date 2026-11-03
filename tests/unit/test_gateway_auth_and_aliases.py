from __future__ import annotations

import json
from pathlib import Path

import pytest

from pvian_gateway.aliases import (
    AliasRegistry,
    AliasRegistryError,
    ModelAlias,
)
from pvian_gateway.auth import (
    ClientAuthenticationError,
    ClientKeyRegistryError,
    GatewayClientAuthenticator,
)
from pvian_gateway.profiles import load_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_client_authentication_uses_non_secret_registry_and_constant_digest_matching(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "client-keys.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.gateway-client-key-registry/v1",
                "keys": [
                    {
                        "key_id": "current",
                        "status": "active",
                        "secret_environment_variable": "CURRENT_KEY",
                    },
                    {
                        "key_id": "old",
                        "status": "revoked",
                        "secret_environment_variable": "OLD_KEY",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    current_key = "gateway-current-key-0123456789abcdef"
    old_key = "gateway-old-key-0123456789abcdefghijk"
    authenticator = GatewayClientAuthenticator.from_file(
        registry_path, {"CURRENT_KEY": current_key, "OLD_KEY": old_key}
    )

    assert authenticator.ready is True
    assert authenticator.authenticate(f"Bearer {current_key}") == "current"
    assert current_key not in repr(authenticator)
    with pytest.raises(ClientAuthenticationError, match="revoked") as revoked:
        authenticator.authenticate(f"Bearer {old_key}")
    assert revoked.value.status_code == 403
    with pytest.raises(ClientAuthenticationError) as invalid:
        authenticator.authenticate("Bearer invalid")
    assert invalid.value.status_code == 401


def test_alias_registry_rejects_duplicate_exact_route_binding(tmp_path: Path) -> None:
    registry_path = tmp_path / "aliases.json"
    entry = {
        "alias": "pvian/example/one",
        "profile_id": "profile",
        "profile_version": "1",
        "endpoint_id": "endpoint",
        "route_id": "route:model",
        "upstream_model": "model",
    }
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.public-model-aliases/v1",
                "aliases": [entry, {**entry, "alias": "pvian/example/two"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AliasRegistryError, match="multiple public aliases"):
        AliasRegistry.from_file(registry_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("profile_id", "unknown", "unknown profile"),
        ("profile_version", "0.0.0", "version does not match"),
        ("endpoint_id", "unknown", "unknown endpoint"),
        ("route_id", "wrong:deepseek-v4-pro", "route does not match"),
        ("upstream_model", "deepseek-v4-flash-0731", "not declared"),
    ],
)
def test_alias_registry_fails_closed_for_cross_artifact_drift(
    field: str, value: str, message: str
) -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    alias = ModelAlias(
        alias="pvian/test/deepseek-v4-pro",
        profile_id="provider-yunwu-openai-compatible",
        profile_version="0.2.0",
        endpoint_id="endpoint-yunwu-openai-compatible",
        route_id="yunwu:deepseek-v4-pro",
        upstream_model="deepseek-v4-pro",
    )
    alias = ModelAlias(**{**alias.__dict__, field: value})
    if field == "upstream_model":
        alias = ModelAlias(**{**alias.__dict__, "route_id": f"yunwu:{value}"})

    with pytest.raises(AliasRegistryError, match=message):
        AliasRegistry((alias,)).validate_bindings(profiles)


def test_alias_registry_accepts_an_exact_declared_openai_binding() -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    alias = ModelAlias(
        alias="pvian/test/deepseek-v4-pro",
        profile_id="provider-yunwu-openai-compatible",
        profile_version="0.2.0",
        endpoint_id="endpoint-yunwu-openai-compatible",
        route_id="yunwu:deepseek-v4-pro",
        upstream_model="deepseek-v4-pro",
    )

    AliasRegistry((alias,)).validate_bindings(profiles)


@pytest.mark.parametrize("entries", [("active", "revoked"), ("revoked", "active")])
def test_client_key_registry_rejects_duplicate_configured_secret(
    tmp_path: Path, entries: tuple[str, str]
) -> None:
    registry_path = tmp_path / "client-keys.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.gateway-client-key-registry/v1",
                "keys": [
                    {
                        "key_id": "first",
                        "status": entries[0],
                        "secret_environment_variable": "FIRST_KEY",
                    },
                    {
                        "key_id": "second",
                        "status": entries[1],
                        "secret_environment_variable": "SECOND_KEY",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    duplicate = "gateway-duplicate-key-0123456789abcdef"
    with pytest.raises(ClientKeyRegistryError, match="duplicate secret"):
        GatewayClientAuthenticator.from_file(
            registry_path, {"FIRST_KEY": duplicate, "SECOND_KEY": duplicate}
        )


def test_client_key_registry_fails_closed_for_short_configured_key(tmp_path: Path) -> None:
    registry_path = tmp_path / "client-keys.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.gateway-client-key-registry/v1",
                "keys": [
                    {
                        "key_id": "current",
                        "status": "active",
                        "secret_environment_variable": "CURRENT_KEY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ClientKeyRegistryError, match="at least 32"):
        GatewayClientAuthenticator.from_file(registry_path, {"CURRENT_KEY": "too-short"})


def test_client_key_registry_is_not_ready_without_an_injected_active_key(tmp_path: Path) -> None:
    registry_path = tmp_path / "client-keys.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "pvian.gateway-client-key-registry/v1",
                "keys": [
                    {
                        "key_id": "current",
                        "status": "active",
                        "secret_environment_variable": "CURRENT_KEY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert GatewayClientAuthenticator.from_file(registry_path, {}).ready is False
