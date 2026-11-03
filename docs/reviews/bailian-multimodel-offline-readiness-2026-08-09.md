# Bailian cn-beijing multi-model gateway integration review

> **Superseded for current runtime status.** This file preserves the earlier
> offline/first-smoke review. The completed capability sweep and isolated 5220
> deployment are recorded in
> `docs/reviews/bailian-multimodel-gateway-live-2026-08-09.md`.

Review date: 2026-08-09  
Scope: gateway profiles, pricing, capability evidence, and forwarding integration  
Live execution status: **one catalog request and eight authorized one-shot forwarding smokes completed**

This review covers `qwen3.8-max`, `kimi/kimi-k3`, `glm-5.2`, `qwen3.7-plus`,
`qwen3-vl-plus`, `deepseek-v4-pro`, `MiniMax/MiniMax-M3`, and
`xiaomi/mimo-v2.5-pro` on Bailian cn-beijing. It is an infrastructure record only
and does not prescribe downstream experiment design or execution.

## Evidence classes must remain separate

| Evidence class | Network / cost | What it can prove | What it cannot prove |
|---|---:|---|---|
| Offline implementation and tests | none | profile/alias/schema parsing, exact binding, fail-closed gating, parameter forwarding, redaction policy | catalog presence, credentials, upstream behavior, current callability |
| Provider probe | provider network; potentially billed | exact upstream requested/reported model, parameter acceptance, usage, finish/error behavior | deployed gateway alias behavior |
| Forwarder smoke | provider network through the gateway forwarding component; potentially billed | exact profile resolution, request rewriting, upstream identity, finish/usage/cost capture, no retry/fallback | Bearer auth or exposure by the currently running gateway process |
| Deployed gateway smoke | gateway plus provider; potentially billed | Bearer auth, public alias exposure, exact route binding, transparent forwarding and safe trace/error envelope | downstream scientific validity |

Evidence directories, manifests, request identifiers, budgets, and conclusions must
identify exactly one class. A direct provider probe, component-level forwarder
smoke, and deployed-gateway smoke must not be represented as the same result.

## Offline admission state

The strings below are exact upstream candidate IDs, including case and embedded
slashes. A profile is only an offline candidate until live catalog discovery and
qualification have succeeded. Candidate profile and exact alias preregistration
are not `/v1/models` evidence: without exact enabled/nominal, unexpired state the
alias must remain hidden and non-callable.

### Authenticated live gateway snapshot

An authenticated `GET http://127.0.0.1:5200/v1/models` returned HTTP 200 on
2026-08-09. It exposed exactly one alias, which is outside the target eight:

```text
alias:           pvian/bailian-cn-beijing/deepseek-v4-flash-0731
route:           bailian-cn-beijing:deepseek-v4-flash-0731
profile:         provider-bailian-cn-beijing-deepseek-v4-flash-0731@0.1.0
upstream_model:  deepseek-v4-flash-0731
valid_until:     2026-08-10T10:20:38.783645Z
```

| Target upstream model | Present in authenticated `/v1/models` | Gap |
|---|---:|---|
| `qwen3.8-max` | no | qualification state, alias admission, deployed-gateway smoke |
| `kimi/kimi-k3` | no | non-truncated qualification, alias admission, deployed-gateway smoke |
| `glm-5.2` | no | qualification state, alias admission, deployed-gateway smoke |
| `qwen3.7-plus` | no | qualification state, alias admission, deployed-gateway smoke |
| `qwen3-vl-plus` | no | qualification state, alias admission, text-interface deployed-gateway smoke |
| `deepseek-v4-pro` | no | configured alias is not callable in this live snapshot; qualification/state admission and smoke remain |
| `MiniMax/MiniMax-M3` | no | qualification state, alias admission, deployed-gateway smoke |
| `xiaomi/mimo-v2.5-pro` | no | reported-model identity mismatch must be resolved before admission |

All eight `present` values are false. The profiles, pricing files, and configured
aliases in the working tree are offline artifacts and do not change this live
snapshot.

### Authorized forwarder smoke result

One provider catalog request confirmed that all eight exact standard-endpoint IDs
are present. The forwarding component then made exactly one non-stream completion
per model, with SDK retries disabled and no fallback. This was not a request to
the already-running public gateway and did not change its `/v1/models` result.

