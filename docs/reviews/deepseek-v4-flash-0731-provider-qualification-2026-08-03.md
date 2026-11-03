# DeepSeek V4 Flash 0731 provider qualification — 2026-08-03

## Scope and handling

This is a redacted, time-bounded qualification record for the exact model ID
`deepseek-v4-flash-0731`. It contains no credential, prompt, response, or reasoning text.
It does not establish upstream weight provenance, price, SLA, automatic routing, fallback,
or automatic routing/fallback. Yunwu remains `candidate`; the Bailian exact
profile is active solely for conditional public-alias admission.

| Provider | Exact model | Result | Key observation | Capability-table conclusion |
|---|---|---|---|---|
| Yunwu | `deepseek-v4-flash-0731` | `not_discovered` | Live catalog produced `results=[]`; the exact ID was absent and no chat request was sent. | Keep all model capabilities `unknown`/`not_tested`; no substitution and no alias. |
| Bailian CN Beijing | `deepseek-v4-flash-0731` | `enabled` | requested and reported IDs matched; message, stream, thinking, native tool, assistant replay, and tool-result replay were all `observed_supported`. | The directly observed message, streaming, reasoning, tool, and usage capabilities are effective `supported` only within the current qualification window. |

## Bailian qualification state

The successful Bailian result is bound to route
`bailian-cn-beijing:deepseek-v4-flash-0731`, profile
`provider-bailian-cn-beijing-deepseek-v4-flash-0731` version `0.1.0`, and
configuration digest `sha256:a6f394cb2687d6b0d039a4971f2994511b17389c44ff9eb23861bc653cf53e97`.
It was observed and qualified at `2026-08-03T06:21:24.472210Z`; its current
validity window ends at `2026-08-04T06:21:24.472210Z`.

The first Bailian attempt did not write qualification state because the legacy Pro
state structure was incompatible. A rerun against the independent Flash state file
completed successfully. That state file remains an operational artifact and is
not committed; this report records only its redacted summary.

## Admission boundary

Yunwu's catalog result is `not_discovered`, not a claim of unsupported behavior.
Bailian's enabled qualification is a time-specific direct observation, not an SLA.
The exact alias `pvian/bailian-cn-beijing/deepseek-v4-flash-0731` is registered,
but the gateway must read this operational state through `PVI_GATEWAY_STATE_FILE`.
It exposes the alias only while the route is enabled/nominal and within its TTL;
missing, candidate, degraded, or expired state hides it from `/v1/models`.
Yunwu has no Flash 0731 alias. No automatic fallback or model substitution is
permitted.
