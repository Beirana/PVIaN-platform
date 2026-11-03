from __future__ import annotations

from typing import Any

from pvi.probe_runner.config import ProbeSettings


class ProbeDependencyError(RuntimeError):
    """Raised when the optional online-probe dependency is unavailable."""


def create_openai_compatible_client(settings: ProbeSettings) -> Any:
    return create_openai_client(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


def create_openai_client(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 30.0,
    max_retries: int = 0,
) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ProbeDependencyError(
            'online probes require: python -m pip install -e ".[probe]"'
        ) from exc

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=max_retries,
    )
