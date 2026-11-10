# -*- coding: utf-8 -*-
"""打包 realtor.com 经纪人抓取器 EXE（onedir，可拷 U 盘到别的电脑离线运行）。

浏览器走两条路：目标机器装了 Chrome/Edge 就用它（指纹更像真人），
没装就用打包进去的 browser\\chrome-win64（见 bundle_browser.py），
所以一台没装过任何浏览器的电脑也能直接跑。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import bundle_browser

BASE = Path(__file__).resolve().parent
NAME = "Realtor经纪人抓取器"
DIST = BASE / "dist_realtor" / NAME


def main():
    print("[1/2] PyInstaller 打包...", flush=True)
    # playwright 本身用不到，但姓名库判定走 engine_core，它在模块层 import playwright
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
        "--name", NAME,
        "--distpath", str(BASE / "dist_realtor"),
        "--workpath", str(BASE / "build_realtor"),
        "--specpath", str(BASE / "build_realtor"),
        "--collect-all", "nodriver",
        "--collect-all", "playwright",
        "--collect-all", "playwright_stealth",
        "--collect-all", "certifi",
        "--collect-all", "gender_guesser",
        str(BASE / "app_realtor.py")], check=True)

    print("[2/2] 复制姓名库 + 双击启动器...", flush=True)
    shutil.copy2(BASE / "user_names.json", DIST / "user_names.json")
    how, size = bundle_browser.bundle(DIST)
    if how is None:
        print("      警告：本机没有 ms-playwright 的 Chromium，这份 EXE 到了"
              "没装 Chrome/Edge 的电脑还是起不了浏览器。"
              "先跑 python -m playwright install chromium 再打包。", flush=True)
    else:
        print(f"      自带 Chromium：{how}，{size / 1e6:.0f} MB -> {DIST / 'browser'}",
              flush=True)
    (DIST / "启动realtor.bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart "" "%NAME%.exe"\r\n'.replace("%NAME%", NAME),
        encoding="gbk")
    # 换电脑起不了浏览器时，双击这个就能在同目录出 browser_check.txt
    (DIST / "浏览器自检.bat").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart /wait "" "%NAME%.exe" --check-browser\r\n'
        'notepad browser_check.txt\r\n'.replace("%NAME%", NAME), encoding="gbk")
    print("完成！交付文件夹：", DIST, flush=True)


if __name__ == "__main__":
    main()
