# -*- coding: utf-8 -*-
"""「五件事」离线自检：不开浏览器、不联网、不花一分钱解码费。

同一份断言，既能跑在底版（限流识别版 24f477d）上量出坏在哪，也能跑在修好的版本上验是否真修好。
    set QT_QPA_PLATFORM=offscreen && python t_five_fixes.py

覆盖他点名的五条：
  T1 日志不会自动删除   —— 屏上日志框只留最近 UI_LOG_LINES 行
  T2 勾选真生效         —— 「解验证码」跑动中取消勾选，下一地址立刻不带解码；勾上立刻带回
  T3 会自动停止         —— 待处理超过 1000 条也不能一次性取 1000 条就收摊
  T4 结束必须说清原因    —— 完成/停止/暂停 + 本轮处理数 + 库里还剩几个待处理
  T5 一条地址炸不能带崩整轮 + 崩了那条不许留在 running
  T6 并发抓             —— 车道号真按车道走（同时在跑的车道数 = 界面并发数），不再一路一个编号
  T7 出口IP 多车道实发   —— egress_ip 模块在、两核心每个浏览器一起来就报自己那条出口
  T8 付费解码真发得出去   —— 勾选=1 次 in.php；不勾=0 次；厂家报错必须落到日志上
"""
import os
import sqlite3
import sys
import threading
import time
import types
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except Exception:
    pass

TMP = HERE / "_t_five_fixes_db"
TMP.mkdir(exist_ok=True)
DB = TMP / "tps_data.db"

import app_tps                    # noqa: E402  界面（含调度线程、日志框）
import engine_tps182 as eng_tps    # noqa: E402 核心一：TPS 1.8.2
import engine_fps as eng_fps       # noqa: E402 核心二：FPS

RESULTS = []
EXC = []          # 探针期间冒出来的异常（比如截行那句一超 200 行就抛）


def _hook_exc(type_, value, tb):
    txt = "".join(traceback.format_exception(type_, value, tb))[:3000]
    EXC.append(txt)
    print("【未捕获异常】" + txt.replace("\n", "\n    ")[:1500])


sys.excepthook = _hook_exc


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, str(detail)[:400]))
    print(("  ok   " if ok else "  FAIL ") + name + ("｜" + str(detail)[:200] if detail else ""))


def fresh_db(path=None):
    """每段测试用各自的一次性库文件，绝不靠删旧文件（Qt 的 3 秒统计定时器还占着它）。"""
    global DB
    DB = path or (TMP / f"db_{int(time.time() * 1000)}_{os.getpid()}.db")
    for p in TMP.glob(f"{Path(DB).name}*"):
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


def set_setting(key, value):
    con = sqlite3.connect(str(DB))
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    con.commit()
    con.close()


def get_setting(key, default=""):
    con = sqlite3.connect(str(DB))
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else default


def add_rows(n, status="pending", tag="a"):
    con = sqlite3.connect(str(DB))
    for i in range(n):
        con.execute("INSERT OR IGNORE INTO addresses(address,status,created_at,updated_at)"
                    " VALUES(?,?,datetime('now'),datetime('now'))",
                    (f"{tag}{i} 1 Main St, Albany, GA 31710", status))
    con.commit()
    n_all = con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    con.close()
    return n_all


def status_counts():
    con = sqlite3.connect(str(DB))
    out = dict(con.execute("SELECT status, COUNT(*) FROM addresses GROUP BY status").fetchall())
    con.close()
    return out


def pump(win, seconds=2.0):
    """跑 Qt 事件循环一会儿（后台线程的 log 信号靠它投递）。"""
    from PySide6.QtWidgets import QApplication
    t0 = time.time()
    while time.time() - t0 < seconds:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()


def wait_finished(win, timeout=60.0):
    from PySide6.QtWidgets import QApplication
    t0 = time.time()
    while win.running and time.time() - t0 < timeout:
        QApplication.processEvents()
        time.sleep(0.02)
    QApplication.processEvents()
    return not win.running


