# WorkBuddy Manager v2.8.4 Issue 审查

> 审查快照：2026-10-24。代码基线为 `v2.8.3`（`90c0638`），上游 issue
> 取自 `ithtelab/workbuddy-manager`；当前工作树的远端是
> `AEmbers/workbuddy-manager`。本文只做内部排期与验收，不是 Release Notes，
> 也不代表已经修改或关闭任何 GitHub issue。
>
> 本次实际有 16 条 open issue。用户列出的 #270、#269、#268、#267、
> #265、#264、#259、#258、#256、#255、#254、#234、#233、#232、#134
> 全部覆盖；另外补上同一批 open 的 #263。

## 1. 发布结论

### 2.8.4 阻塞项（P0）

| 项目 | 原因 | 发布前必须满足 |
|---|---|---|
| #268 Windows 首次启动失败 | 内置 Python 被用户级 site-packages 覆盖，依赖自愈分支又不可达 | 新安装与覆盖安装都能在污染过的用户环境中启动 |
| #258 Responses 结构化输出失效 | `text.format` 在转换层被丢弃，Codex guardian 无法拿到 JSON | `json_schema` / `json_object` 请求有明确约束和回归测试 |
| #259 国内网络重建上游超时 | 随包 `Dockerfile` 的 `go mod download` 默认走不可达的 Go 代理 | 国内网络无代理配置可完成重建；补丁已进上游资产 |
| LOCAL-IMAGE-BUDGET 图片 10 万预算 | 历史内联图片累计超上游约 100k 图片预算，旧会话重试仍会失败 | 请求发出前按新旧梯度压缩；旧会话无需手工改 JSON |

### 2.8.4 之后的计划项（P1，不随本次发布）

| 项目 | 目标 |
|---|---|
| #265 会话粘性绕过三因子权重 | 首绑按积分、快过期积分、闲置权重分配，已有绑定不变；排到 2.8.5 |
| #264 国际版错误尝试领养 Buddy | 国际版不再请求不存在的 `first_buddy` / `chat_5` 任务链；排到 2.8.5 |
| #255 国际版密钥调用同名模型的路由歧义 | 由密钥 realm 明确路由，避免白名单和前缀产生互相矛盾；排到 2.8.5 |
| #256 自动签到偶发 10001/HTTP 500 | 串行错峰，区分“已签”和“服务端临时失败”，对临时失败退避重试；排到 2.8.5 |
| #234 国际版重复要求重新登录 | 用 v2.8.4 的上游身份头与 11-128 分类做回归；无稳定复现证据前不改身份头 |

### 明确不进入 2.8.4

- #270 主动探测矩阵：功能价值高，但 API、限速、UI 与账号安全边界都较大。
- #269 开机自启：Windows 原生增强，适合 2.8.5，不应挤占本次稳定性修复。
- #267 日志显示推理档位：需要数据库迁移和多协议取参，适合作为后续可观测性增强。
- #263 积分收支图型切换：纯界面增强，不影响发布正确性。
- #233 客户端渠道自动切换：高风控风险，先取证再设计，不能把 429 放大成多倍请求。
- #232 日志账号未回填：目前缺 Windows 现场信息；先补诊断，不在原因未知时盲修。
- #134 工具/图片总括 issue：已有多个修复覆盖，先做真实客户端 smoke，仍失败再拆新 issue。
- #254 维护状态提问：无代码范围，2.8.4 的 Release Notes 与 README 给明确答复后关闭。

## 2. Open Issue 逐项方案

### #270 按“账号 × 模型”主动探测

