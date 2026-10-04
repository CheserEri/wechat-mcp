# 第三方声明 / Third-Party Notices

本仓库（wechat-mcp，MPL-2.0）包含或分发下列第三方组件。各组件版权归其各自
作者所有，并适用其各自的许可条款。

---

## 1. 内置源码：`packaging/wechat_bridge.py`（来源：上游项目 `deepseekgirl`）

| 项 | 说明 |
| --- | --- |
| 文件 | `packaging/wechat_bridge.py`（及构建产物中的同名副本） |
| 来源项目 | `deepseekgirl`（原项目仓库：<https://github.com/linxisama/deepseekgirl>） |
| 来源路径 | 本地目录 `F:\Code\deepseekgirl-main` 下的 `src/wechat_bridge.py` |
| 用途 | 复用其 `WeChatBridge`（基于 wxauto / UI Automation 的微信桌面桥接），供源码模式与 PyInstaller 冻结打包内置 |
| 修改情况 | 除文件顶部的来源声明注释块外，内容未做任何修改 |

> **授权状态：上游项目未声明任何开源许可证。**
> 上游项目根目录不存在 `LICENSE` 文件，亦未在其文档中声明授权条款。
> 本仓库以「如实标注来源」的方式收录该文件，**但标注来源并不等同于获得授权**。
> 如你计划对本仓库或其构建产物进行再分发，请自行确认上游项目的授权情况，
> 并自行承担相应风险。

---

## 2. 冻结运行时内置的第三方 Python 包

`packaging/` 构建出的独立运行时（`dist/wechat-mcp/`、`dsh-plugin/bin/wechat-mcp/`）
会打包若干第三方 Python 包，例如：

- `mcp`（本项目的 MCP SDK 依赖）
- `loguru`
- `pywin32`（`win32gui` / `win32api` / `win32con` / `win32process` 等）
- `psutil`
- `wechatauto`（分发名 `wechatauto_replica`）
- `wxauto4`
- `uiautomation`
- `comtypes`
- `Pillow`（`PIL`）

这些包各自的许可证文件随构建产物一同分发，位于运行时目录内的
`_internal/*.dist-info/licenses/`（例如
`_internal/wechatauto_replica-*.dist-info/licenses/LICENSE`、
`_internal/comtypes-*.dist-info/licenses/LICENSE.txt`）。
具体条款请以上述随包文件中各包自身声明为准。

---

## 3. 上游项目最初的依赖说明

上游 `deepseekgirl` 在其 `requirements.*.txt` 中引用了
`wxauto`（<https://github.com/cluic/wxauto>）等第三方项目。本仓库仅使用
其 `wechat_bridge.py` 源码，其余上游资源未收录。