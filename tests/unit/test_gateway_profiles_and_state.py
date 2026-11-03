from __future__ import annotations

import json
import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pvian_gateway.profiles import ProfileError, load_profiles
from pvian_gateway.state import QualificationStateError, QualificationStateStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# An active lifecycle profile used to build enabled state fixtures. The yunwu
# profile was downgraded to candidate, so state fixtures bind the bailian
# deepseek-v4-pro profile instead.
STATE_ROUTE_KEY = "bailian-cn-beijing:deepseek-v4-pro"
STATE_PROFILE_ID = "provider-bailian-cn-beijing"
STATE_PROFILE_VERSION = "0.2.1"
STATE_PROVIDER_ID = "provider-bailian"
STATE_ENDPOINT_ID = "endpoint-bailian-cn-beijing-openai-compatible"


def _record_qualification_in_child(path_text: str, ready: object, start: object) -> None:
    ready.set()
    if not start.wait(10):
        raise RuntimeError("concurrent state test did not start")
    QualificationStateStore(Path(path_text)).record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version=STATE_PROFILE_VERSION,
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=24,
        failure_threshold=5,
        observed_at=datetime(2026, 7, 31, 2, tzinfo=UTC),
    )


def _record_gateway_runtime_in_child(
    path_text: str, operation: str, ready: object, start: object
) -> None:
    ready.set()
    if not start.wait(10):
        raise RuntimeError("concurrent state test did not start")
    store = QualificationStateStore(Path(path_text))
    observed_at = datetime(2026, 7, 31, 2, 1, tzinfo=UTC)
    if operation == "success":
        store.record_success(STATE_ROUTE_KEY, observed_at=observed_at)
    else:
        store.record_failure(STATE_ROUTE_KEY, "upstream_5xx", observed_at=observed_at)


def _hold_lock_and_update_in_child(path_text: str, acquired: object, release: object) -> None:
    store = QualificationStateStore(Path(path_text))
    with store._locked_payload() as payload:
        payload["records"][STATE_ROUTE_KEY]["profile_version"] = STATE_PROFILE_VERSION
        payload["records"][STATE_ROUTE_KEY]["reported_model"] = "deepseek-v4-pro"
        payload["records"][STATE_ROUTE_KEY]["reported_model_identity"] = {
            "schema_version": "pvian.reported-model-identity-decision/v1",
            "policy_id": "pvian.reported-model-identity/exact-v1",
            "match": "exact",
        }
        store._write(payload)
        acquired.set()
        if not release.wait(10):
            raise RuntimeError("concurrent state test did not release the lock")


def _record_success_after_start_in_child(path_text: str, started: object, finished: object) -> None:
    started.set()
    QualificationStateStore(Path(path_text)).record_success(
        STATE_ROUTE_KEY, observed_at=datetime(2026, 7, 31, 2, 1, tzinfo=UTC)
    )
    finished.set()


def test_profiles_keep_models_and_enablement_in_versioned_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    yunwu = profiles["provider-yunwu-openai-compatible"]
    bailian = profiles["provider-bailian-cn-beijing"]

    assert yunwu.policy.validity_hours == 168
    assert yunwu.policy.consecutive_failure_threshold == 5
    assert bailian.policy.required_probes == (
        "message",
        "stream",
        "thinking",
        "native_tool_call",
        "assistant_replay",
        "tool_result_replay",
    )
    assert yunwu.targets[0].select_from_catalog({"deepseek-v4-pro"}) == "deepseek-v4-pro"
    assert yunwu.targets[0].select_from_catalog({"other-model"}) is None
    assert yunwu.declares_upstream_model("deepseek-v4-pro") is True
    assert yunwu.declares_upstream_model("deepseek-v4-flash-0731") is False

    monkeypatch.setenv("PVI_PROBE_YUNWU_BASE_URL", "https://yunwu.ai/v1")
    resolved = yunwu.resolve_endpoint(yunwu.endpoints[0])
    assert resolved.chat_url() == "https://yunwu.ai/v1/chat/completions"
    assert "api_key" not in resolved.safe_summary()
    assert bailian.endpoints[0].required_base_path == "/compatible-mode/v1"


