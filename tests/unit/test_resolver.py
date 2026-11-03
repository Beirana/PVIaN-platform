from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from pvi.application.resolver import resolve
from pvi.domain.models import CapabilityQuery


def test_a1_official_native_tool_is_observed_with_evidence(repository):
    profile = repository.profiles["cap-official-alpha"]
    tool = profile.model_api_capabilities["tool_calling"]
    assert tool["advertised"] == "supported"
    assert tool["observed"] == "supported"
    assert tool["effective"] == "supported"
    assert tool["representation"] == "native_structured"
    assert tool["evidence_ids"]


def test_a2_openai_compatible_broker_is_not_native(repository):
    profile = repository.profiles["cap-broker-alpha"]
    assert profile.assurance.protocol_fidelity.value == "lossy_translation"
    tool = profile.model_api_capabilities["tool_calling"]
    assert tool["effective"] == "unknown"
    assert tool["representation"] == "unknown"
    assert tool["streaming_arguments"] == "unknown"


def test_a3_same_model_different_endpoints_are_not_merged(repository):
    result = resolve(CapabilityQuery(model="fixture-model-alpha"), repository)
    assert [item.profile_id for item in result.candidates] == [
        "cap-broker-alpha",
        "cap-official-alpha",
    ]
    assert {item.endpoint_id for item in result.candidates} == {
        "endpoint-broker-alpha",
        "endpoint-official-alpha",
    }


def test_a4_advertised_is_not_promoted_to_observed_or_effective(repository):
    tool = repository.profiles["cap-official-beta-advertised-only"].model_api_capabilities[
        "tool_calling"
    ]
    assert tool["advertised"] == "supported"
    assert tool["observed"] == "not_tested"
    assert tool["effective"] == "not_tested"


def test_a5_simple_query_returns_complete_profile(repository):
    result = resolve(
        CapabilityQuery(
            model="fixture-model-alpha",
            endpoint_id="endpoint-official-alpha",
        ),
        repository,
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].profile.profile_id == "cap-official-alpha"
    assert not result.candidates[0].satisfied
    assert not result.candidates[0].unsatisfied
    assert not result.candidates[0].unknown


def test_a6_requirements_are_split_and_evidence_is_preserved(repository, project_root):
    query_path = (
        project_root / "fixtures" / "capability" / "a6-requirements-resolution" / "query.json"
    )
    query = CapabilityQuery.model_validate_json(query_path.read_text(encoding="utf-8"))
    result = resolve(query, repository)
    candidate = result.candidates[0]
    assert [item.requirement for item in candidate.satisfied] == ["tool_calling.representation"]
    assert [item.requirement for item in candidate.unsatisfied] == [
        "tool_calling.streaming_arguments"
    ]
    assert [item.requirement for item in candidate.unknown] == ["reasoning.effective"]
    assert set(candidate.satisfied[0].evidence_ids) == {
        "evidence-official-tool-doc",
        "evidence-official-tool-probe",
    }


def test_resolution_is_deterministic_and_schema_valid(repository, project_root):
    query = CapabilityQuery(
        model="fixture-model-alpha",
        endpoint_id="endpoint-official-alpha",
        requirements={"tool_calling.representation": "native_structured"},
    )
    first = resolve(query, repository)
    second = resolve(query, repository)
    assert first == second
    schema = json.loads(
        (project_root / "contracts" / "schemas" / "capability-resolution.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(first.model_dump(mode="json"))


def test_unknown_not_tested_and_inconclusive_never_satisfy(repository):
    for profile_id, path in (
        ("cap-broker-alpha", "tool_calling.effective"),
        ("cap-official-beta-advertised-only", "tool_calling.effective"),
        ("cap-broker-alpha", "streaming.effective"),
    ):
        result = resolve(
            CapabilityQuery(
                profile_id=profile_id,
                requirements={path: "supported"},
            ),
            repository,
        )
        assert not result.candidates[0].satisfied
        assert result.candidates[0].unknown
