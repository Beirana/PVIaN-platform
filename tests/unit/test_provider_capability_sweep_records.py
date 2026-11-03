from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[2]
SCHEMA = ROOT / "contracts" / "schemas" / "capability-sweep.schema.json"
V1 = ROOT / "data" / "evidence-artifacts" / "provider-capability-sweep-bailian-20260809.json"
V2 = ROOT / "data" / "evidence-artifacts" / "provider-capability-sweep-bailian-20260809-v2.json"
V2_EVIDENCE = "evidence-bailian-provider-capability-sweep-20260809-v2"
KIMI_TARGETED = (
    ROOT
    / "data"
    / "evidence-artifacts"
    / "provider-capability-targeted-kimi-k3-tool-required-20260809.json"
)
DEEPSEEK_TARGETED = (
    ROOT
    / "data"
    / "evidence-artifacts"
    / "provider-capability-targeted-deepseek-v4-pro-tool-replay-20260809.json"
)


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {item for child in value.values() for item in _keys(child)}
    if isinstance(value, list):
        return {item for child in value for item in _keys(child)}
    return set()


def test_v1_v2_artifacts_validate_hash_and_supersedes(repository) -> None:
    schema = _load(SCHEMA)
    v1 = _load(V1)
    v2 = _load(V2)
    Draft202012Validator(schema).validate(v1)
    Draft202012Validator(schema).validate(v2)

    v1_sha = hashlib.sha256(V1.read_bytes()).hexdigest()
    v2_sha = hashlib.sha256(V2.read_bytes()).hexdigest()
    assert v2["supersedes_sha256"] == f"sha256:{v1_sha}"
    v1_evidence = repository.evidence[
        "evidence-bailian-provider-capability-sweep-20260809-v1"
    ]
    assert v1_evidence.sha256 == v1_sha
    assert repository.evidence[V2_EVIDENCE].sha256 == v2_sha
    assert v2["sweep_revision"] == "v2"
    assert v2["completion_calls_attempted"] == 57
    for result in v2["results"]:
        for observation in result["observations"]:
            assert "output_nonempty" in observation["validation"]
            assert "finish_protocol_conformant" in observation["validation"]

    forbidden = {
        "prompt",
        "response_content",
        "private_reasoning",
        "raw_provider_body",
        "raw_body",
        "tool_arguments",
        "remote_error_text",
    }
    assert not forbidden & _keys(v1)
    assert not forbidden & _keys(v2)


def test_v2_profiles_preserve_granular_observed_semantics(repository) -> None:
    tool_supported = {
        "qwen3.8-max",
        "glm-5.2",
        "qwen3.7-plus",
        "qwen3-vl-plus",
        "MiniMax/MiniMax-M3",
        "xiaomi/mimo-v2.5-pro",
        "kimi/kimi-k3",
        "deepseek-v4-pro",
    }
    profiles = [
        item
        for item in repository.profiles.values()
        if item.subject.provider_id == "provider-bailian"
        and item.subject.model_requested in {
            "qwen3.8-max", "kimi/kimi-k3", "glm-5.2", "qwen3.7-plus",
            "qwen3-vl-plus", "deepseek-v4-pro", "MiniMax/MiniMax-M3",
            "xiaomi/mimo-v2.5-pro",
        }
    ]
    assert len(profiles) == 8
    for profile in profiles:
        model = profile.subject.model_requested
        caps = profile.model_api_capabilities
        for name in ("messages", "usage_context", "streaming", "reasoning"):
            assert caps[name]["observed"] == "supported"
            assert caps[name]["effective"] == "unknown"
            assert V2_EVIDENCE in caps[name]["evidence_ids"]
        assert caps["structured_output"]["observed"] == (
            "unsupported" if model == "MiniMax/MiniMax-M3" else "supported"
        )
        assert caps["structured_output"]["effective"] == "unknown"
        assert caps["tool_calling"]["observed"] == (
            "supported" if model in tool_supported else "inconclusive"
        )
        assert caps["tool_calling"]["effective"] == "unknown"
        assert caps["sampling_observability"]["observed"] == "not_tested"
        assert caps["builtin_tools"]["observed"] == "not_tested"

    qwen_vl = next(item for item in profiles if item.subject.model_requested == "qwen3-vl-plus")
    assert qwen_vl.model_api_capabilities["multimodal"]["observed"] == "not_tested"
    mimo = next(item for item in profiles if item.subject.model_requested.startswith("xiaomi/"))
    assert mimo.subject.model_reported == "mimo-v2.5-pro"
    assert any(
        "bailian-cn-beijing-xiaomi-mimo-v2-5-pro@1.0.0" in note
        for note in mimo.assurance.usage_notes
    )


