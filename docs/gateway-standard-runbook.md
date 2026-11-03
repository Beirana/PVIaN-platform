# PVIaN 网关标准启动与消费者调用流程

> 本文是其他项目、模型和 Agent 使用 PVIaN 透明网关时的标准入口。只想快速拿凭证并发出第一次调用的
> 消费者，可先看简明的 [网关快速上手](gateway-quickstart.md)。详细的 provider、qualification、
> key 轮换与部署说明见 [`gateway-operator-guide.md`](gateway-operator-guide.md)；消费者接口细节见
> [`consumer-integration.md`](consumer-integration.md)。**负责起网关的基础设施值班人员,另见
> [`gateway-infra-runbook.md`](gateway-infra-runbook.md)(启动/排查/交接手册)。** 本文不授权网络探针、付费调用或科学实验。

## 1. 标准流程

外部启动器向网关进程注入 provider 凭据、qualification state 和 gateway client key，启动本地
`pvian-gateway`；确认 `/readyz` 后，再向消费者进程注入网关根地址、同一 client key 和一个由
`/v1/models` 实际返回的 public alias。消费者只调用网关，不读取 PVIaN 的凭据或状态文件。

```text
operator/launcher
  ├─ inject provider credentials + gateway key + state path
  ├─ start pvian-gateway
  ├─ verify /healthz, /readyz and authenticated /v1/models
  └─ inject base URL + client key + public alias into consumer
        └─ consumer/experiment runner -> PVIaN gateway -> exact provider route
```

环境变量只从父进程传给子进程。启动器设置的值不会反向写入另一个终端，也不会自动成为用户级或系统级
环境变量。因此，新开的 shell 看不到某次运行的临时变量是正常现象。

## 2. 变量边界

网关运维侧变量只属于持有凭据的网关进程：

| 变量 | 用途 |
|---|---|
| `PVI_GATEWAY_HOST` / `PVI_GATEWAY_PORT` | 默认 `127.0.0.1:5200`；不要直接暴露公网 |
| `PVI_GATEWAY_STATE_FILE` | 脱敏 qualification state；决定 alias 当前是否可调用 |
| `PVI_GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | 单次请求给上游 provider 的最长等待秒数；默认 `300`。长推理模型（如 `deepseek-v4-flash` 开 `enable_thinking`）对高推理负载题可能流式生成 2-3 分钟，过短会中途被掐并表现为 `502 upstream_transport_error`。不要设回 60 |
| `PVI_GATEWAY_CLIENT_KEY_REGISTRY_FILE` | client-key 注册元数据；不得包含实际 key 值 |
| 注册表引用的 secret 环境变量 | 实际 gateway client key，只在进程环境或 secret manager 中存在 |
| provider profile 引用的凭据变量 | provider base URL/API key，仅网关进程持有 |

所有新消费者统一使用：

```text
PVIAN_GATEWAY_BASE_URL=http://127.0.0.1:5200
PVIAN_CLIENT_KEY=<gateway-client-key>
PVIAN_MODEL=<从 /v1/models 选择的精确 public alias>
```

`PVIAN_GATEWAY_BASE_URL` 固定为不带末尾 `/v1` 的网关根地址。直接 HTTP 客户端自行追加
`/v1/models` 或 `/v1/chat/completions`；OpenAI SDK 初始化时追加一次 `/v1`。

消费者模块只允许从外部注入的环境或显式函数参数取得这三个值。不得自行加载 `.env`、client-key
注册表、provider profile、qualification state 或其他凭据文件。

历史 S/P runner 使用过 `PVIAN_BASE_URL` 和 `PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`。它们只作为
旧代码兼容入口，不是新规范。若必须运行旧代码，由外部启动器临时映射：

```powershell
$env:PVIAN_BASE_URL = $env:PVIAN_GATEWAY_BASE_URL
$env:PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY = $env:PVIAN_CLIENT_KEY
```

## 3. 启动前检查

1. PVIaN 环境已安装，`pvian-gateway` 可执行。
2. provider 凭据已由运维终端、部署环境或 secret manager 注入网关进程环境；不要打印其值。
3. `PVI_GATEWAY_STATE_FILE` 指向本次目标 provider/model 的脱敏 qualification state。
4. qualification 尚在有效期内；过期、缺失或 degraded 时，alias 不应出现在 `/v1/models`。
5. 调用方已决定唯一 public alias；不得使用裸模型名、近似名称或自动 fallback。
6. 结果目录、run ID、重试与停止规则由调用项目冻结；网关不替实验决定参数。

只有用户另行批准联网与成本后，运维人员才能执行 qualification：

```powershell
pvi-probe qualify --profile-id 'exact-profile-id' --allow-network --confirm-cost
```

普通网关启动、CI 和消费者调用不得自动执行 qualification。

## 4. Windows/PowerShell 标准启动模板

> **适用范围**:本模板是**临时跑实验用**的一次性脚本(每次自建网关、跑完即关、key 用完即弃)。
> **长期常驻场景不要用本模板** —— 改用 `python scripts/gateway_service.py start`,或依赖 Task Scheduler
> 任务 `PVIaN-Gateway-AtLogon` 自启(见 [`gateway-infra-runbook.md`](gateway-infra-runbook.md) §7)。
> 常驻模式 client key 已稳定持久化在 `.env`,不再需要本模板里的随机生成逻辑。

模板生成一次性 client key，分别注入网关与消费者子进程，并在结束时关闭本轮网关。它不会打印 key，
也不会把 key 写入 Git、命令参数或结果文件。运行前需确保 client-key 注册元数据引用
`PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`，且 provider 凭据已在当前受控运维环境中可用。

```powershell
$ErrorActionPreference = 'Stop'
$pvianRoot = 'C:\path\to\PVIaN-platform'
$gatewayHost = '127.0.0.1'
$gatewayPort = 5200
$stateFile = Join-Path $pvianRoot 'var\gateway\qualification-state-flash-0731.json'
$targetAlias = 'pvian/bailian-cn-beijing/deepseek-v4-flash-0731'

