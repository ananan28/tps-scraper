# -*- coding: utf-8 -*-
"""打包 homes.com 经纪人抓取器 EXE（onedir，可拷 U 盘到别的电脑离线运行）。
nodriver 直接用目标机器已装的 Chrome/Edge，不需要捆绑浏览器。
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
NAME = "Homes经纪人抓取器"
DIST = BASE / "dist_homes" / NAME


def main():
    print("[1/2] PyInstaller 打包...", flush=True)
    # playwright 本身用不到，但姓名库判定走 engine_core，它在模块层 import playwright
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
        "--name", NAME,
        "--distpath", str(BASE / "dist_homes"),
        "--workpath", str(BASE / "build_homes"),
        "--specpath", str(BASE / "build_homes"),
        "--collect-all", "nodriver",
        "--collect-all", "playwright",
        "--collect-all", "playwright_stealth",
        "--collect-all", "certifi",
        "--collect-all", "gender_guesser",
        str(BASE / "app_homes.py")], check=True)

    print("[2/2] 复制姓名库 + 双击启动器...", flush=True)
    shutil.copy2(BASE / "user_names.json", DIST / "user_names.json")
    (DIST / "启动homes.bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart "" "%NAME%.exe"\r\n'.replace("%NAME%", NAME),
        encoding="gbk")
    print("完成！交付文件夹：", DIST, flush=True)


if __name__ == "__main__":
    main()
