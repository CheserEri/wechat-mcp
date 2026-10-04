# 变更记录

本项目遵循语义化版本号。日期格式为 `YYYY-MM-DD`。

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