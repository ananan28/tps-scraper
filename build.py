# -*- coding: utf-8 -*-
"""打包 TPS 抓取器：onedir + 捆绑 Playwright 驱动与 Chromium 浏览器（目标电脑离线可用）。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
NAME = "TPS抓取器"
DIST = BASE / "dist" / NAME


def main():
    print("[1/3] PyInstaller 打包（含 Playwright 驱动，体积大请耐心等待）...", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
                    "--name", NAME,
                    "--collect-all", "playwright",
                    "--collect-all", "playwright_stealth",
                    "--collect-all", "certifi",
                    str(BASE / "app_tps.py")], check=True)

    print("[2/3] 捆绑 Chromium 浏览器...", flush=True)
    src = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    dst = DIST / "ms-playwright"
    dst.mkdir(parents=True, exist_ok=True)
    for d in sorted(src.glob("chromium*")):
        if d.is_dir():
            target = dst / d.name
            print("  复制", d.name, flush=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(d, target)

    print("[3/3] 生成双击启动器...", flush=True)
    launcher = DIST / "启动TPS.bat"
    launcher.write_text('@echo off\r\ncd /d "%~dp0"\r\nstart "" "TPS抓取器.exe"\r\n', encoding="gbk")

    print("完成！交付文件夹：", DIST, flush=True)
    print("双击其中的 启动TPS.bat 或 TPS抓取器.exe 即可运行。", flush=True)


if __name__ == "__main__":
    main()