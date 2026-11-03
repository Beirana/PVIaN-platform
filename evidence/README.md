# Evidence runs

本目录只保存脱敏后的探测元数据，不保存 API Key、Authorization header、
原始提示或原始响应。

| UTC session | Purpose | Status |
|---|---|---|
| `yunwu-20260724T123728Z` | 预检：识别上游饱和、GLM 强制流式和响应归一化问题 | superseded |
| `yunwu-20260724T124325Z` | 修正流式归一化与代表模型后执行的最终 4×3 验收参考 | current |

当前报告及其 SHA-256 指向 `yunwu-20260724T124325Z/observations.json`。
历史预检证据保留用于说明适配决策，不参与当前矩阵判定。
