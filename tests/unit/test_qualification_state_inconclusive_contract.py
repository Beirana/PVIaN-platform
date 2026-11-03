from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pvi.probe_runner import qualification
from pvi.probe_runner.qualification import _qualify_model
from pvian_gateway.state import QualificationStateError, QualificationStateStore

SHA = "sha256:" + "a" * 64


def _record(
    path: Path,
    capabilities: dict[str, object],
    *,
    passed: bool = False,
) -> dict[str, object]:
    return QualificationStateStore(path).record_qualification(
        route_id="synthetic:model",
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        provider_id="synthetic-provider",
        endpoint_id="synthetic-endpoint",
        requested_model="model",
        reported_model="model",
        passed=passed,
        capability_summary=capabilities,
        validity_hours=24,
        failure_threshold=5,
    )


@pytest.mark.parametrize(
    ("name", "summary"),
    [
        (
            "message",
            {
                "status": "inconclusive",
                "output_length": 17,
                "output_sha256": SHA,
                "finish_reason": "stop",
            },
        ),
        (
            "stream",
            {
                "status": "inconclusive",
                "chunk_count": 4,
                "output_length": 19,
                "output_sha256": SHA,
                "finish_reason": "stop",
            },
        ),
    ],
)
def test_state_preserves_completed_inconclusive_probe_audit_fields(
    tmp_path: Path, name: str, summary: dict[str, object]
) -> None:
    record = _record(tmp_path / "state.json", {name: summary})

    assert record["qualification_status"] == "candidate"
    assert record["operational_status"] == "unqualified"
    assert record["revalidation_due"] is True
    assert record["capabilities"][name] == summary


@pytest.mark.parametrize(
    "extra",
    [
        {"raw_response": "forbidden"},
        {"error_category": "RuntimeError"},
        {"output_text": "forbidden"},
    ],
)
def test_state_rejects_extra_fields_on_completed_inconclusive_probe(
    tmp_path: Path, extra: dict[str, str]
) -> None:
    summary: dict[str, object] = {
        "status": "inconclusive",
        "output_length": 17,
        "output_sha256": SHA,
        "finish_reason": "stop",
        **extra,
    }

    with pytest.raises(QualificationStateError, match="records are invalid"):
        _record(tmp_path / "state.json", {"message": summary})


def test_qualifier_inconclusive_result_persists_as_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = {
        "status": "inconclusive",
        "output_length": 17,
        "output_sha256": SHA,
        "finish_reason": "stop",
    }
    stream = {
        "status": "inconclusive",
        "chunk_count": 4,
        "output_length": 19,
        "output_sha256": SHA,
        "finish_reason": "stop",
    }
    monkeypatch.setattr(
        qualification,
        "_run_baseline",
        lambda *_: {
            "message": message,
            "stream": stream,
            "usage": {
                "prompt_tokens": 8,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "completion_tokens": 3,
                "total_tokens": 11,
            },
            "reported_model": "model",
        },
    )
    profile = SimpleNamespace(
        profile_id="synthetic-profile",
        profile_version="1.0.0",
        policy=SimpleNamespace(
            required_probes=("message", "stream", "usage"),
            probe_request_policy=None,
        ),
        request_variants=({"variant_id": "baseline"},),
    )

    result = _qualify_model(profile, None, object(), "model")
    record = _record(
        tmp_path / "state.json",
        result.capabilities,
        passed=result.qualification_status == "enabled",
    )

    assert result.qualification_status == "candidate"
    assert result.capabilities["message"] == message
    assert result.capabilities["stream"] == stream
    assert record["qualification_status"] == "candidate"

