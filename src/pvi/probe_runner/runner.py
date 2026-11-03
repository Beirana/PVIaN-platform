from __future__ import annotations

import hashlib
import json
import platform
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pvi.probe_runner.config import ProbeSettings
from pvi.probe_runner.redaction import redact_text

TEXT_PROBE_ID = "online_text_message_probe"
TOOL_PROBE_ID = "online_native_tool_call_probe"
STREAM_PROBE_ID = "online_streaming_probe"
PROBE_VERSION = "1.0.0"

PROBE_ALIASES = {
    "text": TEXT_PROBE_ID,
    "tool": TOOL_PROBE_ID,
    "stream": STREAM_PROBE_ID,
}

TEXT_PROMPT = "Reply with exactly: PVI_TEXT_OK"
STREAM_PROMPT = "Reply with exactly: PVI_STREAM_OK"
TOOL_NONCE = "pvi-probe-nonce"


@dataclass(frozen=True)
class ProbeObservation:
    probe_id: str
    probe_version: str
    started_at: str
    completed_at: str
    verdict: str
    requested_model: str
    reported_model: str | None
    latency_ms: int
    input_digest: str
    configuration_digest: str
    environment_digest: str
    details: dict[str, Any]
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OnlineProbeRunner:
    def __init__(self, client: Any, settings: ProbeSettings) -> None:
        self.client = client
        self.settings = settings
        self.configuration_digest = _digest(settings.safe_summary())
        self.environment_digest = _digest(
            {
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "system": platform.system(),
                "machine": platform.machine(),
            }
        )

    def run(self, probe_names: list[str]) -> list[ProbeObservation]:
        observations: list[ProbeObservation] = []
        for name in probe_names:
            try:
                probe_id = PROBE_ALIASES[name]
            except KeyError as exc:
                raise ValueError(f"unsupported probe: {name}") from exc
            probe = {
                "text": self._run_text,
                "tool": self._run_tool,
                "stream": self._run_stream,
            }[name]
            observations.append(self._capture(probe_id, probe))
        return observations

    def _capture(
        self,
        probe_id: str,
        probe: Callable[[], tuple[str, str | None, dict[str, Any], dict[str, Any]]],
    ) -> ProbeObservation:
        started = datetime.now(UTC)
        monotonic_started = time.perf_counter()
        input_payload: dict[str, Any] = {"probe_id": probe_id}
        try:
            verdict, reported_model, details, input_payload = probe()
            error = None
        except Exception as exc:
            verdict = "inconclusive"
            reported_model = None
            details = {"request_completed": False}
            error = {
                "type": type(exc).__name__,
                "message": redact_text(exc, secrets=(self.settings.api_key,)),
            }
        completed = datetime.now(UTC)
        return ProbeObservation(
            probe_id=probe_id,
            probe_version=PROBE_VERSION,
            started_at=_timestamp(started),
            completed_at=_timestamp(completed),
            verdict=verdict,
            requested_model=self.settings.model,
            reported_model=reported_model,
            latency_ms=max(0, round((time.perf_counter() - monotonic_started) * 1000)),
            input_digest=_digest(input_payload),
            configuration_digest=self.configuration_digest,
            environment_digest=self.environment_digest,
            details=details,
            error=error,
        )

    def _run_text(self) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        request = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": TEXT_PROMPT}],
            "temperature": 0,
            "max_tokens": 16,
        }
        response = self.client.chat.completions.create(**request)
        choice = response.choices[0]
        content = choice.message.content or ""
        passed = content.strip() == "PVI_TEXT_OK"
        details = {
            "request_completed": True,
            "exact_match": passed,
            "output_length": len(content),
            "output_sha256": _sha256_text(content),
            "finish_reason": choice.finish_reason,
            "usage": _safe_usage(response),
            "request_id_present": bool(getattr(response, "_request_id", None)),
        }
        verdict = "observed_supported" if passed else "inconclusive"
        return verdict, getattr(response, "model", None), details, request

    def _run_tool(self) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        tool = {
            "type": "function",
            "function": {
                "name": "pvi_echo_probe",
                "description": "Return a synthetic probe nonce without external side effects.",
                "parameters": {
                    "type": "object",
                    "properties": {"nonce": {"type": "string"}},
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            },
        }
        request = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Call pvi_echo_probe once with nonce "
                        f"{TOOL_NONCE}. Do not answer with plain text."
                    ),
                }
            ],
            "tools": [tool],
            "tool_choice": {
                "type": "function",
                "function": {"name": "pvi_echo_probe"},
            },
            "temperature": 0,
            "max_tokens": 64,
        }
        response = self.client.chat.completions.create(**request)
        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []
        matched = False
        arguments_valid_json = False
        argument_digest = None
        if len(tool_calls) == 1:
            call = tool_calls[0]
            raw_arguments = call.function.arguments or ""
            argument_digest = _sha256_text(raw_arguments)
            try:
                arguments = json.loads(raw_arguments)
                arguments_valid_json = isinstance(arguments, dict)
            except json.JSONDecodeError:
                arguments = None
            matched = (
                call.function.name == "pvi_echo_probe"
                and arguments_valid_json
                and arguments.get("nonce") == TOOL_NONCE
            )
        details = {
            "request_completed": True,
            "native_tool_call_observed": matched,
            "tool_call_count": len(tool_calls),
            "arguments_valid_json": arguments_valid_json,
            "argument_sha256": argument_digest,
            "finish_reason": choice.finish_reason,
            "usage": _safe_usage(response),
        }
        verdict = "observed_supported" if matched else "inconclusive"
        return verdict, getattr(response, "model", None), details, request

    def _run_stream(self) -> tuple[str, str | None, dict[str, Any], dict[str, Any]]:
        request = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": STREAM_PROMPT}],
            "temperature": 0,
            "max_tokens": 16,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        stream = self.client.chat.completions.create(**request)
        parts: list[str] = []
        chunk_count = 0
        finish_reason = None
        reported_model = None
        usage = None
        for chunk in stream:
            chunk_count += 1
            reported_model = reported_model or getattr(chunk, "model", None)
            chunk_usage = _safe_usage(chunk)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            content = getattr(choice.delta, "content", None)
            if content:
                parts.append(content)
            finish_reason = choice.finish_reason or finish_reason
        content = "".join(parts)
        passed = content.strip() == "PVI_STREAM_OK" and chunk_count > 0
        details = {
            "request_completed": True,
            "stream_observed": passed,
            "chunk_count": chunk_count,
            "output_length": len(content),
            "output_sha256": _sha256_text(content),
            "finish_reason": finish_reason,
            "usage": usage,
        }
        verdict = "observed_supported" if passed else "inconclusive"
        return verdict, reported_model, details, request


def _safe_usage(value: Any) -> dict[str, int | None] | None:
    usage = getattr(value, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "cached_tokens": getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
        ),
        "reasoning_tokens": getattr(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None
        ),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
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
