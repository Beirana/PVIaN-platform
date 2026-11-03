# 透明聚合网关：运维、周期复核与 Agent 接入指南

可复制的启动器、消费者变量合同、验证顺序与清理模板见
[`gateway-standard-runbook.md`](gateway-standard-runbook.md)。本文继续作为 provider、qualification、轮换和受控部署的详细运维说明。

## 多模型扩展入口

新增 provider/model、补能力表或把候选 alias 接入网关时，统一从
[`model-extension-runbook.md`](model-extension-runbook.md) 开始，并使用仓库 Skill
`$pvian-model-onboarding`。该流程要求先完成离线 profile/pricing/capability/schema，
再向用户列出精确的 provider completion 调用次数并等待授权；provider probe、forwarder
smoke 与已部署网关 smoke 必须分别保存证据。

requested model 与 provider reported model 不一致时不得由 Agent 自行“纠正”。必须暂停
admission，向用户展示请求值、返回值、catalog/官方来源及风险并取得明确决定。获准接受时只允许
写入单一、版本化的映射对；禁止通用 strip-prefix、模糊匹配或跨厂商归一化。

2026-08-09 的百炼八模型能力 sweep 与隔离 5220 验证见
[`reviews/bailian-multimodel-gateway-live-2026-08-09.md`](reviews/bailian-multimodel-gateway-live-2026-08-09.md)
（早期的离线就绪审查见
[`reviews/bailian-multimodel-offline-readiness-2026-08-09.md`](reviews/bailian-multimodel-offline-readiness-2026-08-09.md)）。
5220 是独立验证实例，不等于修改或重启现有 5200，也不能以临时 state 代替正式部署审批。

## Local usage estimate MVP

`config/pricing/` is offline and versioned. The gateway keeps response bytes
and `x-pvian-cost: unknown` unchanged, then after a complete JSON or SSE body
stores only token summary fields and its local estimate beside qualification
state. Missing, malformed, or impossible usage remains unknown, never zero.

