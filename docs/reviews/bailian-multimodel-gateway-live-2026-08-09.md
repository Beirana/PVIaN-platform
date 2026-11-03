# 百炼 cn-beijing 八模型网关实测与 5220 隔离上线记录

日期：2026-08-09  
范围：PVIaN 网关模型 profile、价格、能力证据、身份映射与隔离实例  
不包含：下游实验设计、批量科学运行、多模态输入或长负载压测

## 当前服务状态

- 原 5200 服务未重启、未改配置，最终复核 `/healthz=200`、`/readyz=200`。
- 新 5220 服务运行中，PID 记录在
  `.codex_tmp/gateway-5220-live-20260809/gateway.pid`。
- 5220 的 `/healthz=200`、`/readyz=200`；经现有受控 client key 调用
  `/v1/models=200`，精确暴露以下八个 alias：

```text
pvian/bailian-cn-beijing/qwen3.8-max
pvian/bailian-cn-beijing/kimi/kimi-k3
pvian/bailian-cn-beijing/glm-5.2
pvian/bailian-cn-beijing/qwen3.7-plus
pvian/bailian-cn-beijing/qwen3-vl-plus
pvian/bailian-cn-beijing/deepseek-v4-pro
pvian/bailian-cn-beijing/MiniMax/MiniMax-M3
pvian/bailian-cn-beijing/xiaomi/mimo-v2.5-pro
```

5220 使用独立 active profile overlay、alias registry、qualification state、
usage ledger、日志和 PID 文件；源仓库中的 candidate lifecycle 没有被改成全局 active。
隔离实例只以已实测的 message、stream、usage 作为基本路由 admission 条件；其他能力
继续由模型能力表单独表达，不以“不支持某个可选能力”为由阻断普通文本调用。

## 实测能力矩阵

`supported` 表示真实百炼调用已取得完整的身份、finish、usage 和请求 ID 证据；
`unsupported` 表示请求成功到达模型，但返回内容未满足该能力的协议合同。能力 profile
中的 `effective` 仍保留为 `unknown`，因为本次只上线隔离 5220，并未把源 profile
全局晋升或部署到原 5200。

| 精确上游模型 | 消息 | 流式 | Usage | Thinking | 工具调用 + 回放 | 结构化输出 | 多模态 |
|---|---|---|---|---|---|---|---|
| `qwen3.8-max` | supported | supported | supported | supported | supported | supported | 未测试 |
| `kimi/kimi-k3` | supported | supported | supported | supported | supported | supported | 未测试 |
| `glm-5.2` | supported | supported | supported | supported | supported | supported | 未测试 |
| `qwen3.7-plus` | supported | supported | supported | supported | supported | supported | 未测试 |
| `qwen3-vl-plus` | supported | supported | supported | supported | supported | supported | 未测试（本轮仅文本） |
| `deepseek-v4-pro` | supported | supported | supported | supported | supported | supported | 未测试 |
| `MiniMax/MiniMax-M3` | supported | supported | supported | supported | supported | **unsupported** | 未测试 |
| `xiaomi/mimo-v2.5-pro` | supported | supported | supported | supported | supported | supported | 未测试 |

Kimi 的首次工具探针失败不是模型不支持工具，而是探针使用了“指定函数对象”的
`tool_choice` 写法。百炼官方合同规定 K3 使用 `auto`、`none` 或 `required`。
修正为首轮 `required`、回放轮 `none`，并在内存中保留 `reasoning_content` 后，
工具调用和工具结果回放均通过。通用 sweep runner 也已改为遵循 profile 的
`tool_choice_mode`，并在回放时保留 reasoning 字段但不将其写入证据。

DeepSeek 首轮返回合法原生工具调用但使用 `finish_reason=stop`；针对性回放将
`tool_choice` 冻结为 `none` 后正常 `stop`，因此工具能力记为 supported，同时保留
该 finish 协议差异。

## MiMo 身份合同

百炼 catalog 与请求使用精确 ID `xiaomi/mimo-v2.5-pro`，响应报告
`mimo-v2.5-pro`。网关使用版本化、单一映射：

