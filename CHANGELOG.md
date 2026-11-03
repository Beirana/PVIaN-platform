# Changelog

## Unreleased

### bailian 按量 deepseek-v4-pro-0813 上线专属 5246 实例（2026-08-21）

- **身份勘定**：0813 只在**按量付费**目录（240 模型，2026-08-21 只读 GET 确认）；
  Token Plan 订阅目录仍无此模型（复核 11 模型清单）——此前冻结的 token-plan
  候选保持不映射。新按量身份 `bailian-cn-beijing:deepseek-v4-pro-0813`
  （profile v0.1.0、cap 0.2.0、token-limits 跨源条目、alias
  `pvian/bailian-cn-beijing/deepseek-v4-pro-0813`，registry 1.12.0）。
- **资质与部署（操作员指令：不进 5200/5239，专属实例承载全量负载）**：限定
  profile 的隔离资质 8 探针全过（身份精确；enable_thinking 真/假 + effort
  high/max 四变体、原生工具、回放、流式、usage 全 observed_supported）；
  overlay 部署于 **127.0.0.1:5246**（`.codex_tmp/gateway-5246-bailian-0813`，
  独立 state/账本/日志，凭证 .env 内存注入），端到端冒烟 200（思考默认开，
  usage 含 reasoning_tokens 明细）。资质至 08-28。
- 复用 `requalify_aliyun_5245.py` 修复教训：本次 qualify 全程限定
  `--profile-id`，无范围外调用。

### Anthropic 原生透传面上线生产 5200（ADR 0004，2026-08-20）

- **新路由 `POST /v1/messages` 与 `/v1/messages/count_tokens`**（零协议翻译，body 仅
  改写 model 字段，SSE/思考块/tool_use 块原样透传）：client-key 复用现有中间件、
  alias→route→state 门控与失败分类与 OpenAI 面同构；响应头新增
  `x-pvian-api-surface: anthropic_native_messages`。两面的 alias 互投对方端点会
  409 fail-closed（传输精确匹配）。OpenAI chat 处理器零改动，全部既有测试保持绿。
- **实现**：新传输 `anthropic_native_messages`（profiles 解析 allowed_hosts/
  base_path/messages_path，复用 https/443 校验）；forwarder 增 `forward_messages`
  （固定 anthropic-version + claude-cli 风格 UA——实测站点 WAF 拒 python UA 502）；
  `AnthropicUsageCollector` 解析 Anthropic usage（非流式顶层 / SSE
  message_start+message_delta，思考 token 不虚构拆分）；aliases/state 校验接受新传输。
  新档案：profile v0.1.0（active）、endpoint 证据、cap 0.2.0（observed 落账）、
  evidence 引用、token-limits（Anthropic 原生 thinking dialect）、alias
  `pvian/aigocode-anthropic/claude-sonnet-5`（registry 1.11.0）。
- **资质与冒烟（用户授权，3+3 次 ≤128 token）**：隔离 wire 资质三探针全过
  （消息+usage/强制 tool_use 原生 dict input/回放），身份精确；晋升生产 state
  （enabled 至 08-26）；5200 重启后 18 alias；端到端冒烟：消息 200（usage 26/15）、
  完整 agent 工具循环 200、count_tokens 透传如实回传上游 404（站点该面未实现，
  Claude Code 容忍）。**Claude 工具循环从此可经网关使用**：
  `ANTHROPIC_BASE_URL=http://127.0.0.1:5200` + `ANTHROPIC_AUTH_TOKEN=<client key>`。
- **`scripts/qualify_anthropic_surface.py`**：Anthropic 线格式资质探针（evidence
  规则同现有：永不落正文，status-only capability summary）。

### 自动续期体系升级：24h 续期窗口 + 全量预算 + 三段式调度（ADR 0001 修订）

- **续期语义按操作员指令改为"到期前续"**：`due_records`/新增 `renewal_due` 把
  剩余有效期进入最后 24h 的路由视为待续期（旧语义只处理已过期/已降级，最长会有
  近 24h 的 alias 消失窗口）；qualifier 的 `--due` 跳过逻辑同步换用该窗口；
  `callable_record` 不变（alias 服务到真正过期，请求路径零影响）。单测锁定：
  窗口内 due 但仍 callable。
- **预算补齐 13 条（9→22）**：deepseek-official ×2 / moonshot / openrouter-free 按
  牌价冻结 CNY；aigocode ×4 / mimo / aliyun ×3 / aigocode-anthropic 为"无牌价类"
  （`frozen_max_cost_cny: null`，冻结调用次数即预算，不虚构 Credits 换算）。
  此前 requalify_due.py 对缺预算路由整轮 fail-closed 中止——不补齐的话 08-23
  deepseek-official 到期起每日任务会全量停摆（本轮已排除该雷）。mimo 生产记录
  当前为降级态（rate_limited ×8），已纳入自动续期重试。
- **每日 04:00 调度改三段式**（复用既有计划任务，零提权）：生产 pass → 5245
  overlay pass（`requalify_aliyun_5245.py`：拷贝 overlay state → qualify --due →
  版本后缀变换 → 绑定校验 → `os.replace` 原子换入；**运行中的 5245 网关进程全程
  零接触**，下个请求即生效）→ anthropic 面 pass（OpenAI qualifier 不认该传输，
  由 wire 探针脚本直写生产 state 续期）。各段独立失败处理，不互相拖垮。

### 服务状态更正与 local-qwen 处置（2026-08-20）

- **更正：5283 不是 PVIaN 网关**——PID 22144 实为搜狗输入法 SGTool.exe（只回
  HTTPS；仓库无任何 5283 足迹）。此前 CHANGELOG"四个已启动服务（…5283）"系误标，
  未对其做任何清理（避免误伤输入法）。当前真实拓扑：5200 生产（18 alias）、
  5245 阿里 token-plan 专属、local-qwen 家族 5239/5240/5242 全停。
- **local-qwen 按操作员指令保持停用但随时可恢复**：上游 SSH 隧道（18000-18003）
  与三个网关进程均已停，档案/state/脚本全部保留；恢复路径 = 起隧道 →
  `gateway_local_qwen_service.py start`（08-25T04:30 state 到期前需续期，否则
  alias 消失）；开机自启 XML 仍待提权导入。

### 阿里云百炼 Token Plan 个人版接入：3 模型全门通过，1 模型目录缺失（2026-08-19）

