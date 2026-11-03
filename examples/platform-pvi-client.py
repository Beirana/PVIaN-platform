"""Reference-only consumer adapter; it never replaces the platform model caller."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CapabilityCheck:
    status: str
    provenance: dict[str, Any] | None
    warning: str | None = None


class PviClient:
    def __init__(self, base_url: str, timeout_seconds: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def resolve(self, query: dict[str, Any]) -> CapabilityCheck:
        request = urllib.request.Request(
            f"{self.base_url}/v1/capability-queries:resolve",
            data=json.dumps(query).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            return CapabilityCheck("unavailable", None, type(exc).__name__)
        candidates = payload.get("candidates", [])
        provenance = None
        if candidates:
            first = candidates[0]
            provenance = {
                "endpoint_id": first["endpoint_id"],
                "profile_id": first["profile_id"],
                "profile_version": first["profile_version"],
                "query_id": payload["query_id"],
                "data_version": payload["data_version"],
            }
        return CapabilityCheck("available", provenance)


def run_with_advisory_capability_check(
    model_call: Callable[[], Any],
    pvi_client: PviClient,
    query: dict[str, Any],
) -> tuple[Any, CapabilityCheck]:
    """PVI is advisory; the existing model caller remains the direct execution path."""
    check = pvi_client.resolve(query)
    result = model_call()
    return result, check
