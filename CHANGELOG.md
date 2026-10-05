# 变更记录

本项目遵循语义化版本号。日期格式为 `YYYY-MM-DD`。

## [0.8.2] - 2026-10-05

桌面端体验修补：不再附带黑色命令行窗口，窗口/任务栏图标改用鲸鱼标志。

### 修复

- 修复**桌面端附带黑色命令行窗口**的问题：打包出的 exe 是控制台程序（MCP
  stdio 模式要靠 stdout 通信），双击启动桌面端时系统会额外分配一个命令行窗口。
  现在启动时自动隐藏它（仅打包产物、且控制台只属于本进程时才隐藏，避免把用户
  在终端里运行的窗口一起藏掉），原本打在控制台的内容——loguru 日志、`print`、
  异常回溯——改为转投到界面「运行日志」页（`desktop.install_console_capture`）。
- 修复**窗口与任务栏图标仍是 Python 图标**的问题：pywebview 未收到 `icon` 时会
  从 `sys.executable` 提取图标，源码方式运行时那是 `python.exe`。现在桌面端启动
  显式传入项目图标（冻结包取 `_MEIPASS` 内那份，spec 已把 `.ico` 一并打入包）。
- 修复界面「运行日志」里 warning 级别没有配色的问题（只定义了 `.log-warn`，
  而引擎发的是 `warning`）。

### 变更

- 引擎日志读写加锁（`BotEngine._log_lock`）：日志现在还会来自 loguru sink、下载
  线程等任意线程，而 `get_logs` 会整体遍历日志缓冲，并发追加会抛
  「deque mutated during iteration」。
- 全套 250 项单元测试通过。

## [0.8.1] - 2026-10-05

打包与品牌化版本：新增 Windows 安装包，修复解压即黑屏，桌面端显示名统一为「灵语」。

### 新增