def test_deepseek_official_profile_supports_pathless_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    profile = profiles["provider-deepseek-official-v4-pro"]
    assert profile.endpoints[0].required_base_path == ""
    assert profile.lifecycle == "active"

    monkeypatch.setenv("PVI_PROBE_DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com")
    resolved = profile.resolve_endpoint(profile.endpoints[0])
    assert resolved.base_url == "https://api.deepseek.com"
    assert resolved.chat_url() == "https://api.deepseek.com/chat/completions"
    assert resolved.catalog_url() == "https://api.deepseek.com/models"

    monkeypatch.setenv("PVI_PROBE_DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    with pytest.raises(ProfileError):
        profile.resolve_endpoint(profile.endpoints[0])


def test_profiles_reject_non_allowlisted_or_non_https_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")[
        "provider-yunwu-openai-compatible"
    ]
    monkeypatch.setenv("PVI_PROBE_YUNWU_BASE_URL", "http://yunwu.ai/v1")
    with pytest.raises(ProfileError):
        profile.resolve_endpoint(profile.endpoints[0])


def test_profiles_reject_unknown_lifecycle(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "pvi.provider-qualification-profile/v1",
                "profile_id": "invalid-lifecycle",
                "profile_version": "1.0.0",
                "lifecycle": "disabled",
                "provider": {
                    "provider_id": "synthetic",
                    "display_name": "Synthetic",
                    "credential_environment_variable": "SYNTHETIC_KEY",
                },
                "endpoints": [],
                "model_selection": {"targets": []},
                "qualification": {"request_variants": [], "enablement": {}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="lifecycle"):
        load_profiles(tmp_path)


def test_state_expires_after_24_hours_and_requires_revalidation_after_five_failures(
    tmp_path: Path,
) -> None:
    state = QualificationStateStore(tmp_path / "qualification-state.json")
    now = datetime(2026, 7, 31, 1, tzinfo=UTC)
    record = state.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id="provider-yunwu-openai-compatible",
        profile_version="0.2.0",
        provider_id="provider-yunwu",
        endpoint_id="endpoint-yunwu-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=24,
        failure_threshold=5,
        observed_at=now,
    )

    assert record["valid_until"] == "2026-08-01T01:00:00Z"
    assert state.callable_record(STATE_ROUTE_KEY, now + timedelta(hours=23))
    assert state.callable_record(STATE_ROUTE_KEY, now + timedelta(hours=24)) is None

    for _ in range(5):
        state.record_failure(STATE_ROUTE_KEY, "upstream_5xx", observed_at=now)
    degraded = state.get(STATE_ROUTE_KEY)
    assert degraded is not None
    assert degraded["operational_status"] == "degraded"
    assert degraded["revalidation_due"] is True
    assert state.callable_record(STATE_ROUTE_KEY, now + timedelta(hours=1)) is None

    serialized = (tmp_path / "qualification-state.json").read_text(encoding="utf-8")
    assert "PVI_QUALIFY" not in serialized
    assert "reasoning_content" not in serialized
    assert json.loads(serialized)["schema_version"] == "pvian.qualification-state/v1"


def test_renewal_window_marks_routes_due_before_expiry_without_hiding_them(
    tmp_path: Path,
) -> None:
    state = QualificationStateStore(tmp_path / "qualification-state.json")
    now = datetime(2026, 8, 20, 4, tzinfo=UTC)
    state.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version="0.1.0",
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={},
        validity_hours=168,
        failure_threshold=5,
        observed_at=now,
    )

    # Fresh record: neither due nor inside the 24h renewal window.
    fresh = now + timedelta(hours=168 - 30)
    assert state.renewal_due(STATE_ROUTE_KEY, now=fresh) is False
    assert [r["route_id"] for r in state.due_records(now=fresh)] == []

    # Inside the final 24h: due for renewal, but still callable until expiry.
    final_day = now + timedelta(hours=168 - 23)
    assert state.renewal_due(STATE_ROUTE_KEY, now=final_day) is True
    assert [r["route_id"] for r in state.due_records(now=final_day)] == [STATE_ROUTE_KEY]
    assert state.callable_record(STATE_ROUTE_KEY, now=final_day) is not None

    # Unknown or non-enabled routes are always due so the qualifier retries them.
    assert state.renewal_due("missing:route", now=now) is True


