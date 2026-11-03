from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ProfileError(ValueError):
    """A versioned provider profile is incomplete or unsafe."""


STRICT_REPORTED_MODEL_POLICY_ID = "pvian.reported-model-identity/exact-v1"


@dataclass(frozen=True)
class ReportedModelIdentityDecision:
    policy_id: str
    match: str

    @property
    def accepted(self) -> bool:
        return self.match in {"exact", "explicit_mapping"}

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": "pvian.reported-model-identity-decision/v1",
            "policy_id": self.policy_id,
            "match": self.match,
        }


@dataclass(frozen=True)
class ReportedModelIdentityMapping:
    policy_id: str
    requested_model: str
    reported_model: str


@dataclass(frozen=True)
class ModelTarget:
    logical_model_id: str
    catalog_id_patterns: tuple[str, ...]
    if_absent: str
    reported_model_identity: ReportedModelIdentityMapping | None = None

    def select_from_catalog(self, catalog_ids: set[str]) -> str | None:
        for pattern in self.catalog_id_patterns:
            matches = sorted(
                model_id for model_id in catalog_ids if re.fullmatch(pattern, model_id)
            )
            if matches:
                return matches[0]
        return None

    def declares(self, model_id: str) -> bool:
        """Return whether an exact upstream model ID is declared by this target."""

        return any(
            re.fullmatch(pattern, model_id) is not None for pattern in self.catalog_id_patterns
        )

    def reported_model_decision(
        self, requested_model: str, reported_model: str | None
    ) -> ReportedModelIdentityDecision:
        if reported_model == requested_model:
            return ReportedModelIdentityDecision(STRICT_REPORTED_MODEL_POLICY_ID, "exact")
        mapping = self.reported_model_identity
        if (
            mapping is not None
            and requested_model == mapping.requested_model
            and reported_model == mapping.reported_model
        ):
            return ReportedModelIdentityDecision(mapping.policy_id, "explicit_mapping")
        policy_id = mapping.policy_id if mapping is not None else STRICT_REPORTED_MODEL_POLICY_ID
        return ReportedModelIdentityDecision(policy_id, "rejected")


@dataclass(frozen=True)
class QualificationPolicy:
    required_probes: tuple[str, ...]
    validity_hours: int = 168
    consecutive_failure_threshold: int = 5
    probe_request_policy: ProbeRequestPolicy | None = None


@dataclass(frozen=True)
class ProbeRequestPolicy:
    """Exact, no-retry request shape used by provider qualification probes.

    ``temperature=None`` and ``seed=None`` mean the fields are omitted, not
    sent as JSON null.  The defaults preserve the legacy qualification shape.
    """

    temperature: int | float | None = 0
    seed: int | None = None
    max_output_parameter: str = "max_tokens"
    message_max_output_tokens: int = 24
    stream_max_output_tokens: int = 24
    thinking_max_output_tokens: int = 24
    tool_max_output_tokens: int = 96
    replay_max_output_tokens: int = 48
    tool_choice_mode: str = "forced_function"
    timeout_seconds: int = 60
    retry_total_attempts: int = 1
    fallback: None = None


ANTHROPIC_TRANSPORT = "anthropic_native_messages"


