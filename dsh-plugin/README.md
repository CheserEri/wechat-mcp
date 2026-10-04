# dsh-wechat-mcp

DeepSeek Harness（DSH）插件：把 [wechat-mcp](../) 的微信桌面自动化能力接入 DSH，
让 Agent 能查询微信状态、读取会话、发送消息与文件。

插件通过官方 MCP 客户端桥接（`@deepseek-ai/dsh-mcp-client`）以 stdio 方式启动
随包内置的独立运行时，微信工具以 `mcp__wechat__*` 暴露给模型。

## 安装

```sh
dsh plugin --profile web add dsh-wechat-mcp
```

安装后 bundle 会自动启用，无需额外配置。

## 前置条件

- Windows 10/11 x64，微信 PC 客户端**已登录且主窗口可见**（最小化 / 锁屏 / 被遮挡会导致操作失败）。
- DSH 自带 Node，无需安装 Python：运行时（PyInstaller 打包的独立 exe）已内置在 npm 包中。

## 运行时路径

插件按以下优先级定位 `wechat-mcp.exe`：

1. 环境变量 `WECHAT_MCP_EXE` 指向的可执行文件（最可靠，任何安装布局都适用）；
2. 包内路径 `bin/wechat-mcp/wechat-mcp.exe`（从 npm 安装时位于 profile 的 `node_modules` 下）。

若两者都不可用，MCP 桥接会报 `ENOENT`。此时可从
[GitHub Releases](https://github.com/CheserEri/wechat-mcp/releases) 下载
`wechat-mcp-win32-x64.zip` 解压，并设置：

```sh
set WECHAT_MCP_EXE=D:\path\to\wechat-mcp\wechat-mcp.exe
```

## 工具

| 工具（DSH 侧名称） | 说明 |
|---|---|
| `mcp__wechat__get_wechat_status` | 检查连接状态、窗口可见性与自动化后端 |
| `mcp__wechat__get_chat_list` | 返回监听期间可读取的会话列表 |
| `mcp__wechat__get_chat_history` | 读取指定会话缓冲区内最近的消息 |
| `mcp__wechat__send_message` | 向明确指定的联系人或群聊发送文本 |
| `mcp__wechat__search_contact` | 按名称搜索联系人/群聊，同名时返回候选不猜测 |
| `mcp__wechat__get_chat_info` | 返回会话名称、类型、消息数与是否在关注列表 |
| `mcp__wechat__send_file` | 发送本地文件（受目录白名单、确认策略约束） |
| `mcp__wechat__get_recent_messages` | 基于游标增量读取新消息 |
| `mcp__wechat__set_monitored_chats` | 白名单/黑名单过滤读取结果 |

参数、返回结构与错误码见 [工具接口文档](../docs/TOOL_REFERENCE.md)。

## 安全与限制

- 发送必须给出明确目标；同名或多个候选时拒绝发送并返回候选列表。
- 发送结果无法确认时返回 `failed`，**不自动重试**。
- `send_file` 默认拒绝所有路径，需通过环境变量 `WECHAT_SEND_DIRS` 显式放宽。
- 读取仅覆盖**服务运行期间监听收到的消息**，不是微信完整历史。
- 本插件通过 UI 自动化复用微信桌面客户端，可能触发风控，请遵守平台服务条款。

## 构建（插件作者）

```powershell
# 1. 生成独立运行时（仓库根）
.venv\Scripts\python.exe packaging\build.py

# 2. 复制运行时进插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 打包 / 发布（prepack 已绑定第 2 步）
npm pack
npm publish --access public
```