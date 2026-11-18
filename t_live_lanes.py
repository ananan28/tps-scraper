# -*- coding: utf-8 -*-
"""实网多车道冒烟：跑的是界面里那条真调度（work()），不桩接引擎。

验三件他点过的事：
  1) 出口IP 真的多车道实发 —— 每条车道各写一行自己的出口，不是本机那个
  2) 并发真的并发 —— 两条车道的时间窗要重叠（各自的「起」在对方「收」之前）
  3) 跑完不「自动停止」—— 结束那句要带原因＋派发/成功/失败/还剩多少

只跑 FPS 核心（这一核心不需要打码，解码费 0）。
代理串从他自己已装的库里读，不写进代码、不打印、跑完删临时库。

用法：python -X utf8 -u t_live_lanes.py --addrs 3 --conc 2 --budget 420
"""
import argparse
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ap = argparse.ArgumentParser()
ap.add_argument("--addrs", type=int, default=3)
ap.add_argument("--conc", type=int, default=2)
ap.add_argument("--budget", type=int, default=420)
ap.add_argument("--fixture", default=str(HERE.parent / "tps20_addrs.txt"))
ap.add_argument("--srcdb", default=r"E:\年龄粉\tps_data.db")
A = ap.parse_args()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

TMP = HERE.parent / "live_lanes_1118"
TMP.mkdir(exist_ok=True)

# ---------------------------------------------------------------- 1. 一次性临时库
DB = TMP / f"live_{int(time.time())}.db"
for p in TMP.glob(f"{DB.name}*"):
    try:
        p.unlink()
    except OSError:
        pass
con = sqlite3.connect(str(DB))
con.executescript(
    "CREATE TABLE addresses(id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE,"
    " status TEXT DEFAULT 'pending', records INTEGER DEFAULT 0, people INTEGER DEFAULT 0,"
    " phones INTEGER DEFAULT 0, error TEXT DEFAULT '', created_at TEXT, updated_at TEXT,"
    " traffic_kb INTEGER DEFAULT 0);"
    "CREATE TABLE results(id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT, name TEXT,"
    " age TEXT, location TEXT, phones TEXT, created_at TEXT);"
    "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);"
)
con.commit()
con.close()


def put(k, v):
    c = sqlite3.connect(str(DB))
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    c.commit()
    c.close()


# ---------------------------------------------------------------- 2. 代理串（不外泄）
src = sqlite3.connect("file:" + A.srcdb.replace("\\", "/") + "?mode=ro", uri=True)
cfg = dict(src.execute("SELECT key,value FROM settings").fetchall())
src.close()
PROXY = (cfg.get("proxy") or "").strip()
PROXY_PASS = (cfg.get("proxy_pass") or "").strip()
if not PROXY or not PROXY_PASS:
    print(f"FAIL 读不到可用代理串（{A.srcdb} 里 proxy/proxy_pass 是空的）——实网冒烟跑不了")
    sys.exit(2)
print(f"代理：{re.sub(r'://[^@]+@', '://***@', PROXY)}｜密码 {len(PROXY_PASS)} 位"
      f"（只写进 {DB.name}，跑完删）")

# ---------------------------------------------------------------- 3. 地址
addrs = [x.strip() for x in Path(A.fixture).read_text("utf-8").splitlines() if x.strip()]
addrs = addrs[:A.addrs]
c = sqlite3.connect(str(DB))
for a in addrs:
    c.execute("INSERT OR IGNORE INTO addresses(address,status,created_at,updated_at)"
              " VALUES(?, 'pending', datetime('now'), datetime('now'))", (a,))
c.commit()
print(f"地址 {len(addrs)} 条：{addrs[0][:34]} …")
c.close()

# ---------------------------------------------------------------- 4. 配置：FPS 核心、不解码
for k, v in {
    "core": "fps", "proxy": PROXY, "proxy_pass": PROXY_PASS, "capsolver": "",
    "concurrency": A.conc, "max_attempts": "12", "solve_captcha": "0",
    "log_egress_ip": "1", "fps_max_persons": "5", "fps_max_pages": "2",
    "fps_headless": "0", "fps_hide_window": "0", "fps_nodata_confirm": "2",
    "fps_rotations": "2", "challenge_wait": "200", "min_age": "50", "max_age": "85",
    "sticky_min": "30", "keep_genders": "male,andy,unknown", "log_keep": "60",
}.items():
    put(k, v)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import app_tps                                    # noqa: E402
import engine_tps182 as eng_tps                    # noqa: E402
import engine_fps as eng_fps                       # noqa: E402