- **类型**：Feature。
- **根因/需求**：现有 `probe_account()` 只能测一个账号且默认模型是 `glm-5.2`，管理端路由也不接受模型参数；当前能力回答不了某个账号对某个模型是否可用。
- **建议方案**：复用直连腾讯的测活路径，把返回改为结构化结果。新增批量探测 API，接收账号集合和模型集合；同账号串行、账号间限并发，每步至少间隔 500ms。按 200、6004、402/14018、401/12153、11102 和请求级错误分类；6004 后不再重试同一账号/模型。界面把 `cn:` / `global:` 同名项合成一行，但分别保留真实状态。探测前显示请求总数和预计等待时间。
- **预计范围**：M-L。`server/services/tencent.py`、`server/routers/accounts.py`、账号页、类型与 i18n；不需要改上游。
- **是否纳入 2.8.4**：否，排到 2.8.5/2.9。
- **验收测试**：mock 上游覆盖六类结果；同一账号请求时间戳满足串行与间隔；6004 后零重试；并发上限生效；`cn/global` 合并不丢状态；页面显示预计请求数。

### #269 Windows 开机自启（静默到托盘）

- **类型**：Feature。
- **根因/需求**：`MainForm` 已创建 `NotifyIcon`，但没有 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 项，也没有静默启动参数。覆盖安装和卸载也未清理启动项。
- **建议方案**：启动器设置增加“开机自动启动（最小化到托盘）”，默认关闭。用当前用户 Run 键，值保存完整 exe 路径与固定参数。参数命中时不先显示主窗体；托盘注册失败则回退显示主窗体。卸载时删除 Run 值。第二个实例启动时优先唤醒已有窗口，不弹“已在运行”后直接退出。
- **预计范围**：M。`windows/launcher/src/Program.cs`、`MainForm.cs`、`AppSettings.cs`、`UninstallForm.cs`、Windows 构建与脚本测试。
- **是否纳入 2.8.4**：否，计划 2.8.5。
- **验收测试**：安装后开关默认关闭；开启后 Run 值指向正确 exe；真实重启不闪窗且托盘可用；模拟 explorer 未就绪时回退主窗口；卸载后 Run 值消失；损坏参数不会创建第二个实例。

### #268 Windows 首次运行环境准备失败

- **类型**：Bug，P0。
- **根因/需求**：内置 CPython 会先读取用户级 `AppData\Roaming\Python\Python311\site-packages`，旧的 `typing_extensions` 可遮蔽随包版本。`prepare.ps1` 在 `$ErrorActionPreference='Stop'` 下执行 native Python，stderr 会先变成终止错误，导致 `$LASTEXITCODE` 修复分支不可达；错误详情又被 `2>$null` 丢弃。准备阶段也早于 UTF-8 环境变量设置。
- **建议方案**：在 `windows/python-env.ps1` 统一设置 `PYTHONNOUSERSITE=1`，所有 dot-source 脚本继承。依赖自检临时切换为 `Continue`，捕获输出和退出码后恢复原设置；失败时显示真实 stderr，并继续尝试安装 `server/requirements.txt`。在调用准备脚本前设置 UTF-8，或在 `python-env.ps1` 一并统一。
- **预计范围**：S-M。`windows/python-env.ps1`、`prepare.ps1`、`start.ps1`、`service-tools.ps1`、`update.ps1`、`server/tests/test_windows_scripts.py`。
- **是否纳入 2.8.4**：是，P0。
- **验收测试**：构造用户级旧 `typing_extensions` 后仍能导入；缺关键包时进入修复分支并输出完整异常；`PYTHONNOUSERSITE` 被所有子进程继承；中文/emoji 日志不产生编码异常；覆盖安装和首次安装各跑一次真实启动。

### #267 请求日志显示推理档位

- **类型**：Enhancement / Observability。
- **根因/需求**：`to_chat_request()` 只把 Responses 的 `reasoning.effort` 转成上游 `reasoning_effort`，随后没有写入请求日志；`request_logs` 也没有档位列。因此日志只能看到模型，无法区分 high/max。
- **建议方案**：新增可空的 `reasoning_effort` 列和迁移。Chat 路径读取请求体 `reasoning_effort`，Responses 读取 `reasoning.effort`；未知或未传保存 `NULL`，不能按模型默认值猜测。日志表格在模型名旁显示短 badge，详情页显示原文。若能在不增加请求成本的前提下从上游响应取得实际档位，以实际值为准，否则明确标为“请求档位”。
- **预计范围**：M。`server/db.py`、`server/routers/gateway.py`、`server/routers/responses.py`、`server/routers/logs.py`、日志页、类型、i18n、迁移测试。
- **是否纳入 2.8.4**：否，P2，排到 2.8.5。
- **验收测试**：Chat 传 high、Responses 传 max 均入库并显示；未传显示“未指定”而非模型默认值；旧数据库迁移后历史行为 `NULL`；四类协议路径不因日志旁路异常影响响应。

