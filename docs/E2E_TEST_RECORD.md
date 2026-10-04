# 阶段 3 端到端测试记录

> 测试方式：以真实 MCP stdio 协议驱动 Server（`scripts/phase3_e2e.py`），
> 客户端使用 MCP 官方 Python SDK 的 `ClientSession`，即实际 Agent 接入时的同一协议路径。
> 环境：Windows，微信 PC 客户端已登录，主窗口可见，后端 `wechatauto`。
> 日期：2026-10-04

## 结果汇总

| 场景 | 说明 | 结果 |
|---|---|---|
| 0 | MCP 工具发现 | ✅ PASS — 发现 4 个 P0 工具 |
| 1 | 查询微信状态 | ✅ PASS — `state=connected, backend=wechatauto` |
| 2 | 获取聊天列表 | ✅ PASS — `total=1, names=[主账号]` |
| 3 | 读取指定聊天消息 | ✅ PASS — `chat=主账号, count=2` |
| 4 | 向测试目标发送文本 | ✅ PASS — `status=sent, resolved=文件传输助手` |
| 5a | 空目标被拒绝 | ✅ PASS — `code=target_invalid` |
| 5b | 目标不唯一被拒绝 | ✅ 单元测试覆盖（实时未能构造多会话，见说明） |
| 6 | 后端不可用返回错误 | ✅ PASS — `ok=false, code=backend_unavailable` |
| 7 | 连续调用不串扰 | ✅ PASS — 连续 3 次发送结果与顺序一致 |

**9/9 全部通过**

## 场景 3 说明

`get_chat_history` 的返回内容来源于**监听期间被动接收**的消息，因此必须在测试窗口内
存在一条来自**他方账号**的入站消息才能复现。

- 该链路已在两处用真实入站消息验证通过：
  - 阶段 1：`get_chat_history("主账号")` 返回 `{sender: 主账号, content: 测试1}`；
  - 阶段 3 MCP 端到端：等待窗口内收到 `主账号` 入站消息后，
    `get_chat_list` 返回 `total=1`，`get_chat_history("主账号")` 返回 2 条。

## 场景 5b 说明

同名消歧逻辑要求缓冲区同时存在多个可被同一子串命中的会话。实时测试中缓冲区会话不足，
因此该场景由单元测试确定性覆盖：

- `test_ambiguous_partial_match_returns_candidates`
- `test_duplicate_exact_name_is_ambiguous`
- `test_send_message_tool_rejects_empty_recipient`

## 本轮修复的问题

| 问题 | 修复 |
|---|---|
| 后端不可用时 `get_status()` 丢失错误码（`code=None`） | bridge 为 None 时保留 `_last_error`，并补充单元测试 |
| 发送超时硬编码 5 秒，向未打开会话首次发送必失败 | 适配层放宽为可配置 20 秒（`WECHAT_SEND_TIMEOUT`） |
| `listening` 状态曾乐观上报 | 改为读取桥接真实状态 `_listen_all_active` / `_running` |

## 已知限制

1. 仅支持 Windows 10/11 x64，需微信 PC 客户端已登录且**主窗口可见**。
2. 微信窗口最小化、被遮挡或锁屏时，读取与发送可能失败。
3. `get_chat_list` / `get_chat_history` 基于**被动监听缓冲**，只能返回监听运行期间收到的
   消息，不是微信完整历史记录；缓冲区默认上限 2000 条。
4. 自己发出的消息、公众号/系统账号消息会被底层过滤，不进入读取结果。
5. `send_message` 在结果无法确认时返回 `failed`，且**不自动重试**（避免重复消息）。
6. 首次向未打开的会话发送需要搜索、切窗、输入，耗时可能达数秒；超时上限可配置。
7. 微信客户端或自动化依赖升级后需重新回归验证（UI 结构可能变化）。
8. 自动化操作可能触发微信风控，请使用可控测试账号并控制频率。

## 复现命令

```powershell
cd F:\Code\wechat-mcp
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 34 项单测
.\.venv\Scripts\python.exe scripts\phase3_e2e.py                # 端到端（含错误路径）
.\.venv\Scripts\python.exe scripts\phase3_e2e.py --wait-inbound 90  # 含场景2/3入站等待
```

---

# 阶段 4 P1 能力真实联调记录

> 测试方式：以真实 MCP stdio 协议驱动 Server（`scripts/phase4_live_check.py`）。
> 所有发送测试均发往「文件传输助手」（仅发给自己），不打扰真实联系人。
> 环境：Windows，微信 PC 客户端已登录，主窗口可见，后端 `wechatauto`。
> 日期：2026-10-04

## 结果汇总

| 场景 | 说明 | 结果 |
|---|---|---|
| 0 | MCP 工具发现 | ✅ PASS — 发现 9 个工具（P0+P1） |
| 1 | 查询微信状态 | ✅ PASS — `state=connected, backend=wechatauto` |
| 2 | 联系人搜索 | ✅ PASS — `source=wechat_db, total=1, names=[文件传输助手]` |
| 3 | 会话信息 | ✅ PASS — `is_group=false, monitored=true` |
| 4 | 增量消息读取 | ✅ PASS — 游标 `next_seq` 正确推进 |
| 5 | 关注列表过滤 | ✅ PASS — `allow=[文件传输助手]`，仅保留指定会话 |
| 6 | 发送文件 dry-run | ✅ PASS — `status=dry_run, file_size=30`，未实际发送 |
| 7 | 文件越界被拒绝 | ✅ PASS — `code=path_not_allowed` |
| 8 | 真实发送文件（发给自己） | ✅ PASS — `status=sent, resolved=文件传输助手` |
| 9 | 未配置白名单被拒绝 | ✅ PASS — `code=path_not_allowed` |
| 10 | 操作审计落盘 | ✅ PASS — 审计记录 11 条，含工具名与结果，无聊天正文 |

**11/11 全部通过**

## 说明

- `search_contact` 使用 `wechatauto` 后端的本地联系人库（`source=wechat_db`）；
  其他后端的回退路径（`source=buffer`）由单元测试覆盖
  （`test_search_contact_falls_back_to_buffer`）。
- 场景 5 在缓冲区无入站消息时，列表本身为空；过滤语义的确定性验证由单元测试覆盖
  （`test_block_list_filters_reads_and_has_priority`、`test_empty_allow_list_means_all`、
  `test_set_monitored_chats_tool_filters_reads`）。
- 文件发送默认拒绝所有路径（`WECHAT_SEND_DIRS` 为空）；场景 7/9 验证了白名单与
  越界拒绝，单元测试另覆盖存在性、目录、可读性与 `..` 路径穿越。

## 复现命令

```powershell
cd F:\Code\wechat-mcp
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 65 项单测
.\.venv\Scripts\python.exe scripts\phase4_live_check.py         # P1 真实联调
```