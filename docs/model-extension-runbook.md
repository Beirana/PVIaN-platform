# PVIaN 模型扩展 Runbook

> 合同版本：`pvian-model-extension-runbook/v1`  
> 修订日期：2026-08-09

本文面向需要把新 provider 或新模型接入 PVIaN 的其他项目。目标是得到一个可审计的精确模型路由，
而不是“某个相近模型大概能用”。任何在线步骤都可能产生费用，必须在执行前单独授权；离线实现、
provider probe、forwarder smoke 和已部署网关 smoke 的结论不得互相冒充。

**相关文档**：接入前的网关运行边界见 [标准 Runbook](gateway-standard-runbook.md) 与
[运维指南](gateway-operator-guide.md)；消费者合同见
[消费者集成](consumer-integration.md) 与 [消费者交接](gateway-consumer-handoff.md)；
当前架构与目录总览见根目录 [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md)；
百炼多模型接入的实测证据见
[`reviews/bailian-multimodel-gateway-live-2026-08-09.md`](reviews/bailian-multimodel-gateway-live-2026-08-09.md)。

## 1. 先冻结边界

PVIaN 有两个不同的网关运行边界：

| 边界 | 用途 | 允许推断的结论 |
|---|---|---|
| 隔离验证实例 `127.0.0.1:5220` | 用独立 profile overlay、alias registry、qualification state、usage ledger、日志和 PID 验证新模型 | 只证明该隔离实例及其证据时间窗可用 |
| 生产/既有实例 `127.0.0.1:5200` | 已部署消费者入口 | 只有经 Bearer 的实时 `/v1/models` 才证明某 alias 当前可调用 |

5220 的通过不能自动晋升 5200。不得复用 5200 的 state 文件，不得覆盖已有 profile 或重启 5200；
把候选部署到 5200 是另一个需要变更说明、回滚方案和明确授权的 Gate。端口都应只绑定 loopback 或
受控内网，不直接暴露公网。

消费者只持有 gateway client key，用它作为 Bearer 调用 `/v1/models` 和
`/v1/chat/completions`。provider key 只存在于网关/qualifier 进程的受控环境变量中：

- `config/gateway-client-keys.json` 只登记 client key ID、状态和密钥环境变量名；
- provider profile 只登记 provider key 环境变量名，不保存值；
- 不把 provider key 下发给消费者，不在命令行参数、diff、日志、证据或测试夹具中复制密钥；
- public alias、route 和 provider key 是三种不同概念，不能互相代替。

## 2. 身份必须是精确四元组

每个模型先冻结以下四个值：

```text
public alias -> exact route_id -> exact profile_id@profile_version -> exact requested upstream model
```

例如，alias 可以包含供应商命名空间、斜杠和大小写。`kimi/kimi-k3`、
`MiniMax/MiniMax-M3`、`xiaomi/mimo-v2.5-pro` 都必须逐字保留。catalog 匹配使用完整正则，
不得通过去前缀、转小写、模糊匹配或“最新版本”选择相近模型。alias 可以提前登记，但只有 exact
enabled、未过期、未降级且 profile/version 未漂移的 state 才会使它出现在 `/v1/models`。

`qwen3.8-max-preview` 不能替换 `qwen3.8-max`。即使两个名称出现在同一官方产品页面，preview、
Token Plan 与标准 endpoint 仍是不同身份；只有 catalog 和真实请求共同证明的 exact ID 才能进入路由。

## 3. 扩模生命周期

