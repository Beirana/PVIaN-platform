from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from pvian_gateway.profiles import ResolvedEndpoint


@dataclass
class ForwardedResponse:
    status_code: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    close: Any


class TransparentForwarder:
    """Forwards a body unchanged and never logs request or response content."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def forward(
        self,
        endpoint: ResolvedEndpoint,
        credential: str,
        body: bytes,
        inbound_headers: dict[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> ForwardedResponse:
        headers = {
            "authorization": f"Bearer {credential}",
            "content-type": inbound_headers.get("content-type", "application/json"),
            "accept": inbound_headers.get("accept", "application/json"),
            # We stream raw upstream bytes.  Request identity encoding so a
            # normal httpx/OpenAI consumer never receives compressed bytes
            # without a matching response content-encoding declaration.
            "accept-encoding": "identity",
        }
        return await self._dispatch(
            endpoint.chat_url(), headers, body, timeout_seconds=timeout_seconds
        )

    async def forward_messages(
        self,
        url: str,
        credential: str,
        body: bytes,
        inbound_headers: dict[str, str],
        *,
        timeout_seconds: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> ForwardedResponse:
        """Transparent forwarding for an Anthropic Messages surface URL.

        Same four-header contract as forward(); ``extra_headers`` carries the
        protocol-mandated Anthropic headers (anthropic-version, user-agent).
        No body content is inspected or rewritten here.
        """
        headers = {
            "authorization": f"Bearer {credential}",
            "content-type": inbound_headers.get("content-type", "application/json"),
            "accept": inbound_headers.get("accept", "application/json"),
            "accept-encoding": "identity",
        }
        if extra_headers:
            headers.update({name.lower(): value for name, value in extra_headers.items()})
        return await self._dispatch(url, headers, body, timeout_seconds=timeout_seconds)

    async def _dispatch(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        *,
        timeout_seconds: float | None = None,
    ) -> ForwardedResponse:
        request = self.client.build_request(
            "POST",
            url,
            content=body,
            headers=headers,
            timeout=timeout_seconds,
        )
        response = await self.client.send(request, stream=True)
        safe_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower()
            in {
                "content-type",
                # Defensive: an upstream may ignore identity.  If it does,
                # raw bytes remain decodable by the downstream HTTP client.
                "content-encoding",
                "cache-control",
                "retry-after",
                "x-request-id",
                "request-id",
                "x-dashscope-request-id",
                "x-acs-request-id",
                "openai-processing-ms",
            }
        }
        return ForwardedResponse(
            status_code=response.status_code,
            headers=safe_headers,
            body=_response_body(response),
            close=response.aclose,
        )


def response_usage(headers: dict[str, str]) -> dict[str, int | None] | None:
    """Usage normally lives in JSON/SSE bodies, which must not be parsed or retained.

    The transparent path intentionally returns no usage accounting from headers.
    Qualification observations can record safely extracted usage summaries.
    """

    del headers
    return None


async def _response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    # MockTransport may construct a response from a complete bytes body, which
    # httpx marks consumed before the gateway can iterate it.  Production
    # ``send(..., stream=True)`` remains raw-streamed.
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk
