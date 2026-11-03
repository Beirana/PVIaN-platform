from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PVI_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 5100
    data_dir: Path = PROJECT_ROOT / "data"
    contract_dir: Path = PROJECT_ROOT / "contracts"
    log_level: str = "INFO"
    environment: str = "development"
    require_data_validation: bool = True
    fail_startup_on_invalid_data: bool = True

    def resolved(self) -> Settings:
        update: dict[str, Path] = {}
        for field in ("data_dir", "contract_dir"):
            value = getattr(self, field)
            if not value.is_absolute():
                update[field] = PROJECT_ROOT / value
        return self.model_copy(update=update)