- 官方取证（help.aliyun.com，2026-08-19）：个人版订阅端点
  `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
  （OpenAI 兼容，另有 Anthropic 兼容面）、`sk-sp-` 专属 Key 与按量 Key/Base URL
  官方明示互不相通、Credits 计量 + 7 天滚动窗口（Lite 2500 / Standard
  10000 / Pro 40000）、仅北京地域、续费换 Key、ToS 限交互式编程/智能体工具
  （API 自动化后端被禁——操作员知情接受灰区，沿 mimo 先例）。
- 冻结 4 身份（qwen3.7-plus/glm-5.2/deepseek-v4-pro/deepseek-v4-flash-0731
  已有按量路由，不重复接入）：**qwen3.8-max**（旗舰，与按量路由同名不同身份）、
  **qwen3.7-max**、**qwen3.6-flash**、**deepseek-v4-pro-0813**。
- **用户授权 live 批次结果（≤42 次预算，实际 30 次 completion + 6 次只读目录 GET）**：
  - **qwen3.8-max / qwen3.7-max / qwen3.6-flash 全门通过**：资质 enabled
    （消息+usage、流式终态 usage、enable_thinking 真/假两变体、原生工具调用、
    assistant 回放、工具结果回放；reported=requested 精确一致）；追加结构化输出
    专项 3 探针 json_object 全可解析；forwarder smoke 3×200（成本诚实
    pricing_missing）；隔离 **5245** 网关 smoke 3×200（Bearer/alias/端到端，
    usage 含 reasoning_tokens 明细）。cap 0.2.0 observed 层落账；
    qwen3.7-max/qwen3.6-flash 的思考默认开+方言由 wire 证据确认，
    token-limits 升 confirmed。
  - **deepseek-v4-pro-0813 目录缺失 fail-closed**：端点实时目录 11 模型无此 ID
    （官方概览页标 NEW 与目录不一致）；零 completion、不替换
    （同目录的 deepseek-v4-pro 是按量重复身份，明示不取）。cap 0.1.1 记录，
    待官方上架后另行授权复测。
- 新脚本 `scripts/gateway_isolated_aliyun_token_plan.py`（隔离 5245 批 smoke）；
  `smoke_provider_forwarder.py` 增 aliyun-token-plan set（3 模型）；
  alias registry 1.10.0 预布线 4 条（0813 无 state 不可调用，设计如此）。
  隔离 state/账本在 `.codex_tmp/aliyun-token-plan-live-20260819/`，
  **生产 5200 与四个已启动服务（5239/5240/5242/5283）全程零接触**。
- 待办：晋升 5200 需另行授权窗口；requalify 预算条目随 aigocode/mimo 一并
  悬决（08-25 TTL 前处理）；订阅续费后需重填 `.env` 的 sk-sp- Key。

### AiGoCode 四模型专项能力核验（流式/工具/回放/结构化，~20 次小调用）

- **claude-sonnet-5 Anthropic 原生面定性通过（08-19，3 次授权小调用）**：操作员
  提供 CCSwitch 配置的端点 `https://api.aigocode.com`（Anthropic Messages 原生，
  Bearer 鉴权）。`/v1/messages` 实测：消息 200 身份精确；强制 tool_use 的
  `input` 为**原生 dict**（无 OpenAI 垫片的 `{}{...}` 拼接缺陷）；tool_result
  回放 200 并正确引用结果。**该模型 agent 工具循环经 Anthropic 原生面完全
  可用**；OpenAI 兼容路由维持已记录缺陷。cap 0.4.0 落账。同 URL 早前 502
  为请求形态问题（双鉴权头/UA），Bearer-only + claude-cli 风格 UA 即通。
- **08-19 复查（生产 5200 意外宕机后重启，PID 66016）**：gpt-5.6-sol 渠道
  502 **已恢复**（消息 200/stop/身份精确）；claude-sonnet-5 对话 200、工具可触发，
  但 `function.arguments` 仍为畸形拼接 JSON（实测 `'{}{"city": "Beijing"}'`，
  与 08-18 缺陷同款式）——**该路由 agent 工具循环仍不可用**，站点直连与目录
  均正常，判定站点转译垫片缺陷未修复。
- 补上资质批次未逐项落账的四项能力实测（每模型 max_tokens≤256）：
  **gemini-3.7-flash 与 grok-4.6 五项全通**（流式含 [DONE]、原生工具调用
  参数合法、工具结果回放 200、json_object 可解析；grok 流式还带 14 条
  reasoning delta）。
- **claude-sonnet-5 发现站点转译缺陷**：工具调用能触发（finish=tool_calls、
  函数名对）但 `function.arguments` 返回畸形拼接 JSON（`{}{"city": ...}`），
  且工具结果回放消息被 400 拒绝（"content: Input should be a valid list"，
  标准 OpenAI 字符串与 Anthropic 列表两种形态都拒）——**agent 类工具循环
  在该路由不可用**，纯对话/流式/结构化正常。cap 0.3.0 落账。
- **gpt-5.6-sol 站点渠道 502**：核验时段 CODEX 渠道所有请求形态持续 502
  （含重试退避；08-18 全套探针曾通过，判定站点侧上游故障）。生产 5200
  该路由在渠道恢复前会 502；恢复后可补测。cap 0.2.2 记录时间戳。

### 会话交接文档

- `docs/handoff-session-2026-08-18.md`：面向新会话的完整交接——API Key
  获取指南（每个 provider 的控制台来源 + .env 变量名，密钥值不落盘）、
  双网关运行状态、18+1 alias 全景、五项未决事项（requalify 预算/5239
  自启导入/未提交工作树/GLM 暂停/可选并入）、推理方言与回环例外等关键
  技术事实、文件地图与命令速查。

### 专属网关实例：local-qwen 独立服务于 127.0.0.1:5239（持久运行）

- 操作员要求：5200 不停（在用），另起一个同步了最新配置的网关实例
  专门服务本地 qwen 调用。落地为**持久专属实例**：独立 overlay
  profiles/aliases/state/**账本**/日志/PID（var/gateway-local-qwen/），
  复用生产 client key 注册表（消费者同 key）。
- 新脚本 `scripts/gateway_local_qwen_service.py`（start/stop/restart/
  status，镜像生产 service 的精确 PID 进程树管理；幂等启动；状态含
  /v1/models）。当前已启动：PID 44624，healthz/readyz 200，暴露唯一
  alias `pvian/local-qwen/qwen3.8-27b-fp8`，端到端验证 200 身份精确。
  **生产 5200 全程零接触（RUNNING）。**