| Requested model | Reported model | HTTP / finish | Usage total | Estimated list cost (CNY) | Interpretation |
|---|---|---|---:|---:|---|
| `qwen3.8-max` | exact | 200 / stop | 45 | 0.000732 | message path observed |
| `kimi/kimi-k3` | exact | 200 / length | 225 | 0.014740 | path reached, but truncated; inconclusive |
| `glm-5.2` | exact | 200 / stop | 26 | 0.000368 | message path observed |
| `qwen3.7-plus` | exact | 200 / stop | 32 | 0.000112 | message path observed |
| `qwen3-vl-plus` | exact | 200 / stop | 29 | 0.000110 | text-message path only; multimodal untested |
| `deepseek-v4-pro` | exact | 200 / stop | 28 | 0.000456 | message path observed |
| `MiniMax/MiniMax-M3` | exact | 200 / stop | 183 | 0.00048636 | message path observed |
| `xiaomi/mimo-v2.5-pro` | `mimo-v2.5-pro` | 200 / stop | 274 | 0.0009688 | identity mismatch; fail closed |

The eight completions cost an estimated CNY 0.01797316 at the captured official
list prices. This value is accounting evidence, not an operational spending cap.
The gateway integration does not stop consumer requests at this amount. The smoke
artifact contains only identifiers, finish/usage/cost metadata, and hashed provider
request IDs; it excludes prompts, completions, reasoning text, provider keys, and
raw response bodies.

### Capability and pricing registry

The advertised columns below come from model-specific official references. The
last column is limited to the one non-stream text-message smoke above; it does not
promote `effective` capability, which remains `unknown` for all eight models.

| Model | Multimodal | Structured output | Tool calling | Reasoning | Streaming | Text-message observation |
|---|---|---|---|---|---|---|
| `qwen3.8-max` | unknown | unknown | unknown | supported | unknown | supported |
| `kimi/kimi-k3` | supported | supported | supported | supported | supported | inconclusive: truncated |
| `glm-5.2` | unsupported | supported | supported | supported | supported | supported |
| `qwen3.7-plus` | supported | supported | supported | supported | supported | supported |
| `qwen3-vl-plus` | supported | supported | supported | supported | supported | supported, text only |
| `deepseek-v4-pro` | unsupported | supported | supported | supported | supported | supported |
| `MiniMax/MiniMax-M3` | supported | unsupported | supported | supported | supported | supported |
| `xiaomi/mimo-v2.5-pro` | unsupported | supported | supported | supported | supported | inconclusive: identity mismatch |

List prices are CNY per one million tokens. Ranges select an accounting tier by
prompt-token count; they are not request limits. A `null` cache price fails cost
estimation closed only when the response reports cached tokens, without stopping
or retrying the consumer request.

| Model / prompt range | Input | Cached input | Output |
|---|---:|---:|---:|
| `qwen3.8-max` <=1M | 12 | unknown | 36 |
| `kimi/kimi-k3` <=1,048,576 | 20 | 2 | 100 |
| `glm-5.2` <=1,048,576 | 8 | 2 | 28 |
| `qwen3.7-plus` <=256K / 256K-1M | 2 / 6 | 0.4 / 1.2 | 8 / 24 |
| `qwen3-vl-plus` <=32K / 32K-128K / 128K-256K | 1 / 1.5 / 3 | 0.2 / 0.3 / 0.6 | 10 / 15 / 30 |
| `deepseek-v4-pro` <=1M | 12 | 1 | 24 |
| `MiniMax/MiniMax-M3` <=1,048,576 | 4.2 | 0.84 | 16.8 |
| `xiaomi/mimo-v2.5-pro` <=256K / 256K-1M | 7 / 14 | 1.4 / unknown | 21 / 42 |

| Class | Exact upstream candidate | Preregistered exact alias | Gateway interpretation |
|---|---|---|---|
| text | `qwen3.8-max` | `pvian/bailian-cn-beijing/qwen3.8-max` | standard catalog ID confirmed; no preview substitution |
| text | `kimi/kimi-k3` | `pvian/bailian-cn-beijing/kimi/kimi-k3` | candidate; preserve both path segments |
| text | `glm-5.2` | `pvian/bailian-cn-beijing/glm-5.2` | candidate; no substitution |
| compatibility | `qwen3.7-plus` | `pvian/bailian-cn-beijing/qwen3.7-plus` | candidate; no substitution |
| compatibility | `qwen3-vl-plus` | `pvian/bailian-cn-beijing/qwen3-vl-plus` | interface/future multimodal candidate only |
| compatibility | `deepseek-v4-pro` | `pvian/bailian-cn-beijing/deepseek-v4-pro` | existing configured route; current callability still comes only from authenticated `/v1/models` |
| compatibility | `MiniMax/MiniMax-M3` | `pvian/bailian-cn-beijing/MiniMax/MiniMax-M3` | candidate; preserve case and both path segments |
| compatibility | `xiaomi/mimo-v2.5-pro` | `pvian/bailian-cn-beijing/xiaomi/mimo-v2.5-pro` | candidate; preserve both path segments |

