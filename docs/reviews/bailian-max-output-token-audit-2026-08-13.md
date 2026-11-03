# 百炼 cn-beijing 模型最大输出 Token 审查（含推理 Token）

日期：2026-08-13  
范围：PVIaN 网关当前 9 个百炼 cn-beijing public alias 的**最大输出 token** 上限，重点厘清推理（thinking）token 与真实消息 token 之间的关系，以及网关请求参数（`max_tokens` / `max_completion_tokens`、`enable_thinking`、`thinking_budget`、`reasoning_effort`）如何与之交互。  
不包含：在线探针、契约/schema 变更、价格更新、catalog ID 漂移复核、运行时预算硬停机设计。

本次为**离线官方资料审查**：所有数字来自阿里云百炼官方帮助文档（`help.aliyun.com/zh/model-studio/...`），未经重新在线探针验证。官方页面为动态内容且未做内容摘要存档，证据仍以 locator 形式引用，符合本项目 evidence-reference metadata-only 惯例。因此本文所有结构化数字落在正文，不回写到 capability-profile / pricing / provider-profile 文件——后者是 frozen contract，新增字段属于另一次契约升级。

## 核心结论：推理 token 计入输出预算

百炼官方明确：**思考过程的 token 计入输出 token，并按输出 token 计费**，模型卡片上的「最大输出长度」上限同时容纳推理与最终回复。依据来自官方深度思考文档（`https://help.aliyun.com/zh/model-studio/deep-thinking`）的 usage 示例：

```json
"usage": {
  "total_tokens": 149,
  "output_tokens": 138,
  "input_tokens": 11,
  "output_tokens_details": { "reasoning_tokens": 50 }
}
```

`reasoning_tokens`（50）是 `output_tokens`（138）的**子集**，而非额外的池。因此：

- 模型卡片的「最大输出长度」是 **推理 + 真实回复的合计硬上限**，不是只算回复。
- `max_tokens` 必须同时为思考内容和最终回复预留空间；官方表述为「`max_tokens` 需要同时容纳思考内容和最终回复」。
- `thinking_budget` 是在**共享输出池内**对推理部分做的二次上限，不是独立于输出的额外配额。其默认值 = 模型卡片的「最大思维链长度」；超过 `thinking_budget` 后「推理内容会进行截断并立刻开始生成最终回复内容」。
- `max_tokens` 取值范围必须是 `[1, 模型最大输出 Token 数]`，超过会返回 HTTP 400 `InvalidParameter`（见 `https://help.aliyun.com/zh/model-studio/error-code`）。

「最大思维链长度」与「最大输出长度」的关系因模型族而异（见下文按族分节）：Qwen3 族是两个**独立**硬上限（chain > output），GLM-5.2 / MiMo 二者同值（同一池），DeepSeek V4 无独立思维链上限（思考折叠进输出）。

### 默认推理行为（不发 reasoning 参数时）

每个模型的「默认思考状态 + 默认强度」已结构化记录在 `output_limits.thinking_default` 对象中，并通过价格快照端点暴露。决策依据——为什么用结构化对象、为什么 GLM 默认 effort 推测 high 并标注、为什么 Kimi 标 `always_on`、为什么数据放在 token-limits 而非 capability-profile——见
[`adr/0003-model-default-thinking-profile.md`](../adr/0003-model-default-thinking-profile.md)。

## 9 模型最大输出总表

所有 token 数均为官方卡片原文（tokens，非字符）。`输出参数` 列指百炼 OpenAI-compatible endpoint 实际生效的参数名。