- 开机自启：本会话无提权无法注册计划任务（Access Denied），已交付
  `deploy/pvian-localqwen-at-logon.xml`（镜像生产任务定义：PT0S 无时限、
  IgnoreNew 防双实例、登录后 60s 延迟、失败重启 3×1min）——**需用户在
  提权命令行执行一次导入**（命令写在文件头注释）。
- 运维注意：实例可用性依赖 SSH 隧道；隧道断开时网关本身健康但上游
  调用失败，隧道恢复后自动恢复转发。账本独立于生产（state 目录派生）。

### 新 provider：自部署 vLLM Qwen3.8-27B-FP8（本地隧道端点，全链路验证通过）

- 操作员部署契约：`127.0.0.1:18000/v1` 固定三值（base_url/model/非空
  dummy key）跨后端机器不变；OpenAI 兼容（chat/completions 含 SSE+tools、
  models、legacy completions）。GLM Coding Plan 维持暂停。
- **代码变更：base URL 校验增加回环例外**（profiles.py 与 probe config
  两处）——127.0.0.1/::1/localhost 允许 http+显式端口（自部署本地运行时
  场景），**外部 host 一律维持 https/443 不放松**；新增两个单元测试锁定
  该边界。这是本批次唯一的网关代码修改。
- live gates（本地零成本）：qualification 一次 **ENABLED**（身份精确；
  思考在 `message.reasoning` 字段透传——探针原生识别该方言；vLLM usage
  不报 reasoning_tokens 记 null）；forwarder smoke 200；隔离 5239 网关
  smoke 200。
- **部署契约逐项实测全过**：5 路并发全部 200 且延迟均匀 6.9-7.8s（超
  4 路上限排队不报错）；legacy `/v1/completions` 200 含 usage；
  `response_format json_object` 200；SSE 流式 reasoning delta 与 content
  delta 分离并以 [DONE] 正常终止。
- token-limits：上下文 131072（服务端目录自报 max_model_len，权威）、
  最大输出 131072（操作员指示 128K）；探针 token 上限按契约放大至
  1024/2048（思考型模型，太小只见 reasoning 无 content）；超时维持
  300s（操作员确认与其他路由一致）；无定价档案（自部署，台账 unknown）。
- 运维要点：可用性依赖 SSH 隧道与远端 GPU；网关无限流器，>100K 长文
  需消费者自律 ≤2 路并发；cap 0.2.0 记录全部实测。

### 统一晋升生产 5200：五新 alias 上线（用户授权重启窗口）

- 晋升前：生产 state 备份为
  `qualification-state-flash-0731.pre-aigocode-mimo-20260818.bak`；五条
  enabled 记录经 `record_qualification` 从隔离证据写入生产 state；离线
  绑定预检通过（18 enabled / 18 callable）后才重启。
- 五 profile lifecycle candidate→active（版本不变：grok/mimo 0.1.0，
  sol/sonnet/gemini 0.1.1）；一次授权重启，healthz/readyz 200，
  `/v1/models` 18 个可调用 alias（13 老 + 5 新），client key 不变。
- 生产 smoke 6/6 通过：五个新 alias 各一次 + 一个老 alias
  （bailian qwen3.8-max）复核，全部 200、reported 身份精确、usage 齐全
  （mimo finish=length 为 64-token 探针上限截断，正常）。台账 22.5MB
  正常落账，无 USD 类拒绝（aigocode/mimo 无定价档案，估算如实 unknown）。
- **运维注意**：五个新 provider 刻意未加入 requalification-budgets.json
  （沿 0d9e008 先例），每日 04:00 自动重qualify 不会刷新它们——
  **08-25 前需手动 requalify 或批准预算条目**。九条 Bailian 老路由由
  每日任务例行维护。

### 三模型放宽重跑：全部 ENABLED，aigocode 五候选就绪（等晋升窗口）

- **放宽的审计记录（profile 0.1.0→0.1.1）**：三 profile 的
  `enablement.required_probes` 由 [message, thinking, usage] 放宽为
  [message, usage]——依据是实证定论：该站点 OpenAI 兼容层与 /responses
  垫片均结构性无法透出推理正文（协议无槽位），继续要求思考可见等于
  永久不可启用。放宽仅豁免「可见性」，不豁免身份精确、usage、finish
  证据；模型仍在思考（token 折入 completion 计费），capability 档案
  保留 reasoning.observed=unsupported/exposure=none 的事实记录。
  审计链：profile 字段+版本、alias 同步升版、CHANGELOG、cap 0.2.1
  notes、git 历史，六处可查。
- **重跑结果（用户授权 +24 次）**：gpt-5.6-sol / claude-sonnet-5 /
  gemini-3.7-flash 全部 ENABLED（隔离 state，有效期至 08-25）；
  forwarder smoke ×3 全部 observed_supported（成本如实 unknown）；
  隔离 5237 网关 smoke ×3 全部 200、身份精确、端口释放。
- 累计花费 64 次 = 原授权 40（用满：34+6）+ 重跑 24。生产 5200 零接触。
- 待晋升（隔离 state 全 enabled）：grok-4.6、gpt-5.6-sol、
  claude-sonnet-5、gemini-3.7-flash、mimo-v2.5-pro——等一次授权重启
  窗口统一晋升。

### MiMo Token Plan 完整资格验证通过 + AiGoCode Responses 端点垫片发现

- **MiMo 全链路 live gates（用户授权，8 次 completion）**：qualification
  ENABLED（身份精确、reasoning_content 透传、usage 完整，有效期至 08-25）→
  forwarder smoke 200（成本如实 pricing_missing）→ 隔离 5238 网关 smoke 200
  （Bearer/alias/state 端到端、缓存命中 usage）。cap 升 0.2.0（仅按 state 落账
  的 message/thinking/usage 记 supported，stream/tool/structured 虽在预算内
  执行但未入 state 摘要，保持 not_tested）。生产 5200 零接触。
- **AiGoCode `/v1/responses` 实测（3 次小探针）**：端点存在且 200
  （object=response），但即使 `reasoning.summary=auto` + 强推理提示也只回
  message 项、reasoning_tokens=0，且 input 被垫片膨胀到 ~4400 token——
  判定为**不透推理的转译垫片**。结论：为获取 OpenAI 思考摘要而给网关加
  Responses 传输在当前站点无收益；Responses 传输评估（改动面：
  profiles transport 枚举 + forwarder 路径 + main.py 新路由/facade +
  Responses 方言 usage 解析 + 探针与合同/ADR，对现有 provider 为
  opt-in 附加不影响）已存档，待未来有原生 Responses 透传的上游或
  Responses 消费者（如 Codex CLI）再立项。