1. **需求冻结**：记录 provider、region、API surface、精确候选 ID、文本/多模态范围和明确不做事项。
2. **官方资料取证**：为模型页、API 参数页和价格页建立 evidence reference；区分当前页面、控制台证据和文档漂移。
3. **候选 profile**：在 `config/provider-profiles/` 冻结 endpoint host/path、catalog 规则、request variants、超时、单次尝试、无 fallback 和停止条件，lifecycle 保持 `candidate`。
4. **价格档案**：在 `config/pricing/` 保存币种、单位、版本、抓取时间、来源和分层费率；未知值保持 `null`/unknown。
5. **能力档案**：在 `data/capability-profiles/` 分开填写 advertised、observed、effective，并绑定 evidence IDs。
6. **Token 上限档案**：在 `config/model-token-limits.json` 记录 advertised 最大输出 token（**含推理 token**）、
   上下文窗口、最大思维链长度、输出参数名、思考控制参数、**结构化默认推理行为**（`thinking_default`：状态 +
   默认强度）、**推荐请求参数配方**（`recommended_defaults`：`send`/`omit`/`required_non_reasoning`）与确认状态；
   未知/跨源值标 `confirmation`/`default_effort_confirmation: cross_source_unconfirmed`，不伪造。该数据注入价格
   快照端点的 `output_limits` 块（schema `pvian.model-token-limits/v1`），与价格同 hot-reload。推理 token 计入
   输出的官方依据、各模型差异与字段设计决策见
   [`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)
   与 [`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md)。
7. **alias 预布线**：在 `config/public-model-aliases.json` 绑定精确 profile/version/endpoint/route；无有效 state 时必须隐藏。
8. **离线验收**：验证 schema、唯一绑定、近似 ID 拒绝、参数透传、无 fallback、脱敏、价格选择和 state fail-closed。
9. **资格 manifest**：冻结 completion 的精确数量、每次请求参数（包括 API 的 max output 参数）、费用预测、证据字段和停止条件；未授权前不调用 provider。费用预测默认只用于统计，不是硬停机线。
10. **provider probe**：按 manifest 调用精确上游，形成 provider 证据；不产生网关部署结论。
11. **forwarder smoke**：通过转发组件验证 model rewrite、route/profile、finish、usage/cost 和无重试；不声称已部署入口可用。
12. **deployed gateway smoke**：在隔离 5220 或获准的部署实例上验证 Bearer、`/v1/models`、public alias 和完整转发链路。
13. **晋升与部署**：独立评审后把 profile lifecycle、state 和 alias 部署到目标实例；生产 5200 的重启/热加载必须另行授权。
14. **周期复核**：到期、连续失败、官方参数/价格/token 上限变化、reported model 漂移或 profile 变更都重新进入 Gate。

任何阶段失败都停在当前层，不得用下一层 smoke 掩盖上一层缺证据，也不得用历史稳定模型 fallback。

## 4. 官方资料与价格合同

官方资料至少回答：精确模型 ID 与 region、标准 endpoint 是否可用、context/max output、thinking/reasoning
参数、stream、tool/structured/multimodal、seed，以及参数之间的限制。页面是动态内容且无法保存内容摘要时，
明确记录 `content_sha256: null` 和原因，不伪造 digest。

价格文件使用官方 list price，不把限时折扣当长期成本上限。每个 tier 明确：

- `currency` 与 `unit`，例如 CNY / `per_1m_tokens`；
- `pricing_version`、`effective_at`、`captured_at` 和官方 source；
- prompt token 范围；
- input、cached input、output 单价；
- 未知缓存价为 `null`，绝不能写成 `0`。

usage ledger 和价格估算是**统计与审计**，不是消费者运行时硬限额。价格未知、usage 缺失或 reported
identity 不匹配时，成本估算应标 unknown；网关不会因此替用户中途截断一次已经授权的正常请求。
需要运行时预算/配额必须由独立设计和 ADR 实现，不能把 probe 的授权上限误作生产限额。

## 5. 能力矩阵怎么填

能力 profile 的 `subject` 保存 endpoint/provider/API、`model_requested`、`model_reported`、region、
配置 digest 和观察时间。能力字段至少覆盖：

- `messages`、`streaming`、`usage_context`；
- `reasoning` 与 continuation artifact；
- `tool_calling`、streaming arguments、多调用/并行调用、call/result correlation 和 replay；
- `structured_output`、`multimodal`、`builtin_tools`；
- `sampling_observability` 中的 seed、decoding controls、logprobs、logits、weights/tokenizer access；
- endpoint transport、usage reporting、routing transparency、fallback、raw evidence 和 model disclosure。

三层状态不能混写：

- `advertised`：官方声称，必须引用官方 evidence；
- `observed`：精确 endpoint/model/config/time-window 的真实探针结果；
- `effective`：当前目标部署实际承诺。隔离 5220 通过并不自动把生产 5200 的 effective 改成 supported。

