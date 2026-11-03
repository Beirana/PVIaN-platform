from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pvi.config import PROJECT_ROOT
from pvian_gateway import __version__
from pvian_gateway.accounting import (
    AnthropicUsageCollector,
    UsageCollector,
    build_pricing_snapshot,
    estimate,
    load_pricing,
    load_token_limits,
    region_for_profile,
    select_quote,
)
from pvian_gateway.aliases import AliasRegistry, alias_matches_record
from pvian_gateway.auth import ClientAuthenticationError, GatewayClientAuthenticator
from pvian_gateway.config import GatewaySettings
from pvian_gateway.forwarder import ForwardedResponse, TransparentForwarder
from pvian_gateway.profiles import (
    ANTHROPIC_TRANSPORT,
    ProfileError,
    ResolvedEndpoint,
    load_profiles,
)
from pvian_gateway.state import (
    QualificationStateStore,
    UsageAttemptLedger,
    reported_model_matches_state,
)

# Protocol-mandated headers for the Anthropic Messages surface. The UA mirrors
# claude-cli because the station WAF was observed (2026-08-19) rejecting a
# generic python UA with 502 on /v1/messages while accepting this shape.
_ANTHROPIC_UPSTREAM_HEADERS = {
    "anthropic-version": "2023-06-01",
    "user-agent": "pvian-gateway/anthropic-passthrough claude-cli/1.0",
}