- **Windows 安装包**（`packaging/installer.iss` + `build.py`）：在 onedir 与 zip 之后自动用
  **Inno Setup 6** 编译出 `dist\wechat-mcp-setup-x64.exe`（约 60.8 MB，lzma2 固实压缩，
  比 zip 更小）。中文/英文向导、开始菜单项与可选桌面快捷方式、控制面板卸载项。
  按用户安装（`{localappdata}\Programs\wechat-mcp`，不弹 UAC，安装目录可写以便写 `logs\`）；
  未安装 Inno Setup 时自动跳过，可用环境变量 `ISCC` 指定编译器，或 `--no-installer` 显式跳过。

### 修复

- 修复**解压即黑屏**：从 zip 解压出的文件会被打上「Internet 区域」标记
  （`Zone.Identifier`，`ZoneId=3`），.NET 出于安全拒绝加载带该标记的程序集，导致
  pythonnet 初始化失败、pywebview 起不来。桌面端启动时自动清除内置程序集
  （`pythonnet/runtime/*.dll`、`webview/lib/**/*.dll`）上的该标记（`unblock.py`）；
  万一仍失败，会弹出可读错误框说明原因，而非静默黑屏。

### 变更

- 桌面端显示名统一为「**灵语**」（窗口标题、侧栏品牌、安装包与快捷方式）；技术标识
  （`wechat-mcp` 包名/exe 名、AppId、npm 包名、MCP 工具前缀）保持不变，以便后续适配
  QQ 等其他聊天软件。
- 应用图标改用项目所用的鲸鱼标志（由 `packaging/make_icon.py` 渲染，16~256 px 多尺寸 `.ico`），
  无需外部素材。
- 全套 250 项单元测试通过。

## [0.8.0] - 2026-10-05

阶段 10：链接解析（内置 yt-dlp）。

### 新增

- **内置 yt-dlp 源码**：完整收录上游 `yt_dlp` 包到 `vendor/yt_dlp/`
  （版本 `2026.08.19`，Unlicense 公有领域，见 `THIRD_PARTY_NOTICES.md`）。
  打包时交给 PyInstaller 静态分析（`pathex` + `hiddenimports`）并启用其自带的
  官方 hook，源码模式与冻结模式均可导入。
- **链接解析**（桌面端新增「链接解析」页面，默认**开启**）：
  - 上下文里出现的链接会被自动解析，结果以 `[链接] …` 附在该条消息之后，
    让机器人能就链接内容回应（标题 / 作者 / 时长 / 来源 / 简介）。
  - 媒体站点走 yt-dlp；普通网页回退到抓取 `<title>` 与 `meta description`。
  - 结果按 URL 缓存（成功永久、失败 10 分钟内不重试），重复出现不再请求。
  - `link_parse_max`（默认 3）：单次请求最多新解析几个链接；已缓存的免费命中。
- **下载并回发**（`link_download_enabled`，默认**关闭**）：开启后把触发消息里
  链接的音视频下载到本地，并作为文件消息发回当前聊天。受 `link_download_max`
  （单次最多几个）与 `link_download_max_mb`（单文件体积上限）约束。
- **链接固定提示**（`link_ack_enabled` / `link_ack_text`，默认开启、文案「正在解析链接」）：
  消息里出现链接时先回一句**固定**文案，让用户知道正在处理。文案由配置写死，
  **不经过大模型**——为一句「正在解析链接」调一次模型既慢又费 token。
- **群聊里的链接也会被处理**（修复）：此前链接解析挂在「这条消息要不要回」之后，
  而群里不 @ 机器人时 `_should_reply` 直接返回 False，整条 `_handle` 提前退出，
  于是群里的链接既不解析也不下载。现在链接服务（`_handle_links`）**独立于触发判定**，
  放在 `_should_reply` 之前：私聊与群聊一视同仁，群里**不必 @ 机器人**。
  仍受「群聊作用范围」「静默时段」约束；同一条消息只处理一次（按消息 ID 去重）。
- `link_llm_followup`（默认开启）：发完固定提示后是否仍让大模型就这条消息接话。
  默认保持原有行为不变；设为关闭则「带链接的消息只回固定提示 + 回发解析结果」。
- 链接内容的「下载并回发」改由 `_handle_links` **统一调度**，不再在模型回复成功后
  二次调度——否则下载很快完成时同一条链接可能被下载两遍、发两次文件。
- **链接解析诊断命令** `wechat-mcp --resolve <url> [--download] [--outdir <目录>]`：
  不依赖微信环境，直接跑一遍解析（可选下载）并打印结果，便于验证打包后链路可用。
- **推文卡片诊断命令** `wechat-mcp --tweet <推文链接> [--outdir <目录>]`：
  抓取一条推文并渲染成卡片图，打印作者/时间/是否含视频/点赞/正文与产物路径。
- **短链展开**：yt-dlp **完全不认识** `b23.tv` / `v.douyin.com` / `xhslink.com`
  / `t.cn` 等短链（会落到 Generic 提取器，只把短码当标题，例如
  `https://b23.tv/P7kJlgt` → 标题 "P7kJlgt"），既取不到元数据也无法下载。
  现在解析与下载前都会先跟随重定向（或从 JS/meta 跳转页正文里捞）拿到真实地址，
  例如 `https://b23.tv/hOgf9CN` → `https://www.bilibili.com/video/BV19pHi6UEmz`
  （BiliBili 提取器，标题/作者/时长齐全）。失效短链会给出明确错误「短链已失效」，
  不再返回无意义的短码标题。注入上下文的地址会裁掉 `share_*` / `buvid` 等跟踪参数。