`qwen3-vl-plus` 即使 advertised multimodal=supported，纯文本 smoke 也只证明文本接口兼容；在没有冻结图片
输入、媒体处理和评价设计前，不得把它解释为与纯文本模型同质的多模态复现。

## 6. 三类在线证据严格分离

| 证据类 | 调用路径 | 能证明 | 不能证明 |
|---|---|---|---|
| provider probe | qualifier → provider | catalog 精确存在、参数接受、requested/reported、能力、finish/error、usage | public alias、Bearer、已部署网关行为 |
| forwarder smoke | forwarding component → provider | 精确 profile/route 解析、model 重写、上游身份、usage/cost、无 retry/fallback | 当前运行进程的 auth 和 `/v1/models` 暴露 |
| deployed gateway smoke | Bearer client → 5220/获准的目标 gateway → provider | auth、alias 暴露、state admission、route/profile headers、端到端保真 | 长期 SLA 或其他端口/部署的状态 |

每个 artifact 只能声明一个 `evidence_class`，分别记录自己的授权、调用数量、配置 digest 和 request ID
策略。终态证据只保存 requested/reported model、attempt、HTTP/finish/error 安全分类、规范化 usage/cost、
耗时和哈希后的 provider request ID；不保存 prompt、模型正文、private reasoning、raw provider body 或远端错误正文。

## 7. 授权与精确 completion 计数

任何付费调用前先生成/审查 manifest，并取得同时包含网络和成本的明确授权。CLI 的双门是：

```powershell
pvi-probe qualify --profile-id '<exact-profile-id>' --allow-network --confirm-cost
```

没有这两个 flag，或设置了 `PVI_NETWORK_DISABLED=1`，在线执行必须拒绝。catalog `GET /models` 单独计数，
不属于 completion。当前 qualifier 的 completion 上限公式为：

```text
message 或 usage 中任一被要求：1 次（usage 搭载 message，不额外调用）
stream：1 次
thinking：每个 request variant 1 次
任一 tool/replay family：合计预留 2 次
SDK retry：0；retry_total_attempts：1；fallback：null
```

可离线打印某个 profile 的精确预算：

```powershell
$env:PYTHONPATH = 'src'
python -c "from pathlib import Path; from pvian_gateway.profiles import load_profiles; from pvi.probe_runner.qualification import qualification_completion_call_count; p=load_profiles(Path('config/provider-profiles'))['<exact-profile-id>']; print(qualification_completion_call_count(p))"
```

授权必须写明 evidence class、profile/version、requested model、catalog GET 数、completion 总数、每个请求参数、
最大 list-price 费用预测、无重试和停止条件。不同证据类不能共享“已用次数”；未用余额也不能未经确认
改作另一类 probe。

默认只有 completion 次数是本次 probe 的硬授权边界。`max_tokens`/`max_completion_tokens` 是模型请求参数，
费用是事前预测与事后统计；除非用户明确另设硬预算，否则不得因累计 token 或费用达到预测值而中断实验或消费者请求。

## 8. 参数案例：Kimi 与 MiMo

### Kimi `tool_choice`

不要把 OpenAI 风格的“指定函数对象”假设推广到 Kimi K3。当前 Bailian profile 的通用 qualification
策略使用 `tool_choice_mode=auto`；针对性实测中，首轮工具选择使用供应商允许的 `required`，回放轮固定
`none` 才完成原生 tool call 与 tool-result replay。回放还需要在内存中保留协议要求的 reasoning 字段，
但不能把 reasoning 正文写入证据。结论是：每个 provider/profile 必须冻结自己的 tool choice 和 replay
形状；参数拒绝时记录协议结果并停止，不能偷偷换成另一个模型或通用 fallback。

### MiMo requested/reported identity

Bailian catalog 与请求使用 `xiaomi/mimo-v2.5-pro`，响应报告 `mimo-v2.5-pro`。requested/reported 不相等
首先必须 fail closed 并停在用户确认 Gate。一次或多次重复看到短名，只能证明行为稳定，不能自行证明身份等价。

在用户确认、证据复核和代码审查后，当前 profile 才加入唯一的版本化映射：