### 推理透传方言定论 + 新 provider：小米 MiMo Token Plan（受控 trial 通过）

- **三家推理暴露定论**（用户提问驱动，官方文档+站点实证各 1 次小探针确认）：
  OpenAI 思考摘要只存在于 Responses API（chat completions 无槽位）；Claude
  thinking 块是原生 API 特性、OpenAI 兼容层默认丢弃；Gemini thought summaries
  仅原生 API（OpenAI 兼容层只透传 thought signature）。三家经 AiGoCode 实测
  响应只有 content+role、usage 无 reasoning 计数——**协议级不透传，非站点
  故障**；grok 与 mimo 方言（reasoning_content）是例外。
- **MiMo Token Plan trial 通过**（操作员知情接受 ToS 灰区）：tp- Key + Bearer
  直连订阅端点，catalog 200（6 模型）+ 单发 completion 200、身份精确、
  reasoning_content 透传、usage 含 reasoning 明细与缓存命中、SDK UA 无 WAF
  拦截——技术上完全可接。
- 新 provider `provider-mimo-token-plan`（token-plan-cn.xiaomimimo.com/v1，
  与按量端点/百炼渠道完全隔离）：candidate profile、endpoint、capability、
  2 条 evidence（docs+trial）、token-limits（1M/128K、enable_thinking 方言、
  max_completion_tokens 口径）、隐藏 alias `pvian/mimo-token-plan/mimo-v2.5-pro`
  （registry 1.7.0→1.8.0）。完整 qualification 批次（6 次 completion：1 变体）
  待授权。

### AiGoCode live gates（用户授权，34 次 completion，生产 5200 零接触）

- 四模型授权组合（操作员调整：sol/sonnet-5/gemini-3.7-flash/grok-4.6，Claude 用
  sonnet 不用 opus，GPT-5.6 只留 sol）。发现并修正 sonnet profile 误留 GROK
  凭证变量（fail-closed 拦截于目录层，零 completion 损失）。
- **grok-4.6 全链路通过**：qualification ENABLED（身份精确、思考三档可见
  reasoning_content、usage 含 reasoning/cached tokens）→ forwarder smoke 200 →
  隔离 5237 网关 smoke 200（Bearer/alias/state/端到端；站点回传
  cost_in_usd_ticks）。capability 档案升至 0.2.0（observed 层落账）。
- **方言发现**：sol/sonnet-5/gemini-3.7-flash 三家 message/usage/身份全过、
  effort 参数全被接受，但站点 OpenAI 兼容层不透传推理正文（thinking 不可观测）
  → 按停止条件停在 candidate。修正方向：三 profile 的 enablement 放宽为
  message+usage（版本 bump 0.1.1）后重跑 24 次——**等待单独授权**。
- WAF/UA 实测：python-httpx 与 OpenAI-SDK UA 均放行（python-urllib 被拦），
  网关 forwarder 无需改动。
- 花费：32（资质）+1（forwarder）+1（隔离网关）= 34 次 ≤ 授权 40；隔离
  state/PID/日志全部在 .codex_tmp/aigocode-live-20260818/ 与 gateway-5237。
- 新脚本 scripts/gateway_isolated_aigocode.py（5237 隔离 smoke）；
  smoke_provider_forwarder.py 增加 aigocode-grok 集合。
- MiMo Token Plan：操作员知情接受灰区做先行尝试；环境变量占位已加
  （tp- key 待提供），离线 candidate 档案与受控 trial 待 Key 到位后进行。

### AiGoCode 重绑最新代模型（六模型 × 四把厂商专属令牌）+ 令牌作用域实测

- 操作员指出站点文档页滞后于真实目录；用四把厂商令牌分别做只读
  `GET /v1/models` 验证（零 completion）：**每把 Key 只能看到对应厂商**
  （CODEX_1.2→7 个 OpenAI 系、CLAUDE_3.9→9 个 Claude、GROK_2.5→2 个 grok、
  GEMINI_1→4 个 gemini，零交叉），且目录中实际存在最新代模型。
- 据此废弃未上线的三条旧绑定（gpt-5.5 / claude-opus-4.8 /
  gemini-3.1-pro-preview），重绑六个精确身份：`gpt-5.6-luna`、`gpt-5.6-terra`、
  `gpt-5.6-sol`（CODEX 令牌）、`claude-opus-5`（CLAUDE 令牌）、
  `gemini-3.7-flash`（GEMINI 令牌）、`grok-4.6`（GROK 令牌）。每个 profile
  绑定各自厂商的凭证变量，绝不跨厂商用 Key。
- token-limits 六条（厂商官方页直读：GPT-5.6 三兄弟均 1.05M/128K/effort
  默认 medium；Opus 5 200K/64K/effort 默认 high；Gemini 3.7 Flash
  1048576/65536；Grok 4.6 500K 上下文、官方无输出上限）。
- **新运维发现**：站点 WAF 按 UA 拦截（python-urllib → Cloudflare 1010），
  浏览器与 OpenAI-SDK 风格 UA 放行；网关/探针 HTTP 栈需在 qualification
  前实测确认——已写入 endpoint 与 capability 档案。
- 新增只读目录 evidence（broker_gateway/provider_wire）；registry 维持
  1.7.0（未发布版本内重绑）；测试计数 29 profiles / 40 evidence。
- 站点逐 token 价格仍在登录墙后：无 pricing 文件，台账估算 unknown。

### 新 provider：AiGoCode 中转站（candidate 离线档案，首版绑定已被上一节重绑取代）

- `provider-aigocode`（api.aigocode.com/v1，Bearer）成立：与 yunwu、openrouter
  完全独立的中转 provider；官方明示「控制台与服务端配置始终优先于文档」。

### 新 provider：智谱 GLM Coding Plan（订阅制端点，candidate 离线档案）

- 首个订阅计费 provider：`provider-bigmodel-coding`（open.bigmodel.cn
  `/api/coding/paas/v4`，套餐专属 Key `PVI_PROBE_BIGMODEL_CODING_API_KEY`）。
  与同主机按量端点 `/api/paas/v4`、与百炼 glm-5.2 均为不同身份——官方明示三套
  Key/Base URL 完全隔离不可混用，故独立 provider、独立凭证变量、独立
  allowed_host+base_path 绑定，绝不复用既有 provider。
- 绑定唯一模型 `glm-5.3`（catalog 精确匹配 `^glm-5\.3$`；`glm-5.3[1m]` 后缀为
  另一身份未收录）。官方模型卡片：上下文 1M、最大输出 128K、思考常开不可关、
  reasoning_effort low/high/max 默认 max；按量 API 截图时未上线，Coding Plan
  独占期。glm-4.7（小模型）留待后续批次。
