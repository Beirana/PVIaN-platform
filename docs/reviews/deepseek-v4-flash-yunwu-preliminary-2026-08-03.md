# Yunwu DeepSeek V4 Flash preliminary qualification - 2026-08-03

## Result

| Field | Observation |
|---|---|
| Provider | Yunwu |
| Exact catalog model | `deepseek-v4-flash` |
| Catalog discovery | Present |
| Qualification result | `candidate`; discovered but calls failed |
| Message / stream | Both `inconclusive` with provider 5xx |
| Thinking variants | Baseline, `enable_thinking=true`, `reasoning_effort=high`, and `max` all failed with provider 5xx |
| Tool and replay | `inconclusive` with provider 5xx |
| Reported model / usage | Not returned |
| Public alias | None |

The provider catalog currently exposes the exact model ID, so this differs from
`deepseek-v4-flash-0731`, which was `not_discovered` on Yunwu. The observed result is
`discovered_but_call_failed`; it is not evidence that the model's scientific capabilities
are unsupported. No nearby model was substituted, and no D/S/P experiment call was made.

The redacted qualification state is stored locally in
`var/gateway/qualification-state-yunwu-deepseek-v4-flash.json`. No credential, raw prompt,
response, reasoning text, or remote error body is recorded here.
