# 项目结构与当前架构

> 当前权威总览。基线：**v0.5.1（2026-07-31 内部版）**。本文说明当前实现，而非历史设计愿望；历史材料见
> [归档索引](docs/archive/INDEX.md)，v0.5.0 运行时冻结范围见
> [v0.5.0 说明](docs/releases/v0.5.0-internal.md)。

## 定位与边界

PVIaN 由三条彼此分离的能力组成：

| 组件 | 当前职责 | 不负责 |
|---|---|---|
| Capability Service | 只读端点、能力档案、证据与 requirements 解析 | provider 凭据、模型调用、自动选路 |
| qualification / probe | 在显式网络与成本授权下，对精确 provider/endpoint/model 产生脱敏时间窗观察 | 自动运行、保存原始 prompt/response/thinking/错误正文 |
| Transparent Gateway | 为已资格认证的 OpenAI-compatible route 提供受鉴权的调用入口 | 验收 B Turn/SSE Runtime、工具执行、自动 fallback/路由/SLA |

v0.5.x 是本机或受控内网基础设施。它不是公网多租户 API、计费/配额系统、模型市场、自动负载均衡或
长期可用性承诺。一次 probe、smoke 或 burst 的结论只对应其时间窗。

## 目录与责任

```text
src/pvi/                 Capability Service；pvi-probe 的 legacy 检查及 profile qualification
src/pvian_gateway/       独立透明网关、鉴权、alias、forwarder、脱敏状态存储
src/pvian_probe/         固定 Yunwu 4×3 观察矩阵（历史/对照 preset，不是通用 runner）
config/provider-profiles/ 版本化 provider、endpoint、模型候选与 thinking 变体
config/public-model-aliases.json  唯一的 public alias registry
config/gateway-client-keys.json   key ID、状态、环境变量名（不含密钥值）
config/pricing/                   版本化模型费率（prompt/cached/completion），编辑后热重载
config/pricing/snapshots/         独立、可审计的价格快照（schema `pvian.price-snapshot/v1`，带 SHA-256 digest）
data/ contracts/         Capability Service 的冻结数据、Schema 与 OpenAPI
var/gateway/             Git 忽略的脱敏 qualification state 及相邻 lock 文件
tests/                   unit、contract、integration、acceptance 合同
acceptance/ scripts/     离线验收和数据/OpenAPI/矩阵/安全校验入口
docs/                    当前专题说明；docs/archive/ 为非当前权威的历史资料
```

跨项目接入新 provider/模型时，以 [模型扩展 Runbook](docs/model-extension-runbook.md) 为执行入口。该 Runbook
把 candidate profile、价格与能力档案、精确调用预算、provider/forwarder/deployed-gateway 三类证据、
reported-model identity Gate，以及隔离 5220 到生产 5200 的部署边界串成一条可审计生命周期。

`pvi-probe check/run` 仍是固定 Yunwu 单模型的 legacy 工具；`pvian-probe` 仍是固定 Yunwu 4×3
矩阵。后续扩展应收敛到 profile-driven `pvi-probe qualify`，不得再创建第三套通用 runner。

## 请求与状态流

```text
consumer
  └─ Bearer gateway client key + public alias
       └─ PVIaN Gateway
            ├─ 验证 client key（key_id / 状态 / 环境变量注入的值）
            ├─ 检查 alias → 精确 profile/version/endpoint/route/upstream model
            ├─ 检查脱敏 qualification state（24h、有无漂移、可调用性）
            └─ 仅在内部注入对应 provider key → 精确 OpenAI-compatible upstream

external scheduler（显式授权）
  └─ pvi-probe qualify [--due] → 发现目录、执行资格探针 → 写回脱敏 state
```

gateway client key 与 provider key 完全分离：前者由下游持有，后者仅存在于网关机器的独立环境变量中。
`config/` 只保存环境变量名称和身份元数据，不保存值。裸 upstream model 在多个 provider 都存在时返回
`409 model_is_ambiguous`；不会自动换 provider、重试或 fallback。`X-PVIAN-Route-ID + exact model`
保留为同样需要 Bearer 的内部诊断路径。

qualification state 默认是 `var/gateway/qualification-state.json`。网关和外部 qualifier 对其所有读取及
读改写都使用相邻 `.lock` 的 Windows/POSIX 跨进程锁；临时文件、`fsync` 和 `os.replace` 用于原子发布。
锁只覆盖本地状态临界区，绝不覆盖 provider 网络请求。state 只含身份、资格/能力摘要、时间、失败分类和
usage 聚合，不含原始内容。

