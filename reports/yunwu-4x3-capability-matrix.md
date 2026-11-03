# Yunwu 4×3 Capability Matrix

**验收参考点：** `AP-01-yunwu-provider-capability-reference`

**生成时间：** `2026-07-24T12:43:25.683272Z`

**端点类型：** `broker_gateway`

**API：** `OpenAI-compatible Chat Completions`

## 能力矩阵

| Vendor / exact model | Message | Thinking exposure | Native tool call |
|---|---:|---:|---:|
| **openai** / `o4-mini` | ✅ observed<br><sub>1270 ms</sub> | ✅ observed<br><sub>token_usage_only; 2150 ms</sub> | ✅ observed<br><sub>2165 ms</sub> |
| **deepseek** / `deepseek-v3.2` | ✅ observed<br><sub>1767 ms</sub> | ◻ not observed<br><sub>4714 ms</sub> | ✅ observed<br><sub>2011 ms</sub> |
| **grok** / `grok-4-1-fast-reasoning` | ✅ observed<br><sub>6136 ms</sub> | ✅ observed<br><sub>visible_reasoning_text; 12186 ms</sub> | ✅ observed<br><sub>11565 ms</sub> |
| **glm** / `glm-4.5` | ✅ observed<br><sub>6467 ms</sub> | ✅ observed<br><sub>visible_reasoning_text; 102930 ms</sub> | ✅ observed<br><sub>5234 ms</sub> |

## 判定口径

- `✅ observed`：本次合成请求观察到目标信号。
- `◻ not observed`：请求完成，但未观察到目标信号；不等同厂商永久不支持。
- `⚠ inconclusive`：请求错误或返回形状不足以得出结论。
- Thinking 只统计独立 reasoning/thinking 字段或 reasoning token；普通解释文字不计。
- Tool Calling 只验证结构化调用生成，不执行工具。

## 证据与限制

- Evidence：`evidence/yunwu-20260724T124325Z/observations.json`
- SHA-256：`e2c2923eb05cf54d8766962883a3b8c62ee4b3d95a42f6f9cf690788965f1e7e`
- 证据仅包含元数据、摘要、usage、延迟和脱敏错误。
- 结果只适用于表中的精确模型 ID、Yunwu 网关和本次测试时间。
- 网关返回的模型名不能单独证明真实上游模型身份或协议完全保真。
