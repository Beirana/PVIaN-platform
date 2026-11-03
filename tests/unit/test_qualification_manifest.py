from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pvi.probe_runner.qualification_manifest import (
    QualificationManifestError,
    qualification_manifest_digest,
    validate_qualification_manifest,
)

ROOT = Path(__file__).parents[2]
SCHEMA = json.loads(
    (ROOT / "contracts" / "schemas" / "qualification-manifest.schema.json").read_text(
        encoding="utf-8"
    )
)


def _manifest(*, model: str = "qwen3.8-max", alias: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "pvian.provider-qualification-manifest/v1",
        "manifest_id": "qualify-qwen3-8-max-v1",
        "evidence_class": "provider_probe",
        "identity": {
            "provider": "bailian",
            "region": "cn-beijing",
            "requested_model": model,
            "route_id": f"bailian-cn-beijing:{model}",
            "profile_id": "provider-bailian-cn-beijing-qwen3-8-max",
            "profile_version": "1.0.0",
            "public_alias": alias,
            "gateway_smoke_eligible": alias is not None,
            "model_comparability": "homogeneous_text_candidate",
        },
        "request": {
            "transport": "openai_compatible_chat_completions",
            "stream": False,
            "max_output_parameter": "max_tokens",
            "max_output_tokens": 256,
            "context_window": {
                "tokens": 1000000,
                "status": "official_documented",
                "source": "https://example.invalid/official-model-card",
            },
            "timeout_seconds": 180,
            "retry_total_attempts": 1,
            "fallback": None,
            "parameters": [
                {"name": "enable_thinking", "request_state": "sent", "value": True},
                {"name": "max_tokens", "request_state": "sent", "value": 256},
                {"name": "seed", "request_state": "unsupported", "value": None},
            ],
            "seed_support": "unsupported",
        },
        "probe_plan": {
            "probes": ["message", "thinking", "usage"],
            "request_variant_ids": ["enable_thinking_true"],
            "catalog_gets": 1,
            "completion_calls": 2,
            "max_completion_calls": 2,
            "input_token_cap_total": 128,
            "output_token_cap_total": 512,
        },
        "observations": {
            "requested_model": "persist_exact",
            "reported_model": "persist_exact_or_absent",
            "gateway_request_id": "persist_opaque_exact_or_absent",
            "provider_request_id": "persist_sha256_or_absent",
            "attempt": "persist_ordinal_and_terminal_status",
            "finish_reason": "persist_safe_enum_or_absent",
            "error": "category_and_status_only_no_remote_body",
            "usage": "persist_normalized_or_mark_unknown",
            "cost": "persist_estimate_or_mark_unknown",
            "elapsed_ms": "persist_nonnegative_integer_or_absent",
        },
        "pricing": {
            "currency": "CNY",
            "pricing_version": "official-captured-2026-08-09",
            "input_per_1m": "12",
            "cached_input_per_1m": "0",
            "output_per_1m": "36",
            "source_document": "config/pricing/qwen3-8-max.json",
            "source_document_sha256": "a" * 64,
            "captured_at": "2026-08-09T00:00:00Z",
            "maximum_cost": "0.019968",
            "maximum_cost_formula": (
                "(input_token_cap_total*max(input_per_1m,"
                "coalesce(cached_input_per_1m,input_per_1m))"
                "+output_token_cap_total*output_per_1m)/1000000"
            ),
        },
        "readiness": {
            "status": "blocked",
            "blockers": ["live_probe_not_authorized"],
        },
        "stop_conditions": [
            "catalog_exact_model_absent",
            "requested_reported_model_mismatch",
            "unexpected_parameter_rejection",
            "missing_required_usage",
            "missing_required_finish_reason",
            "transport_or_provider_error",
            "cost_cap_would_be_exceeded",
            "manifest_or_profile_drift",
        ],
        "content_policy": (
            "no_prompt_no_question_no_choices_no_private_reasoning_"
            "no_raw_provider_body_no_remote_error_text"
        ),
    }
    value["manifest_digest"] = qualification_manifest_digest(value)
    return value


def test_manifest_separates_provider_probe_and_freezes_exact_budget() -> None:
    validate_qualification_manifest(_manifest(), SCHEMA)


@pytest.mark.parametrize("field", ["completion_calls", "max_completion_calls"])
def test_manifest_rejects_call_budget_drift(field: str) -> None:
    value = _manifest()
    value["probe_plan"][field] = 3
    value["manifest_digest"] = qualification_manifest_digest(value)
    with pytest.raises(QualificationManifestError, match="call budget"):
        validate_qualification_manifest(value, SCHEMA)


def test_manifest_rejects_raw_or_private_parameter_names() -> None:
    value = _manifest()
    value["request"]["parameters"].append(
        {"name": "raw_provider_body", "request_state": "sent", "value": "forbidden"}
    )
    value["manifest_digest"] = qualification_manifest_digest(value)
    with pytest.raises(QualificationManifestError, match="unsafe"):
        validate_qualification_manifest(value, SCHEMA)


def test_qwen_vl_is_interface_compatibility_only_for_the_text_bank() -> None:
    value = _manifest(model="qwen3-vl-plus")
    value["manifest_digest"] = qualification_manifest_digest(value)
    with pytest.raises(QualificationManifestError, match="not a homogeneous text"):
        validate_qualification_manifest(value, SCHEMA)
    value["identity"]["model_comparability"] = "interface_compatibility_only"
    value["manifest_digest"] = qualification_manifest_digest(value)
    validate_qualification_manifest(value, SCHEMA)


def test_candidate_without_public_alias_cannot_claim_gateway_smoke_eligibility() -> None:
    value = _manifest()
    value["identity"]["gateway_smoke_eligible"] = True
    value["manifest_digest"] = qualification_manifest_digest(value)
    with pytest.raises(QualificationManifestError, match="schema"):
        validate_qualification_manifest(value, SCHEMA)


def test_manifest_digest_detects_any_offline_drift() -> None:
    value = _manifest()
    drifted = copy.deepcopy(value)
    drifted["request"]["timeout_seconds"] = 181
    with pytest.raises(QualificationManifestError, match="digest"):
        validate_qualification_manifest(drifted, SCHEMA)


def test_unknown_cached_price_is_null_not_a_zero_placeholder() -> None:
    value = _manifest()
    value["pricing"]["cached_input_per_1m"] = None
    value["manifest_digest"] = qualification_manifest_digest(value)

    validate_qualification_manifest(value, SCHEMA)
