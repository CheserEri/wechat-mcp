# 故障排查

按「先看结构化错误码，再定位根因」的顺序排查。所有工具失败都会返回
`{"ok": false, "code": "...", "message": "...", "detail": {...}}`。

## 错误码速查

| 错误码 | 含义 | 常见原因 |
|---|---|---|
| `backend_unavailable` | 自动化后端/依赖不可用 | `DEEPSEEKGIRL_PATH` 错误、依赖未安装 |
| `not_connected` | 尚未连接微信 | 微信未登录、主窗口不可见、连接失败 |
| `connect_failed` | 连接失败 | 微信进程/窗口异常 |
| `target_invalid` | 目标不合法 | 名称为空、含控制字符、超长 |
| `target_ambiguous` | 目标不唯一 | 存在同名或部分匹配的多个会话 |
| `file_invalid` | 文件路径不合法 | 文件不存在、不是文件、不可读 |
| `path_not_allowed` | 文件不在允许目录 | 未配置 `WECHAT_SEND_DIRS` 或文件越界 |
| `confirmation_required` | 需要显式确认 | `WECHAT_REQUIRE_CONFIRM=1` 但未传 `confirm=true` |
| `unsupported_operation` | 当前后端不支持 | 该能力依赖特定后端 |

## 连接失败 / `not_connected`

1. 确认微信 PC 客户端已启动并登录。
2. 确认微信主窗口**可见**：最小化、锁屏、被其他窗口完全遮挡都可能导致失败。
3. 调用 `get_wechat_status` 查看 `window_visible`、`uia_available`、`backend`。
4. 若 `backend` 为空或 `uia_available=false`，检查自动化依赖是否安装：

   ```powershell
   .venv\Scripts\python.exe -m pip install -e ".[wechat]"
   ```

## `backend_unavailable`

- 检查 `DEEPSEEKGIRL_PATH` 是否指向包含 `src\wechat_bridge.py` 的原项目目录。
- 该错误在 `get_wechat_status.error` 中会保留，便于诊断。

## `get_chat_list` / `get_chat_history` 结果为空

这是**预期行为**，不是 Bug：

- 读取能力基于监听回调收集的消息，只能获取适配层启动后收到的新消息。
- 结果只包含**来自其他账号的入站消息**；自己发送的消息和系统号消息会被过滤。
- 若刚启动服务，请在监听到他方消息后再查询；或调用 `get_recent_messages` 观察增量。

## 发送超时或 `status=failed`

- 向**尚未打开**的会话首次发送需要搜索、切换、输入，耗时较长。底层发送超时默认
  放宽到 20 秒，可通过 `WECHAT_SEND_TIMEOUT` 调整。
- 结果无法确认时返回 `failed`，**不会自动重试**。请先人工确认该消息是否已发出，
  再决定是否重新发送，避免重复消息。
- 发送失败后底层有短冷却与串行锁，连续发送会自动排队，属正常现象。

## `target_ambiguous`

- 目标名称匹配到多个会话。请使用更完整的名称重新调用。
- 若名称包含前导/尾随空格，可用 `search_contact` 获取准确名称。

## `path_not_allowed` / 发送文件被拒绝

- 默认拒绝发送任何文件是**有意为之**。配置允许目录后再试：

  ```
  WECHAT_SEND_DIRS=F:\Code\shared,F:\tmp\send
  ```

- 文件必须位于允许目录内；使用 `..` 尝试越界会被拒绝。
- 确认文件存在、是普通文件且可读，否则返回 `file_invalid`。

## 链接解析失败 / 无法解析链接

先用诊断命令确认打包版链路本身是否可用（不启动微信）：

```powershell
.\wechat-mcp.exe --selfcheck                                  # 看 yt_dlp_version / ffmpeg 是否正常
.\wechat-mcp.exe --resolve <url> [--download] [--outdir <目录>]  # 直接跑一遍解析
```

- `--selfcheck` 里 `yt_dlp_version` 显示 `(不可用)`：内置 yt-dlp 导入失败，
  其下方会给出 `yt_dlp_error`（冻结包缺标准库子模块的典型症状）。
- 媒体站点（B 站 / YouTube 等）解析不出标题：确认网络可达，且该站需要登录时
  在 `cookies` 层面另行处理；普通网页会自动回退到抓取 `<title>`。
- **下载**音视频失败：DASH 分离流必须靠 ffmpeg 合流，`--selfcheck` 的 `ffmpeg`
  必须可用（打包版已内置 `imageio_ffmpeg` 自带的 ffmpeg）。
- 「下载并回发」默认关闭；开启后下载目录会被自动纳入发送白名单，否则回发会被
  `path_not_allowed` 拒绝。

## 日志与审计

- 运行日志输出到 **stderr**，不污染 stdio 协议消息。若在客户端看不到日志属正常。
- 审计默认写入 `<项目根>/logs/audit.jsonl`（JSONL，一行一条），可用
  `WECHAT_AUDIT_LOG` 覆盖，或 `WECHAT_AUDIT_ENABLED=0` 关闭。
- 审计只记录工具名、目标、时间、结果与错误码，默认不记录聊天正文。

## 并发与窗口争抢

- 发送操作（文本/文件）通过互斥锁串行化，多个 Agent 调用会排队而非同时操作窗口。
- 期间请不要手动操作微信窗口，以免与自动化抢焦点导致失败。