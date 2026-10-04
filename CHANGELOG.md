# 变更记录

本项目遵循语义化版本号。日期格式为 `YYYY-MM-DD`。

## [0.4.0] - 2026-10-04

阶段 6：实时自动回复桌面助手。

### 新增

- 新增桌面应用（pywebview + WebView2，DSH 风格界面）：
  - `.venv\Scripts\python.exe -m wechat_mcp.desktop`，或独立 exe 加 `--gui`。
  - 页面：运行状态、群聊设置、模型设置、人设、运行日志。
- 新增实时触发：消息到达即处理，无需 Agent 轮询。
  - 群聊：@我 或 引用/回复我的消息；私聊：每条入站消息（开关可配）。
  - 命中后取该会话最近 N 条上下文（默认 10）+ 人设（可为空）发给大模型并回复。
- 新增 OpenAI 兼容模型客户端（默认 DeepSeek），界面可改 API 地址/Key/模型/
  temperature/max_tokens；含「测试连接」。
- 人设默认**留空**（不注入 system 提示词），支持界面编辑、保存与清空
  （留空即不注入人设）。
- 同一发送者冷却（默认 3 秒）与消息去重，避免刷屏与重复回复。
- 新增会话延续：刚被回复过的人在该窗口内（默认 120 秒，可配 0 关闭）继续
  发言也会接着回复，支持连续对话。
- 配置持久化在 `%APPDATA%\wechat-mcp\bot.json`（可用 `WECHAT_BOT_CONFIG` 覆盖）。

### 修复

- 修复「B 被 @ 后回答的是更早 A 的话题」：触发消息在上下文中标记为
  `[待回复]`，并固定一条中性的行为约束，明确只回答该条消息。
- 上下文条数默认 10 → 20、最大回复 tokens 默认 100 → 300（原值会让回复被截断）。
- 行为约束中要求纯文本回复、禁用 Markdown 标记（微信不渲染）。

### 变更

- `MessageRecord` 新增 `is_at_me` / `reply_to_name` 并由适配层透传；
  适配层新增实时消息订阅（`add_message_listener`）与 `self_names()`。
- 入库 `wechat_bridge.py` 增加最小增量：`WeChatMessage.reply_to_name` 与
  `WeChatBridge._extract_reply_target`（解析引用消息的被引用者显示名）。
- 会话识别改为以会话 ID 为稳定标识：`MessageRecord` 新增 `chat_id`；
  发送前把退化成 ID 的显示名按 username 反查为可搜索名，解析不到则拒绝发送
  （不再把 `xxx@chatroom` 当关键词搜进微信搜索框）；群范围同时接受群名与会话 ID。
- 打包入口支持双模式：默认 stdio（DSH 兼容）、`--gui` 桌面窗口；
  onedir 约 75 MB / zip 约 35 MB。

### 说明

- 自动回复仅在程序运行期间有效；已验证冻结 GUI 真实触发 DeepSeek API（200）
  并回复，stdio 模式仍枚举 9 个工具。93 项单元测试全部通过。

## [0.3.0] - 2026-10-04

阶段 5：打包分发（独立运行时 + DeepSeek Harness 插件）。

### 新增

- 新增 `packaging/`：PyInstaller 打包脚本与 spec，产出 Windows onedir 独立运行时
  （约 71 MB 原始 / 32.7 MB zip），并自动打包为 `wechat-mcp-win32-x64.zip`。
- 新增 `dsh-plugin/`：DeepSeek Harness（DSH）插件 npm 包 `dsh-wechat-mcp`，通过官方
  MCP 客户端桥接（`@deepseek-ai/dsh-mcp-client`）以 stdio 启动随包内置的运行时，
  微信工具以 `mcp__wechat__*` 暴露给模型；安装命令为
  `dsh plugin --profile web add dsh-wechat-mcp`。

### 变更

- 冻结打包时**内置**上游 `wechat_bridge.py`，独立运行时不依赖
  `DEEPSEEKGIRL_PATH`；源码模式行为保持不变。
- 打包会剔除 `cv2` / `numpy` / `imageio_ffmpeg` / `winsdk` 等仅用于朋友圈、媒体下载
  的惰性重依赖，避免体积膨胀到约 341 MB。

### 说明

- 上游 `wechat_bridge.py` 已收录进仓库（`packaging/wechat_bridge.py`），并在文件头
  与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 标注来源；上游项目未声明许可证，
  再分发前请自行确认授权情况。构建不再依赖外部 `DEEPSEEKGIRL_PATH`，仓库 CI 可自动构建。
- 已通过官方规范校验 `dsh-plugin-standard`（0 FAIL）与真实微信联调
  （打包内运行时枚举 9 个工具并成功连接）。

## [0.2.0] - 2026-10-04

阶段 4：补充 P1 能力与发布文档。

### 新增

- 新增 P1 工具：
  - `search_contact`：按名称搜索联系人/群聊，同名返回候选列表，不自行猜测。
  - `get_chat_info`：返回会话名称、类型、观察到的消息数、最后时间与关注状态。
  - `send_file`：发送本地文件，含目录白名单、可配置确认策略与结构化结果。
  - `get_recent_messages`：基于 `next_seq` 游标的增量消息读取。
  - `set_monitored_chats`：白名单/黑名单过滤读取结果，支持运行时增删与配置预设。
- 新增操作审计（`wechat_mcp.audit`）：以 JSONL 记录工具名、目标、时间、结果与
  错误码，默认不记录聊天正文，可通过环境变量开关与指定路径。
- 新增会话关注列表模块（`wechat_mcp.monitoring`）：`allow`（空=关注全部）与
  `block`（优先级更高）两种模式，仅过滤本服务读取结果。
- 新增文件路径白名单校验与 `..` 路径穿越防护。
- 新增发送确认机制：`WECHAT_REQUIRE_CONFIRM=1` 时需显式 `confirm=true`。
- 新增文档：`README.md`、`docs/TROUBLESHOOTING.md`、本变更记录。

### 变更

- `send_message` 增加可选参数 `confirm`。
- 新增错误码：`file_invalid`、`path_not_allowed`、`confirmation_required`。
- 发送状态新增 `confirmation_required`。
- 新增环境变量：`WECHAT_SEND_DIRS`、`WECHAT_REQUIRE_CONFIRM`、
  `WECHAT_AUDIT_ENABLED`、`WECHAT_AUDIT_LOG`、`WECHAT_MONITOR_MODE`、
  `WECHAT_MONITOR_ALLOW`、`WECHAT_MONITOR_BLOCK`。

### 说明

- `send_file` 默认拒绝发送任何文件，需通过 `WECHAT_SEND_DIRS` 显式放宽。
- 读取能力仍基于被动监听缓冲，仅包含来自其他账号的入站消息。

## [0.1.0] - 2026-10-04

阶段 1–3：MVP。

### 新增

- 抽离独立的微信自动化适配层（复用 `deepseekgirl` 的 `WeChatBridge`，不导入其
  机器人业务逻辑），返回结构化 dataclass 结果。
- 基于 Python MCP SDK 的 Server（stdio 传输），注册 P0 工具：
  `get_wechat_status`、`get_chat_list`、`get_chat_history`、`send_message`。
- 目标校验、同名消歧、发送串行化与超时放宽（原硬编码 5 秒 → 可配置 20 秒）。
- 工具接口文档、示例配置与端到端测试记录。