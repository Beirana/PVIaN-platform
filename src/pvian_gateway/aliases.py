from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pvian_gateway.profiles import ProviderProfile


class AliasRegistryError(ValueError):
    """The versioned public model-alias registry is invalid."""


@dataclass(frozen=True)
class ModelAlias:
    alias: str
    profile_id: str
    profile_version: str
    endpoint_id: str
    route_id: str
    upstream_model: str

    def as_openai_model(self, record: dict[str, object]) -> dict[str, object]:
        return {
            "id": self.alias,
            "object": "model",
            "owned_by": "pvian",
            "pvian": {
                "route_id": self.route_id,
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
                "endpoint_id": self.endpoint_id,
                "upstream_model": self.upstream_model,
                "valid_until": record["valid_until"],
                "capabilities": record["capabilities"],
            },
        }


class AliasRegistry:
    def __init__(self, aliases: tuple[ModelAlias, ...]) -> None:
        self.aliases = aliases
        self._by_alias = {item.alias: item for item in aliases}

    @classmethod
    def from_file(cls, path: Path) -> AliasRegistry:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AliasRegistryError("cannot read public model-alias registry") from exc
        if not isinstance(value, dict) or value.get("schema_version") != (
            "pvian.public-model-aliases/v1"
        ):
            raise AliasRegistryError("unsupported public model-alias registry schema")
        entries = value.get("aliases")
        if not isinstance(entries, list) or not entries:
            raise AliasRegistryError("public model-alias registry must contain aliases")
        aliases = tuple(_parse_alias(item) for item in entries)
        seen_aliases: set[str] = set()
        seen_targets: set[tuple[str, str, str, str, str]] = set()
        for item in aliases:
            if item.alias in seen_aliases:
                raise AliasRegistryError("duplicate public model alias")
            seen_aliases.add(item.alias)
            target = (
                item.profile_id,
                item.profile_version,
                item.endpoint_id,
                item.route_id,
                item.upstream_model,
            )
            if target in seen_targets:
                raise AliasRegistryError("multiple public aliases cannot bind the same exact route")
            seen_targets.add(target)
        return cls(aliases)

    def get(self, alias: str) -> ModelAlias | None:
        return self._by_alias.get(alias)

    def aliases_for_upstream_model(self, model: str) -> tuple[ModelAlias, ...]:
        return tuple(item for item in self.aliases if item.upstream_model == model)

    def validate_bindings(self, profiles: dict[str, ProviderProfile]) -> None:
        """Fail closed unless every public alias exactly binds a declared route."""

        for alias in self.aliases:
            profile = profiles.get(alias.profile_id)
            if profile is None:
                raise AliasRegistryError("public model alias references an unknown profile")
            if profile.profile_version != alias.profile_version:
                raise AliasRegistryError("public model alias profile version does not match")
            endpoint = next(
                (item for item in profile.endpoints if item.endpoint_id == alias.endpoint_id),
                None,
            )
            if endpoint is None:
                raise AliasRegistryError("public model alias references an unknown endpoint")
            if endpoint.transport not in {
                "openai_compatible_chat_completions",
                "anthropic_native_messages",
            }:
                raise AliasRegistryError("public model alias endpoint transport is not supported")
            if alias.route_id != f"{endpoint.route_id_prefix}:{alias.upstream_model}":
                raise AliasRegistryError("public model alias route does not match its exact target")
            if not profile.declares_upstream_model(alias.upstream_model):
                raise AliasRegistryError(
                    "public model alias upstream model is not declared by profile"
                )


def alias_matches_record(alias: ModelAlias, record: dict[str, object] | None) -> bool:
    if record is None:
        return False
    return (
        record.get("route_id") == alias.route_id
        and record.get("profile_id") == alias.profile_id
        and record.get("profile_version") == alias.profile_version
        and record.get("endpoint_id") == alias.endpoint_id
        and record.get("requested_model") == alias.upstream_model
    )


def _parse_alias(value: object) -> ModelAlias:
    if not isinstance(value, dict):
        raise AliasRegistryError("public model-alias entry must be an object")
    return ModelAlias(
        alias=_required_string(value, "alias"),
        profile_id=_required_string(value, "profile_id"),
        profile_version=_required_string(value, "profile_version"),
        endpoint_id=_required_string(value, "endpoint_id"),
        route_id=_required_string(value, "route_id"),
        upstream_model=_required_string(value, "upstream_model"),
    )


def _required_string(value: dict[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise AliasRegistryError(f"public model-alias field {name} must be a non-empty string")
    return result.strip()
