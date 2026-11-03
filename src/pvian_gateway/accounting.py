from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class AccountingError(ValueError):
    pass


class SnapshotValidationError(AccountingError):
    """Raised when a price snapshot fails validation."""
    pass


@dataclass(frozen=True)
class UsageSummary:
    prompt_tokens: int
    cached_tokens: int
    reasoning_tokens: int | None
    completion_tokens: int
    total_tokens: int | None

    def as_dict(self) -> dict[str, int | None]:
        return asdict(self)


@dataclass(frozen=True)
class PriceTier:
    input_tokens_gt: int
    input_tokens_lte: int
    input_per_1m: Decimal
    cached_input_per_1m: Decimal | None
    output_per_1m: Decimal

    def contains(self, input_tokens: int) -> bool:
        return self.input_tokens_gt < input_tokens <= self.input_tokens_lte


@dataclass(frozen=True)
class PriceQuote:
    provider: str
    profile_id: str
    region: str
    model: str
    pricing_version: str
    effective_at: str | None
    currency: str
    tiers: tuple[PriceTier, ...]
    captured_at: str | None = None
    source: dict[str, Any] | None = None
    schema_version: str = ""

    def tier_for(self, input_tokens: int) -> PriceTier | None:
        return next((tier for tier in self.tiers if tier.contains(input_tokens)), None)


def load_pricing(directory: Path) -> tuple[PriceQuote, ...]:
    quotes: list[PriceQuote] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        except (OSError, json.JSONDecodeError) as exc:
            raise AccountingError("cannot read local pricing document") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            "pvian.local-pricing/v1",
            "pvian.local-pricing/v2",
        }:
            raise AccountingError("unsupported local pricing schema")
        if payload.get("unit") != "per_1m_tokens":
            raise AccountingError("invalid local pricing document")
        schema_version = payload["schema_version"]
        if schema_version == "pvian.local-pricing/v1":
            rates = payload.get("rates")
            if not isinstance(rates, dict):
                raise AccountingError("invalid local pricing document")
            tiers = (
                PriceTier(
                    input_tokens_gt=0,
                    input_tokens_lte=2**63 - 1,
                    input_per_1m=_rate(rates, "input"),
                    cached_input_per_1m=_rate(rates, "cached_input"),
                    output_per_1m=_rate(rates, "output"),
                ),
            )
            captured_at: str | None = None
            source: dict[str, Any] | None = None
        else:
            tiers = _price_tiers(payload)
            captured_at = _optional_string(payload, "captured_at")
            raw_source = payload.get("source")
            source = raw_source if isinstance(raw_source, dict) else None
        quotes.append(
            PriceQuote(
                provider=_string(payload, "provider"),
                profile_id=_string(payload, "profile_id"),
                region=_string(payload, "region"),
                model=_string(payload, "model"),
                pricing_version=_string(payload, "pricing_version"),
                effective_at=_optional_string(payload, "effective_at"),
                currency=_string(payload, "currency"),
                tiers=tiers,
                captured_at=captured_at,
                source=source,
                schema_version=schema_version,
            )
        )
    return tuple(quotes)


def select_quote(
    quotes: tuple[PriceQuote, ...], provider: str, region: str, model: str
) -> PriceQuote | None:
    matches = [
        quote
        for quote in quotes
        if (quote.provider, quote.region, quote.model) == (provider, region, model)
    ]
    if len(matches) > 1:
        raise AccountingError("ambiguous local pricing")
    return matches[0] if matches else None


def region_for_profile(
    quotes: tuple[PriceQuote, ...], provider: str, profile_id: str, model: str
) -> str | None:
    """Resolve a route's region only from the versioned local pricing binding."""

    regions = {
        quote.region
        for quote in quotes
        if (quote.provider, quote.profile_id, quote.model) == (provider, profile_id, model)
    }
    return next(iter(regions)) if len(regions) == 1 else None


# ---------------------------------------------------------------------------
# D: Pricing snapshot response builder (for GET /v1/gateway/pricing/snapshots)
# ---------------------------------------------------------------------------

SNAPSHOT_RESPONSE_SCHEMA_VERSION = "pvian.pricing-snapshot-response/v1"

TOKEN_LIMITS_SCHEMA_VERSION = "pvian.model-token-limits/v1"


