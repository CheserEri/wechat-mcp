/**
 * dsh-wechat-mcp —— bundle 的自描述插件。
 *
 * 微信能力由本包 `cordis.patch.yml` 插入的 `@deepseek-ai/dsh-mcp-client` 行提供。
 * 这个插件只做一件事：加载时校验随包内置的运行时是否存在，把问题以清晰日志暴露出来，
 * 避免用户只看到 MCP 桥接的 ENOENT 而无从下手。
 */
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-wechat-mcp'

const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BUNDLED_EXE = join(PACKAGE_ROOT, 'bin', 'wechat-mcp', 'wechat-mcp.exe')

export function apply(ctx) {
  if (process.env.WECHAT_MCP_EXE) return
  if (existsSync(BUNDLED_EXE)) return
  ctx.logger?.warn(
    `[dsh-wechat-mcp] 未找到内置运行时 ${BUNDLED_EXE}；` +
      '请设置 WECHAT_MCP_EXE 指向 wechat-mcp.exe，或从 GitHub Release 下载 wechat-mcp-win32-x64.zip 解压后设置该变量。',
  )
}