from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pvi.domain.models import EvidenceReference, ProbeRun
from pvi.probe_runner.config import ProbeConfigurationError, ProbeSettings
from pvi.probe_runner.evidence import write_probe_bundle
from pvi.probe_runner.gate import NetworkAuthorizationError, require_network_authorization
from pvi.probe_runner.qualification import _qualify_model
from pvi.probe_runner.runner import OnlineProbeRunner
from pvian_gateway.state import QualificationStateStore

TEST_SECRET = "test-secret-must-never-be-written"


class FakeCompletions:
    def create(self, **request):
        if request.get("stream"):
            return [
                SimpleNamespace(
                    model="gpt-4o-mini-reported",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="PVI_"),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    model="gpt-4o-mini-reported",
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="STREAM_OK"),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                ),
                SimpleNamespace(
                    model="gpt-4o-mini-reported",
                    choices=[],
                    usage=SimpleNamespace(
                        prompt_tokens=8,
                        completion_tokens=3,
                        total_tokens=11,
                    ),
                ),
            ]

        usage = SimpleNamespace(
            prompt_tokens=8,
            completion_tokens=3,
            total_tokens=11,
        )
        if request.get("tools"):
            message = SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="pvi_echo_probe",
                            arguments='{"nonce":"pvi-probe-nonce"}',
                        )
                    )
                ],
            )
            finish_reason = "tool_calls"
        else:
            message = SimpleNamespace(content="PVI_TEXT_OK", tool_calls=None)
            finish_reason = "stop"
        return SimpleNamespace(
            id="chatcmpl-synthetic",
            model="gpt-4o-mini-reported",
            choices=[
                SimpleNamespace(
                    message=message,
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
            _request_id="request-synthetic",
        )


class FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


class FailingCompletions:
    def create(self, **request):
        raise RuntimeError(f"Authorization: Bearer {TEST_SECRET}")


class FailingClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FailingCompletions())


def make_settings() -> ProbeSettings:
    return ProbeSettings(
        provider_name="yunwu",
        provider_id="provider-yunwu",
        endpoint_id="endpoint-yunwu-openai-compatible",
        subject_profile_id="cap-yunwu-gpt-4o-mini",
        base_url="https://yunwu.ai/v1",
        model="gpt-4o-mini",
        api_key=TEST_SECRET,
    )


def test_settings_use_provider_specific_namespace() -> None:
    settings = ProbeSettings.from_env(
        "yunwu",
        {
            "PVI_PROBE_YUNWU_API_KEY": TEST_SECRET,
            "PVI_PROBE_YUNWU_BASE_URL": "https://yunwu.ai/v1/",
            "PVI_PROBE_YUNWU_MODEL": "gpt-4o-mini",
            "OPENAI_API_KEY": "must-not-be-used",
        },
    )

    assert settings.api_key == TEST_SECRET
    assert settings.base_url == "https://yunwu.ai/v1"
    assert "api_key" not in settings.safe_summary()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://yunwu.ai/v1",
        "https://example.com/v1",
        "https://user:password@yunwu.ai/v1",
        "https://yunwu.ai/v1?key=value",
        "https://yunwu.ai/v2",
    ],
)
def test_settings_reject_unsafe_base_urls(base_url: str) -> None:
    with pytest.raises(ProbeConfigurationError):
        ProbeSettings.from_env(
            "yunwu",
            {
                "PVI_PROBE_YUNWU_API_KEY": TEST_SECRET,
                "PVI_PROBE_YUNWU_BASE_URL": base_url,
                "PVI_PROBE_YUNWU_MODEL": "gpt-4o-mini",
            },
        )


def test_network_requires_two_explicit_flags_and_respects_offline_mode() -> None:
    with pytest.raises(NetworkAuthorizationError):
        require_network_authorization(
            allow_network=False,
            confirm_cost=True,
            environ={},
        )
    with pytest.raises(NetworkAuthorizationError):
        require_network_authorization(
            allow_network=True,
            confirm_cost=False,
            environ={},
        )
    with pytest.raises(NetworkAuthorizationError):
        require_network_authorization(
            allow_network=True,
            confirm_cost=True,
            environ={"PVI_NETWORK_DISABLED": "1"},
        )

    require_network_authorization(
        allow_network=True,
        confirm_cost=True,
        environ={},
    )


def test_runner_observes_text_tool_and_stream_without_raw_payloads() -> None:
    observations = OnlineProbeRunner(FakeClient(), make_settings()).run(["text", "tool", "stream"])

    assert [item.verdict for item in observations] == [
        "observed_supported",
        "observed_supported",
        "observed_supported",
    ]
    assert observations[0].details["exact_match"] is True
    assert observations[1].details["native_tool_call_observed"] is True
    assert observations[2].details["stream_observed"] is True
    assert all(item.reported_model == "gpt-4o-mini-reported" for item in observations)
    serialized = json.dumps([item.to_dict() for item in observations])
    assert TEST_SECRET not in serialized
    assert "Reply with exactly" not in serialized


def test_bundle_is_redacted_and_candidates_match_frozen_models(tmp_path: Path) -> None:
    settings = make_settings()
    observations = OnlineProbeRunner(FakeClient(), settings).run(["text", "tool"])

    session_dir = write_probe_bundle(
        output_root=tmp_path,
        settings=settings,
        observations=observations,
    )

    combined = "\n".join(path.read_text(encoding="utf-8") for path in session_dir.glob("*.json"))
    assert TEST_SECRET not in combined
    assert "contains_credentials" in combined
    assert '"contains_credentials": false' in combined

    evidence_files = sorted(session_dir.glob("candidate-evidence-*.json"))
    run_files = sorted(session_dir.glob("candidate-probe-run-*.json"))
    assert len(evidence_files) == 2
    assert len(run_files) == 2
    for path in evidence_files:
        EvidenceReference.model_validate_json(path.read_text(encoding="utf-8"))
    for path in run_files:
        ProbeRun.model_validate_json(path.read_text(encoding="utf-8"))