- **X（Twitter）推文卡片**（桌面端「链接解析」新增「X（Twitter）推文卡片」开关，
  `link_tweet_card_enabled`，默认**开启**）：上下文里出现 `x.com` / `twitter.com`
  的推文链接时，不再走 yt-dlp（它只能拿到视频、拿不到正文），而是直连 X 的
  syndication 接口取回结构化数据，并**本地用 PIL 渲染成推文卡片图片**发回聊天：
  - 数据源 `cdn.syndication.twimg.com/tweet-result`，token 按 react-tweet 的算法
    自算（`((id / 1e15) * PI).toString(36)` 去掉 `0` 与 `.`），无需登录、无需 API Key。
  - 卡片含头像（圆形裁剪）、昵称、`@handle`、认证蓝标、正文（中英混排自动折行）、
    时间、点赞数，以及正文配图（最多 4 张，圆角裁切，超出限高自动缩放）。
    视频/动图推文用 syndication 给的**封面帧**当缩略图，并在正中叠一个播放角标。
  - 字体按**逐字符**选：中文/西文用微软雅黑、emoji 用 Segoe UI Emoji，
    避免「中文变豆腐块」或「emoji 变豆腐块」。
  - 解析结果与链接解析共用同一份缓存，注入上下文的摘要前缀为 `[推文]`（普通链接仍为
    `[链接]`），形如 `[推文] <url>｜作者：名字（@handle）；来源：X；2014-09-03 23:18
    · 含 1 张图 · 458 喜欢；简介：<正文>`。
  - 若该推文**含视频**，会在卡片之后再按链接下载流程把视频发回；卡片渲染失败时
    自动退回原下载路径，不会因为渲染异常而丢内容。

### 修复

- 修复微信**分享链接卡片**丢失 URL 的问题：`normalize_message_content` 原先只
  保留 `<title>`、丢掉 `<url>`，导致最常见的「分享一个链接」场景下游根本拿不到
  链接、无法解析。现保留标题的同时附上 URL。
- 修复冻结包中内置 yt-dlp **无法导入**的问题：早期把 `vendor/yt_dlp` 仅当作
  数据目录分发、不参与 PyInstaller 静态分析，其内部 import 的标准库子模块
  （实测先缺 `optparse`，补齐后又缺 `html.parser`）不会被自动收集。现改为让
  PyInstaller 真正分析 `yt_dlp`，标准库子模块与 940 个提取器全部自动收集，
  无需再手工枚举标准库。
- 修复打包版**发送消息首次必然失败**的问题：spec 的 `excludes` 里排除了
  `winsdk`，但 wechatauto 的 UIA 发送路径依赖它，运行时报
  `发送消息失败: No module named 'winsdk'`，随后才退化成 OCR 兼容路径重试。
  现不再排除并显式收集 `winsdk`（约 43 MB）。
- 修复**下载回发会卡住机器人**的问题：`_consume` 串行消费消息队列，而 `_handle`
  会 `await` 下载——一个 21 分钟的视频能把后续消息全积压几分钟。现改为后台任务
  （`_schedule_download`），并按 URL 去重，同一链接不会并发重复下载。

### 变更

- 新增 `src/wechat_mcp/bot/links.py`：URL 提取（RFC 3986 字符集，不吞中文、
  保留成对括号）、`LinkResolver`（线程池执行 yt-dlp，超时/缓存/下载）。
- `BotConfig` 新增 `link_parse_enabled` / `link_parse_max` / `link_parse_timeout`
  / `link_download_enabled` / `link_download_max` / `link_download_dir` /
  `link_download_max_mb` / `link_tweet_card_enabled` 字段，`from_dict` 做范围钳制，
  并新增 `download_dir()`（默认 `%APPDATA%\wechat-mcp\downloads`）。
- 新增 `src/wechat_mcp/bot/tweet.py`：推文 URL 识别、syndication token、
  抓取与解析（`Tweet` / `fetch_tweet` / `fetch_tweet_cached`）、PIL 卡片渲染
  （`render_card`：圆形头像、认证徽标、中英 emoji 混排折行、配图网格）。
- `BotEngine`：构建上下文前预解析链接（`_prefetch_links`），渲染时注入
  `[链接] …`（`_link_block`）；触发回复成功后按需下载回发
  （`_download_and_send`）。下载目录会被自动纳入适配层的发送白名单
  （`_ensure_download_dir_allowed`）——否则「未配置允许目录即拒发」会让回发必然失败。
- 新增 `src/wechat_mcp/bot/tweet.py`：推文 URL 识别、syndication 抓取与解析、
  内存缓存、以及 `render_card`（PIL 本地渲染推文卡片）。
- `BotConfig` 新增 `link_tweet_card_enabled` 字段（默认 `True`）。
- `BotEngine` 的回发改由分发器 `_download_and_send` 处理：推文走 `_send_tweet`
  （先发卡片，有视频再发视频），其余链接仍走 `_download_and_send_one`。
