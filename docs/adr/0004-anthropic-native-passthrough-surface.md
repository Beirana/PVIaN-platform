# ADR 0004: Anthropic Native Passthrough Surface

Date: 2026-08-20
Status: Accepted
Operators: user directive 2026-08-19/20 ("我还是希望能够经过网关进行适配，但是不一定要用openai格式…如果不稳定可以直接透传anthropic格式")

## Context

The AiGoCode relay serves `claude-sonnet-5` on two protocol surfaces:

- an OpenAI-compatible `/v1/chat/completions` face, whose station-side
  translation shim has two recorded defects (2026-08-18/19): malformed
  concatenated tool-call arguments (`{}{"city": ...}`) and 400-rejected
  tool-result replay — agent tool loops are unusable there;
- an Anthropic-native `/v1/messages` face (operator-provided via CCSwitch),
  wire-characterized 2026-08-19 with three authorized calls: message 200 with
  exact identity, forced `tool_use` returning a **native dict** `input`, and
  `tool_result` replay accepted 200.

Consumers (Claude Code / CCSwitch / Anthropic SDK) want the Claude tool loop
behind the gateway's single client key, alias registry, qualification gating,
and usage ledger.

## Decision

Expose **transparent Anthropic Messages passthrough** on the gateway
(`POST /v1/messages`, `POST /v1/messages/count_tokens`). No protocol
translation is performed anywhere: the only body mutation is the existing
single-field model facade rewrite (alias → exact upstream model); thinking
blocks, `tool_use` blocks, and SSE events pass through unmodified.

Rejected alternative — **protocol bridging** (consumer speaks OpenAI chat,
gateway translates to Anthropic): translation is precisely where the station
shim's defects live, and the OpenAI Chat Completions protocol has no
reasoning slot (settled conclusion in the 2026-08-18 handoff), so a bridge
would either drop Claude thinking or invent nonstandard fields. Building our
own bridge would recreate the same defect class we just escaped. The OpenAI
Responses API was rejected for the same reason: the station's `/v1/responses`
is also a translation shim, already proven lossy for reasoning.

## Implementation contract

- New transport `anthropic_native_messages` in provider profiles: parses
  `allowed_hosts` / `required_base_path` (same https/443 enforcement) plus a
  required `messages_path`; `ResolvedEndpoint.messages_url()` builds the
  upstream URL.
- Alias registry accepts the new transport; aliases bind per-endpoint as
  before (`pvian/aigocode-anthropic/claude-sonnet-5` →
  `aigocode-anthropic:claude-sonnet-5`).
- `POST /v1/messages` mirrors the chat handler's auth (client-key Bearer),
  alias resolution, `callable_record` gating, failure categories, ledger
  attempts, and `x-pvian-*` response headers; it adds
  `x-pvian-api-surface: anthropic_native_messages`. The OpenAI chat handler
  is untouched; an OpenAI-surface alias sent to `/v1/messages` (or the
  reverse) fails closed with 409 because endpoint resolution is
  transport-exact.
- Upstream headers: Bearer credential + pinned `anthropic-version` and a
  claude-cli style User-Agent (the station WAF returned 502 for a generic
  python UA on 2026-08-19).
- Usage accounting: `AnthropicUsageCollector` reads Anthropic
  `usage.input_tokens` / `usage.output_tokens` (non-stream top level;
  `message_start` / `message_delta` on SSE); thinking tokens stay folded in
  output_tokens — no invented split. Cost stays `unknown` (station prices are
  console-walled).
- Qualification for this transport runs through a dedicated Anthropic
  wire-format probe script (message / forced tool_use / tool_result replay
  with requested=reported identity); the OpenAI qualifier skips the
  transport, and there is no catalog GET on this surface — identity is proven
  on the wire.
- Renewal: the route follows the same 24h-window daily requalification and
  budget membership as every other production route (ADR 0001 as amended).

## Consequences

- Positive: Claude tool loops and thinking become gateway-first with zero
  translation risk; consumers migrate by pointing `ANTHROPIC_BASE_URL` at
  the gateway and `ANTHROPIC_AUTH_TOKEN` at the client key.
- Positive: the OpenAI surface keeps its exact behavior (existing tests stay
  green; new surface is additive).
- Negative: consumers wanting OpenAI-chat-shaped Claude with tool loops must
  switch to the Anthropic surface (the OpenAI-compat route remains
  dialogue/stream/structured-only until the station fixes its shim).
- Negative: usage parsing is format-specific; a second collector class must
  track Anthropic wire changes.
