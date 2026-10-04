/**
 * 把仓库根 `dist/wechat-mcp`（PyInstaller onedir 产物）复制进本插件的 `bin/`，
 * 使运行时随 npm 包一起分发，最终用户无需安装 Python。
 *
 * 用法（仓库根）：
 *   1. .venv\Scripts\python.exe packaging\build.py     # 生成 dist/wechat-mcp
 *   2. node dsh-plugin/scripts/stage-runtime.mjs       # 复制进 bin/
 *
 * `prepack` 已绑定本脚本，因此 `npm pack` / `npm publish` 会自动内置运行时。
 */
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(pluginRoot, '..')
const source = join(repoRoot, 'dist', 'wechat-mcp')
const target = join(pluginRoot, 'bin', 'wechat-mcp')

if (!existsSync(join(source, 'wechat-mcp.exe'))) {
  console.error(`[stage-runtime] 未找到构建产物 ${join(source, 'wechat-mcp.exe')}`)
  console.error('[stage-runtime] 请先在仓库根执行：.venv\\Scripts\\python.exe packaging\\build.py')
  process.exit(1)
}

rmSync(target, { recursive: true, force: true })
mkdirSync(dirname(target), { recursive: true })
cpSync(source, target, { recursive: true })

if (!existsSync(join(target, 'wechat-mcp.exe'))) {
  console.error(`[stage-runtime] 复制后仍缺少 ${join(target, 'wechat-mcp.exe')}`)
  process.exit(1)
}
console.log(`[stage-runtime] 已内置运行时 -> ${target}`)