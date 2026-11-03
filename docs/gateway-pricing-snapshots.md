# Gateway Pricing Snapshots

`GET /v1/gateway/pricing/snapshots` — versioned, self-describing pricing facts
for the experiment side. The gateway provides **price facts only**; it does
not decide call/token/CNY budgets for experiments.

**相关文档**：消费者首次接入看 [快速上手](gateway-quickstart.md)；完整调用流程与变量边界见
[标准 Runbook](gateway-standard-runbook.md) §7；价格与 usage 的消费者侧约定见
[消费者集成](consumer-integration.md)；架构交接与时间窗观察见
[消费者交接](gateway-consumer-handoff.md)。价格 schema 见
`contracts/schemas/local-pricing-v2.schema.json` 与
`contracts/schemas/pricing-snapshot-response.schema.json`；模型 token 上限 schema 见
`contracts/schemas/model-token-limits.schema.json`，数字来源与推理 token 计费规则见
[`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)。

## Endpoint

```
GET /v1/gateway/pricing/snapshots?model_alias=...&provider=...&region=...&model=...
```

All parameters are optional. Requires `Authorization: Bearer <gateway-client-key>`.

| Parameter | Description |
|---|---|
| `model_alias` | Exact public alias (e.g. `pvian/bailian-cn-beijing/glm-5.2`). Returns the single snapshot bound to that alias. 404 if unknown. |
| `provider` | Filter by provider (e.g. `bailian`). |
| `region` | Filter by region (e.g. `cn-beijing`). |
| `model` | Filter by upstream model (e.g. `glm-5.2`). |

Returns `{"items": [...]}`. Each item is a pricing snapshot (see below).

## Response fields

| Field | Description |
|---|---|
| `schema_version` | Always `pvian.pricing-snapshot-response/v1`. |
| `snapshot_id` | Deterministic id derived from `(provider, region, upstream_model, pricing_version)`. Any price change naturally produces a new id. |
| `snapshot_digest` | SHA-256 of canonical JSON of everything except itself. Lets the experiment side re-verify integrity after persisting. |
| `provider`, `region`, `currency` | Price identity. |
| `public_alias`, `route`, `upstream_model` | Alias join. `null` if no public alias binds to this pricing. |
| `profile_id`, `model` | Profile and model identity. |
| `rates` | First-tier rates: `{prompt, cached_prompt, completion, unit}`. `cached_prompt` is `null` when unknown. |
| `tiers` | Full tier array (preserves multi-tier capability). |
| `cache_mode` | `explicit_cache` if a cached rate exists; `no_cache_rate` otherwise. |
| `pricing_version`, `effective_at`, `captured_at` | Versioning and provenance timestamps from the pricing file. |
| `valid_until` | `null` — the price is valid until superseded by a new `snapshot_id`. Expiry requires human/Agent verification of the source page. |
| `source_urls` | Source page URLs (from `source.url` in the pricing file). |
| `source_digests` | Content-level digests if available (`content_sha256`). Often empty — dynamic pages that cannot be archived have `content_sha256: null`. |
| `output_limits` | Advertised model output/context ceilings (nullable object, see below). `null` when no token limits are bound to this model. |

## output_limits（模型最大输出 Token）

`output_limits` 是模型的**advertised 能力事实**（静态上限，来自官方页面），不是单次调用的
usage 统计——后者从 `resp.usage` 读（思考模型的 `completion_tokens` 已含 `reasoning_tokens`）。
两套数据性质不同，不要混用。

每个 `output_limits` 对象包含：

| 字段 | 含义 |
|---|---|
| `context_window` | 上下文长度（输入侧硬上限）。 |
| `max_output_tokens` | **最大输出长度——同时容纳推理 token 与真实消息 token**（见下方规则）。 |
| `max_thinking_chain_tokens` | 最大思维链长度；`null` 表示该模型思考折叠进输出、无独立思维链上限（如 DeepSeek V4）。 |
| `output_parameter` | 百炼 OpenAI-compatible endpoint 实际生效的输出参数名（`max_tokens`）。 |
| `thinking_default` | 对象，描述不发 reasoning 参数时的上游默认行为：`state`（`on`/`off`/`always_on`/`adaptive`）+ `default_effort`（官方默认 reasoning_effort 档位，如 DeepSeek V4=high、Kimi K3=max；无档位的模型为 null）+ `default_thinking_budget`（官方默认 thinking_budget，通常=最大思维链长度；无 budget 概念为 null）+ `default_effort_confirmation`（`confirmed`=官方明说，`cross_source_unconfirmed`=官方未明说、按同体系推测，仅 GLM-5.2）。字段设计决策见 [`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md)。 |
| `thinking_controls` | 该模型支持的思考控制参数（`enable_thinking` / `thinking_budget` / `reasoning_effort` / `thinking.type`）。 |
| `recommended_defaults` | **拿来即用的请求参数配方**（对象）。`send`：为复现项目默认行为应显式发的 reasoning 参数（多数模型为 `{}`，即全省略即得上游默认；`qwen3-vl-plus` 为 `{enable_thinking:true}` 以强制开思考）。`omit`：该模型**不支持**、发了会 400 的参数（如 MiMo 的 `reasoning_effort`、Kimi k3 的 `enable_thinking`）。`required_non_reasoning`：非 reasoning 类必发参数（如 `kimi-k3` 的 `{stream:true}`）。消费者把 `send` + `required_non_reasoning` 合并进自己的 `messages`/`model`/`max_tokens` 即可发出合规请求。设计决策见 [`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md)。 |
| `confirmation` | `confirmed` = 官方卡片直接列出；`cross_source_unconfirmed` = 官方卡片未列、跨源推断（如 MiniMax-M3 / DeepSeek V4），需控制台复核。 |
| `source_url` | 该模型官方卡片 URL。 |
| `notes` | 关键语义注记（如 Kimi 默认 131072、DeepSeek 不支持 thinking_budget）。 |

**推理 token 计费规则**（来自官方 deep-thinking 页 usage 示例）：`reasoning_tokens` 是
`output_tokens` 的子集，即 `max_output_tokens` 上限**同时容纳思考与最终回复**；`thinking_budget`
仅在共享输出池内对推理部分做二次上限，默认取 `max_thinking_chain_tokens`。`max_tokens` 超过
`max_output_tokens` 会返回 HTTP 400。各模型差异（独立思维链上限 vs 折叠进输出）见审查文档
[`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)。

