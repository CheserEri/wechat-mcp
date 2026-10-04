"""构建可分发的独立 exe（Windows onedir）并打包为 zip。

用法（仓库根目录）：

    .venv\\Scripts\\python.exe packaging\\build.py

产物：

    dist\\wechat-mcp\\                 PyInstaller onedir 目录
    dist\\wechat-mcp-win32-x64.zip     供 GitHub Release 与 npm 插件分发的压缩包

前置：需先在 venv 中安装 PyInstaller，例如：

    uv pip install pyinstaller --python .venv\\Scripts\\python.exe

构建会把本仓库内的 ``packaging/wechat_bridge.py``（从上游 deepseekgirl 收录，
来源见文件头注释与 ``THIRD_PARTY_NOTICES.md``）作为数据文件内置进包。
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent
ROOT = SPEC_DIR.parent
BRIDGE = SPEC_DIR / "wechat_bridge.py"
ZIP_NAME = "wechat-mcp-win32-x64.zip"


def check_bridge() -> None:
    """确认仓库内的 wechat_bridge.py 存在，供 spec 内置。"""
    if not BRIDGE.is_file():
        raise SystemExit(
            f"未找到 {BRIDGE}\n该文件应随仓库一同提供（见 THIRD_PARTY_NOTICES.md）。"
        )
    print(f"[build] 将内置 wechat_bridge.py <- {BRIDGE}")


def run_pyinstaller() -> Path:
    dist = ROOT / "dist"
    work = ROOT / "build"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_DIR / "wechat-mcp.spec"),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    out = dist / "wechat-mcp"
    if not out.is_dir():
        raise SystemExit(f"打包失败，未生成目录: {out}")
    return out


def zip_dir(src: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src.parent))
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"[build] 已生成 {target} ({size_mb:.1f} MB)")


def main() -> None:
    check_bridge()
    out = run_pyinstaller()
    raw_size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[build] onedir 原始体积 {raw_size / 1024 / 1024:.1f} MB")
    zip_dir(out, ROOT / "dist" / ZIP_NAME)


if __name__ == "__main__":
    main()