- 订阅制无逐 token 牌价：**不创建 pricing 文件**，台账成本估算按 unknown
  （pricing_missing）而非 0——这是诚实表示，不是缺档。
- 新增：provider profile（candidate）、token-limits 条目、endpoint 记录、
  advertised-only capability profile、evidence reference、隐藏预布线 alias
  `pvian/bigmodel-coding/glm-5.3`（registry 1.5.0→1.6.0）、.env.example 变量、
  合同测试计数与 provider_regions 映射。
- 待用户提供套餐 API Key 后按授权 manifest 执行 live gates（资质探针 →
  forwarder smoke → 独立端口网关 smoke）；生产 5200 不受影响，晋升另行授权。

### DeepSeek 官方定价切换为人民币（中国站实际账单口径）

- 用户提供官方中文定价页（api-docs.deepseek.com/zh-cn/quick_start/pricing/）：中国站
  以人民币计费。两个定价文件由 USD 切换为 **CNY peak 标准价**（v4-pro 输入 ¥9 /
  缓存命中 ¥0.30 / 输出 ¥27；v4-flash ¥3 / ¥0.10 / ¥9 每百万），off-peak 半价不计入，
  pricing_version 升为 `2026-08-17-official-list-cn`。与英文页 USD 价交叉验证比率
  ≈6.8，口径一致（peak/空闲时段定义两页相同：北京时间 9-12、14-18 为高峰）。
- token-limits 两条 notes 同步更新；重启网关一次使估算路径生效（生产验证：台账
  CNY 估算 ¥0.00252 正常落账）。OpenRouter 维持 USD（账户为 USD 充值）。

### 修复：USD 记账被台账安全白名单拒绝（晋升后生产异常，52d3872）

- **现象**：统一晋升后，completed 且以 USD 计价的请求（deepseek-official 两模型、
  openrouter 免费层）在响应体转发完成后写 usage 台账时抛
  `QualificationStateError: usage attempt entry is invalid or unsafe`（ASGI 异常打在
  网关启动终端）。非流式请求客户端通常已收到完整 200，但该次记账丢失。Kimi（CNY）
  不受影响——这就是只有部分调用报错的原因。
- **根因**：`_safe_attempt_estimate` 硬编码 `currency == "CNY"`，未随定价 schema 的
  USD 扩展同步放宽。
- **顺带修复**：`extract_usage` 移除「reasoning_tokens 必须是 completion_tokens 子集」
  的不变量——DeepSeek 语义里推理折叠进输出，但 OpenRouter 把 reasoning 独立计数
  （实测 dots:free reasoning 66 > completion 60），导致其 usage 永远提取失败、
  成本永远 unknown。两方言现在都可记账。
- 回归测试：USD estimate 端到端落账、reasoning>completion 形态提取。已重启生效并
  生产验证（USD 估算 + dots usage 正常写入台账）。晋升到修复之间（约 13 分钟窗口）
  的 USD completed 请求响应本身正常，仅缺 per-attempt 台账行。

### 统一晋升生产 5200 完成（2026-08-17，用户授权窗口）

- deepseek-official-v4-pro / v4-flash、moonshot-official-kimi-k3、
  openrouter-dots-3-note-free 四个 profile lifecycle candidate→**active**（版本保持
  0.1.0）；nemotron:free（inconclusive）与付费 claude-sonnet-5 / gpt-5.6-sol 保持
  candidate。
- 生产 state 写入 4 条 enabled 记录（源自隔离资格化证据，TTL 至 2026-08-23）；
  重启前备份 `qualification-state-flash-0731.json.pre-unified-promotion-20260817.bak`。
- 重启 5200 一次（约 3 秒新连接窗口）：`/v1/models` 现 **13 个 callable alias**
  （百炼 9 + 新 4），client key 与百炼路由不变；healthz/readyz 正常。
- 生产端到端确认：每个新 alias 一次真实 completion，4/4 返回 200、reported model
  精确、usage 完整。
- **复核注意**：新 provider 未加入 `config/requalification-budgets.json`，每日 04:00
  自动复核会跳过（有意为之：kimi 余额约 ¥5、openrouter 免费层日配额有限）。TTL 到期
  （2026-08-23）前需手动 `pvi-probe qualify` 或届时评估加入自动复核预算。
- 回滚路径：恢复上述 state 备份 + `git revert` 本次 profile 变更 + 重启。

### Moonshot Kimi 官方 + OpenRouter 候选档案（offline，未资格化；与 DeepSeek 统一晋升）

- **moonshot-official**（`https://api.moonshot.cn/v1`，中国站身份；全局端点
  `api.moonshot.ai` 为不同身份未纳入）：candidate profile 精确目录 ID `kimi-k3`。
  官方方言与百炼版**不同**：思考常开 + `reasoning_effort` low/high/max（默认 max，
  百炼版仅 max）、**非流式请求官方支持**（百炼版要求 stream:true）、输出参数
  `max_completion_tokens`（默认 131072/上限 1048576）；官方固定 temperature=1.0/
  top_p=0.95 建议勿显式设置。CNY 定价（输入 20/输出 100/缓存 2 每百万）。
- **openrouter**（`https://openrouter.ai/api/v1`，broker_gateway）：首批两个 candidate
  ——`anthropic/claude-sonnet-5`（1M ctx/128K out，$2/$10/cache-read $0.20）与
  `openai/gpt-5.6-sol`（1.05M ctx/128K out，$5/$30，**prompt 超 272K 双层加倍**）。
  关键身份风险已冻结进配方：OpenRouter 默认在 provider 5xx/限流时**静默换后端**，
  `recommended_defaults.send` 固定 `provider:{allow_fallbacks:false}`；统一
  `reasoning:{effort}` 对象（include_reasoning 已弃用列入 omit）；`:nitro`/`:floor`/
  `:batch`/`-pro` 后缀为不同身份不可互替换。未披露值（默认 effort 等）标
  cross_source_unconfirmed 不伪造。
- local-pricing-v2 schema 枚举再放宽（provider += moonshot-official/openrouter，
  region += cn）；GPT-5.6-Sol 使用**双层 tier** 定价文件。
- token limits 追加三条；capability profiles（advertised-only）、endpoint 记录
  （moonshot=official_direct、openrouter=broker_gateway）、evidence 引用与隐藏态
  alias 预布线（registry 1.4.0，共 +3）；.env.example 登记
  `PVI_PROBE_MOONSHOT_API_KEY` / `PVI_PROBE_OPENROUTER_API_KEY` 及各自 base URL。