### #265 会话粘性首绑绕过三因子权重

- **类型**：Bug / 选号策略，P1。
- **根因/需求**：默认开启会话粘性时，首绑使用 `hashIndex(key, n)`，只看会话键字节；已有绑定又一直续期，实际流量走不到 `Pool.pick` 的三因子权重。于是没有快过期积分的账号可独占会话流量。
- **建议方案**：采用 PR #266 的方向：只改首绑，在候选段内按 `credits × 10 + 快过期积分 × 8 + 闲置补偿` 抽签；已有绑定不重算。首绑不做成本硬过滤、不做 Top-5 排序截断、不推进 `lastUsed/usedSeq`。回调缺失或返回候选外 UID 时回落到旧哈希语义。
- **预计范围**：M。上游补丁、pool/session/wiring、Go 单测和说明文档；面板侧不改。
- **是否纳入 2.8.4**：是，P1。
- **验收测试**：在已应用 0001-0019 的干净基线运行 PR #266 的 pool/session/cmd 测试；1000 次确定性抽样中快过期号显著占优；粘性命中只调用一次选择回调；空候选/候选外 UID 正确回落；模型级 6004 冷却只影响该模型。全量 Go 测试若仍有 `cmd/stats` 基线失败，必须在未打补丁的基线上复现并单独记录。

### #264 国际版每天错误尝试领养 Buddy

- **类型**：Bug，P1。
- **根因/需求**：国际版任务中心实际只有 `first_chat / skill_installed / wechat_linked / expert_summoned / template_used`，没有 `first_buddy` / `chat_5`。9:00 旅行巡检有 `IsGlobal()` 门控，但 10:00 活跃上报后的 `travelAdoptForce()` 没有，导致每日一次无效 400。
- **建议方案**：在上游 `travelAdoptForce` 入口增加与 `runTravel` 相同的 global 门控，直接返回。管理端任务日志不再把“conversation threshold not reached”翻译成失败告警，而是不产生领养记录；如果旧日志仍被采集，标成“版本不适用”而非失败。
- **预计范围**：S-M。新上游补丁（建议接在最终编号之后，例如 `0022-global-travel-adopt-gate.patch`）、scheduler 单测、tasklog 映射与测试。
- **是否纳入 2.8.4**：是，P1。
- **验收测试**：global 活跃上报完成后 `buddy/first` 请求数必须为 0；cn 行为不变；同一 9 个 global 账号连续两轮日志无 adopt 失败；Go scheduler 测试与 Python tasklog 测试通过。

### #263 积分收支增加图型切换

- **类型**：Enhancement / UI。
- **根因/需求**：当前双轴折线让收入与支出不可直接比较，净增只有数字没有图形；后端 `daily[].net` 已存在，不需要数据接口改动。
- **建议方案**：保留现有折线视图，增加“折线 / 柱状”切换并记忆选择。柱状用单轴 `ComposedChart`，支出映射为负值，零线突出，净增叠线；Tooltip 把支出显示为绝对值。默认建议先保持折线，避免升级后视图突变；是否把柱状设为默认应结合 Release Note 单独决定。
- **预计范围**：S-M。`web/app/(main)/dashboard/page.tsx`、i18n 与界面验收；无后端改动。
- **是否纳入 2.8.4**：否，P2。
- **验收测试**：1/3/7/15/30 天均能切换；支出 Tooltip 不为负；净增负值正确；刷新后保留选择；键盘可达；窄屏不溢出；旧折线视图无回归。

### #259 国内网络更新卡在 `go mod download`

