from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from pvian_gateway.accounting import PriceQuote, UsageSummary, estimate, select_quote
from pvian_gateway.profiles import ProbeRequestPolicy, ProviderProfile

SCHEMA_VERSION = "pvian.capability-sweep/v1"
MAX_COMPLETION_CALLS = 58
TARGET_MODELS = (
    "qwen3.8-max",
    "kimi/kimi-k3",
    "glm-5.2",
    "qwen3.7-plus",
    "qwen3-vl-plus",
    "deepseek-v4-pro",
    "MiniMax/MiniMax-M3",
    "xiaomi/mimo-v2.5-pro",
)

_MESSAGE_PROMPT = "Reply exactly: PVI_CAPABILITY_OK"
_STREAM_PROMPT = "Reply exactly: PVI_STREAM_OK"
_THINKING_PROMPT = "Reason briefly, then reply exactly: PVI_THINKING_OK"
_TOOL_NONCE = "pvi-capability-sweep-nonce"
_STRUCTURED_PROMPT = 'Return JSON with the single property "ok" set to true.'


class SweepError(RuntimeError):
    pass


class GlobalSweepStop(SweepError):
    pass


@dataclass(frozen=True)
class ProbeSpec:
    model: str
    profile_id: str
    probe: str
    variant_id: str | None
    max_calls: int


@dataclass
class CallBudget:
    maximum: int = MAX_COMPLETION_CALLS
    attempted: int = 0

    def consume(self) -> int:
        if self.attempted >= self.maximum:
            raise GlobalSweepStop("completion call budget exceeded")
        self.attempted += 1
        return self.attempted


def build_sweep_plan(profiles_by_model: dict[str, ProviderProfile]) -> tuple[ProbeSpec, ...]:
    """Freeze the 58-call upper bound before any network access."""

    if tuple(profiles_by_model) != TARGET_MODELS:
        raise SweepError("profiles must be ordered for the exact eight-model target set")
    plan: list[ProbeSpec] = []
    for model, profile in profiles_by_model.items():
        if not profile.declares_upstream_model(model):
            raise SweepError(f"profile does not declare exact target: {model}")
        plan.extend(
            (
                ProbeSpec(model, profile.profile_id, "message_usage", None, 1),
                ProbeSpec(model, profile.profile_id, "stream", None, 1),
            )
        )
        plan.extend(
            ProbeSpec(
                model,
                profile.profile_id,
                "thinking",
                _required_variant_id(variant),
                1,
            )
            for variant in profile.request_variants
        )
        plan.extend(
            (
                ProbeSpec(model, profile.profile_id, "structured_output", None, 1),
                ProbeSpec(model, profile.profile_id, "tool_and_replay", None, 2),
            )
        )
    if sum(item.max_calls for item in plan) != MAX_COMPLETION_CALLS:
        raise SweepError("capability sweep plan must reserve exactly 58 completion calls")
    return tuple(plan)