@dataclass(frozen=True)
class EndpointProfile:
    endpoint_id: str
    route_id_prefix: str
    transport: str
    base_url_environment_variable: str
    allowed_hosts: frozenset[str]
    required_base_path: str
    chat_completions_path: str
    qualification_enabled: bool
    upstream_timeout_seconds: int | None = None
    messages_path: str | None = None


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    profile_version: str
    lifecycle: str
    provider_id: str
    region: str | None
    display_name: str
    credential_environment_variable: str
    endpoints: tuple[EndpointProfile, ...]
    targets: tuple[ModelTarget, ...]
    request_variants: tuple[dict[str, object], ...]
    probe_set: tuple[str, ...]
    policy: QualificationPolicy

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProviderProfile:
        if value.get("schema_version") != "pvi.provider-qualification-profile/v1":
            raise ProfileError("unsupported provider qualification profile schema")
        lifecycle = _required_string(value, "lifecycle")
        if lifecycle not in {"active", "candidate"}:
            raise ProfileError("profile lifecycle must be active or candidate")
        provider = _mapping(value, "provider")
        qualification = _mapping(value, "qualification")
        enablement = _mapping(qualification, "enablement")
        model_selection = _mapping(value, "model_selection")
        endpoint_values = _list(value, "endpoints")
        endpoints = tuple(_parse_endpoint(item) for item in endpoint_values)
        if not endpoints:
            raise ProfileError("profile must define at least one endpoint")
        targets = tuple(_parse_target(item) for item in _list(model_selection, "targets"))
        if not targets:
            raise ProfileError("profile must define at least one model target")
        variants = tuple(_mapping_item(item) for item in _list(qualification, "request_variants"))
        if not variants:
            raise ProfileError("profile must define at least one request variant")
        policy = QualificationPolicy(
            required_probes=tuple(_string_list(enablement, "required_probes")),
            validity_hours=_positive_int(enablement.get("validity_hours", 168), "validity_hours"),
            consecutive_failure_threshold=_positive_int(
                enablement.get("consecutive_failure_threshold", 5),
                "consecutive_failure_threshold",
            ),
            probe_request_policy=_parse_probe_request_policy(
                qualification.get("probe_request_policy")
            ),
        )
        if not policy.required_probes:
            raise ProfileError("enablement.required_probes must not be empty")
        declared_probes = tuple(_string_list(qualification, "probe_set"))
        supported_probes = {
            "message",
            "stream",
            "thinking",
            "native_tool_call",
            "assistant_replay",
            "tool_result_replay",
            "usage",
        }
        if not set(declared_probes) <= supported_probes:
            raise ProfileError("qualification.probe_set contains an unsupported probe")
        if not set(policy.required_probes) <= set(declared_probes) | {"usage"}:
            raise ProfileError("enablement.required_probes must be declared by probe_set")
        if (
            set(policy.required_probes)
            & {"native_tool_call", "assistant_replay", "tool_result_replay"}
            and policy.probe_request_policy is not None
            and policy.probe_request_policy.tool_choice_mode == "none"
        ):
            raise ProfileError("tool probes cannot be required when tool_choice_mode is none")
        return cls(
            profile_id=_required_string(value, "profile_id"),
            profile_version=_required_string(value, "profile_version"),
            lifecycle=lifecycle,
            provider_id=_required_string(provider, "provider_id"),
            region=_optional_string(provider, "region"),
            display_name=_required_string(provider, "display_name"),
            credential_environment_variable=_required_string(
                provider, "credential_environment_variable"
            ),
            endpoints=endpoints,
            targets=targets,
            request_variants=variants,
            probe_set=declared_probes,
            policy=policy,
        )

    def credential(self, environ: Mapping[str, str] | None = None) -> str:
        value = (
            (os.environ if environ is None else environ)
            .get(self.credential_environment_variable, "")
            .strip()
        )
        if not value:
            raise ProfileError(
                f"required environment variable is missing: {self.credential_environment_variable}"
            )
        return value

    def declares_upstream_model(self, model_id: str) -> bool:
        """Require an exact full-pattern match; never infer a nearby model ID."""

        return any(target.declares(model_id) for target in self.targets)

    def reported_model_decision(
        self, requested_model: str, reported_model: str | None
    ) -> ReportedModelIdentityDecision:
        target = next((item for item in self.targets if item.declares(requested_model)), None)
        if target is None:
            return ReportedModelIdentityDecision(STRICT_REPORTED_MODEL_POLICY_ID, "rejected")
        return target.reported_model_decision(requested_model, reported_model)

    def resolve_endpoint(
        self,
        endpoint: EndpointProfile,
        environ: Mapping[str, str] | None = None,
    ) -> ResolvedEndpoint:
        values = os.environ if environ is None else environ
        base_url = _normalize_base_url(
            _required_env(values, endpoint.base_url_environment_variable),
            endpoint.allowed_hosts,
            endpoint.required_base_path,
        )
        return ResolvedEndpoint(profile=self, endpoint=endpoint, base_url=base_url)


@dataclass(frozen=True)
class ResolvedEndpoint:
    profile: ProviderProfile
    endpoint: EndpointProfile
    base_url: str

    def route_id(self, model: str) -> str:
        return f"{self.endpoint.route_id_prefix}:{model}"

    def chat_url(self) -> str:
        return f"{self.base_url}{self.endpoint.chat_completions_path}"

    def messages_url(self) -> str:
        if self.endpoint.messages_path is None:
            raise ProfileError(
                "endpoint does not declare an Anthropic messages path "
                f"({self.endpoint.endpoint_id})"
            )
        return f"{self.base_url}{self.endpoint.messages_path}"

    def catalog_url(self) -> str:
        return f"{self.base_url}/models"

    def safe_summary(self) -> dict[str, object]:
        return {
            "profile_id": self.profile.profile_id,
            "profile_version": self.profile.profile_version,
            "provider_id": self.profile.provider_id,
            "endpoint_id": self.endpoint.endpoint_id,
            "transport": self.endpoint.transport,
            "base_url": self.base_url,
            "required_base_path": self.endpoint.required_base_path,
            "chat_completions_path": self.endpoint.chat_completions_path,
            "messages_path": self.endpoint.messages_path,
            "credential_environment_variable": self.profile.credential_environment_variable,
        }


