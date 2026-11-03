# 在线 Probe Runner

> 本文是受控在线验证的专题操作说明。日常接管先读 [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md) 和
> [ROADMAP.md](../ROADMAP.md)。其中 `pvi-probe check/run` 与 `pvian-probe` 是 legacy/对照入口；新 provider
> 的主路径是 profile-driven `pvi-probe qualify`。

在线 Runner 是验收 A 之外的可选工具。它不会在 PVI HTTP 服务启动时运行，也不会
自动修改 Capability Profile。

## 安装

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,probe]"
```

## 设置本机环境变量

环境变量只保存密钥和端点身份；不得在其中保存模型 ID、思考开关或推理强度。可变的模型选择、候选请求变体和受控资格验证规则由版本化的 Provider Qualification Profile 维护在 `config/provider-profiles/`。

Runner 在获得网络与成本双重授权后，必须先读取 profile 指定的实时模型目录，再按其 `catalog_id_patterns` 选择候选模型并逐项验证。目录中没有候选模型时记录 `not_discovered`；不得换用近似模型、别名或另一个端点。每次验证都记录 requested/reported model、请求变体和脱敏观察结果。

遗留的 `pvi-probe check/run` 命令仍仅支持 Yunwu 单模型 Probe；新的
`pvi-probe qualify` 已读取 `config/provider-profiles/`，可对 Yunwu、百炼等 active profile
进行目录发现和资格验证。它复用 `pvian_probe` 的 stream/reasoning 归一化逻辑；既有 Yunwu
4×3 Matrix 保留为历史 preset，不自动写回正式档案。

资格验证示例：

```powershell
pvi-probe qualify --profile-id provider-yunwu-openai-compatible --allow-network --confirm-cost
pvi-probe qualify --profile-id provider-bailian-cn-beijing --allow-network --confirm-cost
```

运行 `pvi-probe qualify --due --allow-network --confirm-cost` 时，只应复核到期或五连败触发
重验的 route；该命令由外部计划任务调用，CI 与服务启动均不得自动执行。

当前 PowerShell 会话：

```powershell
$secret = Read-Host "请输入 Yunwu API Key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try {
    $env:PVI_PROBE_YUNWU_API_KEY =
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}

$env:PVI_PROBE_YUNWU_BASE_URL = "https://yunwu.ai/v1"
$env:PVI_PROBE_YUNWU_MODEL = "gpt-4o-mini"
```

不要显示、记录或提交真实 Key。

## 无网络配置检查

```powershell
pvi-probe check --provider yunwu
```

该命令验证环境变量、HTTPS、主机白名单和 `/v1` 路径，不发送请求。

## 最小真实请求

```powershell
pvi-probe run `
  --provider yunwu `
  --probe text `
  --allow-network `
  --confirm-cost
```

运行全部首期 Probe：

```powershell
pvi-probe run `
  --provider yunwu `
  --probe all `
  --allow-network `
  --confirm-cost
```

也可以重复 `--probe text --probe tool --probe stream`。CLI 默认关闭 SDK 重试。

## 输出与审核

结果写入：

```text
artifacts/probes/probe-session-<time>-<id>/
```

每个目录包含：

- `artifact.json`：不含原始提示、原始响应或凭证的观察元数据；
- `candidate-evidence-*.json`：符合 Evidence Reference v1 的候选记录；
- `candidate-probe-run-*.json`：符合 Probe Run v1 的候选记录；
- `manifest.json`：摘要、摘要哈希和人工审核状态。

`artifacts/probes/` 已被 Git 忽略。只有在确认脱敏结果、模型标识和 verdict 合理后，
才能手工复制候选记录到 `data/`，再更新 Capability Profile。不得自动把
`unknown/not_tested/inconclusive` 提升为 `supported`。

## Yunwu 4×3 Matrix Probe

同仓库的 `pvian-probe` 对 `config/models.json` 中四个精确模型分别运行
Message、Thinking exposure 和原生工具调用，共 12 个受控请求：

```powershell
pvian-probe --allow-network --confirm-cost
```

Runner 会先校验实时 `/v1/models` 目录，GLM 可配置为流式模式。流式内容、
reasoning 字段和分片工具参数会归一化后再判定。报告写入 `reports/`，
脱敏观察证据写入 `evidence/yunwu-<UTC>/`。

离线验收会验证当前报告恰好包含 4×3 结果、Evidence SHA-256 一致、
不含原始输入输出，并且工具执行次数为 0；CI 不会重新运行在线请求。

## 安全边界

- `PVI_NETWORK_DISABLED=1` 时，即使传入两个授权参数也拒绝联网；
- 只允许 `https://yunwu.ai/v1`，不接受 HTTP、URL 内凭证、查询参数或其他主机；
- 工具 Probe 只要求模型生成合成函数调用，不执行任何业务工具；
- 报错信息经过 Key、Authorization 和常见凭证字段脱敏；
- 本机环境变量不会同步到 GitHub；GitHub CI 保持离线。