@pytest.mark.parametrize("operation", ["success", "failure"])
def test_state_cross_process_lock_preserves_qualification_and_runtime_updates(
    tmp_path: Path, operation: str
) -> None:
    state_path = tmp_path / "qualification-state.json"
    store = QualificationStateStore(state_path)
    store.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version="0.1.0",
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="old-model",
        passed=True,
        capability_summary={},
        validity_hours=24,
        failure_threshold=5,
        observed_at=datetime(2026, 7, 31, 1, tzinfo=UTC),
    )
    context = multiprocessing.get_context("spawn")
    qualification_ready = context.Event()
    runtime_ready = context.Event()
    start = context.Event()
    qualification = context.Process(
        target=_record_qualification_in_child,
        args=(str(state_path), qualification_ready, start),
    )
    runtime = context.Process(
        target=_record_gateway_runtime_in_child,
        args=(str(state_path), operation, runtime_ready, start),
    )
    qualification.start()
    runtime.start()
    assert qualification_ready.wait(10)
    assert runtime_ready.wait(10)
    start.set()
    qualification.join(10)
    runtime.join(10)
    assert qualification.exitcode == 0
    assert runtime.exitcode == 0

    record = QualificationStateStore(state_path).get(STATE_ROUTE_KEY)
    assert record is not None
    assert record["profile_version"] == STATE_PROFILE_VERSION
    assert record["reported_model"] == "deepseek-v4-pro"
    assert record["request_count"] == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == (
        "pvian.qualification-state/v1"
    )
    assert state_path.with_name(f"{state_path.name}.lock").exists()
    if operation == "success":
        assert record["consecutive_failures"] == 0
        assert record["last_success_at"] == "2026-07-31T02:01:00Z"
    else:
        assert record["consecutive_failures"] == 1
        assert record["last_failure_category"] == "upstream_5xx"


def test_state_cross_process_lock_blocks_then_exposes_committed_update(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    QualificationStateStore(state_path).record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version="0.1.0",
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="old-model",
        passed=True,
        capability_summary={},
        validity_hours=24,
        failure_threshold=5,
        observed_at=datetime(2026, 7, 31, 1, tzinfo=UTC),
    )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    started = context.Event()
    finished = context.Event()
    holder = context.Process(
        target=_hold_lock_and_update_in_child,
        args=(str(state_path), acquired, release),
    )
    runtime = context.Process(
        target=_record_success_after_start_in_child,
        args=(str(state_path), started, finished),
    )
    holder.start()
    assert acquired.wait(10)
    runtime.start()
    assert started.wait(10)
    assert finished.wait(0.2) is False
    release.set()
    holder.join(10)
    runtime.join(10)
    assert holder.exitcode == 0
    assert runtime.exitcode == 0

    record = QualificationStateStore(state_path).get(STATE_ROUTE_KEY)
    assert record is not None
    assert record["profile_version"] == STATE_PROFILE_VERSION
    assert record["reported_model"] == "deepseek-v4-pro"
    assert record["request_count"] == 1


def test_qualification_preserves_runtime_observation_at_the_same_timestamp(tmp_path: Path) -> None:
    state = QualificationStateStore(tmp_path / "qualification-state.json")
    observed_at = datetime(2026, 7, 31, 2, tzinfo=UTC)
    state.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version="0.1.0",
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="old-model",
        passed=True,
        capability_summary={},
        validity_hours=24,
        failure_threshold=5,
        observed_at=observed_at - timedelta(minutes=1),
    )
    state.record_failure(STATE_ROUTE_KEY, "upstream_5xx", observed_at=observed_at)
    state.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version=STATE_PROFILE_VERSION,
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={},
        validity_hours=24,
        failure_threshold=5,
        observed_at=observed_at,
    )

    record = state.get(STATE_ROUTE_KEY)
    assert record is not None
    assert record["profile_version"] == STATE_PROFILE_VERSION
    assert record["consecutive_failures"] == 1
    assert record["last_failure_category"] == "upstream_5xx"


