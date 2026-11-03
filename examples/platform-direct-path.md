# 原平台保持直接模型调用路径

PVI 查询只发生在运行前，用于展示能力边界和保存 provenance：

```text
原平台 ──HTTP──> PVI /v1/capability-queries:resolve
   │
   └──原有 SDK / 网关 / benchmark adapter──> 模型端点
```

接入必须满足：

- PVI 不接收模型凭证；
- PVI 不返回自动选择并立即调用的端点；
- PVI 超时或断连时，平台按自己的 `warn/block/ignore` 策略处理；
- 在 `warn` 或 `ignore` 模式，原有模型调用函数仍被调用；
- 运行记录只保存 `endpoint_id/profile_id/profile_version` 和必要 Resolution 摘要；
- 不导入 PVI 的 Python Domain Model，不共享数据库。

本仓库的 `platform-pvi-client.py` 是 HTTP 合同示例，不代表已修改任何真实原平台。