class NoSleep:
    """只把调度线程里的空转等待去掉，让 1200 条地址的调度测试几秒内跑完。
    time.time / strftime 照旧走真模块，绝不影响计时。"""

    def __init__(self, real):
        self._real = real

    def sleep(self, s):
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def make_win(app_tps, n_addr=3, tag="w"):
    fresh_db()
    eng_tps.DB_PATH = DB
    eng_fps.DB_PATH = DB
    set_setting("proxy", "http://u:{session}@proxy.invalid:10000")
    set_setting("proxy_pass", "pw")
    set_setting("capsolver", "TESTKEY")
    set_setting("core", "tps")
    add_rows(n_addr, tag=tag)
    win = app_tps.Main()
    return win


# ------------------------------------------------------------------ 假页面（T8 用）
class FakeResponse:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def text(self):
        return str(self._p)


class FakeRequest:
    """page.request.get —— 出口探活走的就是这一条（不渲染、最省）。"""

    def __init__(self, body):
        self._b = body

    def get(self, url, timeout=None):
        return FakeResponse(self._b)


class FakePage:
    """够用的 Playwright page 替身：详情页被拦 → 走 try_solve_challenge。"""

    def __init__(self, url="https://www.truepeoplesearch.com/details#abcd",
                 content="<html><body>checking your browser</body></html>", title="",
                 echo="198.51.100.77"):
        self.url = url
        self._content = content
        self.title = title or "Just a moment, please wait"
        self.eval_calls = []
        self.request = FakeRequest(echo)

    def wait_for_timeout(self, ms):
        return None

    def content(self):
        return self._content

    def evaluate(self, js, arg=None):
        self.eval_calls.append(js[:40])
        # 顺序要紧：CHALLENGE_INFO_JS 和 SUBMIT_CAPTCHA_JS 里都写着 captchaForm
        if "captchaToken" in js:
            return {"success": True, "redirectUrl": self.url}
        if "returnUrl" in js:
            return {"action": "900", "returnUrl": self.url, "formAction": self.url}
        if "!!document.getElementById('captchaForm')" in js:
            return True
        return "198.51.100.77"