- **类型**：Bug / 更新链路，P0。
- **根因/需求**：上游构建阶段未设 `GOPROXY`，默认访问 `proxy.golang.org`；国内网络超时后“全部更新”停在同步随包上游。更新器会覆盖本地 `Dockerfile`，手改不能持久。
- **建议方案**：采用 PR #260。在上游 `Dockerfile` 构建阶段设置 `ARG GOPROXY=https://goproxy.cn,direct` 和 `ENV GOPROXY=${GOPROXY}`，保留 build-arg 覆盖能力。它还只影响构建阶段，运行时行为不变。合并后必须重打 `upstream-src` 资产。
- **预计范围**：S。上游 Dockerfile 补丁、文档、资产重打包与国内网络构建验收。
- **是否纳入 2.8.4**：是，P0。
- **验收测试**：在无 `proxy.golang.org` 直连的干净环境 `docker compose build --no-cache` 成功；上游 `/healthz` 为 200；产物二进制与普通代理构建一致；自定义 `GOPROXY` build-arg 生效；发布 CI 的补丁计数与 `go build/test` 通过。

### #258 `/v1/responses` 丢弃 `text.format`

- **类型**：Bug / Codex 兼容，P0。
- **根因/需求**：`to_chat_request()` 只挑固定字段，未读取 `body["text"]["format"]`。上游又实测忽略非法或有效 `response_format`，因此仅透传字段也不能保证 JSON，Codex guardian 会持续报 `guardian assessment was not valid JSON`。
- **建议方案**：解析 `json_object` 与 `json_schema`；向 Chat 请求写入对应 `response_format`，同时追加一条明确的 developer/system 约束，包含完整 schema、只输出 JSON、不包 Markdown 围栏。对结构化输出来做一次本地 JSON 解析校验；解析失败时只进行一次受控重试，仍失败则返回可诊断错误，不能把普通文本伪装成合法 JSON。保留原始 schema 需有大小上限，防止请求体放大。
- **预计范围**：M。`server/routers/responses.py`，可能复用转换后的 system message；`server/tests/test_responses_api.py` 或新增结构化输出测试。
- **是否纳入 2.8.4**：是，P0。
- **验收测试**：`json_schema` 与 `json_object` 都出现在发往 mock 上游的 payload 中；提示词包含 schema 且无 Markdown 围栏；输出合法 JSON 时原样返回；输出围栏 JSON 可安全规范化；两次非法输出后返回明确错误；Codex guardian 的真实审批 smoke 通过；普通非结构化文本请求零变化。

### #256 自动签到偶发 `code=10001` / HTTP 500

- **类型**：Bug，P1。
- **根因/需求**：当前 `checkin()` 只按业务码把 10001 统一解释成“今日已签到”，但现场日志是 HTTP 500 + `签到失败，请稍后重试`，两种 10001 语义被混为一谈。批量签到默认并发 5，且可能与管理端和上游两个 9/21 点调度重叠，形成短时间风暴。
- **建议方案**：签到结果至少保留 HTTP 状态、业务码和原文；“今日已签”只在状态和文案与幂等语义一致时成立。HTTP 500/10001 改判为临时失败，按 2-5 秒抖动后重试 1-2 次；批量默认降为串行或低并发并加入账号间 jitter。若管理端签到计划开启，界面提示上游调度也会签到，避免双重触发。汇总区分 `fail_checkin` 与 `fail_resource`。
- **预计范围**：M。`server/services/tencent.py`、`server/routers/accounts.py`、`server/config.py`、`.env.example`、签到记录 UI/类型与测试。
- **是否纳入 2.8.4**：是，P1。
- **验收测试**：HTTP 500 + `签到失败` 不能记成已签；HTTP 200 + “今日已签到”仍幂等成功；临时失败重试后成功；重试上限与抖动可测；12 账号批量默认不再同时发出；余额查询失败不计入签到失败；国际版仍零请求跳过。

### #255 国际版密钥调用同名 deepseek 的路由歧义

