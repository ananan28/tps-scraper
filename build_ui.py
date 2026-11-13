# -*- coding: utf-8 -*-
"""打包「1.8.2 引擎 + 升级界面」版：onedir + 捆绑 Playwright 驱动与 Chromium。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
NAME = "新2.0版本"
DIST = BASE / "dist_ui" / NAME


def main():
    print("[1/3] PyInstaller 打包（含 Playwright 驱动，请耐心等待）...", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
                    "--name", NAME,
                    "--distpath", str(BASE / "dist_ui"),
                    "--workpath", str(BASE / "build_ui"),
                    "--specpath", str(BASE / "build_ui"),
                    "--collect-all", "playwright",
                    "--collect-all", "playwright_stealth",
                    "--collect-all", "certifi",
                    "--collect-all", "gender_guesser",
                    str(BASE / "app_ui.py")], check=True)

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

    print("[3/3] 复制用户姓名库 + 生成双击启动器...", flush=True)
    shutil.copy2(BASE / "user_names.json", DIST / "user_names.json")
    launcher = DIST / f"启动{NAME}.bat"
    launcher.write_text('@echo off\r\ncd /d "%~dp0"\r\nstart "" "%s.exe"\r\n' % NAME, encoding="gbk")

    print("完成！交付文件夹：", DIST, flush=True)


if __name__ == "__main__":
    main()
