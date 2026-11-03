"""Kimi K3 route-scoped SSE-to-nonstream compatibility adapter.

The adapter is installed only by an explicitly isolated recovery entrypoint.
It is intentionally not part of the default transparent gateway: a profile
version ``0.1.0-gateway5231.3`` is the sole opt-in.  The consumer request is
non-streaming and unchanged; only the upstream transport body uses
``stream=true``.  Provider response text is kept in memory and never logged
or persisted by this module.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import wraps
from typing import Any

import httpx

from pvian_gateway.forwarder import ForwardedResponse, TransparentForwarder
from pvian_gateway.profiles import ResolvedEndpoint

KIMI_PROFILE_ID = "provider-bailian-cn-beijing-kimi-k3"
KIMI_PROFILE_VERSION = "0.1.0-gateway5231.3"
AGGREGATION_MODE = "sse_to_nonstream_v1"


class KimiSSEAggregationError(httpx.HTTPError):
    """The upstream SSE stream cannot be represented as one safe response."""


def is_opted_in(endpoint: ResolvedEndpoint, body: bytes) -> bool:
    if (
        endpoint.profile.profile_id != KIMI_PROFILE_ID
        or endpoint.profile.profile_version != KIMI_PROFILE_VERSION
    ):
        return False
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("stream") is False


def install() -> None:
    """Install the adapter into the process-local forwarder class once."""

    if getattr(TransparentForwarder.forward, "_kimi_sse_adapter", False):
        return
    original = TransparentForwarder.forward

    @wraps(original)
    async def wrapped(
        self: TransparentForwarder,
        endpoint: ResolvedEndpoint,
        credential: str,
        body: bytes,
        inbound_headers: dict[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> ForwardedResponse:
        if is_opted_in(endpoint, body):
            return await _forward_aggregated(
                self,
                endpoint,
                credential,
                body,
                inbound_headers,
                timeout_seconds=timeout_seconds,
            )
        return await original(
            self,
            endpoint,
            credential,
            body,
            inbound_headers,
            timeout_seconds=timeout_seconds,
        )

    wrapped._kimi_sse_adapter = True  # type: ignore[attr-defined]
    TransparentForwarder.forward = wrapped  # type: ignore[method-assign]


async def _forward_aggregated(
    forwarder: TransparentForwarder,
    endpoint: ResolvedEndpoint,
    credential: str,
    body: bytes,
    inbound_headers: dict[str, str],
    *,
    timeout_seconds: float | None,
) -> ForwardedResponse:
    request_value = json.loads(body)
    upstream_value = dict(request_value)
    upstream_value["stream"] = True
    upstream_body = json.dumps(upstream_value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    headers = {
        "authorization": f"Bearer {credential}",
        "content-type": inbound_headers.get("content-type", "application/json"),
        "accept": "text/event-stream",
        "accept-encoding": "identity",
    }
    request = forwarder.client.build_request(
        "POST",
        endpoint.chat_url(),
        content=upstream_body,
        headers=headers,
        timeout=timeout_seconds,
    )
    response = await forwarder.client.send(request, stream=True)
    safe_headers = _safe_upstream_headers(response.headers)
    if not 200 <= response.status_code < 300:
        return ForwardedResponse(
            status_code=response.status_code,
            headers=safe_headers,
            body=_response_body(response),
            close=response.aclose,
        )
    try:
        aggregate, termination = await _aggregate_sse(response)
    except Exception:
        await response.aclose()
        raise
    await response.aclose()
    safe_headers["content-type"] = "application/json"
    safe_headers["x-pvian-sse-termination"] = termination
    return ForwardedResponse(
        status_code=response.status_code,
        headers=safe_headers,
        body=_one_body(
            json.dumps(aggregate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ),
        close=_closed,
    )


async def _aggregate_sse(response: httpx.Response) -> tuple[dict[str, Any], str]:
    values: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    model: str | None = None
    created: int | None = None
    data_lines: list[str] = []
    done = False

    async def consume(data: str) -> None:
        nonlocal usage, response_id, model, created, done
        if data == "[DONE]":
            done = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise KimiSSEAggregationError("malformed_sse_json") from exc
        if not isinstance(chunk, dict):
            raise KimiSSEAggregationError("sse_chunk_not_object")
        response_id = response_id or _optional_string(chunk.get("id"))
        model = model or _optional_string(chunk.get("model"))
        if created is None and isinstance(chunk.get("created"), int):
            created = chunk["created"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                raise KimiSSEAggregationError("sse_choice_not_object")
            index = choice.get("index", 0)
            if not isinstance(index, int) or index < 0:
                raise KimiSSEAggregationError("sse_choice_index_invalid")
            item = values.setdefault(
                index,
                {
                    "index": index,
                    "role": None,
                    "content": [],
                    "reasoning_content": [],
                    "refusal": [],
                    "tool_calls": {},
                    "finish_reason": None,
                },
            )
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            for field in ("content", "reasoning_content", "reasoning", "refusal"):
                value = delta.get(field)
                if isinstance(value, str):
                    target = "reasoning_content" if field == "reasoning" else field
                    item[target].append(value)
            if isinstance(delta.get("role"), str):
                item["role"] = delta["role"]
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    _merge_tool_call(item["tool_calls"], call)
            if choice.get("finish_reason") is not None:
                item["finish_reason"] = choice["finish_reason"]

    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                await consume("\n".join(data_lines))
                data_lines = []
            if done:
                break
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        await consume("\n".join(data_lines))
    # Bailian may close cleanly after a final usage event without a literal
    # [DONE] marker. A complete usage-bearing EOF is safely aggregatable.
    if not values:
        raise KimiSSEAggregationError("sse_stream_not_terminated")
    if not done and (
        usage is None or any(item["finish_reason"] is None for item in values.values())
    ):
        raise KimiSSEAggregationError("sse_stream_not_terminated")
    if response_id is None or model is None or usage is None:
        raise KimiSSEAggregationError("sse_required_metadata_missing")

    choices: list[dict[str, Any]] = []
    for index in sorted(values):
        item = values[index]
        finish = item["finish_reason"]
        if not isinstance(finish, str) or not finish:
            raise KimiSSEAggregationError("sse_finish_reason_missing")
        message: dict[str, Any] = {
            "role": item["role"] or "assistant",
            "content": "".join(item["content"]) if item["content"] else None,
        }
        if item["reasoning_content"]:
            message["reasoning_content"] = "".join(item["reasoning_content"])
        if item["refusal"]:
            message["refusal"] = "".join(item["refusal"])
        if item["tool_calls"]:
            message["tool_calls"] = [item["tool_calls"][key] for key in sorted(item["tool_calls"])]
        choices.append({"index": index, "message": message, "finish_reason": finish})
    result: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "model": model,
        "choices": choices,
        "usage": usage,
    }
    if created is not None:
        result["created"] = created
    return result, "done" if done else "eof"


def _merge_tool_call(target: dict[int, dict[str, Any]], call: object) -> None:
    if not isinstance(call, dict):
        raise KimiSSEAggregationError("sse_tool_call_not_object")
    index = call.get("index", 0)
    if not isinstance(index, int) or index < 0:
        raise KimiSSEAggregationError("sse_tool_call_index_invalid")
    item = target.setdefault(
        index,
        {
            "id": None,
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if isinstance(call.get("id"), str):
        item["id"] = item["id"] or call["id"]
    if isinstance(call.get("type"), str):
        item["type"] = call["type"]
    function = call.get("function")
    if isinstance(function, dict):
        if isinstance(function.get("name"), str):
            item["function"]["name"] += function["name"]
        if isinstance(function.get("arguments"), str):
            item["function"]["arguments"] += function["arguments"]


def _safe_upstream_headers(headers: Any) -> dict[str, str]:
    allowed = {
        "x-request-id",
        "request-id",
        "x-dashscope-request-id",
        "x-acs-request-id",
        "openai-processing-ms",
    }
    return {name: value for name, value in headers.items() if name.lower() in allowed}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def _one_body(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


async def _closed() -> None:
    return None