- **类型**：Bug / Product boundary，P1。
- **根因/需求**：当前路由完全由实际模型名的 `global:` 前缀决定，密钥只做 realm 约束；`global:` 又保留在白名单比对中。结果是一个限定国际版的密钥如果请求裸名会收到 `realm_mismatch`，而补 `global:` 后若白名单只写了裸名又会收到 `model_not_allowed`。仓库没有“按密钥绑定某个账号”的语义。
- **建议方案**：当密钥明确限定 `realm=global` 且请求模型无前缀、也没有映射到另一个 realm 时，在鉴权前规范化为 `global:<model>`；`realm=cn` 对称处理 `cn:`。白名单对限定 realm 的密钥接受“裸名或同 realm 前缀”两种写法，但下游列表仍下发实际可调用名。无 realm 密钥继续保留显式前缀规则，避免改变现有裸名走 CN 的语义。密钥编辑页直接展示最终 wire name，不再让用户手猜。
- **预计范围**：M。`server/routers/gateway.py`、`server/routers/responses.py`、`server/keysvc.py`、密钥表单、文档与多协议测试。
- **是否纳入 2.8.4**：是，P1；若实现中发现会改变无 realm 密钥的现有路由则立即缩回，只做显式提示与白名单校验。
- **验收测试**：global 密钥 + 裸 `deepseek-v4.1-flash` 最终调用 `global:deepseek...`；白名单写裸名或 global 前缀都可用；CN 账号冻结不影响 global 候选；无 realm 密钥行为不变；日志 realm 为 global；错误提示不再要求用户同时修前缀和白名单。

### #254 项目是否会被“大手制裁”

- **类型**：Question / Maintenance policy。
- **根因/需求**：原上游公开仓库消失、旧社区项目停更，用户担心本项目也停止维护。
- **建议方案**：不把它当 bug。AEmbers fork 建立独立维护分支，在 README、README.en、Release Notes 说明维护主体、版本来源、上游源码随包策略、停止维护时的数据迁移/导出承诺。不要承诺规避平台风控、永久可用或替代官方客户端。
- **预计范围**：S，纯文档与发布说明。
- **是否纳入 2.8.4**：纳入发布说明，不占代码排期；发布后在原 issue 给出维护边界并关闭。
- **验收测试**：README 中英文一致；Release Notes 写明 fork 地址和独立维护；备份/导出入口仍可访问；没有声称保证账号永久可用。

### #234 国际版账号重复提示需要重新登录

- **类型**：Bug / regression verification，P1。
- **根因/需求**：原现场混有两个问题：Docker socket 不可用只影响“重载上游”，不是账号失效；国际版短暂 11-128 又被旧上游误判为封禁。当前 v2.8.3 已有多轮修复，但 issue 最新评论仍指出默认请求头配置与“官方版本对齐”说明可能不一致，因此不能仅凭旧修复直接关闭。
- **建议方案**：先用 v2.8.4 当前随包上游复测 global 账号。若失败，抓取脱敏后的请求头族、状态码、业务码和上游日志，确认是 UA/渠道身份、11-128 分类还是 token 真失效。不要根据“需要重新登录”UI 文案直接删号；先检查原 token、刷新结果和 `/status`。若头部说明与实际不一致，修正配置文案或实际头族，不能写“已对齐”却继续透传随机 UA。
- **预计范围**：先 S 做现场诊断；确认代码缺陷后再定级。可能涉及上游身份头与账号状态机，非纯 UI。
- **是否纳入 2.8.4**：纳入回归验收门；若 2.8.4 发布候选仍稳定复现则升 P0。无复现证据前不做猜测性改动。
- **验收测试**：global 账号连续 30 分钟多轮请求保持在线；12153 仅真实会话失效时触发；11-128 不写永久禁用；docker socket 缺失只提示重载失败；UI 显示上游 `.upstream-id`；默认头配置与页面说明一致。

### #233 CLI / VS Code / WorkBuddy 渠道自动切换