- `LinkResolver` 在解析与下载前会先识别推文链接并改走推文通道
  （`_tweet_link_info`），结果仍挂回**原始 URL** 上缓存，真实地址留在 `webpage_url`。

### 说明

- 默认配置下：解析开启（只在消息含链接时才触发，无链接零开销），下载关闭。
- 下载默认用「最佳单文件」格式；系统 PATH 上有 `ffmpeg` 时才请求音视频合流
  （`bv*+ba/b`），否则退化为单文件，避免因缺 ffmpeg 直接失败。打包版内置
  `imageio_ffmpeg` 自带的 ffmpeg（约 84 MB）与 `winsdk`（约 43 MB），故冻结包
  体积明显增大（onedir 约 205 MB）。
- 全套 238 项单元测试通过（新增链接抽取、短链展开、推文识别/解析/token 校验/
  卡片渲染、引擎「先卡片后视频」、群聊未 @ 也回固定提示/下载、提示文案与开关、
  静默时段与群范围跳过、同消息去重、配置钳制、引擎注入/下载回发、下载路径抓取、
  链接卡片归一化、`--resolve` / `--tweet` 诊断等用例）。
- 冻结包已实测：`--selfcheck` 报 `yt_dlp_version: 2026.08.19`；
  `--resolve` 可解析网页（`example.com`）、通用媒体（`Generic`，含下载）、
  站点提取器（B 站 `BiliBili`）与短链（`b23.tv` 有效/失效两种情形）；
  `--resolve` 对推文链接给出 `[推文]` 摘要，`--tweet` 可直接产出卡片 PNG。

## [0.7.0] - 2026-10-04

阶段 9：图像识别（多模态）。

### 新增

- 图像识别管道（桌面端「模型设置」新增「图像识别」卡片）：
  - 监听收到的**图片消息**时，从微信本地数据库取出图片，解密微信私有
    `.dat` 格式（v1/v2/WXAM）后落到临时目录，读取为 base64 data URI，
    作为多模态内容随上下文一起送入模型。
  - 新增开关 `vision_enabled`（默认**关闭**）：开启后模型上下文中的图片会以
    `image_url` 形式附带；关闭时行为与旧版一致（图片仅以 `[图片]` 占位文本呈现）。
  - `vision_max_images`（默认 3）：单次请求最多附带的历史图片数量，超出部分
    只保留文本，避免请求体过大。
- 上下文中的历史图片同样参与识别：只要在附带范围内（`vision_max_images`），
  无论是否为本轮触发消息都会被带上。

### 修复

- 修复图片消息原始 XML 漏进上下文的 bug：微信图片消息正文带
  `<?xml version="1.0"?>` 声明，而判定逻辑只匹配 `<msg` 开头，导致整段原始
  XML 被当作消息文本送进模型。现先剥离 XML 声明再判定，图片消息正确归一化为
  `[图片]`。
- 修复媒体提取（图片 / 语音）在**全局监听**下静默失败的 bug：桥接层用的是
  `AddListenAll`，其会话占位 `_AllMessageChat` 只有 `.who` / `._wxid`、
  **没有 `._db`**，原实现因此取不到数据库句柄而直接放弃（日志表现为
  「图片消息缺少媒体上下文」）。现抽出 `_resolve_media_context` 统一解析，
  并回退到桥接层自身的 `self._wx._db`；`user` 明确取会话 wxid。语音提取
  同源 bug 一并修复，其保存目录也改到系统临时目录（原为包内路径，冻结后不可靠）。

### 变更

- `BotConfig` 新增 `vision_enabled` / `vision_max_images` 字段，`from_dict`
  对图片数量取正并设上限。
- `MessageRecord` 新增 `image_path` 字段；适配层透传；`BotEngine` 渲染上下文时
  按槽位决定是否附加图片（`_render_context` / `_image_slots` / `_image_data_uri`）。
- 打包内置桥接层 `packaging/wechat_bridge.py` 新增 `_extract_image_file`，
  镜像语音提取逻辑，把解密后的图片写入临时目录并回填 `WeChatMessage.image_path`。

### 说明