def load_profiles(directory: Path) -> dict[str, ProviderProfile]:
    if not directory.is_dir():
        raise ProfileError(f"provider profile directory does not exist: {directory}")
    profiles: dict[str, ProviderProfile] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"failed to read provider profile {path.name}") from exc
        profile = ProviderProfile.from_dict(_mapping_item(value))
        if profile.profile_id in profiles:
            raise ProfileError(f"duplicate provider profile_id: {profile.profile_id}")
        profiles[profile.profile_id] = profile
    return profiles


def _parse_endpoint(value: object) -> EndpointProfile:
    item = _mapping_item(value)
    transport = _required_string(item, "transport")
    upstream_timeout_seconds = _optional_positive_int(
        item.get("upstream_timeout_seconds"), "upstream_timeout_seconds"
    )
    if transport == ANTHROPIC_TRANSPORT:
        return EndpointProfile(
            endpoint_id=_required_string(item, "endpoint_id"),
            route_id_prefix=_required_string(item, "route_id_prefix"),
            transport=transport,
            base_url_environment_variable=_required_string(item, "base_url_environment_variable"),
            allowed_hosts=frozenset(_string_list(item, "allowed_hosts")),
            required_base_path=_required_base_path(item),
            # The Anthropic Messages surface has no chat-completions path; the
            # required request path is declared via messages_path instead.
            chat_completions_path="",
            messages_path=_required_string(item, "messages_path"),
            qualification_enabled=bool(item.get("qualification_enabled", False)),
            upstream_timeout_seconds=upstream_timeout_seconds,
        )
    if transport != "openai_compatible_chat_completions":
        return EndpointProfile(
            endpoint_id=_required_string(item, "endpoint_id"),
            route_id_prefix=str(item.get("route_id_prefix") or item["endpoint_id"]),
            transport=transport,
            base_url_environment_variable=_required_string(item, "base_url_environment_variable"),
            allowed_hosts=frozenset(),
            required_base_path="/v1",
            chat_completions_path="/chat/completions",
            qualification_enabled=bool(item.get("qualification_enabled", False)),
            upstream_timeout_seconds=upstream_timeout_seconds,
        )
    return EndpointProfile(
        endpoint_id=_required_string(item, "endpoint_id"),
        route_id_prefix=_required_string(item, "route_id_prefix"),
        transport=transport,
        base_url_environment_variable=_required_string(item, "base_url_environment_variable"),
        allowed_hosts=frozenset(_string_list(item, "allowed_hosts")),
        # An empty base path is valid: some official APIs (e.g. DeepSeek) publish
        # their canonical OpenAI-compatible base URL without any path component.
        required_base_path=_required_base_path(item),
        chat_completions_path=_required_string(item, "chat_completions_path"),
        qualification_enabled=bool(item.get("qualification_enabled", False)),
        upstream_timeout_seconds=upstream_timeout_seconds,
    )


def _parse_target(value: object) -> ModelTarget:
    item = _mapping_item(value)
    patterns = tuple(_string_list(item, "catalog_id_patterns"))
    identity = _parse_reported_model_identity(item.get("reported_model_identity"), patterns)
    return ModelTarget(
        logical_model_id=_required_string(item, "logical_model_id"),
        catalog_id_patterns=patterns,
        if_absent=_required_string(item, "if_absent"),
        reported_model_identity=identity,
    )


def _parse_reported_model_identity(
    value: object, catalog_id_patterns: tuple[str, ...]
) -> ReportedModelIdentityMapping | None:
    if value is None:
        return None
    item = _mapping_item(value)
    if set(item) != {"schema_version", "policy_id", "requested_model", "reported_model"}:
        raise ProfileError("reported_model_identity must be one exact versioned mapping")
    if item.get("schema_version") != "pvian.reported-model-identity/v1":
        raise ProfileError("unsupported reported_model_identity schema")
    requested_model = _required_string(item, "requested_model")
    reported_model = _required_string(item, "reported_model")
    if not any(re.fullmatch(pattern, requested_model) for pattern in catalog_id_patterns):
        raise ProfileError("reported_model_identity requested_model is outside its target")
    if reported_model == requested_model:
        raise ProfileError("reported_model_identity must not duplicate strict exact identity")
    return ReportedModelIdentityMapping(
        policy_id=_required_string(item, "policy_id"),
        requested_model=requested_model,
        reported_model=reported_model,
    )


