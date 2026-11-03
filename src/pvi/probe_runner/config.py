from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


class ProbeConfigurationError(ValueError):
    """Raised when an online probe endpoint is unsafe or incomplete."""


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    env_namespace: str
    endpoint_id: str
    subject_profile_id: str
    allowed_hosts: frozenset[str]


PROVIDERS: dict[str, ProviderSpec] = {
    "yunwu": ProviderSpec(
        provider_id="provider-yunwu",
        env_namespace="PVI_PROBE_YUNWU",
        endpoint_id="endpoint-yunwu-openai-compatible",
        subject_profile_id="cap-yunwu-gpt-4o-mini",
        allowed_hosts=frozenset({"yunwu.ai"}),
    )
}


@dataclass(frozen=True)
class ProbeSettings:
    provider_name: str
    provider_id: str
    endpoint_id: str
    subject_profile_id: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    max_retries: int = 0

    @classmethod
    def from_env(
        cls,
        provider_name: str,
        environ: Mapping[str, str] | None = None,
    ) -> ProbeSettings:
        values = os.environ if environ is None else environ
        try:
            spec = PROVIDERS[provider_name]
        except KeyError as exc:
            supported = ", ".join(sorted(PROVIDERS))
            raise ProbeConfigurationError(
                f"unsupported probe provider {provider_name!r}; supported: {supported}"
            ) from exc

        api_key = _required(values, f"{spec.env_namespace}_API_KEY")
        base_url = _normalize_base_url(
            _required(values, f"{spec.env_namespace}_BASE_URL"),
            spec.allowed_hosts,
        )
        model = _required(values, f"{spec.env_namespace}_MODEL")

        return cls(
            provider_name=provider_name,
            provider_id=spec.provider_id,
            endpoint_id=spec.endpoint_id,
            subject_profile_id=spec.subject_profile_id,
            base_url=base_url,
            model=model,
            api_key=api_key,
        )

    def safe_summary(self) -> dict[str, object]:
        return {
            "provider_name": self.provider_name,
            "provider_id": self.provider_id,
            "endpoint_id": self.endpoint_id,
            "subject_profile_id": self.subject_profile_id,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "key_configured": bool(self.api_key),
        }


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ProbeConfigurationError(f"required environment variable is missing: {name}")
    return value


def _normalize_base_url(value: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(value)
    # Loopback hosts may serve plain HTTP on an explicit port (self-hosted
    # local runtimes); every other host stays on strict HTTPS, default port.
    is_loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    scheme = parsed.scheme.lower()
    if is_loopback:
        if scheme not in ("http", "https"):
            raise ProbeConfigurationError("loopback probe base URL must use HTTP or HTTPS")
    elif scheme != "https":
        raise ProbeConfigurationError("probe base URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ProbeConfigurationError("probe base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProbeConfigurationError("probe base URL must not contain query or fragment")
    if parsed.hostname not in allowed_hosts:
        raise ProbeConfigurationError(
            f"probe base URL host {parsed.hostname!r} is not in the provider allowlist"
        )
    if not is_loopback and parsed.port not in (None, 443):
        raise ProbeConfigurationError("probe base URL must use the default HTTPS port")
    normalized_path = parsed.path.rstrip("/")
    if normalized_path != "/v1":
        raise ProbeConfigurationError("probe base URL path must be exactly /v1")
    return urlunsplit((scheme, parsed.netloc.lower(), normalized_path, "", ""))
