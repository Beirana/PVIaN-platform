from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from pvi.config import PROJECT_ROOT


class GatewaySettings(BaseSettings):
    """Configuration for the credential-holding gateway process only."""

    model_config = SettingsConfigDict(env_prefix="PVI_GATEWAY_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 5200
    profile_dir: Path = PROJECT_ROOT / "config" / "provider-profiles"
    client_key_registry_file: Path = PROJECT_ROOT / "config" / "gateway-client-keys.json"
    alias_registry_file: Path = PROJECT_ROOT / "config" / "public-model-aliases.json"
    state_file: Path = PROJECT_ROOT / "var" / "gateway" / "qualification-state.json"
    # Per-request wall-clock budget the gateway allows the upstream provider.
    # A long-reasoning model (e.g. deepseek-v4-flash with enable_thinking) can
    # stream a high-reasoning-load item for 2-3 minutes before the full response
    # is ready; 60s cut such calls off mid-generation and surfaced them as
    # 502 upstream_transport_error. 300s gives a comfortable margin while still
    # bounding genuinely stuck requests. Override per launch with the env var.
    upstream_timeout_seconds: float = 300.0

    def resolved(self) -> GatewaySettings:
        updates: dict[str, Path] = {}
        for field in (
            "profile_dir",
            "client_key_registry_file",
            "alias_registry_file",
            "state_file",
        ):
            value = getattr(self, field)
            if not value.is_absolute():
                updates[field] = PROJECT_ROOT / value
        return self.model_copy(update=updates)
