from __future__ import annotations

import hashlib
import json
from typing import Any

from pvi.domain.models import (
    CapabilityQuery,
    CapabilityResolution,
    ResolutionCandidate,
    ResolutionItem,
)
from pvi.infrastructure.repository import Repository

UNKNOWN_VALUES = {"unknown", "not_tested", "inconclusive"}
UNSATISFIED_VALUES = {"unsupported", "not_applicable", "failed"}


def _lookup(profile: dict[str, Any], path: str) -> tuple[Any, list[str]]:
    parts = path.split(".")
    roots = (
        [profile]
        if parts[0] in profile
        else [
            profile.get("model_api_capabilities", {}),
            profile.get("endpoint_capabilities", {}),
            profile.get("assurance", {}),
            profile.get("subject", {}),
        ]
    )
    for root in roots:
        current: Any = root
        evidence_ids: list[str] = []
        found = True
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            if isinstance(current.get("evidence_ids"), list):
                evidence_ids = list(current["evidence_ids"])
            current = current[part]
        if found:
            if isinstance(current, dict) and "effective" in current:
                evidence_ids = list(current.get("evidence_ids", evidence_ids))
                current = current["effective"]
            return current, evidence_ids
    return None, []


def _classify(requirement: str, expected: Any, actual: Any, evidence_ids: list[str]):
    common = {
        "requirement": requirement,
        "expected": expected,
        "actual": actual,
        "evidence_ids": evidence_ids,
    }
    if actual is None:
        return "unknown", ResolutionItem(**common, reason="field_missing")
    if isinstance(actual, str) and actual in UNKNOWN_VALUES:
        return "unknown", ResolutionItem(**common, reason=actual)
    if isinstance(actual, str) and actual in UNSATISFIED_VALUES:
        return "unsatisfied", ResolutionItem(**common, reason=actual)
    matches = actual in expected if isinstance(expected, list) else actual == expected
    if matches:
        return "satisfied", ResolutionItem(**common)
    return "unsatisfied", ResolutionItem(**common, reason="explicit_mismatch")


def resolve(query: CapabilityQuery, repository: Repository) -> CapabilityResolution:
    profiles = list(repository.profiles.values())
    if query.profile_id:
        profiles = [item for item in profiles if item.profile_id == query.profile_id]
    if query.model:
        profiles = [
            item
            for item in profiles
            if query.model in {item.subject.model_requested, item.subject.model_reported}
        ]
    if query.endpoint_id:
        profiles = [item for item in profiles if item.subject.endpoint_id == query.endpoint_id]
    if query.profile_version:
        profiles = [item for item in profiles if item.profile_version == query.profile_version]
    profiles.sort(
        key=lambda item: (
            item.subject.endpoint_id,
            item.profile_id,
            item.profile_version,
        )
    )

    candidates: list[ResolutionCandidate] = []
    for profile in profiles:
        profile_data = profile.model_dump(mode="json")
        buckets: dict[str, list[ResolutionItem]] = {
            "satisfied": [],
            "unsatisfied": [],
            "unknown": [],
        }
        for requirement, expected in sorted(query.requirements.items()):
            actual, evidence_ids = _lookup(profile_data, requirement)
            bucket, item = _classify(requirement, expected, actual, evidence_ids)
            buckets[bucket].append(item)
        candidates.append(
            ResolutionCandidate(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                endpoint_id=profile.subject.endpoint_id,
                profile=profile,
                **buckets,
            )
        )

    query_payload = query.model_dump(mode="json")
    query_payload["data_version"] = repository.data_version
    query_bytes = json.dumps(
        query_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    query_id = f"query-{hashlib.sha256(query_bytes).hexdigest()[:24]}"
    resolved_at = max((item.subject.observed_at for item in profiles), default=None)
    return CapabilityResolution(
        schema_version="pvi.capability-resolution/v1",
        query_id=query_id,
        data_version=repository.data_version,
        resolved_at=resolved_at,
        candidates=candidates,
    )