```text
policy_id: bailian-cn-beijing-xiaomi-mimo-v2-5-pro@1.0.0
xiaomi/mimo-v2.5-pro -> mimo-v2.5-pro
```

默认策略仍是 `exact-v1`。映射只接受这一对 requested/reported；其他前缀、短名、大小写变化、近似 ID
和跨 provider 模型全部拒绝。禁止实现通用 `strip-prefix`。profile 语义变化必须 bump version，并同步 alias、
state binding 和后续证据；历史 artifact 继续绑定其原 profile version，不回写历史解释。

## 9. 停止条件

遇到以下任一条件立即停止当前在线阶段，不重试、不 fallback，并保存脱敏终态：

- catalog 中 exact model 缺失，或只找到 preview/相近 ID；
- requested/reported identity 不相等且没有经用户确认的版本化精确映射；
- manifest/profile/version/route/alias 或价格快照发生漂移；
- 参数被意外拒绝，或工具/replay 协议不满足；
- required usage、finish reason、request ID 策略或终态字段缺失；
- HTTP、transport、timeout、auth、rate-limit 或 provider 错误；
- 下一次调用会超过本次明确授权的 completion 次数；只有用户另行明确设置硬 token/费用预算时，才按该预算停止；
- artifact schema/redaction 失败，或出现 prompt、正文、private reasoning、raw body、凭据；
- 5220 已被占用，既有 overlay/state 已失效，或新进程启动失败；
- 目标是生产 5200，但没有部署/重启授权和回滚计划。

## 10. 离线复现与隔离 smoke

先执行完全离线验证：

```powershell
$env:PVI_NETWORK_DISABLED = '1'
python -m pytest tests/unit tests/contract -q --basetemp .codex_tmp/pytest-model-extension
python scripts/validate_data.py
python scripts/validate_openapi.py
# 把 <task-owned-script.py> 替换为本次新增或修改的脚本；不要扫描/改写用户已有脏脚本。
python -m ruff check src tests scripts/<task-owned-script.py>
```

当前仓库的 5220 脚本是八模型实证的专用复现入口，不是任意项目的通用 launcher。离线构建 overlay 不调用网络：

```powershell
python scripts/gateway_5220_isolated.py --root .codex_tmp/gateway-5220-review
```

只有在专门批准 deployed gateway smoke 后才可执行：

```powershell
python scripts/gateway_5220_isolated.py --root .codex_tmp/gateway-5220-review --live --allow-network --confirm-cost
```

live 模式固定检查 5220 空闲、使用独立 overlay/state/ledger/log/PID，探活后每个 exact alias 一次 completion；
失败只终止它新启动的精确 PID，不触碰 5200。运行前仍必须复核脚本内目标集合、调用总数和最大成本是否与本次授权一致。

## 11. 交付清单

交接给另一个项目时至少包含：

- exact provider/region/endpoint、requested/reported model 和观察时间；
- public alias、route、profile ID/version、lifecycle、配置 digest；
- 官方模型/API/价格 evidence references 与内容摘要状态；
- tiered pricing、币种/单位、未知字段和实际 usage/cost；
- token 上限档案（`config/model-token-limits.json`）：advertised 最大输出（含推理）、上下文、
  最大思维链、思考控制参数、结构化默认推理行为（`thinking_default`）、推荐请求配方
  （`recommended_defaults`）、确认状态与来源；未确认值标注而非伪造；
- capability profile 的 advertised/observed/effective 三层矩阵；
- qualification manifest、精确 catalog/completion 计数、请求参数、费用预测、授权记录与停止条件；如存在用户明确设置的硬预算，单独标注；
- provider probe、forwarder smoke、deployed gateway smoke 三份独立 artifact/digest；
- request IDs 的安全策略、attempt、finish/error 分类、retry=0、fallback=null；
- reported identity exact 或经确认的单一版本化 mapping；
- `/healthz`、`/readyz`、Bearer `/v1/models` 和 exact alias smoke 结果；
- 离线测试、schema、Ruff、链接检查结果，以及尚未实测/不应推断的能力；
- 生产 5200 是否实际部署、是否重启、回滚方式和当前可调用 alias 快照。

缺少其中任何一项时，交付状态应写成 candidate、blocked 或 unknown，而不是“已上线”。