`output_limits` 数据来自 `config/model-token-limits.json`（独立于 pricing 文件，schema 见
`contracts/schemas/model-token-limits.schema.json`），与价格同源同生命周期——官方改卡片两者一起变。
该字段纳入 `snapshot_digest` 完整性校验；hot-reload 行为同价格（见下节）。`output_limits` 缺失时
为 `null`，价格事实仍正常返回（limits 缺失不是硬失败）。

## Hot reload

Editing `config/pricing/*.json` takes effect on the **next request** to this
endpoint — no gateway restart needed. The endpoint checks file mtimes and
reloads pricing if any changed. `config/model-token-limits.json` follows the
same mtime-triggered hot-reload. If a reload fails (e.g. a half-written file),
the last-known-good pricing/limits are preserved.

The `/v1/chat/completions` cost-estimation path continues to use the
in-memory cache for performance; only this endpoint triggers hot reload in
this version.

## Gateway boundary

The gateway provides **price facts only**:

- It does **not** decide or enforce call/token/CNY budgets.
- It does **not** fetch or refresh prices from the web — pricing is updated
  by a human or Agent editing `config/pricing/*.json` (the endpoint then
  hot-reloads).
- It does **not** verify content-level source hashes at runtime —
  `source_digests` honestly reports what was captured.

## Experiment-side obligations

1. **Persist the snapshot verbatim** and bind it to its `snapshot_digest`.
2. **Never modify a historical snapshot** — price changes produce a new
   `snapshot_id`; old snapshots remain valid for auditing past runs.
3. **Budget decisions are yours** — the gateway gives you price facts; how
   to bound calls/tokens/CNY is an experiment-level decision.

## snapshot_id semantics

`snapshot_id` is `snap-{provider}-{region}-{upstream_model}-{pricing_version}`.
The same price identity always yields the same id. When the pricing file's
`pricing_version` changes (e.g. a rate update), the new price facts produce a
new `snapshot_id` and a new `snapshot_digest`. Historical snapshots are never
modified in place — they are immutable price facts.
