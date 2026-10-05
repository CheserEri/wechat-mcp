# WeChat MCP Server

把 Windows 微信桌面客户端的自动化能力封装为 **MCP Server**，供通用 AI Agent
（Codex、Claude Code 等支持 MCP 的客户端）通过标准工具调用。

- Agent 负责理解任务、规划步骤、选择工具。
- 本服务只提供稳定、可控、可测试的微信操作工具。
- 不内置大模型、不保留原项目的固定人设与自动回复循环。

> 前置条件：微信 PC 客户端已登录，且主窗口可见（最小化 / 锁屏 / 被遮挡会导致操作失败）。

## 功能

**P0（MVP）**

| 工具 | 说明 |
|---|---|
| `get_wechat_status` | 检查连接状态、窗口可见性与自动化后端 |
| `get_chat_list` | 返回监听期间可读取的会话列表 |
| `get_chat_history` | 读取指定会话缓冲区内最近的消息 |
| `send_message` | 向明确指定的联系人或群聊发送文本 |

**P1（扩展）**

| 工具 | 说明 |
|---|---|
| `search_contact` | 按名称搜索联系人/群聊，同名时返回候选不猜测 |
| `get_chat_info` | 返回会话名称、类型、消息数与是否在关注列表 |
| `send_file` | 发送本地文件（受目录白名单、确认策略约束） |
| `get_recent_messages` | 基于游标增量读取新消息 |
| `set_monitored_chats` | 白名单/黑名单过滤读取结果（不改动底层监听） |

详细的参数、返回结构与错误码见 [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)。

## 安装

### 方式一：预编译独立包（推荐，无需 Python）

从 [GitHub Releases](https://github.com/CheserEri/wechat-mcp/releases) 下载
`wechat-mcp-win32-x64.zip`，解压后直接把 `wechat-mcp.exe` 配到 MCP 客户端即可：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "D:\\path\\to\\wechat-mcp\\wechat-mcp.exe",
      "args": []
    }
  }
}
```

该包已内置微信桥接模块，**不依赖 `DEEPSEEKGIRL_PATH`**。

**桌面版（实时自动回复助手）**：同一个 exe 加 `--gui` 参数启动原生窗口，
无需 Agent 轮询：消息到达即处理，群里有人 @我 / 引用回复我（私聊每条）时，
取最近 N 条上下文 + 人设发给大模型并自动回复；人设、模型、群范围均可在界面修改。
拟人化（回复概率、长回复分条发送、口语化措辞）默认开启，可在界面「拟人化」页调整或关闭。
「静默与水群」页可设置静默时段、群级回复节流，以及可选的「偶发主动参与」（默认关闭）。
「链接解析」页开启后，消息里的链接会被自动解析（内置 yt-dlp，取标题/作者/时长/简介）
并附到上下文，让机器人能就链接内容回应；还可选把音视频下下来发回当前聊天（默认关闭）。
**私聊和群聊一视同仁**：群里**不必 @ 机器人**，只要消息里有链接就会先回一句固定提示
（默认「正在解析链接」，**不经过大模型**，文案可在界面改），再把解析结果发回。
其中的 **X（Twitter）推文卡片**（默认开启）会把推文**本地渲染成一张卡片图**发回聊天
（头像 / 昵称 / 认证 / 正文 / 配图 / 时间 / 点赞，无需登录），推文含视频时再下载视频一并回发。
「模型设置」页的「图像识别」开启后（需模型支持视觉），图片会随上下文一起送给模型。

```powershell
.\wechat-mcp.exe --gui
```

**自检与链接解析诊断**：不确定打包版是否正常时，可跑这几条命令（都不启动微信）：

```powershell
.\wechat-mcp.exe --selfcheck                                  # 版本 / 内置 yt-dlp / ffmpeg / webui
.\wechat-mcp.exe --resolve <url> [--download] [--outdir <目录>]  # 直接解析（可选下载）一个链接
.\wechat-mcp.exe --tweet <推文链接> [--outdir <目录>]            # 抓取推文并渲染出卡片图
```

`--selfcheck` 里 `yt_dlp_version` 与 `ffmpeg` 应显示具体版本/路径；`--resolve` 会打印
标题、作者、时长、来源、简介（以及 `--download` 时的本地文件路径）；`--tweet` 会打印
作者、时间、是否含视频、点赞数与生成的卡片 PNG 路径。

### 方式二：DeepSeek Harness 插件（输入名称即安装）

```sh
dsh plugin --profile web add dsh-wechat-mcp
```

插件包见 [dsh-plugin/](dsh-plugin/)，运行时随 npm 包分发，微信工具以
`mcp__wechat__*` 暴露给模型。详见 [dsh-plugin/README.md](dsh-plugin/README.md)。

### 方式三：源码运行（开发用）

环境要求：Windows 10/11 x64、Python 3.10+。

```powershell
cd F:\Code\wechat-mcp
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[wechat]"
```

`[wechat]` 会安装微信自动化所需的运行时依赖（`wechatauto-replica`、`wxauto4`、
`uiautomation`、`loguru`）。源码模式下适配层从 `DEEPSEEKGIRL_PATH` 指定的项目复用
`WeChatBridge`；冻结打包时会自动内置该模块。

桌面窗口（界面 API Key 在界面中填写，无需环境变量）：

```powershell
.venv\Scripts\python.exe -m wechat_mcp.desktop
```

## 配置

复制 [.env.example](.env.example) 为 `.env`，或在 MCP 客户端配置的 `env` 中设置。
常用变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEKGIRL_PATH` | `F:\Code\deepseekgirl-main` | 复用 WeChatBridge 的原项目路径 |
| `WECHAT_BACKEND` | `auto` | `auto` / `wechatauto` / `wxauto4` / `wxauto` |
| `WECHAT_SEND_TIMEOUT` | `20` | 单次发送最长等待秒数 |
| `WECHAT_SEND_DIRS` | 空 | 允许发送文件的目录；**空 = 拒绝发送文件** |
| `WECHAT_REQUIRE_CONFIRM` | `0` | 发送前是否需 `confirm=true` |
| `WECHAT_AUDIT_ENABLED` | `1` | 是否记录操作审计 |

