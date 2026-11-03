from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pvi.config import PROJECT_ROOT

REPORT_PATH = PROJECT_ROOT / "reports" / "yunwu-4x3-capability-matrix.json"
EXPECTED_VENDORS = ("openai", "deepseek", "grok", "glm")
EXPECTED_CAPABILITIES = {"message", "thinking", "tool_calling"}
ALLOWED_STATUSES = {
    "observed_supported",
    "not_observed",
    "inconclusive",
}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(PROJECT_ROOT)} must contain an object")
    return value


def _resolve_evidence(relative_path: str) -> Path:
    candidate = (PROJECT_ROOT / relative_path).resolve()
    if not candidate.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("matrix evidence path escapes the repository")
    if not candidate.is_file():
        raise ValueError(f"matrix evidence is missing: {relative_path}")
    return candidate


def main() -> None:
    matrix = _load_json(REPORT_PATH)
    if matrix.get("schema_version") != "pvian.vendor-capability-matrix/v1":
        raise SystemExit("unsupported matrix report schema")

    targets = matrix.get("targets")
    if not isinstance(targets, list):
        raise SystemExit("matrix targets must be an array")
    vendors = tuple(item.get("vendor") for item in targets)
    if vendors != EXPECTED_VENDORS:
        raise SystemExit(f"matrix vendors must be {EXPECTED_VENDORS!r}")

    observations: list[dict[str, Any]] = []
    for target in targets:
        results = target.get("results")
        if not isinstance(results, dict) or set(results) != EXPECTED_CAPABILITIES:
            raise SystemExit(f"{target.get('vendor')}: expected exactly three capability results")
        for capability, result in results.items():
            if result.get("vendor") != target.get("vendor"):
                raise SystemExit(f"{target.get('vendor')}/{capability}: vendor mismatch")
            if result.get("requested_model") != target.get("requested_model"):
                raise SystemExit(f"{target.get('vendor')}/{capability}: model mismatch")
            if result.get("status") not in ALLOWED_STATUSES:
                raise SystemExit(f"{target.get('vendor')}/{capability}: invalid status")
            if capability == "tool_calling":
                if result.get("details", {}).get("tool_executed") is not False:
                    raise SystemExit(
                        f"{target.get('vendor')}/{capability}: tool execution is forbidden"
                    )
            observations.append(result)

    evidence_metadata = matrix.get("evidence")
    if not isinstance(evidence_metadata, dict):
        raise SystemExit("matrix evidence metadata is missing")
    evidence_path = _resolve_evidence(str(evidence_metadata.get("path", "")))
    actual_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    if actual_digest != evidence_metadata.get("sha256"):
        raise SystemExit("matrix evidence SHA-256 mismatch")

    evidence = _load_json(evidence_path)
    if evidence.get("contains_credentials") is not False:
        raise SystemExit("matrix evidence credential flag must be false")
    if evidence.get("contains_raw_prompts") is not False:
        raise SystemExit("matrix evidence raw-prompt flag must be false")
    if evidence.get("contains_raw_responses") is not False:
        raise SystemExit("matrix evidence raw-response flag must be false")
    if evidence.get("tool_execution_count") != 0:
        raise SystemExit("matrix evidence reports tool execution")
    if evidence.get("observations") != observations:
        raise SystemExit("matrix report and evidence observations differ")

    print(
        "validated Yunwu 4x3 matrix: "
        f"{len(targets)} vendors, {len(observations)} observations, "
        f"sha256={actual_digest}"
    )


if __name__ == "__main__":
    main()
