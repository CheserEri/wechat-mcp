# 工具接口文档（P0 + P1）

WeChat MCP Server 通过 stdio 传输暴露以下工具。所有工具返回结构化 JSON，
失败时返回 `{"ok": false, "code": "...", "message": "..."}`，不会虚报成功。

- P0：`get_wechat_status`、`get_chat_list`、`get_chat_history`、`send_message`
- P1：`search_contact`、`get_chat_info`、`send_file`、`get_recent_messages`、`set_monitored_chats`

## get_wechat_status

检查微信连接状态、窗口可见性与自动化后端。

**输入**：无

**输出**

| 字段 | 说明 |
|---|---|
| `ok` | 是否已连接 |
| `state` | `disconnected` / `connecting` / `connected` / `error` |
| `backend` | 生效的自动化后端（`wechatauto` / `wxauto4` / `wxauto`） |
| `listening` | 消息监听是否真正处于活动状态 |
| `window_visible` | 微信主窗口是否可见 |
| `uia_available` | UI Automation 是否可用 |
| `detail` | 附加诊断信息 |
| `error` | 连接失败时的结构化错误 |

## get_chat_list

返回当前可读取的会话列表。

> 读取基于监听期间**被动接收**到的消息，不是微信完整会话列表。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `limit` | int | 否 | 最大返回数量 |
| `keyword` | str | 否 | 会话名过滤条件 |

**输出**：`ok`、`chats[]`（`name`/`is_group`/`message_count`/`last_message_at`）、
`total`、`complete`、`note`。

## get_chat_history

读取指定会话在缓冲区内最近的消息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 必须明确指定会话名 |
| `limit` | int | 否 | 返回条数，默认 20 |

**输出**：`ok`、`chat`、`messages[]`（`chat`/`sender`/`content`/`message_type`/
`is_group`/`timestamp`/`message_id`）、`complete`、`note`。

## send_message

向明确指定的联系人或群聊发送文本。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `message` | str | 是 | 文本内容 |
| `dry_run` | bool | 否 | 仅校验、不实际发送 |
| `confirm` | bool | 否 | 显式确认发送（当 `WECHAT_REQUIRE_CONFIRM=1` 时必需） |

**输出**：`ok`、`status`（`sent` / `failed` / `dry_run` / `confirmation_required`）、
`recipient`、`resolved_recipient`、`message_preview`、`error`。

**安全约定**

- 目标为空、含控制字符或超长 → `target_invalid`。
- 目标不唯一（同名或多个部分匹配）→ `target_ambiguous`，返回候选列表，拒绝发送。
- 未连接微信 → `not_connected`。
- 开启发送确认且未带 `confirm=true` → `status=confirmation_required`，不发送。
- 超时或结果无法确认 → `status=failed`，**不自动重试**，避免重复消息。

## search_contact

按名称搜索联系人/群聊，返回候选列表。**同名时原样返回多个候选，不自行猜测**。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | str | 是 | 昵称 / 备注 / 微信号片段 |
| `limit` | int | 否 | 最大返回数量，默认 20 |

**输出**：`ok`、`keyword`、`candidates[]`（`name`/`username`/`remark`/`is_group`）、
`total`、`source`、`note`、`error`。

> `source`：`wechat_db` 表示来自微信本地联系人库（后端为 wechatauto）；
> `buffer` 表示后端无本地库，退回为「监听期间观察到的会话名称」。

## get_chat_info

返回指定会话已确认可获取的信息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `chat_name` | str | 是 | 会话名称 |

**输出**：`ok`、`name`、`is_group`、`message_count`、`last_message_at`、
`monitored`（是否在关注列表内）、`note`、`error`。

## send_file

向明确指定的联系人或群聊发送本地文件。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `recipient` | str | 是 | 联系人或群聊名称 |
| `file_path` | str | 是 | 本地文件绝对路径 |
| `dry_run` | bool | 否 | 仅校验文件与目标、不实际发送 |
| `confirm` | bool | 否 | 显式确认发送（当 `WECHAT_REQUIRE_CONFIRM=1` 时必需） |

**输出**：`ok`、`status`、`recipient`、`resolved_recipient`、`file_path`、
`file_name`、`file_size`、`dry_run`、`error`。

**安全约定**

- 路径为空 / 不存在 / 不是文件 / 不可读 → `file_invalid`。
- 未配置可发送目录，或文件不在允许目录内（含 `..` 路径穿越）→ `path_not_allowed`。
- 目标不唯一 → `target_ambiguous`，拒绝发送。
- 发送结果无法确认 → `status=failed`，**不自动重试**。

## get_recent_messages

增量读取监听缓冲区中新增的消息。

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `after_seq` | int | 否 | 上次返回的 `next_seq`，默认 0（从头） |
| `limit` | int | 否 | 单次最大返回条数，默认 20 |
| `chat_name` | str | 否 | 仅返回该会话的消息 |

**输出**：`ok`、`messages[]`、`next_seq`、`complete`、`note`、`error`。

> 调用方应保存 `next_seq` 并在下次调用时作为 `after_seq` 传入。
> 被关注列表过滤掉的消息会跳过，但游标仍向前推进。

## set_monitored_chats

设置会话关注/忽略列表。**仅过滤本服务返回的读取结果，不改变底层监听行为。**

**输入**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `add` | list[str] | 否 | 加入当前模式对应列表的会话名 |
| `remove` | list[str] | 否 | 从两个列表移除的会话名 |
| `mode` | str | 否 | `allow`（白名单，空=关注全部）或 `block`（黑名单） |

**输出**：`ok`、`mode`、`allow`、`block`、`note`、`error`。

**行为约定**

- `allow` 模式且列表为空 → 关注全部会话。
- `block` 优先级高于 `allow`：命中黑名单的会话一律不出现在读取结果中。
- 可在 `WECHAT_MONITOR_MODE` / `WECHAT_MONITOR_ALLOW` / `WECHAT_MONITOR_BLOCK` 预设，
  运行时通过本工具增删。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEKGIRL_PATH` | `F:\Code\deepseekgirl-main` | 复用 WeChatBridge 的原项目路径 |
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_LISTEN_ON_CONNECT` | `1` | 连接后是否启动消息监听 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录（逗号分隔）；空=拒绝发送文件 |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |
| `WECHAT_AUDIT_LOG` | `<项目根>/logs/audit.jsonl` | 审计日志路径 |
| `WECHAT_MONITOR_MODE` | `allow` | 关注列表模式 `allow` / `block` |
| `WECHAT_MONITOR_ALLOW` | 空 | 预设白名单（逗号分隔，空=全部） |
| `WECHAT_MONITOR_BLOCK` | 空 | 预设黑名单（逗号分隔） |

## 客户端配置示例

```json
{
  "mcpServers": {
    "wechat": {
      "command": "F:\\Code\\wechat-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "wechat_mcp.server"]
    }
  }
}
```

## 已知限制

- 需要 Windows 10/11 x64 且微信 PC 客户端已登录、主窗口可见。
- 读取能力基于被动监听缓冲，只能获取监听运行期间收到的消息。
- 微信窗口被遮挡、最小化或锁屏时可能失败。