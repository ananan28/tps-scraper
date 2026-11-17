# -*- coding: utf-8 -*-
"""工人端 EXE 真启动冒烟（11-18，只在构建目录里跑，绝不双击交付文件夹）。

A 段（默认关着）：
① 进程活过 12 秒（没在 import/建界面时崩）；
② 真在 exe 同级建了 logs\\运行_*.txt 且有字（日志落文件那条没倒退）；
③ 一个中心都不连：这台中心（真 hub_server，临时库+现挑端口）里 0 台机器 0 行日志
   —— 「默认只监控、不勾就零请求」在 exe 上成立，不只是源码自检里成立。
B 段（勾上之后）：往 tps_data.db 写 hub_on/hub_url/hub_token（等价于他在界面上填那三格+打勾），
重新双击同一个 exe：
④ 心跳真进中心，机器名=本机 COMPUTERNAME，进度/内存/出口字段齐；
⑤ 「把号码也交上去」没勾 → 中心 rows 表 0 行（一个号码都不外传）；
⑥ 中心这边 control=0（只监控）→ 心跳带回 0 条指令，程序不会被远端动手；
⑦ 关窗口后进程真收掉。
只杀我自己起的那个 PID，不动他机器上任何别处的程序。
"""
import json
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import hub_client as hc
import hub_server as hs

DIST = pathlib.Path(r"C:\Users\AQ28n\AppData\Local\Temp\sourcescan\tps-src\dist\TPS抓取器")
EXE = DIST / "TPS抓取器.exe"
LOGS = DIST / "logs"
DB = DIST / "tps_data.db"
TMP = pathlib.Path(r"C:\Users\AQ28n\AppData\Local\Temp\sourcescan") / "worker_smoke_hub"
TMP.mkdir(parents=True, exist_ok=True)
TK = "SMOKE-WORKER-1118"
ME = hc.machine_name()
n = 0


def check(name, cond, extra=""):
    global n
    assert cond, f"✗ {name} {extra}"
    n += 1
    print(f"  ok   {name}{('｜' + str(extra)[:72]) if extra else ''}", flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def one(sql, args=()):
    return hs.db().execute(sql, args).fetchone()


def start_hub():
    hs.DB = TMP / "hub_data.db"
    for p in [hs.DB] + [pathlib.Path(str(hs.DB) + s) for s in ("-wal", "-shm")]:
        if p.exists():
            p.unlink()
    hs.CON = None
    hs.db()
    hs.conf_set("token", TK)
    hs.conf_set("control", "0")              # 中心这边：默认只监控，正好是他要的那档
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), hs.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def stop_exe(p):
    p.terminate()
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(8)


def seed_hub_settings(port):
    """等价于他在界面上填那三格 + 只勾第一个（程序是关着的才写，写完再双击）。"""
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    for k, v in (("hub_on", "1"), ("hub_url", f"http://127.0.0.1:{port}"), ("hub_token", TK)):
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit()
    con.close()


assert EXE.exists(), f"没有 {EXE}，先跑 build.py"
# 构建目录里上一次冒烟留下的库先删掉：A 段要验的是「什么都没配 = 零请求」，
# 留着一份 hub_on=1 的旧库会让那条断言假失败（也只有构建目录，交付夹一个字节都不动）
for x in list(DIST.glob("*.db*")):
    x.unlink()
    print("  清掉构建目录里的旧库：", x.name)