- **状态**：离线建档完成。等待用户填两家 key 后执行在线资格化（kimi-k3 每模型
  5 次、OpenRouter 每模型 5 次 completion 上限），随后与 deepseek-official 一起
  **统一晋升**生产 5200（用户已明确：三家一起、一次重启）。
- **追加（同日，用户告知账户约束后）**：OpenRouter 账户仅能调用免费模型，故新增两个
  `:free` 身份 candidate——`dots-studio/dots-3-note-preview:free`（280B MoE 预览版，
  512K ctx/out，支持 reasoning/tools/structured_outputs）与
  `nvidia/nemotron-3.5-lightning:free`（30B MoE，1M ctx/64K out，无 structured_outputs）。
  零费率为目录明示免费价（非未知占位）；付费的 claude-sonnet-5 / gpt-5.6-sol profile
  保留为候选，待账户有额度后直接资格化。registry 1.5.0。
- **在线验证完成（同日）**：kimi-k3 官方 **enabled**（身份精确，reasoning_content 全
  变体观察到，CNY 成本估算端到端）；dots-3-note:free **enabled**（broker 完整回显
  `:free` slug，身份门通过；OpenRouter cost:0 明细透传）；**nemotron:free 如实停在
  candidate**（message 哨兵探针 inconclusive，thinking 探针通过——免费层 3B 模型精度
  所致，fail-closed 不重试）。统一 5231 隔离 smoke 4/4 通过（4 alias 精确暴露、每
  alias 一次 completion 成功）。capability profiles 升 0.2.0（kimi/dots observed=
  supported；nemotron message observed=inconclusive）。完整记录见
  [`reviews/moonshot-openrouter-onboarding-2026-08-17.md`](docs/reviews/moonshot-openrouter-onboarding-2026-08-17.md)。
  **统一晋升 5200（deepseek×2 + kimi-k3 + dots:free 共 4 个新 alias）等待用户时间窗。**

### DeepSeek 官方直连候选档案（offline，未资格化）

- 新增 provider `deepseek-official`（endpoint `api.deepseek.com`，OpenAI-compatible，
  无路径 base URL）的两个 **candidate** profile：`deepseek-v4-pro`（当前指向
  V4-Pro-0813）与 `deepseek-v4-flash`（动态指针，当前 V4-Flash-0731）。与百炼版
  同名模型为**不同身份**，不继承任何百炼侧观察结论。
- 官方 thinking 方言为请求对象 `thinking: {type, reasoning_effort}`（默认 enabled +
  high），与百炼 `enable_thinking` 布尔方言不同；request variants 按官方对象形式冻结。
  `frequency_penalty`/`presence_penalty` 官方已弃用，进入 recommended_defaults 的
  omit 列表。
- 价格快照：新增 USD 定价文件（peak 标准 list price；off-peak 时段官方半价，未计入
  费率）。为容纳官方直连，`local-pricing-v2` 冻结 schema 的 `provider`/`region`/
  `currency` 由 const 放宽为向后兼容枚举（`bailian|deepseek-official`、
  `cn-beijing|global`、`CNY|USD`）；现有文件全部仍合法。
- `config/model-token-limits.json` 追加两个 deepseek-official 条目（1M 上下文、
  384K 最大输出含推理 token、无独立思维链上限，官方页 2026-08-17 取证，
  K 显示值按仓库惯例换算为二进制 token 数）。
- capability profiles `cap-deepseek-official-v4-pro`/`v4-flash`（advertised-only：
  官方文档证据，observed 全部 not_tested，effective unknown）与对应 evidence
  references；alias 预布线 `pvian/deepseek-official/deepseek-v4-pro`、
  `pvian/deepseek-official/deepseek-v4-flash`（registry 1.3.0，无 qualification
  state 前对消费者隐藏）。
- profiles loader 允许 `required_base_path` 为空字符串以表达官方无路径 base URL
  （`https://api.deepseek.com`），非空取值行为不变；.env.example 登记
  `PVI_PROBE_DEEPSEEK_API_KEY` / `PVI_PROBE_DEEPSEEK_OPENAI_BASE_URL`。
- **状态**：离线建档完成，等待用户提供 API key 与显式网络/成本授权后执行
  qualification probe（每模型 catalog GET 1 次 + 最多 5 次 completion，两模型合计
  最多 10 次；按 peak list price 上界成本 <$0.01）。
- **在线验证完成（同日，用户授权后）**：两模型 qualification 均 enabled（requested/reported
  精确相等，无需映射，有效期至 2026-08-23）；forwarder smoke 与 5230 隔离 deployed gateway
  smoke 全过（Bearer、/v1/models 精确暴露、端到端转发、USD 成本估算）。验证全程零接触
  生产 5200：qualification 写入隔离 state（`var/gateway-isolated/`），smoke 使用独立
  overlay/ledger/PID 的 5230 子进程并在验证后终止。capability profiles 升至 0.2.0
  （message/reasoning/usage observed=supported；streaming/tool/replay 保持 not_tested）。
  完整脱敏记录见
  [`reviews/deepseek-official-provider-onboarding-2026-08-17.md`](docs/reviews/deepseek-official-provider-onboarding-2026-08-17.md)。
  **晋升生产 5200 等待用户授权时间窗**（需 candidate→active + 生产 state + 一次重启）。

### 默认推理行为结构化 + 推荐请求参数配方（recommended_defaults）

- `output_limits.thinking_default` 从单字符串升级为**结构化对象**（`state` + `default_effort`
  + `default_thinking_budget` + `default_effort_confirmation`），统一记录每个模型"默认思考状态 +
  默认强度"两个维度。决策依据见
  [`docs/adr/0003-model-default-thinking-profile.md`](docs/adr/0003-model-default-thinking-profile.md)。
- 新增 `output_limits.recommended_defaults` 块——**拿来即用的请求参数配方**：`send`（应显式发的
  reasoning 参数）、`omit`（该模型不支持、发了会 400 的参数）、`required_non_reasoning`（非 reasoning
  类必发参数，如 kimi-k3 的 `stream:true`）。消费者合并进自己的 `messages`/`model`/`max_tokens` 即可
  发出合规请求，无需读各模型官方文档。
