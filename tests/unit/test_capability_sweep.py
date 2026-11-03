from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pvi.probe_runner.capability_sweep import (
    MAX_COMPLETION_CALLS,
    TARGET_MODELS,
    CallBudget,
    GlobalSweepStop,
    ProbeSpec,
    _base_observation,
    _execute_spec,
    _request_parameters,
    build_sweep_plan,
)
from pvian_gateway.profiles import ProbeRequestPolicy, ProviderProfile, load_profiles

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / "contracts" / "schemas" / "capability-sweep.schema.json").read_text(
        encoding="utf-8"
    )
)
PROFILE_IDS = (
    "provider-bailian-cn-beijing-qwen3-8-max",
    "provider-bailian-cn-beijing-kimi-k3",
    "provider-bailian-cn-beijing-glm-5-2",
    "provider-bailian-cn-beijing-qwen3-7-plus",
    "provider-bailian-cn-beijing-qwen3-vl-plus",
    "provider-bailian-cn-beijing",
    "provider-bailian-cn-beijing-minimax-m3",
    "provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro",
)


def _profiles() -> dict[str, ProviderProfile]:
    loaded = load_profiles(ROOT / "config" / "provider-profiles")
    return {
        model: loaded[profile_id]
        for model, profile_id in zip(TARGET_MODELS, PROFILE_IDS, strict=True)
    }


def _response(
    model: str,
    *,
    content: str = "PVI_CAPABILITY_OK",
    finish: str = "stop",
    tool_calls: list[Any] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        reasoning_content=None,
        reasoning=None,
        analysis=None,
    )
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=usage,
        _request_id="provider-secret-id",
    )


class _Completions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(*responses: Any) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(list(responses))))


def test_plan_reserves_exactly_58_completion_calls() -> None:
    plan = build_sweep_plan(_profiles())
    assert sum(item.max_calls for item in plan) == MAX_COMPLETION_CALLS == 58
    assert sum(item.probe == "message_usage" for item in plan) == 8
    assert sum(item.probe == "stream" for item in plan) == 8
    assert sum(item.probe == "thinking" for item in plan) == 18
    assert sum(item.probe == "tool_and_replay" for item in plan) == 8
    assert sum(item.probe == "structured_output" for item in plan) == 8
    for model in TARGET_MODELS:
        probes = [item.probe for item in plan if item.model == model]
        assert probes.index("structured_output") < probes.index("tool_and_replay")


def test_full_58_call_plan_artifact_schema_accepts_deepseek_nine() -> None:
    profiles = _profiles()
    plan = build_sweep_plan(profiles)
    results = []
    for model, profile in profiles.items():
        planned = sum(item.max_calls for item in plan if item.model == model)
        results.append(
            {
                "requested_model": model,
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "route_id": f"bailian-cn-beijing:{model}",
                "catalog_discovered": True,
                "planned_completion_calls": planned,
                "attempted_completion_calls": 0,
                "status": "completed",
                "stopped_after": None,
                "observations": [],
            }
        )
    artifact = _minimal_artifact()
    artifact["completion_calls_attempted"] = 0
    artifact["results"] = results
    Draft202012Validator(SCHEMA).validate(artifact)
    assert sum(item["planned_completion_calls"] for item in results) == 58
    deepseek = next(item for item in results if item["requested_model"] == "deepseek-v4-pro")
    assert deepseek["planned_completion_calls"] == 9


def test_budget_blocks_the_59th_completion_call() -> None:
    budget = CallBudget()
    assert [budget.consume() for _ in range(58)][-1] == 58
    with pytest.raises(GlobalSweepStop, match="budget exceeded"):
        budget.consume()


def test_mimo_identity_uses_profile_explicit_mapping() -> None:
    profile = _profiles()["xiaomi/mimo-v2.5-pro"]
    spec = ProbeSpec(
        "xiaomi/mimo-v2.5-pro", profile.profile_id, "message_usage", None, 1
    )
    observation = _base_observation(
        profile,
        spec,
        _response("mimo-v2.5-pro"),
        (),
    )
    assert observation["status"] == "observed_supported"
    assert observation["reported_model_identity"]["match"] == "explicit_mapping"
    assert observation["reported_model"] == "mimo-v2.5-pro"