## API、CLI、配置与测试入口

- Capability Service：`pvi.main:app`；读取 `/v1/endpoints`、`/v1/capability-profiles`、
  `/v1/capability-queries:resolve`、`/v1/probes`、`/v1/probe-runs/{id}`、`/v1/evidence/{id}`。
- Gateway：`pvian-gateway`；除 `/healthz` 与 `/readyz` 外均要求 Bearer，提供
  `/v1/gateway/routes`、`/v1/gateway/capabilities`、`/v1/gateway/pricing/snapshots`（版本化价格事实，
  编辑 `config/pricing/*.json` 后热重载，详见 [价格快照说明](docs/gateway-pricing-snapshots.md)）、
  OpenAI-compatible `/v1/models` 与 `/v1/chat/completions`。
- Qualification：`pvi-probe qualify --profile-id <id> --allow-network --confirm-cost`；仅复核到期或五连败
  route 用 `pvi-probe qualify --due --allow-network --confirm-cost`。`PVI_NETWORK_DISABLED=1` 会拒绝在线调用。
- 离线校验：`acceptance/run-offline.ps1` / `.sh`，以及 `scripts/validate_data.py`、
  `validate_openapi.py`、`validate_matrix_report.py`、`check_forbidden_fields.py`。

Docker Compose 同时提供 Capability Service 和网关；端口默认仅映射到 loopback。受控内网部署、TLS、日志脱敏
与密钥轮换以 [网关运维指南](docs/gateway-operator-guide.md) 为准。

## Thinking、工具与流式合同

网关不会跨 provider 统一 reasoning/thinking 字段。它对显式 route 保持请求字节透明；public alias facade
只重写 JSON 顶层 `model`，因此是**字段级语义保真**而非请求字节保真。其余 `messages`、`tools`、
`tool_choice`、thinking 参数、tool-call ID/arguments、assistant replay 与 tool-result replay 不被改写；
HTTP/SSE response body 也不解析或改写。

为避免压缩链路损坏普通 httpx/OpenAI 客户端，gateway 向上游固定发送 `Accept-Encoding: identity`；若上游仍压缩，
会保留 `Content-Encoding`。SSE 的 `[DONE]`、reasoning 分片、usage 和错误状态通过合同测试验证为保留。

qualification 的 thinking 变体由 provider profile 冻结：`reasoning_effort` 是 SDK 顶层参数，厂商扩展如
`enable_thinking` 通过 `extra_body` 发送。它只记录 reasoning 字段存在性、字段名和 reasoning token 摘要，
不记录 reasoning 文本。当前 DeepSeek V4 Pro 的真实观察必须以 2026-07-31 的
[release](docs/releases/v0.5.0-internal.md) 与 [短 burst 记录](docs/reviews/gateway-short-burst-2026-07-31.md)
为准，且仅代表当时的精确端点/配置/时间窗；测试中的 synthetic DeepSeek 数据不构成生产实证。

## 接入一个新 provider 的最短路径

1. 仅在网关机器为该 provider 注入独立的 base URL 与 API key 环境变量；不把模型 ID、thinking 参数或
   fallback 写入 `.env`。
2. 在 `config/provider-profiles/` 新增版本化 profile：精确 provider/endpoint/route 身份、HTTPS host/path、
   OpenAI-compatible transport、凭据变量名、目录发现与模型候选规则、request variants、probe set、有效期和阈值。
3. 添加离线解析、脱敏与保真合同测试。非 OpenAI-compatible 协议必须先有独立 adapter 与 ADR，当前不能直接启用。
4. 在获得显式网络与成本授权后运行 qualification；未发现精确模型或未通过资格时不得替代为近似模型。
5. 仅对 `enabled`、未过期且绑定未漂移的精确 route，更新 `config/public-model-aliases.json`；重载网关后由
   `/v1/models` 向消费者发现。

详细技术边界见 [架构说明](docs/architecture.md)、[ADR-003](decisions/ADR-TRANSPARENT-GATEWAY-003.md) 和
[ADR-004](decisions/ADR-GATEWAY-CLIENT-CONTRACT-004.md)。后续结果导向工作见 [ROADMAP](ROADMAP.md)。