def run_t8(app_tps, eng_tps):
    """勾选 → 恰好 1 次 in.php；取消勾选 → 0 次；厂家报错必须出现在日志里。"""
    calls = []
    reply = {"status": 1, "request": "TOKEN"}

    class StubRequests:
        @staticmethod
        def post(url, data=None, timeout=None, **kw):
            calls.append(("post", url, dict(data or {})))
            return FakeResponse(reply)

        @staticmethod
        def get(url, params=None, timeout=None, **kw):
            calls.append(("get", url, dict(params or {})))
            return FakeResponse({"status": 1, "request": "3A1B2C"})

        class exceptions:
            @staticmethod
            def RequestException(*a):
                return Exception(*a)

    real_requests = eng_tps.requests
    eng_tps.requests = StubRequests
    logs = []
    try:
        # 甲：勾了解码（默认）→ 必须发一次 in.php
        calls.clear()
        eng_tps.scrape_detail.capsolver_key = "TESTKEY"
        eng_tps.scrape_detail.solve_captcha = True
        eng_tps.scrape_detail.upstream_proxy = "http://u:s1@proxy.invalid:10000"
        eng_tps.scrape_detail.progress = lambda m: logs.append(str(m))
        page = FakePage()
        real_goto = eng_tps.goto_page
        eng_tps.goto_page = lambda p, u, **kw: True
        try:
            eng_tps.scrape_detail(page, {"link": page.url}, "x", "2026-11-18 00:00:00")
        finally:
            eng_tps.goto_page = real_goto
        posts = [c for c in calls if c[0] == "post" and "in.php" in c[1]]
        checked_n = len(posts)
        post_params = posts[0][2] if posts else {}
        post_url = posts[0][1] if posts else ""

        # 乙：取消勾选 → 一个解码请求都不许发
        calls.clear()
        logs.clear()
        eng_tps.scrape_detail.solve_captcha = False
        page2 = FakePage()
        eng_tps.goto_page = lambda p, u, **kw: True
        try:
            eng_tps.scrape_detail(page2, {"link": page2.url}, "x", "2026-11-18 00:00:00")
        finally:
            eng_tps.goto_page = real_goto
        unchecked_n = len([c for c in calls if c[0] == "post" and "in.php" in c[1]])

        # 丙：厂家回错误码 → 错误码要落到日志（不能只写「未返回 token」）
        calls.clear()
        logs.clear()
        reply_saved = dict(reply)
        reply.clear()
        reply.update({"status": 0, "request": "ERROR_ZERO_BALANCE"})
        eng_tps.scrape_detail.solve_captcha = True
        page3 = FakePage()
        eng_tps.goto_page = lambda p, u, **kw: True
        try:
            eng_tps.scrape_detail(page3, {"link": page3.url}, "x", "2026-11-18 00:00:00")
        finally:
            eng_tps.goto_page = real_goto
            reply.clear()
            reply.update(reply_saved)
        err_seen = any("ERROR_ZERO_BALANCE" in m for m in logs)
    finally:
        eng_tps.requests = real_requests

    check("T8 勾选「解验证码」＝恰好发 1 次解码请求", checked_n == 1, f"实测 in.php {checked_n} 次")
    check("T8 请求带的是 2Captcha 的接口", bool(post_params.get("key")) and "2captcha" in post_url,
          post_url)
    check("T8 出口代理一起交给解码商（token 才绑得住 IP）",
          bool(post_params.get("proxy")) and bool(post_params.get("proxytype")),
          sorted(post_params.keys()))
    check("T8 TPS 那个 action 值有带过去（参数名以官方为准）",
          any(k in post_params for k in ("action", "data-s")), sorted(post_params.keys()))
    check("T8 取消勾选＝0 次解码请求（一分钱都不花）", unchecked_n == 0, f"实测 {unchecked_n} 次")
    check("T8 厂家报错码原样落到日志（不再是光一句「未返回 token」）", err_seen, logs[:3])


def close_win(win):
    try:
        win._stats_timer.stop()
    except Exception:
        pass
    getattr(win, "close")()
    win.deleteLater()