def test_kimi_length_is_inconclusive_and_caps_are_not_artificially_small() -> None:
    profile = _profiles()["kimi/kimi-k3"]
    policy = profile.policy.probe_request_policy
    assert policy is not None
    assert _request_parameters(policy, "kimi/kimi-k3", "message")[
        policy.max_output_parameter
    ] >= 1024
    assert _request_parameters(policy, "kimi/kimi-k3", "thinking")[
        policy.max_output_parameter
    ] >= 2048
    client = _client(_response("kimi/kimi-k3", finish="length"))
    observation, calls, terminal = _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("kimi/kimi-k3", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 1
    assert terminal is True
    assert observation["status"] == "inconclusive"
    assert observation["error_category"] == "output_limit_reached"


def test_v2_reasoning_and_tool_caps_are_conservative_for_every_model() -> None:
    for model, profile in _profiles().items():
        policy = profile.policy.probe_request_policy
        if policy is None:
            policy = ProbeRequestPolicy()
        thinking = _request_parameters(policy, model, "thinking")[
            policy.max_output_parameter
        ]
        expected_thinking = 4096 if model in {"glm-5.2", "deepseek-v4-pro"} else 2048
        assert thinking >= expected_thinking
        assert _request_parameters(policy, model, "tool")[policy.max_output_parameter] >= 1024
        assert _request_parameters(policy, model, "replay")[policy.max_output_parameter] >= 1024


def test_invalid_tool_call_never_executes_replay() -> None:
    profile = _profiles()["qwen3.8-max"]
    client = _client(_response("qwen3.8-max", tool_calls=[]))
    observation, calls, terminal = _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "tool_and_replay", None, 2),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 1
    assert terminal is False
    assert len(client.chat.completions.requests) == 1
    assert observation["status"] == "observed_unsupported"
    assert observation["error_category"] == "invalid_tool_call"
    assert observation["validation"]["replay_completed"] is None


def test_tool_probe_honors_kimi_auto_tool_choice_policy() -> None:
    profile = _profiles()["kimi/kimi-k3"]
    client = _client(_response("kimi/kimi-k3", tool_calls=[]))
    _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("kimi/kimi-k3", profile.profile_id, "tool_and_replay", None, 2),
        budget=CallBudget(),
        quotes=(),
    )
    assert client.chat.completions.requests[0]["tool_choice"] == "auto"


def test_schema_rejects_sensitive_content_fields() -> None:
    artifact = _minimal_artifact()
    Draft202012Validator(SCHEMA).validate(artifact)
    unsafe = copy.deepcopy(artifact)
    unsafe["results"][0]["observations"][0]["raw_content"] = "secret provider body"
    errors = list(Draft202012Validator(SCHEMA).iter_errors(unsafe))
    assert errors
    assert any("Additional properties" in item.message for item in errors)


