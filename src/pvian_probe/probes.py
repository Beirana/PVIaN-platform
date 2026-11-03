from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from pvian_probe.config import EndpointSettings, ModelTarget
from pvian_probe.redaction import redact_text

MESSAGE_PROMPT = "Reply with exactly PVI_MESSAGE_OK and no punctuation."
THINKING_PROMPT = (
    "Start with x = 29. Repeat exactly eight times: x = (x*x + 17) mod 997. "
    "Think as needed, then return only the final integer."
)
THINKING_EXPECTED = "26"
TOOL_NONCE = "pvian-matrix-nonce"

CAPABILITIES = ("message", "thinking", "tool_calling")


@dataclass(frozen=True)
class ProbeObservation:
    vendor: str
    capability: str
    requested_model: str
    reported_model: str | None
    status: str
    started_at: str
    completed_at: str
    latency_ms: int
    input_digest: str
    details: dict[str, Any]
    error: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MatrixProbeRunner:
    def __init__(self, client: Any, settings: EndpointSettings) -> None:
        self.client = client
        self.settings = settings

    def run(self, targets: list[ModelTarget]) -> list[ProbeObservation]:
        observations: list[ProbeObservation] = []
        for target in targets:
            observations.extend(
                [
                    self._capture(target, "message", self._probe_message),
                    self._capture(target, "thinking", self._probe_thinking),
                    self._capture(target, "tool_calling", self._probe_tool),
                ]
            )
        return observations

    def _capture(
        self,
        target: ModelTarget,
        capability: str,
        probe: Callable[
            [ModelTarget],
            tuple[str, str | None, dict[str, Any], dict[str, Any]],
        ],
    ) -> ProbeObservation:
        started = datetime.now(UTC)
        started_monotonic = time.perf_counter()
        request_summary: dict[str, Any] = {
            "vendor": target.vendor,
            "model": target.model,
            "capability": capability,
        }
        try:
            status, reported_model, details, request_summary = probe(target)
            error = None
        except Exception as exc:
            status = "inconclusive"
            reported_model = None
            details = {"request_completed": False}
            error = {
                "type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "message": redact_text(exc, secrets=(self.settings.api_key,)),
            }
        completed = datetime.now(UTC)
        return ProbeObservation(
            vendor=target.vendor,
            capability=capability,
            requested_model=target.model,
            reported_model=reported_model,
            status=status,
            started_at=_timestamp(started),
            completed_at=_timestamp(completed),
            latency_ms=max(
                0,
                round((time.perf_counter() - started_monotonic) * 1000),
            ),
            input_digest=_digest(request_summary),
            details=details,
            error=error,
        )

    def _probe_message(
        self,
        target: ModelTarget,
    ) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        request = self._base_request(target, MESSAGE_PROMPT, token_limit=128)
        response = self._create_response(request)
        choice = response.choices[0]
        content = choice.message.content or ""
        matched = content.strip() == "PVI_MESSAGE_OK"
        details = {
            "request_completed": True,
            "exact_match": matched,
            "output_length": len(content),
            "output_sha256": _sha256_text(content),
            "finish_reason": choice.finish_reason,
            "usage": _usage(response),
        }
        return (
            "observed_supported" if matched else "inconclusive",
            getattr(response, "model", None),
            details,
            _request_summary(request),
        )

    def _probe_thinking(
        self,
        target: ModelTarget,
    ) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        request = self._base_request(target, THINKING_PROMPT, token_limit=192)
        response = self._create_response(request)
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        exposure, native_field_names = _reasoning_exposure(message, response)
        final_exact_match = content.strip() == THINKING_EXPECTED
        details = {
            "request_completed": True,
            "reasoning_exposure": exposure,
            "native_reasoning_field_names": native_field_names,
            "reasoning_tokens": _reasoning_tokens(response),
            "final_exact_match": final_exact_match,
            "output_length": len(content),
            "output_sha256": _sha256_text(content),
            "finish_reason": choice.finish_reason,
            "usage": _usage(response),
        }
        status = "observed_supported" if exposure != "none_observed" else "not_observed"
        return (
            status,
            getattr(response, "model", None),
            details,
            _request_summary(request),
        )

    def _probe_tool(
        self,
        target: ModelTarget,
    ) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        request = self._base_request(
            target,
            (
                "Call pvi_matrix_probe exactly once with nonce "
                f"{TOOL_NONCE}. Do not answer with plain text."
            ),
            token_limit=192,
        )
        request["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "pvi_matrix_probe",
                    "description": "Accept a synthetic nonce; has no side effects.",
                    "parameters": {
                        "type": "object",
                        "properties": {"nonce": {"type": "string"}},
                        "required": ["nonce"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        request["tool_choice"] = {
            "type": "function",
            "function": {"name": "pvi_matrix_probe"},
        }
        response = self._create_response(request)
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        matched = False
        valid_json = False
        arguments_digest = None
        if len(tool_calls) == 1:
            call = tool_calls[0]
            raw_arguments = call.function.arguments or ""
            arguments_digest = _sha256_text(raw_arguments)
            try:
                arguments = json.loads(raw_arguments)
                valid_json = isinstance(arguments, dict)
            except json.JSONDecodeError:
                arguments = None
            matched = (
                call.function.name == "pvi_matrix_probe"
                and valid_json
                and arguments.get("nonce") == TOOL_NONCE
            )
        details = {
            "request_completed": True,
            "native_tool_call_observed": matched,
            "tool_call_count": len(tool_calls),
            "arguments_valid_json": valid_json,
            "arguments_sha256": arguments_digest,
            "finish_reason": choice.finish_reason,
            "usage": _usage(response),
            "tool_executed": False,
        }
        return (
            "observed_supported" if matched else "not_observed",
            getattr(response, "model", None),
            details,
            _request_summary(request),
        )

    def _create_response(self, request: dict[str, Any]) -> Any:
        response = self.client.chat.completions.create(**request)
        if request.get("stream"):
            return _collect_stream(response)
        return response

    @staticmethod
    def _base_request(
        target: ModelTarget,
        prompt: str,
        *,
        token_limit: int,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": target.model,
            "messages": [{"role": "user", "content": prompt}],
            target.token_limit_parameter: token_limit,
        }
        if target.reasoning_effort:
            request["reasoning_effort"] = target.reasoning_effort
        if target.stream:
            request["stream"] = True
        return request


def _collect_stream(stream: Any) -> Any:
    content_parts: list[str] = []
    reasoning_parts: dict[str, list[str]] = {}
    tool_parts: dict[int, dict[str, str]] = {}
    finish_reason = None
    reported_model = None
    usage = None

    for chunk in stream:
        reported_model = getattr(chunk, "model", None) or reported_model
        usage = getattr(chunk, "usage", None) or usage
        for choice in getattr(chunk, "choices", []) or []:
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = _read_field(delta, "content")
            if isinstance(content, str):
                content_parts.append(content)
            for field_name in (
                "reasoning_content",
                "reasoning",
                "reasoning_details",
                "thinking",
                "thinking_content",
            ):
                piece = _read_field(delta, field_name)
                if piece not in (None, "", [], {}):
                    reasoning_parts.setdefault(field_name, []).append(str(piece))
            for call in _read_field(delta, "tool_calls") or []:
                index = getattr(call, "index", None)
                if not isinstance(index, int):
                    index = 0
                entry = tool_parts.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                entry["id"] += getattr(call, "id", None) or ""
                function = getattr(call, "function", None)
                if function is not None:
                    entry["name"] += getattr(function, "name", None) or ""
                    entry["arguments"] += getattr(function, "arguments", None) or ""

    tool_calls = [
        SimpleNamespace(
            id=value["id"] or None,
            function=SimpleNamespace(
                name=value["name"],
                arguments=value["arguments"],
            ),
        )
        for _, value in sorted(tool_parts.items())
    ]
    message = SimpleNamespace(
        content="".join(content_parts) or None,
        tool_calls=tool_calls or None,
        model_extra={name: "".join(parts) for name, parts in reasoning_parts.items()},
    )
    return SimpleNamespace(
        model=reported_model,
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


def _reasoning_exposure(message: Any, response: Any) -> tuple[str, list[str]]:
    exposed_fields: list[str] = []
    for field_name in (
        "reasoning_content",
        "reasoning",
        "reasoning_details",
        "thinking",
        "thinking_content",
    ):
        value = _read_field(message, field_name)
        if value not in (None, "", [], {}):
            exposed_fields.append(field_name)
    if exposed_fields:
        return "visible_reasoning_text", exposed_fields
    if (_reasoning_tokens(response) or 0) > 0:
        return "token_usage_only", []
    return "none_observed", []


def _read_field(value: Any, field_name: str) -> Any:
    attribute = getattr(value, field_name, None)
    if attribute is not None:
        return attribute
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(field_name)
    return None


def _usage(response: Any) -> dict[str, int | None] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "cached_tokens": getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
        ),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "reasoning_tokens": _reasoning_tokens(response),
    }


def _reasoning_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    if value is not None:
        return value
    extra = getattr(usage, "model_extra", None)
    if isinstance(extra, dict):
        completion_details = extra.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            candidate = completion_details.get("reasoning_tokens")
            return candidate if isinstance(candidate, int) else None
    return None


def _request_summary(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": request["model"],
        "message_roles": [message.get("role") for message in request.get("messages", [])],
        "has_tools": bool(request.get("tools")),
        "tool_choice_present": "tool_choice" in request,
        "reasoning_effort": request.get("reasoning_effort"),
        "stream": bool(request.get("stream")),
        "max_tokens": request.get("max_tokens"),
        "max_completion_tokens": request.get("max_completion_tokens"),
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
