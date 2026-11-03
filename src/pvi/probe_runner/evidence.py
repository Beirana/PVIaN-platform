from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pvi.domain.models import EvidenceReference, ProbeRun
from pvi.probe_runner.config import ProbeSettings
from pvi.probe_runner.runner import ProbeObservation


def write_probe_bundle(
    *,
    output_root: Path,
    settings: ProbeSettings,
    observations: list[ProbeObservation],
) -> Path:
    session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
    session_dir = output_root / f"probe-session-{session_id}"
    session_dir.mkdir(parents=True, exist_ok=False)

    artifact = {
        "schema_version": "pvi.online-probe-artifact/v1",
        "artifact_id": f"online-probe-artifact-{session_id}",
        "captured_at": _now(),
        "endpoint": settings.safe_summary(),
        "redaction_profile": "pvi-online-probe-metadata-only/v1",
        "contains_raw_prompt": False,
        "contains_raw_response": False,
        "contains_credentials": False,
        "observations": [item.to_dict() for item in observations],
    }
    artifact_path = session_dir / "artifact.json"
    _write_json(artifact_path, artifact)
    artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    candidate_files: list[str] = []
    for index, observation in enumerate(observations):
        suffix = uuid4().hex[:12]
        evidence_id = f"evidence-online-{observation.probe_id}-{suffix}"
        evidence = EvidenceReference.model_validate(
            {
                "schema_version": "pvi.evidence-reference/v1",
                "evidence_id": evidence_id,
                "source_kind": "broker_gateway",
                "host_profile_id": None,
                "capture_surface": "host_normalized_event",
                "sha256": artifact_digest,
                "media_type": "application/json",
                "redaction_profile": "pvi-online-probe-metadata-only/v1",
                "locator": f"artifact.json#/observations/{index}",
                "captured_at": observation.completed_at,
                "retention_class": "metadata_only",
            }
        )
        run = ProbeRun.model_validate(
            {
                "schema_version": "pvi.probe-run/v1",
                "probe_run_id": f"probe-run-online-{observation.probe_id}-{suffix}",
                "probe_id": observation.probe_id,
                "probe_version": observation.probe_version,
                "subject_profile_id": settings.subject_profile_id,
                "input_digest": observation.input_digest,
                "configuration_digest": observation.configuration_digest,
                "environment_digest": observation.environment_digest,
                "started_at": observation.started_at,
                "completed_at": observation.completed_at,
                "verdict": observation.verdict,
                "evidence_ids": [evidence_id],
                "normalization_error_ids": [],
            }
        )
        evidence_name = f"candidate-evidence-{index + 1}.json"
        run_name = f"candidate-probe-run-{index + 1}.json"
        _write_json(
            session_dir / evidence_name,
            evidence.model_dump(mode="json"),
        )
        _write_json(
            session_dir / run_name,
            run.model_dump(mode="json"),
        )
        candidate_files.extend([evidence_name, run_name])

    manifest = {
        "schema_version": "pvi.online-probe-manifest/v1",
        "session_id": session_id,
        "artifact": artifact_path.name,
        "artifact_sha256": artifact_digest,
        "candidate_files": candidate_files,
        "promotion_status": "manual_review_required",
        "instructions": (
            "Review the redacted artifact before copying candidate evidence and "
            "probe-run records into data/. Do not promote capabilities automatically."
        ),
    }
    _write_json(session_dir / "manifest.json", manifest)
    return session_dir


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