def validate_sweep_artifact(value: object, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        raise SweepError(f"invalid capability sweep artifact: {errors[0].message}")


def run_capability_sweep(
    *,
    client: Any,
    profiles_by_model: dict[str, ProviderProfile],
    quotes: tuple[PriceQuote, ...] = (),
    supersedes_sha256: str | None = None,
    sweep_revision: str = "v2",
) -> dict[str, Any]:
    """Run one catalog GET and a bounded, no-retry provider capability sweep.

    The supplied client must itself be configured with ``max_retries=0``. No
    response content, tool arguments, provider IDs, raw bodies, or private
    reasoning are returned by this function.
    """

    plan = build_sweep_plan(profiles_by_model)
    budget = CallBudget()
    try:
        catalog_response = _catalog_once(client)
    except GlobalSweepStop as exc:
        return _artifact(
            catalog_hash=None,
            budget=budget,
            global_stop=str(exc),
            results=[],
            supersedes_sha256=supersedes_sha256,
            sweep_revision=sweep_revision,
        )
    catalog_ids = {_get(item, "id") for item in _get(catalog_response, "data", [])}
    catalog_ids = {item for item in catalog_ids if isinstance(item, str)}
    catalog_hash = _provider_id_hash(catalog_response)
    results: list[dict[str, Any]] = []
    global_stop: str | None = None

    for model, profile in profiles_by_model.items():
        model_specs = [item for item in plan if item.model == model]
        model_result: dict[str, Any] = {
            "requested_model": model,
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "route_id": f"bailian-cn-beijing:{model}",
            "catalog_discovered": model in catalog_ids,
            "planned_completion_calls": sum(item.max_calls for item in model_specs),
            "attempted_completion_calls": 0,
            "status": "not_discovered" if model not in catalog_ids else "completed",
            "stopped_after": None,
            "observations": [],
        }
        results.append(model_result)
        if model not in catalog_ids:
            continue
        try:
            for spec in model_specs:
                calls_before_spec = budget.attempted
                observation, calls, terminal = _execute_spec(
                    client=client,
                    profile=profile,
                    spec=spec,
                    budget=budget,
                    quotes=quotes,
                )
                model_result["observations"].append(observation)
                model_result["attempted_completion_calls"] += calls
                if terminal:
                    model_result["status"] = "stopped_inconclusive"
                    model_result["stopped_after"] = spec.probe
                    break
        except GlobalSweepStop as exc:
            model_result["attempted_completion_calls"] += budget.attempted - calls_before_spec
            model_result["status"] = "global_stop"
            model_result["stopped_after"] = spec.probe
            global_stop = str(exc)
            break

    return _artifact(
        catalog_hash=catalog_hash,
        budget=budget,
        global_stop=global_stop,
        results=results,
        supersedes_sha256=supersedes_sha256,
        sweep_revision=sweep_revision,
    )


def _artifact(
    *,
    catalog_hash: str | None,
    budget: CallBudget,
    global_stop: str | None,
    results: list[dict[str, Any]],
    supersedes_sha256: str | None,
    sweep_revision: str,
) -> dict[str, Any]:
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": "provider_capability_sweep",
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "catalog_gets": 1,
        "catalog_request_id_sha256": catalog_hash,
        "completion_call_budget": MAX_COMPLETION_CALLS,
        "completion_calls_attempted": budget.attempted,
        "retry_total_attempts": 1,
        "fallback": None,
        "content_policy": {
            "persist_raw_content": False,
            "persist_private_reasoning": False,
            "persist_raw_provider_body": False,
            "persist_tool_arguments": False,
        },
        "global_stop": global_stop,
        "results": results,
    }
    artifact["sweep_revision"] = sweep_revision
    if supersedes_sha256 is not None:
        artifact["supersedes_sha256"] = supersedes_sha256
    return artifact


def _catalog_once(client: Any) -> Any:
    try:
        return client.models.list()
    except Exception as exc:
        raise GlobalSweepStop(_exception_category(exc)) from None


