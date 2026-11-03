from __future__ import annotations

import os
from collections.abc import Mapping


class NetworkAuthorizationError(RuntimeError):
    """Raised when a paid outbound probe was not explicitly authorized."""


def require_network_authorization(
    *,
    allow_network: bool,
    confirm_cost: bool,
    environ: Mapping[str, str] | None = None,
) -> None:
    values = os.environ if environ is None else environ
    if _is_truthy(values.get("PVI_NETWORK_DISABLED")):
        raise NetworkAuthorizationError("network probes are disabled by PVI_NETWORK_DISABLED")
    if not allow_network:
        raise NetworkAuthorizationError("missing explicit --allow-network")
    if not confirm_cost:
        raise NetworkAuthorizationError("missing explicit --confirm-cost")


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}
