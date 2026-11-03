"""Profile-driven, explicitly authorized qualification for the transparent gateway.

Only synthetic inputs are sent.  This module returns and persists metadata,
field-presence summaries and hashes; raw prompts, outputs, reasoning and remote
error text never enter a qualification record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pvi.probe_runner.client import create_openai_client
from pvian_gateway.profiles import (
    STRICT_REPORTED_MODEL_POLICY_ID,
    ProbeRequestPolicy,
    ProviderProfile,
    ReportedModelIdentityDecision,
    ResolvedEndpoint,
)
from pvian_gateway.state import QualificationStateStore
from pvian_probe.probes import _collect_stream, _reasoning_exposure, _reasoning_tokens

TEXT_PROMPT = "Reply with exactly: PVI_QUALIFY_OK"
STREAM_PROMPT = "Reply with exactly: PVI_QUALIFY_STREAM_OK"
TOOL_NONCE = "pvian-qualification-nonce"
_TOOL_PROBES = {"native_tool_call", "assistant_replay", "tool_result_replay"}


@dataclass(frozen=True)
class QualificationResult:
    route_id: str
    profile_id: str
    requested_model: str
    reported_model: str | None
    reported_model_identity: dict[str, str]
    qualification_status: str
    valid_until: str | None
    capabilities: dict[str, Any]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "profile_id": self.profile_id,
            "requested_model": self.requested_model,
            "reported_model": self.reported_model,
            "reported_model_identity": self.reported_model_identity,
            "qualification_status": self.qualification_status,
            "valid_until": self.valid_until,
            "capabilities": self.capabilities,
        }


def qualify_profile(
    profile: ProviderProfile,
    state: QualificationStateStore,
    *,
    due_only: bool = False,
) -> list[QualificationResult]:
    results: list[QualificationResult] = []
    credential = profile.credential()
    for endpoint_definition in profile.endpoints:
        if (
            not endpoint_definition.qualification_enabled
            or endpoint_definition.transport != "openai_compatible_chat_completions"
        ):
            continue
        endpoint = profile.resolve_endpoint(endpoint_definition)
        client = create_openai_client(
            base_url=endpoint.base_url,
            api_key=credential,
            timeout_seconds=_probe_policy(profile).timeout_seconds,
            max_retries=0,
        )
        catalog_ids = {item.id for item in client.models.list().data}
        for target in profile.targets:
            model = target.select_from_catalog(catalog_ids)
            if model is None:
                continue
            route_id = endpoint.route_id(model)
            if due_only and not state.renewal_due(route_id):
                continue
            result = _qualify_model(profile, endpoint, client, model)
            record = state.record_qualification(
                route_id=route_id,
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                provider_id=profile.provider_id,
                endpoint_id=endpoint.endpoint.endpoint_id,
                requested_model=model,
                reported_model=result.reported_model,
                reported_model_identity=result.reported_model_identity,
                passed=result.qualification_status == "enabled",
                capability_summary=result.capabilities,
                validity_hours=profile.policy.validity_hours,
                failure_threshold=profile.policy.consecutive_failure_threshold,
            )
            results.append(
                QualificationResult(
                    route_id=route_id,
                    profile_id=profile.profile_id,
                    requested_model=model,
                    reported_model=result.reported_model,
                    reported_model_identity=result.reported_model_identity,
                    qualification_status=record["qualification_status"],
                    valid_until=record["valid_until"],
                    capabilities=result.capabilities,
                )
            )
    return results


def _qualify_model(
    profile: ProviderProfile,
    endpoint: ResolvedEndpoint,
    client: Any,
    model: str,
) -> QualificationResult:
    del endpoint
    probes = set(profile.policy.required_probes)
    policy = _probe_policy(profile)
    baseline = _run_baseline(
        client,
        model,
        _baseline_variant(profile),
        policy,
        bool(probes & {"message", "usage"}),
        "stream" in probes,
    )
    capabilities: dict[str, Any] = {
        "configuration_digest": _digest(
            {
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "model": model,
                "required_probes": sorted(probes),
                "variants": profile.request_variants,
                "probe_request_policy": policy.__dict__,
            }
        ),
    }
    for name in ("message", "stream"):
        if name in probes:
            capabilities[name] = baseline.get(name)
    # Usage is a zero-extra-call observation of the message probe.  Preserve it
    # whenever a message was sent, even when it is not an enablement criterion.
    if probes & {"message", "usage"}:
        capabilities["usage"] = baseline.get("usage")
    thinking: dict[str, Any] = {}
    if "thinking" in probes:
        thinking = _run_thinking(client, model, profile.request_variants, policy)
        capabilities["thinking"] = thinking
    tools: dict[str, Any] = {}
    if probes & _TOOL_PROBES:
        if policy.tool_choice_mode == "none":
            raise ValueError("tool probes cannot run with tool_choice_mode=none")
        tools = _run_tool_and_replay(client, model, _baseline_variant(profile), policy)
        for name in _TOOL_PROBES:
            if name in probes:
                capabilities[name] = tools[name]
    passed = all(_probe_passed(name, capabilities.get(name)) for name in probes)
    reported_model = (
        baseline.get("reported_model")
        or thinking.get("reported_model")
        or tools.get("reported_model")
    )
    identity = _reported_model_identity(profile, model, reported_model)
    return QualificationResult(
        route_id="",
        profile_id=profile.profile_id,
        requested_model=model,
        reported_model=reported_model,
        reported_model_identity=identity.as_dict(),
        qualification_status="enabled" if passed and identity.accepted else "candidate",
        valid_until=None,
        capabilities=capabilities,
    )


def _reported_model_identity(
    profile: ProviderProfile | Any, requested_model: str, reported_model: str | None
) -> ReportedModelIdentityDecision:
    resolver = getattr(profile, "reported_model_decision", None)
    if callable(resolver):
        return resolver(requested_model, reported_model)
    return ReportedModelIdentityDecision(
        STRICT_REPORTED_MODEL_POLICY_ID,
        "exact" if reported_model == requested_model else "rejected",
    )


def _baseline_variant(profile: ProviderProfile) -> dict[str, object]:
    for variant in profile.request_variants:
        if variant.get("variant_id") == "baseline":
            return variant
    return profile.request_variants[0]


def _run_baseline(
    client: Any,
    model: str,
    variant: dict[str, object],
    policy: ProbeRequestPolicy,
    message: bool,
    stream: bool,
) -> dict[str, Any]:
    overrides = _request_overrides(variant)
    message_result = _message_probe(client, model, overrides, policy) if message else None
    stream_result = _stream_probe(client, model, overrides, policy) if stream else None
    return {
        "message": message_result["result"] if message_result is not None else None,
        "stream": stream_result["result"] if stream_result is not None else None,
        "usage": message_result["usage"] if message_result is not None else None,
        "reported_model": (
            (message_result or {}).get("reported_model")
            or (stream_result or {}).get("reported_model")
        ),
    }


def _message_probe(
    client: Any, model: str, overrides: dict[str, object], policy: ProbeRequestPolicy
) -> dict[str, Any]:
    request = {
        "model": model,
        "messages": [{"role": "user", "content": TEXT_PROMPT}],
        **_base_request_parameters(policy, "message"),
        **_with_overrides(overrides),
    }
    try:
        response = client.chat.completions.create(**request)
        content = _get(response, "choices", [])[0].message.content or ""
        return {
            "result": {
                "status": "observed_supported"
                if content.strip() == "PVI_QUALIFY_OK"
                else "inconclusive",
                "output_length": len(content),
                "output_sha256": _hash_text(content),
                "finish_reason": _get(response, "choices", [])[0].finish_reason,
            },
            "reported_model": _get(response, "model"),
            "usage": _safe_usage(response),
        }
    except Exception as exc:  # SDK-specific failure classes are intentionally not persisted.
        return {"result": _inconclusive(exc), "reported_model": None, "usage": None}


def _stream_probe(
    client: Any, model: str, overrides: dict[str, object], policy: ProbeRequestPolicy
) -> dict[str, Any]:
    request = {
        "model": model,
        "messages": [{"role": "user", "content": STREAM_PROMPT}],
        **_base_request_parameters(policy, "stream"),
        "stream": True,
        "stream_options": {"include_usage": True},
        **_with_overrides(overrides),
    }
    try:
        chunks = list(client.chat.completions.create(**request))
        response = _collect_stream(chunks)
        choice = _get(response, "choices", [])[0]
        content = _get(_get(choice, "message"), "content") or ""
        return {
            "result": {
                "status": "observed_supported"
                if content.strip() == "PVI_QUALIFY_STREAM_OK" and chunks
                else "inconclusive",
                "chunk_count": len(chunks),
                "output_length": len(content),
                "output_sha256": _hash_text(content),
                "finish_reason": _get(choice, "finish_reason"),
            },
            "reported_model": _get(response, "model"),
        }
    except Exception as exc:
        return {"result": _inconclusive(exc), "reported_model": None}


def _run_thinking(
    client: Any,
    model: str,
    variants: tuple[dict[str, object], ...],
    policy: ProbeRequestPolicy,
) -> dict[str, Any]:
    observations: list[dict[str, object]] = []
    reported_model = None
    for variant in variants:
        request = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply exactly: PVI_THINKING_OK"}],
            **_base_request_parameters(policy, "thinking"),
            **_with_overrides(_request_overrides(variant)),
        }
        try:
            response = client.chat.completions.create(**request)
            reported_model = reported_model or _get(response, "model")
            message = _get(_get(response, "choices", [])[0], "message")
            exposure, native_fields = _reasoning_exposure(message, response)
            usage = _safe_usage(response) or {}
            reasoning_tokens = _reasoning_tokens(response)
            observations.append(
                {
                    "variant_id": variant.get("variant_id"),
                    "request_completed": True,
                    "reasoning_field_present": exposure == "visible_reasoning_text",
                    "reasoning_field_names": native_fields,
                    "reasoning_tokens": reasoning_tokens,
                    "usage_present": bool(usage),
                }
            )
        except Exception as exc:
            observations.append(
                {
                    "variant_id": variant.get("variant_id"),
                    "request_completed": False,
                    "error_category": type(exc).__name__,
                }
            )
    supported = any(
        bool(item.get("reasoning_field_present"))
        or (isinstance(item.get("reasoning_tokens"), int) and item["reasoning_tokens"] > 0)
        for item in observations
    )
    return {
        "status": "observed_supported" if supported else "not_observed",
        "variants": observations,
        "reported_model": reported_model,
    }


def _run_tool_and_replay(
    client: Any,
    model: str,
    variant: dict[str, object],
    policy: ProbeRequestPolicy,
) -> dict[str, Any]:
    tool = {
        "type": "function",
        "function": {
            "name": "pvian_echo_probe",
            "description": "Return a synthetic nonce. No side effects.",
            "parameters": {
                "type": "object",
                "properties": {"nonce": {"type": "string"}},
                "required": ["nonce"],
                "additionalProperties": False,
            },
        },
    }
    request = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Call pvian_echo_probe exactly once with nonce " + TOOL_NONCE,
            }
        ],
        "tools": [tool],
        "tool_choice": (
            {"type": "function", "function": {"name": "pvian_echo_probe"}}
            if policy.tool_choice_mode == "forced_function"
            else "auto"
        ),
        **_base_request_parameters(policy, "tool"),
        **_with_overrides(_request_overrides(variant)),
    }
    try:
        response = client.chat.completions.create(**request)
        choice = _get(response, "choices", [])[0]
        calls = _get(_get(choice, "message"), "tool_calls") or []
        reported_model = _get(response, "model")
        if len(calls) != 1:
            return _tool_result(
                "not_observed", "not_observed", "not_observed", reported_model, 0, None
            )
        call = calls[0]
        name = _get(_get(call, "function"), "name")
        raw_arguments = _get(_get(call, "function"), "arguments") or ""
        call_id = _get(call, "id")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = None
        native_ok = (
            isinstance(call_id, str)
            and name == "pvian_echo_probe"
            and isinstance(arguments, dict)
            and arguments.get("nonce") == TOOL_NONCE
        )
        if not native_ok:
            return _tool_result(
                "inconclusive", "not_observed", "not_observed", reported_model, len(calls), None
            )
        assistant_message = {
            "role": "assistant",
            "content": _get(_get(choice, "message"), "content"),
            "tool_calls": [
                {
                    "id": call_id,
                    "type": _get(call, "type") or "function",
                    "function": {"name": name, "arguments": raw_arguments},
                }
            ],
        }
        replay = client.chat.completions.create(
            model=model,
            messages=[
                request["messages"][0],
                assistant_message,
                {"role": "tool", "tool_call_id": call_id, "content": "PVI_TOOL_RESULT_OK"},
            ],
            **_base_request_parameters(policy, "replay"),
            **_with_overrides(_request_overrides(variant)),
        )
        replay_choice = _get(replay, "choices", [])[0]
        replay_content = _get(_get(replay_choice, "message"), "content") or ""
        replay_ok = (
            bool(replay_content.strip()) and _get(replay_choice, "finish_reason") != "tool_calls"
        )
        status = "observed_supported" if replay_ok else "inconclusive"
        return _tool_result(
            "observed_supported",
            status,
            status,
            reported_model,
            len(calls),
            _hash_text(call_id),
        )
    except Exception as exc:
        result = _inconclusive(exc)
        return {
            "native_tool_call": result,
            "assistant_replay": result,
            "tool_result_replay": result,
            "reported_model": None,
        }


def _tool_result(
    native: str,
    assistant_replay: str,
    tool_replay: str,
    reported_model: str | None,
    call_count: int,
    call_id_hash: str | None,
) -> dict[str, Any]:
    return {
        "native_tool_call": {
            "status": native,
            "tool_call_count": call_count,
            "call_id_sha256": call_id_hash,
        },
        "assistant_replay": {"status": assistant_replay},
        "tool_result_replay": {"status": tool_replay},
        "reported_model": reported_model,
    }


def _request_overrides(variant: dict[str, object]) -> dict[str, object]:
    value = variant.get("request_overrides", {})
    return dict(value) if isinstance(value, dict) else {}


def qualification_completion_call_count(profile: ProviderProfile | Any) -> int:
    """Return the exact maximum completion-call budget for a profile.

    Catalog discovery is a separate GET and is intentionally not counted here.
    A tool/replay plan reserves two calls; fail-closed early termination can use
    fewer, but can never exceed this value because the SDK retries are disabled.
    """

    probes = set(profile.policy.required_probes)
    count = int(bool(probes & {"message", "usage"}))
    count += int("stream" in probes)
    count += len(profile.request_variants) if "thinking" in probes else 0
    count += 2 if probes & _TOOL_PROBES else 0
    return count


def _probe_policy(profile: ProviderProfile | Any) -> ProbeRequestPolicy:
    return getattr(profile.policy, "probe_request_policy", None) or ProbeRequestPolicy()


def _probe_passed(name: str, observation: object) -> bool:
    if name == "usage":
        return isinstance(observation, dict)
    return isinstance(observation, dict) and observation.get("status") == "observed_supported"


def _base_request_parameters(policy: ProbeRequestPolicy, probe: str) -> dict[str, object]:
    token_field = {
        "message": policy.message_max_output_tokens,
        "stream": policy.stream_max_output_tokens,
        "thinking": policy.thinking_max_output_tokens,
        "tool": policy.tool_max_output_tokens,
        "replay": policy.replay_max_output_tokens,
    }[probe]
    result: dict[str, object] = {policy.max_output_parameter: token_field}
    if policy.temperature is not None:
        result["temperature"] = policy.temperature
    if policy.seed is not None:
        result["seed"] = policy.seed
    return result


def _with_overrides(overrides: dict[str, object]) -> dict[str, object]:
    """Pass vendor extensions without asking the OpenAI SDK to type them.

    ``reasoning_effort`` is accepted by the SDK; fields such as
    ``enable_thinking`` must be emitted through ``extra_body`` so they reach
    the exact upstream body instead of failing client-side.
    """

    standard: dict[str, object] = {}
    extensions: dict[str, object] = {}
    for name, value in overrides.items():
        if name == "reasoning_effort":
            standard[name] = value
        else:
            extensions[name] = value
    if extensions:
        standard["extra_body"] = extensions
    return standard


def _inconclusive(exc: Exception) -> dict[str, object]:
    return {"status": "inconclusive", "error_category": type(exc).__name__}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_usage(value: Any) -> dict[str, int | None] | None:
    """Extract a canonical 5-key redacted usage summary from a provider response.

    Returns ``None`` when usage is absent or required fields are invalid,
    so the state validator treats it as unknown rather than persisting
    a structurally invalid partial summary.
    """

    usage = _get(value, "usage")
    if usage is None:
        return None
    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    total = _get(usage, "total_tokens")
    # A: required fields must be non-negative ints; otherwise usage is unknown.
    if not _is_safe_int(prompt) or not _is_safe_int(completion):
        return None
    if total is not None and not _is_safe_int(total):
        return None
    cached = _get(_get(usage, "prompt_tokens_details"), "cached_tokens")
    reasoning = _get(_get(usage, "completion_tokens_details"), "reasoning_tokens")
    if cached is not None and not _is_safe_int(cached):
        return None
    if reasoning is not None and not _is_safe_int(reasoning):
        return None
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "reasoning_tokens": reasoning,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _is_safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
