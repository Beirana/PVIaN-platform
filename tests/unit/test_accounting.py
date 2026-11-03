from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pvian_gateway.accounting import (
    AccountingError,
    RecommendedDefaults,
    SnapshotValidationError,
    ThinkingDefault,
    TokenLimits,
    UsageCollector,
    build_pricing_snapshot,
    estimate,
    extract_usage,
    load_price_snapshot,
    load_pricing,
    load_token_limits,
    select_quote,
)
from pvian_gateway.main import _usage_attempt
from pvian_gateway.state import QualificationStateStore, UsageAttemptLedger, _add_usage


def _usage(*, cached: int = 2) -> dict[str, object]:
    return {
        "usage": {
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 14,
        }
    }


def test_exact_local_price_estimates_cached_input_once() -> None:
    root = Path(__file__).parents[2]
    quote = select_quote(
        load_pricing(root / "config" / "pricing"),
        "bailian",
        "cn-beijing",
        "deepseek-v4-flash-0731",
    )
    result = estimate(extract_usage(_usage()), quote)
    # (8 * 1 + 2 * .2 + 4 * 2) / 1m; reasoning adds no second charge.
    assert result == {
        "status": "estimated",
        "currency": "CNY",
        "pricing_version": "2026-08-03",
        "effective_at": "2026-08-03T00:00:00Z",
        "amount": "0.0000164",
    }


def test_tiered_local_pricing_selects_by_total_input_tokens(tmp_path: Path) -> None:
    pricing = {
        "schema_version": "pvian.local-pricing/v2",
        "pricing_version": "official-list-captured-2026-08-09",
        "effective_at": None,
        "provider": "bailian",
        "profile_id": "provider-bailian-cn-beijing-qwen3-7-plus",
        "region": "cn-beijing",
        "model": "qwen3.7-plus",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "tiers": [
            {
                "input_tokens_gt": 0,
                "input_tokens_lte": 256000,
                "rates": {"input": 2, "cached_input": 0.4, "output": 8},
            },
            {
                "input_tokens_gt": 256000,
                "input_tokens_lte": 1000000,
                "rates": {"input": 6, "cached_input": 1.2, "output": 24},
            },
        ],
    }
    (tmp_path / "qwen.json").write_text(json.dumps(pricing), encoding="utf-8")
    quote = load_pricing(tmp_path)[0]
    usage = extract_usage(
        {
            "usage": {
                "prompt_tokens": 256001,
                "prompt_tokens_details": {"cached_tokens": 1},
                "completion_tokens": 1,
                "total_tokens": 256002,
            }
        }
    )

    assert estimate(usage, quote) == {
        "status": "estimated",
        "currency": "CNY",
        "pricing_version": "official-list-captured-2026-08-09",
        "effective_at": None,
        "amount": "1.5360252",
    }


def test_tiered_pricing_unknown_cached_rate_fails_closed(tmp_path: Path) -> None:
    pricing = {
        "schema_version": "pvian.local-pricing/v2",
        "pricing_version": "official-list-captured-2026-08-09",
        "effective_at": None,
        "provider": "bailian",
        "profile_id": "provider-bailian-cn-beijing-qwen3-8-max",
        "region": "cn-beijing",
        "model": "qwen3.8-max",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "tiers": [
            {
                "input_tokens_gt": 0,
                "input_tokens_lte": 1000000,
                "rates": {"input": 12, "cached_input": None, "output": 36},
            }
        ],
    }
    (tmp_path / "qwen.json").write_text(json.dumps(pricing), encoding="utf-8")
    quote = load_pricing(tmp_path)[0]

    assert estimate(extract_usage(_usage(cached=2)), quote) == {
        "status": "unknown",
        "reason": "cached_input_pricing_unknown",
    }


