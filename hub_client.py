# -*- coding: utf-8 -*-
"""工人端：抓取器程序里那一层「往中心报」的代码。只标准库，且默认关掉
（settings.hub_on='0' 时一个包都不发、一个线程都不起）。

三个勾，各管各的（11-18 定：他说「如果不要控制呢？我只是想随时监控这些电脑」）：
  · 连到中心 hub_on      —— 开了才有心跳＋日志，这两样就是「随时监控」要的东西；
  · 交号码 hub_upload    —— 默认 0：连了中心也一行号码都不发，只报「几个人/几个号」这些数字；
  · 允许控制 hub_ctl     —— 默认 0：中心点开始/暂停/分配任务，这台机器只回一句「没开」，不动手。
                            对面中心也得勾「允许远程控制」，两头都开才真执行（谁都能单方面关掉）。

三条通道，都走同一个后台线程，抓取线程里绝不做网络：
  · 日志：界面每写一行屏上日志 → offer() 进队列（有上限，满了丢最旧的并计数），
    线程每 2 秒成批推走（中心按 机器+seq 去重，重推不会变两条；seq 落库，重启也不断号）。
  · 数据：每 15 秒读一次本地库 results 里 id 比上次大的行（只读连接，1 秒拿不到就跳过），
    按 uid=机器:rowid 推上去 ⇒ 断网期间攒着，回头一次性补，中心再按号码去重。
  · 指令：心跳（30 秒）的响应里带回面板点的按钮，塞给界面主线程去执行，执行完 ack。

所有网络异常一律吞掉只留一行本地日志：中心挂了、IP 改了、没开机，
都不该影响这台机器继续抓号。
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"
POLL_S = 2.0            # 主循环间隔（日志成批推 / 取指令）
HB_S = 30.0             # 心跳间隔（面板上「多少秒前」就按这个走）
ROW_S = 15.0            # 数据同步间隔
LOG_BATCH = 200
ROW_BATCH = 500         # 一趟最多推 500 行
ROW_CATCHUP = 20        # 补传时最多连推 20 趟 = 1 万行
LOG_QUEUE_MAX = 4000    # 队列上限：中心长时间不在线也不能把内存吃掉
NET_TIMEOUT = 4.0
FAIL_LOG_EVERY = 10     # 连续失败每这么多轮才在本地日志说一句，别刷屏

# 中心在局域网里，绝对不能用系统代理出去（挂了代理这台机器的出口就走代理了）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def machine_name():
    return (os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC").strip()[:40]


class Hub:
    """一个实例 = 这台程序到中心的连接。界面拿一个实例就够。"""

    def __init__(self, get_setting, set_setting, log, status_fn, run_cmd_fn):
        """get_setting/set_setting：读写 settings 表；log(msg)：写界面日志（会自己进队列，
        所以本模块自己发的话用 note()，别用 log()，免得绕圈）；
        status_fn() -> dict：当前状态（核心/并发/车道/人数/号码数/内存/出口分布…）；
        run_cmd_fn(cmd, arg) -> (ok, msg)：在界面主线程执行面板指令。"""
        self.get_setting = get_setting
        self.set_setting = set_setting
        self.log = log
        self.status_fn = status_fn
        self.run_cmd_fn = run_cmd_fn
        self.q: queue.Queue = queue.Queue(maxsize=LOG_QUEUE_MAX)
        self.seq = int(self.get_setting("hub_seq", "0") or 0)
        self.last_rid = int(self.get_setting("hub_last_rid", "0") or 0)
        self.started = False
        self._th = None
        self.stop_flag = threading.Event()
        self.sent_lines = 0
        self.dropped = 0
        self.sent_rows = 0
        self.fails = 0
        self.note_lines: list[str] = []
        # 日志这一路只准一个人同时往外推：关窗口时界面线程也想补一脚（finish），
        # 两边一起数 seq 会撞号 ⇒ 中心按 (机器,seq) 去重就会吞掉一行、屏上少一段。
        self._flush_lock = threading.Lock()

    # ---------- 开关与地址 ----------
    def enabled(self):
        return (str(self.get_setting("hub_on", "0")) == "1"
                and bool(str(self.get_setting("hub_url", "")).strip()))

    def url(self):
        return str(self.get_setting("hub_url", "")).strip().rstrip("/")

    def token(self):
        return "".join(ch for ch in str(self.get_setting("hub_token", "")) if 33 <= ord(ch) < 127)

    def upload_rows(self):
        """「连到中心」≠「把号码交上去」。只想监控时一个号码都不发（默认关）。"""
        return str(self.get_setting("hub_upload", "0")) == "1"

    def allow_control(self):
        """本机是否听中心下发的按钮。关着时收到指令只回一句「本机没开」，绝不动手。"""
        return str(self.get_setting("hub_ctl", "0")) == "1"

    def note(self, msg):
        """自己想在本机日志留一句话：攒着，下一轮循环再交给界面（不在网络线程里直接摸 Qt）。"""
        self.note_lines.append(msg[:200])

    # ---------- 界面调用这两个 ----------
    def offer(self, line):
        """界面每行日志调一次。绝不阻塞：满就丢最旧的。"""
        if not self.started or not self.enabled():
            return
        try:
            self.q.put_nowait(line)
        except queue.Full:
            try:
                self.q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(line)
            except queue.Full:
                self.dropped += 1

    def start(self):
        """可以反复调：线程还活着就不管；停过之后勾回来也能重新起一条（界面里
        「连到中心」这个勾就是在调它，不该要求重启程序）。"""
        if self._th and self._th.is_alive():
            return
        self.started = True
        self.stop_flag.clear()
        self._th = threading.Thread(target=self._loop, daemon=True, name="hub-client")
        self._th.start()

    def alive(self):
        return bool(self._th and self._th.is_alive())

    def ping(self):
        """界面「测试一下」：马上发一次心跳，把结果翻成人话报给他。
        故意不看 hub_on 那个勾 —— 没勾上也能先把地址、令牌试通，免得勾了才发现填错。
        中心顺带下发的指令照原路执行（不接住就等于把那几条弄丢了）。"""
        if not self.url():
            return False, "中心地址是空的，先填 http://那台电脑的局域网IP:8760"
        st = self._status_safe()
        try:
            out = self._post("/api/hb", st)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, f"中心回了 401：令牌不对（{self.url()}）"
            return False, f"中心回了 {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            why = getattr(exc, "reason", None) or exc
            return False, (f"{self.url()} 连不上：{why}"
                           "（中心那台的黑窗口开着吗？第一次启动要在防火墙弹窗上勾"
                           "「专用网络 → 允许访问」，不然别台进不来）")[:200]
        except ValueError:
            return False, "中心回的不是 JSON：那个地址多半填成了别的服务"
        if not out.get("ok"):
            return False, str(out.get("msg") or "中心拒了")[:160]
        for c in out.get("cmds") or []:
            self._run_cmd(c)
        return True, f"{self.url()} 通了｜这台叫 {machine_name()}｜心跳已记进中心"

    def finish(self, grace=0.8, timeout=1.0):
        """关程序时给屏上最后几行日志一次机会：让循环线程先收手，再由这里补推一把。
        全程限时（默认 0.8+1.0 秒），推不掉就算了 —— 中心那边 95 秒没心跳自己就报掉线，
        绝不能因为这一层把关窗口的动作卡住。"""
        self.stop_flag.set()
        th = self._th
        if th is not None and th.is_alive():
            th.join(grace)
        if not self.started or not self.enabled() or self.fails or self.q.empty():
            return
        self._flush_logs(timeout=timeout)

    # ---------- 网络 ----------
    def _post(self, path, obj, timeout=NET_TIMEOUT):
        req = urllib.request.Request(
            self.url() + path, data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Hub-Token": self.token()})
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def _get(self, path, timeout=NET_TIMEOUT):
        sep = "&" if "?" in path else "?"
        req = urllib.request.Request(self.url() + path + sep + "token=" + urllib.parse.quote(self.token()))
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def _safe(self, name, fn):
        try:
            out = fn()
            self.fails = 0
            return out
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            self.fails += 1
            if self.fails == 1 or self.fails % FAIL_LOG_EVERY == 0:
                self.note(f"中心没连上（{type(exc).__name__}），本机照跑不误：{self.url()}"
                          f"｜连续第 {self.fails} 轮")
            return None

    # ---------- 主循环 ----------
    def _loop(self):
        next_hb = next_rows = 0.0
        while not self.stop_flag.is_set():
            t0 = time.time()
            if not self.enabled():
                # 关了就把队列倒空，别攒着；seq 不推就等于没发过
                while not self.q.empty():
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        break
                self.stop_flag.wait(1.0)
                continue
            self._flush_logs()
            if t0 >= next_rows:
                next_rows = t0 + ROW_S
                self._push_rows()                 # 里面第一件事就是看那个勾，没开直接返回
            if t0 >= next_hb:
                next_hb = t0 + HB_S
                self._heartbeat()
            while self.note_lines:
                self.log(self.note_lines.pop(0))
            self.stop_flag.wait(max(0.2, POLL_S - (time.time() - t0)))

    def _flush_logs(self, timeout=NET_TIMEOUT):
        if not self._flush_lock.acquire(blocking=False):
            return                      # 那一条线程正在推：这轮算了，绝不抢同一串 seq
        try:
            self._flush(timeout)
        finally:
            self._flush_lock.release()

    def _flush(self, timeout=NET_TIMEOUT):
        items = []
        while len(items) < LOG_BATCH:
            try:
                items.append({"seq": self.seq + len(items) + 1, "line": self.q.get_nowait()})
            except queue.Empty:
                break
        if not items:
            return
        out = self._safe("logs", lambda: self._post("/api/logs", {
            "machine": machine_name(),
            "items": [{"seq": it["seq"], "ts": int(time.time()), "line": it["line"]} for it in items]},
            timeout=timeout))
        if out and out.get("ok"):
            self.seq += len(items)
            self.set_setting("hub_seq", self.seq)     # 序号落库：重启后中心也不会当成重复
            self.sent_lines += len(items)
        else:
            # 没送出去：原样放回队首（顺序不能乱，不然日志看着前后颠倒），序号不推进，
            # 中心那边就不会有洞，回头补传时 seq 还是从这几条接着数。
            # 队列有上限：先把没发走的这几行倒出来，再整团塞回去，塞不下就丢最旧的，
            # 绝不能在后台线程里 put() 干等（那会把心跳和上传一起堵死）。
            rest = []
            while True:
                try:
                    rest.append(self.q.get_nowait())
                except queue.Empty:
                    break
            for line in [it["line"] for it in items] + rest:
                try:
                    self.q.put_nowait(line)
                except queue.Full:
                    self.dropped += 1

    def _status_safe(self):
        """界面报状态那个函数坏也不能把这条线程带下去：坏了自己填一句原因，
        中心面板上就写「status()XxxError」，比这台干脆从面板上消失好查得多。"""
        try:
            st = self.status_fn() or {}
        except Exception as exc:
            st = {"state": "unknown", "reason": f"status(){type(exc).__name__}"}
        return dict(st, machine=machine_name(), ver=VERSION, hb_client=VERSION,
                    # 这台自己那两个勾也报上去：面板上要能看出「这台听了也不动手」，
                    # 不然他点了按钮没反应，只能猜是哪一头没开。
                    upload=1 if self.upload_rows() else 0, ctl=1 if self.allow_control() else 0)

    def _heartbeat(self):
        st = self._status_safe()
        out = self._safe("hb", lambda: self._post("/api/hb", st))
        if not out:
            return
        for c in out.get("cmds") or []:
            self._run_cmd(c)

    def _run_cmd(self, c):
        cmd, arg, cid = str(c.get("cmd", "")), str(c.get("arg", "")), c.get("id")
        ok, msg = False, "界面没接执行口"
        if not self.allow_control():
            # 中心那边勾了「允许控制」也没用：这台机器自己没同意，一个按钮都不碰
            msg = "这台电脑没勾「允许中心控制」，指令没执行（只监控照旧）"
        elif cmd == "import":
            ok, msg = self._take_and_import(arg)
        else:
            try:
                ok, msg = self.run_cmd_fn(cmd, arg)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"[:120]
        self.note(f"中心指令 {cmd}{' ' + arg if arg else ''} → {'ok' if ok else '没成'}｜{msg}")
        self._safe("ack", lambda: self._post("/api/cmd_ack", {
            "machine": machine_name(), "id": cid, "ok": 1 if ok else 0, "msg": str(msg)[:300]}))

    def _take_and_import(self, arg):
        """面板「分配任务」只往 cmds 里塞一个批次号（心跳那几十字节不带地址，
        一批几百条会把报文撑爆）。真正的地址在这里按机器名取回来：
        /api/task_get 一次只给这台的一批、取过就标记，不会串到别的机器。"""
        try:
            tid = int(str(arg).split(":")[0])
        except (TypeError, ValueError):
            tid = 0
        got = self._safe("task", lambda: self._get(
            "/api/task_get?machine=" + urllib.parse.quote(machine_name())))
        task = (got or {}).get("task") or {}
        addrs = [str(a) for a in (task.get("addrs") or []) if str(a).strip()]
        if not addrs:
            return False, f"中心说派了批次 {tid}，但这台没取到地址（可能已经被取走了）"
        payload = json.dumps({"task": task.get("task", tid), "addrs": addrs}, ensure_ascii=False)
        try:
            ok, msg = self.run_cmd_fn("import", payload)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:120]
        if ok:
            # 回执只改中心那条批次上的一行备注（面板显示「已落进队列」），不动数据
            self._safe("task_done", lambda: self._post(
                "/api/task_done", {"machine": machine_name(),
                                   "task": task.get("task", tid), "msg": str(msg)[:120]}))
        return ok, msg

    def _push_rows(self):
        """门就放在这个函数里（不是放在调用方）：谁调都一样 ——
        「连到中心」≠「交号码」，那个勾没打上就一行都不读、一个包都不发。
        读的是本地库「上次推过之后」的新行：只读连接 + 短超时，抢不过抓取线程就这轮算了。"""
        if not self.upload_rows():
            return False
        got_any = False
        for _ in range(ROW_CATCHUP):
            batch = self._read_new_rows()
            if batch is None:
                return None if not got_any else True
            if not batch:
                break
            got_any = True
            out = self._safe("rows", lambda: self._post("/api/rows", {
                "machine": machine_name(), "items": batch}))
            if not (out and out.get("ok")):
                return None if not got_any else True
            self.sent_rows += out.get("stored", 0)
            self.last_rid = batch[-1]["rid"]
            self.set_setting("hub_last_rid", self.last_rid)
            if out.get("fresh"):
                self.note(f"上传 {len(batch)} 行 → 中心说净新号 {out['fresh']} 个")
            if len(batch) < ROW_BATCH:
                break
        return got_any

    def _read_new_rows(self):
        import sqlite3
        try:
            import engine_tps182 as tps
            from engine_core import clean_phone, gender_bucket
            db = str(tps.DB_PATH)
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            con.row_factory = sqlite3.Row
        except Exception as exc:
            self.note(f"读本地库失败：{type(exc).__name__}")
            return None
        try:
            rows = con.execute(
                "SELECT id,address,name,age,location,phones,created_at FROM results "
                "WHERE id>? ORDER BY id LIMIT ?", (self.last_rid, ROW_BATCH)).fetchall()
        except sqlite3.Error:
            return None
        finally:
            con.close()
        out = []
        for r in rows:
            phones = [p for p in str(r["phones"] or "").split(",") if p.strip()]
            out.append({"rid": r["id"],
                        "phone": clean_phone(phones[0]) if phones else "",
                        "name": r["name"], "age": r["age"], "location": r["location"],
                        "address": r["address"], "created_at": r["created_at"],
                        "gender": {"male": "男", "female": "女", "andy": "中性",
                                   "unknown": "未知"}.get(gender_bucket(r["name"]), "未知")})
        return out

    def stop(self):
        self.stop_flag.set()