$bytes = New-Object byte[] 48
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$clientKey = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

$env:PVI_GATEWAY_HOST = $gatewayHost
$env:PVI_GATEWAY_PORT = [string]$gatewayPort
$env:PVI_GATEWAY_STATE_FILE = $stateFile
$env:PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY = $clientKey

$gateway = $null
try {
    $gateway = Start-Process `
        -FilePath "$pvianRoot\.venv\Scripts\pvian-gateway.exe" `
        -ArgumentList '--host', $gatewayHost, '--port', [string]$gatewayPort `
        -WorkingDirectory $pvianRoot `
        -WindowStyle Hidden `
        -PassThru

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing `
                -Uri "http://${gatewayHost}:${gatewayPort}/readyz" -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'PVIaN gateway did not become ready' }

    $env:PVIAN_GATEWAY_BASE_URL = "http://${gatewayHost}:${gatewayPort}"
    $env:PVIAN_CLIENT_KEY = $clientKey
    $env:PVIAN_MODEL = $targetAlias

    $headers = @{ Authorization = "Bearer $($env:PVIAN_CLIENT_KEY)" }
    $catalog = Invoke-RestMethod -Method Get `
        -Uri "$($env:PVIAN_GATEWAY_BASE_URL)/v1/models" -Headers $headers
    $visibleAliases = @($catalog.data | ForEach-Object { $_.id })
    if ($targetAlias -notin $visibleAliases) {
        throw "Target alias is not callable: $targetAlias"
    }

    # 在这里启动调用项目自己的 runner。runner 只继承三个 PVIAN_* 消费者变量。
    # 示例：& python 'D:\path\to\consumer_runner.py'
    # if ($LASTEXITCODE -ne 0) { throw "consumer runner failed: $LASTEXITCODE" }
}
finally {
    if ($null -ne $gateway -and -not $gateway.HasExited) {
        Stop-Process -Id $gateway.Id -Force
    }
    Remove-Item Env:PVIAN_GATEWAY_BASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:PVIAN_CLIENT_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:PVIAN_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY -ErrorAction SilentlyContinue
    $clientKey = $null
    $bytes = $null
}
```

长期受控服务**已落地**(2026-08-10 工单):网关由 `scripts/gateway_service.py` 管理,Windows
Task Scheduler 任务 `PVIaN-Gateway-AtLogon`(登录自启)和 `PVIaN-Requalify-Daily`(每日 04:00 自动复核)
负责常驻与续期;网关侧变量与稳定 client key 注入也由这两者 + `gateway_service.py` 完成,不再需要
外部"服务管理器"。消费者仍只获取三个 `PVIAN_*` 变量。运维细节、手动命令和故障排查见
[`gateway-infra-runbook.md`](gateway-infra-runbook.md) §7「长期常驻服务」。不要让每个消费者各自启动一套
长期网关。

## 5. 消费者调用模板

```python
import os

from openai import OpenAI

gateway_root = os.environ["PVIAN_GATEWAY_BASE_URL"].rstrip("/")
client = OpenAI(
    base_url=f"{gateway_root}/v1",
    api_key=os.environ["PVIAN_CLIENT_KEY"],
    max_retries=0,
)
response = client.chat.completions.create(
    model=os.environ["PVIAN_MODEL"],
    messages=[{"role": "user", "content": "synthetic test"}],
)
```

