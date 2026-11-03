# 透明聚合网关：P0 优先级与迁移登记册

> **历史过渡登记，非当前规划。** 它记录 v0.5 前后的 P0 收敛过程；现行 0.5.x 工作项见
> [`ROADMAP.md`](../../../ROADMAP.md)，架构入口见 [`PROJECT_STRUCTURE.md`](../../../PROJECT_STRUCTURE.md)。

> 状态：P0 已确认；Yunwu/百炼的第一阶段 profile、资格 CLI、透明网关与离线 POC 已实现。
> 部署周期任务、更多 provider 与业务仓库接入仍待完成。权威决策见
> [`ADR-TRANSPARENT-GATEWAY-003`](../../../decisions/ADR-TRANSPARENT-GATEWAY-003.md)。
> 本文不把时间点观察当作供应商 SLA，也不替代验收 A/B 的验收记录。

## 当前优先级

当前最高优先级是：为 Yunwu、阿里云百炼及后续明确登记的端点建立保真优先的统一调用层，
并以直连差分证据验证 thinking、SSE、工具调用与 tool-result replay。它优先于新增模型名、
通用路由、成本统计和管理界面。

```text
P0 透明网关与保真资格验证
 ├─ P0.1 精确 route/profile 身份与目录发现
 ├─ P0.2 直连 vs 网关的协议保真 POC
 ├─ P0.3 两个现有 Probe 收敛为 qualification runner
 └─ P0.4 业务仓库按显式 route 接入

保持不变：验收 A 能力档案服务
明确未实现：验收 B Turn/SSE Runtime
后置：自动路由、fallback、成本优化、长期 SLO/SLA 监控
```

## 需要替换、归档或更新的现状

| 对象 | 处理 | 原因与完成条件 |
|---|---|---|
| 验收 A Capability Service | 保留，不并入网关 | 它仍是只读能力与证据服务；网关不得要求它在请求期间在线。 |
| “本仓库不做网关”的笼统表述 | 更新为“验收 A 不做；透明网关为独立 P0 工作流，MVP 已实现” | 防止把历史 A 范围误读为否决新业务基础设施，同时不把未实现的 consumer facade 写成已完成。 |
| `ADR-REUSE-001` | 范围限定 | 对验收 A 继续有效；网关工作流由 ADR-003 决定。 |
| `research/reuse-landscape.md` | 标记为 A-only 历史调研 | 不删除；新网关选型由 ADR-003 和后续 POC 证据决定。 |
| `pvi.probe_runner`（单模型 Yunwu） | 演进/收敛 | 作为 profile-driven qualification runner 的基础；先补 provider profile 与差分测试，再移除固定 Yunwu 假设。 |
| `pvian_probe`（Yunwu 4×3） | 演进/收敛 | 复用其 reasoning 和 stream 归一化；把 4×3 降为 preset，禁止再维护并行通用 runner。 |
| Yunwu 4×3 报告与 evidence | 归档保留 | 是带时间窗的历史观察；不覆盖、不提升为长期能力或路由依据。 |
| `.env` / `.env.example` | 更新为“provider 与 gateway client 凭证分离” | provider 与 gateway client key 都只以独立环境变量注入；client registry 只保存 key ID、状态和变量名；模型选择、thinking 参数和 alias 保留在版本化 JSON。 |
| provider profiles | 更新并成为唯一模型/参数配置源 | 必须支持目录发现、精确候选、请求参数版本与禁用静默 fallback。 |
| `misalignment-platform` 与其他业务目录 | 后续新增薄客户端 | 网关已提供 Bearer + public alias 合同；业务侧接入必须在其仓库单独实施和验收，不能复制 provider adapter。 |
| 验收 B Runtime | 保持未实现、单独登记 | 透明转发不是 canonical Turn/SSE Runtime；不得把两者混写为已完成。 |

## 实施阶段与停止条件

| 阶段 | 最小交付 | 停止/拒绝条件 |
|---|---|---|
| G0：身份与配置 | YAML/JSON provider profile、目录发现、route identity、无密钥落盘检查 | 端点、path、模型或 transport 不能精确冻结；profile 企图配置静默 fallback。 |
| G1：直连基线 | Yunwu 与百炼的小样本非流式/SSE/thinking/tool/replay 观察 | 没有显式网络与成本授权；任何原始敏感内容会落盘。 |
| G2：透明网关 POC | 自维护实现与 Portkey passthrough 对照；逐字段差分报告 | 丢失/改写 reasoning、SSE、tool id/arguments、usage、错误或 replay 语义。 |
| G3：runner 收敛 | 一个 profile-driven qualification runner；现有 4×3 作为 preset | 以“重写”为由丢弃已有脱敏/流式解析测试，或新增第三套独立 runner。 |
| G4：业务接入 | 开发目录与 `misalignment-platform` 的薄客户端、显式 route、回归测试 | 网关在不通过保真门禁时被作为默认调用路径；任何业务侧自动切换上游。 |
| G4.1：统一消费者合同（已实现） | Gateway client API Key、`/v1/models` public alias、按 alias 选择唯一显式 route；route 诊断入口同样鉴权 | 将裸 `deepseek-v4-pro` 在多个 provider 间静默选路，或把 provider API Key 下发给消费者。 |
| G5：周期复核与观察 | 24 小时资格有效期、五连败提前复核、外部 scheduler 调用 `pvi-probe qualify --due` | 把短测宣称为 SLA，或让 CI/服务启动自动发起付费请求。费用上限暂不定义，只记录可获得的 usage。 |

## 必须持续遵守的治理规则

- 证据身份至少冻结 `provider + base URL + path + transport + requested/reported model + 参数摘要 + observed_at`。
- `reported_model` 仅为响应声明，不证明实际权重或上游模型身份。
- 失败一次只是一条观测；区分 429、5xx、timeout、DNS、TLS 与协议错误，不能直接判成“不支持”。
- 不存原始 prompt、response、reasoning、API Key、Authorization 或远端错误正文；只存脱敏分类、摘要/哈希与指标。
- 压力、稳定性和恢复时间需要周期调度才能有时间序列含义；在 G5 前仅可报告某次时间窗的运行观察。
- 当前短 burst 的人工运行选择可优先百炼 route；Yunwu 的 429 必须按 rate limit 记录，不得转化为
  自动 failover 或“不支持”结论。见 `docs/reviews/gateway-short-burst-2026-07-31.md`。
- 任何自动路由、fallback、健康度选路、重试、费用策略、工具执行或跨供应商语义转换，都需要新的 ADR。

## 优先级防稀释检查

每个新的 Issue、PR 或文档更新都应回答以下问题；任何一项为“否”就不得占用 P0 排期：

1. 是否直接提高精确端点的协议保真或可验证证据？
2. 是否让多个业务目录减少一份重复的 provider 适配？
3. 是否保持验收 A、验收 B、历史 online probe 三个边界的正确表述？
4. 是否没有引入 API Key 落盘、原始内容留存或静默路由/fallback？