def _execute_spec(
    *,
    client: Any,
    profile: ProviderProfile,
    spec: ProbeSpec,
    budget: CallBudget,
    quotes: tuple[PriceQuote, ...],
) -> tuple[dict[str, Any], int, bool]:
    policy = profile.policy.probe_request_policy or ProbeRequestPolicy()
    calls_before = budget.attempted
    try:
        if spec.probe == "message_usage":
            response = _create(
                client,
                budget,
                model=spec.model,
                messages=[{"role": "user", "content": _MESSAGE_PROMPT}],
                **_request_parameters(policy, spec.model, "message"),
                **_variant_parameters(profile.request_variants[0]),
            )
            observation = _ordinary_observation(
                profile, spec, response, quotes, _MESSAGE_PROMPT
            )
            observation["max_output_tokens"] = _output_cap(policy, spec.model, "message")
            return observation, 1, _evidence_chain_failed(observation)
        if spec.probe == "stream":
            return _stream_observation(client, profile, spec, policy, budget, quotes)
        if spec.probe == "thinking":
            variant = next(
                item
                for item in profile.request_variants
                if item.get("variant_id") == spec.variant_id
            )
            response = _create(
                client,
                budget,
                model=spec.model,
                messages=[{"role": "user", "content": _THINKING_PROMPT}],
                **_request_parameters(policy, spec.model, "thinking"),
                **_variant_parameters(variant),
            )
            observation = _thinking_observation(profile, spec, response, quotes)
            observation["max_output_tokens"] = _output_cap(policy, spec.model, "thinking")
            return observation, 1, _evidence_chain_failed(observation)
        if spec.probe == "tool_and_replay":
            return _tool_observation(client, profile, spec, policy, budget, quotes)
        if spec.probe == "structured_output":
            response = _create(
                client,
                budget,
                model=spec.model,
                messages=[{"role": "user", "content": _STRUCTURED_PROMPT}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "pvian_capability_probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"const": True}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
                **_request_parameters(policy, spec.model, "message"),
                **_variant_parameters(profile.request_variants[0]),
            )
            observation = _structured_observation(profile, spec, response, quotes)
            observation["max_output_tokens"] = _output_cap(policy, spec.model, "message")
            return observation, 1, _evidence_chain_failed(observation)
        raise SweepError(f"unsupported probe: {spec.probe}")
    except GlobalSweepStop:
        raise
    except Exception as exc:
        category = _exception_category(exc)
        if category in {"authentication", "rate_limited"}:
            raise GlobalSweepStop(category) from None
        if category == "capability_parameter_rejected":
            return (
                _error_observation(
                    profile,
                    spec,
                    category,
                    status="observed_unsupported",
                ),
                budget.attempted - calls_before,
                False,
            )
        return (
            _error_observation(profile, spec, category),
            budget.attempted - calls_before,
            True,
        )


def _create(client: Any, budget: CallBudget, **request: Any) -> Any:
    budget.consume()
    return client.chat.completions.create(**request)


def _ordinary_observation(
    profile: ProviderProfile,
    spec: ProbeSpec,
    response: Any,
    quotes: tuple[PriceQuote, ...],
    expected: str,
) -> dict[str, Any]:
    content = _message_content(response)
    observation = _base_observation(profile, spec, response, quotes)
    observation["validation"]["output_match"] = content.strip() == expected.split(": ")[-1]
    observation["validation"]["output_nonempty"] = bool(content.strip())
    if observation["error_category"] == "reported_model_rejected":
        return observation
    if observation["usage"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
    elif observation["provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
    elif observation["finish_reason"] != "stop":
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(observation["finish_reason"])
    elif not observation["validation"]["output_nonempty"]:
        observation["status"] = "inconclusive"
        observation["error_category"] = "empty_output"
    return observation


def _stream_observation(
    client: Any,
    profile: ProviderProfile,
    spec: ProbeSpec,
    policy: ProbeRequestPolicy,
    budget: CallBudget,
    quotes: tuple[PriceQuote, ...],
) -> tuple[dict[str, Any], int, bool]:
    stream_response = _create(
        client,
        budget,
        model=spec.model,
        messages=[{"role": "user", "content": _STREAM_PROMPT}],
        stream=True,
        stream_options={"include_usage": True},
        **_request_parameters(policy, spec.model, "stream"),
        **_variant_parameters(profile.request_variants[0]),
    )
    chunks = list(stream_response)
    content = "".join(
        str(_get(_get(_first_choice(chunk), "delta"), "content") or "") for chunk in chunks
    )
    terminal = next((chunk for chunk in reversed(chunks) if _get(chunk, "usage") is not None), None)
    terminal = terminal or (chunks[-1] if chunks else {})
    finish = next(
        (
            _get(_first_choice(chunk), "finish_reason")
            for chunk in reversed(chunks)
            if _get(_first_choice(chunk), "finish_reason") is not None
        ),
        None,
    )
    response_view = {
        "model": next((_get(chunk, "model") for chunk in chunks if _get(chunk, "model")), None),
        "usage": _get(terminal, "usage"),
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
    }
    observation = _base_observation(profile, spec, response_view, quotes)
    observation["provider_request_id_sha256"] = _provider_id_hash(stream_response) or next(
        (_provider_id_hash(chunk) for chunk in chunks if _provider_id_hash(chunk)), None
    )
    observation["validation"]["stream_chunks"] = len(chunks)
    observation["validation"]["output_match"] = content.strip() == "PVI_STREAM_OK"
    observation["validation"]["output_nonempty"] = bool(content.strip())
    observation["max_output_tokens"] = _output_cap(policy, spec.model, "stream")
    if observation["error_category"] == "reported_model_rejected":
        return observation, 1, True
    if observation["usage"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
    elif observation["provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
    elif finish != "stop":
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(finish)
    elif not chunks or not observation["validation"]["output_nonempty"]:
        observation["status"] = "inconclusive"
        observation["error_category"] = "invalid_stream"
    return observation, 1, _evidence_chain_failed(observation)


def _thinking_observation(
    profile: ProviderProfile,
    spec: ProbeSpec,
    response: Any,
    quotes: tuple[PriceQuote, ...],
) -> dict[str, Any]:
    observation = _base_observation(profile, spec, response, quotes)
    message = _get(_first_choice(response), "message", {})
    reasoning_present = any(
        bool(_get(message, name)) for name in ("reasoning_content", "reasoning", "analysis")
    )
    reasoning_tokens = (observation["usage"] or {}).get("reasoning_tokens")
    observation["validation"]["reasoning_field_present"] = reasoning_present
    observation["validation"]["reasoning_tokens"] = reasoning_tokens
    if observation["error_category"] == "reported_model_rejected":
        return observation
    if observation["usage"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
    elif observation["provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
    elif observation["finish_reason"] != "stop":
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(observation["finish_reason"])
    elif not reasoning_present and not (
        isinstance(reasoning_tokens, int) and reasoning_tokens > 0
    ):
        observation["status"] = "observed_unsupported"
        observation["error_category"] = "reasoning_not_observed"
    return observation


def _tool_observation(
    client: Any,
    profile: ProviderProfile,
    spec: ProbeSpec,
    policy: ProbeRequestPolicy,
    budget: CallBudget,
    quotes: tuple[PriceQuote, ...],
) -> tuple[dict[str, Any], int, bool]:
    tool = {
        "type": "function",
        "function": {
            "name": "pvian_echo_probe",
            "description": "Return a synthetic nonce without side effects.",
            "parameters": {
                "type": "object",
                "properties": {"nonce": {"type": "string"}},
                "required": ["nonce"],
                "additionalProperties": False,
            },
        },
    }
    first = _create(
        client,
        budget,
        model=spec.model,
        messages=[
            {
                "role": "user",
                "content": f"Call pvian_echo_probe exactly once with nonce {_TOOL_NONCE}",
            }
        ],
        tools=[tool],
        tool_choice=_tool_choice(policy),
        **_request_parameters(policy, spec.model, "tool"),
        **_variant_parameters(profile.request_variants[0]),
    )
    observation = _base_observation(profile, spec, first, quotes)
    observation["max_output_tokens"] = _output_cap(policy, spec.model, "tool")
    message = _get(_first_choice(first), "message", {})
    calls = _get(message, "tool_calls", []) or []
    observation["validation"]["tool_call_count"] = len(calls)
    valid = False
    call_id = None
    raw_arguments = None
    if len(calls) == 1:
        call = calls[0]
        function = _get(call, "function", {})
        call_id = _get(call, "id")
        raw_arguments = _get(function, "arguments")
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else None
        except json.JSONDecodeError:
            arguments = None
        valid = (
            isinstance(call_id, str)
            and _get(function, "name") == "pvian_echo_probe"
            and arguments == {"nonce": _TOOL_NONCE}
        )
    observation["validation"]["tool_arguments_valid"] = valid
    observation["validation"]["finish_protocol_conformant"] = (
        observation["finish_reason"] == "tool_calls"
    )
    if observation["error_category"] == "reported_model_rejected":
        return observation, 1, True
    if observation["usage"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
        return observation, 1, True
    if observation["provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
        return observation, 1, True
    if observation["finish_reason"] not in {"tool_calls", "stop"}:
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(observation["finish_reason"])
        return observation, 1, True
    if not valid:
        observation["status"] = "observed_unsupported"
        observation["error_category"] = "invalid_tool_call"
        return observation, 1, False
    replay_assistant: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "pvian_echo_probe",
                    "arguments": raw_arguments,
                },
            }
        ],
    }
    reasoning_content = _get(message, "reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        # Kimi's OpenAI-compatible contract requires the assistant reasoning
        # field to be replayed in memory.  It is never copied to evidence.
        replay_assistant["reasoning_content"] = reasoning_content
    replay = _create(
        client,
        budget,
        model=spec.model,
        messages=[
            {"role": "user", "content": "Use the synthetic tool."},
            replay_assistant,
            {"role": "tool", "tool_call_id": call_id, "content": "PVI_TOOL_RESULT_OK"},
        ],
        tools=[tool],
        tool_choice="none",
        **_request_parameters(policy, spec.model, "replay"),
        **_variant_parameters(profile.request_variants[0]),
    )
    replay_finish = _finish_reason(replay)
    replay_reported = _get(replay, "model")
    replay_identity = profile.reported_model_decision(spec.model, replay_reported)
    replay_usage = _usage(replay)
    replay_quote = select_quote(quotes, "bailian", "cn-beijing", spec.model)
    observation["replay_reported_model"] = replay_reported
    observation["replay_reported_model_identity"] = replay_identity.as_dict()
    observation["replay_finish_reason"] = replay_finish
    observation["replay_usage"] = replay_usage.as_dict() if replay_usage else None
    observation["replay_cost"] = estimate(replay_usage, replay_quote)
    observation["replay_provider_request_id_sha256"] = _provider_id_hash(replay)
    observation["validation"]["replay_completed"] = replay_finish == "stop"
    if not replay_identity.accepted:
        observation["status"] = "inconclusive"
        observation["error_category"] = "reported_model_rejected"
    elif replay_usage is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
    elif observation["replay_provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
    elif not observation["validation"]["replay_completed"]:
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(replay_finish)
    return observation, 2, _evidence_chain_failed(observation)


def _structured_observation(
    profile: ProviderProfile,
    spec: ProbeSpec,
    response: Any,
    quotes: tuple[PriceQuote, ...],
) -> dict[str, Any]:
    observation = _base_observation(profile, spec, response, quotes)
    try:
        valid = json.loads(_message_content(response)) == {"ok": True}
    except (json.JSONDecodeError, TypeError):
        valid = False
    observation["validation"]["structured_schema_valid"] = valid
    if observation["error_category"] == "reported_model_rejected":
        return observation
    if observation["usage"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "usage_missing"
    elif observation["provider_request_id_sha256"] is None:
        observation["status"] = "inconclusive"
        observation["error_category"] = "provider_request_id_missing"
    elif observation["finish_reason"] != "stop":
        observation["status"] = "inconclusive"
        observation["error_category"] = _finish_error(observation["finish_reason"])
    elif not valid:
        observation["status"] = "observed_unsupported"
        observation["error_category"] = "structured_schema_invalid"
    return observation


def _base_observation(
    profile: ProviderProfile,
    spec: ProbeSpec,
    response: Any,
    quotes: tuple[PriceQuote, ...],
) -> dict[str, Any]:
    reported = _get(response, "model")
    decision = profile.reported_model_decision(spec.model, reported)
    usage = _usage(response)
    quote = select_quote(quotes, "bailian", "cn-beijing", spec.model)
    finish = _finish_reason(response)
    result = {
        "probe": spec.probe,
        "variant_id": spec.variant_id,
        "status": "observed_supported",
        "requested_model": spec.model,
        "reported_model": reported,
        "reported_model_identity": decision.as_dict(),
        "finish_reason": finish,
        "usage": usage.as_dict() if usage else None,
        "cost": estimate(usage, quote),
        "provider_request_id_sha256": _provider_id_hash(response),
        "replay_reported_model": None,
        "replay_reported_model_identity": None,
        "replay_finish_reason": None,
        "replay_usage": None,
        "replay_cost": None,
        "replay_provider_request_id_sha256": None,
        "max_output_tokens": None,
        "validation": _empty_validation(text_only=spec.model == "qwen3-vl-plus"),
        "error_category": None,
    }
    if not decision.accepted:
        result["status"] = "inconclusive"
        result["error_category"] = "reported_model_rejected"
    elif finish == "length":
        result["status"] = "inconclusive"
        result["error_category"] = "output_limit_reached"
    elif finish is None:
        result["status"] = "inconclusive"
        result["error_category"] = "finish_reason_missing"
    return result


def _error_observation(
    profile: ProviderProfile,
    spec: ProbeSpec,
    category: str,
    *,
    status: str = "inconclusive",
) -> dict[str, Any]:
    decision = profile.reported_model_decision(spec.model, None)
    return {
        "probe": spec.probe,
        "variant_id": spec.variant_id,
        "status": status,
        "requested_model": spec.model,
        "reported_model": None,
        "reported_model_identity": decision.as_dict(),
        "finish_reason": None,
        "usage": None,
        "cost": {"status": "unknown", "reason": "usage_unknown"},
        "provider_request_id_sha256": None,
        "replay_reported_model": None,
        "replay_reported_model_identity": None,
        "replay_finish_reason": None,
        "replay_usage": None,
        "replay_cost": None,
        "replay_provider_request_id_sha256": None,
        "max_output_tokens": None,
        "validation": _empty_validation(text_only=spec.model == "qwen3-vl-plus"),
        "error_category": category,
    }


def _request_parameters(
    policy: ProbeRequestPolicy, model: str, probe: str
) -> dict[str, object]:
    cap = _output_cap(policy, model, probe)
    value: dict[str, object] = {policy.max_output_parameter: cap}
    if policy.temperature is not None:
        value["temperature"] = policy.temperature
    if policy.seed is not None:
        value["seed"] = policy.seed
    return value


def _output_cap(policy: ProbeRequestPolicy, model: str, probe: str) -> int:
    cap = {
        "message": policy.message_max_output_tokens,
        "stream": policy.stream_max_output_tokens,
        "thinking": policy.thinking_max_output_tokens,
        "tool": policy.tool_max_output_tokens,
        "replay": policy.replay_max_output_tokens,
    }[probe]
    if probe == "thinking":
        cap = max(cap, 4096 if model in {"glm-5.2", "deepseek-v4-pro"} else 2048)
    elif probe in {"tool", "replay"}:
        cap = max(cap, 1024)
    elif model == "kimi/kimi-k3":
        cap = max(cap, 1024)
    return cap


def _tool_choice(policy: ProbeRequestPolicy) -> object:
    """Honor the provider profile instead of assuming named-function forcing.

    Some OpenAI-compatible providers, notably Kimi through Bailian, accept
    ``auto`` but reject the OpenAI named-function object.  Profiles that need
    deterministic named-function forcing retain the existing object form.
    """

    if policy.tool_choice_mode == "auto":
        return "auto"
    if policy.tool_choice_mode == "none":
        return "none"
    return {"type": "function", "function": {"name": "pvian_echo_probe"}}


def _variant_parameters(variant: dict[str, object]) -> dict[str, object]:
    overrides = variant.get("request_overrides")
    if not isinstance(overrides, dict):
        return {}
    standard: dict[str, object] = {}
    extra: dict[str, object] = {}
    for name, value in overrides.items():
        (standard if name == "reasoning_effort" else extra)[name] = value
    if extra:
        standard["extra_body"] = extra
    return standard


def _usage(response: Any) -> UsageSummary | None:
    raw = _get(response, "usage")
    if raw is None:
        return None
    prompt = _safe_int(_get(raw, "prompt_tokens"))
    completion = _safe_int(_get(raw, "completion_tokens"))
    total = _safe_int(_get(raw, "total_tokens"))
    cached = _safe_int(_get(_get(raw, "prompt_tokens_details", {}), "cached_tokens")) or 0
    reasoning = _safe_int(
        _get(_get(raw, "completion_tokens_details", {}), "reasoning_tokens")
    )
    if prompt is None or completion is None or cached > prompt:
        return None
    if total is not None and total != prompt + completion:
        return None
    return UsageSummary(prompt, cached, reasoning, completion, total)


def _provider_id_hash(value: Any) -> str | None:
    provider_id = _get(value, "_request_id") or _get(value, "request_id")
    field_name = "provider-request-id"
    if not provider_id:
        response = _get(value, "response") or _get(value, "_response")
        headers = _get(response, "headers", {})
        for name in (
            "x-request-id",
            "request-id",
            "x-dashscope-request-id",
            "x-acs-request-id",
        ):
            getter = getattr(headers, "get", None)
            candidate = getter(name) if callable(getter) else _get(headers, name)
            if isinstance(candidate, str) and candidate:
                provider_id = candidate
                field_name = name
                break
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 1024:
        return None
    return "sha256:" + hashlib.sha256(f"{field_name}:{provider_id}".encode()).hexdigest()


def _exception_category(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status in {401, 403}:
        return "authentication"
    if status == 429:
        return "rate_limited"
    if status in {408, 504}:
        return "timeout"
    if isinstance(status, int) and status >= 500:
        return "upstream_5xx"
    if isinstance(status, int):
        return "capability_parameter_rejected"
    return "transport_or_client_error"


def _length_stop(response: Any) -> bool:
    return _finish_reason(response) == "length"


def _finish_error(finish_reason: str | None) -> str:
    if finish_reason == "length":
        return "output_limit_reached"
    if finish_reason is None:
        return "finish_reason_missing"
    return "unexpected_finish_reason"


def _evidence_chain_failed(observation: dict[str, Any]) -> bool:
    return observation["status"] == "inconclusive"


def _finish_reason(response: Any) -> str | None:
    return _get(_first_choice(response), "finish_reason")


def _message_content(response: Any) -> str:
    value = _get(_get(_first_choice(response), "message", {}), "content")
    return value if isinstance(value, str) else ""


def _first_choice(value: Any) -> Any:
    choices = _get(value, "choices", [])
    return choices[0] if choices else {}


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _required_variant_id(variant: dict[str, object]) -> str:
    value = variant.get("variant_id")
    if not isinstance(value, str) or not value:
        raise SweepError("thinking variant_id must be a non-empty string")
    return value


def _empty_validation(*, text_only: bool) -> dict[str, Any]:
    return {
        "output_match": None,
        "output_nonempty": None,
        "stream_chunks": None,
        "reasoning_field_present": None,
        "reasoning_tokens": None,
        "tool_call_count": None,
        "tool_arguments_valid": None,
        "replay_completed": None,
        "structured_schema_valid": None,
        "finish_protocol_conformant": None,
        "text_only": text_only,
    }