The typo `cn-beepseek` is invalid and must not appear in aliases, profiles, test
fixtures, or environment examples. The historical stable alias
`pvian/bailian-cn-beijing/deepseek-v4-flash-0731` is a separate route and is not a
fallback for any candidate above.

`qwen3-vl-plus` advertises multimodal capability. This integration only exercised
its OpenAI-compatible text message interface; image input remains untested and is
not implied by the text smoke.

## Per-model qualification manifest contract

Each model needs its own immutable manifest and result. At minimum, the manifest
must freeze and the result must persist the following non-secret fields:

- evidence class: `provider_probe` only for qualification;
- provider and region, exact requested model, reported model requirement;
- exact `route_id`, `profile_id`, `profile_version`, and nullable public alias;
- model comparability class (`text_candidate`, `multimodal_interface_only`, or an
  equivalently strict enum);
- transport and exact request shape: stream/non-stream, thinking/reasoning fields,
  `seed` support status, `max_tokens`, context claim/source, timeout, and retry;
- `fallback: null` and one total attempt unless a separately reviewed manifest
  explicitly authorizes more;
- usage required/unknown policy, pricing snapshot identity, currency/rates, and
  actual usage/cost accounting; a probe-only authorization envelope must never be
  enforced as a consumer runtime spending cap;
- request identity policy (hashed or absent provider request ID), finish reason,
  safe error category, and explicit stop conditions;
- content policy forbidding credentials, question/choice bodies, private reasoning,
  thinking payloads, and raw provider response bodies from terminal evidence;
- canonical digest covering all frozen fields.

Unknown parameter support or price is represented as unknown, never as `0`, a
nearby model's value, or a setting inherited from another route. Every model must
independently prove or reject its request settings before the gateway can publish
an alias.

## Gateway capability handoff and live gates

For each model, the gateway handoff must provide one exact tuple:

```text
(public_alias, requested_model, reported_model, route_id,
 profile_id, profile_version, qualification_manifest_digest,
 thinking/reasoning parameters, seed support, max_tokens, context limit,
 stream mode, timeout, gateway retry policy, and pricing snapshot)
```

No consumer receives a provider credential, and no gateway request may use
automatic routing, substitution, or fallback. Live evidence advances in this
order, with a separate artifact and authorization boundary at every generation
step:

1. authenticated `GET /v1/models` records the gateway's current exact aliases;
2. a separately authorized provider qualification uses the immutable per-model
   manifest, exact call count, parameters, and stop conditions;
3. only an exact requested/reported-model pass may create enabled qualification
   state and expose the preregistered public alias;
4. a separately authorized gateway smoke sends the exact public alias through the
   gateway and verifies Bearer auth, route/profile headers, parameter transparency,
   request IDs, usage/cost evidence, finish/error category, and no fallback;
5. the resulting tuple and evidence digests are handed to the downstream owner.

Provider qualification and gateway smoke are different evidence classes and must
not share a result manifest or be described as one test. This platform handoff
does not prescribe or modify downstream experiment parameters, budgets, runners,
or scientific contracts.

## Audit of pre-existing untracked launcher/runbook

`scripts/launch_gateway.py` and `docs/gateway-infra-runbook.md` pre-date this review
and were deliberately left unchanged. They are not safe authorization references
for this work:

- a default launcher invocation performs one direct provider completion and one
  gateway completion without `--allow-network` and `--confirm-cost` gates;
- the launcher logs the full generated gateway client key and logs the first part
  of upstream error bodies;
- the runbook calls this behavior the only correct startup path and includes a
  command that reads `.env`, sends a provider completion, and prints an upstream
  error body;
- the runbook also presents manual qualification-state mutation as routine
  recovery, bypassing an auditable qualification transition;
- these procedures mix startup, provider probe, gateway smoke, and operational
  recovery evidence.

Until separately repaired and reviewed, do not run that launcher or its inline
probe/state-reset recipes. Read-only health checks and authenticated model catalog
discovery remain distinct, non-generation operations.

## Offline exit criteria

Offline readiness is complete only when all of the following pass without network:

- all eight exact candidate IDs are declared by one and only one intended Bailian
  profile, and nearby/typo IDs do not match;
- all preregistered registry bindings validate exactly; candidate aliases have no
  callable state and therefore cannot appear in `/v1/models`;
- gateway forwarding rewrites only `model` and preserves frozen request fields;
- missing qualification, profile/version drift, reported-model mismatch, expiry,
  or degraded status fails closed without an upstream request;
- upstream failures retain their category and never trigger fallback;
- qualification and terminal-state serializers reject secret, raw-response,
  prompt, and private-reasoning fields;
- the multimodal-only interpretation of `qwen3-vl-plus` is machine-readable and
  documented;
- prices are source-attributed exact list values or explicitly unavailable. No
  zero placeholder is accepted, and accounting never imposes a runtime cost cap.