def create_app(
    settings: GatewaySettings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    configured = (settings or GatewaySettings()).resolved()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.profiles = load_profiles(configured.profile_dir)
        app.state.authenticator = GatewayClientAuthenticator.from_file(
            configured.client_key_registry_file
        )
        app.state.aliases = AliasRegistry.from_file(configured.alias_registry_file)
        app.state.aliases.validate_bindings(app.state.profiles)
        app.state.store = QualificationStateStore(configured.state_file)
        app.state.usage_ledger = UsageAttemptLedger(configured.state_file)
        app.state.pricing_dir = PROJECT_ROOT / "config" / "pricing"
        app.state.pricing = load_pricing(app.state.pricing_dir)
        app.state.pricing_mtime = _pricing_dir_mtime(app.state.pricing_dir)
        app.state.token_limits_file = PROJECT_ROOT / "config" / "model-token-limits.json"
        app.state.token_limits = _load_token_limits_safe(app.state.token_limits_file)
        app.state.token_limits_mtime = _file_mtime(app.state.token_limits_file)
        app.state.store.validate_profile_bindings(app.state.profiles)
        app.state.client = client or httpx.AsyncClient(
            timeout=configured.upstream_timeout_seconds,
            follow_redirects=False,
        )
        app.state.owns_client = client is None
        app.state.forwarder = TransparentForwarder(app.state.client)
        try:
            yield
        finally:
            if app.state.owns_client:
                await app.state.client.aclose()

    app = FastAPI(
        title="PVIaN Transparent Gateway",
        version=__version__,
        openapi_version="3.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def require_gateway_client_auth(request: Request, call_next: Any) -> Any:
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        try:
            request.state.gateway_client_key_id = request.app.state.authenticator.authenticate(
                request.headers.get("authorization")
            )
        except ClientAuthenticationError as exc:
            headers = {"www-authenticate": "Bearer"} if exc.status_code == 401 else None
            return _gateway_error(exc.status_code, exc.code, exc.message, headers=headers)
        return await call_next(request)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> JSONResponse:
        if request.app.state.authenticator.ready:
            return JSONResponse(status_code=200, content={"status": "ready"})
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.get("/v1/gateway/routes")
    def list_routes(request: Request) -> dict[str, Any]:
        return {"items": [_public_record(item) for item in request.app.state.store.list_records()]}

    @app.get("/v1/gateway/routes/{route_id:path}")
    def get_route(route_id: str, request: Request) -> dict[str, Any]:
        record = request.app.state.store.get(route_id)
        if record is None:
            raise HTTPException(status_code=404, detail="route_not_found")
        return _public_record(record)

    @app.get("/v1/gateway/capabilities")
    def list_capabilities(request: Request) -> dict[str, Any]:
        return {
            "items": [
                {
                    "route_id": item["route_id"],
                    "requested_model": item["requested_model"],
                    "qualification_status": item["qualification_status"],
                    "operational_status": item["operational_status"],
                    "qualified_at": item["qualified_at"],
                    "valid_until": item["valid_until"],
                    "capabilities": item["capabilities"],
                }
                for item in request.app.state.store.list_records()
            ]
        }

    @app.get("/v1/gateway/pricing/snapshots")
    def list_pricing_snapshots(
        request: Request,
        model_alias: str | None = None,
        provider: str | None = None,
        region: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        _refresh_pricing_if_changed(request)
        quotes = request.app.state.pricing
        aliases = request.app.state.aliases.aliases

        # Resolve aliases that bind to each quote via (profile_id, upstream_model).
        alias_index: dict[tuple[str, str], list[Any]] = {}
        for alias in aliases:
            key = (alias.profile_id, alias.upstream_model)
            alias_index.setdefault(key, []).append(alias)

        # model_alias filter: narrow to the exact alias (404 if unknown).
        pinned_alias = None
        if model_alias is not None:
            pinned_alias = request.app.state.aliases.get(model_alias)
            if pinned_alias is None:
                return _gateway_error(404, "alias_not_found", "Unknown model alias.")

        items: list[dict[str, Any]] = []
        token_limits = request.app.state.token_limits
        for quote in quotes:
            if provider is not None and quote.provider != provider:
                continue
            if region is not None and quote.region != region:
                continue
            if model is not None and quote.model != model:
                continue

            bound = alias_index.get((quote.profile_id, quote.model), [])
            if pinned_alias is not None:
                bound = [a for a in bound if a.alias == pinned_alias.alias]
                if not bound:
                    continue

            if bound:
                for alias in bound:
                    items.append(
                        build_pricing_snapshot(
                            quote,
                            public_alias=alias.alias,
                            route_id=alias.route_id,
                            upstream_model=alias.upstream_model,
                            limits=_lookup_limits(token_limits, quote, alias.upstream_model),
                        )
                    )
            else:
                # Pricing exists but no public alias binds to it yet.
                items.append(
                    build_pricing_snapshot(
                        quote,
                        public_alias=None,
                        route_id=None,
                        upstream_model=quote.model,
                        limits=_lookup_limits(token_limits, quote, quote.model),
                    )
                )
        return {"items": items}

    @app.get("/v1/models")
    def list_models(request: Request) -> dict[str, Any]:
        models = []
        for alias in request.app.state.aliases.aliases:
            record = request.app.state.store.callable_record(alias.route_id)
            if alias_matches_record(alias, record) and _is_currently_callable(
                request.app.state.profiles, record
            ):
                models.append(alias.as_openai_model(record))
        return {"object": "list", "data": models}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        route_id: str | None = Header(default=None, alias="x-pvian-route-id"),
    ) -> StreamingResponse:
        request_id = uuid.uuid4().hex
        trace_headers = {"x-pvian-request-id": request_id}
        logical_call_id = request.headers.get("x-pvian-logical-call-id")
        if logical_call_id is not None:
            if not _is_safe_logical_call_id(logical_call_id):
                return _gateway_error(
                    422,
                    "invalid_logical_call_id",
                    "Logical call ID must be 1-128 ASCII correlation characters.",
                    headers=trace_headers,
                )
            trace_headers["x-pvian-logical-call-id"] = logical_call_id
        body = await request.body()
        request_value = _request_object(body)
        model = request_value["model"]
        public_alias: str | None = None
        if route_id is not None:
            record = request.app.state.store.callable_record(route_id)
            if record is None:
                return _gateway_error(
                    409,
                    "route_not_callable",
                    "Route is unqualified, expired, or awaiting revalidation.",
                    headers=trace_headers,
                )
            if model != record["requested_model"]:
                return _gateway_error(
                    422,
                    "model_route_mismatch",
                    "Request model does not match the qualified route.",
                    headers=trace_headers,
                )
        else:
            alias = request.app.state.aliases.get(model)
            if alias is None:
                ambiguous = request.app.state.aliases.aliases_for_upstream_model(model)
                if len(ambiguous) > 1:
                    return _gateway_error(
                        409,
                        "model_is_ambiguous",
                        "Exact upstream model IDs require a public PVIaN model alias.",
                        headers=trace_headers,
                    )
                return _gateway_error(
                    404,
                    "model_not_found",
                    "Public model alias was not found.",
                    headers=trace_headers,
                )
            record = request.app.state.store.callable_record(alias.route_id)
            if not alias_matches_record(alias, record):
                return _gateway_error(
                    409,
                    "alias_not_callable",
                    "Public model alias is unqualified, expired, or has drifted from its profile.",
                    headers=trace_headers,
                )
            body = _alias_facade_body(request_value, alias.upstream_model)
            route_id = alias.route_id
            public_alias = alias.alias
        try:
            endpoint, credential = _resolve_callable_endpoint(request.app.state.profiles, record)
        except ProfileError:
            return _gateway_error(
                409,
                "alias_not_callable" if public_alias is not None else "route_not_callable",
                "Route profile, endpoint, or local credential is not currently callable.",
                headers=trace_headers,
            )
        attempt_started_at = time.monotonic()
        try:
            forwarded = await request.app.state.forwarder.forward(
                endpoint,
                credential,
                body,
                _upstream_headers(request.headers),
                timeout_seconds=(
                    endpoint.endpoint.upstream_timeout_seconds
                    or configured.upstream_timeout_seconds
                ),
            )
        except httpx.HTTPError:
            request.app.state.store.record_failure(route_id, "transport_error")
            request.app.state.usage_ledger.record(
                _usage_attempt(
                    request_id,
                    logical_call_id,
                    route_id,
                    record,
                    endpoint,
                    "transport_error",
                    error_category="transport_error",
                    attempt_started_at=attempt_started_at,
                )
            )
            return _gateway_error(
                502,
                "upstream_transport_error",
                "Upstream transport failed.",
                headers=trace_headers,
            )
        category = _failure_category(forwarded.status_code)
        if category is not None:
            request.app.state.store.record_failure(route_id, category)
            request.app.state.usage_ledger.record(
                _usage_attempt(
                    request_id,
                    logical_call_id,
                    route_id,
                    record,
                    endpoint,
                    "upstream_error",
                    upstream_status_code=forwarded.status_code,
                    upstream_request_id_sha256=_hashed_upstream_request_id(forwarded.headers),
                    error_category=category,
                    attempt_started_at=attempt_started_at,
                )
            )
        headers = {
            name: value
            for name, value in forwarded.headers.items()
            if not name.lower().startswith("x-pvian-")
        }

        headers.update(trace_headers)
        headers.update(
            {
                "x-pvian-route-id": route_id,
                "x-pvian-profile-id": endpoint.profile.profile_id,
                "x-pvian-profile-version": endpoint.profile.profile_version,
                "x-pvian-endpoint-id": endpoint.endpoint.endpoint_id,
                "x-pvian-provider-id": endpoint.profile.provider_id,
                "x-pvian-upstream-model": str(record["requested_model"]),
                "x-pvian-cost": "unknown",
            }
        )
        if public_alias is not None:
            headers["x-pvian-public-model"] = public_alias
            headers["x-pvian-alias-facade"] = "semantic-model-mapping/v1"
        provider = _canonical_provider(endpoint.profile.provider_id)
        model_id = str(record["requested_model"])
        region = region_for_profile(
            request.app.state.pricing, provider, endpoint.profile.profile_id, model_id
        )
        quote = (
            select_quote(request.app.state.pricing, provider, region, model_id)
            if region is not None
            else None
        )
        collector = UsageCollector(forwarded.headers.get("content-type"))
        store = request.app.state.store
        ledger = request.app.state.usage_ledger
        return StreamingResponse(
            _body_then_close(
                forwarded,
                collector,
                (
                    lambda exhausted: (
                        _on_success_body_complete(
                            store,
                            ledger,
                            request_id,
                            logical_call_id,
                            route_id,
                            record,
                            endpoint,
                            exhausted,
                            collector,
                            quote,
                            bool(request_value.get("stream")),
                            forwarded.status_code,
                            _hashed_upstream_request_id(forwarded.headers),
                            attempt_started_at,
                        )
                        if category is None
                        else None
                    )
                ),
            ),
            status_code=forwarded.status_code,
            headers=headers,
            media_type=forwarded.headers.get("content-type"),
        )

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> StreamingResponse:
        """Transparent Anthropic Messages passthrough (ADR 0004).

        Alias-only resolution against anthropic_native_messages endpoints: the
        body is forwarded byte-preserved except the single model facade
        rewrite, SSE streams pass through untouched, and the same
        client-key auth / state gating / ledger discipline as the OpenAI
        surface applies. No protocol translation happens here.
        """
        request_id = uuid.uuid4().hex
        trace_headers = {"x-pvian-request-id": request_id}
        logical_call_id = request.headers.get("x-pvian-logical-call-id")
        if logical_call_id is not None:
            if not _is_safe_logical_call_id(logical_call_id):
                return _gateway_error(
                    422,
                    "invalid_logical_call_id",
                    "Logical call ID must be 1-128 ASCII correlation characters.",
                    headers=trace_headers,
                )
            trace_headers["x-pvian-logical-call-id"] = logical_call_id
        body = await request.body()
        request_value = _request_object(body)
        model = request_value["model"]
        alias = request.app.state.aliases.get(model)
        if alias is None:
            ambiguous = request.app.state.aliases.aliases_for_upstream_model(model)
            if len(ambiguous) > 1:
                return _gateway_error(
                    409,
                    "model_is_ambiguous",
                    "Exact upstream model IDs require a public PVIaN model alias.",
                    headers=trace_headers,
                )
            return _gateway_error(
                404,
                "model_not_found",
                "Public model alias was not found.",
                headers=trace_headers,
            )
        record = request.app.state.store.callable_record(alias.route_id)
        if not alias_matches_record(alias, record):
            return _gateway_error(
                409,
                "alias_not_callable",
                "Public model alias is unqualified, expired, or has drifted from its profile.",
                headers=trace_headers,
            )
        body = _alias_facade_body(request_value, alias.upstream_model)
        route_id = alias.route_id
        try:
            endpoint, credential = _resolve_callable_endpoint(
                request.app.state.profiles, record, transport=ANTHROPIC_TRANSPORT
            )
        except ProfileError:
            return _gateway_error(
                409,
                "alias_not_callable",
                "Alias is not bound to an Anthropic-native endpoint or is not currently callable.",
                headers=trace_headers,
            )
        attempt_started_at = time.monotonic()
        try:
            forwarded = await request.app.state.forwarder.forward_messages(
                endpoint.messages_url(),
                credential,
                body,
                _upstream_headers(request.headers),
                timeout_seconds=(
                    endpoint.endpoint.upstream_timeout_seconds
                    or configured.upstream_timeout_seconds
                ),
                extra_headers=_ANTHROPIC_UPSTREAM_HEADERS,
            )
        except httpx.HTTPError:
            request.app.state.store.record_failure(route_id, "transport_error")
            request.app.state.usage_ledger.record(
                _usage_attempt(
                    request_id,
                    logical_call_id,
                    route_id,
                    record,
                    endpoint,
                    "transport_error",
                    error_category="transport_error",
                    attempt_started_at=attempt_started_at,
                )
            )
            return _gateway_error(
                502,
                "upstream_transport_error",
                "Upstream transport failed.",
                headers=trace_headers,
            )
        category = _failure_category(forwarded.status_code)
        if category is not None:
            request.app.state.store.record_failure(route_id, category)
            request.app.state.usage_ledger.record(
                _usage_attempt(
                    request_id,
                    logical_call_id,
                    route_id,
                    record,
                    endpoint,
                    "upstream_error",
                    upstream_status_code=forwarded.status_code,
                    upstream_request_id_sha256=_hashed_upstream_request_id(forwarded.headers),
                    error_category=category,
                    attempt_started_at=attempt_started_at,
                )
            )
        headers = {
            name: value
            for name, value in forwarded.headers.items()
            if not name.lower().startswith("x-pvian-")
        }
        headers.update(trace_headers)
        headers.update(
            {
                "x-pvian-route-id": route_id,
                "x-pvian-profile-id": endpoint.profile.profile_id,
                "x-pvian-profile-version": endpoint.profile.profile_version,
                "x-pvian-endpoint-id": endpoint.endpoint.endpoint_id,
                "x-pvian-provider-id": endpoint.profile.provider_id,
                "x-pvian-upstream-model": str(record["requested_model"]),
                "x-pvian-api-surface": "anthropic_native_messages",
                "x-pvian-cost": "unknown",
                "x-pvian-public-model": alias.alias,
                "x-pvian-alias-facade": "semantic-model-mapping/v1",
            }
        )
        provider = _canonical_provider(endpoint.profile.provider_id)
        model_id = str(record["requested_model"])
        region = region_for_profile(
            request.app.state.pricing, provider, endpoint.profile.profile_id, model_id
        )
        quote = (
            select_quote(request.app.state.pricing, provider, region, model_id)
            if region is not None
            else None
        )
        collector = AnthropicUsageCollector(forwarded.headers.get("content-type"))
        store = request.app.state.store
        ledger = request.app.state.usage_ledger
        return StreamingResponse(
            _body_then_close(
                forwarded,
                collector,
                (
                    lambda exhausted: (
                        _on_success_body_complete(
                            store,
                            ledger,
                            request_id,
                            logical_call_id,
                            route_id,
                            record,
                            endpoint,
                            exhausted,
                            collector,
                            quote,
                            bool(request_value.get("stream")),
                            forwarded.status_code,
                            _hashed_upstream_request_id(forwarded.headers),
                            attempt_started_at,
                        )
                        if category is None
                        else None
                    )
                ),
            ),
            status_code=forwarded.status_code,
            headers=headers,
            media_type=forwarded.headers.get("content-type"),
        )

    @app.post("/v1/messages/count_tokens")
    async def anthropic_count_tokens(request: Request) -> StreamingResponse:
        """Pure passthrough for the Anthropic count_tokens helper endpoint.

        Same alias/state gating as /v1/messages; responses carry no message
        usage, so the ledger attempt records the call without token totals.
        """
        request_id = uuid.uuid4().hex
        trace_headers = {"x-pvian-request-id": request_id}
        body = await request.body()
        request_value = _request_object(body)
        model = request_value["model"]
        alias = request.app.state.aliases.get(model)
        if alias is None:
            return _gateway_error(
                404,
                "model_not_found",
                "Public model alias was not found.",
                headers=trace_headers,
            )
        record = request.app.state.store.callable_record(alias.route_id)
        if not alias_matches_record(alias, record):
            return _gateway_error(
                409,
                "alias_not_callable",
                "Public model alias is unqualified, expired, or has drifted from its profile.",
                headers=trace_headers,
            )
        body = _alias_facade_body(request_value, alias.upstream_model)
        route_id = alias.route_id
        try:
            endpoint, credential = _resolve_callable_endpoint(
                request.app.state.profiles, record, transport=ANTHROPIC_TRANSPORT
            )
        except ProfileError:
            return _gateway_error(
                409,
                "alias_not_callable",
                "Alias is not bound to an Anthropic-native endpoint or is not currently callable.",
                headers=trace_headers,
            )
        attempt_started_at = time.monotonic()
        try:
            forwarded = await request.app.state.forwarder.forward_messages(
                f"{endpoint.messages_url()}/count_tokens",
                credential,
                body,
                _upstream_headers(request.headers),
                timeout_seconds=(
                    endpoint.endpoint.upstream_timeout_seconds
                    or configured.upstream_timeout_seconds
                ),
                extra_headers=_ANTHROPIC_UPSTREAM_HEADERS,
            )
        except httpx.HTTPError:
            request.app.state.store.record_failure(route_id, "transport_error")
            return _gateway_error(
                502,
                "upstream_transport_error",
                "Upstream transport failed.",
                headers=trace_headers,
            )
        category = _failure_category(forwarded.status_code)
        if category is not None:
            request.app.state.store.record_failure(route_id, category)
            request.app.state.usage_ledger.record(
                _usage_attempt(
                    request_id,
                    None,
                    route_id,
                    record,
                    endpoint,
                    "upstream_error",
                    upstream_status_code=forwarded.status_code,
                    upstream_request_id_sha256=_hashed_upstream_request_id(forwarded.headers),
                    error_category=category,
                    attempt_started_at=attempt_started_at,
                )
            )
        headers = {
            name: value
            for name, value in forwarded.headers.items()
            if not name.lower().startswith("x-pvian-")
        }
        headers.update(trace_headers)
        headers.update(
            {
                "x-pvian-route-id": route_id,
                "x-pvian-api-surface": "anthropic_native_messages",
            }
        )
        collector = AnthropicUsageCollector(None)
        store = request.app.state.store
        ledger = request.app.state.usage_ledger
        return StreamingResponse(
            _body_then_close(
                forwarded,
                collector,
                (
                    lambda exhausted: (
                        _on_success_body_complete(
                            store,
                            ledger,
                            request_id,
                            None,
                            route_id,
                            record,
                            endpoint,
                            exhausted,
                            collector,
                            None,
                            False,
                            forwarded.status_code,
                            _hashed_upstream_request_id(forwarded.headers),
                            attempt_started_at,
                        )
                        if category is None
                        else None
                    )
                ),
            ),
            status_code=forwarded.status_code,
            headers=headers,
            media_type=forwarded.headers.get("content-type"),
        )

    return app


async def _body_then_close(
    response: ForwardedResponse, collector: UsageCollector, complete: Any
) -> AsyncIterator[bytes]:
    exhausted = False
    try:
        async for chunk in response.body:
            collector.feed(chunk)
            yield chunk
        exhausted = True
    finally:
        await response.close()
        if complete is not None:
            if exhausted:
                collector.final_usage()
            complete(exhausted)


def _on_success_body_complete(
    store: QualificationStateStore,
    ledger: UsageAttemptLedger,
    request_id: str,
    logical_call_id: str | None,
    route_id: str,
    record: dict[str, Any],
    endpoint: ResolvedEndpoint,
    exhausted: bool,
    collector: UsageCollector,
    quote: Any,
    stream: bool,
    upstream_status_code: int,
    upstream_request_id_sha256: str | None,
    attempt_started_at: float,
) -> None:
    """D: After body consumption, record success and aggregate usage.

    Called only for 2xx responses.  The ledger keeps the authoritative
    per-attempt record; the state aggregate is updated only when the
    collector produced a valid normalized usage summary (non-streaming
    body fully consumed, or SSE stream fully consumed with ``[DONE]``).
    Interrupted SSE, missing usage, or malformed usage yield ``None``;
    ``request_count`` and success timestamps are still updated, but no
    token totals are aggregated, avoiding the ``request_count>0, token=0``
    misinterpretation.
    """

    usage_summary = collector.final_usage() if exhausted and collector.complete else None
    store.record_success(
        route_id,
        usage_summary.as_dict() if usage_summary is not None else None,
    )
    ledger.record(
        _usage_attempt(
            request_id,
            logical_call_id,
            route_id,
            record,
            endpoint,
            "completed" if exhausted and collector.complete else "stream_interrupted",
            usage_summary,
            quote,
            stream,
            collector.reported_model,
            upstream_status_code=upstream_status_code,
            upstream_request_id_sha256=upstream_request_id_sha256,
            finish_reason=collector.finish_reason,
            error_category=(None if exhausted and collector.complete else "upstream_error"),
            attempt_started_at=attempt_started_at,
        )
    )


def _canonical_provider(provider_id: str) -> str:
    return provider_id.removeprefix("provider-")


def _usage_attempt(
    request_id: str,
    logical_call_id: str | None,
    route_id: str,
    record: dict[str, Any],
    endpoint: ResolvedEndpoint,
    status: str,
    usage: Any = None,
    quote: Any = None,
    stream: bool = False,
    reported_model: str | None = None,
    *,
    upstream_status_code: int | None = None,
    upstream_request_id_sha256: str | None = None,
    finish_reason: str | None = None,
    error_category: str | None = None,
    attempt_started_at: float | None = None,
) -> dict[str, Any]:
    # Backward-compatible helper default for existing offline callers.  The
    # live gateway always passes the observed upstream status explicitly.
    if upstream_status_code is None and status in {"completed", "stream_interrupted"}:
        upstream_status_code = 200
    requested_model = str(record["requested_model"])
    model_mismatch = bool(
        reported_model and not reported_model_matches_state(record, reported_model)
    )
    cost = (
        {"status": "unknown", "reason": "reported_model_mismatch"}
        if model_mismatch
        else estimate(usage, quote)
    )
    return {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "request_id": request_id,
        "logical_call_id": logical_call_id,
        "route_id": route_id,
        "provider": _canonical_provider(record["provider_id"]),
        "region": quote.region if quote is not None else "unknown",
        "requested_model": requested_model,
        "reported_model": reported_model,
        "status": status,
        "stream": stream,
        "usage": usage.as_dict() if usage is not None else None,
        "cost_status": cost["status"],
        "estimated_list_cost_cny": cost.get("amount") if cost.get("currency") == "CNY" else None,
        "pricing_version": (
            quote.pricing_version if quote is not None and not model_mismatch else None
        ),
        "estimate": cost,
        "attempt_count": 1,
        "upstream_request_id_sha256": upstream_request_id_sha256,
        "upstream_status_code": upstream_status_code,
        "finish_reason": finish_reason,
        "error_category": error_category,
        "elapsed_ms": (
            max(0, round((time.monotonic() - attempt_started_at) * 1000))
            if attempt_started_at is not None
            else None
        ),
    }


def _hashed_upstream_request_id(headers: dict[str, str]) -> str | None:
    accepted = {"x-request-id", "request-id", "x-dashscope-request-id", "x-acs-request-id"}
    matched = next(
        ((name.lower(), item) for name, item in headers.items() if name.lower() in accepted),
        None,
    )
    if matched is None:
        return None
    name, value = matched
    if not isinstance(value, str) or not value or len(value) > 1024:
        return None
    return "sha256:" + hashlib.sha256(f"{name}:{value}".encode()).hexdigest()


def _request_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid_json_body") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid_json_body")
    model = value.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=422, detail="model_is_required")
    return value