```text
xiaomi/mimo-v2.5-pro -> mimo-v2.5-pro
```

只有这一对值可被接受；没有通用的 `strip-prefix`，其他短名、大小写变化、近似 ID
或跨厂商模型仍会 fail closed。5220 smoke 中该映射再次实测通过。

## 5220 网关 smoke

启动后只做八次非流式 completion，每个 public alias 一次，SDK/gateway 均无重试、
无 fallback。八次全部 HTTP 200、`finish_reason=stop`、usage 完整，route/profile/
alias headers 精确匹配。

| Alias 后缀 | Reported model | 身份 | Total tokens | 估算费用 CNY |
|---|---|---|---:|---:|
| `qwen3.8-max` | `qwen3.8-max` | exact | 127 | 0.002868 |
| `kimi/kimi-k3` | `kimi/kimi-k3` | exact | 594 | 0.0518 |
| `glm-5.2` | `glm-5.2` | exact | 212 | 0.005496 |
| `qwen3.7-plus` | `qwen3.7-plus` | exact | 271 | 0.002048 |
| `qwen3-vl-plus` | `qwen3-vl-plus` | exact | 28 | 0.000118 |
| `deepseek-v4-pro` | `deepseek-v4-pro` | exact | 78 | 0.001704 |
| `MiniMax/MiniMax-M3` | `MiniMax/MiniMax-M3` | exact | 211 | 0.00081816 |
| `xiaomi/mimo-v2.5-pro` | `mimo-v2.5-pro` | explicit mapping | 315 | 0.0018858 |

本轮 5220 smoke 合计估算 `0.06673796 CNY`。安全 artifact 位于
`.codex_tmp/gateway-5220-live-20260809/gateway-smoke.json`，通过专用 schema；
其中不含 prompt、模型输出、private reasoning、provider body 或任何凭据。

## 可归属费用

以下是本任务所有成功保存并可按模型归属的 list-price 估算；它们用于统计，不是运行时
限额，也不会导致实验中途停止。

| 模型 | 可归属累计费用 CNY |
|---|---:|
| `qwen3.8-max` | 0.036348 |
| `kimi/kimi-k3` | 0.215004 |
| `glm-5.2` | 0.024768 |
| `qwen3.7-plus` | 0.024662 |
| `qwen3-vl-plus` | 0.040178 |
| `deepseek-v4-pro` | 0.027144 |
| `MiniMax/MiniMax-M3` | 0.01421196 |
| `xiaomi/mimo-v2.5-pro` | 0.0208236 |
| **合计** | **0.40313956** |

另有一次 capability sweep 在完成 provider 调用后因本地 schema 上限错误而未能保存
安全 artifact，因此其费用无法在本地精确归属，不混入上述合计。后续 v1、v2、Kimi/
DeepSeek targeted 和 5220 smoke 均已完整保存 usage/cost。

## qwen3.8-max 与 preview

本次 provider catalog、能力调用和 5220 smoke 实际验证的是精确
`qwen3.8-max`。没有把 `qwen3.8-max-preview` 当作替代，也没有在 5220 暴露 preview。
旧公开列表仍可能把 preview 标为 Token Plan only，但控制台与当前价格资料已列出
`qwen3.8-max`，而且标准百炼 endpoint 已真实调用成功。因此网关应继续使用已验证的
`qwen3.8-max`，不推断 preview 与正式 ID 可互换。

## 验证与安全边界

- 全仓离线回归：244 passed。
- Ruff：本任务涉及的 `src`、`tests` 和新增安全脚本全部通过。
- `git diff --check`：通过，仅有既存 Windows 换行提示。
- 5200 未重启、未部署、未改运行状态；5220 启动失败路径只会终止本次新 PID。
- 未提交、未推送。
- 用户既有脏文件 `docs/gateway-standard-runbook.md`、
  `src/pvian_gateway/config.py`、`.zcode/`、`docs/gateway-infra-runbook.md`、
  `scripts/launch_gateway.py` 未被覆盖、回退或删除；其中旧 launcher/runbook 的付费调用、
  凭据与 raw-body 日志风险仍需单独处理，不能作为本次 5220 的安全启动入口。
