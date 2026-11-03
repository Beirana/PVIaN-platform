# 网关快速上手

> 本文是**消费者侧**的最短上手路径：拿到凭证 → 选 alias → 发起第一次调用。
> 面向在本机或受控内网调用 PVIaN 网关的下游项目、实验 runner 和 Agent。

PVIaN 网关是一个跑在本机 `127.0.0.1:5200` 的透明转发网关。它集中持有百炼等 provider 的
密钥，下游只需 **Base URL + gateway client key + 一个 public alias** 即可调用已资格认证的模型。
它不是公网 API、不做自动 fallback、不执行业务工具，也不是 Capability Service。

## 相关文档

| 你想做的事 | 看这篇 |
|---|---|
| 完整启动流程、变量边界、验证顺序 | [`gateway-standard-runbook.md`](gateway-standard-runbook.md) |
| 消费者接口细节、薄客户端边界 | [`consumer-integration.md`](consumer-integration.md) |
| 架构交接与时间窗观察（背景） | [`gateway-consumer-handoff.md`](gateway-consumer-handoff.md) |
| 价格事实 / 成本核算 | [`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md) |
| 新增 provider/model 的完整流程 | [`model-extension-runbook.md`](model-extension-runbook.md) |
| 网关运维、轮换、受控部署 | [`gateway-operator-guide.md`](gateway-operator-guide.md) |
| 常驻服务、重启、故障排查 | [`gateway-infra-runbook.md`](gateway-infra-runbook.md) |

运维/重启网关才需要看后三篇；普通消费者只需前三篇加本文。

## 凭证（下游只需这三个）

凭证已存在网关机器的 `.env`，下游消费者**只需配置以下三项**。provider 密钥（百炼、Yunwu）
永远不下发，只留在网关进程。

```text
PVIAN_GATEWAY_BASE_URL = http://127.0.0.1:5200
PVIAN_CLIENT_KEY       = <gateway-client-key>     # 见下方"获取 client key"
PVIAN_MODEL            = <从下面的 alias 列表选一个>
```

- **Base URL**：固定为不带末尾 `/v1` 的网关根地址。直接 HTTP 客户端自行追加 `/v1/...`；
  OpenAI SDK 初始化时追加一次 `/v1`。
- **Client Key**：由网关运维侧用 `python scripts/gateway_service.py client-key show` 从 `.env`
  读出并下发给本机消费者。该 key 在 `.env` 中稳定（`PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`），
  重启不变，下游无需跟着更新——除非运维侧做了轮换。**不要外发、不要提交到下游仓库或日志。**
- **Provider 密钥**：百炼 `PVI_PROBE_BAILIAN_API_KEY`、Yunwu `PVI_PROBE_YUNWU_API_KEY` 等
  **永远不下发**，只存在于网关机器的 `.env`。

### 获取 client key

网关运维侧执行（会打印 key 值，仅限本机受控终端）：

```bash
python scripts/gateway_service.py client-key show
```

### 确认网关在线与可调用 alias

```bash
python scripts/gateway_service.py status
```

输出包含进程状态、`/healthz`、`/readyz`、`/v1/models` 当前可调用 alias 及各自 TTL。

## 当前可调用的 public alias

以下为 `/v1/models` 在资格有效期内返回的精确 alias（**截至 2026-08-13**）。每次正式运行前
应经 Bearer 重新查询 `/v1/models`，只使用当时返回的 alias——不要硬编码一份"固定列表"。

```text
pvian/bailian-cn-beijing/qwen3.8-max
pvian/bailian-cn-beijing/kimi/kimi-k3
pvian/bailian-cn-beijing/glm-5.2
pvian/bailian-cn-beijing/qwen3.7-plus
pvian/bailian-cn-beijing/qwen3-vl-plus
pvian/bailian-cn-beijing/MiniMax/MiniMax-M3
pvian/bailian-cn-beijing/xiaomi/mimo-v2.5-pro
pvian/bailian-cn-beijing/deepseek-v4-pro
pvian/bailian-cn-beijing/deepseek-v4-flash-0731
```

全部绑定百炼北京 endpoint，资格有效期 7 天，由 Task Scheduler 任务 `PVIaN-Requalify-Daily`
（每日 04:00）自动续期，通常无需人工干预。**不要使用裸模型名**（如 `glm-5.2`）——当同一裸名
在多个 provider 出现时，网关返回 `409 model_is_ambiguous`，不会静默选路。

## 最小调用示例

### Python（OpenAI SDK）

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["PVIAN_GATEWAY_BASE_URL"].rstrip("/") + "/v1",
    api_key=os.environ["PVIAN_CLIENT_KEY"],
    max_retries=0,  # 网关不自动 fallback，重试由调用方按错误分类决定
)
resp = client.chat.completions.create(
    model=os.environ["PVIAN_MODEL"],  # 例如 pvian/bailian-cn-beijing/glm-5.2
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
print(resp.usage)
```

### curl

```bash
curl http://127.0.0.1:5200/v1/chat/completions \
  -H "Authorization: Bearer $PVIAN_CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$PVIAN_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

### PowerShell

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "Authorization" = "Bearer $env:PVIAN_CLIENT_KEY"
}
$body = @{ model = $env:PVIAN_MODEL; messages = @(@{ role = "user"; content = "你好" }) } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5200/v1/chat/completions" -Headers $headers -Body $body
```

一个完整的 PowerShell 启动器模板（一次性实验场景，含 key 生成、健康检查、清理）见
[`gateway-standard-runbook.md` §4](gateway-standard-runbook.md)。

## 关键规则

1. **不要自动 fallback / 换 provider**——网关不做，调用方也不要做；失败按预注册错误分类重试。
2. **网关只透明转发**：不改 `messages`/`tools`/thinking 参数，HTTP/SSE response body 按上游
   字节序列透传；reasoning/thinking 字段不跨 provider 统一。
3. **usage 从 provider 原生响应读**（`resp.usage`），缺失记 unknown，别写成 0。
4. **需要价格做成本核算**：调 `GET /v1/gateway/pricing/snapshots`（同样用上面的 Bearer key），
   返回带 `snapshot_digest` 的可审计快照，其中 `output_limits` 给出每个模型的最大输出/上下文
   上限（**含推理 token**，用于设 `max_tokens`/`thinking_budget` 预算），以及 `recommended_defaults`
   ——拿来即用的请求参数配方（该发什么、千万别发什么、是否必须 stream）。详见
   [`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md)，数字来源与推理 token 计费规则见
   [`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)。
5. **别绕过网关直连 provider**——既没必要（网关已注入密钥），也违反密钥不下发原则。

## 常见问题

**报 401 `invalid_client_api_key`？** client key 不对或未注入。向网关运维侧确认 key 值；
不要绕过鉴权。

**`/readyz` 正常但目标 alias 不在 `/v1/models`？** 该 route 资格过期或被降级。联系运维侧
重新 qualification，或换一个仍有效的 alias。不要换近似模型名。

**能否用裸模型名（如 `deepseek-v4-pro`）？** 不能。同一裸名在多个 provider 存在时网关返回
`409 model_is_ambiguous`。必须用 `/v1/models` 返回的完整 public alias。

**怎么知道网关是否在跑？** `python scripts/gateway_service.py status`。启动与排查详见
[`gateway-infra-runbook.md`](gateway-infra-runbook.md)。
