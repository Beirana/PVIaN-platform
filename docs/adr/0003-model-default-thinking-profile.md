# ADR 0003: Model Default Thinking Profile in Token-Limits Data

Date: 2026-08-14
Status: Accepted
Related: `docs/reviews/bailian-max-output-token-audit-2026-08-13.md`,
`docs/gateway-pricing-snapshots.md` (output_limits block)

## Context

The gateway exposes 9 Bailian cn-beijing models with heterogeneous thinking/reasoning
behavior. When a consumer sends a request **without any reasoning parameter**, the
upstream's default decides whether and how intensely the model thinks. This default
matters for two reasons:

1. **Budgeting.** `max_tokens` must hold reasoning tokens + the final reply together
   (ADR-confirmed in the token audit). A consumer who does not know the model defaults
   to thinking-on at effort=high will under-size `max_tokens` and get truncated or empty
   replies, with no error from the gateway (it transparently forwards).
2. **Parameter shape differs per model family.** Qwen3/GLM use `thinking_budget` (token
   cap); DeepSeek V4 uses `reasoning_effort` (ordinal dial) and has **no** thinking_budget;
   Kimi K3 is always-on and only accepts `reasoning_effort=max`; MiMo has no intensity
   control at all (only an on/off switch); MiniMax uses `thinking.type=adaptive|disabled`.
   A single scalar or string cannot capture these.

The initial token-limits data (added 2026-08-13) recorded `thinking_default` as a single
string (`on`/`off`/`thinking_only`/`adaptive`). This conflated **state** with **intensity**
and could not express "default on, default effort=high" or "default budget=262144". It
also encoded DeepSeek V4 as `hybrid` (a model *class*) rather than a default *state*,
which was imprecise and inconsistent with the other entries.

A separate question arose: the official Bailian page for GLM-5.2 lists the
`reasoning_effort` value *range* (none/minimal/low/medium/high/xhigh/max) but does **not**
state the default value when the parameter is omitted.

## Decision

**Record each model's default thinking behavior as a structured `thinking_default` object
in `config/model-token-limits.json`, surfaced via the pricing-snapshot endpoint's
`output_limits` block — not in capability-profiles.**

The object has four fields:

```json
"thinking_default": {
  "state": "on",
  "default_effort": "high",
  "default_thinking_budget": 131072,
  "default_effort_confirmation": "confirmed"
}
```

- **`state`** — `on` / `off` / `always_on` (cannot be disabled, e.g. Kimi K3) /
  `adaptive` (MiniMax self-decides). This is the default thinking state when no parameter
  is sent.
- **`default_effort`** — the official default `reasoning_effort` value (e.g. DeepSeek V4
  `high`, Kimi K3 `max`). `null` when the model has no effort dial (Qwen3, GLM budget
  path, MiMo, MiniMax).
- **`default_thinking_budget`** — the official default `thinking_budget` (usually equals
  the model's max thinking-chain length). `null` when the model has no budget concept.
- **`default_effort_confirmation`** — `confirmed` (official page states the default) or
  `cross_source_unconfirmed` (official page does not state it; inferred).

### Decision sub-points

1. **Structured object, not a string.** Two orthogonal dimensions (state + intensity)
   cannot fit one string without inventing a parseable mini-syntax, which is fragile and
   opaque. The object is validated by JSON Schema (`additionalProperties: false`) the same
   way every other frozen contract field is.

2. **GLM-5.2 default_effort = high, marked `cross_source_unconfirmed`.** The official page
   omits the default. Rather than record `null` (which would lose useful information) or
   guess silently, we infer `high` from the directly analogous DeepSeek V4 (same hybrid
   model class, same `reasoning_effort` parameter set, same `enable_thinking` priority
   rule) and **mark the inference explicitly**. The consumer sees both the value and that
   it is not page-confirmed, preserving the project's "未知不伪造" (do not forge the
   unknown) principle while still being useful.

3. **Kimi K3 corrected to `always_on` + `reasoning_effort=max` (Bailian-specific).** The
   Bailian-deployed kimi-k3 is a "仅思考模型" (thinking-only, cannot disable); its
   `reasoning_effort` accepts **only** `max` on Bailian — unlike Moonshot upstream
   (low/high/max). `enable_thinking` does not apply to k3. The earlier data incorrectly
   listed `enable_thinking` as a control and `thinking_only` as the state.

