# -*- coding: utf-8 -*-
"""查打的 exe 里到底有没有这次新写的代码（11-18 口径）。

为什么不能只 grep 文件：PyInstaller 6.x 的 onedir 里 CArchive TOC 只有十几项，
本地模块全在 exe 内部的 PYZ.pyz 里，而且 3.11+ 的字符串常量不在 co_names 里 ——
所以要 `CArchiveReader.open_embedded_archive("PYZ.pyz")` 拿到模块的 **code object**，
再递归 co_consts 把字符串收出来比。

只读文件，不起进程、不碰数据库。
"""
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
EXE = Path(sys.argv[1] if len(sys.argv) > 1
           else Path(__file__).resolve().parent / "dist" / "TPS抓取器" / "TPS抓取器.exe")
n = 0


def check(name, cond, extra=""):
    global n
    assert cond, f"✗ {name} {extra}"
    n += 1
    print(f"  ok   {name}{('｜' + str(extra)[:78]) if extra else ''}", flush=True)


def strs(co, acc=None):
    """code object 里能捞到的字符串常量（含嵌套的函数/类）。"""
    if acc is None:
        acc = set()
    for c in getattr(co, "co_consts", ()) or ():
        if isinstance(c, str):
            acc.add(c)
        elif hasattr(c, "co_consts"):
            strs(c, acc)
    for c in getattr(co, "co_names", ()) or ():
        if isinstance(c, str):
            acc.add(c)
    return acc


print(f"查 {EXE.name}（{EXE.stat().st_size/2**20:.2f}MB）")
ar = CArchiveReader(str(EXE))
pyz = ar.open_embedded_archive("PYZ.pyz")
mods = set(pyz.toc.keys()) if isinstance(pyz.toc, dict) else set(getattr(pyz, "toc", {}).keys())


def app_code():
    """入口脚本（app_tps.py）不在 PYZ 里，它在 CArchive 顶层 —— 11-18 实测：
    TOC 就 11 项、含 app_tps；本地模块才在 PYZ（1653 项）。"""
    co = mod_code("app_tps") if "app_tps" in mods else None
    if co is not None:
        return co
    raw = ar.extract("app_tps")
    import marshal
    for skip in (0, 16):          # .pyc 头：magic+flags+mtime+size = 16 字节
        try:
            return marshal.loads(raw[skip:])
        except Exception:
            continue
    raise AssertionError("取不到入口脚本的 code object")


for m in ("hub_client", "egress_ip", "engine_tps182", "engine_fps"):
    check(f"PYZ 里有模块 {m}", m in mods, sorted(x for x in mods if x.startswith(("app_", "hub_", "engine_", "egress")))[:8])
co_app = app_code()
check("入口脚本 app_tps 也在包里（CArchive 顶层）", co_app is not None and hasattr(co_app, "co_consts"))

def as_code(x):
    """reader.extract() 在不同版本里有的回 code object、有的回 marshal 字节：两种都接下来。"""
    import marshal
    if hasattr(x, "co_consts"):
        return x
    if isinstance(x, (bytes, bytearray)):
        for skip in (0, 16):
            try:
                return marshal.loads(bytes(x)[skip:])
            except Exception:
                continue
    raise AssertionError(f"extract 回来的不是 code object：{type(x)}")


def mod_code(name):
    return as_code(pyz.extract(name))


hc = strs(mod_code("hub_client"))
ALL_HC = "\n".join(hc)
for lit in ("/api/hb", "/api/logs", "/api/rows", "/api/cmd_ack", "/api/task_get", "/api/task_done",
            "hub_seq", "hub_last_rid", "hub_upload", "hub_ctl", "hub_on", "hub_url", "hub_token",
            "machine_name"):
    check(f"hub_client 真带了 {lit}", lit in ALL_HC)
fns = {"ping", "finish", "_take_and_import", "_flush_logs", "_run_cmd", "offer", "_push_rows",
       "_status_safe", "enabled", "upload_rows", "allow_control", "machine_name"}
check("工人端的函数（ping/finish/取地址/成批推/三个开关…）名字全在 code 里",
      fns <= hc, sorted(fns - hc))

AT = "\n".join(strs(co_app))     # 界面上的字多半嵌在长句里（勾的文案就是整句），所以按包含比
for lit in ("连到中心", "把号码也交上去", "允许中心控制这台", "中心地址", "测试一下", "中心/远程",
            "hub_on", "hub_url", "hub_token", "hub_upload", "hub_ctl"):
    check(f"界面里有 {lit}", lit in AT)

fs = strs(mod_code("engine_fps")) if "engine_fps" in mods else set()
tp = strs(mod_code("engine_tps182")) if "engine_tps182" in mods else set()
check("两个核心的引擎都还在包里（没被这次改动挤掉）", bool(fs) and bool(tp),
      f"fps {len(fs)} 串 / tps {len(tp)} 串")
check("1.8.2 引擎的解码关键字没动（说明抽取算法还是那条）",
      any("humancheck" in s or "2captcha" in s.lower() or "recaptcha" in s.lower() for s in tp),
      [s for s in list(tp) if "check" in s.lower()][:3])
check("两个核心都挂了出口 IP 模块（那行「[W2] 出口 …」是它们自己 import 的）",
      "egress_ip" in fs and "egress_ip" in tp)
check("出口模块自己知道去哪读 IP",
      any(k in "\n".join(strs(mod_code("egress_ip"))) for k in ("ip-api", "ipify")))
print(f"\n✓ exe 内容核对全过（{n} 项）｜{EXE.name} {EXE.stat().st_size} 字节")