完整列表见 [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md#环境变量)。

## 在 Agent 中接入

在 MCP 客户端配置中加入本地 stdio 服务，示例见
[mcp.config.example.json](mcp.config.example.json)：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "F:\\Code\\wechat-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "wechat_mcp.server"],
      "env": { "WECHAT_SEND_DIRS": "F:\\Code\\shared" }
    }
  }
}
```

## 安全设计

- **不隐式指定目标**：发送必须给出明确对象；同名或多个候选时拒绝发送并返回候选列表。
- **不虚报成功**：发送结果无法确认时返回 `failed`，**不自动重试**，避免重复消息。
- **文件白名单**：`send_file` 默认拒绝所有路径，需通过 `WECHAT_SEND_DIRS` 显式放宽；
  含 `..` 路径穿越会被拒绝。
- **可配置确认**：`WECHAT_REQUIRE_CONFIRM=1` 时，未带 `confirm=true` 的发送只返回预览。
- **操作审计**：记录工具名、目标、时间与结果，默认不记录聊天正文
  （默认写入 `logs/audit.jsonl`）。
- **关注列表**：可只关注/忽略指定会话，仅过滤本服务读取结果，不改变底层监听。

> 隐私提示：聊天内容可能包含敏感信息。若 Agent 使用在线模型，被读取的内容可能
> 发送给相应服务商，请自行评估并选择模型服务。

## 已知限制

- 依赖 Windows 微信桌面客户端，需已登录且窗口可见。
- 底层库不提供「按需拉取完整会话列表 / 历史消息」接口；`get_chat_list` 与
  `get_chat_history` 返回的是**适配层运行期间被动接收到的消息**，不是微信完整历史。
- `search_contact` 的本地联系人库查询依赖 `wechatauto` 后端；其他后端退回为
  「观察到的会话名称」。
- 自动化可能触发微信风控或受版本变化影响，不保证账号不受限制。

## 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

真实环境端到端与联调脚本位于 [scripts/](scripts/)：

```powershell
# 阶段3：以 stdio 协议驱动 Server，覆盖计划书测试场景
.venv\Scripts\python.exe scripts\phase3_e2e.py --wait-inbound 60

# 阶段4：P1 能力真实微信联调
.venv\Scripts\python.exe scripts\phase4_live_check.py
```

## 构建与发布

```powershell
# 1. 生成独立运行时（onedir + zip），产物在 dist\
.venv\Scripts\python.exe packaging\build.py

# 2. 把运行时内置进 DSH 插件包
node dsh-plugin\scripts\stage-runtime.mjs

# 3. 发布 GitHub Release（需已登录 gh）
gh release create v0.3.0 dist\wechat-mcp-win32-x64.zip --title "v0.3.0" --notes "…"

# 4. 发布 npm 插件（prepack 会自动执行第 2 步）
cd dsh-plugin
npm publish --access public
```

> 打包会把**仓库内**的 `packaging/wechat_bridge.py`（从上游
> `deepseekgirl` 收录，来源与授权说明见
> [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）内置进 exe，
> 因此构建不再依赖外部 `DEEPSEEKGIRL_PATH`，仓库 CI 可自动构建。
>
> 发布 npm 后，在 GitHub 仓库的 About → Topics 中添加 `dsh-plugin`，
> 插件即会被 DSH 社区索引收录。

## 文档

- [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md) — 工具接口与环境变量
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 故障排查
- [docs/E2E_TEST_RECORD.md](docs/E2E_TEST_RECORD.md) — 端到端测试记录
- [dsh-plugin/README.md](dsh-plugin/README.md) — DeepSeek Harness 插件安装与说明
- [CHANGELOG.md](CHANGELOG.md) — 版本与变更记录

## 第三方自动化风险

本项目通过 UI 自动化复用微信桌面客户端，属于第三方自动化手段。使用时请遵守
微信及所在平台的服务条款；自动化操作可能导致账号被限制。建议使用独立的测试账号
或在可控测试会话中验证，避免对真实联系人开展未经确认的自动发送。