from __future__ import annotations

import json
import shutil

import pytest
from jsonschema import Draft202012Validator

from pvi.infrastructure.repository import DataValidationError, RepositoryLoader


def test_all_schemas_are_valid_draft_2020_12(project_root):
    for path in sorted((project_root / "contracts" / "schemas").glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_repository_loads_all_frozen_data(repository):
    assert len(repository.endpoints) == 14
    assert len(repository.profiles) == 35
    assert {
        "cap-bailian-qwen3-8-max",
        "cap-bailian-kimi-k3",
        "cap-bailian-glm-5-2",
        "cap-bailian-qwen3-7-plus",
        "cap-bailian-qwen3-vl-plus",
        "cap-bailian-deepseek-v4-pro",
        "cap-bailian-minimax-m3",
        "cap-bailian-xiaomi-mimo-v2-5-pro",
        "cap-bigmodel-coding-glm-5-3",
        "cap-aigocode-gpt-5-6-sol",
        "cap-aigocode-claude-sonnet-5",
        "cap-aigocode-gemini-3-7-flash",
        "cap-aigocode-grok-4-6",
        "cap-mimo-token-plan-mimo-v2-5-pro",
        "cap-local-qwen-qwen3-8-27b-fp8",
        "cap-aliyun-token-plan-qwen3-8-max",
        "cap-aliyun-token-plan-qwen3-7-max",
        "cap-aliyun-token-plan-qwen3-6-flash",
        "cap-aliyun-token-plan-deepseek-v4-pro-0813",
        "cap-aigocode-anthropic-claude-sonnet-5",
        "cap-bailian-deepseek-v4-pro-0813",
    } <= set(repository.profiles)
    assert len(repository.evidence) == 49
    assert {
        "evidence-bailian-qwen3-8-max-official-20260809",
        "evidence-bailian-kimi-k3-official-20260809",
        "evidence-bailian-glm-5-2-official-20260809",
        "evidence-bailian-qwen3-7-plus-official-20260809",
        "evidence-bailian-qwen3-vl-plus-official-20260809",
        "evidence-bailian-deepseek-v4-pro-official-20260809",
        "evidence-bailian-minimax-m3-official-20260809",
        "evidence-bailian-xiaomi-mimo-v2-5-pro-official-20260809",
        "evidence-bigmodel-coding-glm-5-3-docs-20260817",
        "evidence-aigocode-docs-20260817",
        "evidence-aigocode-catalog-20260818",
        "evidence-mimo-token-plan-docs-20260818",
        "evidence-mimo-token-plan-trial-20260818",
        "evidence-local-qwen-contract-20260818",
        "evidence-local-qwen-catalog-20260818",
        "evidence-aliyun-token-plan-docs-20260819",
        "evidence-aliyun-token-plan-endpoint-20260819",
        "evidence-aliyun-token-plan-qualification-20260819",
        "evidence-aigocode-anthropic-20260819",
        "evidence-bailian-deepseek-v4-pro-0813-20260821",
    } <= set(repository.evidence)
    assert len(repository.probe_catalog.probes) == 23
    assert len(repository.probe_runs) == 22
    assert repository.data_version.startswith("sha256:")


def test_probe_catalog_contains_required_ids(repository):
    actual = {item.probe_id for item in repository.probe_catalog.probes}
    required = {
        "text_message_probe",
        "role_support_probe",
        "multimodal_content_probe",
        "structured_output_probe",
        "native_tool_call_probe",
        "text_simulated_tool_probe",
        "multiple_tool_call_probe",
        "streaming_arguments_probe",
        "invalid_json_probe",
        "stream_interruption_probe",
        "tool_result_correlation_probe",
        "reasoning_exposure_probe",
        "continuation_artifact_probe",
        "usage_probe",
        "seed_repeatability_probe",
        "logprobs_probe",
        "full_logits_probe",
        "unknown_event_probe",
        "gateway_fidelity_probe",
        "online_text_message_probe",
        "online_native_tool_call_probe",
        "online_streaming_probe",
        "provider_capability_sweep_probe",
    }
    assert actual == required


def test_online_probes_are_explicitly_marked(repository):
    online = {item.probe_id for item in repository.probe_catalog.probes if not item.offline_only}
    assert online == {
        "online_text_message_probe",
        "online_native_tool_call_probe",
        "online_streaming_probe",
        "provider_capability_sweep_probe",
    }


def test_invalid_profile_fails_repository_startup(project_root, tmp_path):
    data_dir = tmp_path / "data"
    contracts_dir = tmp_path / "contracts"
    shutil.copytree(project_root / "data", data_dir)
    shutil.copytree(project_root / "contracts", contracts_dir)
    profile_path = data_dir / "capability-profiles" / "cap-official-alpha.json"
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    del payload["subject"]["endpoint_id"]
    profile_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataValidationError, match="endpoint_id"):
        RepositoryLoader(data_dir, contracts_dir, validate=True).load()


def test_no_trust_score_field(repository):
    for profile in repository.profiles.values():
        payload = profile.model_dump(mode="json")
        assert "trust_score" not in json.dumps(payload)


def test_model_token_limits_file_matches_schema(project_root):
    """config/model-token-limits.json must satisfy its frozen schema."""

    schema_path = project_root / "contracts" / "schemas" / "model-token-limits.schema.json"
    data_path = project_root / "config" / "model-token-limits.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def test_model_token_limits_cover_all_bailian_aliases(project_root):
    """Every public alias of an onboarded provider must have advertised token limits."""

    from pvian_gateway.accounting import load_token_limits

    limits = load_token_limits(project_root / "config" / "model-token-limits.json")
    aliases = json.loads(
        (project_root / "config" / "public-model-aliases.json").read_text(encoding="utf-8")
    )
    provider_regions = {
        "provider-bailian-cn-beijing": ("bailian", "cn-beijing"),
        "provider-deepseek-official": ("deepseek-official", "global"),
        "provider-moonshot-official": ("moonshot-official", "cn"),
        "provider-openrouter": ("openrouter", "global"),
        "provider-bigmodel-coding": ("bigmodel-coding", "cn"),
        "provider-aigocode": ("aigocode", "global"),
        "provider-aigocode-anthropic": ("aigocode-anthropic", "global"),
        "provider-mimo-token-plan": ("mimo-token-plan", "cn"),
        "provider-aliyun-token-plan": ("aliyun-token-plan", "cn-beijing"),
        "provider-local-qwen": ("local-qwen", "local"),
    }
    for alias in aliases["aliases"]:
        provider_region = next(
            (value for prefix, value in provider_regions.items()
             if alias["profile_id"] == prefix
             or alias["profile_id"].startswith(prefix + "-")),
            None,
        )
        if provider_region is None:
            continue
        key = (provider_region[0], provider_region[1], alias["upstream_model"])
        assert key in limits, f"missing token limits for {alias['alias']}"
