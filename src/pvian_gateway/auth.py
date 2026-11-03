from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ClientKeyRegistryError(ValueError):
    """The non-secret gateway client-key registry is invalid."""


@dataclass(frozen=True)
class ClientKey:
    key_id: str
    status: str
    digest: bytes


class ClientAuthenticationError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class GatewayClientAuthenticator:
    """Validates only SHA-256 digests of locally injected client keys.

    The registry names environment variables but never contains a key or its
    digest.  Multiple active entries allow overlapping key rotation; a revoked
    entry remains identifiable while its local secret remains configured.
    """

    def __init__(self, keys: tuple[ClientKey, ...]) -> None:
        self._keys = keys

    @classmethod
    def from_file(
        cls,
        path: Path,
        environ: Mapping[str, str] | None = None,
    ) -> GatewayClientAuthenticator:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ClientKeyRegistryError("cannot read gateway client-key registry") from exc
        if not isinstance(value, dict) or value.get("schema_version") != (
            "pvian.gateway-client-key-registry/v1"
        ):
            raise ClientKeyRegistryError("unsupported gateway client-key registry schema")
        entries = value.get("keys")
        if not isinstance(entries, list) or not entries:
            raise ClientKeyRegistryError("gateway client-key registry must contain keys")
        values = os.environ if environ is None else environ
        key_ids: set[str] = set()
        digests: set[bytes] = set()
        keys: list[ClientKey] = []
        for item in entries:
            if not isinstance(item, dict):
                raise ClientKeyRegistryError("gateway client-key entry must be an object")
            key_id = _required_string(item, "key_id")
            if key_id in key_ids:
                raise ClientKeyRegistryError("duplicate gateway client key_id")
            key_ids.add(key_id)
            status = _required_string(item, "status")
            if status not in {"active", "revoked"}:
                raise ClientKeyRegistryError("gateway client-key status must be active or revoked")
            environment_variable = _required_string(item, "secret_environment_variable")
            secret = values.get(environment_variable, "")
            if secret:
                _validate_secret_length(secret)
                digest = hashlib.sha256(secret.encode("utf-8")).digest()
                if digest in digests:
                    raise ClientKeyRegistryError("gateway client-key registry has duplicate secret")
                digests.add(digest)
                keys.append(
                    ClientKey(
                        key_id=key_id,
                        status=status,
                        digest=digest,
                    )
                )
        return cls(tuple(keys))

    @property
    def ready(self) -> bool:
        return any(key.status == "active" for key in self._keys)

    def authenticate(self, authorization: str | None) -> str:
        token = _bearer_token(authorization)
        presented_digest = hashlib.sha256(token.encode("utf-8")).digest()
        matches = tuple(
            key for key in self._keys if hmac.compare_digest(presented_digest, key.digest)
        )
        if any(key.status == "revoked" for key in matches):
            raise ClientAuthenticationError(
                403, "client_api_key_revoked", "Client API key is revoked."
            )
        active = next((key for key in matches if key.status == "active"), None)
        if active is not None:
            return active.key_id
        raise ClientAuthenticationError(401, "invalid_client_api_key", "Client API key is invalid.")


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise ClientAuthenticationError(
            401, "client_api_key_required", "Authorization Bearer token is required."
        )
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or token.strip() != token:
        raise ClientAuthenticationError(
            401, "client_api_key_required", "Authorization Bearer token is required."
        )
    return token


def _required_string(value: Mapping[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise ClientKeyRegistryError(f"gateway client-key field {name} must be a non-empty string")
    return result.strip()


def _validate_secret_length(secret: str) -> None:
    if len(secret.encode("utf-8")) < 32:
        raise ClientKeyRegistryError("gateway client key must be at least 32 UTF-8 bytes")