def test_state_write_failure_releases_lock_and_recovers_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "qualification-state.json"
    store = QualificationStateStore(state_path)
    store.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id="provider-yunwu-openai-compatible",
        profile_version="0.2.0",
        provider_id="provider-yunwu",
        endpoint_id="endpoint-yunwu-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={},
        validity_hours=24,
        failure_threshold=5,
    )
    import pvian_gateway.state as state_module

    replace = state_module.os.replace

    def fail_replace(source: object, target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        store.record_success(STATE_ROUTE_KEY)
    monkeypatch.setattr(state_module.os, "replace", replace)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    finished = context.Event()
    recovery = context.Process(
        target=_record_success_after_start_in_child,
        args=(str(state_path), started, finished),
    )
    recovery.start()
    assert started.wait(10)
    assert finished.wait(10)
    recovery.join(10)
    assert recovery.exitcode == 0
    recovered = QualificationStateStore(state_path).get(STATE_ROUTE_KEY)

    assert recovered is not None
    assert recovered["request_count"] == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == (
        "pvian.qualification-state/v1"
    )
    assert list(tmp_path.glob("tmp*")) == []


def _write_synthetic_state_record(
    state_path: Path, *, reported_model: str | None = "deepseek-v4-pro", passed: bool = True
) -> None:
    QualificationStateStore(state_path).record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version=STATE_PROFILE_VERSION,
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model=reported_model,
        passed=passed,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
    )


def test_candidate_with_a_nonmatching_reported_model_has_a_valid_profile_binding(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path, reported_model="nearby-model", passed=True)

    store = QualificationStateStore(state_path)
    store.validate_profile_bindings(load_profiles(PROJECT_ROOT / "config" / "provider-profiles"))
    assert store.get(STATE_ROUTE_KEY)["qualification_status"] == "candidate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_id", "unknown-profile"),
        ("profile_version", "0.0.0"),
        ("provider_id", "wrong-provider"),
        ("endpoint_id", "wrong-endpoint"),
        ("requested_model", "deepseek-v4-flash-0731"),
        ("reported_model", "nearby-model"),
        ("last_observed_at", "not-a-timestamp"),
        ("raw_response", "must-not-be-stored"),
    ],
)
def test_state_binding_validation_rejects_tampered_enabled_records(
    tmp_path: Path, field: str, value: str
) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record[field] = value
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(
            load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
        )