- **类型**：Enhancement / Risk-sensitive。
- **根因/需求**：上游只呈现一种出站身份，429 或渠道校验时没有退路；日志也缺“客户端渠道”维度。现有维护者已明确：贸然切换头族可能触发更严厉校验。
- **建议方案**：先做只读取证，不做自动多倍重试。为请求日志增加客户端渠道识别和渠道校验原始摘要；收集足够样本后，才评估“同账号、明确渠道错误、仅重试一次”的窄策略。渠道不可由普通密钥任意指定；必须有固定白名单、单次预算和可关闭开关。
- **预计范围**：L。涉及上游头族、选号冷却、日志 schema、密钥/模型配置、前端统计和风控测试。
- **是否纳入 2.8.4**：否，高风险功能，排到有真实样本后的版本。
- **验收测试**：渠道分类可观测；无样本时不切换；若将来启用，每次请求最多一次路径切换；429 不触发无限重试；冷却按账号/渠道而非全池误伤；日志显示渠道与最终路径。

### #232 请求日志“账号”一直未采集

- **类型**：Bug / Needs reproduction。
- **根因/需求**：账号不是网关直接知道的，而是后台从上游日志按结束时间回填。Windows 原生模式依赖 `WB2API_MODE=native`、可读且持续写入的 `WB2API_LOG_FILE`。现有 issue 没有提供这些现场值和 collector warning，不能判断是路径、日志格式、旧版模型名截断还是采集间隔问题。
- **建议方案**：先把“未采集”从无限等待改成可诊断状态：返回 collector source（docker/native）、日志路径是否可读、最后成功时间、上次错误、匹配成功/失败计数，并提供“立即采集”按钮。Windows 设置页校验日志路径。若账号回填持续失败，显示具体修法，而不是永久“稍后自动回填”。
- **预计范围**：S-M。`server/services/accountlog.py`、`server/routers/logs.py`、日志页与 Windows `.env` 校验；真正解析修复取决于现场证据。
- **是否纳入 2.8.4**：否；如果 #268 的 Windows 修复已触碰环境配置，可顺带加入诊断，但不承诺根因修复。
- **验收测试**：Docker 与 native 两种来源各自回填成功；日志路径不存在时 UI 立即说明；最后成功超过阈值时告警；手动采集幂等；模型名长度变更仍能匹配；无日志不影响网关请求。

### #134 Codex 工具调用与 DSH 图片总括

- **类型**：Feature umbrella / Needs fresh smoke。
- **根因/需求**：原始报告混合了 Codex 工具调用、DSH 发图、积分耗尽换号、国际版活跃等多个问题。仓库已分别修过命名空间工具回程（#158）、工具结果图片排序（#174/#177）、Responses 流末尾与工具发现（#182）和思考内容空回复（#135），但 issue 没有一次完整复测。
- **建议方案**：将总括 issue 拆成可验证矩阵：Codex `/v1/responses` 普通工具、namespace 工具、并行工具、工具结果带图、用户直接发图；DSH 分别测 `/v1/responses` 和 `/v1/chat/completions`。2.8.4 图片预算生效后再做真实长会话。只有仍有失败才创建独立 issue，并附模型、协议、事件顺序和脱敏日志。
- **预计范围**：S 验证；若工具链仍坏则 M-L。
- **是否纳入 2.8.4**：纳入 smoke 验收，不默认新增功能代码。
- **验收测试**：Codex 能列工具、调用工具、接收结果并继续；namespace 名正确还原；工具图片不破坏 `assistant.tool_calls → tool` 顺序；DSH 两条协议各有成功/失败样本；长会话中旧图压缩、新图清晰、占位图可按需查看。

### LOCAL-IMAGE-BUDGET 历史图片合计预算与梯度压缩

- **类型**：Local regression / Codex compatibility，P0。
- **根因/需求**：腾讯 Copilot 路由的图片输入有约 100k token 的请求级准入预算；该预算不是 1M 总上下文，也不是账号套餐。历史内联图片累计超过预算时，上游返回近似 `100001 > 100000` 的 `prompt_too_long`。已有会话再次发送时，如果不改请求内容，换账号和重试都不会恢复。
- **建议方案**：把当前本地原型正式移植到 fork 的 Responses→Chat 转换之后、发往上游之前。按 base64 长度估算图片 token，默认 75k 触发、65k 目标、90k 硬上限；按距离当前用户轮的远近从旧到新逐张压缩，每压一张重新估算，达到目标立即停止。建议档位：
  - 当前用户轮：长边 1600、JPEG 90；
  - 最近 1-3 轮：1280、84；
  - 4-8 轮前：1024、76；
  - 9-12 轮前：900、70；
  - 更老：从 720、65 起，必要时继续降到 640/55、480/45。

  只有请求工具中存在 `view_image` 时才允许把更老的图片替换成 `<image_ref id="sha256" path="..." width="..." height="..." note="..."/>`；否则继续降清晰度，不能生成模型无法读取的裸路径。原图按 SHA-256 写到 `data/image_refs`，压缩图写 `data/image_cache`，支持去重、TTL 和容量清理。命中精确的图片预算错误时，用未修改的 Responses body 重建一次并强制激进压缩，最多重试一次。不要把图片内容、base64 或完整路径写进普通日志。