def test_tiered_pricing_rejects_gaps_and_overlap(tmp_path: Path) -> None:
    pricing = {
        "schema_version": "pvian.local-pricing/v2",
        "pricing_version": "test",
        "effective_at": None,
        "provider": "bailian",
        "profile_id": "profile",
        "region": "cn-beijing",
        "model": "model",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "tiers": [
            {
                "input_tokens_gt": 1,
                "input_tokens_lte": 100,
                "rates": {"input": 1, "cached_input": 0.2, "output": 2},
            }
        ],
    }
    (tmp_path / "bad.json").write_text(json.dumps(pricing), encoding="utf-8")

    with pytest.raises(AccountingError, match="contiguous"):
        load_pricing(tmp_path)


def test_negative_or_impossible_cached_usage_fails_closed() -> None:
    assert extract_usage(_usage(cached=-1)) is None
    assert extract_usage(_usage(cached=11)) is None
    malformed = _usage()
    malformed["usage"]["total_tokens"] = 15
    assert extract_usage(malformed) is None


def test_reasoning_may_exceed_completion_tokens() -> None:
    # DeepSeek folds reasoning into completion_tokens, but OpenRouter reports
    # reasoning_tokens as a separate pool (observed 2026-08-17 on
    # dots-studio/dots-3-note-preview:free with reasoning 66 > completion 60).
    # The extractor must keep both vendor dialects accountable.
    payload = _usage()
    payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 66}
    payload["usage"]["completion_tokens"] = 60
    payload["usage"]["total_tokens"] = 81
    payload["usage"]["prompt_tokens"] = 21
    summary = extract_usage(payload)
    assert summary is not None
    assert summary.reasoning_tokens == 66 and summary.completion_tokens == 60