def test_kimi_targeted_tool_required_evidence_supersedes_variant_inconclusive(repository) -> None:
    artifact = _load(KIMI_TARGETED)
    assert artifact["completion_call_budget"] == artifact["completion_calls_attempted"] == 2
    assert artifact["retry_total_attempts"] == 1
    assert artifact["fallback"] is None
    assert artifact["tool_choice_initial"] == "required"
    assert artifact["tool_choice_replay"] == "none"
    assert artifact["validation"] == {
        "native_tool_call_valid": True,
        "passed": True,
        "reasoning_content_preserved_in_memory": True,
        "replay_output_nonempty": True,
    }
    assert [item["finish_reason"] for item in artifact["attempts"]] == ["tool_calls", "stop"]
    assert all(item["reported_model"] == "kimi/kimi-k3" for item in artifact["attempts"])
    assert not {
        "prompt", "response_content", "private_reasoning", "raw_provider_body",
        "tool_arguments", "remote_error_text",
    } & _keys(artifact)

    evidence = repository.evidence[
        "evidence-bailian-kimi-k3-tool-required-targeted-20260809"
    ]
    assert evidence.sha256 == hashlib.sha256(KIMI_TARGETED.read_bytes()).hexdigest()
    profile = repository.profiles["cap-bailian-kimi-k3"]
    assert profile.profile_version == "1.1.1"
    tool = profile.model_api_capabilities["tool_calling"]
    assert tool["observed"] == "supported"
    assert tool["effective"] == "unknown"
    assert tool["representation"] == "native_structured"
    assert tool["stable_call_result_correlation"] == "supported"
    assert tool["native_tool_result_continuation"] == "supported"


def test_deepseek_targeted_replay_resolves_protocol_shape_inconclusive(repository) -> None:
    artifact = _load(DEEPSEEK_TARGETED)
    assert artifact["completion_call_budget"] == artifact["completion_calls_attempted"] == 2
    assert artifact["retry_total_attempts"] == 1
    assert artifact["fallback"] is None
    assert artifact["tool_choice_initial"] == "named_function"
    assert artifact["tool_choice_replay"] == "none"
    assert artifact["validation"] == {
        "native_tool_call_valid": True,
        "passed": True,
        "replay_output_nonempty": True,
    }
    assert [item["finish_reason"] for item in artifact["attempts"]] == ["stop", "stop"]
    assert all(item["reported_model"] == "deepseek-v4-pro" for item in artifact["attempts"])
    assert not {
        "prompt", "response_content", "private_reasoning", "raw_provider_body",
        "tool_arguments", "remote_error_text",
    } & _keys(artifact)

    evidence = repository.evidence[
        "evidence-bailian-deepseek-v4-pro-tool-replay-targeted-20260809"
    ]
    assert evidence.sha256 == hashlib.sha256(DEEPSEEK_TARGETED.read_bytes()).hexdigest()
    profile = repository.profiles["cap-bailian-deepseek-v4-pro"]
    assert profile.profile_version == "1.1.1"
    tool = profile.model_api_capabilities["tool_calling"]
    assert tool["observed"] == "supported"
    assert tool["effective"] == "unknown"
    assert tool["representation"] == "native_structured"
    assert tool["stable_call_result_correlation"] == "supported"
    assert tool["native_tool_result_continuation"] == "supported"


def test_v2_has_eight_provider_sweep_probe_runs(repository) -> None:
    runs = [
        item
        for item in repository.probe_runs.values()
        if item.probe_id == "provider_capability_sweep_probe"
    ]
    assert len(runs) == 8
    assert all(item.evidence_ids == [V2_EVIDENCE] for item in runs)
    verdicts = {item.subject_profile_id: item.verdict for item in runs}
    assert verdicts["cap-bailian-kimi-k3"] == "inconclusive"
    assert verdicts["cap-bailian-deepseek-v4-pro"] == "inconclusive"
    assert verdicts["cap-bailian-minimax-m3"] == "observed_supported"
