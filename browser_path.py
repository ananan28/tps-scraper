# -*- coding: utf-8 -*-
"""本机 Chromium 系浏览器定位 + 软件自带 Chromium 兜底。

nodriver 自带的 find_chrome_executable() 只翻 PROGRAMFILES 那几个环境变量，
打包成 EXE 后在别的机器上常常一个都命不中，直接抛
"could not find chrome browser binary"。这里改成四级：
界面里手选的路径 → 注册表 App Paths / 各盘符 Program Files / 所有用户配置 /
PATH（Chrome 优先、Edge、Brave 兜底） → 交付文件夹里自带的 browser\\chrome-win64。
最后这级保证目标机器一个浏览器都没装也能跑起来。
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
EDGE_SUBS = (("Microsoft", "Edge", "Application", "msedge.exe"),
             ("Microsoft", "EdgeCore", "msedge.exe"))
BRAVE_SUBS = (("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),)
# 装到别的目录时靠这两条通配兜住：<根>/<某目录>/Application/chrome.exe
GLOBS = ("*/Application/chrome.exe", "*/Application/msedge.exe",
         "*/Application/brave.exe", "*/chrome.exe", "*/Chrome-bin/chrome.exe")


def _registry_paths():
    out = []
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except ImportError:
        return out
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in ("chrome.exe", "msedge.exe", "brave.exe"):
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
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
                "LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
    for sub in ("Local", "Roaming"):
        local = Path.home() / "AppData" / sub
        if local not in roots:
            roots.append(local)
    return roots


def _fixed_paths():
    """写死到盘符上再查一遍。

    换一台机器、或者用别的东西转启动（计划任务、第三方管理器）时，
    PROGRAMFILES 这类环境变量可能是空的或指向另一个用户的配置目录，
    只按环境变量找就会全数落空，然后 nodriver 报
    "could not find chrome browser binary"。Windows 一定带 Edge，
    所以这条路至少能兜住"没装 Chrome 但有 Edge"的机器。
    """
    cands = []
    if sys.platform == "win32":
        for drive in ("C:\\", "D:\\", "E:\\"):
            for pf in (drive + "Program Files", drive + "Program Files (x86)"):
                for subs in (*CHROME_SUBS, *EDGE_SUBS, *BRAVE_SUBS):
                    cands.append(Path(pf).joinpath(*subs))
    return cands


def _other_profiles():
    """C:\\Users\\ 下别的用户配置里装的浏览器（当前账号是管理员、Chrome
    装在另一个账号名下时很常见）。只走一层通配，不递归扫盘。"""
    if sys.platform != "win32":
        return []
    out = []
    for prof in Path("C:\\Users").glob("*"):
        for sub in ("Local", "Roaming"):
            root = prof / "AppData" / sub
            for subs in (*CHROME_SUBS, *EDGE_SUBS, *BRAVE_SUBS):
                out.append(root.joinpath(*subs))
            try:
                out += [p for p in root.glob("*/Application/chrome.exe") if p.is_file()]
                out += [p for p in root.glob("*/Application/msedge.exe") if p.is_file()]
            except OSError:
                pass
    return out


def bundled_browser():
    """软件自带的那份 Chromium（交付文件夹里的 browser\\chrome-win64\\chrome.exe）。

    目标是「拷到任何一台电脑都能直接跑」，包括根本没装过 Chrome/Edge 的机器。
    顺序上放在最后：本机装了真 Chrome 就用真的，指纹比自带的干净 Chromium
    更像普通用户，被 Akamai 打分的概率低一截。
    """
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)
    for r in roots:
        for sub in (("browser", "chrome-win64", "chrome.exe"),
                    ("browser", "chrome.exe"),
                    ("browser", "chrome-win", "chrome.exe")):
            p = r.joinpath(*sub)
            if p.is_file():
                return str(p)
    return ""


def _candidates():
    cands = [Path(p) for p in _registry_paths()]
    for root in _env_dirs():
        for subs in (*CHROME_SUBS, *EDGE_SUBS, *BRAVE_SUBS):
            cands.append(root.joinpath(*subs))
        for pat in GLOBS:
            try:
                cands += [p for p in root.glob(pat) if p.is_file()]
            except OSError:
                pass
    cands += _fixed_paths() + _other_profiles()
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
_OVERRIDE = ""


def set_override(path):
    """界面里手填的浏览器路径。空串 = 交回自动探测。改一次就清一次缓存。"""
    global _OVERRIDE, _CACHE
    p = str(path or "").strip().strip('"')
    _OVERRIDE = p if p and Path(p).is_file() else ""
    _CACHE = None
    return _OVERRIDE


def find_browser():
    """返回本机可用的浏览器绝对路径；都没有就返回软件自带的那份。

    优先级：界面里手选的 → 本机装的 Chrome/Edge/Brave → 自带的 Chromium。
    """
    global _CACHE
    if _OVERRIDE:
        return _OVERRIDE
    if _CACHE is None:
        _CACHE = ""
        for c in _candidates():
            if c.is_file() and os.access(str(c), os.X_OK):
                _CACHE = str(c)
                break
        if not _CACHE:
            _CACHE = bundled_browser()
    return _CACHE or None


def browser_kind(exe=None):
    """这个浏览器是本机装的还是软件自带的，界面日志里要说清楚。"""
    exe = exe if exe is not None else find_browser()
    if not exe:
        return "没找到"
    if _OVERRIDE and Path(exe) == Path(_OVERRIDE):
        return "手动指定"
    if is_bundled(exe):
        return "软件自带"
    return "本机已装"


def is_bundled(exe=None):
    """用的是不是软件自己带的那份 Chromium。

    带的那份要关沙箱起：它从交付文件夹里跑，路径不在标准安装位置，
    Windows 沙箱偶尔会报 "Sandbox cannot access executable"，
    --no-sandbox 就绕开了。本机真装的 Chrome 不动，保持默认沙箱。
    """
    exe = exe if exe is not None else find_browser()
    b = bundled_browser()
    if not exe or not b:
        return False
    try:
        return Path(exe).resolve() == Path(b).resolve()
    except OSError:
        return False


def describe_search(limit=6):
    """把查过的位置拼成一句话，出错时好让用户知道我们找的是什么地方。"""
    tried = []
    for c in _candidates():
        tried.append(str(c))
        if len(tried) >= limit:
            break
    return "、".join(tried)


def check_text():
    """自检报告：用的是哪个、是本机装还是自带的、还找到几个备选、查过哪些地方。"""
    hits = [str(c) for c in _candidates() if c.is_file()]
    win = find_browser()
    b = bundled_browser()
    out = []
    if _OVERRIDE:
        out.append(f"手选浏览器：{_OVERRIDE}")
    out.append(f"实际会用：{win}（{browser_kind(win)}）" if win
               else "实际会用：（没找到任何浏览器，本机没装、软件也没带）")
    out.append(f"软件自带的 Chromium：{b}" if b else "软件自带的 Chromium：没有（browser 目录缺失）")
    out.append(f"本机装的浏览器共 {len(hits)} 个：")
    out += ["  " + h for h in hits[:12]]
    out.append(f"自动探测查过 {len(_candidates())} 个位置，例如：{describe_search(4)}")
    return "\n".join(out)