def _alias_facade_body(request_value: dict[str, Any], upstream_model: str) -> bytes:
    """Make the only allowed public-alias mutation explicit and reproducible.

    This intentionally reserializes JSON, so the facade promises field-level
    semantic preservation rather than request-byte preservation.  The explicit
    route diagnostic path remains byte-transparent.
    """

    rewritten = dict(request_value)
    rewritten["model"] = upstream_model
    return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _resolve_record_endpoint(
    profiles: dict[str, Any],
    record: dict[str, Any],
    *,
    transport: str = "openai_compatible_chat_completions",
) -> ResolvedEndpoint:
    profile = profiles.get(record["profile_id"])
    if profile is None or profile.profile_version != record["profile_version"]:
        raise ProfileError("qualified profile is missing or version changed")
    if profile.lifecycle != "active":
        raise ProfileError("qualified profile lifecycle is not active")
    endpoint = next(
        (item for item in profile.endpoints if item.endpoint_id == record["endpoint_id"]), None
    )
    if endpoint is None or endpoint.transport != transport:
        raise ProfileError("qualified endpoint is unavailable for transparent forwarding")
    requested_model = record.get("requested_model")
    if not isinstance(requested_model, str) or not profile.declares_upstream_model(requested_model):
        raise ProfileError("qualified model is not declared by the frozen profile")
    if record.get("route_id") != f"{endpoint.route_id_prefix}:{requested_model}":
        raise ProfileError("qualified route does not match its exact model target")
    return profile.resolve_endpoint(endpoint)


