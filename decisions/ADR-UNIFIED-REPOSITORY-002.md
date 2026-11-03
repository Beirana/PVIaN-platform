# ADR-UNIFIED-REPOSITORY-002：PVIaN-platform 作为唯一主仓库

- 状态：Accepted
- 日期：2026-07-24

## 背景

Capability Service 与 Yunwu 4×3 Matrix Probe 最初分别位于
`pvi-capability-service` 和 `PVIaN-platform`。两边都包含受控在线 Probe
所需的端点校验、网络授权和脱敏边界，继续独立维护会造成职责和安全实现漂移。

## 决策

`wwweibing-prog/PVIaN-platform` 是唯一主仓库：

- `src/pvi` 保存只读 Capability Service 和单模型 Probe；
- `src/pvian_probe` 保存四厂商 4×3 Matrix Probe；
- 两个 CLI 共用 `pvi.probe_runner` 的网络授权与 OpenAI-compatible 客户端；
- 合同、冻结数据、矩阵证据、报告、测试、Docker 和 CI 在同一仓库审查；
- `pvi-capability-service` 保留为历史来源，不再作为后续交付目标。

在线探测结果仍需人工审核，Matrix Report 不会自动写入正式 `data/`。

## 后果

- 单一 PR 可以同时验证服务合同与在线证据格式；
- GitHub Actions 保持无凭证和无网络；
- 发布包同时提供 `pvi-probe` 与 `pvian-probe`；
- 原平台只需依赖一个仓库和一个 Capability Service API。