def _parse_probe_request_policy(value: object) -> ProbeRequestPolicy | None:
    if value is None:
        return None
    item = _mapping_item(value)
    allowed = {
        "temperature",
        "seed",
        "max_output_parameter",
        "max_output_tokens",
        "tool_choice_mode",
        "timeout_seconds",
        "retry_total_attempts",
        "fallback",
    }
    if not set(item) <= allowed:
        raise ProfileError("probe_request_policy contains an unsupported field")
    temperature = item.get("temperature", 0)
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
    ):
        raise ProfileError("probe_request_policy.temperature must be finite or null")
    seed = item.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ProfileError("probe_request_policy.seed must be an integer or null")
    parameter = item.get("max_output_parameter", "max_tokens")
    if parameter not in {"max_tokens", "max_completion_tokens"}:
        raise ProfileError("probe_request_policy.max_output_parameter is unsupported")
    token_values = item.get("max_output_tokens", {})
    if not isinstance(token_values, Mapping) or not set(token_values) <= {
        "message",
        "stream",
        "thinking",
        "tool",
        "replay",
    }:
        raise ProfileError("probe_request_policy.max_output_tokens is invalid")
    defaults = {"message": 24, "stream": 24, "thinking": 24, "tool": 96, "replay": 48}
    tokens = {
        name: _positive_int(token_values.get(name, default), f"max_output_tokens.{name}")
        for name, default in defaults.items()
    }
    tool_choice_mode = item.get("tool_choice_mode", "forced_function")
    if tool_choice_mode not in {"forced_function", "auto", "none"}:
        raise ProfileError("probe_request_policy.tool_choice_mode is unsupported")
    timeout_seconds = _positive_int(item.get("timeout_seconds", 60), "timeout_seconds")
    retry_total_attempts = item.get("retry_total_attempts", 1)
    if retry_total_attempts != 1:
        raise ProfileError("qualification probes must use exactly one total attempt")
    if item.get("fallback") is not None:
        raise ProfileError("qualification probes must not configure fallback")
    return ProbeRequestPolicy(
        temperature=temperature,
        seed=seed,
        max_output_parameter=parameter,
        message_max_output_tokens=tokens["message"],
        stream_max_output_tokens=tokens["stream"],
        thinking_max_output_tokens=tokens["thinking"],
        tool_max_output_tokens=tokens["tool"],
        replay_max_output_tokens=tokens["replay"],
        tool_choice_mode=tool_choice_mode,
        timeout_seconds=timeout_seconds,
        retry_total_attempts=1,
        fallback=None,
    )


def _mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _mapping_item(value.get(name))


def _mapping_item(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProfileError("profile field must be an object")
    return value


def _list(value: Mapping[str, object], name: str) -> list[object]:
    result = value.get(name)
    if not isinstance(result, list):
        raise ProfileError(f"profile field {name} must be a list")
    return result


def _required_string(value: Mapping[str, object], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result.strip():
        raise ProfileError(f"profile field {name} must be a non-empty string")
    return result.strip()


def _required_base_path(value: Mapping[str, object]) -> str:
    result = value.get("required_base_path")
    if not isinstance(result, str):
        raise ProfileError("profile field required_base_path must be a string")
    return result


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    result = value.get(name)
    if result is None:
        return None
    if not isinstance(result, str) or not result.strip():
        raise ProfileError(f"profile field {name} must be a non-empty string or omitted")
    return result.strip()


def _string_list(value: Mapping[str, object], name: str) -> list[str]:
    result = _list(value, name)
    if not all(isinstance(item, str) and item.strip() for item in result):
        raise ProfileError(f"profile field {name} must contain non-empty strings")
    return [item.strip() for item in result]


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or value < 1:
        raise ProfileError(f"profile field {name} must be a positive integer")
    return value


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileError(f"profile field {name} must be a positive integer or omitted")
    return value


def _required_env(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ProfileError(f"required environment variable is missing: {name}")
    return value


def _normalize_base_url(
    value: str,
    allowed_hosts: frozenset[str],
    required_base_path: str,
) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    # Loopback hosts may serve plain HTTP on an explicit port (self-hosted
    # local runtimes tunnelled to this machine); every other host stays on
    # strict HTTPS with the default port.
    is_loopback = hostname in {"127.0.0.1", "::1", "localhost"}
    scheme = parsed.scheme.lower()
    if is_loopback:
        if scheme not in ("http", "https"):
            raise ProfileError("loopback provider base URL must use HTTP or HTTPS")
    elif scheme != "https":
        raise ProfileError("provider base URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProfileError("provider base URL must not contain credentials, query or fragment")
    if parsed.hostname not in allowed_hosts:
        raise ProfileError("provider base URL host is not in the profile allowlist")
    if not is_loopback and parsed.port not in (None, 443):
        raise ProfileError("provider base URL must use the default HTTPS port")
    path = parsed.path.rstrip("/")
    if path != required_base_path:
        raise ProfileError("provider base URL path does not match the frozen profile")
    return urlunsplit((scheme, parsed.netloc.lower(), path, "", ""))
