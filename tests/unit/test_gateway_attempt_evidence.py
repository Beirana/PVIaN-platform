from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from pvian_gateway.accounting import UsageCollector
from pvian_gateway.main import _hashed_upstream_request_id, _usage_attempt
from pvian_gateway.state import QualificationStateError, UsageAttemptLedger


def _entry() -> dict[str, object]:
    return _usage_attempt(
        "gateway-request-opaque",
        "logical-1",
        "bailian-cn-beijing:model",
        {"provider_id": "provider-bailian", "requested_model": "model"},
        None,
        "completed",
        upstream_status_code=200,
        upstream_request_id_sha256="sha256:" + "a" * 64,
        finish_reason="stop",
        attempt_started_at=time.monotonic(),
    )


def test_terminal_attempt_is_allowlisted_and_proves_no_gateway_retry(tmp_path: Path) -> None:
    ledger = UsageAttemptLedger(tmp_path / "qualification-state.json")
    ledger.record(_entry())
    stored = ledger.list_attempts()[0]
    assert stored["attempt_count"] == 1
    assert stored["finish_reason"] == "stop"
    assert stored["upstream_status_code"] == 200
    assert stored["error_category"] is None
    assert isinstance(stored["elapsed_ms"], int) and stored["elapsed_ms"] >= 0


def test_terminal_attempt_accepts_usd_estimates_from_official_and_broker_providers(
    tmp_path: Path,
) -> None:
    # Regression (2026-08-17): the ledger safety allowlist only accepted CNY
    # estimates, so every completed DeepSeek-official / OpenRouter request
    # raised QualificationStateError while recording accounting evidence and
    # surfaced as an ASGI exception after the body was already forwarded.
    entry = _entry()
    entry["cost_status"] = "estimated"
    entry["estimate"] = {
        "status": "estimated",
        "currency": "USD",
        "pricing_version": "2026-08-17-official-list",
        "effective_at": None,
        "amount": "0.0003762",
    }
    ledger = UsageAttemptLedger(tmp_path / "qualification-state.json")
    ledger.record(entry)
    stored = ledger.list_attempts()[0]
    assert stored["estimate"]["currency"] == "USD"
    assert stored["estimated_list_cost_cny"] is None


@pytest.mark.parametrize("forbidden", ["raw_provider_body", "private_reasoning", "error_text"])
def test_terminal_attempt_rejects_unallowlisted_sensitive_fields(
    tmp_path: Path, forbidden: str
) -> None:
    entry = _entry()
    entry[forbidden] = "must-not-persist"
    with pytest.raises(QualificationStateError, match="invalid or unsafe"):
        UsageAttemptLedger(tmp_path / "qualification-state.json").record(entry)
    assert not (tmp_path / "usage-attempt-ledger.json").exists()


def test_provider_request_id_is_hashed_but_gateway_id_remains_opaque() -> None:
    provider_id = "provider-request-may-be-sensitive"
    assert _hashed_upstream_request_id({"X-Request-ID": provider_id}) == (
        "sha256:" + hashlib.sha256(f"x-request-id:{provider_id}".encode()).hexdigest()
    )
    assert provider_id not in json.dumps(_entry())


def test_collector_persists_only_safe_finish_and_never_body_text() -> None:
    collector = UsageCollector("application/json")
    collector.feed(
        b'{"model":"model","choices":[{"message":{"content":"secret output"},'
        b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,'
        b'"completion_tokens":1,"total_tokens":2}}'
    )
    assert collector.final_usage() is not None
    assert collector.reported_model == "model"
    assert collector.finish_reason == "stop"
    assert not hasattr(collector, "content")


@pytest.mark.parametrize(
    ("status", "status_code", "error"),
    [
        ("completed", 200, "upstream_error"),
        ("upstream_error", 200, "upstream_error"),
        ("transport_error", 502, "transport_error"),
    ],
)
def test_terminal_attempt_rejects_internally_inconsistent_status_relations(
    tmp_path: Path, status: str, status_code: int, error: str
) -> None:
    entry = _entry()
    entry.update(
        {
            "status": status,
            "upstream_status_code": status_code,
            "error_category": error,
            "reported_model": None,
            "finish_reason": None,
            "usage": None,
        }
    )
    with pytest.raises(QualificationStateError, match="invalid or unsafe"):
        UsageAttemptLedger(tmp_path / "qualification-state.json").record(entry)