- **qwen3-vl-plus 强制默认开思考**：它是 9 个模型里唯一官方默认关思考的；按"默认尽量开思考"策略，
  其 `recommended_defaults.send = {enable_thinking:true}`。`thinking_default.state` 仍记 `off` 以
  如实保留上游默认事实，策略覆盖与事实记录分离。
- 顺带修正：Kimi K3 控制参数改为仅 `reasoning_effort`（百炼版唯一支持 max，enable_thinking 不适用 k3）；
  GLM-5.2 补 `reasoning_effort` 到 thinking_controls，默认 effort 推测 high 并标 cross_source_unconfirmed。
- 契约改动：`model-token-limits.schema.json` + `pricing-snapshot-response.schema.json` 同步新结构；
  新增 `accounting.ThinkingDefault` / `RecommendedDefaults` dataclass 与 loader。**需重启网关一次**生效。

### 价格快照端点增加模型 token 上限（output_limits）

- `GET /v1/gateway/pricing/snapshots` 的响应新增 `output_limits` 块，承载每个模型的
  **advertised 最大输出 token 上限**（含推理 token 关系）、上下文窗口、最大思维链长度、
  输出参数名、思考控制参数与确认状态。实验侧一次查询即可拿到「价格 + 能力上限」完整预算依据。
- 核心规则（官方 deep-thinking 页 usage 示例）：`reasoning_tokens` 是 `output_tokens` 的子集，
  即 `max_output_tokens` 上限**同时容纳推理与真实消息**；`thinking_budget` 仅在共享池内对推理
  部分做二次上限。各模型差异（独立思维链上限 vs 折叠进输出）见
  [`reviews/bailian-max-output-token-audit-2026-08-13.md`](docs/reviews/bailian-max-output-token-audit-2026-08-13.md)。
- 新增独立数据文件 `config/model-token-limits.json`（9 个百炼模型，schema
  `pvian.model-token-limits/v1`）与契约 `contracts/schemas/model-token-limits.schema.json`；
  未确认值（MiniMax-M3、DeepSeek V4）标 `confirmation: cross_source_unconfirmed`，不伪造。
- `output_limits` 纳入 `snapshot_digest` 完整性校验；与价格同 hot-reload 机制（编辑
  `config/model-token-limits.json` 下次查询即生效，无需重启）。模型无 limits 时 `output_limits: null`，
  价格事实仍正常返回（fail-open）。
- 实现层只扩端点响应：新增 `accounting.TokenLimits` / `load_token_limits` 与
  `build_pricing_snapshot` 的 `limits` 注入；不动 `PriceQuote`、不动 `local-pricing-v2` frozen schema、
  不改 OpenAPI 冻结合约（该端点不在 capability OpenAPI 内）。
- **前置条件**：本次为代码改动，**需重启网关一次**才生效；之后修改 token-limits 文件无需重启。

### 网关交付：版本化价格快照端点与热重载（aa00609）

- 新增 `GET /v1/gateway/pricing/snapshots`：向实验侧返回自描述、可审计的价格事实。
  支持 `model_alias` / `provider` / `region` / `model` 过滤；未知 alias 返回 404。
  端点只提供**价格事实**，不替实验决定调用/token/CNY 预算，也不联网拉取或刷新价格。
- 每个 snapshot 携带确定性 `snapshot_id`（由 provider/region/upstream_model/pricing_version 派生，
  任何价格变动自然产生新 id）与 `snapshot_digest`（除自身外规范化 JSON 的 SHA-256），便于实验侧
  落盘后重新校验完整性。历史快照不可变，价格变动产生新 `snapshot_id` 而非原地改写。
- 热重载：编辑 `config/pricing/*.json` 在下次查询本端点时自动生效，**无需重启网关**；
  重载失败（如半写文件）时保留上一份已知良好价格。`/v1/chat/completions` 的成本估算路径
  仍用内存缓存，本版本仅此端点触发热重载。
- 新增 schema `contracts/schemas/pricing-snapshot-response.schema.json`
  （`pvian.pricing-snapshot-response/v1`）；新增 8 单元测试 + 6 集成测试；
  新增文档 `docs/gateway-pricing-snapshots.md`。
- 一并提交 8 个 v2 定价文件，覆盖 9 个百炼模型的 prompt/cached/completion 费率。
- **前置条件**：本端点需重启网关一次后才生效；之后修改价格无需重启。

### Provider capability sweep probe（2fa789e）

- 新增 provider 能力扫描 probe 与有界 forwarder smoke：在显式网络与成本授权下，
  对精确 provider/endpoint/model 产生能力与转发行为的脱敏观察。

### Contracts schemas（90abe36）

- 新增 6 个 schema：`capability-sweep`、`gateway-5220-smoke`、`gateway-attempt-evidence`、
  `gateway-forwarder-smoke`、`local-pricing-v2`、`qualification-manifest`，为能力扫描、
  网关 smoke、价格 v2 与资格清单提供合同约束。

### 网关长期运行加固（166c5d2）

- 实现 reported-model identity gate：校验上游返回的 model 身份与 profile 声明一致，
  防止聚合/中转 provider 静默替换模型来源。
- Kimi SSE 恢复：修复流式分片解析在中断/缺失 usage 下的状态问题。
- 长期运行网关硬化与运维脚本/文档。

### 百炼多模型冻结数据（02d4522）

- 新增百炼多模型能力档案、脱敏证据与 probe 运行记录。

- Add independent, auditable price snapshot file (`config/pricing/snapshots/`)
  with schema `pvian.price-snapshot/v1`. Each snapshot carries a SHA-256
  digest of its canonical JSON and a SHA-256 of the source pricing document,
  making the pricing commitment tamper-evident without runtime network access.
- Add `load_price_snapshot()` validator in `accounting.py`: enforces schema
  version, required fields, digest integrity, rate non-negativity, and
  source-document provenance. Raises `SnapshotValidationError` on tamper.
- Migrate experiment preflight (`p_phase2_preflight.py`) to consume the
  independent snapshot via `PVIAN_PRICING_SNAPSHOT_FILE` env var instead
  of reading pricing from the qualification state file. Preflight validates
  digest, exact provider/region/model match, pricing version, and rate
  non-negativity; failures yield `price_snapshot_unavailable` / `NO_GO`.
- Perform one-time offline migration of qualification state file
  `var/gateway/qualification-state-flash-0731.json` via `repair_state()`:
  legacy 3-key `usage_totals` migrated to canonical 5-key form, backup
  written to `.bak`, idempotency verified.
