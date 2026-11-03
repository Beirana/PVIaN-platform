# PVIaN Platform

PVIaN 是一个用于**供应商能力档案、受控资格验证与保真优先内部模型网关**的单一主仓库。
当前基线为 **v0.5.1 内部版**：下游可用一个网关 Base URL、一个 gateway client key 和一个
public model alias 调用已资格认证的精确上游；它不等于 Capability Service，也不等于验收 B Runtime。

先读这两份当前核心文档：

- [项目结构与当前架构](PROJECT_STRUCTURE.md)：现在有什么、各组件如何协作、怎样接入新 provider；
- [当前规划与迭代计划](ROADMAP.md)：0.5.x 要完成什么、完成标准与明确不做事项。

要直接启动网关并接入一个下游 runner，使用
[网关标准启动与消费者调用流程](docs/gateway-standard-runbook.md)。该文档统一了进程级凭据注入、
探活、alias 发现、调用、usage/cost 落盘和清理顺序。只想快速拿到凭证并发出第一次调用的下游消费者，
可先看 [网关快速上手](docs/gateway-quickstart.md)。

要为另一个项目增加 provider 或模型，先按
[模型扩展 Runbook](docs/model-extension-runbook.md) 完成精确身份、价格、能力、资格预算、三类在线证据和
5220/5200 部署边界的分层验收。

## 快速使用

要求 Python 3.11+。本地已有项目环境时，先运行离线验收：

```powershell
$env:PVI_NETWORK_DISABLED = '1'
uv run --offline --extra dev --extra probe pytest
```

启动只读 Capability Service：

```powershell
uv run uvicorn pvi.main:app --host 127.0.0.1 --port 5100
```

网关只在 gateway client key、provider 凭据和有效 qualification state 已由运维人员在本机配置后启动：

```powershell
pvian-gateway
```

消费者以 Bearer 调用 `GET /v1/models`，从返回的 callable public alias 中选择模型，再向
`POST /v1/chat/completions` 请求。不要把 provider API key、裸 upstream model 或
`X-PVIAN-Route-ID` 放入常规消费者配置；显式 route 仅是内部诊断入口。

## 最小索引

- [网关快速上手（凭证 + 第一次调用）](docs/gateway-quickstart.md)
- [网关标准启动与消费者调用流程](docs/gateway-standard-runbook.md)
- [网关运维、周期资格复核与受控内网部署](docs/gateway-operator-guide.md)
- [消费者集成合同与薄客户端边界](docs/consumer-integration.md)
- [网关价格快照端点（热重载，含模型 token 上限）](docs/gateway-pricing-snapshots.md)
- [百炼模型最大输出 Token 审查（含推理 token）](docs/reviews/bailian-max-output-token-audit-2026-08-13.md)
- [在线 Probe 与 qualification 操作说明](docs/online-probe-runner.md)
- [模型扩展 Runbook：新 provider/模型、证据与上线 Gate](docs/model-extension-runbook.md)
- [v0.5.1 规划更新与 v0.5.0 运行时冻结范围](docs/releases/v0.5.1-internal.md)
- [架构细节](docs/architecture.md) 与 [网关/消费者交接快照](docs/gateway-consumer-handoff.md)
- [ADR 决策](decisions/) 与 [网关运维 ADR](docs/adr/)（含 [模型默认推理行为标注（ADR 0003）](docs/adr/0003-model-default-thinking-profile.md)）与 [历史资料索引](docs/archive/INDEX.md)

在线 qualification/probe 必须显式传入 `--allow-network --confirm-cost`；本仓库的 CI 和正常启动均不
联网。任何观察只代表精确端点、模型、配置与时间窗，不能表述为 SLA。