def test_provider_error_cannot_persist_the_key(tmp_path: Path) -> None:
    settings = make_settings()
    observations = OnlineProbeRunner(FailingClient(), settings).run(["text"])

    assert observations[0].verdict == "inconclusive"
    assert observations[0].error is not None
    assert TEST_SECRET not in observations[0].error["message"]
    assert "[REDACTED]" in observations[0].error["message"]

    session_dir = write_probe_bundle(
        output_root=tmp_path,
        settings=settings,
        observations=observations,
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in session_dir.glob("*.json"))
    assert TEST_SECRET not in combined


@pytest.mark.parametrize(
    ("requested_model", "reported_model", "expected_status"),
    [
        ("synthetic-v1", "synthetic-v1", "enabled"),
        ("synthetic-v1", "synthetic-v1-nearby", "candidate"),
        ("synthetic-v1", None, "candidate"),
    ],
)
def test_qualification_requires_an_exact_reported_model_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_model: str,
    reported_model: str | None,
    expected_status: str,
) -> None:
    """All probe observations are synthetic; no provider client is invoked."""

    import pvi.probe_runner.qualification as qualification

    profile = SimpleNamespace(
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        policy=SimpleNamespace(required_probes=("message", "stream")),
        request_variants=({"variant_id": "baseline"},),
    )
    supported = {"status": "observed_supported"}
    monkeypatch.setattr(
        qualification,
        "_run_baseline",
        lambda *_: {
            "message": supported,
            "stream": supported,
            "usage": supported,
            "reported_model": reported_model,
        },
    )
    monkeypatch.setattr(
        qualification,
        "_run_thinking",
        lambda *_: {"status": "not_observed", "variants": [], "reported_model": None},
    )
    monkeypatch.setattr(
        qualification,
        "_run_tool_and_replay",
        lambda *_: {
            "native_tool_call": {"status": "not_observed"},
            "assistant_replay": {"status": "not_observed"},
            "tool_result_replay": {"status": "not_observed"},
            "reported_model": None,
        },
    )

    result = _qualify_model(profile, None, object(), requested_model)
    record = QualificationStateStore(tmp_path / "state.json").record_qualification(
        route_id=f"synthetic:{requested_model}",
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        provider_id="synthetic-provider",
        endpoint_id="synthetic-endpoint",
        requested_model=requested_model,
        reported_model=result.reported_model,
        passed=True,
        capability_summary=result.capabilities,
        validity_hours=24,
        failure_threshold=5,
    )

    assert result.qualification_status == expected_status
    assert record["qualification_status"] == expected_status
    assert record["requested_model"] == requested_model
    assert record["reported_model"] == reported_model
    assert TEST_SECRET not in json.dumps(record)


def test_qualification_safe_usage_produces_five_key_canonical_summary() -> None:
    """A: qualification _safe_usage must produce the canonical 5-key structure."""

    from pvi.probe_runner.qualification import _safe_usage

    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            completion_tokens=4,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
            total_tokens=14,
        )
    )
    usage = _safe_usage(response)
    assert usage == {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }


def test_qualification_safe_usage_returns_none_for_missing_required_fields() -> None:
    """A: incomplete usage must yield None, not a partial 5-key dict."""

    from pvi.probe_runner.qualification import _safe_usage

    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=None,
            completion_tokens=4,
            total_tokens=4,
        )
    )
    assert _safe_usage(response) is None


def test_qualification_safe_usage_returns_none_for_negative_values() -> None:
    """A: negative or bool values must yield None."""

    from pvi.probe_runner.qualification import _safe_usage

    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=-1,
            completion_tokens=4,
            total_tokens=3,
        )
    )
    assert _safe_usage(response) is None


def test_qualification_state_writes_five_key_usage_totals_after_qualify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A: state usage_totals after qualification must have 5 keys."""

    import pvi.probe_runner.qualification as qualification

    profile = SimpleNamespace(
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        policy=SimpleNamespace(required_probes=("message", "stream")),
        request_variants=({"variant_id": "baseline"},),
    )
    five_key_usage = {
        "prompt_tokens": 10,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
    supported = {"status": "observed_supported"}
    monkeypatch.setattr(
        qualification,
        "_run_baseline",
        lambda *_: {
            "message": supported,
            "stream": supported,
            "usage": five_key_usage,
            "reported_model": "synthetic-v1",
        },
    )
    monkeypatch.setattr(
        qualification,
        "_run_thinking",
        lambda *_: {"status": "not_observed", "variants": [], "reported_model": None},
    )
    monkeypatch.setattr(
        qualification,
        "_run_tool_and_replay",
        lambda *_: {
            "native_tool_call": {"status": "not_observed"},
            "assistant_replay": {"status": "not_observed"},
            "tool_result_replay": {"status": "not_observed"},
            "reported_model": None,
        },
    )

    result = _qualify_model(profile, None, object(), "synthetic-v1")
    assert result.capabilities["usage"] == five_key_usage
    record = QualificationStateStore(tmp_path / "state.json").record_qualification(
        route_id="synthetic:synthetic-v1",
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        provider_id="synthetic-provider",
        endpoint_id="synthetic-endpoint",
        requested_model="synthetic-v1",
        reported_model=result.reported_model,
        passed=True,
        capability_summary=result.capabilities,
        validity_hours=24,
        failure_threshold=5,
    )
    assert set(record["usage_totals"]) == {
        "prompt_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "completion_tokens",
        "total_tokens",
    }