def test_sse_collector_is_transparent_and_uses_final_usage_across_chunks() -> None:
    collector = UsageCollector("text/event-stream; charset=utf-8")
    chunks = [
        b'data: {"choices":[{"delta":{"content":"x"}}]}\r\n\r\n',
        b'data: {"usage":{"prompt_tokens":10,"prompt_tokens_details":{"cached_tokens":2},',
        b'"completion_tokens":4,"completion_tokens_details":{"reasoning_tokens":3},"total_tokens":14}}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ]
    for chunk in chunks:
        collector.feed(chunk)
    assert collector.final_usage().as_dict() == {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    assert b"".join(chunks).endswith(b"[DONE]\r\n\r\n")
    assert collector.complete is True


def test_sse_without_done_is_not_a_completed_usage() -> None:
    collector = UsageCollector("text/event-stream")
    collector.feed(
        b'data: {"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    )
    assert collector.final_usage() is not None
    assert collector.complete is False


def test_gateway_ledger_records_canonical_timestamp_and_model_mismatch_unknown(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    quote = select_quote(
        load_pricing(root / "config" / "pricing"),
        "bailian",
        "cn-beijing",
        "deepseek-v4-flash-0731",
    )
    entry = _usage_attempt(
        "request-1",
        "logical-1",
        "bailian-cn-beijing:deepseek-v4-flash-0731",
        {
            "provider_id": "provider-bailian",
            "profile_id": "provider-bailian-cn-beijing-deepseek-v4-flash-0731",
            "requested_model": "deepseek-v4-flash-0731",
        },
        None,
        "completed",
        extract_usage(_usage()),
        quote,
        False,
        "reported-other-model",
    )
    ledger = UsageAttemptLedger(tmp_path / "qualification-state.json")
    ledger.record(entry)
    stored = ledger.list_attempts()[0]
    recorded_at = datetime.fromisoformat(stored["recorded_at"].replace("Z", "+00:00"))
    assert recorded_at.tzinfo is not None and recorded_at.utcoffset() == UTC.utcoffset(recorded_at)
    assert stored["recorded_at"] == recorded_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    assert stored["usage"]["prompt_tokens"] == 10
    assert stored["cost_status"] == "unknown"
    assert stored["estimated_list_cost_cny"] is None
    assert stored["pricing_version"] is None
    assert stored["estimate"] == {"status": "unknown", "reason": "reported_model_mismatch"}


# ---------------------------------------------------------------------------
# D: Five-key usage aggregation in state totals
# ---------------------------------------------------------------------------


def test_add_usage_aggregates_five_key_canonical_totals() -> None:
    target = {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    first = {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    second = {
        "prompt_tokens": 5,
        "cached_tokens": 1,
        "reasoning_tokens": None,
        "completion_tokens": 2,
        "total_tokens": 7,
    }
    _add_usage(target, first)
    _add_usage(target, second)
    assert target == {
        "prompt_tokens": 15,
        "cached_tokens": 3,
        "reasoning_tokens": 3,
        "completion_tokens": 6,
        "total_tokens": 21,
    }


def test_add_usage_with_none_does_not_aggregate() -> None:
    target = {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    _add_usage(target, None)
    assert target == {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }


def test_record_success_aggregates_usage_only_when_known(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    store = QualificationStateStore(state_path)
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    store.record_qualification(
        route_id="synthetic:model",
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        provider_id="synthetic-provider",
        endpoint_id="synthetic-endpoint",
        requested_model="model",
        reported_model="model",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=24,
        failure_threshold=5,
        observed_at=now,
    )
    # Known usage — must aggregate.
    known_usage = {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    store.record_success("synthetic:model", usage=known_usage, observed_at=now)
    # Unknown usage — must NOT aggregate, but request_count still increments.
    store.record_success("synthetic:model", usage=None, observed_at=now)
    record = store.get("synthetic:model")
    assert record is not None
    assert record["request_count"] == 2
    assert record["usage_totals"]["prompt_tokens"] == 10
    assert record["usage_totals"]["cached_tokens"] == 2
    assert record["usage_totals"]["reasoning_tokens"] == 3
    assert record["usage_totals"]["completion_tokens"] == 4
    assert record["usage_totals"]["total_tokens"] == 14
    # D: known_usage_attempts tracks which requests had valid usage.
    assert record["known_usage_attempts"] == 1
    assert record["request_count"] > record["known_usage_attempts"]


# ---------------------------------------------------------------------------
# E: Price snapshot loading & validation
# ---------------------------------------------------------------------------


def _make_snapshot(
    tmp_path: Path,
    *,
    source_rel: str = "pricing.json",
    source_content: bytes | None = None,
    **overrides: object,
) -> tuple[Path, Path, str]:
    """Create a self-proving synthetic snapshot plus its source pricing file.

    Writes the snapshot to ``tmp_path/snapshot.json`` and a fake source
    pricing document to ``tmp_path/<source_rel>``.  The snapshot's
    ``source_pricing_document_sha256`` is computed from the actual bytes
    written, so the loader's fail-closed provenance check succeeds.

    Returns ``(snapshot_path, source_path, source_sha256)``.
    """
    if source_content is None:
        source_content = b'{"rates": {"input": 1, "cached_input": 0.2, "output": 2}}'
    source_path = tmp_path / source_rel
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source_content)
    source_sha = hashlib.sha256(source_content).hexdigest()

    base: dict[str, object] = {
        "schema_version": "pvian.price-snapshot/v1",
        "snapshot_id": "snap-test-001",
        "created_at": "2026-08-05T00:00:00Z",
        "effective_at": "2026-08-03T00:00:00Z",
        "currency": "CNY",
        "source_pricing_document": source_rel,
        "source_pricing_document_sha256": source_sha,
        "pricing_version": "2026-08-03",
        "provider": "bailian",
        "region": "cn-beijing",
        "model": "deepseek-v4-flash-0731",
        "rates": {"input": 1, "cached_input": 0.2, "output": 2},
    }
    base.update(overrides)
    sans_digest = {k: v for k, v in base.items() if k != "snapshot_digest"}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    base["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8")
    return path, source_path, source_sha


def test_load_price_snapshot_valid(tmp_path: Path) -> None:
    """A well-formed snapshot with correct digest and provenance loads successfully."""
    path, _src, _sha = _make_snapshot(tmp_path)
    data = load_price_snapshot(path, base_dir=tmp_path)
    assert data["schema_version"] == "pvian.price-snapshot/v1"
    assert data["provider"] == "bailian"
    assert data["rates"]["input"] == 1


def test_load_price_snapshot_tampered_digest_rejected(tmp_path: Path) -> None:
    """A snapshot with a tampered digest is rejected."""
    path, _src, _sha = _make_snapshot(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["snapshot_digest"] = "0" * 64
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="digest"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_tampered_source_sha_rejected(tmp_path: Path) -> None:
    """Recomputed digest but tampered source_pricing_document_sha256 is rejected.

    This is the key fail-closed guard: a snapshot that is self-consistent
    (digest matches its content) but claims a *wrong* source provenance must
    be rejected once the source file is verified.
    """
    path, _src, _sha = _make_snapshot(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["source_pricing_document_sha256"] = "a" * 64
    sans_digest = {k: v for k, v in raw.items() if k != "snapshot_digest"}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="source_pricing_document_sha256 mismatch"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_missing_source_file_rejected(tmp_path: Path) -> None:
    """Fail-closed: a source file that does not exist is rejected, not skipped."""
    path, src, _sha = _make_snapshot(tmp_path)
    src.unlink()  # remove the source pricing file
    with pytest.raises(SnapshotValidationError, match="not found or not a regular file"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_wrong_provider_loads_when_source_ok(tmp_path: Path) -> None:
    """A provider mismatch alone does not break provenance — different concern.

    The loader validates structure + provenance; provider/region/model policy
    matching is the consumer's responsibility (e.g. the experiment preflight).
    Here the source file exists and its SHA matches, so the snapshot loads.
    """
    path, _src, _sha = _make_snapshot(tmp_path, provider="other-provider")
    raw = json.loads(path.read_text(encoding="utf-8"))
    sans_digest = {k: v for k, v in raw.items() if k != "snapshot_digest"}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    data = load_price_snapshot(path, base_dir=tmp_path)
    assert data["provider"] == "other-provider"


def test_load_price_snapshot_negative_rate_rejected(tmp_path: Path) -> None:
    """A snapshot with a negative rate is rejected."""
    path, _src, _sha = _make_snapshot(
        tmp_path, rates={"input": -1, "cached_input": 0.2, "output": 2}
    )
    with pytest.raises(SnapshotValidationError, match="negative"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_wrong_rate_keys_rejected(tmp_path: Path) -> None:
    """Rates must have exactly input/cached_input/output — missing or extra keys rejected."""
    # Missing cached_input
    path, _src, _sha = _make_snapshot(
        tmp_path, rates={"input": 1, "output": 2}
    )
    with pytest.raises(SnapshotValidationError, match="rates keys mismatch"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_extra_rate_key_rejected(tmp_path: Path) -> None:
    """An unexpected rate key is rejected."""
    path, _src, _sha = _make_snapshot(
        tmp_path,
        rates={"input": 1, "cached_input": 0.2, "output": 2, "bonus": 0.5},
    )
    with pytest.raises(SnapshotValidationError, match="rates keys mismatch"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_missing_field_rejected(tmp_path: Path) -> None:
    """A snapshot missing a required field is rejected."""
    path, _src, _sha = _make_snapshot(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["model"]
    sans_digest = {k: v for k, v in raw.items() if k != "snapshot_digest"}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="missing required field"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_wrong_schema_rejected(tmp_path: Path) -> None:
    """A snapshot with wrong schema_version is rejected."""
    base: dict[str, object] = {
        "schema_version": "pvian.local-pricing/v1",
        "snapshot_id": "snap-test-001",
    }
    sans_digest = {k: v for k, v in base.items()}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    base["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="unsupported snapshot schema"):
        load_price_snapshot(path, base_dir=tmp_path)


def test_load_price_snapshot_source_document_sha256_verified() -> None:
    """The real bundled snapshot passes source document SHA-256 verification.

    This exercises the real (non-synthetic) path: the loader resolves the
    source pricing file relative to the repo root and verifies its hash.
    """
    root = Path(__file__).parents[2]
    snapshot_path = (
        root / "config" / "pricing" / "snapshots"
        / "bailian-cn-beijing-deepseek-v4-flash-0731.json"
    )
    if not snapshot_path.exists():
        pytest.skip("bundled snapshot not present")
    data = load_price_snapshot(snapshot_path, base_dir=root)
    assert data["provider"] == "bailian"
    assert data["model"] == "deepseek-v4-flash-0731"
    assert data["snapshot_digest"] != ""


# ---------------------------------------------------------------------------
# F: Pricing snapshot response builder (GET /v1/gateway/pricing/snapshots)
# ---------------------------------------------------------------------------


def _v2_pricing(**overrides: object) -> dict[str, object]:
    """A minimal v2 pricing payload with source provenance for snapshot tests."""
    base: dict[str, object] = {
        "schema_version": "pvian.local-pricing/v2",
        "pricing_version": "2026-08-09-official-list",
        "effective_at": None,
        "captured_at": "2026-08-09T00:00:00Z",
        "source": {
            "kind": "official_documentation",
            "url": "https://help.aliyun.com/zh/model-studio/glm-5-2",
            "price_basis": "advertised_list_price",
            "discounts_included": False,
            "captured_via": "official_page_field_capture",
            "content_sha256": None,
            "content_digest_status": "unavailable_dynamic_page_not_archived",
        },
        "provider": "bailian",
        "profile_id": "provider-bailian-cn-beijing-glm-5-2",
        "region": "cn-beijing",
        "model": "glm-5.2",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "tiers": [
            {
                "input_tokens_gt": 0,
                "input_tokens_lte": 1048576,
                "rates": {"input": 8, "cached_input": 2, "output": 28},
            }
        ],
    }
    base.update(overrides)
    return base


def _v1_pricing(**overrides: object) -> dict[str, object]:
    """A minimal v1 (flat rates) pricing payload."""
    base: dict[str, object] = {
        "schema_version": "pvian.local-pricing/v1",
        "pricing_version": "2026-08-03",
        "effective_at": "2026-08-03T00:00:00Z",
        "provider": "bailian",
        "profile_id": "provider-bailian-cn-beijing-flash",
        "region": "cn-beijing",
        "model": "deepseek-v4-flash-0731",
        "currency": "CNY",
        "unit": "per_1m_tokens",
        "rates": {"input": 1, "cached_input": 0.2, "output": 2},
    }
    base.update(overrides)
    return base


def _load_single(pricing: dict[str, object], tmp_path: Path):
    (tmp_path / "p.json").write_text(json.dumps(pricing), encoding="utf-8")
    return load_pricing(tmp_path)[0]


def test_load_pricing_v2_carries_provenance(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    assert quote.captured_at == "2026-08-09T00:00:00Z"
    assert quote.source is not None
    assert quote.source["url"] == "https://help.aliyun.com/zh/model-studio/glm-5-2"
    assert quote.schema_version == "pvian.local-pricing/v2"


def test_load_pricing_v1_has_no_provenance(tmp_path: Path) -> None:
    quote = _load_single(_v1_pricing(), tmp_path)
    assert quote.captured_at is None
    assert quote.source is None
    assert quote.schema_version == "pvian.local-pricing/v1"


def test_build_pricing_snapshot_single_tier(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    snap = build_pricing_snapshot(
        quote,
        public_alias="pvian/bailian-cn-beijing/glm-5.2",
        route_id="bailian-cn-beijing:glm-5.2",
        upstream_model="glm-5.2",
    )
    assert snap["schema_version"] == "pvian.pricing-snapshot-response/v1"
    assert snap["snapshot_id"] == "snap-bailian-cn-beijing-glm-5.2-2026-08-09-official-list"
    assert len(snap["snapshot_digest"]) == 64
    # Rates renamed: input→prompt, cached_input→cached_prompt, output→completion
    assert snap["rates"] == {
        "prompt": 8.0,
        "cached_prompt": 2.0,
        "completion": 28.0,
        "unit": "per_1m_tokens",
    }
    assert snap["cache_mode"] == "explicit_cache"
    assert snap["provider"] == "bailian"
    assert snap["region"] == "cn-beijing"
    assert snap["currency"] == "CNY"
    assert snap["public_alias"] == "pvian/bailian-cn-beijing/glm-5.2"
    assert snap["route"] == "bailian-cn-beijing:glm-5.2"
    assert snap["upstream_model"] == "glm-5.2"
    assert snap["captured_at"] == "2026-08-09T00:00:00Z"
    assert snap["valid_until"] is None
    assert snap["source_urls"] == ["https://help.aliyun.com/zh/model-studio/glm-5-2"]
    assert snap["source_digests"] == []  # content_sha256 is null


def test_build_pricing_snapshot_cached_null(tmp_path: Path) -> None:
    pricing = _v2_pricing(
        tiers=[
            {
                "input_tokens_gt": 0,
                "input_tokens_lte": 1000000,
                "rates": {"input": 12, "cached_input": None, "output": 36},
            }
        ]
    )
    quote = _load_single(pricing, tmp_path)
    snap = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="m"
    )
    assert snap["rates"]["cached_prompt"] is None
    assert snap["cache_mode"] == "no_cache_rate"


def test_build_pricing_snapshot_digest_stable(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    snap_a = build_pricing_snapshot(
        quote, public_alias="pvian/a", route_id="r:a", upstream_model="glm-5.2"
    )
    snap_b = build_pricing_snapshot(
        quote, public_alias="pvian/a", route_id="r:a", upstream_model="glm-5.2"
    )
    assert snap_a["snapshot_id"] == snap_b["snapshot_id"]
    assert snap_a["snapshot_digest"] == snap_b["snapshot_digest"]


def test_build_pricing_snapshot_price_change_new_id(tmp_path: Path) -> None:
    quote_a = _load_single(_v2_pricing(), tmp_path)

    # Second pricing with a different pricing_version → new snapshot_id.
    (tmp_path / "p.json").unlink()
    quote_b = _load_single(_v2_pricing(pricing_version="2026-09-01-official-list"), tmp_path)

    snap_a = build_pricing_snapshot(
        quote_a, public_alias=None, route_id=None, upstream_model="glm-5.2"
    )
    snap_b = build_pricing_snapshot(
        quote_b, public_alias=None, route_id=None, upstream_model="glm-5.2"
    )
    assert snap_a["snapshot_id"] != snap_b["snapshot_id"]
    assert snap_a["snapshot_digest"] != snap_b["snapshot_digest"]


def test_build_pricing_snapshot_v1(tmp_path: Path) -> None:
    quote = _load_single(_v1_pricing(), tmp_path)
    snap = build_pricing_snapshot(
        quote,
        public_alias="pvian/bailian-cn-beijing/deepseek-v4-flash-0731",
        route_id="bailian-cn-beijing:deepseek-v4-flash-0731",
        upstream_model="deepseek-v4-flash-0731",
    )
    assert snap["captured_at"] is None
    assert snap["source_urls"] == []
    assert snap["source_digests"] == []
    assert snap["rates"]["prompt"] == 1.0
    assert snap["rates"]["cached_prompt"] == 0.2
    assert snap["rates"]["completion"] == 2.0


def test_build_pricing_snapshot_with_content_sha(tmp_path: Path) -> None:
    pricing = _v2_pricing()
    pricing["source"]["content_sha256"] = "a" * 64  # type: ignore[index]
    pricing["source"]["content_digest_status"] = "verified"  # type: ignore[index]
    quote = _load_single(pricing, tmp_path)
    snap = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2"
    )
    assert len(snap["source_digests"]) == 1
    assert snap["source_digests"][0]["kind"] == "content_sha256"
    assert snap["source_digests"][0]["value"] == "a" * 64
    assert snap["source_digests"][0]["status"] == "verified"


# ---------------------------------------------------------------------------
# Token limits (output_limits injection into pricing snapshots)
# ---------------------------------------------------------------------------


def _token_limits_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema_version": "pvian.model-token-limits/v1",
        "captured_at": "2026-08-13T00:00:00Z",
        "source_pages": ["https://help.aliyun.com/zh/model-studio/deep-thinking"],
        "limits": [
            {
                "provider": "bailian",
                "region": "cn-beijing",
                "upstream_model": "glm-5.2",
                "context_window": 1048576,
                "max_output_tokens": 131072,
                "max_thinking_chain_tokens": 131072,
                "output_parameter": "max_tokens",
                "thinking_default": {
                    "state": "on",
                    "default_effort": "high",
                    "default_thinking_budget": 131072,
                    "default_effort_confirmation": "cross_source_unconfirmed",
                },
                "thinking_controls": ["enable_thinking", "thinking_budget"],
                "recommended_defaults": {
                    "send": {},
                    "omit": [],
                    "required_non_reasoning": {},
                },
                "confirmation": "confirmed",
                "source_url": "https://help.aliyun.com/zh/model-studio/glm-5-2",
                "notes": None,
            }
        ],
    }
    doc.update(overrides)
    return doc


def _write_limits(doc: dict[str, object], tmp_path: Path) -> Path:
    path = tmp_path / "model-token-limits.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_load_token_limits_indexes_by_provider_region_model(tmp_path: Path) -> None:
    path = _write_limits(_token_limits_doc(), tmp_path)
    index = load_token_limits(path)
    assert set(index.keys()) == {("bailian", "cn-beijing", "glm-5.2")}
    limits = index[("bailian", "cn-beijing", "glm-5.2")]
    assert isinstance(limits, TokenLimits)
    assert limits.context_window == 1048576
    assert limits.max_output_tokens == 131072
    assert limits.max_thinking_chain_tokens == 131072
    assert limits.thinking_controls == ("enable_thinking", "thinking_budget")


def test_load_token_limits_rejects_bad_schema_version(tmp_path: Path) -> None:
    path = _write_limits(_token_limits_doc(schema_version="pvian.wrong/v1"), tmp_path)
    with pytest.raises(AccountingError):
        load_token_limits(path)


def test_load_token_limits_allows_null_thinking_chain(tmp_path: Path) -> None:
    doc = _token_limits_doc()
    doc["limits"][0]["max_thinking_chain_tokens"] = None  # type: ignore[index]
    path = _write_limits(doc, tmp_path)
    limits = load_token_limits(path)[("bailian", "cn-beijing", "glm-5.2")]
    assert limits.max_thinking_chain_tokens is None


def test_build_pricing_snapshot_injects_output_limits(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    limits = TokenLimits(
        provider="bailian",
        region="cn-beijing",
        upstream_model="glm-5.2",
        context_window=1048576,
        max_output_tokens=131072,
        max_thinking_chain_tokens=131072,
        output_parameter="max_tokens",
        thinking_default=ThinkingDefault(
            state="on",
            default_effort="high",
            default_thinking_budget=131072,
            default_effort_confirmation="cross_source_unconfirmed",
        ),
        thinking_controls=("enable_thinking", "thinking_budget"),
        recommended_defaults=RecommendedDefaults(
            send={}, omit=(), required_non_reasoning={}
        ),
        confirmation="confirmed",
        source_url="https://help.aliyun.com/zh/model-studio/glm-5-2",
        notes=None,
    )
    snap = build_pricing_snapshot(
        quote,
        public_alias="pvian/a",
        route_id="r:a",
        upstream_model="glm-5.2",
        limits=limits,
    )
    assert snap["output_limits"] == {
        "context_window": 1048576,
        "max_output_tokens": 131072,
        "max_thinking_chain_tokens": 131072,
        "output_parameter": "max_tokens",
        "thinking_default": {
            "state": "on",
            "default_effort": "high",
            "default_thinking_budget": 131072,
            "default_effort_confirmation": "cross_source_unconfirmed",
        },
        "thinking_controls": ["enable_thinking", "thinking_budget"],
        "recommended_defaults": {"send": {}, "omit": [], "required_non_reasoning": {}},
        "confirmation": "confirmed",
        "source_url": "https://help.aliyun.com/zh/model-studio/glm-5-2",
        "notes": None,
    }
    assert len(snap["snapshot_digest"]) == 64


def test_build_pricing_snapshot_output_limits_null_when_absent(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    snap = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2"
    )
    assert snap["output_limits"] is None


def test_build_pricing_snapshot_digest_covers_output_limits(tmp_path: Path) -> None:
    """limits content must be part of the digest — changing it changes the digest."""

    quote = _load_single(_v2_pricing(), tmp_path)
    base = TokenLimits(
        provider="bailian",
        region="cn-beijing",
        upstream_model="glm-5.2",
        context_window=1048576,
        max_output_tokens=131072,
        max_thinking_chain_tokens=131072,
        output_parameter="max_tokens",
        thinking_default=ThinkingDefault(
            state="on", default_effort="high", default_thinking_budget=131072,
            default_effort_confirmation="cross_source_unconfirmed",
        ),
        thinking_controls=("enable_thinking",),
        recommended_defaults=RecommendedDefaults(
            send={}, omit=(), required_non_reasoning={}
        ),
        confirmation="confirmed",
        source_url="https://help.aliyun.com/zh/model-studio/glm-5-2",
        notes=None,
    )
    snap_a = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2", limits=base
    )
    changed = TokenLimits(
        provider="bailian",
        region="cn-beijing",
        upstream_model="glm-5.2",
        context_window=1048576,
        max_output_tokens=32768,  # different ceiling
        max_thinking_chain_tokens=81920,
        output_parameter="max_tokens",
        thinking_default=ThinkingDefault(
            state="off", default_effort=None, default_thinking_budget=81920,
            default_effort_confirmation="confirmed",
        ),
        thinking_controls=("enable_thinking",),
        recommended_defaults=RecommendedDefaults(
            send={"enable_thinking": True}, omit=("reasoning_effort",),
            required_non_reasoning={},
        ),
        confirmation="confirmed",
        source_url="https://help.aliyun.com/zh/model-studio/glm-5-2",
        notes=None,
    )
    snap_b = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2", limits=changed
    )
    # snapshot_id is price-derived, so unchanged; but digest must differ.
    assert snap_a["snapshot_id"] == snap_b["snapshot_id"]
    assert snap_a["snapshot_digest"] != snap_b["snapshot_digest"]


def test_build_pricing_snapshot_digest_stable_with_limits(tmp_path: Path) -> None:
    quote = _load_single(_v2_pricing(), tmp_path)
    limits = TokenLimits(
        provider="bailian",
        region="cn-beijing",
        upstream_model="glm-5.2",
        context_window=1048576,
        max_output_tokens=131072,
        max_thinking_chain_tokens=131072,
        output_parameter="max_tokens",
        thinking_default=ThinkingDefault(
            state="on", default_effort="high", default_thinking_budget=131072,
            default_effort_confirmation="cross_source_unconfirmed",
        ),
        thinking_controls=("enable_thinking", "thinking_budget"),
        recommended_defaults=RecommendedDefaults(
            send={}, omit=(), required_non_reasoning={}
        ),
        confirmation="confirmed",
        source_url="https://help.aliyun.com/zh/model-studio/glm-5-2",
        notes=None,
    )
    snap_a = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2", limits=limits
    )
    snap_b = build_pricing_snapshot(
        quote, public_alias=None, route_id=None, upstream_model="glm-5.2", limits=limits
    )
    assert snap_a["snapshot_digest"] == snap_b["snapshot_digest"]