- **需要模型支持视觉**：请把模型端点配置为支持图像的多模态模型
  （如 `qwen-vl-max`、`glm-4v`、`gpt-4o` 等）；纯文本模型开启 `vision_enabled`
  会被忽略或报错。
- 默认关闭 `vision_enabled`，因此升级后行为不变；图片解密依赖
  `cryptography`，打包已内置。
- 新增图像管道、XML 声明归一化、媒体上下文解析等单元测试，全套 159 项通过
  （`test_end_to_end` 补齐 `reply_probability=1.0`，消除默认 0.85 概率导致的
  偶发失败）。

## [0.6.0] - 2026-10-04

阶段 8：长时间静默 + 偶发参与（水群）。

### 新增

- 新增「静默与水群」设置（桌面端独立页面）：
  - **静默时段** `quiet_hours_*`：指定区间内不回复任何人（含 @我），支持跨零点
    （如 23:00-08:00）；起止相同或格式非法则不生效。
  - **群级节流** `group_rate_*`：每个群在窗口内最多回复 N 条，超出即静默跳过
    （默认 30 分钟内 5 条）；仅作用于群聊，私聊不受影响。
  - **偶发参与（主动水群）** `proactive_*`：引擎周期性检查群聊，按概率主动接一句。
    默认**关闭**（会向真实群发消息）；开启后仅当群里最近有人说话、模型也认为
    有话可说时才发言，否则模型回复 `[SILENT]` 保持安静；同一群两次主动发言有
    最小间隔（默认 30 分钟）。

### 变更

- `BotConfig` 新增 `quiet_hours_enabled/start/end`、`group_rate_enabled/window_minutes/
  max_replies`、`proactive_enabled/interval_min/max/probability/recent_seconds/
  per_group_cooldown` 字段，`from_dict` 做类型与范围纠正（概率钳制 `[0,1]`、
  区间下限不低于下限、间隔下限 ≥1 秒等）。
- `BotEngine`：新增静默/节流判定与主动参与循环（`_proactive_loop` / `_proactive_tick`
  / `_speak_proactively`）；发送逻辑抽出为 `_send_reply` 供触发回复与主动发言复用；
  `_record_inbound` 额外记录最近入站消息以挑选主动参与目标。
- 主动参与循环常驻并每轮读取配置，支持界面热开热关。

### 说明

- 默认配置下行为不变：静默时段关闭、主动参与关闭，仅群级节流（30 分钟 5 条）
  作为安全网默认开启。
- 145 项单元测试全部通过（新增静默时段、群级节流、主动参与含 `[SILENT]` 哨兵等用例）。

## [0.5.0] - 2026-10-04

阶段 7：拟人化自动回复。

### 新增

- 新增「拟人化」设置（桌面端新增独立页面，默认**开启**）：
  - `reply_probability`（默认 0.85）：触发后按概率决定是否回复，模拟真人并非
    每条都回；未回复时同样记录上下文与冷却，避免反复触发。
  - `split_replies`（默认开）：较长回复按句末标点拆成多条短消息发出，
    条间加入随机间隔（`split_delay_min` / `split_delay_max`），像真人分句连发；
    受 `split_max_parts`（默认 3）与 `split_min_length`（默认 40 字）约束。
  - `humanize` 总开关：关闭后恢复「收到即整段回复」的旧行为。
- 内置行为约束新增口语化要求：多用短句、可用语气词、一般两三句以内、
  不端着一本正经长篇大论（仍保留「纯文本、禁 Markdown」与「只回应待回复消息」）。

### 变更

- `BotConfig` 新增 `humanize` / `reply_probability` / `split_replies` /
  `split_max_parts` / `split_min_length` / `split_delay_min` / `split_delay_max`
  字段，`from_dict` 对概率做 `[0,1]` 钳制、条数取正、间隔上限不低于下限。
- 桌面端 range 滑块的数值标签改为按 `data-val` 定位，支持多个滑块
  （原实现写死 `#temp-val`）。

### 说明

- 分条发送只切分不丢内容（拼接各条可还原原文）；无句末标点或长度不足时不拆分。
- 拟人化默认开启，会影响回复条数与频率；如需旧的「必回且整段」行为，
  在「拟人化」页关闭总开关即可。126 项单元测试全部通过。

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