# Moonshot 官方与 OpenRouter 免费层接入记录（2026-08-17）

> 本文档是统一批次（deepseek-official + moonshot-official + openrouter）中后两家的
> 脱敏交付记录。所有观察仅代表 2026-08-17 的精确 endpoint/model/配置/时间窗，不构成
> SLA。生产 5200 在整个验证期间未被触碰；qualification 写隔离 state
> （`var/gateway-isolated/`），deployed smoke 使用 5231 隔离实例并已终止。

## 账户约束（用户告知）

- Kimi 官方账户余额约 ¥5；本次实际消耗远低于预算（见下）。
- OpenRouter 账户仅可调用 `:free` 模型；付费 candidate（claude-sonnet-5、
  gpt-5.6-sol）保留档案待额度充值后资格化。

## 1. Provider qualification（pvi-probe qualify，隔离 state）

| 模型 | 结果 | 说明 |
|---|---|---|
| `kimi-k3`（moonshot 官方） | **enabled**，身份精确，至 08-23 | thinking 变体（baseline=max/low/high）全部 request_completed，`reasoning_content` 存在；官方非流式路径验证通过 |
| `dots-studio/dots-3-note-preview:free` | **enabled**，身份精确，至 08-23 | broker 完整回显含 `:free` 的完整 slug，无需映射；reasoning 以规范化 `reasoning`/`reasoning_details` 字段暴露 |
| `nvidia/nemotron-3.5-lightning:free` | **candidate / inconclusive** | message 哨兵回显探针未通过（thinking 探针通过、身份精确）；与免费层 3B 激活 MoE 的指令精度一致。按 fail-closed 停层，不改参重试，**不进入本批晋升** |

授权边界：每模型 catalog GET 1 次 + completion ≤5 次，实际未超界。

## 2. Forwarder smoke（scripts/smoke_provider_forwarder.py）

- moonshot：catalog 200，`kimi-k3` 1 次 completion → 200、身份精确、finish=stop、
  usage 完整（70 reasoning / 93 completion），**CNY 成本估算 ¥0.0112**。
- openrouter-free：catalog 200，dots:free 1 次 completion → 200、身份精确（完整
  slug 回显）、usage 完整，**$0**（免费层目录明示价）。
- 两家响应头均无可识别 request-ID，证据记 null。

## 3. Deployed gateway smoke（统一 5231 隔离实例）

- 隔离资源：`.codex_tmp/gateway-5231-unified-batch/`（独立 profiles（active，
  `0.1.0-gateway5231.1`）/alias/state/ledger/日志/PID；复用只读 client-key
  registry）；验证后子进程终止、端口释放。
- `/healthz`、`/readyz` 200；Bearer `/v1/models` 精确返回 4 个统一批次 alias
  （deepseek-v4-pro、deepseek-v4-flash、kimi-k3、dots-3-note:free）。
- 每 alias 一次 completion（max_tokens=64）：4/4 均 200、身份精确、usage 完整。
  dots:free 透传 OpenRouter 的 `cost:0` 与 cost_details 明细。
- 行为事实：三个思考型模型（deepseek×2、kimi-k3）在 64 token 配额下全部
  finish_reason=length（推理消耗配额）；dots:free finish=stop。消费者应按
  token-limits 档案给足输出预算。

## 未测与边界

- streaming、native tool call/replay、structured output 未在本批 enablement 范围内，
  observed 保持 not_tested（nemotron message 为 inconclusive）。
- `:free` 身份可被 OpenRouter 单独限流/下线；免费层有请求频率与日配额限制。
- Kimi 官方 thinking_default 已按实测确认（always_on + max）；OpenRouter 免费模型
  默认思考状态目录未披露，token-limits 仍标 cross_source_unconfirmed 待复核。

## 待办

- 统一晋升生产 5200（4 个新 alias + 现有 9 个百炼 alias 一次重启共存）：**等待用户
  授权时间窗**。nemotron 不在本批。
- 周期复核注意：kimi-k3 日复核约 ¥0.15/天（用户余额约 ¥5，可支撑约一个月，建议
  余额告警）；openrouter 免费层日配额有限，建议不纳入每日自动复核或单独设预算。
