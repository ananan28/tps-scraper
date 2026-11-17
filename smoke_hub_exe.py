# -*- coding: utf-8 -*-
"""中心端 EXE 真启动冒烟（11-18）：把桌面那份拷到临时目录跑，绝不在交付文件夹里跑
（真起服务会在 exe 同级生成 hub_data.db，那就把无数据包弄脏了）。

要验的是打进去的那份代码真能干活：
· 黑窗口那几行（中心地址 / 令牌 / 当前模式）打没打出来；
· 没令牌 401、有令牌 200；
· 默认就是「只监控」：面板上按钮不出现、/api/cmd 直接 403；
· 面板 HTML 真发得出来（他浏览器里看到的就是这份）。

两处坑（第一版撞过，别再撞）：
1 端口必须现挑：这台机器上同时挂着兄弟会话的服务（8799 就被 oh_sink.py 占过），
  写死端口会连到别人家去，判出来的全是假结果；
2 读黑窗口输出不能阻塞在 readline 上：banner 打完就没声了，主线程等下一行能等到天荒地老
  （第一版就是这么挂住的）。所以输出交给一条线程灌队列，主线程一律带超时地取。
"""
import json
import os
import pathlib
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = pathlib.Path.home() / "Desktop" / "抓号机器中心"
TMP = pathlib.Path(r"C:\Users\AQ28n\AppData\Local\Temp\sourcescan") / "hub_smoke"
TOKEN = "SMOKE-TK-" + str(int(time.time()))[-6:]
n = 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check(name, cond, extra=""):
    global n
    assert cond, f"✗ {name} {extra}"
    n += 1
    print(f"  ok   {name}{('｜' + str(extra)[:70]) if extra else ''}", flush=True)


PORT = free_port()


def call(path, tok="", obj=None, timeout=8):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                                 headers={"X-Hub-Token": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
shutil.copytree(SRC, TMP)
exe = TMP / "hub.exe"
# PYTHONUTF8：不加的话 exe 用 GBK 打屏，这边按 utf-8 解就成了乱码，"这个窗口别关"永远匹配不上
env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
p = subprocess.Popen([str(exe), "--port", str(PORT), "--token", TOKEN],
                     cwd=str(TMP), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     env=env)

lines: "queue.Queue[str]" = queue.Queue()


def pump():
    """子进程输出 → 逐行塞队列；进程死了就塞一个 None 收尾，主线程绝不干等。"""
    try:
        for raw in iter(p.stdout.readline, b""):
            lines.put(raw.decode("utf-8", "replace"))
    except Exception:
        pass
    lines.put(None)


threading.Thread(target=pump, daemon=True).start()
try:
    # ---------- 等黑窗口那几行（总预算 30 秒，取不到就带着已收到的部分继续） ----------
    banner, t0 = "", time.time()
    while time.time() - t0 < 30:
        try:
            item = lines.get(timeout=1.0)
        except queue.Empty:
            if "这个窗口别关" in banner:        # 打完了，别再干等下一行
                break
            if p.poll() is not None:
                break
            continue
        if item is None:
            break
        banner += item
        if "这个窗口别关" in banner:
            break
    print("--- 黑窗口打出来的：")
    print(banner.rstrip())
    check("exe 真起来了（进程还活着）", p.poll() is None, f"退出码={p.poll()}｜{banner[-200:]}")
    lan = [l for l in banner.splitlines() if "http://" in l and "127.0.0.1" not in l]
    check("banner 有中心地址（局域网 IP:端口，不是 127）",
          bool(lan) and str(PORT) in lan[0], lan[:1])
    check("banner 把令牌打在屏上", TOKEN in banner)
    check("banner 说明默认是只监控", "只监控" in banner)
    check("banner 提醒窗口别关", "这个窗口别关" in banner)

    # ---------- 中心真在serve：等它开口，最多 20 秒 ----------
    code = None
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            code, _ = call("/api/state", timeout=3)
            break
        except Exception:
            time.sleep(0.5)
    check("端口真的在服务（第一趟就有回包）", code is not None, code)
    code, body = call("/api/state")
    check("没令牌 401", code == 401, body[:40])
    code, body = call("/api/state", tok=TOKEN)
    check("对令牌 200", code == 200, body[:40])
    st = json.loads(body)
    check("exe 里的默认就是只监控（control=0）", st["control"] == 0, st["control"])
    code, body = call("/api/cmd", tok=TOKEN, obj={"machine": "PCX", "cmd": "start"})
    check("只监控下 /api/cmd 拒收（403）", code == 403, body[:60])
    code, body = call("/api/hb", tok=TOKEN, obj={"machine": "冒烟机", "state": "running",
                                                 "people": 3, "phones": 7, "ctl": 0, "upload": 0})
    check("心跳吃得下（只监控也收心跳）", code == 200 and json.loads(body).get("ok") is True, body[:60])
    code, body = call("/api/state", tok=TOKEN)
    m0 = [m for m in json.loads(body)["machines"] if m["machine"] == "冒烟机"]
    check("报上来的数字在中心落住了", m0 and m0[0]["people"] == 3 and m0[0]["phones"] == 7,
          m0[:1])
    code, body = call("/")
    check("面板页发得出来", code == 200 and "允许远程控制和分配任务" in body, len(body))
    check("面板上有「只监控」那句提示", "只监控：进度、日志、出口都在看" in body)
    check("面板卡片带交号码/听控制两格", "不听控制" in body and "只报数" in body)
    db = sorted(x.name for x in TMP.glob("hub_data.db*"))
    check("真跑过一趟，临时目录里确实生成了库（证明能落盘）", bool(db), db)
finally:
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(5)
check("中心进程收工后不留残留", p.poll() is not None, p.poll())
check("桌面那份交付夹仍然一个数据文件都没有",
      not list(SRC.rglob("*.db*")), list(SRC.rglob("*.db*")))
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n✓ 中心端 EXE 冒烟全过（{n} 项）｜端口 {PORT} 现挑，未写死")