4. **Data lives in token-limits, not capability-profiles.** Capability-profile is a frozen
   contract (`additionalProperties: false` + Pydantic `extra="forbid"` + a guard test).
   Adding a structured field there is a full contract upgrade touching the Pydantic model,
   OpenAPI, and all 9 profile files. The default-thinking profile is (a) advertised facts
   from official pages, same provenance/lifecycle as price facts, and (b) already consumed
   alongside price in the pricing-snapshot endpoint. Putting it in token-limits keeps it
   with its natural consumer (the `output_limits` block) at a fraction of the contract
   cost. The per-model thinking *capability* (whether the model *can* think, observed vs
   advertised) stays in capability-profiles; the *default behavior* lives here.

5. **MiMo records `default_effort=null` and `default_thinking_budget=null`.** The official
   page explicitly lists `reasoning_effort` and `thinking_budget` as unsupported. Recording
   `null` (not omitting) makes "this model has no intensity control" a positive, auditable
   statement rather than an absence.

6. **A `recommended_defaults` block gives consumers a ready-to-use request recipe.** Beyond
   describing what the model *can* do (`thinking_controls`) and what it *defaults to*
   (`thinking_default`), each model carries a `recommended_defaults` object with three
   fields: `send` (reasoning params to set explicitly to reproduce the project's chosen
   default behavior — empty `{}` means "omit all reasoning params" for models whose
   upstream default already matches project policy), `omit` (params this model does NOT
   support and will 400 on, e.g. MiMo's `reasoning_effort`, kimi-k3's `enable_thinking`),
   and `required_non_reasoning` (non-reasoning params required for a valid request, e.g.
   `{"stream": true}` for kimi-k3). Consumers merge `send` + `required_non_reasoning` with
   their own `messages`/`model`/`max_tokens` and get a compliant request without reading
   per-model docs.

7. **qwen3-vl-plus `send` overrides its upstream default to force thinking on.** The
   official default for qwen3-vl-plus is `enable_thinking=false` (the only model in the set
   that defaults off). Project policy is "default thinking on where possible", so its
   `recommended_defaults.send = {"enable_thinking": true}`. Its `thinking_default.state`
   stays `off` to record the upstream's *actual* default honestly; the override lives in
   `recommended_defaults`, keeping "what the upstream does" and "what we recommend"
   separate and auditable.

## Consequences

- **Positive:** A consumer querying one endpoint gets the complete default-behavior
  picture (state + intensity + confirmation) needed to size `max_tokens`/`thinking_budget`
  per model without reading 9 different official pages.
- **Positive:** The GLM inference is transparent — the value is useful but its
  unconfirmed status is machine-readable (`default_effort_confirmation`), so downstream
  code can treat it differently if it wants hard guarantees.
- **Positive:** The structured object is covered by `snapshot_digest`, so any change to a
  model's recorded default produces a new digest (tamper-evident).
- **Negative:** Two frozen contracts changed (`model-token-limits.schema.json` +
  `pricing-snapshot-response.schema.json`), and the gateway needed a restart because the
  loader/dataclass code changed (not just the data). Future *content* edits to
  `model-token-limits.json` hot-reload without restart; only schema/code changes need one.
- **Negative:** `default_effort` values are free-form strings (`high`, `max`, ...) rather
  than an enum, because each vendor's dial has different ordinals (DeepSeek
  low/medium/high/xhigh/max vs GLM none/minimal/low/medium/high/xhigh/max vs Kimi max-only).
  An enum would either be a union (weak) or per-model (complex); the string + confirmation
  pair is the pragmatic choice.

## Alternatives considered

- **Single string with encoded syntax (e.g. `"on/high"`).** Rejected: opaque, no place for
  the confirmation flag, hard to validate, and cannot express budget-vs-effort duality.
- **`default_effort = null` for GLM (strict unknown).** Rejected: loses the useful
  inference. The explicit `cross_source_unconfirmed` flag is a better trade — useful but
  honest.
- **Add the field to capability-profiles.** Rejected: full frozen-contract upgrade
  (Pydantic + OpenAPI + 9 files) for data that is naturally co-consumed with price. See
  decision sub-point 4.
- **Record only state, omit intensity.** Rejected: the user's requirement was explicitly
  "默认开启思考，然后默认强度是多少" — both dimensions are needed for budgeting.

## Open items

- **GLM-5.2 default effort** remains `cross_source_unconfirmed` until confirmed via the
  Bailian console or a later official page update; then flip to `confirmed`.
- **MiniMax-M3 `max_output_tokens`** and DeepSeek V4 "共 393,216" semantics remain
  `cross_source_unconfirmed` at the model-level `confirmation` field (orthogonal to
  `default_effort_confirmation`, which is about the effort value specifically).