@dataclass(frozen=True)
class ThinkingDefault:
    """A model's default thinking/reasoning behavior when no params are sent.

    Captures both dimensions the official pages expose: the default *state*
    (on/off/always_on/adaptive) and the default *intensity* — either a
    ``reasoning_effort`` dial value (``default_effort``) or a ``thinking_budget``
    token cap (``default_thinking_budget``). A model that has no intensity dial
    (e.g. MiMo, MiniMax adaptive) leaves both null. ``default_effort_confirmation``
    is ``cross_source_unconfirmed`` only when the official page does not state the
    default effort and it was inferred (e.g. GLM-5.2 reasoning_effort).
    """

    state: str
    default_effort: str | None
    default_thinking_budget: int | None
    default_effort_confirmation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "default_effort": self.default_effort,
            "default_thinking_budget": self.default_thinking_budget,
            "default_effort_confirmation": self.default_effort_confirmation,
        }


@dataclass(frozen=True)
class RecommendedDefaults:
    """A ready-to-use request-parameter recipe to reproduce a sensible default.

    Consumers combine these with their own ``messages``/``model`` and a chosen
    ``max_tokens`` to build a complete request without reading per-model docs:

    - ``send``: reasoning params to set explicitly (empty ``{}`` = omit all
      reasoning params to get the upstream default; non-empty where the project
      policy overrides the upstream default, e.g. qwen3-vl-plus forced on).
    - ``omit``: params this model does NOT support and will 400 on if sent.
    - ``required_non_reasoning``: non-reasoning params required for a valid
      default request (e.g. ``{"stream": true}`` for kimi-k3).
    """

    send: dict[str, Any]
    omit: tuple[str, ...]
    required_non_reasoning: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "send": dict(self.send),
            "omit": list(self.omit),
            "required_non_reasoning": dict(self.required_non_reasoning),
        }


@dataclass(frozen=True)
class TokenLimits:
    """Advertised model output/context limits sourced from official pages.

    These are static capability facts (not runtime usage stats): the model's
    hard ceilings on context, output and thinking-chain length, plus the
    request parameter and thinking controls documented for that model. They are
    injected into the pricing-snapshot response so the experiment side can size
    ``max_tokens``/``thinking_budget`` budgets alongside price facts in one
    query. Reasoning tokens count toward the output budget on all thinking
    models (see the official deep-thinking page usage example).
    """

    provider: str
    region: str
    upstream_model: str
    context_window: int
    max_output_tokens: int
    max_thinking_chain_tokens: int | None
    output_parameter: str
    thinking_default: ThinkingDefault
    thinking_controls: tuple[str, ...]
    recommended_defaults: RecommendedDefaults
    confirmation: str
    source_url: str
    notes: str | None

    def as_output_limits(self) -> dict[str, Any]:
        """Render the ``output_limits`` block for a pricing-snapshot response."""
        return {
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "max_thinking_chain_tokens": self.max_thinking_chain_tokens,
            "output_parameter": self.output_parameter,
            "thinking_default": self.thinking_default.as_dict(),
            "thinking_controls": list(self.thinking_controls),
            "recommended_defaults": self.recommended_defaults.as_dict(),
            "confirmation": self.confirmation,
            "source_url": self.source_url,
            "notes": self.notes,
        }


