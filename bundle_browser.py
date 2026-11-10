# -*- coding: utf-8 -*-
"""把一份 Chromium 塞进交付文件夹，让 EXE 在没装浏览器的电脑上也能跑。

本机 python -m playwright install chromium 下好的那版（Chrome 151 内核）就是
完整的 chrome.exe，nodriver 直接把它当 browser_executable_path 用，实测能开页。
打包脚本调 bundle()，目标是 <交付夹>\\browser\\chrome-win64\\chrome.exe；
运行时由 browser_path.bundled_browser() 找到它。

优先硬链接（同盘不占第二份空间，U 盘拷贝时会展开成真文件），
跨盘或文件系统不支持时退化成整份复制。
"""
import os
import shutil
import sys
from pathlib import Path


def source_dir():
    """找本机已有的 Chromium：ms-playwright 缓存里版本号最大的那个。"""
    roots = []
    if sys.platform == "win32":
        ld = os.environ.get("LOCALAPPDATA")
        if ld:
            roots.append(Path(ld) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for r in roots:
        if not r.is_dir():
            continue
        for d in sorted(r.glob("chromium-*"), reverse=True):
            exe = d / "chrome-win64" / "chrome.exe"
            if exe.is_file():
                return exe.parent
            exe = d / "chrome-linux" / "chrome"
            if exe.is_file():
                return exe.parent
    return None


def _link_tree(src, dst):
    """先试硬链接，失败就整份复制。返回用的哪种方式。"""
    if dst.exists():
        shutil.rmtree(dst)
    try:
        shutil.copytree(src, dst, copy_function=os.link)
        return "硬链接"
    except OSError:
        shutil.copytree(src, dst)
        return "复制"


def bundle(dist_dir, force=False):
    """确保 dist 里有 browser\\chrome-win64，返回 (方式, 字节数) 或 (None, 0)。"""
    dst = Path(dist_dir) / "browser" / "chrome-win64"
    if not force and (dst / "chrome.exe").is_file():
        total = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
        return "已存在", total
    src = source_dir()
    if src is None:
        return None, 0
    how = _link_tree(src, dst)
    total = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    return how, total


if __name__ == "__main__":
    s = source_dir()
    print("可用 Chromium 源：", s)
    print("bundled:", bundle(Path(sys.argv[1])) if len(sys.argv) > 1 else "没给目录")