def _resolve_callable_endpoint(
    profiles: dict[str, Any],
    record: dict[str, Any],
    *,
    transport: str = "openai_compatible_chat_completions",
) -> tuple[ResolvedEndpoint, str]:
    endpoint = _resolve_record_endpoint(profiles, record, transport=transport)
    return endpoint, endpoint.profile.credential()


def _is_currently_callable(profiles: dict[str, Any], record: dict[str, Any]) -> bool:
    for transport in ("openai_compatible_chat_completions", ANTHROPIC_TRANSPORT):
        try:
            _resolve_callable_endpoint(profiles, record, transport=transport)
        except ProfileError:
            continue
        return True
    return False


def _failure_category(status_code: int) -> str | None:
    if 200 <= status_code < 300:
        return None
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code < 600:
        return "upstream_5xx"
    if status_code in (408, 504):
        return "timeout"
    if 400 <= status_code < 500:
        return "upstream_protocol_or_auth"
    return "upstream_error"


def _is_safe_logical_call_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is not None


def _upstream_headers(headers: Any) -> dict[str, str]:
    """Strip gateway-only provenance before transparent upstream forwarding."""

    return {
        name.lower(): value
        for name, value in headers.items()
        if not name.lower().startswith("x-pvian-") and name.lower() != "x-request-id"
    }