def load_token_limits(path: Path) -> dict[tuple[str, str, str], TokenLimits]:
    """Load advertised token limits indexed by ``(provider, region, model)``.

    Mirrors ``load_pricing``'s fail-safe posture: a malformed document raises
    ``AccountingError``; the caller (gateway hot-reload) catches and keeps the
    last-known-good limits. Unknown keys are rejected to surface drift early.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountingError("cannot read model token limits document") from exc
    if not isinstance(payload, dict):
        raise AccountingError("invalid model token limits document")
    if payload.get("schema_version") != TOKEN_LIMITS_SCHEMA_VERSION:
        raise AccountingError("unsupported model token limits schema")

    raw_limits = payload.get("limits")
    if not isinstance(raw_limits, list):
        raise AccountingError("invalid model token limits document")

    index: dict[tuple[str, str, str], TokenLimits] = {}
    for entry in raw_limits:
        if not isinstance(entry, dict):
            raise AccountingError("invalid model token limits document")
        try:
            provider = _string(entry, "provider")
            region = _string(entry, "region")
            upstream_model = _string(entry, "upstream_model")
            controls_raw = entry.get("thinking_controls")
            if not isinstance(controls_raw, list) or not all(
                isinstance(c, str) for c in controls_raw
            ):
                raise AccountingError("invalid thinking_controls")
            limits = TokenLimits(
                provider=provider,
                region=region,
                upstream_model=upstream_model,
                context_window=_int_limit(entry, "context_window"),
                max_output_tokens=_int_limit(entry, "max_output_tokens"),
                max_thinking_chain_tokens=_optional_int_limit(
                    entry, "max_thinking_chain_tokens"
                ),
                output_parameter=_string(entry, "output_parameter"),
                thinking_default=_thinking_default(entry),
                thinking_controls=tuple(controls_raw),
                recommended_defaults=_recommended_defaults(entry),
                confirmation=_string(entry, "confirmation"),
                source_url=_string(entry, "source_url"),
                notes=_optional_string(entry, "notes"),
            )
        except AccountingError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountingError("invalid model token limits document") from exc
        index[(provider, region, upstream_model)] = limits
    return index


def _int_limit(entry: Mapping[str, Any], key: str) -> int:
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AccountingError(f"invalid {key}")
    return value


def _thinking_default(entry: Mapping[str, Any]) -> ThinkingDefault:
    """Parse the ``thinking_default`` object, rejecting unknown keys."""

    raw = entry.get("thinking_default")
    if not isinstance(raw, dict):
        raise AccountingError("invalid thinking_default")
    allowed = {"state", "default_effort", "default_thinking_budget", "default_effort_confirmation"}
    if not raw.keys() <= allowed:
        raise AccountingError("invalid thinking_default")
    state = raw.get("state")
    if state not in {"on", "off", "always_on", "adaptive"}:
        raise AccountingError("invalid thinking_default.state")
    effort = raw.get("default_effort")
    if effort is not None and not (isinstance(effort, str) and effort):
        raise AccountingError("invalid thinking_default.default_effort")
    budget = raw.get("default_thinking_budget")
    if budget is not None and (
        not isinstance(budget, int) or isinstance(budget, bool) or budget < 1
    ):
        raise AccountingError("invalid thinking_default.default_thinking_budget")
    effort_conf = raw.get("default_effort_confirmation")
    if effort_conf not in {"confirmed", "cross_source_unconfirmed"}:
        raise AccountingError("invalid thinking_default.default_effort_confirmation")
    return ThinkingDefault(
        state=state,
        default_effort=effort,
        default_thinking_budget=budget,
        default_effort_confirmation=effort_conf,
    )


def _recommended_defaults(entry: Mapping[str, Any]) -> RecommendedDefaults:
    """Parse the ``recommended_defaults`` object, rejecting unknown keys."""

    raw = entry.get("recommended_defaults")
    if not isinstance(raw, dict):
        raise AccountingError("invalid recommended_defaults")
    allowed = {"send", "omit", "required_non_reasoning"}
    if not raw.keys() <= allowed:
        raise AccountingError("invalid recommended_defaults")
    send = raw.get("send")
    if not isinstance(send, dict):
        raise AccountingError("invalid recommended_defaults.send")
    omit_raw = raw.get("omit")
    if not isinstance(omit_raw, list) or not all(isinstance(o, str) and o for o in omit_raw):
        raise AccountingError("invalid recommended_defaults.omit")
    required_nr = raw.get("required_non_reasoning")
    if not isinstance(required_nr, dict):
        raise AccountingError("invalid recommended_defaults.required_non_reasoning")
    return RecommendedDefaults(
        send=dict(send),
        omit=tuple(omit_raw),
        required_non_reasoning=dict(required_nr),
    )


def _optional_int_limit(entry: Mapping[str, Any], key: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AccountingError(f"invalid {key}")
    return value


def build_pricing_snapshot(
    quote: PriceQuote,
    *,
    public_alias: str | None,
    route_id: str | None,
    upstream_model: str,
    limits: TokenLimits | None = None,
) -> dict[str, Any]:
    """Assemble a versioned pricing-snapshot response object from a price quote.

    The snapshot is *derived* from the gateway's loaded pricing facts at request
    time.  ``snapshot_id`` is deterministically derived from the price identity
    (provider/region/upstream_model/pricing_version), so any price change
    naturally produces a new id.  ``snapshot_digest`` is the SHA-256 of the
    canonical JSON of everything except itself, letting the experiment side
    re-verify integrity after persisting the snapshot verbatim.

    Provenance fields (``source_urls``/``source_digests``) are echoed verbatim
    from the pricing file — the gateway does not verify content hashes; it
    honestly reports what was captured (often ``content_sha256: null`` for
    dynamic pages that could not be archived).

    ``limits``, when provided, populates the ``output_limits`` block with the
    model's advertised context/output/thinking-chain ceilings. It is injected
    *before* the digest is computed so it is covered by ``snapshot_digest``.
    When no limits are bound to the model, ``output_limits`` is ``null`` (the
    price facts are still served — limits absence is not a hard failure).
    """

    first_tier = quote.tiers[0]
    cached_prompt: float | None = (
        None if first_tier.cached_input_per_1m is None else float(first_tier.cached_input_per_1m)
    )
    cache_mode = "explicit_cache" if cached_prompt is not None else "no_cache_rate"

    rates = {
        "prompt": float(first_tier.input_per_1m),
        "cached_prompt": cached_prompt,
        "completion": float(first_tier.output_per_1m),
        "unit": "per_1m_tokens",
    }

    tiers_serialized = [
        {
            "prompt_tokens_gt": tier.input_tokens_gt,
            "prompt_tokens_lte": tier.input_tokens_lte,
            "rates": {
                "prompt": float(tier.input_per_1m),
                "cached_prompt": (
                    None if tier.cached_input_per_1m is None else float(tier.cached_input_per_1m)
                ),
                "completion": float(tier.output_per_1m),
            },
        }
        for tier in quote.tiers
    ]

    source_urls: list[str] = []
    source_digests: list[dict[str, str]] = []
    if isinstance(quote.source, dict):
        url = quote.source.get("url")
        if isinstance(url, str) and url:
            source_urls.append(url)
        content_sha = quote.source.get("content_sha256")
        if isinstance(content_sha, str) and content_sha:
            source_digests.append(
                {
                    "kind": "content_sha256",
                    "value": content_sha,
                    "status": str(
                        quote.source.get("content_digest_status", "unknown")
                    ),
                }
            )

    snapshot_id = f"snap-{quote.provider}-{quote.region}-{upstream_model}-{quote.pricing_version}"

    snapshot: dict[str, Any] = {
        "schema_version": SNAPSHOT_RESPONSE_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "provider": quote.provider,
        "region": quote.region,
        "currency": quote.currency,
        "public_alias": public_alias,
        "route": route_id,
        "upstream_model": upstream_model,
        "profile_id": quote.profile_id,
        "model": quote.model,
        "rates": rates,
        "tiers": tiers_serialized,
        "cache_mode": cache_mode,
        "pricing_version": quote.pricing_version,
        "effective_at": quote.effective_at,
        "captured_at": quote.captured_at,
        "valid_until": None,
        "source_urls": source_urls,
        "source_digests": source_digests,
        "output_limits": limits.as_output_limits() if limits is not None else None,
    }

    # Compute snapshot_digest: SHA-256 of canonical JSON excluding snapshot_digest.
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    snapshot["snapshot_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return snapshot


def extract_usage(payload: object) -> UsageSummary | None:
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    if not isinstance(usage, Mapping):
        return None
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total_raw = usage.get("total_tokens")
    total = None if total_raw is None else _int(total_raw)
    if prompt is None or completion is None or (total_raw is not None and total is None):
        return None
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
    completion_details = completion_details if isinstance(completion_details, Mapping) else {}
    cached_raw = prompt_details.get("cached_tokens", 0)
    cached = _int(cached_raw)
    reasoning_raw = completion_details.get("reasoning_tokens")
    reasoning = None if reasoning_raw is None else _int(reasoning_raw)
    if (
        cached is None
        or cached > prompt
        # No subset invariant for reasoning: DeepSeek folds reasoning into
        # completion_tokens, but e.g. OpenRouter reports reasoning_tokens as a
        # separate pool that may exceed completion_tokens.
        or (total is not None and total != prompt + completion)
    ):
        return None
    return UsageSummary(prompt, cached, reasoning, completion, total)


def estimate(usage: UsageSummary | None, quote: PriceQuote | None) -> dict[str, Any]:
    if usage is None:
        return {"status": "unknown", "reason": "usage_unknown"}
    if quote is None:
        return {"status": "unknown", "reason": "pricing_missing"}
    tier = quote.tier_for(usage.prompt_tokens)
    if tier is None:
        return {"status": "unknown", "reason": "pricing_tier_missing"}
    if usage.cached_tokens and tier.cached_input_per_1m is None:
        return {"status": "unknown", "reason": "cached_input_pricing_unknown"}
    cached_rate = tier.cached_input_per_1m or Decimal(0)
    amount = (
        Decimal(usage.prompt_tokens - usage.cached_tokens) * tier.input_per_1m
        + Decimal(usage.cached_tokens) * cached_rate
        + Decimal(usage.completion_tokens) * tier.output_per_1m
    ) / Decimal(1_000_000)
    return {
        "status": "estimated",
        "currency": quote.currency,
        "pricing_version": quote.pricing_version,
        "effective_at": quote.effective_at,
        "amount": format(amount, "f"),
    }


class UsageCollector:
    """A bounded ephemeral parser: forwarded bytes are not modified or persisted."""

    _MAX_BYTES = 1_048_576

    def __init__(self, content_type: str | None) -> None:
        self._sse = bool(content_type and "text/event-stream" in content_type.lower())
        self._buffer = bytearray()
        self._overflow = False
        self.reported_model: str | None = None
        self.finish_reason: str | None = None
        self._sse_complete = not self._sse

    def feed(self, chunk: bytes) -> None:
        if self._overflow:
            return
        if len(self._buffer) + len(chunk) > self._MAX_BYTES:
            self._buffer.clear()
            self._overflow = True
            return
        self._buffer.extend(chunk)

    def final_usage(self) -> UsageSummary | None:
        if self._overflow:
            return None
        try:
            if not self._sse:
                return self._usage_from_payload(json.loads(bytes(self._buffer)))
            last: UsageSummary | None = None
            events = bytes(self._buffer).decode("utf-8").replace("\r\n", "\n").split("\n\n")
            for event in events:
                data = "\n".join(
                    line[5:].lstrip() for line in event.split("\n") if line.startswith("data:")
                )
                if data == "[DONE]":
                    self._sse_complete = True
                elif data:
                    usage = self._usage_from_payload(json.loads(data))
                    if usage is not None:
                        last = usage
            return last
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._sse_complete = False
            return None

    @property
    def complete(self) -> bool:
        return not self._overflow and self._sse_complete

    def _usage_from_payload(self, payload: object) -> UsageSummary | None:
        if isinstance(payload, Mapping):
            model = payload.get("model")
            if isinstance(model, str) and model:
                self.reported_model = model
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                finish_reason = choices[0].get("finish_reason")
                if isinstance(finish_reason, str) and re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,64}", finish_reason
                ):
                    self.finish_reason = finish_reason
        return extract_usage(payload)


class AnthropicUsageCollector:
    """Same bounded-ephemeral contract as UsageCollector, Anthropic wire format.

    Non-streaming bodies carry ``usage.{input_tokens,output_tokens}`` at the
    message top level; SSE streams report input tokens in ``message_start``
    and cumulative output tokens in ``message_delta`` until ``message_stop``.
    Thinking tokens are folded into output_tokens by the Anthropic API, so
    ``reasoning_tokens`` stays None rather than inventing a split.
    """

    _MAX_BYTES = 1_048_576

    def __init__(self, content_type: str | None) -> None:
        self._sse = bool(content_type and "text/event-stream" in content_type.lower())
        self._buffer = bytearray()
        self._overflow = False
        self.reported_model: str | None = None
        self.finish_reason: str | None = None
        self._complete = not self._sse

    def feed(self, chunk: bytes) -> None:
        if self._overflow:
            return
        if len(self._buffer) + len(chunk) > self._MAX_BYTES:
            self._buffer.clear()
            self._overflow = True
            return
        self._buffer.extend(chunk)

    def final_usage(self) -> UsageSummary | None:
        if self._overflow:
            return None
        try:
            if not self._sse:
                return self._usage_from_message(json.loads(bytes(self._buffer)))
            prompt_tokens: int | None = None
            output_tokens: int | None = None
            cached_tokens = 0
            events = bytes(self._buffer).decode("utf-8").replace("\r\n", "\n").split("\n\n")
            for event in events:
                data = "\n".join(
                    line[5:].lstrip() for line in event.split("\n") if line.startswith("data:")
                )
                if not data:
                    continue
                payload = json.loads(data)
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") == "message_start":
                    message = payload.get("message")
                    if isinstance(message, Mapping):
                        model = message.get("model")
                        if isinstance(model, str) and model:
                            self.reported_model = model
                        usage = message.get("usage")
                        if isinstance(usage, Mapping):
                            prompt_tokens = _nonnegative_or_none(usage.get("input_tokens"))
                            cached_tokens = _nonnegative_or_none(
                                usage.get("cache_read_input_tokens")
                            ) or 0
                elif payload.get("type") == "message_delta":
                    delta = payload.get("delta")
                    if isinstance(delta, Mapping):
                        stop = delta.get("stop_reason")
                        if isinstance(stop, str) and re.fullmatch(
                            r"[A-Za-z0-9_.-]{1,64}", stop
                        ):
                            self.finish_reason = stop
                    usage = payload.get("usage")
                    if isinstance(usage, Mapping):
                        output_tokens = _nonnegative_or_none(usage.get("output_tokens"))
                elif payload.get("type") == "message_stop":
                    self._complete = True
            if prompt_tokens is None or output_tokens is None:
                return None
            return UsageSummary(
                prompt_tokens=prompt_tokens,
                cached_tokens=cached_tokens,
                reasoning_tokens=None,
                completion_tokens=output_tokens,
                total_tokens=prompt_tokens + output_tokens,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._complete = False
            return None

    @property
    def complete(self) -> bool:
        return not self._overflow and self._complete

    def _usage_from_message(self, payload: object) -> UsageSummary | None:
        if not isinstance(payload, Mapping):
            return None
        model = payload.get("model")
        if isinstance(model, str) and model:
            self.reported_model = model
        stop_reason = payload.get("stop_reason")
        if isinstance(stop_reason, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", stop_reason):
            self.finish_reason = stop_reason
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return None
        prompt_tokens = _nonnegative_or_none(usage.get("input_tokens"))
        output_tokens = _nonnegative_or_none(usage.get("output_tokens"))
        if prompt_tokens is None or output_tokens is None:
            return None
        return UsageSummary(
            prompt_tokens=prompt_tokens,
            cached_tokens=_nonnegative_or_none(usage.get("cache_read_input_tokens")) or 0,
            reasoning_tokens=None,
            completion_tokens=output_tokens,
            total_tokens=prompt_tokens + output_tokens,
        )


def _nonnegative_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise AccountingError(f"invalid local pricing {name}")
    return item


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    item = value.get(name)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise AccountingError(f"invalid local pricing {name}")
    return item


def _rate(value: Mapping[str, object], name: str) -> Decimal:
    item = value.get(name)
    if isinstance(item, bool) or not isinstance(item, (int, Decimal)):
        raise AccountingError(f"invalid local pricing {name}")
    result = Decimal(item)
    if result < 0:
        raise AccountingError(f"negative local pricing {name}")
    return result


def _optional_rate(value: Mapping[str, object], name: str) -> Decimal | None:
    if value.get(name) is None:
        return None
    return _rate(value, name)


def _price_tiers(payload: Mapping[str, object]) -> tuple[PriceTier, ...]:
    raw_tiers = payload.get("tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise AccountingError("invalid local pricing tiers")
    tiers: list[PriceTier] = []
    expected_gt = 0
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, Mapping):
            raise AccountingError("invalid local pricing tier")
        input_tokens_gt = _nonnegative_int(raw_tier.get("input_tokens_gt"), "input_tokens_gt")
        input_tokens_lte = _positive_int(raw_tier.get("input_tokens_lte"), "input_tokens_lte")
        if input_tokens_gt != expected_gt or input_tokens_lte <= input_tokens_gt:
            raise AccountingError("local pricing tiers must be contiguous and increasing")
        rates = raw_tier.get("rates")
        if not isinstance(rates, Mapping) or set(rates) != {"input", "cached_input", "output"}:
            raise AccountingError("invalid local pricing tier rates")
        tiers.append(
            PriceTier(
                input_tokens_gt=input_tokens_gt,
                input_tokens_lte=input_tokens_lte,
                input_per_1m=_rate(rates, "input"),
                cached_input_per_1m=_optional_rate(rates, "cached_input"),
                output_per_1m=_rate(rates, "output"),
            )
        )
        expected_gt = input_tokens_lte
    return tuple(tiers)


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AccountingError(f"invalid local pricing {name}")
    return value


def _positive_int(value: object, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise AccountingError(f"invalid local pricing {name}")
    return result


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# ---------------------------------------------------------------------------
# E: Independent price snapshot loading & validation
# ---------------------------------------------------------------------------

_SNAPSHOT_SCHEMA_VERSION = "pvian.price-snapshot/v1"
_SNAPSHOT_REQUIRED_RATE_KEYS = frozenset({"input", "cached_input", "output"})


def load_price_snapshot(path: Path, *, base_dir: Path | None = None) -> dict[str, Any]:
    """Load and validate a price snapshot file.

    Reads the JSON file, validates schema, digest integrity, required fields,
    rate structure (exactly ``input``/``cached_input``/``output``, non-negative),
    and **fail-closed** source-document provenance.  Returns the validated
    snapshot dict on success.

    ``base_dir`` is the root that ``source_pricing_document`` (a repo-relative
    path) is resolved against.  It defaults to ``path.parents[3]`` — the
    repository root for the canonical layout
    ``<root>/config/pricing/snapshots/<model>.json``.  Callers loading a
    snapshot from a non-canonical location must pass ``base_dir`` explicitly.

    Raises ``SnapshotValidationError`` on any validation failure, including a
    missing or non-regular source file (fail-closed: provenance that cannot be
    verified is treated as invalid, not skipped).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotValidationError(f"cannot read snapshot file: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotValidationError(f"snapshot file is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SnapshotValidationError("snapshot root must be an object")

    # Schema version
    if data.get("schema_version") != _SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotValidationError(
            f"unsupported snapshot schema: {data.get('schema_version')!r}"
        )

    # Required fields
    for field in (
        "snapshot_id",
        "created_at",
        "effective_at",
        "currency",
        "source_pricing_document",
        "source_pricing_document_sha256",
        "pricing_version",
        "provider",
        "region",
        "model",
        "rates",
        "snapshot_digest",
    ):
        if field not in data:
            raise SnapshotValidationError(f"missing required field: {field}")

    # Digest integrity: SHA-256 of canonical JSON of everything except digest.
    stored_digest = data["snapshot_digest"]
    if not isinstance(stored_digest, str) or len(stored_digest) != 64:
        raise SnapshotValidationError("invalid snapshot_digest format")
    sans_digest = {k: v for k, v in data.items() if k != "snapshot_digest"}
    canonical = json.dumps(sans_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    computed_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if computed_digest != stored_digest:
        raise SnapshotValidationError("snapshot_digest integrity check failed")

    # Rates: exactly the required keys, each non-negative numeric.
    rates = data["rates"]
    if not isinstance(rates, dict):
        raise SnapshotValidationError("rates must be an object")
    rate_keys = set(rates)
    if rate_keys != set(_SNAPSHOT_REQUIRED_RATE_KEYS):
        missing = sorted(_SNAPSHOT_REQUIRED_RATE_KEYS - rate_keys)
        extra = sorted(rate_keys - _SNAPSHOT_REQUIRED_RATE_KEYS)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        raise SnapshotValidationError(f"rates keys mismatch: {', '.join(detail)}")
    for rate_name in _SNAPSHOT_REQUIRED_RATE_KEYS:
        rate_value = rates[rate_name]
        if isinstance(rate_value, bool) or not isinstance(rate_value, (int, Decimal, float)):
            raise SnapshotValidationError(f"invalid rate type for {rate_name}")
        if rate_value < 0:
            raise SnapshotValidationError(f"negative rate for {rate_name}")

    # Source-document provenance — fail-closed.
    source_rel = data["source_pricing_document"]
    root = base_dir if base_dir is not None else path.parents[3]
    source_path = root / source_rel
    # Fail-closed: a missing or non-regular source file means provenance
    # cannot be verified, which is treated as invalid (not silently skipped).
    if not source_path.is_file():
        raise SnapshotValidationError(
            f"source pricing document not found or not a regular file: {source_path}"
        )
    actual_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if actual_sha != data["source_pricing_document_sha256"]:
        stored_short = data["source_pricing_document_sha256"][:16]
        raise SnapshotValidationError(
            f"source_pricing_document_sha256 mismatch: stored={stored_short}... "
            f"actual={actual_sha[:16]}..."
        )

    return data
