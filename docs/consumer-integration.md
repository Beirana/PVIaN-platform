# Consumer Integration

从启动网关到向消费者子进程注入变量的规范流程见
[`gateway-standard-runbook.md`](gateway-standard-runbook.md)。

模型扩展、能力字段解释、alias/身份映射和授权 probe 流程见
[`model-extension-runbook.md`](model-extension-runbook.md)。消费者不得维护一份脱离网关状态的
“固定可用模型列表”；每次运行前应经 Bearer 调用 `/v1/models`，只使用当时返回的精确 public
alias。provider key 永远不下发，gateway client key 也不得写入仓库、日志或运行证据。

`requested_model != reported_model` 不是消费者可自行忽略的显示差异。只有网关已有经用户明确确认、
版本化且精确到单一模型对的 identity mapping 时，该 alias 才可调用；否则必须 fail closed。

## Usage-cost visibility

Consumers must treat `x-pvian-cost: unknown` as the only response-header cost
signal. The local pricing estimate and redacted attempt ledger are controlled
operator state, not a consumer endpoint, invoice, or budget API.

## 集成原则

消费者在模型调用前向 PVI 查询能力，并把 `endpoint_id + profile_id + profile_version` 写入自己的 run provenance。模型调用仍由消费者原 SDK、网关或 benchmark adapter 发出。

透明网关是独立的可选路径：它可使下游不再持有 provider API Key。下游使用 gateway Bearer key 和
`/v1/models` 返回的 public alias；显式 `X-PVIAN-Route-ID` 只保留为受相同 Bearer 保护的诊断路径。
它仍仅适用于本机/受控内网，不能把它当作公网、多租户模型 API。完整合同见
[`gateway-consumer-handoff.md`](gateway-consumer-handoff.md)。

```text
capability check: consumer -> PVI
model execution:  consumer -> existing model path
```

消费者不得导入 `pvi.domain`，也不得共享 PVI 数据目录或数据库。

## 最小流程

1. 平台已有配置提供精确 `endpoint_id` 与 requested model。
2. 调用 `POST /v1/capability-queries:resolve`。
3. 展示 satisfied / unsatisfied / unknown；平台自己的策略决定继续、警告或阻止。
4. 保存实际返回的 profile/version/data_version。
5. 通过原调用路径运行模型。

## 故障分类

| 情况 | 建议平台行为 |
|---|---|
| 正常结果 | 保存 provenance，继续原路径 |
| unknown | 显示未知项；由平台策略决定 |
| unsupported | 显示不满足；由平台策略决定 |
| 404 | 标记 profile/endpoint 配置错误 |
| 超时/断连 | 标记 capability check unavailable |
| Schema 不兼容 | 明确兼容性错误，不静默接受 |

`examples/platform-pvi-client.py` 演示 `warn` 型降级：即使 PVI 不可用，原模型调用函数仍执行。真实平台接入必须在提供其仓库并确认技术栈后实施。

## 配置建议

```env
PVI_ENABLED=true
PVI_BASE_URL=http://pvi-capability:5100
PVI_TIMEOUT_SECONDS=3
PVI_FAILURE_MODE=warn
```

## Flash 0731 callable alias notice

`deepseek-v4-flash-0731` is now an exact callable Bailian public alias
(`pvian/bailian-cn-beijing/deepseek-v4-flash-0731`), admitted conditionally on its
local qualification state and TTL. It is not a promise of permanent availability:
the alias appears in `/v1/models` only while its exact route is enabled/nominal
and within TTL. Missing, candidate, degraded, or expired state automatically
hides it. Consumers must not substitute a nearby model or rely on automatic
fallback; before each formal round, the runner must explicitly select one visible
alias. Yunwu Flash 0731 remains candidate-only with no alias.

Gateway responses may carry an opaque `X-PVIAN-Request-ID` and, when a validated
`X-PVIAN-Logical-Call-ID` was supplied, that correlation value. These are gateway
trace headers only and are never sent upstream. Response `usage` remains native
body content; cost is explicitly `unknown` and is neither calculated nor stored.

这些是消费者配置，不是 PVI 服务配置。对历史直连路径，模型 API Key 仍由消费者管理，
不得发送给 Capability Service。对透明网关路径，provider API Key 由网关进程管理且不得下发给
消费者；消费者只持有独立的 gateway client API Key。

## 网关薄客户端合同

```env
PVIAN_GATEWAY_BASE_URL=http://127.0.0.1:5200
PVIAN_CLIENT_KEY=<gateway-client-key>
PVIAN_MODEL=pvian/bailian-cn-beijing/deepseek-v4-pro
```

``PVIAN_GATEWAY_BASE_URL`` 固定为不带 ``/v1`` 的网关根地址；直接 HTTP 客户端追加 ``/v1/...``，OpenAI SDK 初始化时追加一次 ``/v1``。Python 调用只需将 gateway key 作为 Bearer，模型填 public alias。不要把 provider API Key、
`X-PVIAN-Route-ID` 或裸 `deepseek-v4-pro` 放入业务侧配置；相同裸模型出现在多个 provider 时网关会
返回 `409 model_is_ambiguous`，不会静默改选上游。