def _gateway_error(
    status: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key
        not in {
            "last_failure_category",
        }
    }


def _pricing_dir_mtime(directory: Path) -> float:
    """Return the newest mtime among pricing ``*.json`` files (0 if none)."""

    try:
        return max(path.stat().st_mtime for path in directory.glob("*.json"))
    except (OSError, ValueError):
        return 0.0


def _file_mtime(path: Path) -> float:
    """Return a file's mtime (0 if missing/unreadable)."""

    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _load_token_limits_safe(path: Path) -> dict | None:
    """Load token limits, returning ``None`` (not raising) if the file is absent.

    Token limits are optional advertised facts; a missing file must not prevent
    the gateway from starting or serving price facts. A present-but-malformed
    file still raises so drift is surfaced at startup rather than silently
    serving empty limits.
    """

    if not path.exists():
        return None
    return load_token_limits(path)


def _lookup_limits(
    token_limits: dict | None,
    quote: Any,
    upstream_model: str,
) -> Any:
    """Resolve advertised token limits for a price quote's model, or ``None``.

    Limits are keyed by ``(provider, region, upstream_model)``. Missing limits
    return ``None`` so the price snapshot is still served with
    ``output_limits: null`` rather than failing the whole query.
    """

    if not token_limits:
        return None
    return token_limits.get((quote.provider, quote.region, upstream_model))


