# PVIaN 网关：面向测试与下游调用的交接说明

> 适用对象：后续规划/实施 Agent、`misalignment-platform` 与其他下游项目维护者。
> 本文描述的是 2026-07-31 的实际状态，不能把目标合同当成已实现功能。

> 2026-08-09 多模型扩展后的当前使用入口是
> [`model-extension-runbook.md`](model-extension-runbook.md)；百炼八模型实测状态见
> [`reviews/bailian-multimodel-gateway-live-2026-08-09.md`](reviews/bailian-multimodel-gateway-live-2026-08-09.md)。
> 本文后续的 Flash/早期 route 表仅作为历史时间窗记录，不应覆盖当前 `/v1/models` 结果。

> 2026-08-13 起新增版本化价格快照端点 `GET /v1/gateway/pricing/snapshots`：实验侧获取可审计、
> 带 digest 的价格事实的当前入口是
> [`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md)。该端点**需重启网关一次后生效**，
> 之后修改 `config/pricing/*.json` 无需重启（热重载）。本轮同时覆盖 9 个百炼模型的 prompt/cached/completion 费率。
> 同日（2026-08-13）该端点响应新增 `output_limits` 块：每个模型的 advertised 最大输出 token 上限
> （**含推理 token**）、上下文窗口、最大思维链长度、思考控制参数，以及 `recommended_defaults`（拿来即用的
> 请求参数配方：该发/不该发哪些 reasoning 参数、是否必须 stream）。`thinking_default` 进一步结构化为
> 「默认状态 + 默认强度」对象。数字来源与推理 token 计费规则见
> [`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)，
> 字段设计决策见 [`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md)；
> 该改动为代码变更，**同样需重启网关一次**才生效。

新的调用方应从当前权威的
[`gateway-standard-runbook.md`](gateway-standard-runbook.md) 开始；本文保留架构交接与时间窗观察。

## Usage-cost visibility

The gateway preserves `x-pvian-cost: unknown` for consumers. A versioned local
price estimate may be written to controlled operator state after a response
finishes; it is not exposed as a consumer API, invoice, or budget decision.

实验侧如需可审计的价格事实，调用 `GET /v1/gateway/pricing/snapshots`（同样要求 Bearer）。
它返回每个绑定身份的版本化快照，含确定性 `snapshot_id` 与 `snapshot_digest`（规范化 JSON 的
SHA-256），支持 `model_alias`/`provider`/`region`/`model` 过滤；未知 alias 返回 404。端点只提供
**价格事实**，不决定预算、不联网刷新价格。快照同时携带 `output_limits` 块——每个模型的 advertised
最大输出 token 上限（**含推理 token**）、上下文窗口、最大思维链长度、思考控制参数、结构化默认推理行为
（`thinking_default`），以及 `recommended_defaults`（拿来即用的请求参数配方：`send`/`omit`/
`required_non_reasoning`，合并进 `messages`/`model`/`max_tokens` 即可发合规请求）；该块纳入
`snapshot_digest` 完整性校验。修改 `config/pricing/*.json`
或 `config/model-token-limits.json` 后下次查询自动热重载，无需重启网关。完整字段、热重载边界与实验侧
落盘义务见 [`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md)；数字来源见
[`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)。
该端点首次需重启网关一次后生效。

价格相关合同已扩展：`contracts/schemas/local-pricing-v2.schema.json`（v2 费率结构）、
`contracts/schemas/pricing-snapshot-response.schema.json`（端点响应，含 `output_limits`）与
`contracts/schemas/model-token-limits.schema.json`（模型 token 上限数据）为定价与能力上限数据提供可验证约束。

## Flash 0731 conditional alias admission

`pvian/bailian-cn-beijing/deepseek-v4-flash-0731` is an exact Bailian public
alias, not a promise of permanent availability. Operators must point
`PVI_GATEWAY_STATE_FILE` at the existing redacted qualification state; `/v1/models`
lists the alias only while that matching route is enabled/nominal and within its
qualification TTL. Missing, candidate, degraded, or expired state hides it.
Yunwu Flash 0731 remains candidate-only and has no alias. Formal rounds select one
visible alias explicitly; the gateway performs no automatic fallback. 两份脱敏资格
证据：百炼
[`reviews/deepseek-v4-flash-0731-provider-qualification-2026-08-03.md`](reviews/deepseek-v4-flash-0731-provider-qualification-2026-08-03.md)
与 Yunwu 初步
[`reviews/deepseek-v4-flash-yunwu-preliminary-2026-08-03.md`](reviews/deepseek-v4-flash-yunwu-preliminary-2026-08-03.md)。

Only a separately authorized real qualification with `--allow-network` and
`--confirm-cost` can justify changing production profile or alias configuration.
The gateway preserves native response bodies, exposes cost as `unknown`, and keeps
its request/logical-call provenance headers out of upstream requests.

## 一句话结论

**Provider 密钥集中化与统一的下游网关凭证合同均已实现。**

目前可用的是一个本机/受控内网的透明网关：网关机器持有 Yunwu、百炼等 provider API Key，
下游只需网关 Base URL、独立 gateway Bearer key 和唯一 public model alias，不需持有 provider API Key。
它仍不是公网、多用户或跨不可信网络的统一 API 服务。

## 已实现的测试与调用闭环

1. `config/provider-profiles/*.json` 冻结 provider、host、base URL path、transport、模型候选、
   thinking 变体、24 小时有效期与五连败阈值。
2. `pvi-probe qualify --profile-id <id> --allow-network --confirm-cost` 先发现实时目录，再进行
   合成 message、SSE、thinking、native tool call、assistant/tool-result replay 资格验证。
3. 结果只写入 Git 忽略的脱敏资格状态文件；`enabled` 且未过期的 route 才由网关放行。
4. 下游通过受 Bearer 保护的 `GET /v1/models` 发现可调用 public alias，再向
   `POST /v1/chat/completions` 发请求。
5. 网关在内部注入正确的 provider API Key，并保留 request body 与 HTTP/SSE response body；
   不记录原始 prompt、response、reasoning 或远端错误正文。
   网关固定请求上游 `Accept-Encoding: identity`；下游可以使用普通 httpx/OpenAI SDK，
   不需要额外设置该 header。

当前第一批实测 route（下表是**初次 qualification 的时间窗快照**；它不覆盖后续下游真实接入观察）：

| route_id | 精确 model | 当前资格结论 |
|---|---|---|
| `yunwu:deepseek-v4-pro` | `deepseek-v4-pro` | 初次 qualification 中 message、SSE、reasoning 已观察；该次 tool/replay 受限流，结论保持 inconclusive。 |
| `bailian-cn-beijing:deepseek-v4-pro` | `deepseek-v4-pro` | message、SSE、thinking、native tool、assistant replay、tool-result replay 已观察。 |

这些是特定时间窗的观察，不是 SLA，也不证明实际权重来源。

下游真实接入测试还确认：缺 route、未知 route、model/route 不一致均在网关本地拒绝；百炼的
non-stream、SSE、tool/replay 与 thinking+tool auto 通过；Yunwu的 thinking+tool/replay 通过。
百炼 `thinking=true + 强制 function tool_choice` 返回 400，因此联合 thinking/tool 的 profile
合同应使用 `tool_choice=auto`，baseline non-thinking 才可强制 function。

最新的 gzip 修复后短 burst（每 route 5 次、无重试）见
[`reviews/gateway-short-burst-2026-07-31.md`](reviews/gateway-short-burst-2026-07-31.md)：
百炼为 5/5 HTTP 200，Yunwu 为 3/5 HTTP 200 与 2 次真实 429。当前真实运行由人工显式选择
百炼 route；Yunwu 仅作为显式备用/对照，网关不得自动 fallback。

## 当前下游调用方式

```text
Base URL: http://127.0.0.1:5200
认证：Authorization: Bearer <gateway-client-key>
模型：/v1/models 返回的 public alias
路由：无需提供；X-PVIAN-Route-ID 仅限内部诊断，且同样要求 Bearer
```

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "Authorization" = "Bearer <gateway-client-key>"
}
$body = '{"model":"pvian/bailian-cn-beijing/deepseek-v4-pro","messages":[{"role":"user","content":"test"}]}'
Invoke-WebRequest -Method Post -Uri "http://127.0.0.1:5200/v1/chat/completions" -Headers $headers -Body $body
```

## 当前统一消费者合同

下游最终应只配置：

```text
PVIAN_GATEWAY_BASE_URL=https://<internal-gateway>
PVIAN_CLIENT_KEY=<gateway-client-key>
PVIAN_MODEL=pvian/bailian-cn-beijing/deepseek-v4-pro
```

其中 `public-model-alias` 必须一对一指向一个已资格认证 route，例如：

```text
pvian/yunwu/deepseek-v4-pro
pvian/bailian-cn-beijing/deepseek-v4-pro
```

网关在内部把该 alias 映射为精确 `route_id + upstream model`，并在 response headers 返回 alias 与
route。`/v1/models` 返回 profile/version、有效期和能力摘要。`deepseek-v4-pro` 这种裸模型名在多个
provider 都存在时会拒绝为 `model_is_ambiguous`，绝不静默选路、fallback 或按健康度替换。

`examples/gateway-openai-client.py` 是最小 OpenAI Python SDK 调用示例。`misalignment-platform` 的薄客户端、
mock 与回归测试仍须在其仓库实施；TLS、反向代理、日志脱敏与网络隔离由本仓库提供样例和合同，但需按
部署环境落实。

## 给规划/实施 Agent 的工作边界

- 不要把 Capability Service 当作网关；它仍无 provider 凭证、无出站调用。
- 不要把透明网关写成验收 B Runtime，或声称已实现自动路由/长期 SLA。
- 不要向下游项目复制 Yunwu、百炼、OpenRouter 等 provider API Key。
- 不要因同一裸 model ID 在多个 provider 出现而自动择一；必须使用唯一 public alias。
- 周期资格复核通过 `scripts/requalify_due.py`（封装 `pvi-probe qualify --due --allow-network --confirm-cost`）
  由 Windows Task Scheduler 任务 `PVIaN-Requalify-Daily`（每天 04:00）自动执行；
  CI、网关启动和普通下游调用均不得自动触发付费验证。详见 `docs/gateway-infra-runbook.md §7`。
