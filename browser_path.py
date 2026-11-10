# -*- coding: utf-8 -*-
"""本机 Chromium 系浏览器定位。

nodriver 自带的 find_chrome_executable() 只翻 PROGRAMFILES 那几个环境变量，
打包成 EXE 后在别的机器上常常一个都命不中，直接抛
"could not find chrome browser binary"。这里改成：先问注册表 App Paths，
再看常见安装目录和 PATH，Chrome 优先、Edge 兜底。
"""
import os
import shutil
import sys
from pathlib import Path

CHROME_SUBS = (
    ("Google", "Chrome", "Application", "chrome.exe"),
    ("Google", "Chrome Beta", "Application", "chrome.exe"),
    ("Google", "Chrome Canary", "Application", "chrome.exe"),
    ("Chromium", "Application", "chrome.exe"),
)
EDGE_SUBS = (("Microsoft", "Edge", "Application", "msedge.exe"),)


def _registry_paths():
    out = []
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except ImportError:
        return out
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in ("chrome.exe", "msedge.exe"):
            key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
            try:
                with winreg.OpenKey(hive, key) as h:
                    val = winreg.QueryValue(h, None)
            except OSError:
                continue
            if val:
                out.append(str(val).strip('"'))
    return out


def _env_dirs():
    roots = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    local = Path.home() / "AppData" / "Local"
    if local not in roots:
        roots.append(local)
    return roots


def _candidates():
    cands = [Path(p) for p in _registry_paths()]
    for root in _env_dirs():
        for subs in (*CHROME_SUBS, *EDGE_SUBS):
            cands.append(root.joinpath(*subs))
    for name in ("chrome", "chromium", "chromium-browser", "google-chrome", "msedge"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    if sys.platform == "darwin":
        cands += [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        for name in ("chrome", "chromium", "chromium-browser", "google-chrome-stable"):
            cands.append(Path("/usr/bin") / name)
    seen, uniq = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen:
            continue
        seen.add(r)
        uniq.append(r)
    return uniq


_CACHE = None


def find_browser():
    """返回本机可用的浏览器绝对路径；没装就返回 None。查一次记下来，翻页轮换不重复扫盘。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = ""
        for c in _candidates():
            if c.is_file() and os.access(str(c), os.X_OK):
                _CACHE = str(c)
                break
    return _CACHE or None


def describe_search(limit=6):
    """把查过的位置拼成一句话，出错时好让用户知道我们找的是什么地方。"""
    tried = []
    for c in _candidates():
        tried.append(str(c))
        if len(tried) >= limit:
            break
    return "、".join(tried)