调用方必须显式冻结 `stream`、thinking、tools、`tool_choice`、超时、重试和停止条件。PVIaN 只透明
转发已支持字段，不替调用方补科学设计，也不进行自动 provider fallback。

## 6. 验证顺序

| 顺序 | 检查 | 通过条件 | 失败时处理 |
|---|---|---|---|
| 1 | `GET /healthz` | 网关进程存活 | 修复启动/端口问题 |
| 2 | `GET /readyz` | 至少一个 active client key 可用 | 修复网关侧 key 注入或注册元数据 |
| 3 | Bearer `GET /v1/models` | HTTP 200 | 修复 client key；不要绕过鉴权 |
| 4 | 精确 alias 可见 | 目标 alias 出现在目录 | 检查 state、TTL 和绑定；不得换近似模型 |
| 5 | 独立 synthetic smoke | 响应结构、usage 和路由头符合合同 | 分类协议/能力/基础设施问题 |
| 6 | 业务或科学 runner | 使用独立 run ID/结果目录 | 从 checkpoint 恢复，不覆盖旧结果 |

provider probe、网关 smoke 和科学正式运行是三类不同证据，不得混入同一个结果集合。

## 7. Usage、Token 与成本

- 消费者从 provider 原生响应体 `usage` 读取输入、输出、缓存命中和总 token；缺失时记录 unknown，
  不得写成 0。
- usage 应与 logical call ID、attempt、public alias 和 run ID 绑定并持久化。
- PVIaN 可在 qualification state 相邻的 `usage-attempt-ledger.json` 保存脱敏 usage 和本地价格估算。
- 本地估算来自 `config/pricing/` 的版本化价格表，只用于核算；它不是 provider 账单或预算 API。
- 实验侧如需可审计、可落盘复验的价格事实，调用
  `GET /v1/gateway/pricing/snapshots`（同样要求 Bearer），返回每个绑定身份的版本化快照（确定性
  `snapshot_id` + `snapshot_digest`）。快照同时携带 `output_limits` 块——每个模型的 advertised
  最大输出 token 上限（**含推理 token**）、上下文窗口、最大思维链长度、思考控制参数，以及
  `recommended_defaults`（拿来即用的请求参数配方：该发/不该发哪些 reasoning 参数、是否必须 stream），
  用于设 `max_tokens`/`thinking_budget` 预算与组装合规请求。修改 `config/pricing/*.json` 或 `config/model-token-limits.json`
  后下次查询热重载，无需重启网关；但端点首次上线需重启网关一次。字段、热重载边界与实验侧落盘义务见
  [`gateway-pricing-snapshots.md`](gateway-pricing-snapshots.md)；数字来源与推理 token 计费规则见
  [`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md)。
- 下游应保存币种、价格版本和缓存 token 口径，再与 provider 控制台账单核对。

## 8. 常见问题

**当前 shell 没有变量，但旧实验为什么能运行？** 旧实验由外层 launcher 设置变量后启动子进程；另一个
shell 不会继承，也不能从孤立网关进程反向取得 key。再次运行时重新启动受控网关并重新注入。

**`/readyz` 正常但没有目标模型？** `/readyz` 只说明鉴权配置可用。检查 qualification 是否为
`enabled/nominal`、是否仍在 TTL 内，以及 alias/profile/endpoint/model 绑定是否一致。

**能否直接使用裸模型名？** 不能。使用 `/v1/models` 当前返回的唯一 public alias，例如
`pvian/bailian-cn-beijing/deepseek-v4-flash-0731`。

**失败后能否自动换 provider？** 不能。重试由调用项目按预注册错误分类执行；PVIaN 不做自动 fallback、
健康度换路或近似模型替换。

## 9. Agent 接入清单

- 明确网关、消费者和 provider 凭据分别由谁持有。
- 明确网关根地址、public alias、profile/version 和 qualification 有效期。
- 三个消费者变量由 launcher 显式注入，业务模块不读取凭据文件。
- `/readyz` 和经 Bearer 的 `/v1/models` 分别通过。
- 先完成独立 synthetic smoke，再进入业务/科学运行。
- runner 持久化最终 assistant content、usage、attempt 和 checkpoint，不保存 private reasoning、raw provider
  response 或密钥。
- 每次运行使用新的 run ID/目录；恢复不重发已确认完成的调用。
- 禁止自动 fallback、近似模型替换和未批准的 provider probe。

任一项不明确时停止在调用前；先补齐运行身份和外部注入，不要由模块猜测或扫描本机凭据。