Advertised model token ceilings (max output incl. reasoning tokens, context
window, max thinking-chain length, thinking controls, default thinking behavior,
and a ready-to-use `recommended_defaults` request recipe) live in
`config/model-token-limits.json` and are exposed via the pricing-snapshot
endpoint's `output_limits` block. They are static capability facts from official
pages, not runtime usage; reasoning tokens count toward `max_output_tokens` on
thinking models. The `recommended_defaults` block (send/omit/required_non_reasoning)
lets consumers assemble a compliant request per model without reading each official
page. See
[`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md), the audit
[`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md),
and the design decision
[`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md).
Both pricing and token-limits hot-reload on next query; code changes still need
one gateway restart.

## Flash 0731 conditional alias admission

The active Bailian Flash 0731 profile is bound to the exact public alias
`pvian/bailian-cn-beijing/deepseek-v4-flash-0731`. Set `PVI_GATEWAY_STATE_FILE`
to the existing redacted qualification state before startup; the alias is exposed
only while its exact route is enabled/nominal and within TTL. Missing, candidate,
degraded, or expired state automatically removes it from `/v1/models`; renewal
requires a separately authorized qualification. Yunwu Flash 0731 remains
candidate-only with no alias.

For every formal round, operators must choose one alias beforehand. Automatic
routing, fallback, retry, and substitution remain prohibited. Gateway request and
logical-call IDs are opaque trace values; they must not be forwarded upstream.
Usage remains only in the provider-native response body, while cost is `unknown`.

本指南面向维护者、自动化 Agent 与下游平台使用者。网关的职责是以显式 `route_id` 透明转发
已资格认证的 OpenAI-compatible 请求；它不是自动选路器，也不是 PVI 验收 B Runtime。

## 当前访问边界

当前实现已把 Yunwu、百炼等 **provider API Key 集中在网关进程**，下游项目不需要复制这些
密钥。除 `/healthz` 和 `/readyz` 外，所有网关 API 都要求独立的 `Authorization: Bearer`；下游以
`/v1/models` 返回的 public alias 调用模型，不需要 `X-PVIAN-Route-ID`。显式 route 仅是受相同
Bearer 保护的内部诊断基准。网关仍必须只绑定 `127.0.0.1` 或受控私网，并由反向代理/网络策略隔离。
详见 [`gateway-consumer-handoff.md`](gateway-consumer-handoff.md)。

### Client key 轮换

`config/gateway-client-keys.json` 只保存 `key_id`、`active/revoked` 与环境变量名；值只由本机
`.env` 或 secret manager 注入。网关启动后只以 SHA-256 digest 和 constant-time 比较验证 Bearer。
每个 client key 必须至少 32 个 UTF-8 bytes；可由受控终端使用 `secrets.token_urlsafe(32)` 生成，
但不得把生成值粘贴进 Git、命令历史或报告。
轮换时先新增一个 `active` 条目并部署其变量，验证新 key，再把旧条目标为 `revoked`。如需让旧 key
明确得到 403，在撤销窗口内仍保留该旧变量的本机值；绝不能将值写入 Git、命令参数、日志或证据。
registry 和环境变量在进程启动时加载；轮换配置生效需要受控重启网关，并在重启后检查 `/readyz` 与
经 Bearer 的 `/v1/models`。

## 一次性接入一个新 Provider / 联合网关

1. 在 `.env` 只加入独立的 API Key 环境变量名和 base URL；不要加入模型 ID、thinking 参数或
   fallback 配置。API Key 不得提交、打印或写进 profile。
2. 在 `config/provider-profiles/<provider>.json` 新增版本化 profile：冻结允许主机、`/v1`、
   transport、目录发现规则、模型候选正则、请求变体与启用条件。
3. 为 profile 补离线解析/脱敏测试；没有通过测试不得进入真实调用。
4. 人工执行资格验证。目录找不到候选模型时只记录未发现，不替换为近似模型。
5. 审核 `var/gateway/qualification-state.json` 的脱敏摘要；状态为 `enabled` 后，下游才可发现
   相应 route。

第一批 profile：

- `provider-yunwu-openai-compatible`
- `provider-bailian-cn-beijing`

## 资格状态与有效期

| 状态 | 含义 | 是否可调用 |
|---|---|---|
| `candidate` | 未通过必要基线或尚未验证 | 否 |
| `enabled / nominal` | 已通过所需基线，资格未过期 | 是 |
| `enabled / degraded` | 连续五次分类运行失败，提前复核到期 | 否 |
| `enabled` 但 `valid_until` 已过 | 7 天（168h）资格窗口结束 | 否 |

默认策略由 profile 的 `qualification.enablement` 冻结：有效期 7 天（168h），连续失败阈值 5。
一次 429、5xx、timeout 或 transport error 只是一条运行观测；它不会单独宣告模型不支持。

2026-07-31 的 gzip 修复后短 burst 观察显示：百炼 route 5/5 HTTP 200，Yunwu route 3/5 HTTP 200
且有 2 次真实 429。该观察只用于当前人工 route 选择（优先百炼、Yunwu 显式备用/对照），
绝不启用自动 fallback。详情见
[`reviews/gateway-short-burst-2026-07-31.md`](reviews/gateway-short-burst-2026-07-31.md)。

## 手动与周期验证

手动验证某家 provider：

```powershell
pvi-probe qualify --profile-id provider-yunwu-openai-compatible --allow-network --confirm-cost
```

仅验证到期或五连败后需复核的 route：

```powershell
pvi-probe qualify --due --allow-network --confirm-cost
```

建议由 Windows Task Scheduler、cron 或部署平台每天执行一次第二条命令，并把退出码、时间、
route 数量与状态摘要发送到既有运维渠道。不要把该命令放入 GitHub Actions：CI 保持无密钥、
无网络、无付费调用。当前不设置费用上限；仅记录可获得的 usage 计数，费用治理需另立 ADR。

## 下游 API 使用

发现已资格认证的 public alias：

```text
GET http://127.0.0.1:5200/v1/models
```

透明调用示例：

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "Authorization" = "Bearer <gateway-client-key>"
}
$body = '{"model":"pvian/bailian-cn-beijing/deepseek-v4-pro","messages":[{"role":"user","content":"hello"}]}'
Invoke-WebRequest -Method Post -Uri "http://127.0.0.1:5200/v1/chat/completions" -Headers $headers -Body $body
```

public alias facade 只把 JSON 顶层 `model` 改为其冻结的精确 upstream model，并以 response headers
披露 alias 与 route；因此它是**字段级语义保真**而非请求 JSON 原始字节保真。其余 messages、tools、
reasoning 参数、tool-call ID/arguments、assistant/tool replay 均不得修改。对于 HTTP/SSE response body，
网关按上游字节序列透传。裸 upstream model 若存在多个 alias 会返回 `409 model_is_ambiguous`。

### 压缩合同

网关固定向上游发送 `Accept-Encoding: identity`，不转发下游的压缩偏好。这是当前 MVP 的
显式传输规范：可避免 raw stream 与遗漏的 `Content-Encoding` 组合导致普通 httpx/OpenAI
消费者无法解码 JSON 或 SSE。若某个上游仍返回压缩 body，网关会保留其 `Content-Encoding`
响应头；不转发 `Content-Length` 或 `Transfer-Encoding`，由本地流式响应正确处理。

## 受控内网部署与恢复

1. 网关默认通过 Docker Compose 或 `pvian-gateway` 绑定 loopback；保持 `127.0.0.1:5200`，不要直接
   映射为公网端口。
2. 如需内网访问，使用受控反向代理终结 TLS，并采用
   [`../deploy/nginx/pvian-gateway.conf.example`](../deploy/nginx/pvian-gateway.conf.example) 作为起点。
   证书路径、内部 DNS 和网络 ACL 由部署环境提供，不写入仓库。
3. 健康检查使用无鉴权的 `/healthz`；`/readyz` 在至少一个 active gateway client key 本机可用时返回
   200。模型是否可调用以经 Bearer 的 `/v1/models` 为准。
4. 停止后先保护 `var/gateway/qualification-state-flash-0731.json`（其仅含脱敏状态）；恢复服务后不要自动联网，
   仅在到期或五连败时由 Task Scheduler 任务 `PVIaN-Requalify-Daily`（04:00）通过
   `scripts/requalify_due.py` 执行 `pvi-probe qualify --due --allow-network --confirm-cost`。
   详见 `docs/gateway-infra-runbook.md §7`。
   网关与 qualifier 共享该文件时，还会使用相邻的
   `qualification-state.json.lock` 跨进程互斥；两个文件必须在同一可写、支持 Windows/POSIX 文件锁的
   本机卷或等价共享卷。不要把它们放在不保证锁语义的多节点网络文件系统。
5. 访问日志必须排除 Authorization 和 body；网关本身不记录 request/response/thinking/远端错误正文。

## Agent 变更清单

Agent 对新增 provider 或模型的 PR 必须包含：

- profile 的精确身份、允许主机和模型候选规则；
- 不读取/打印/提交 API Key 的测试；
- 直连资格验证的脱敏结果及时间窗；
- 如接入网关，直连与网关的 fields/SSE/tools/replay 差分结果；
- 不将一次失败、reported model 或短测写成长期能力、上游身份或 SLA；
- 若要新增自动路由、fallback、费用策略、重试或原始内容留存，先新增 ADR。