def test_qwen_vl_sweep_requests_are_text_only() -> None:
    profile = _profiles()["qwen3-vl-plus"]
    client = _client(_response("qwen3-vl-plus"))
    observation, _, _ = _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("qwen3-vl-plus", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    content = client.chat.completions.requests[0]["messages"][0]["content"]
    assert isinstance(content, str)
    assert "image" not in content.lower()
    assert observation["validation"]["text_only"] is True


@pytest.mark.parametrize("missing", ["usage", "request_id", "finish"])
def test_message_evidence_gap_is_inconclusive_and_terminal(missing: str) -> None:
    profile = _profiles()["qwen3.8-max"]
    response = _response("qwen3.8-max")
    if missing == "usage":
        response.usage = None
    elif missing == "request_id":
        response._request_id = None
    else:
        response.choices[0].finish_reason = None
    observation, _, terminal = _execute_spec(
        client=_client(response),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["status"] == "inconclusive"
    assert terminal is True


def test_message_sentinel_mismatch_is_diagnostic_when_output_is_nonempty() -> None:
    profile = _profiles()["qwen3.8-max"]
    observation, _, terminal = _execute_spec(
        client=_client(_response("qwen3.8-max", content="A valid non-sentinel answer")),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["status"] == "observed_supported"
    assert observation["validation"]["output_nonempty"] is True
    assert observation["validation"]["output_match"] is False
    assert terminal is False


@pytest.mark.parametrize("probe", ["thinking", "structured_output"])
def test_nonmessage_probe_requires_usage_and_request_id(probe: str) -> None:
    profile = _profiles()["qwen3.8-max"]
    response = _response(
        "qwen3.8-max",
        content='{"ok":true}' if probe == "structured_output" else "PVI_THINKING_OK",
    )
    response._request_id = None
    spec = ProbeSpec(
        "qwen3.8-max",
        profile.profile_id,
        probe,
        "enable_thinking_false" if probe == "thinking" else None,
        1,
    )
    observation, _, terminal = _execute_spec(
        client=_client(response),
        profile=profile,
        spec=spec,
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["error_category"] == "provider_request_id_missing"
    assert terminal is True


def test_strict_finish_reason_is_enforced() -> None:
    profile = _profiles()["qwen3.8-max"]
    observation, _, terminal = _execute_spec(
        client=_client(_response("qwen3.8-max", finish="content_filter")),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["error_category"] == "unexpected_finish_reason"
    assert terminal is True


def test_identity_rejection_is_immediate_terminal_evidence_failure() -> None:
    profile = _profiles()["qwen3.8-max"]
    observation, _, terminal = _execute_spec(
        client=_client(_response("other-vendor-model")),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "message_usage", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["reported_model_identity"]["match"] == "rejected"
    assert observation["error_category"] == "reported_model_rejected"
    assert terminal is True


def test_capability_4xx_is_explicitly_unsupported_but_not_evidence_failfast() -> None:
    class CapabilityParameterError(Exception):
        status_code = 400

    profile = _profiles()["qwen3.8-max"]
    observation, calls, terminal = _execute_spec(
        client=_client(CapabilityParameterError("raw provider text must not persist")),
        profile=profile,
        spec=ProbeSpec(
            "qwen3.8-max", profile.profile_id, "structured_output", None, 1
        ),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 1
    assert observation["status"] == "observed_unsupported"
    assert observation["error_category"] == "capability_parameter_rejected"
    assert terminal is False
    assert "raw provider text" not in json.dumps(observation)


def test_tool_and_replay_require_strict_finishes_and_replay_evidence() -> None:
    profile = _profiles()["qwen3.8-max"]
    tool_call = SimpleNamespace(
        id="private-tool-call-id",
        type="function",
        function=SimpleNamespace(
            name="pvian_echo_probe",
            arguments='{"nonce":"pvi-capability-sweep-nonce"}',
        ),
    )
    first = _response("qwen3.8-max", finish="tool_calls", tool_calls=[tool_call])
    replay = _response("qwen3.8-max", finish="stop")
    replay._request_id = None
    observation, calls, terminal = _execute_spec(
        client=_client(first, replay),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "tool_and_replay", None, 2),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 2
    assert observation["error_category"] == "provider_request_id_missing"
    assert observation["replay_provider_request_id_sha256"] is None
    assert terminal is True


def test_valid_native_tool_finish_stop_replays_but_records_nonconformance() -> None:
    profile = _profiles()["qwen3.8-max"]
    tool_call = SimpleNamespace(
        id="private-tool-call-id",
        type="function",
        function=SimpleNamespace(
            name="pvian_echo_probe",
            arguments='{"nonce":"pvi-capability-sweep-nonce"}',
        ),
    )
    client = _client(
        _response("qwen3.8-max", finish="stop", tool_calls=[tool_call]),
        _response("qwen3.8-max", finish="stop"),
    )
    observation, calls, terminal = _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "tool_and_replay", None, 2),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 2
    assert len(client.chat.completions.requests) == 2
    assert observation["status"] == "observed_supported"
    assert observation["validation"]["finish_protocol_conformant"] is False
    assert observation["validation"]["replay_completed"] is True
    assert terminal is False


def test_tool_replay_preserves_reasoning_in_memory_but_not_evidence() -> None:
    profile = _profiles()["kimi/kimi-k3"]
    tool_call = SimpleNamespace(
        id="private-tool-call-id",
        type="function",
        function=SimpleNamespace(
            name="pvian_echo_probe",
            arguments='{"nonce":"pvi-capability-sweep-nonce"}',
        ),
    )
    first = _response("kimi/kimi-k3", finish="tool_calls", tool_calls=[tool_call])
    first.choices[0].message.reasoning_content = "private reasoning must not persist"
    client = _client(first, _response("kimi/kimi-k3", finish="stop"))
    observation, calls, terminal = _execute_spec(
        client=client,
        profile=profile,
        spec=ProbeSpec("kimi/kimi-k3", profile.profile_id, "tool_and_replay", None, 2),
        budget=CallBudget(),
        quotes=(),
    )
    replay_request = client.chat.completions.requests[1]
    assert replay_request["tool_choice"] == "none"
    assert replay_request["messages"][1]["reasoning_content"] == (
        "private reasoning must not persist"
    )
    assert "private reasoning" not in json.dumps(observation)
    assert calls == 2
    assert terminal is False


def test_stream_requires_hashed_provider_request_id_and_stop_finish() -> None:
    profile = _profiles()["qwen3.8-max"]
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    chunks = [
        SimpleNamespace(
            model="qwen3.8-max",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="PVI_STREAM_OK"),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            model="qwen3.8-max",
            choices=[
                SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")
            ],
            usage=usage,
        ),
    ]

    class Stream:
        _request_id = None

        def __iter__(self):
            return iter(chunks)

    observation, calls, terminal = _execute_spec(
        client=_client(Stream()),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "stream", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert calls == 1
    assert observation["error_category"] == "provider_request_id_missing"
    assert terminal is True


def test_stream_sentinel_mismatch_is_diagnostic_when_output_is_nonempty() -> None:
    profile = _profiles()["qwen3.8-max"]
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    chunks = [
        SimpleNamespace(
            model="qwen3.8-max",
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content="Non-sentinel stream output"),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            model="qwen3.8-max",
            choices=[
                SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")
            ],
            usage=usage,
        ),
    ]

    class Stream:
        _request_id = "private-stream-request-id"

        def __iter__(self):
            return iter(chunks)

    observation, _, terminal = _execute_spec(
        client=_client(Stream()),
        profile=profile,
        spec=ProbeSpec("qwen3.8-max", profile.profile_id, "stream", None, 1),
        budget=CallBudget(),
        quotes=(),
    )
    assert observation["status"] == "observed_supported"
    assert observation["validation"]["output_nonempty"] is True
    assert observation["validation"]["output_match"] is False
    assert terminal is False


def test_cli_persists_pending_then_promotes_valid_artifact(tmp_path: Path) -> None:
    module = _load_sweep_script()
    output = tmp_path / "sweep.json"
    status, persisted = module._persist_artifact(_minimal_artifact(), output)
    assert status == "artifact_written"
    assert persisted == output
    assert output.is_file()
    assert not (tmp_path / "sweep.json.pending.json").exists()
    assert not (tmp_path / "sweep.json.rejected.json").exists()


def test_cli_preserves_schema_failure_as_rejected_without_overwrite(tmp_path: Path) -> None:
    module = _load_sweep_script()
    output = tmp_path / "sweep.json"
    artifact = _minimal_artifact()
    artifact["results"][0]["planned_completion_calls"] = 10
    status, persisted = module._persist_artifact(artifact, output)
    assert status == "artifact_rejected"
    assert persisted == tmp_path / "sweep.json.rejected.json"
    assert persisted.is_file()
    assert not output.exists()
    assert not (tmp_path / "sweep.json.pending.json").exists()
    with pytest.raises(FileExistsError, match="overwrite"):
        module._persist_artifact(_minimal_artifact(), output)


def test_cli_supersedes_hash_binds_old_artifact_without_modifying_it(tmp_path: Path) -> None:
    module = _load_sweep_script()
    old = tmp_path / "old-v1.json"
    old_bytes = b'{"safe":"old-evidence"}\n'
    old.write_bytes(old_bytes)
    digest = module._sha256_file(old)
    artifact = _minimal_artifact()
    artifact["sweep_revision"] = "v2"
    artifact["supersedes_sha256"] = digest
    Draft202012Validator(SCHEMA).validate(artifact)
    assert digest.startswith("sha256:")
    assert old.read_bytes() == old_bytes


def _minimal_artifact() -> dict[str, Any]:
    profile = _profiles()["qwen3.8-max"]
    observation = _base_observation(
        profile,
        ProbeSpec("qwen3.8-max", profile.profile_id, "message_usage", None, 1),
        _response("qwen3.8-max"),
        (),
    )
    observation["max_output_tokens"] = 64
    return {
        "schema_version": "pvian.capability-sweep/v1",
        "evidence_class": "provider_capability_sweep",
        "completed_at": "2026-08-09T12:00:00Z",
        "catalog_gets": 1,
        "catalog_request_id_sha256": None,
        "completion_call_budget": 58,
        "completion_calls_attempted": 1,
        "retry_total_attempts": 1,
        "fallback": None,
        "content_policy": {
            "persist_raw_content": False,
            "persist_private_reasoning": False,
            "persist_raw_provider_body": False,
            "persist_tool_arguments": False,
        },
        "global_stop": None,
        "results": [
            {
                "requested_model": "qwen3.8-max",
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "route_id": "bailian-cn-beijing:qwen3.8-max",
                "catalog_discovered": True,
                "planned_completion_calls": 6,
                "attempted_completion_calls": 1,
                "status": "completed",
                "stopped_after": None,
                "observations": [observation],
            }
        ],
    }


def _load_sweep_script() -> Any:
    path = ROOT / "scripts" / "sweep_bailian_capabilities.py"
    spec = importlib.util.spec_from_file_location("test_sweep_bailian_capabilities", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