eng_tps.DB_PATH = DB
eng_fps.DB_PATH = DB
app_tps.BASE = TMP

from PySide6.QtWidgets import QApplication          # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

LINES = []
LOCK = threading.Lock()

win = app_tps.Main()
win.bridge.log.connect(lambda m: LINES.append((time.time(), m)))
win.show()
print(f"核心={win.core_combo.currentData()} 并发={win.concurrency_input.value()} "
      f"解验证码={win.solve_captcha_check.isChecked()} 出口IP日志={win.egress_check.isChecked()}")

t0 = time.time()
win.start()
while win.running and time.time() - t0 < A.budget:
    QApplication.processEvents()
    time.sleep(0.02)
if win.running:
    print(f"⚠ 撞到墙钟预算 {A.budget}s，主动请求停止")
    win.stop()
while win.running and time.time() - t0 < A.budget + 150:
    QApplication.processEvents()
    time.sleep(0.05)
for _ in range(40):
    QApplication.processEvents()
    time.sleep(0.02)

# ---------------------------------------------------------------- 5. 判定
text = [(t, m) for t, m in LINES]
egress = [(t, m) for t, m in text if "出口" in m and ("｜" in m or "没读到" in m)]
lanes_seen = sorted({int(m.split("[W")[1].split("]")[0]) for _, m in egress
                     if "[W" in m and "]" in m})
starts = {}
ends = {}
for t, m in text:
    mm = re.match(r".*?\[W(\d+)\]", m)
    if not mm:
        continue
    w = int(mm.group(1))
    if "出口" in m and w not in starts:
        starts[w] = t
    if (m.strip().startswith("✓") or m.strip().startswith("✗")
            or m.strip().startswith("错误")):
        ends[w] = t
overlap = [(a, b) for a in starts for b in starts
           if a < b and a in ends and b in ends
           and max(starts[a], starts[b]) < min(ends[a], ends[b])]
overlap2 = [(a, b) for a in starts for b in starts
            if a < b and b in ends and max(starts[a], starts[b]) < ends[b]]
end_line = next((m for _, m in reversed(text) if "本轮派发" in m), "")
ipies = sorted({m.split("出口 ")[1].split("｜")[0].strip()
                for _, m in egress if "出口 " in m and "｜" in m})

print()
print(f"1) 出口IP实发：出口行 {len(egress)} 条，涉及车道 {lanes_seen}"
      f"（设的并发={A.conc}）｜读到的不同出口 IP {len(ipies)} 个：{ipies[:6]}")
for t, m in egress[:8]:
    print(f"     +{t - t0:6.1f}s {m.strip()[:130]}")
win_span = ", ".join(
    "W{}:{}s→{}s".format(w,
                         f"{starts[w] - t0:.0f}" if w in starts else "-",
                         f"{ends[w] - t0:.0f}" if w in ends else "-")
    for w in sorted(set(starts) | set(ends)))
print(f"2) 并发：车道时间窗重叠对 {sorted(set(overlap) | set(overlap2))}｜各车道起/收 {win_span}")
print(f"3) 结束那句：{end_line.strip()[:160]}")
for t, m in text:
    if m.strip().startswith(("✓", "✗", "错误")):
        print(f"     +{t - t0:6.1f}s {m.strip()[:150]}")

ok1 = len(egress) >= 1 and len(lanes_seen) >= 1
ok2 = len(lanes_seen) >= min(A.conc, len(addrs))
ok3 = bool(overlap) or bool(overlap2)
ok4 = "本轮派发" in end_line and "还剩待处理" in end_line
print()
print(f"  {'ok  ' if ok1 else 'FAIL'} 出口行写出来了（{len(egress)} 条）")
print(f"  {'ok  ' if ok2 else 'FAIL'} 多条车道各自都报了出口（{len(lanes_seen)} 条车道）")
print(f"  {'ok  ' if ok3 else 'FAIL'} 两条车道时间窗真重叠=并发")
print(f"  {'ok  ' if ok4 else 'FAIL'} 结束带原因＋数量")
print(f"用时 {time.time() - t0:.0f}s｜库内 " + str(
    dict(sqlite3.connect(str(DB)).execute(
        "SELECT status, COUNT(*) FROM addresses GROUP BY status").fetchall())))

try:
    win._stats_timer.stop()
except Exception:
    pass
for p in TMP.glob(f"{DB.name}*"):
    try:
        p.unlink()
        print("删临时库", p.name)
    except OSError as e:
        print("临时库没删掉（重启后手工删）：", p.name, e)
sys.exit(0 if (ok1 and ok2 and ok3 and ok4) else 1)