def test_state_binding_validation_rejects_bad_route_key_and_disabled_endpoint(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["records"]["wrong:deepseek-v4-pro"] = payload["records"].pop(STATE_ROUTE_KEY)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(profiles)

    _write_synthetic_state_record(state_path)
    profile = profiles[STATE_PROFILE_ID]
    endpoint = replace(profile.endpoints[0], qualification_enabled=False)
    disabled_profiles = {**profiles, profile.profile_id: replace(profile, endpoints=(endpoint,))}
    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(disabled_profiles)


def test_state_binding_validation_rejects_a_non_object_root(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    state_path.write_text("[]", encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings({})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-31T02:00:00",
        "2026-07-31T02:00:00+00:00",
        "2026-07-31T10:00:00+08:00",
        "2026-07-31T02:00:00.000000Z",
    ],
)
def test_state_binding_validation_rejects_noncanonical_or_non_utc_timestamps(
    tmp_path: Path, timestamp: str
) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["records"][STATE_ROUTE_KEY]["last_observed_at"] = timestamp
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(
            load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
        )


def test_state_binding_validation_rejects_invalid_timestamp_relations(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["valid_until"] = record["qualified_at"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(
            load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
        )


def test_state_never_compares_naive_times(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    state = QualificationStateStore(state_path)

    assert state.callable_record(STATE_ROUTE_KEY, datetime(2026, 7, 31)) is None
    with pytest.raises(QualificationStateError):
        state.due_records(datetime(2026, 7, 31))
    with pytest.raises(QualificationStateError):
        state.record_success(STATE_ROUTE_KEY, observed_at=datetime(2026, 7, 31))


@pytest.mark.parametrize("status", ["enabled", "candidate"])
@pytest.mark.parametrize("nested_key", ["raw_response", "api_key", "prompt", "reasoning_content"])
def test_state_binding_validation_rejects_nested_capability_payloads(
    tmp_path: Path, status: str, nested_key: str
) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path, passed=status == "enabled")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    if status == "candidate":
        record["reported_model"] = "nearby-model"
    record["capabilities"] = {"message": {"status": "observed_supported", nested_key: "raw"}}
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).validate_profile_bindings(
            load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
        )


# ---------------------------------------------------------------------------
# A: Five-key canonical usage_totals + legacy 3-key backward compatibility
# ---------------------------------------------------------------------------


def test_new_qualification_writes_five_key_usage_totals(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    usage = payload["records"][STATE_ROUTE_KEY]["usage_totals"]
    assert set(usage) == {
        "prompt_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "completion_tokens",
        "total_tokens",
    }


def test_legacy_three_key_usage_totals_still_loads(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    # Manually downgrade to legacy 3-key structure.
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["usage_totals"] = {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    # Gateway must still load and use this state without error.
    store = QualificationStateStore(state_path)
    loaded = store.get(STATE_ROUTE_KEY)
    assert loaded is not None
    assert set(loaded["usage_totals"]) == {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }


def test_five_key_capability_usage_with_cached_and_reasoning_loads(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    store = QualificationStateStore(state_path)
    store.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id="provider-yunwu-openai-compatible",
        profile_version="0.2.0",
        provider_id="provider-yunwu",
        endpoint_id="endpoint-yunwu-openai-compatible",
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={
            "message": {"status": "observed_supported"},
            "usage": {
                "prompt_tokens": 10,
                "cached_tokens": 2,
                "reasoning_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 14,
            },
        },
        validity_hours=24,
        failure_threshold=5,
    )
    loaded = store.get(STATE_ROUTE_KEY)
    assert loaded is not None
    assert loaded["capabilities"]["usage"]["cached_tokens"] == 2
    assert loaded["capabilities"]["usage"]["reasoning_tokens"] == 3


# ---------------------------------------------------------------------------
# B: 502 -> 200 recovery clears failure timestamps as a pair
# ---------------------------------------------------------------------------


def test_success_after_failure_clears_both_failure_category_and_timestamp(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "qualification-state.json"
    store = QualificationStateStore(state_path)
    now = datetime(2026, 7, 31, 1, tzinfo=UTC)
    store.record_qualification(
        route_id=STATE_ROUTE_KEY,
        profile_id=STATE_PROFILE_ID,
        profile_version=STATE_PROFILE_VERSION,
        provider_id=STATE_PROVIDER_ID,
        endpoint_id=STATE_ENDPOINT_ID,
        requested_model="deepseek-v4-pro",
        reported_model="deepseek-v4-pro",
        passed=True,
        capability_summary={"message": {"status": "observed_supported"}},
        validity_hours=168,
        failure_threshold=5,
        observed_at=now,
    )
    # Simulate a 502.
    store.record_failure(
        STATE_ROUTE_KEY, "upstream_5xx", observed_at=now + timedelta(minutes=1)
    )
    degraded = store.get(STATE_ROUTE_KEY)
    assert degraded is not None
    assert degraded["last_failure_category"] == "upstream_5xx"
    assert degraded["last_failure_at"] is not None
    # Simulate recovery (200).
    store.record_success(
        STATE_ROUTE_KEY,  # gitleaks:allow -- route identifier, not a credential
        observed_at=now + timedelta(minutes=2)
    )
    recovered = store.get(STATE_ROUTE_KEY)
    assert recovered is not None
    assert recovered["last_failure_category"] is None
    assert recovered["last_failure_at"] is None
    assert recovered["consecutive_failures"] == 0
    assert recovered["operational_status"] == "nominal"
    # State must remain loadable after recovery.
    QualificationStateStore(state_path).validate_profile_bindings(
        load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    )


# ---------------------------------------------------------------------------
# C: Write-before self-validation and offline repair-state
# ---------------------------------------------------------------------------


def test_invalid_write_does_not_replace_original_file(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    original_content = state_path.read_text(encoding="utf-8")
    store = QualificationStateStore(state_path)
    # Inject an invalid record via direct manipulation, then attempt _write.
    import pvian_gateway.state as state_module

    real_validate = state_module._validate_state_payload

    def tamper_and_validate(payload: object) -> None:
        if isinstance(payload, dict) and "records" in payload:
            for record in payload["records"].values():
                if isinstance(record, dict):
                    record["usage_totals"] = {"prompt_tokens": -1}
        real_validate(payload)

    monkeypatch.setattr(state_module, "_validate_state_payload", tamper_and_validate)
    with pytest.raises(QualificationStateError):
        store.record_success(STATE_ROUTE_KEY)
    # Original file must be unchanged.
    assert state_path.read_text(encoding="utf-8") == original_content


def test_repair_state_migrates_legacy_three_key_to_five_key(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    # Downgrade to legacy 3-key.
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["usage_totals"] = {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    store = QualificationStateStore(state_path)
    result = store.repair_state()
    assert result["records_repaired"] == 1
    assert result["records_unchanged"] == 0
    # Backup must exist.
    assert state_path.with_suffix(".bak").exists()
    # Repaired state must have 5 keys.
    repaired = store.get(STATE_ROUTE_KEY)
    assert repaired is not None
    assert set(repaired["usage_totals"]) == {
        "prompt_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "completion_tokens",
        "total_tokens",
    }
    assert repaired["usage_totals"]["prompt_tokens"] == 10
    assert repaired["usage_totals"]["cached_tokens"] == 0


def test_repair_state_rejects_structurally_damaged_state(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    # Corrupt with an unknown sensitive field.
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["records"][STATE_ROUTE_KEY]["raw_response"] = "secret"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    store = QualificationStateStore(state_path)
    with pytest.raises(QualificationStateError):
        store.repair_state()
    # No backup should have been written because damage is unknown.
    # (backup is written only after validation passes)
    # State file must remain unchanged from what was on disk.
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert "raw_response" in on_disk["records"][STATE_ROUTE_KEY]


# ---------------------------------------------------------------------------
# A2 bad state: last_failure_category cleared but last_failure_at not
# ---------------------------------------------------------------------------


def test_repair_state_fixes_a2_failure_category_cleared_but_timestamp_not(
    tmp_path: Path,
) -> None:
    """Simulate pre-fix-B self-corruption: record_success cleared
    last_failure_category but left last_failure_at as a valid timestamp.
    repair_state must clear last_failure_at to restore the pair invariant."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    # Simulate the old bug: record_failure then old-style record_success
    # that only cleared category.
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["last_failure_category"] = None
    record["last_failure_at"] = "2026-07-31T02:01:00Z"
    record["last_success_at"] = "2026-07-31T02:02:00Z"
    record["last_observed_at"] = "2026-07-31T02:02:00Z"
    record["request_count"] = 1
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    # This state would fail _read() because the pair invariant is violated.
    # But repair_state uses _read_raw (envelope-only) and fixes it.

    store = QualificationStateStore(state_path)
    result = store.repair_state()
    assert result["records_repaired"] == 1

    repaired = store.get(STATE_ROUTE_KEY)
    assert repaired is not None
    assert repaired["last_failure_category"] is None
    assert repaired["last_failure_at"] is None
    # State must be loadable after repair.
    QualificationStateStore(state_path).validate_profile_bindings(
        load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    )


def test_repair_state_does_not_touch_reverse_pair_violation(tmp_path: Path) -> None:
    """last_failure_category is set but last_failure_at is None — not a known
    historical issue; repair_state must reject it, not guess a fix."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["last_failure_category"] = "upstream_5xx"
    record["last_failure_at"] = None
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    store = QualificationStateStore(state_path)
    with pytest.raises(QualificationStateError):
        store.repair_state()


def test_repair_state_rejects_bad_timestamp_in_failure_at(tmp_path: Path) -> None:
    """last_failure_category is None and last_failure_at is a non-timestamp
    string — repair cannot fix this; must reject."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["records"][STATE_ROUTE_KEY]
    record["last_failure_category"] = None
    record["last_failure_at"] = "not-a-timestamp"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    store = QualificationStateStore(state_path)
    with pytest.raises(QualificationStateError):
        store.repair_state()


# ---------------------------------------------------------------------------
# D: known_usage_attempts — machine-readable known/unknown distinction
# ---------------------------------------------------------------------------


def test_new_qualification_record_has_known_usage_attempts_zero(tmp_path: Path) -> None:
    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["records"][STATE_ROUTE_KEY]["known_usage_attempts"] == 0


def test_record_success_increments_known_usage_attempts_only_with_usage(
    tmp_path: Path,
) -> None:
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
    known_usage = {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    # Known usage: increments known_usage_attempts.
    store.record_success("synthetic:model", usage=known_usage, observed_at=now)
    # Unknown usage: does NOT increment known_usage_attempts.
    store.record_success("synthetic:model", usage=None, observed_at=now)
    record = store.get("synthetic:model")
    assert record is not None
    assert record["request_count"] == 2
    assert record["known_usage_attempts"] == 1


def test_request_count_gt_zero_with_unknown_usage_not_misread_as_zero(tmp_path: Path) -> None:
    """D: request_count>0 + known_usage_attempts=0 + zero totals must not
    be expressible as 'zero tokens consumed' — the known/unknown distinction
    makes this machine-readable."""

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
    # Unknown usage only.
    store.record_success("synthetic:model", usage=None, observed_at=now)
    record = store.get("synthetic:model")
    assert record is not None
    assert record["request_count"] == 1
    assert record["known_usage_attempts"] == 0
    assert record["usage_totals"]["prompt_tokens"] == 0
    # Machine-readable: request_count > known_usage_attempts means some
    # attempts have unknown usage. Not zero consumption.
    assert record["request_count"] > record["known_usage_attempts"]


def test_legacy_v1_record_without_known_usage_attempts_still_loads(tmp_path: Path) -> None:
    """D: existing state files without known_usage_attempts remain loadable."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    # Remove known_usage_attempts to simulate V1 record.
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    del payload["records"][STATE_ROUTE_KEY]["known_usage_attempts"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    store = QualificationStateStore(state_path)
    loaded = store.get(STATE_ROUTE_KEY)
    assert loaded is not None
    assert "known_usage_attempts" not in loaded
    # validate_profile_bindings must also accept V1 records.
    store.validate_profile_bindings(
        load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    )


@pytest.mark.parametrize("malformed", [True, "1", 1.5, -1])
def test_repair_state_rejects_malformed_legacy_usage_values(
    tmp_path: Path, malformed: object
) -> None:
    """Repair must not coerce malformed legacy token totals into valid data."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["records"][STATE_ROUTE_KEY]["usage_totals"] = {
        "prompt_tokens": malformed,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    original = json.dumps(payload)
    state_path.write_text(original, encoding="utf-8")

    with pytest.raises(QualificationStateError):
        QualificationStateStore(state_path).repair_state()

    assert state_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# E: Post-migration state integrity verification
# ---------------------------------------------------------------------------


def test_migration_preserves_totals_creates_backup_and_is_idempotent(tmp_path: Path) -> None:
    """Migration evidence must not depend on a mutable operator state file."""

    state_path = tmp_path / "qualification-state.json"
    _write_synthetic_state_record(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["records"][STATE_ROUTE_KEY]["usage_totals"] = {
        "prompt_tokens": 9,
        "completion_tokens": 9,
        "total_tokens": 18,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    store = QualificationStateStore(state_path)
    first = store.repair_state()
    assert first == {"records_repaired": 1, "records_unchanged": 0}
    assert state_path.with_suffix(".bak").exists()
    record = store.get(STATE_ROUTE_KEY)
    assert record is not None
    assert record["usage_totals"] == {
        "prompt_tokens": 9,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "completion_tokens": 9,
        "total_tokens": 18,
    }

    second = store.repair_state()
    assert second == {"records_repaired": 0, "records_unchanged": 1}


def test_loopback_base_url_allows_http_and_port() -> None:
    """Self-hosted local runtimes may use plain HTTP on an explicit loopback port."""
    from pvian_gateway.profiles import _normalize_base_url

    hosts = frozenset({"127.0.0.1"})
    normalized = _normalize_base_url("http://127.0.0.1:18000/v1", hosts, "/v1")
    assert normalized == "http://127.0.0.1:18000/v1"


def test_non_loopback_base_url_still_requires_https() -> None:
    """External provider hosts keep the strict HTTPS/default-port contract."""
    import pytest

    from pvian_gateway.profiles import ProfileError, _normalize_base_url

    hosts = frozenset({"api.example.com"})
    with pytest.raises(ProfileError, match="HTTPS"):
        _normalize_base_url("http://api.example.com/v1", hosts, "/v1")
    with pytest.raises(ProfileError, match="default HTTPS port"):
        _normalize_base_url("https://api.example.com:8443/v1", hosts, "/v1")