- Fix qualification usage schema drift: canonical redacted summary is now fixed to
  five keys (prompt_tokens, cached_tokens, reasoning_tokens, completion_tokens,
   total_tokens). Legacy 3-key state remains loadable; new writes always use 5 keys.
  Unknown fields, negatives, bools, and raw response/prompt/reasoning content are
  rejected.
- Fix state self-corruption after 502 → 200 recovery: `record_success` now clears
  `last_failure_at` and `last_failure_category` as a pair. Previously, only
  `last_failure_category` was cleared, leaving the state invalid.
- Add write-before self-validation: `QualificationStateStore._write` validates the
  full payload before atomic replace; invalid writes do not corrupt the original
  file.
- Add offline `repair-state` CLI subcommand: migrates legacy 3-key `usage_totals`
  to 5-key canonical form, clears orphaned `last_failure_at` when
  `last_failure_category` is already `None`, writes a backup, and rejects
  unknown/sensitive/structural damage. Uses a restricted raw-JSON reader
  (envelope-only) so the very issues being repaired can be loaded. Does not
  run at gateway startup.
- Add `known_usage_attempts` field to qualification state records: provides a
  machine-readable distinction between known-usage and unknown-usage attempts.
  `request_count > known_usage_attempts` with zero `usage_totals` is now
  unambiguously "unknown" rather than "zero consumption." Legacy records
  without this field remain loadable.
- Fix usage totals vs attempt ledger disconnect: `record_success` is now deferred
  to after body consumption. Only non-streaming or fully consumed SSE with valid
  normalized usage updates state aggregates. Interrupted SSE, missing usage, and
  malformed usage remain unknown (token totals stay at zero, not misread as
  zero-consumption). No double counting between ledger and state.
- Admit the exact Bailian Beijing Flash 0731 public alias conditionally on its
  local qualification state and TTL; Yunwu Flash remains candidate-only with no
  alias or fallback.
- Add an offline, versioned token-usage estimate for the Bailian Beijing DeepSeek
  V4 Flash route. Estimates stay in a local redacted attempt ledger; no CNY API
  response header is added.

## 0.5.1 - 2026-07-31 (internal release)

- 将根目录 `PROJECT_STRUCTURE.md` 与 `ROADMAP.md` 设为当前架构和规划入口，README 瘦身，历史设计、
  旧计划和阶段性审核移入带有非当前权威标识的归档；
- 新增 provider/model 能力表与协议方言收敛路线：未来能力结论绑定到精确 provider、endpoint、model、
  API surface 和请求方言版本，覆盖 thinking、tool/replay、SSE、Chat Completions 与 Responses API；
- 明确 0.5.x 先积累 `advertised / observed / effective` 能力表和脱敏证据，证据充分后才设计薄 adapter；
  不提前实现统一 Runtime，不把不同供应商或 API surface 视为等价；
- 本版本不改变 v0.5.0 网关运行时、provider 配置、资格状态或消费者 API。

## 0.5.0 - 2026-07-31 (internal release)

- 增加 gateway client Bearer 鉴权；密钥以版本化 key ID/状态/环境变量名登记，启动后仅用 SHA-256
  digest 和 constant-time 比较验证，支持 active 重叠轮换与 revoked 403；
- 增加版本化 public model alias registry、OpenAI-compatible `GET /v1/models` 和 alias facade；
  下游只需 Base URL、gateway key 与唯一 alias，裸 upstream model 歧义明确拒绝；
- 保留显式 route 作为受同样 Bearer 保护的字节透明诊断基准；alias 只披露并改写顶层 `model`，
  HTTP/SSE response body、thinking、tool/replay、usage 和上游错误继续透传；
- 增加 gzip、SSE `[DONE]`/reasoning/usage、429、过期/漂移、无 fallback、key 泄漏 sentinel 和
  OpenAI Python SDK 离线合同测试；
- 增加受控内网 TLS/reverse-proxy 样例、轮换/恢复指南与下游 OpenAI SDK 示例；
- 不包含公网多租户、自动路由/fallback/重试、费用优化、长期 SLA、业务工具执行、验收 B Runtime 或
  `misalignment-platform` 代码改动。

## 0.4.0rc1 - 2026-07-31 (internal test freeze)

- 增加与 Capability Service 分离的 profile-driven 透明聚合网关 MVP；provider API Key
  只保留在网关进程，按显式 `route_id` 转发已资格认证的 OpenAI-compatible 请求；
- 将受控 Probe 演进为 provider profile 资格验证，覆盖目录发现、message、SSE、thinking、
  原生 tool call 与 assistant/tool-result replay；资格默认有效 24 小时、连续五次分类失败提前复核；
- 增加 Yunwu 与阿里云百炼的第一批 profile、网关发现接口、route 门与脱敏状态记录；
- 固定网关上游压缩合同为 `Accept-Encoding: identity`，修复默认 httpx 消费者的 gzip 解码缺陷；
- 记录 2026-07-31 的受控真实接入与短 burst 观察：百炼当前短窗优先，Yunwu 仅作为显式备用/对照；
- 此版本仅适合本机/受控内网内测：尚未实现网关 client API Key、public model alias、自动路由/
  fallback、费用治理、长期稳定性监测或验收 B Runtime。

## 0.3.0 - 2026-07-24

- 将 Capability Service 与 Yunwu 4×3 Matrix Probe 合并到 PVIaN-platform；
- 保留两个原始仓库的 Git 历史，并将旧服务仓库降为历史来源；
- 统一网络授权和 OpenAI-compatible 客户端；
- 增加矩阵报告、Evidence SHA-256 和禁止工具执行的离线校验；
- 发布包同时提供 `pvi-probe` 和 `pvian-probe`。

## 0.2.0 - 2026-07-24

- 增加默认关闭、显式授权的独立在线 Probe Runner；
- 增加 Yunwu OpenAI-compatible 独立环境变量命名空间和端点白名单；
- 增加文本、无副作用原生工具调用、流式 Probe；
- 生成脱敏 Evidence/Probe Run 候选记录，保持人工审核和手动晋升；
- 核心服务、Docker 和 GitHub CI 继续保持无凭证、无网络。

## 0.1.0 - 2026-07-24

首次工程实现：

- Endpoint、Capability Profile、Evidence、Probe Run 和 Resolution 合同；
- 8 个冻结验收 A HTTP 接口；
- 确定性 requirements resolver；
- A1～A6 脱敏 fixtures；
- Windows 与 POSIX 离线验收入口；
- Docker、GitHub Actions、接入示例和架构文档。

本版本不表示完整 PVI 已完成；Turn/SSE Runtime、真实供应商 adapter、真实原平台接入和独立接手审核不在已完成范围。
