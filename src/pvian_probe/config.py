from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """Raised when credentials, endpoint, or model configuration is invalid."""


@dataclass(frozen=True)
class EndpointSettings:
    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 45.0
    max_retries: int = 0

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> EndpointSettings:
        values = os.environ if environ is None else environ
        api_key = _required(values, "PVI_PROBE_YUNWU_API_KEY")
        base_url = _normalize_base_url(_required(values, "PVI_PROBE_YUNWU_BASE_URL"))
        return cls(base_url=base_url, api_key=api_key)

    def safe_summary(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "endpoint_kind": "broker_gateway",
            "api_surface": "openai_compatible_chat_completions",
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "key_configured": bool(self.api_key),
        }


@dataclass(frozen=True)
class ModelTarget:
    vendor: str
    model: str
    token_limit_parameter: str
    reasoning_effort: str | None
    stream: bool = False


def load_model_targets(path: Path) -> list[ModelTarget]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "pvian.vendor-matrix-models/v1":
        raise ConfigurationError("unsupported model configuration schema")
    targets = [ModelTarget(**item) for item in data.get("models", [])]
    if [target.vendor for target in targets] != [
        "openai",
        "deepseek",
        "grok",
        "glm",
    ]:
        raise ConfigurationError("model configuration must contain openai/deepseek/grok/glm")
    allowed_token_parameters = {"max_tokens", "max_completion_tokens"}
    if any(target.token_limit_parameter not in allowed_token_parameters for target in targets):
        raise ConfigurationError("unsupported token limit parameter")
    return targets


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"required environment variable is missing: {name}")
    return value


def _normalize_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise ConfigurationError("base URL must use HTTPS")
    if parsed.hostname != "yunwu.ai":
        raise ConfigurationError("base URL host must be yunwu.ai")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError("base URL must not contain credentials, query, or fragment")
    if parsed.port not in (None, 443):
        raise ConfigurationError("base URL must use the default HTTPS port")
    if parsed.path.rstrip("/") != "/v1":
        raise ConfigurationError("base URL path must be exactly /v1")
    return urlunsplit(("https", parsed.netloc.lower(), "/v1", "", ""))