srv, PORT = start_hub()
try:
    # ---------------- A 段：默认关着 ----------------
    before = set(LOGS.glob("运行_*.txt")) if LOGS.exists() else set()
    t0 = time.time()
    p = subprocess.Popen([str(EXE)], cwd=str(DIST), env=dict(os.environ, PYTHONUTF8="1"))
    print(f"已启动 PID={p.pid}（构建目录内，不是交付文件夹）", flush=True)
    for i in range(24):
        time.sleep(0.5)
        if p.poll() is not None:
            break
    check("① exe 活过 12 秒：界面建起来了、没在 import 期崩", p.poll() is None,
          f"第 {(i + 1) * 0.5:.1f} 秒自己退出，返回码 {p.returncode}")
    after = set(LOGS.glob("运行_*.txt")) if LOGS.exists() else set()
    new = sorted(after - before, key=lambda x: x.stat().st_mtime)
    check("② 真在 exe 同级新建了日志文件", bool(new), [x.name for x in new])
    txt = ""
    for _ in range(8):
        try:
            txt = new[-1].read_text(encoding="utf-8", errors="replace") if new else ""
        except OSError:
            txt = ""
        if txt.strip():
            break
        time.sleep(0.5)
    check("② 日志文件里有字（不是空壳）", len(txt.strip()) > 0, f"{len(txt)} 字节")
    time.sleep(3.0)      # 真在偷偷发的话，这 3 秒足够发一跳
    check("③ 没勾中心 = 一个包都不发（中心里 0 台机器）",
          one("SELECT COUNT(*) c FROM machines")[0] == 0,
          [r["machine"] for r in hs.db().execute("SELECT machine FROM machines")])
    check("③ 屏上也没冒「中心没连上」", "中心没连上" not in txt,
          [l for l in txt.splitlines() if "中心" in l][:2])
    stop_exe(p)
    check("④ A 段收工：进程真没了", p.poll() is not None, p.poll())

    # ---------------- B 段：填好那三格、勾上第一个，再双击同一个 exe ----------------
    assert DB.exists(), f"程序跑过却没建库：{DB}"
    seed_hub_settings(PORT)
    p2 = subprocess.Popen([str(EXE)], cwd=str(DIST), env=dict(os.environ, PYTHONUTF8="1"))
    print(f"第二次启动 PID={p2.pid}（已勾「连到中心」）", flush=True)
    got = None
    t0 = time.time()
    while time.time() - t0 < 75:
        if p2.poll() is not None:
            break
        r = one("SELECT * FROM machines WHERE machine=?", (ME,))
        if r is not None:
            got = dict(r)
            break
        time.sleep(1.0)
    check("④ 真 exe 的心跳进来了（不用我这边动手，程序自己发的）", got is not None,
          f"等了 {time.time()-t0:.0f} 秒")
    if got:
        need = ("core", "state", "people", "phones", "mem_mb", "egress", "hb_at", "upload", "ctl")
        missing = [k for k in need if k not in got]
        check("④ 监控要看的那几项中心全都有", not missing, missing)
        check("④ 机器名就是本机 COMPUTERNAME", got["machine"] == ME, got["machine"])
        check("⑤ 「把号码也交上去」没勾 → 中心 0 行号码",
              one("SELECT COUNT(*) c FROM rows")[0] == 0)
        check("⑥ 这台自己报的 ctl=0（不听控制）", got.get("ctl") in (0, None), got.get("ctl"))
        st = hs.snapshot()
        m0 = [m for m in st["machines"] if m["machine"] == ME][0]
        check("⑥ 面板取快照拿得到这台，且标着「不听控制 / 只报数」",
              m0["ctl"] == 0 and m0["upload"] == 0 and m0["online"] is True,
              {k: m0[k] for k in ("state", "people", "phones", "ctl", "upload", "ago")})
        html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/?token={TK}", timeout=6).read().decode("utf-8")
        check("⑥ 面板页发得出来（他浏览器看到的就是这份）",
              "允许远程控制和分配任务" in html and "只监控：进度、日志、出口都在看" in html, len(html))
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/cmd",
                                     data=json.dumps({"machine": ME, "cmd": "start"}).encode(),
                                     headers={"X-Hub-Token": TK, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=6)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("⑥ 中心只监控时控制指令真被拒（403），那台 exe 不会被远端开动", code == 403, code)
    stop_exe(p2)
    check("⑦ B 段收工：进程真没了", p2.poll() is not None, p2.poll())
    left = sorted(x.name for x in DIST.glob("*.db*"))
    print(f"  ……构建目录里跑完留下的东西（不进包，包里只拷 exe）：{left}")
    print(f"  ……启动到界面出来：A 段 {time.strftime('%H:%M:%S', time.localtime(t0))}")
finally:
    srv.shutdown()
    srv.server_close()
print(f"\n✓ 工人端 EXE 冒烟全过（{n} 项）｜中心端口 {PORT} 现挑")
