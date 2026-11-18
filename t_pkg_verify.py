# -*- coding: utf-8 -*-
"""交付前两道硬检查：
1) 新 exe 需要的 _internal 文件集合，必须是底版 _internal 的子集 ——
   是子集就意味着「只换 exe」不会缺 DLL；多出别的文件也不要。
2) 新 exe 里真的带着这轮的四件事：egress_ip 模块、200 行截行、按批捞地址、解码报错落日志。
"""
import hashlib
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

NEW = Path(r"C:/Users/AQ28n/AppData/Local/Temp/sourcescan/tps-base/dist/TPS抓取器")
BASE = Path(r"E:/限流识别版_两核心覆盖包_11")


def rel(root):
    return {str(p.relative_to(root)).replace("\\", "/").lower()
            for p in root.rglob("*") if p.is_file()}


bad = 0

# ---------- 1) _internal 依赖集合 ----------
new_i, base_i = rel(NEW / "_internal"), rel(BASE / "_internal")
missing = sorted(new_i - base_i)
print(f"  _internal：新包 {len(new_i)} 个文件｜底版 {len(base_i)} 个")
if missing:
    print(f"  FAIL 新 exe 需要、底版却没有的文件 {len(missing)} 个（只换 exe 会缺料）：")
    for m in missing[:15]:
        print(f"         {m}")
    bad += 1
else:
    print("  ok   新包要的 _internal 文件底版全有 ⇒ 只换 exe 安全")

# 底版有、新包没有的：不影响（老 DLL 留着没人用），只报个数
print(f"  （底版独有 {len(base_i - new_i)} 个，留着不碍事）")

# ---------- 2) exe 里真的带着新代码 ----------
from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

ar = CArchiveReader(str(NEW / "TPS抓取器.exe"))
toc = set(ar.toc)
print(f"  CArchive TOC {len(toc)} 项")

import marshal  # noqa: E402

pyz = ar.open_embedded_archive("PYZ.pyz")
mods = set(pyz.toc)


def strings_of(mod_name):
    """app_tps 是入口脚本，躺在 CArchive 里（类型 s）；其余本地模块在 PYZ.pyz 里。
    两处都要能看到，才算这轮改动真打进 exe。"""
    if mod_name in ar.toc:
        code = ar.extract(mod_name)
    else:
        code = pyz.extract(mod_name)
    if isinstance(code, (bytes, bytearray)):
        code = marshal.loads(code)
    out = []

    def walk(o):
        if isinstance(o, str):
            out.append(o)
        elif isinstance(o, (bytes, bytearray)):
            out.append(o.decode("utf-8", "replace"))
        elif isinstance(o, tuple) or isinstance(o, list):
            for x in o:
                walk(x)
        elif hasattr(o, "co_consts"):
            out.extend(n for n in getattr(o, "co_names", ()) if isinstance(n, str))
            out.extend(n for n in getattr(o, "co_varnames", ()) if isinstance(n, str))
            for c in o.co_consts:
                walk(c)

    walk(code)
    return out


need_mod = ["egress_ip", "app_tps", "engine_tps182", "engine_fps"]
for m in need_mod:
    where = "CArchive(入口脚本)" if m in ar.toc else ("PYZ.pyz" if m in mods else "")
    ok = bool(where)
    print(f"  {'ok  ' if ok else 'FAIL'} exe 里有模块 {m}"
          + (f"（{where}，{len(strings_of(m))} 段字符串）" if ok else ""))
    if not ok:
        bad += 1

checks = [
    ("app_tps", "MoveOperation", "屏上截行用了全限定枚举（修「日志不会自动删除」）"),
    ("app_tps", "UI_LOG_LINES", "屏上只留 200 行那个常量在"),
    ("app_tps", "_claim_pending", "按批回库捞地址（修「会自动停止」）"),
    ("app_tps", "库里已无待处理地址", "结束那句带原因"),
    ("app_tps", "日志写出口IP", "界面多了出口IP勾选框"),
    ("app_tps", "下一条地址起生效", "跑动中改勾选立刻生效"),
    ("engine_tps182", "2Captcha 拒收任务", "厂家原话落日志（修「付费解码查不出原因」）"),
    ("engine_tps182", "这次不解码", "不解码必说一句原因"),
    ("engine_fps", "_log_egress", "FPS 每车道报出口"),
    ("engine_fps", "humancheck", "站点「Are you human?」确认页有独立档（11-18 他 8 张截图）"),
    ("engine_fps", "reCAPTCHA 确认页", "确认页报错那句带原因"),
    ("engine_fps", "_is_blank", "全白页单列档（修「空白页白等 201s」）"),
    ("engine_fps", "重开浏览器换出口", "白页/死口到点换出口那句话在"),
]
for mod, needle, what in checks:
    hay = "\n".join(strings_of(mod))
    ok = needle in hay
    print(f"  {'ok  ' if ok else 'FAIL'} {mod} 含「{needle}」—— {what}")
    if not ok:
        bad += 1

h = hashlib.sha256((NEW / "TPS抓取器.exe").read_bytes()).hexdigest()
print(f"\n  新 exe：{(NEW / 'TPS抓取器.exe').stat().st_size} 字节｜SHA256 {h}")
old_h = hashlib.sha256((BASE / "TPS抓取器.exe").read_bytes()).hexdigest()
print(f"  底版 exe：{(BASE / 'TPS抓取器.exe').stat().st_size} 字节｜SHA256 {old_h}")

print(f"\n合计不过 {bad} 项")
sys.exit(1 if bad else 0)