- **预计范围**：M。`server/services/image_budget.py`（新增）、`server/routers/responses.py`、`server/requirements.txt`、`.env.example`、`server/tests/test_image_budget.py`、`server/tests/test_responses_image_budget.py`。
- **是否纳入 2.8.4**：是，P0。
- **验收测试**：低于阈值时不解码、不写缓存；超阈值优先压旧图，最新图保持最高质量；同一 SHA-256 不重复存原图；无 `view_image` 时不产生占位符；有 `view_image` 时可生成占位符且原图存在；强制重试只匹配 `100001 > 100000`，不误伤 1M 文本超限；非流式与开流前错误各重试一次，流开始后不重启；当前两个失败旧会话直接重发即可成功，且无需改写会话 JSON。

## 3. 上游补丁顺序与冲突

当前两个待合并 PR **都使用 `0020` 前缀**：

- PR #260：`dev/upstream-patches/0020-upstream-build-goproxy.patch`
- PR #266：`dev/upstream-patches/0020-sticky-first-bind-weighted.patch`

不能原样同时合并。建议：

1. 先合 PR #260，保留 `0020-upstream-build-goproxy.patch`；
2. 将 PR #266 rebase 并改名为 `0021-sticky-first-bind-weighted.patch`，同步改文档中的补丁编号；
3. #264 的新补丁接在 `0022`；
4. 每合入一条上游补丁，都在干净 `upstream-src` 基线上执行 `apply_upstream_patches.py` 两次，分别验收首次 `applied` 和再次 `already applied`；
5. 所有补丁稳定后重新打包并覆盖 `upstream-src` 载体 Release，再打面板 tag。否则发布 CI 会因补丁计数不一致失败。

补丁文件保持 LF、无 BOM。上游代码保留 MIT LICENSE 与版权声明，不要把完整上游源码重新提交进仓库。

## 4. Closed Issue 对照

以下 closed 项用于避免把已修问题重新排进 2.8.4：

