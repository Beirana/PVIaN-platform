# DeepSeek 官方直连 provider 接入记录（2026-08-17）

> 本文档是 `deepseek-official` provider 三个在线证据类的脱敏交付记录。所有观察仅代表
> 2026-08-17 的精确 endpoint/model/配置/时间窗，不构成 SLA。生产 5200 在整个验证期间
> 未被触碰；所有在线验证均通过独立进程与 5230 隔离实例完成。

## 需求与身份冻结

- provider：`provider-deepseek-official`，endpoint `https://api.deepseek.com`（官方无路径
  base URL），transport `openai_compatible_chat_completions`，region `global`。
- 目标模型：`deepseek-v4-pro`（当前指向 V4-Pro-0813）、`deepseek-v4-flash`（动态指针，
  当前 V4-Flash-0731）。与百炼版同族模型为不同身份，本记录不继承百炼侧任何结论。
- alias：`pvian/deepseek-official/deepseek-v4-pro`、`pvian/deepseek-official/deepseek-v4-flash`。
- 官方方言：`thinking: {type: enabled|disabled, reasoning_effort: low|high|max}`，默认
  enabled + high；`frequency_penalty`/`presence_penalty` 已弃用。

## 1. Provider qualification（pvi-probe qualify）

- 授权边界：每模型 catalog GET 1 次 + completion 5 次（message+usage 1、thinking 变体
  4：baseline / disabled / effort-low / effort-max），retry=1，无 fallback。实际消耗与
  预测一致，未超界。
- 隔离 state：`var/gateway-isolated/deepseek-official-state.json`（未写生产 state）。
- 结果（两模型相同）：
  - `qualification_status: enabled`，有效期至 2026-08-23；
  - requested/reported **精确相等**（identity match: exact），无需任何映射；
  - message → observed_supported；thinking → observed_supported：全部 4 个变体
    request_completed，`reasoning_content` 字段存在，reasoning token 摘要与 usage 均在
    （disabled 变体按协议预期不产生 reasoning 字段，其余变体 reasoning_tokens 15–41）。
- 停止条件未触发；未发生鉴权/限流/传输错误。

## 2. Forwarder smoke（scripts/smoke_deepseek_official_forwarder.py）

- catalog GET 1 次（200，两精确 ID 均在目录中）+ 每模型 1 次 completion，经
  TransparentForwarder 直达上游。
- 两模型均：HTTP 200、reported==requested、finish_reason=stop、usage 完整（含
  reasoning_tokens 与 cache 字段）。
- 成本估算（USD，peak list price，pricing_version 2026-08-17-official-list）：
  v4-pro $0.00024684（127 tokens）、v4-flash $0.00007436（121 tokens）。
- 观察事实：响应头无可识别的 request-ID（x-request-id 等均缺失），证据中记 `null`，
  不伪造。
- artifact：`data/evidence-artifacts/gateway-forwarder-smoke-deepseek-official-20260817.json`。

## 3. Deployed gateway smoke（隔离 5230 实例）

- 隔离资源：`.codex_tmp/gateway-5230-deepseek/` 下独立 profiles（active，
  `0.1.0-gateway5230.1`）、alias registry、qualification state、usage ledger、日志与 PID；
  复用只读 client-key registry；验证后子进程已终止、端口已释放。
- `/healthz`、`/readyz` 200；Bearer `/v1/models` 精确返回且仅返回两个 deepseek alias。
- 每 alias 一次 completion（max_tokens=64）：两模型均 200、身份精确、usage 完整。
  行为事实：v4-pro finish_reason=length——思考默认开启，64 token 配额全部被推理消耗
  （completion_tokens_details.reasoning_tokens=64）；v4-flash finish_reason=stop
  （37 reasoning / 45 completion）。这不是能力缺陷，是小配额下的预期行为；消费者应按
  token-limits 档案给足输出预算。
- artifact：`data/evidence-artifacts/gateway-5230-smoke-deepseek-official-20260817.json`。

## 未测与边界

- streaming、native tool call、assistant/tool-result replay、structured output：本次
  enablement 未要求，observed 保持 not_tested，不得从官方文档推断为已观察。
- 成本口径为 USD peak 标准价；off-peak（UTC 01:00-04:00、06:00-10:00 以外时段）官方
  半价未计入估算。
- 总实际消耗：14 次 completion + 3 次 catalog GET，list-price 成本合计 <$0.01。

## 待办

- 晋升生产 5200（profile lifecycle candidate→active + 生产 state 记录 + 一次重启）：
  **等待用户授权时间窗**；当前 5200 持续服务百炼路由，未做任何变更。
- 周期复核：TTL 168h，到期或五连败后按标准流程 requalify。