def main():
    eng_tps.DB_PATH = DB
    eng_fps.DB_PATH = DB
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    print(f"底版探测：app_tps.py 来自 {Path(app_tps.__file__).name}｜"
          f"UI 截行数={getattr(app_tps, 'UI_LOG_LINES', '（没这个常量）')}")

    # ---------------------------------------------------------------- T1 屏上日志自动删除
    win = make_win(app_tps, 3)
    t1_err = ""
    for i in range(400):
        try:
            win.log(f"第{i}行")
        except Exception as e:
            t1_err = f"{type(e).__name__}: {e}"
    doc = win.log_box.document()
    lines = doc.toPlainText().splitlines()
    check("T1 屏上日志只留最近 200 行（多了自动删旧的）", doc.blockCount() <= 200,
          f"灌 400 行后屏上实际 {doc.blockCount()} 行；"
          f"最后一行={lines[-1][:24] if lines else ''}；抛的异常={t1_err or '无'}")
    check("T1 删的是最旧的、留下的必须是最近的", bool(lines) and lines[-1].endswith("第399行"),
          lines[-1][:30] if lines else "")
    close_win(win)

    # ---------------------------------------------------------------- T2 勾选跑动中真生效
    win = make_win(app_tps, 2, tag="s")
    seen = []
    gate = threading.Event()
    first = threading.Event()

    def fake_solve_flagged(address, *a, **kw):
        seen.append((address, kw.get("solve_captcha", "（没传这个参数）")))
        first.set()
        gate.wait(30)
        con = sqlite3.connect(str(DB))
        con.execute("UPDATE addresses SET status='done' WHERE address=?", (address,))
        con.commit()
        con.close()
        return True, {"people": 1, "phones": 1, "traffic_kb": 1}

    real_proc = eng_tps.process_address
    eng_tps.process_address = fake_solve_flagged
    try:
        win.solve_captcha_check.setChecked(True)
        win.concurrency_input.setValue(1)
        win.start()
        first.wait(20)
        # 跑动中取消勾选（他就是在跑到一半的时候点的）
        win.solve_captcha_check.click()
        checked_after = win.solve_captcha_check.isChecked()
        setting_after = get_setting("solve_captcha", "（没落库）")
        gate.set()
        wait_finished(win, 30)
        pump(win, 0.3)
        second_flag = seen[1][1] if len(seen) > 1 else "（第二条根本没派发）"
        check("T2 勾选框真能取消（点一下就变）", checked_after is False, f"取消后 isChecked={checked_after}")
        check("T2 跑动中取消，下一地址立刻不解码（不静默按开跑前那一次算）",
              second_flag is False, f"第二条地址拿到的 solve_captcha={second_flag}")
        check("T2 取消这件事当场落库（重启还认）", setting_after == "0", f"solve_captcha={setting_after}")
    finally:
        eng_tps.process_address = real_proc
        close_win(win)

    # ---------------------------------------------------------------- T3 超过 1000 条不许收摊
    win = make_win(app_tps, 1200, tag="big")
    disp = []
    real_time = app_tps.time
    app_tps.time = NoSleep(real_time)

    def fake_count(address, *a, **kw):
        disp.append(address)
        con = sqlite3.connect(str(DB))
        con.execute("UPDATE addresses SET status='done' WHERE address=?", (address,))
        con.commit()
        con.close()
        return True, {"people": 1, "phones": 1, "traffic_kb": 1}

    eng_tps.process_address = fake_count
    try:
        win.concurrency_input.setValue(8)
        win.start()
        done = wait_finished(win, 120)
        pump(win, 0.5)
        left = status_counts().get("pending", 0)
        check("T3 库里 1200 条待处理，一条都不许落下（不再取 1000 就收摊）",
              done and len(disp) == 1200 and left == 0,
              f"本轮派发 {len(disp)} 条，跑完还剩待处理 {left} 条")
        txt = win.log_box.toPlainText().splitlines()
        tail = txt[-1] if txt else ""
        check("T4 结束那句要说清原因＋数量（不是光一个「完成」）",
              ("待处理" in tail and "本轮" in tail) or ("停止" in tail or "暂停" in tail),
              tail[:80])
    finally:
        eng_tps.process_address = real_proc
        app_tps.time = real_time
        close_win(win)

    # ---------------------------------------------------------------- T5 一条炸不能带崩整轮
    win = make_win(app_tps, 3, tag="boom")
    hits = []

    def fake_boom(address, *a, **kw):
        hits.append(address)
        con = sqlite3.connect(str(DB))
        if len(hits) == 1:
            con.execute("UPDATE addresses SET status='running' WHERE address=?", (address,))
            con.commit()
            con.close()
            raise RuntimeError("浏览器炸了（模拟单条地址异常）")
        con.execute("UPDATE addresses SET status='done' WHERE address=?", (address,))
        con.commit()
        con.close()
        return True, {"people": 1, "phones": 1, "traffic_kb": 1}

    eng_tps.process_address = fake_boom
    try:
        win.concurrency_input.setValue(2)
        win.start()
        wait_finished(win, 60)
        pump(win, 0.4)
        c = status_counts()
        check("T5 一条地址抛异常，剩下的照跑（本轮派满 3 条）", len(hits) == 3,
              f"实测派发 {len(hits)}/3")
        check("T5 炸那条不许永远留在运行中（当场落 failed/pending）",
              c.get("running", 0) == 0, c)
    finally:
        eng_tps.process_address = real_proc
        close_win(win)

    # ---------------------------------------------------------------- T6 并发车道号
    win = make_win(app_tps, 12, tag="lane")
    spans = []
    lock = threading.Lock()
    inflight = [0]
    max_over = [0]
    lanes_inflight = []
    lane_collide = [0]

    def fake_lane(address, proxy, pw, key, worker_id=0, progress_cb=None, **kw):
        with lock:
            inflight[0] += 1
            max_over[0] = max(max_over[0], inflight[0])
            lanes_inflight.append(worker_id)
            if len(set(lanes_inflight[-1:])) == 0:
                lane_collide[0] += 1
            # 同一条刻不能有两个在跑的任务挂同一个车道号
            cur = dict()
            for a, w in zip(fake_lane.addr_hist, fake_lane.lane_hist):
                cur[a] = w
        time.sleep(0.05)
        with lock:
            inflight[0] -= 1
            try:
                lanes_inflight.remove(worker_id)
            except ValueError:
                pass
        con = sqlite3.connect(str(DB))
        con.execute("UPDATE addresses SET status='done' WHERE address=?", (address,))
        con.commit()
        con.close()
        return True, {"people": 1, "phones": 1, "traffic_kb": 1}

    fake_lane.addr_hist = []
    fake_lane.lane_hist = []

    def fake_lane_wrap(address, proxy, pw, key, worker_id=0, progress_cb=None, **kw):
        with lock:
            fake_lane.addr_hist.append(address)
            fake_lane.lane_hist.append(worker_id)
        return fake_lane(address, proxy, pw, key, worker_id=worker_id,
                         progress_cb=progress_cb, **kw)

    eng_tps.process_address = fake_lane_wrap
    try:
        win.concurrency_input.setValue(4)
        win.start()
        wait_finished(win, 90)
        pump(win, 0.3)
        used = sorted(set(fake_lane.lane_hist))
        check("T6 并发确实是多车道同时在跑", max_over[0] >= 4, f"实测同时在跑最多 {max_over[0]} 个（设的并发=4）")
        check("T6 车道号按车道走（0..并发-1），不再一条地址一个号",
              len(used) <= 4 and max(used) <= 3 if used else False,
              f"用到的车道号 {used[:12]}（地址 12 条）")
    finally:
        eng_tps.process_address = real_proc
        close_win(win)

    # ---------------------------------------------------------------- T7 出口IP 模块与挂点
    have_mod = (HERE / "egress_ip.py").exists()
    egress = None
    if have_mod:
        try:
            import egress_ip as egress
        except Exception as e:
            check("T7 egress_ip 模块在且能 import", False, f"{type(e).__name__}: {e}")
    else:
        check("T7 egress_ip 模块在且能 import", False, f"文件不在：{HERE / 'egress_ip.py'}")
    if egress:
        check("T7 egress_ip 模块在且能 import", True, "标准库实现")
        fp = FakePage()
        real_local, real_geo = egress.local_ip, egress.geo
        egress.local_ip = lambda: "203.0.113.9"          # 本机出口假值：一个真请求都不发
        egress.geo = lambda ip, *a, **k: "美国 加州 洛杉矶（测试桩）"
        try:
            msg = egress.probe_sync(fp, "sid_test", "")
        finally:
            egress.local_ip, egress.geo = real_local, real_geo
        check("T7 一条车道的出口行写得出来（带 IP）", bool(msg) and ("198.51.100.77" in str(msg) or "没读到" in str(msg)),
              str(msg)[:80])
        src_tps = (HERE / "engine_tps182.py").read_text(encoding="utf-8")
        src_fps = (HERE / "engine_fps.py").read_text(encoding="utf-8")
        check("T7 TPS 核心每车道报出口", "egress.probe_sync" in src_tps, "")
        check("T7 FPS 核心每车道报出口", "probe_async" in src_fps or "_log_egress" in src_fps, "")

    # ---------------------------------------------------------------- T8 解码请求
    run_t8(app_tps, eng_tps)

    bad = [r for r in RESULTS if not r[0]]
    print(f"\n合计 {len(RESULTS)} 项｜过 {len(RESULTS) - len(bad)}｜不过 {len(bad)}")
    for _, n, d in bad:
        print(f"   ✗ {n}｜{d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
