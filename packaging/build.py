"""构建可分发的独立 exe（Windows onedir）、zip 与 Windows 安装包。

用法（仓库根目录）：

    .venv\\Scripts\\python.exe packaging\\build.py                # 全部产物
    .venv\\Scripts\\python.exe packaging\\build.py --no-installer  # 跳过安装包

产物：

    dist\\wechat-mcp\\                     PyInstaller onedir 目录
    dist\\wechat-mcp-win32-x64.zip         供 GitHub Release 与 npm 插件分发的压缩包
    dist\\wechat-mcp-setup-x64.exe         Windows 安装包（需本机装有 Inno Setup 6）

前置：

* venv 中安装 PyInstaller：

      uv pip install pyinstaller --python .venv\\Scripts\\python.exe

* 生成安装包需要 Inno Setup 6（<https://jrsoftware.org/isdl.php>）的 ``ISCC.exe``。
  未安装时会**跳过**安装包并给出提示，不影响前两项产物。可用环境变量
  ``ISCC`` 指定编译器路径。

构建会把本仓库内的 ``packaging/wechat_bridge.py``（从上游 deepseekgirl 收录，
来源见文件头注释与 ``THIRD_PARTY_NOTICES.md``）作为数据文件内置进包。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent
ROOT = SPEC_DIR.parent
BRIDGE = SPEC_DIR / "wechat_bridge.py"
ISS = SPEC_DIR / "installer.iss"
ZIP_NAME = "wechat-mcp-win32-x64.zip"
SETUP_NAME = "wechat-mcp-setup-x64.exe"

# ISCC.exe 的常见位置（PATH 之外）。Inno Setup 允许装到任意目录，
# 所以这里只覆盖常见几种，其余情况请设环境变量 ISCC。
_ISCC_CANDIDATES = (
    r"F:\SoftWare\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe",
)


def check_bridge() -> None:
    """确认仓库内的 wechat_bridge.py 存在，供 spec 内置。"""
    if not BRIDGE.is_file():
        raise SystemExit(
            f"未找到 {BRIDGE}\n该文件应随仓库一同提供（见 THIRD_PARTY_NOTICES.md）。"
        )
    print(f"[build] 将内置 wechat_bridge.py <- {BRIDGE}")


def read_version() -> str:
    """从 pyproject.toml 读版本号，用于安装包显示。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


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


def find_iscc() -> Path | None:
    """定位 Inno Setup 的命令行编译器 ISCC.exe。"""
    override = os.environ.get("ISCC")
    if override and Path(override).is_file():
        return Path(override)
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for candidate in _ISCC_CANDIDATES:
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return path
    return None


def build_installer() -> None:
    """用 Inno Setup 编译 Windows 安装包。"""
    if not ISS.is_file():
        raise SystemExit(f"未找到安装脚本: {ISS}")
    iscc = find_iscc()
    if iscc is None:
        print(
            "[build] 未找到 ISCC.exe，跳过安装包。\n"
            "        安装 Inno Setup 6 后重跑，或用环境变量 ISCC 指定路径：\n"
            "        https://jrsoftware.org/isdl.php"
        )
        return
    version = read_version()
    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        str(ISS),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    setup = ROOT / "dist" / SETUP_NAME
    if not setup.is_file():
        raise SystemExit(f"安装包编译失败，未生成: {setup}")
    size_mb = setup.stat().st_size / 1024 / 1024
    print(f"[build] 已生成 {setup} ({size_mb:.1f} MB)")


def main() -> None:
    want_installer = "--no-installer" not in sys.argv
    check_bridge()
    out = run_pyinstaller()
    raw_size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[build] onedir 原始体积 {raw_size / 1024 / 1024:.1f} MB")
    zip_dir(out, ROOT / "dist" / ZIP_NAME)
    if want_installer:
        build_installer()


if __name__ == "__main__":
    main()