| Issue | 当前事实 | 对本次排期的影响 |
|---|---|---|
| #250 | DSH Web GUI 跨句指纹已在 v2.8.3 修复 | #234 若再出现 11-128，必须抓新样本，不能回退到旧根因 |
| #247 | 国内 DeepSeek 无模型提示实为 11-128/11102 连锁 | #270 必须区分“账号无模型”和渠道/请求错误 |
| #246 | DSH system 首句指纹已改中性句并禁止零宽混淆 | 新收集器继续保留原文，展示层翻译 |
| #245 | `/console` 的 11102 不再写 6 小时模型冷却 | #270 探测同样不能把 11102 计为账号故障 |
| #243 | `host.docker.internal` 与上游端口绑定已在 v2.8.1 修复 | #259 只处理 Go 模块代理，不重开旧端口问题 |
| #241 | GHCR 上游镜像和 OCI label 已加入发布链路 | 2.8.4 必须继续产出 amd64/arm64 两种镜像 |
| #240 | 随包上游同步已存在，问题是旧更新器自举 | 发布包必须继续随包下发 `server/` 与 `deploy/` |
| #239 | `/v1/models` 已按实时模型中心裁剪 | #255 不应通过恢复旧模型来“修路由” |
| #238 | 国际版 DeepSeek max 档已加入 | #267 只记录客户端请求实际档位，不猜测默认值 |
| #236 | 国际版渠道校验现场缺少稳定日志 | #233 先做可观测，不做激进自动换渠道 |
| #231 | 旧上游导致 global 11-128；升级上游并重新启用可恢复 | #234 先验证 `.upstream-id` 与账号状态，不删号重扫 |
| #228 | global 积分刷新已由上游补丁 0015 修复 | #265 的可选号权重现在对 global 也应有真实余额数据 |
| #211 | 下游模态/推理档位补齐，SS 线路不内置 | #134 图片能力不等于内置代理内核，应分开处理 |
| #209 | `X-Conversation-ID` 头族已由上游补 | #234 若再出渠道校验，需确认新头族是否仍被放行 |
| #207 | 日志模型宽度截断已从 26→32 修复 | #232 若 Windows 仍空白，下一嫌疑是日志路径/格式，不是该旧根因 |
| #201 | 11-128 分类和误禁用处理已修 | #234/#233 不应重新把渠道错误写死成永久封禁 |
| #180 | 413 的上限来源与压缩边界已说清 | 图片预算解决的是 100k 图片子预算，不是所有 413 |
| #174/#177 | 工具结果图片排序和 11148 配对已修 | #134/#LOCAL-IMAGE-BUDGET 只能替换图片块，不能破坏消息顺序 |
| #158 | Codex namespace 工具回程已修 | #134 先做真实客户端 smoke，不能仅按旧标题重写一次 |
| #135 | 纯思考无正文时已有可配置回填 | #267 不得把 thinking 文本误当推理档位 |

## 5. 2.8.4 发布资产与门禁

沿用现有 CI 发布方式，tag `v2.8.4` 最终应有：

- `workbuddy-manager-v2.8.4.tar.gz`：一键更新使用的正式包；
- `workbuddy-manager-v2.8.4.tar.gz.sig`：给仍停留在 v1.0.85 及更早更新器的过渡签名；
- `workbuddy-manager-v2.8.4.zip`：手动下载/Windows 用户备用包；
- `WorkBuddyManagerSetup.exe`：Windows 单文件安装器；
- `WorkBuddyManagerSetup.exe.sha256`：安装器摘要；
- `ghcr.io/<fork-owner>/workbuddy-manager:v2.8.4`：多架构管理端镜像；
- `ghcr.io/<fork-owner>/workbuddy2api:v2.8.4`：多架构上游镜像；
- `upstream-src` 预发布载体中的 `workbuddy2api-src.tar.gz`：下一版 CI 默认取用的上游源码快照。

发布顺序必须是“tag → CI 草稿 Release → 下载并核对 tar.gz sha256/包内版本 → 等 EXE 与镜像完成 → 发布草稿”。CI 直接公开或产物未齐时发布都不允许。Release Notes 从 `CHANGELOG.md` 的 `[2.8.4]` 段提取，必须写用户可见变化和升级步骤，不写内部排障过程。

## 6. 合并验收顺序

1. **Windows P0**：#268，先保证安装器能被污染环境用户启动。
2. **协议 P0**：#258，使用 Codex guardian 与非 guardian 两类真实请求回归。
3. **图片 P0**：LOCAL-IMAGE-BUDGET，用两个已失败旧会话直接重发做验收。
4. **上游 P0/P1**：先合 #260，重打资产；再 rebase/renumber #266；随后 #264。
5. **账号任务 P1**：#256、#234、#255，全部用 mock 单测 + 至少一个真实部署 smoke。
6. **全量门禁**：
   - `python -m unittest discover -s server/tests -t . -v`
   - `cd web && npm run lint && npm run build:export`
   - 解包 upstream-src 后 `go build ./...`、`go test ./...`
   - Windows `windows/launcher/build.ps1` 构建 EXE，并在受控用户级 site-packages 污染环境中首次启动
   - Release 草稿下载后运行 `deploy/verify-release.sh`，核对 tar.gz、zip 结构、EXE sha256 和包内 `.version`

所有新增测试必须先证明“修复前失败、修复后通过”。只做代码搜索或只跑正常路径不能作为 #258、#268、图片预算和 #256 的发布证据。
