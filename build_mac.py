# -*- coding: utf-8 -*-
"""在 macOS 上打包 TPS 抓取器为 .app（必须在 Mac 上运行；Windows 无法跨平台打包）。
用法：Mac 终端进入本目录后运行  python3 build_mac.py  （或双击 打包Mac.command）。
流程：装依赖 → 下载 Mac 版 Chromium → PyInstaller 出 .app → 把浏览器塞进包 →  ad-hoc 签名。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
NAME = "TPS抓取器"
DIST = BASE / "dist"


def run(cmd):
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True)


def main():
    if sys.platform != "darwin":
        print("此脚本只能在 macOS 上运行（Mac 的 .app 必须在 Mac 上打包）。")
        sys.exit(1)

    print("[1/5] 安装依赖...", flush=True)
    run([sys.executable, "-m", "pip", "install", "-r", str(BASE / "requirements.txt")])

    print("[2/5] 下载 Mac 版 Chromium...", flush=True)
    run([sys.executable, "-m", "playwright", "install", "chromium"])

    print("[3/5] PyInstaller 打包 .app...", flush=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
         "--name", NAME,
         "--collect-all", "playwright",
         "--collect-all", "playwright_stealth",
         "--collect-all", "certifi",
         str(BASE / "app_tps.py")])

    app = DIST / (NAME + ".app")
    if app.exists():
        exe_dir = app / "Contents" / "MacOS"
    else:
        exe_dir = DIST / NAME

    print("[4/5] 捆绑 Chromium 进包...", flush=True)
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    dst = exe_dir / "ms-playwright"
    dst.mkdir(parents=True, exist_ok=True)
    for d in sorted(cache.glob("chromium*")):
        if d.is_dir():
            target = dst / d.name
            print("  复制", d.name, flush=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(d, target)

    print("[5/5] ad-hoc 签名 + 清隔离标记...", flush=True)
    if app.exists():
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=False)
        subprocess.run(["xattr", "-cr", str(app)], check=False)

    print("完成！交付：", app if app.exists() else exe_dir, flush=True)
    print("双击 .app 即可运行；数据存于 ~/Library/Application Support/TPS抓取器。", flush=True)


if __name__ == "__main__":
    main()