def _refresh_pricing_if_changed(request: Request) -> None:
    """Hot-reload pricing from disk if any ``*.json`` mtime changed.

    Called only from the pricing-snapshots endpoint so that editing
    ``config/pricing/*.json`` takes effect without restarting the gateway.
    If re-loading fails (e.g. a half-written file), the previously loaded
    ``app.state.pricing`` is preserved — the gateway never crashes on a bad
    pricing file, it simply serves the last-known-good facts.
    """

    directory = request.app.state.pricing_dir
    current_mtime = _pricing_dir_mtime(directory)
    if current_mtime != request.app.state.pricing_mtime:
        try:
            request.app.state.pricing = load_pricing(directory)
            request.app.state.pricing_mtime = current_mtime
        except Exception:
            # Fail-safe: keep the old pricing; mtime stays stale so we retry next
            # request (the file may have been mid-write).
            pass

    # Hot-reload token limits from its single file using the same mtime guard.
    limits_file = request.app.state.token_limits_file
    limits_mtime = _file_mtime(limits_file)
    if limits_mtime != request.app.state.token_limits_mtime:
        try:
            request.app.state.token_limits = load_token_limits(limits_file)
            request.app.state.token_limits_mtime = limits_mtime
        except Exception:
            # Fail-safe: keep the old limits; mtime stays stale so we retry next
            # request. A missing file is valid (limits are optional) — clear it.
            if not limits_file.exists():
                request.app.state.token_limits = None
                request.app.state.token_limits_mtime = limits_mtime


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(prog="pvian-gateway")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    settings = GatewaySettings().resolved()
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.host,
        port=args.port or settings.port,
    )
    return 0


app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