| 精确上游模型 | Context | 最大输出 | 最大思维链 | 思考默认 | 输出参数 | 思考控制 | 来源 |
|---|---:|---:|---:|---|---|---|---|
| `qwen3.8-max` | 1,000,000 | 131,072 | 262,144 | 开 | `max_tokens` | `enable_thinking`；`thinking_budget`（默认 262,144） | [qwen3-8-max](https://help.aliyun.com/zh/model-studio/qwen3-8-max) |
| `qwen3.7-plus` | 1,000,000 | 131,072 | 262,144 | 开 | `max_tokens` | `enable_thinking`；`thinking_budget`（默认 262,144） | [qwen3-7-plus](https://help.aliyun.com/zh/model-studio/qwen3-7-plus) |
| `qwen3-vl-plus` | 262,144 | 32,768 | 81,920 | **关** | `max_tokens` | `enable_thinking`；`thinking_budget`（默认 81,920） | [qwen3-vl-plus](https://help.aliyun.com/zh/model-studio/qwen3-vl-plus) |
| `glm-5.2` | 1,048,576 | 131,072 | 131,072 | 开（默认 effort 推测 high⚠️） | `max_tokens` | `enable_thinking`（优先级高于 effort）；`reasoning_effort` none/minimal/low/medium/high/xhigh/max；`thinking_budget`（默认 131,072）。官方未明说默认 effort，按 DeepSeek V4 同体系推测 high | [glm-5-2](https://help.aliyun.com/zh/model-studio/glm-5-2) |
| `kimi/kimi-k3` | 1,048,576 | 1,048,576（**默认 131,072**） | 1,048,576 | 仅思考（常开，不可关） | `max_tokens` | `reasoning_effort`（百炼版**唯一支持 max**，默认 max）；`enable_thinking` 不适用 k3 | [kimi-k3](https://help.aliyun.com/zh/model-studio/kimi-k3) |
| `deepseek-v4-pro` | 1,048,576 | 393,216 | （折叠进输出） | 开（默认 high） | `max_tokens` | `enable_thinking`；`reasoning_effort` low/medium/high/xhigh/max；**不支持 `thinking_budget`** | [deepseek-api](https://help.aliyun.com/zh/model-studio/deepseek-api) |
| `deepseek-v4-flash-0731` | 1,048,576 | 393,216 | （折叠进输出） | 开（默认 high） | `max_tokens` | 同 `deepseek-v4-pro` | [deepseek-api](https://help.aliyun.com/zh/model-studio/deepseek-api) |
| `MiniMax/MiniMax-M3` | 1,048,576 | **131,072（跨源；官方卡片显「—」，待控制台复核）** | 未文档化 | 自适应 | `max_tokens` | `thinking.type` = `disabled` / `adaptive`；无数值 budget | [minimax-api](https://help.aliyun.com/zh/model-studio/minimax-api-by-minimax)、[minimax-m3](https://help.aliyun.com/zh/model-studio/minimax-m3) |
| `xiaomi/mimo-v2.5-pro` | 1,048,576 | 131,072（CoT 上限同值） | 131,072（与输出共享） | 开 | `max_tokens` | `enable_thinking`；**不支持 `thinking_budget` / `reasoning_effort`** | [mimo](https://help.aliyun.com/zh/model-studio/mimo)、[mimo-v2-5-pro](https://help.aliyun.com/en/model-studio/mimo-v2-5-pro) |

注意事项：

- `qwen3-vl-plus` 是**较新的可思考视觉模型**（256K context / 32K output），与遗留的 `qwen-vl-plus`（128K / 8K，非思考）是两个共存的不同 ID。网关 alias 绑定的是前者，catalog 正则 `^qwen3-vl-plus$` 锚定、`if_absent` 禁止替换，不会静默落到 `qwen-vl-plus`。
- `kimi/kimi-k3` 卡片把最大输出列为 1,048,576（与 context 同值），但 Moonshot 上游 quickstart 明确「`max_completion_tokens` 默认 131,072，最大可设置为 1,048,576」——即**默认 128K、绝对上限 1M**。表中以「默认 131,072」标注。
- `deepseek-v4-pro` / `deepseek-v4-flash-0731` 的 393,216 来自官方参数表 `max_tokens` 列「共 393,216」。V4 context 在另一处独立列为 1M，故 393,216 是输出侧上限而非总 context。官方未对「共」字单独注解，精确语义仍建议控制台复核。

## 按族分节的关键差异

### Qwen3 族（qwen3.8-max / qwen3.7-plus / qwen3-vl-plus）

「最大输出长度」与「最大思维链长度」是**两个独立的硬上限**，思维链上限更大（output 131,072 / chain 262,144；VL 因 context 小为 32,768 / 81,920）。`thinking_budget` 默认取思维链长度，可下调以把更多输出预算留给最终回复，但不能超过思维链上限。`qwen3-vl-plus` 的 `enable_thinking` **默认关闭**，与其余 Qwen3（默认开）不同；调用方需要显式开启才会产生推理 token。

### GLM-5.2 与 MiMo（glm-5.2 / xiaomi/mimo-v2.5-pro）

思维链长度 = 输出长度（同一 131,072 池），即思考与回复共用同一上限，没有独立的更大思维链池。GLM-5.2 的 `enable_thinking` 优先级**高于** `reasoning_effort`；MiMo 不支持 `thinking_budget` 与 `reasoning_effort`，只能用 `enable_thinking` 开关思考。

### Kimi K3（kimi/kimi-k3）

仅思考模式（常开）。输出上限卡片列 1M，但默认 131,072；调用方需主动调高 `max_completion_tokens` 才能用满。思考控制用 `reasoning_effort`（low/high/max，默认 max），而非 `thinking_budget`。

### DeepSeek V4（deepseek-v4-pro / deepseek-v4-flash-0731）

官方明确 deepseek-v4-pro/flash/flash-0731 **默认开启思考**，**默认 `reasoning_effort=high`**（可选 low/medium/high/xhigh/max）。393,216 单一输出池，思考折叠进输出，**无独立 `thinking_budget`**（参数表列为 `-`）。思考深度靠 `reasoning_effort` 调节，`enable_thinking` 切换思考开关；调用方无法像 Qwen3 那样精确截断思考，只能调 effort 档位或用 `enable_thinking:false` 关闭。官方**未说明** `enable_thinking` 与 `reasoning_effort` 同时传时的优先级（不像 GLM 明确 `enable_thinking` 更高）。

### MiniMax-M3（MiniMax/MiniMax-M3）

不用 `enable_thinking` / `thinking_budget` / `reasoning_effort`，而是用 `thinking.type` = `disabled`（不思考）或 `adaptive`（模型自决），无数值 budget。usage 中 `reasoning_tokens` 单列，但官方未说明它是否计入 `max_tokens` 上限——这是一个**未文档化**点。M3 的 `temperature` / `top_p` 固定不可调，不支持 `n`，`tool_choice` 仅 `none` / `auto`。

## 与网关现状的差异

审查发现两处「现状字段语义 ≠ 模型 advertised 上限」的漂移，供后续 profile 维护参考（**本次不改文件**）：

1. **provider-profile 的 `max_output_tokens` 是 probe 预算，不是模型上限。** 各 profile 的 `qualification.probe_request_policy.max_output_tokens` 当前是 `{message:64, stream:64, thinking:256, tool:256, replay:128}` 这类探针级小预算，远小于模型真实最大输出（131K–393K）。这是有意为之——探针只需验证能力，不需要顶满——但字段名容易误读为「模型最大输出」。
2. **`max_output_parameter` 跨 profile 取值不一致**（`glm-5.2`、`kimi-k3` 填 `max_completion_tokens`；`qwen3.8-max`/`qwen3.7-plus`/`qwen3-vl-plus`/`minimax-m3`/`mimo` 填 `max_tokens`；`deepseek-v4-flash-0731` 未填）。该字段只在 **probe 探针** 发请求时决定把 token 上限塞进请求体的哪个键（见 `src/pvi/probe_runner/capability_sweep.py`、`qualification.py`），**不影响网关透明转发**——`forwarder.py` 按字节透传请求体，不解析、不重写消费者参数。因此风险缩小为 probe 层：若取值与该模型上游实际接受的参数名不符，探针的输出上限可能被上游忽略或返回 400，但不会污染生产转发路径。官方 OpenAI 兼容参数文档只列 `max_tokens`，未提 `max_completion_tokens`；后者仅个别上游（如 Kimi 原生 quickstart）使用。建议下次 profile 版本 bump 时按模型核对并统一（属于 model-extension runbook 的周期复核触发点，不在本次审查范围）。

各 alias 的 probe 预算 vs 官方最大输出对比（仅示最常用的 message/stream/thinking 档）：

| Alias 后缀 | profile probe `max_output_tokens`（message/stream/thinking） | 官方最大输出 |
|---|---|---:|
| `qwen3.8-max` | 64 / 64 / 256 | 131,072 |
| `qwen3.7-plus` | 64 / 64 / 256 | 131,072 |
| `qwen3-vl-plus` | 64 / 64 / 256 | 32,768 |
| `glm-5.2` | 64 / 64 / 256 | 131,072 |
| `kimi/kimi-k3` | 64 / 64 / 256 | 1,048,576（默认 131,072） |
| `deepseek-v4-pro` | 64 / 64 / 256 | 393,216 |
| `deepseek-v4-flash-0731` | 64 / 64 / 256 | 393,216 |
| `MiniMax/MiniMax-M3` | 64 / 64 / 256 | 131,072（待复核） |
| `xiaomi/mimo-v2.5-pro` | 64 / 64 / 256 | 131,072 |

（probe 预算为各 profile 当前值，精确值以 `config/provider-profiles/*.json` 为准；此处取代表档位说明数量级差异。）

## 消费者实操要点

1. **`max_tokens` 要为思考 + 回复一起预留**。思考模型若把 `max_tokens` 设得过小，思考会先吃满预算导致回复被截断或为空。安全做法：`max_tokens` ≥ 预期回复 + 预期思考。
2. **DeepSeek V4 不能用 `thinking_budget` 截断思考**——它不支持该参数；只能调 `reasoning_effort` 档位。
3. **MiniMax-M3 与 Kimi K3 的思考控制参数名与 Qwen3/GLM 不同**：M3 用 `thinking.type`，K3 用 `reasoning_effort`。不要假设 `enable_thinking` 跨模型通用。
4. **`max_tokens` 超过模型最大输出会 HTTP 400**。各模型上限差异大（VL 仅 32K，DeepSeek V4 达 393K），不要用一个固定值套所有 alias。
5. **usage 从 provider 原生响应读**（`resp.usage`），思考模型的 `completion_tokens` 已含 `reasoning_tokens`；缺失记 unknown，别写成 0（与 quickstart 关键规则一致）。

## 未确认项与后续动作

- **MiniMax-M3 最大输出**：官方 `minimax-m3` 卡片该列显示「—」，131,072 来自跨源一致（OpenRouter 等）。需在百炼控制台模型详情页复核后才能视为已确认。
- **DeepSeek V4「共 393,216」精确语义**：官方未注解「共」字；若需硬保证，以控制台模型详情为准。
- **MiniMax-M3 `reasoning_tokens` 是否计入 `max_tokens`**：官方未说明。若做运行时预算，需针对性探针验证。
- **`max_output_parameter` 漂移**：profile 写 `max_completion_tokens` vs 上游实际接受 `max_tokens`，建议下次 profile 版本 bump 时用探针核对并修正（属于 model-extension runbook 的周期复核触发点，不在本次审查范围）。

## 验证与安全边界

- 本次为**离线官方资料审查**，未发起任何在线探针，未产生 provider 调用费用。
- 未修改任何 spec / schema / Pydantic 模型 / cap 文件 / pricing 文件 / provider-profile 文件 / OpenAPI 合约。
- 未提交、未推送。
- 引用的官方 evidence locator：
  - 深度思考参数与 usage 计费规则：`https://help.aliyun.com/zh/model-studio/deep-thinking`
  - `max_tokens` 范围与错误码：`https://help.aliyun.com/zh/model-studio/error-code`
  - 各模型卡片：见总表「来源」列。
  - 本地 evidence-reference（metadata-only locator，内容在远端页面）：
    `evidence-bailian-qwen3-8-max-official-20260809`、
    `evidence-bailian-qwen3-7-plus-official-20260809`、
    `evidence-bailian-qwen3-vl-plus-official-20260809`、
    `evidence-bailian-glm-5-2-official-20260809`、
    `evidence-bailian-kimi-k3-official-20260809`、
    `evidence-bailian-deepseek-v4-pro-official-20260809`、
    `evidence-bailian-deepseek-v4-flash-0731-qualification-20260803`（flash-0731 暂无 official 卡片 locator，其最大输出与 v4-pro 同引自 [deepseek-api](https://help.aliyun.com/zh/model-studio/deepseek-api)）、
    `evidence-bailian-minimax-m3-official-20260809`、
    `evidence-bailian-xiaomi-mimo-v2-5-pro-official-20260809`。
- 官方页面为动态内容、未做内容摘要存档（`content_sha256: null`），与现有 evidence-reference 惯例一致；数字以页面当前显示为准，未来若官方调整需重新审查。
