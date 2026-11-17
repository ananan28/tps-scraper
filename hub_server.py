# -*- coding: utf-8 -*-
"""中心端（跑在他一台常开的 Windows 上，双击 hub.exe 就行）：管若干台抓号机器。

一台机器 = 一个「工人」。工人主动往外连这里（所以不用公网 IP、不用配域名、不用动路由器）：
  POST /api/hb     心跳：状态/核心/并发/车道/人数号码数/内存/出口IP分布/版本
  POST /api/logs   日志增量（按 machine+seq 去重，重复推不炸库）
  POST /api/rows   号码行增量（按 machine+rowid 去重；中心再按号码全局去重算净新增）
  POST /api/cmd    面板点按钮（开始/暂停/恢复/停止/改并发/导出/清数据…）⇒ 排队等心跳带走
  POST /api/cmd_ack 工人回报执行结果
  GET  /api/task_get 工人按机器名取属于它的那批地址（一次一批，取过就标记，不串台）
  POST /api/task_done 工人导入完回执（面板才知道那批真落进了队列）
  GET  /api/state  面板用的总快照；GET /  控制面板（手机浏览器也能开）

以上除面板页本身，每一个都要令牌（11-18 复核：task_get 原先漏了这道门，局域网里
别人能白拿整批地址，已补）。

只有标准库。安全口径：默认只在内网监听 + 全接口要令牌；令牌没配就随机生成一个打在屏上，
绝不接受匿名连，也不做「公网直接暴露」（那等于把几百万条号码库挂到全网扫描里）。

默认「只监控」：面板只出机器卡片＋日志流，开始/暂停/分配任务那一整套控制
（/api/cmd、/api/dispatch、心跳带回指令）全部关着，要的人在面板右上角勾一下。
工人端另有自己那两个勾（交号码 / 允许控制），两头都开才真动手 —— 谁都能单方面关掉。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB = BASE / "hub_data.db"
PORT_DEFAULT = 8760
HB_STALE_S = 95          # 心跳超过这个秒数 = 这台机器掉线（30 秒一跳，跳失 3 次）
STALL_S = 900            # 标着「跑批中」但产量 15 分钟没动 ⇒ 事件提醒
LOG_KEEP_PER_MACHINE = 6000
CMD_TIMEOUT_S = 120      # 指令下发后多久没回执算超时（面板上标红）

MACHINE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{1,40}$")


# ---------------------------------------------------------------- 库
def connect():
    con = sqlite3.connect(str(DB), timeout=20, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


LOCK = threading.Lock()
CON = None


def db():
    global CON
    if CON is None:
        CON = connect()
        init_db(CON)
    return CON


def init_db(con):
    ex = con.execute
    ex("""CREATE TABLE IF NOT EXISTS conf(key TEXT PRIMARY KEY, value TEXT)""")
    ex("""CREATE TABLE IF NOT EXISTS machines(
            machine TEXT PRIMARY KEY,
            ver TEXT, core TEXT, state TEXT, reason TEXT,
            lanes INTEGER, concurrency INTEGER,
            people INTEGER, phones INTEGER, done INTEGER, pending INTEGER, failed INTEGER,
            mem_mb REAL, egress TEXT, ip TEXT,
            upload INTEGER, ctl INTEGER,
            hb_at INTEGER, prod_at INTEGER, prod_people INTEGER, rows_at INTEGER)""")
    # 老库补列：中心已经在跑的那些台不该因为升级丢心跳表（CREATE TABLE 对已存在的表不动）
    have = {r["name"] for r in con.execute("PRAGMA table_info(machines)").fetchall()}
    for col in ("upload", "ctl"):
        if col not in have:
            con.execute(f"ALTER TABLE machines ADD COLUMN {col} INTEGER")
    ex("""CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine TEXT, seq INTEGER, ts INTEGER, line TEXT)""")
    ex("CREATE UNIQUE INDEX IF NOT EXISTS ux_logs ON logs(machine,seq)")
    ex("CREATE INDEX IF NOT EXISTS ix_logs_ts ON logs(ts DESC)")
    # 号码行：uid=机器+本地 rowid（工人重推天然幂等）；phone 单独一张表算全局净新增
    ex("""CREATE TABLE IF NOT EXISTS rows(
            uid TEXT PRIMARY KEY,
            machine TEXT, phone TEXT, name TEXT, gender TEXT, age TEXT,
            location TEXT, address TEXT, created_at TEXT, got_at INTEGER)""")
    ex("CREATE INDEX IF NOT EXISTS ix_rows_phone ON rows(phone)")
    ex("""CREATE TABLE IF NOT EXISTS phones(
            phone TEXT PRIMARY KEY, machine TEXT, first_at INTEGER)""")
    ex("""CREATE TABLE IF NOT EXISTS cmds(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine TEXT, cmd TEXT, arg TEXT, made_at INTEGER,
            sent_at INTEGER, ack_ok INTEGER, ack_msg TEXT)""")
    ex("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine TEXT, kind TEXT, msg TEXT, ts INTEGER)""")
    # 派发的地址批次：面板上粘一批 → 切成 task → 工人用 /api/task_get 取回去 import
    ex("""CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine TEXT, n INTEGER, made_at INTEGER, got_at INTEGER, note TEXT)""")
    ex("""CREATE TABLE IF NOT EXISTS task_items(
            task INTEGER, addr TEXT, UNIQUE(task, addr))""")
    # 防重复派发：同一条地址默认只派给一台（除非面板勾「允许多机重复」）
    ex("""CREATE TABLE IF NOT EXISTS assigned(
            addr TEXT PRIMARY KEY, machine TEXT, task INTEGER, made_at INTEGER)""")
    con.commit()


def conf_get(key, default=""):
    row = db().execute("SELECT value FROM conf WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def conf_set(key, value):
    with LOCK:
        db().execute("INSERT OR REPLACE INTO conf(key,value) VALUES(?,?)", (key, str(value)))
        db().commit()


def now():
    return int(time.time())


def stamp():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- 令牌
def control_on():
    """中心默认「只监控」：面板只有机器卡片＋日志，一个按钮都不给，
    /api/cmd、/api/dispatch 一律拒，心跳也不带回指令。
    想要「在这台电脑上点开始/暂停/分配任务」，面板右上角勾一下才开
    （勾一次记在库里，重启还是关的 —— 免得哪天忘了它开着）。"""
    return conf_get("control", "0") == "1"


def clean_token(t):
    """HTTP 头只能 latin-1，令牌给成中文会一路 UnicodeEncodeError（自检真撞过），
    所以这里只留 ASCII 可见字符；全被剔光就重新随机生成。"""
    t = "".join(ch for ch in str(t or "") if 33 <= ord(ch) < 127)
    return t or secrets.token_urlsafe(9)


def token():
    t = conf_get("token", "")
    if not t:
        t = clean_token("")
        conf_set("token", t)
    return t


def lan_ip():
    """本机在局域网里的那个 IP（给工人填地址用）；没网就回 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 业务
def upsert_hb(d):
    m = str(d.get("machine", "")).strip()
    if not MACHINE_NAME_RE.match(m):
        return f"机器名不合法：{m!r}"
    with LOCK:
        con = db()
        con.execute("""INSERT INTO machines(machine,ver,core,state,reason,lanes,concurrency,
                        people,phones,done,pending,failed,mem_mb,egress,ip,upload,ctl,
                        hb_at,prod_at,prod_people,rows_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(machine) DO UPDATE SET
                         ver=excluded.ver, core=excluded.core, state=excluded.state,
                         reason=excluded.reason, lanes=excluded.lanes,
                         concurrency=excluded.concurrency, people=excluded.people,
                         phones=excluded.phones, done=excluded.done, pending=excluded.pending,
                         failed=excluded.failed, mem_mb=excluded.mem_mb, egress=excluded.egress,
                         ip=excluded.ip, upload=excluded.upload, ctl=excluded.ctl,
                         hb_at=excluded.hb_at""",
                    (m, str(d.get("ver", ""))[:40], str(d.get("core", ""))[:12],
                     str(d.get("state", "idle"))[:12], str(d.get("reason", ""))[:120],
                     int(d.get("lanes") or 0), int(d.get("concurrency") or 0),
                     int(d.get("people") or 0), int(d.get("phones") or 0),
                     int(d.get("done") or 0), int(d.get("pending") or 0), int(d.get("failed") or 0),
                     float(d.get("mem_mb") or 0), json.dumps(d.get("egress") or {}, ensure_ascii=False),
                     str(d.get("ip", ""))[:40],
                     1 if d.get("upload") else 0, 1 if d.get("ctl") else 0,
                     now(), now(), int(d.get("people") or 0),
                     int(d.get("rows_at") or 0)))
        # 「跑批中但产量不动」判一次（只在跨过阈值时留一条，不刷屏）
        if str(d.get("state")) == "running":
            row = con.execute("SELECT prod_at,prod_people FROM machines WHERE machine=?", (m,)).fetchone()
            if row and row["prod_people"] and int(d.get("people") or 0) <= row["prod_people"] \
                    and now() - int(row["prod_at"] or 0) > STALL_S:
                last = con.execute("SELECT ts FROM events WHERE machine=? AND kind='停滞' "
                                   "ORDER BY id DESC LIMIT 1", (m,)).fetchone()
                if not last or now() - last["ts"] > STALL_S:
                    con.execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                                (m, "停滞", f"标着跑批中，产量 {STALL_S // 60} 分钟没动", now()))
        elif d.get("people"):
            con.execute("UPDATE machines SET prod_at=?,prod_people=? WHERE machine=?",
                        (now(), int(d["people"]), m))
        con.commit()
    return ""


def put_logs(machine, items):
    n = 0
    with LOCK:
        con = db()
        for it in items:
            try:
                cur = con.execute("INSERT OR IGNORE INTO logs(machine,seq,ts,line) VALUES(?,?,?,?)",
                                  (machine, int(it["seq"]), int(it.get("ts") or now()),
                                   str(it.get("line", ""))[:600]))
                n += cur.rowcount or 0
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                continue
        con.commit()
        con.execute("""DELETE FROM logs WHERE machine=? AND id NOT IN
                       (SELECT id FROM logs WHERE machine=? ORDER BY id DESC LIMIT ?)""",
                    (machine, machine, LOG_KEEP_PER_MACHINE))
        con.commit()
    return n


def put_rows(machine, items):
    """工人推的号码行：uid 幂等；新号码进 phones ⇒ 能算跨机净新增。"""
    got = 0
    fresh = 0
    with LOCK:
        con = db()
        for it in items:
            phone = re.sub(r"\D", "", str(it.get("phone") or ""))
            uid = f"{machine}:{it.get('rid')}"
            cur = con.execute("""INSERT OR IGNORE INTO rows(uid,machine,phone,name,gender,age,
                                 location,address,created_at,got_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                              (uid, machine, phone, str(it.get("name", ""))[:120],
                               str(it.get("gender", ""))[:8], str(it.get("age", ""))[:8],
                               str(it.get("location", ""))[:160], str(it.get("address", ""))[:200],
                               str(it.get("created_at", ""))[:32], now()))
            if not (cur.rowcount or 0):
                continue
            got += 1
            if phone:
                c2 = con.execute("INSERT OR IGNORE INTO phones(phone,machine,first_at) VALUES(?,?,?)",
                                 (phone, machine, now()))
                fresh += c2.rowcount or 0
        con.commit()
        con.execute("UPDATE machines SET rows_at=? WHERE machine=?", (now(), machine))
        con.commit()
    return got, fresh


CMDS = ("start", "pause", "resume", "stop", "set_concurrency", "export", "retry_failed",
        "delete_done", "clear_exported", "import", "set_core", "set_age", "set_egress_log")


def _add_cmd(con, machine, cmd, arg=""):
    """不带锁的内部版：dispatch 自己已经持锁了，再调 add_cmd 会撞死在非重入锁上
    （11-18 自检实测：/api/dispatch 直接挂住、客户端 6 秒超时）。"""
    if cmd not in CMDS:
        return ""
    cur = con.execute("INSERT INTO cmds(machine,cmd,arg,made_at) VALUES(?,?,?,?)",
                      (machine, cmd, str(arg)[:40], now()))
    return str(cur.lastrowid)


def add_cmd(machine, cmd, arg=""):
    if cmd not in CMDS:
        return ""
    with LOCK:
        cid = _add_cmd(db(), machine, cmd, arg)
        db().commit()
        return cid


def online_machines(mins=HB_STALE_S // 60 + 1):
    rows = db().execute("SELECT machine FROM machines WHERE hb_at>=? ORDER BY machine",
                        (now() - HB_STALE_S,)).fetchall()
    return [r["machine"] for r in rows]


def dispatch(text, machine, allow_dup=False):
    """面板「分配任务」：粘一批地址，派给一台，或 machine='*' 平均分给在线的几台。
    返回 (每台派了几条, 因「已派过」跳过的条数, 这批里本身重复的行数) ——
    三个数加起来必须等于他粘进来的行数，少一行都要说清去哪了。"""
    addrs, seen, in_dup = [], set(), 0
    for line in str(text).splitlines():
        a = line.strip().strip('"').strip()
        if not a:
            continue
        if a in seen:
            in_dup += 1
            continue
        seen.add(a)
        addrs.append(a[:300])
    if not addrs:
        return {}, 0, in_dup
    targets = [machine] if machine != "*" else online_machines()
    if not targets:
        return {}, len(addrs), in_dup
    per = {t: 0 for t in targets}
    with LOCK:
        con = db()
        buckets = {t: [] for t in targets}
        skipped = 0
        for i, a in enumerate(addrs):
            if not allow_dup and con.execute("SELECT 1 FROM assigned WHERE addr=?", (a,)).fetchone():
                skipped += 1
                continue
            t = targets[i % len(targets)]          # 轮转 = 平均分给几台
            buckets[t].append(a)
            con.execute("INSERT OR IGNORE INTO assigned(addr,machine,task,made_at) VALUES(?,?,?,?)",
                        (a, t, 0, now()))
            per[t] += 1
        for t, lst in buckets.items():
            if not lst:
                continue
            cur = con.execute("INSERT INTO tasks(machine,n,made_at) VALUES(?,?,?)", (t, len(lst), now()))
            tid = cur.lastrowid
            con.executemany("INSERT OR IGNORE INTO task_items(task,addr) VALUES(?,?)",
                            [(tid, a) for a in lst])
            con.execute("UPDATE assigned SET task=? WHERE machine=? AND task=0 AND addr IN "
                        "(SELECT addr FROM task_items WHERE task=?)", (tid, t, tid))
            cid = _add_cmd(con, t, "import", tid)
            con.execute("UPDATE cmds SET arg=? WHERE id=?", (f"{tid}:{len(lst)}", int(cid)))
        con.commit()
    return per, skipped, in_dup


def take_task(machine):
    """工人来取属于它的最早一批地址。"""
    with LOCK:
        con = db()
        r = con.execute("""SELECT id,n FROM tasks WHERE machine=? AND got_at IS NULL
                           ORDER BY id LIMIT 1""", (machine,)).fetchone()
        if not r:
            return None
        items = [x[0] for x in con.execute(
            "SELECT addr FROM task_items WHERE task=? ORDER BY rowid", (r["id"],)).fetchall()]
        con.execute("UPDATE tasks SET got_at=? WHERE id=?", (now(), r["id"]))
        con.commit()
    return {"task": r["id"], "addrs": items}


def take_cmds(machine):
    with LOCK:
        con = db()
        rows = con.execute("""SELECT id,cmd,arg FROM cmds WHERE machine=? AND sent_at IS NULL
                              ORDER BY id""", (machine,)).fetchall()
        for r in rows:
            con.execute("UPDATE cmds SET sent_at=? WHERE id=?", (now(), r["id"]))
        con.commit()
    return [{"id": r["id"], "cmd": r["cmd"], "arg": r["arg"]} for r in rows]


def ack_cmd(cid, ok, msg):
    with LOCK:
        db().execute("UPDATE cmds SET ack_ok=?,ack_msg=? WHERE id=?", (1 if ok else 0, str(msg)[:300], int(cid)))
        db().commit()


def snapshot():
    con = db()
    t = now()
    ms = []
    for r in con.execute("SELECT * FROM machines ORDER BY machine").fetchall():
        ago = t - int(r["hb_at"] or 0)
        try:
            eg = json.loads(r["egress"] or "{}")
        except json.JSONDecodeError:
            eg = {}
        ms.append({**{k: r[k] for k in r.keys()}, "ago": ago, "online": ago <= HB_STALE_S,
                   "egress": eg})
    pend = {row["machine"]: row["c"] for row in con.execute(
        "SELECT machine,COUNT(*) c FROM cmds WHERE ack_ok IS NULL GROUP BY machine")}
    logs = [{"machine": r["machine"], "ts": r["ts"], "line": r["line"]}
            for r in con.execute("SELECT machine,ts,line FROM logs ORDER BY id DESC LIMIT 400")]
    events = [{"machine": r["machine"], "kind": r["kind"], "msg": r["msg"], "ts": r["ts"]}
              for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 40")]
    tot = con.execute("SELECT COUNT(*) c, COUNT(DISTINCT phone) p FROM rows").fetchone()
    per = {r["machine"]: {"rows": r["c"], "phones": r["p"]} for r in con.execute(
        "SELECT machine,COUNT(*) c,COUNT(DISTINCT phone) p FROM rows GROUP BY machine")}
    dup = int(tot["c"] or 0) - int(tot["p"] or 0)
    tasks = {r["machine"]: {"批": r["b"], "条": r["n"], "未取": r["w"]} for r in con.execute(
        """SELECT machine,COUNT(*) b,SUM(n) n,SUM(CASE WHEN got_at IS NULL THEN n ELSE 0 END) w
           FROM tasks GROUP BY machine""").fetchall()}
    assigned = int(con.execute("SELECT COUNT(*) FROM assigned").fetchone()[0] or 0)
    return {"machines": ms, "pending_cmds": pend, "logs": logs, "events": events,
            "totals": {"rows": int(tot["c"] or 0), "phones": int(tot["p"] or 0), "dup": max(dup, 0)},
            "per_machine_rows": per, "tasks": tasks, "assigned": assigned,
            "control": 1 if control_on() else 0,
            "online": online_machines(),
            "server_time": t, "port": int(conf_get("port", PORT_DEFAULT))}


def merged_csv():
    """跨机合并去重后的一人一行表（同机同地址同名同岁只留首个常用号）。"""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["姓名", "性别", "年龄", "居住地", "电话", "查询地址", "采集时间", "来自机器"])
    con = db()
    seen = set()
    n = 0
    for r in con.execute("""SELECT phone,name,gender,age,location,address,created_at,machine
                            FROM rows WHERE phone<>'' ORDER BY got_at DESC""").fetchall():
        if r["phone"] in seen:
            continue
        seen.add(r["phone"])
        w.writerow([r["name"], r["gender"], r["age"], r["location"], r["phone"],
                    r["address"], r["created_at"], r["machine"]])
        n += 1
    return buf.getvalue(), n


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "TPSHub/1.0"

    def log_message(self, fmt, *a):      # 别把每个请求打在控制台刷个没完
        return

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, **kw):
        self._json(200, {"ok": True, **kw})

    def _authed(self, q):
        got = self.headers.get("X-Hub-Token") or (q.get("token") or [""])[0]
        return got == token()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8_000_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/task_get":
            if not self._authed(q):
                return self._json(401, {"ok": False, "msg": "令牌不对"})
            m = (q.get("machine") or [""])[0][:40]
            if not m:
                return self._json(400, {"ok": False, "msg": "没带机器名"})
            return self._json(200, {"ok": True, "task": take_task(m)})
        if u.path == "/api/state":
            if not self._authed(q):
                return self._json(401, {"ok": False, "msg": "令牌不对"})
            return self._json(200, snapshot())
        if u.path == "/api/export":
            if not self._authed(q):
                return self._json(401, {"ok": False, "msg": "令牌不对"})
            text, n = merged_csv()
            body = ("\ufeff" + text).encode("utf-8")
            fn = f"合并去重_{datetime.now().strftime('%m%d_%H%M')}_{n}人.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(fn)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"ok": False, "msg": "没有这个接口"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._json(401, {"ok": False, "msg": "令牌不对"})
        try:
            d = self._body()
        except Exception as exc:
            return self._json(400, {"ok": False, "msg": f"报文不对：{type(exc).__name__}"})
        m = str(d.get("machine", "")).strip()[:40]
        if u.path in ("/api/cmd", "/api/dispatch") and not control_on():
            return self._json(403, {"ok": False,
                                    "msg": "中心现在是「只监控」，控制和分配任务都没开"
                                          "（面板右上角勾一下才允许下发）"})
        if u.path == "/api/hb":
            err = upsert_hb(d)
            if err:
                return self._json(400, {"ok": False, "msg": err})
            return self._ok(cmds=take_cmds(m) if control_on() else [])
        if u.path == "/api/control":              # 面板右上角那个勾
            on = "1" if d.get("on") else "0"
            conf_set("control", on)
            with LOCK:
                db().execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                             ("*", "控制开关", "已开启远程控制（面板上的按钮能用）" if on == "1"
                              else "已回到只监控（按钮藏起来，指令接口拒收）", now()))
                db().commit()
            return self._ok(control=on)
        if u.path == "/api/logs":
            return self._ok(stored=put_logs(m, d.get("items") or []))
        if u.path == "/api/rows":
            got, fresh = put_rows(m, d.get("items") or [])
            return self._ok(stored=got, fresh=fresh)
        if u.path == "/api/cmd_ack":
            ack_cmd(d.get("id", 0), d.get("ok"), d.get("msg", ""))
            return self._ok()
        if u.path == "/api/dispatch":              # 面板「分配任务」
            machine = str(d.get("machine") or "*").strip()[:40]
            per, skipped, in_dup = dispatch(d.get("text") or "", machine, bool(d.get("allow_dup")))
            if not any(per.values()):
                return self._json(400, {"ok": False, "msg": "一条都没派出去（没在线机器，或全是重复地址）",
                                        "skipped": skipped, "dup_in_batch": in_dup})
            with LOCK:
                db().execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                             (machine, "派发",
                              f"{'平均分给在线' if machine == '*' else machine}："
                              + "，".join(f"{k} {v} 条" for k, v in per.items() if v)
                              + (f"，跳过已派过 {skipped} 条" if skipped else "")
                              + (f"，这批里重复 {in_dup} 行" if in_dup else ""), now()))
                db().commit()
            return self._ok(per=per, skipped=skipped, dup_in_batch=in_dup)
        if u.path == "/api/task_done":             # 工人导入完回执，面板才显示「已落进队列」
            with LOCK:
                db().execute("UPDATE tasks SET note=? WHERE id=?",
                             (str(d.get("msg", ""))[:120], int(d.get("task") or 0)))
                db().commit()
            return self._ok()
        if u.path == "/api/cmd":                 # 面板点按钮走这里
            cid = add_cmd(m, str(d.get("cmd", "")), d.get("arg", ""))
            if not cid:
                return self._json(400, {"ok": False, "msg": "指令不认识"})
            with LOCK:
                db().execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                             (m, "指令", f"下发 {d.get('cmd')} {d.get('arg') or ''}".strip(), now()))
                db().commit()
            return self._ok(id=cid)
        if u.path == "/api/forget":
            with LOCK:
                con = db()
                con.execute("DELETE FROM machines WHERE machine=?", (m,))
                con.execute("DELETE FROM logs WHERE machine=?", (m,))
                con.execute("DELETE FROM cmds WHERE machine=?", (m,))
                con.commit()
            return self._ok()
        self._json(404, {"ok": False, "msg": "没有这个接口"})


from urllib.parse import quote     # noqa: E402  （面板下载文件名要用）


# ---------------------------------------------------------------- 面板
PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抓号机器中心</title><style>
body{font:14px/1.5 "Microsoft YaHei",sans-serif;margin:0;background:#101418;color:#e6edf3}
header{position:sticky;top:0;background:#0b0f13;padding:10px 16px;display:flex;gap:12px;
align-items:center;flex-wrap:wrap;border-bottom:1px solid #232a33;z-index:5}
input{background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:6px 8px;border-radius:6px}
button{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer;margin:2px}
button.p{background:#9e6a03}button.s{background:#cf222e}button.g{background:#238636}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;padding:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px}
.off{opacity:.45}
h3{margin:0 0 6px;font-size:16px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.on{background:#3fb950}.grey{background:#8b949e}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:2px 4px;vertical-align:top}td.k{color:#8b949e;width:38%}
#log{background:#0d1117;border:1px solid #30363d;border-radius:10px;margin:0 14px 14px;
padding:10px;height:340px;overflow:auto;font:12px/1.45 Consolas,monospace;white-space:pre-wrap}
.ev{color:#f85149}.ok{color:#3fb950}.mu{color:#8b949e}
.badge{background:#21262d;border-radius:10px;padding:1px 7px;font-size:12px;margin-left:6px}
#disp{padding:10px 14px 4px}
#disp textarea{width:100%;box-sizing:border-box;background:#0d1117;color:#e6edf3;
border:1px solid #30363d;border-radius:8px;padding:8px;font:12px/1.4 Consolas,monospace}
#disp select,#disp input{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:5px}
#evs{margin:6px 14px 0;padding:8px 10px;background:#161b22;border:1px solid #30363d;
border-radius:10px;font-size:13px;max-height:120px;overflow:auto}
</style></head><body>
<header><b>抓号机器中心</b>
令牌 <input id="tk" size="14"> <button onclick="save()">用</button>
<span id="sum" class="mu"></span>
<input id="flt" size="10" placeholder="只看这台" oninput="render()">
<button class="g" onclick="dl()">导出合并去重表</button>
<label style="margin-left:auto"><input type="checkbox" id="ctl" onchange="toctl(this.checked)">
 允许远程控制和分配任务</label>
<span class="mu" id="mode"></span>
<span class="mu" id="err"></span></header>
<div id="disp"><textarea id="addrs" placeholder="粘地址，一行一条……" rows="3"></textarea>
 <div>派给 <select id="dst"><option value="*">平均分给在线的几台</option></select>
 <label><input type="checkbox" id="dup"> 允许多机重复</label>
 <button class="g" onclick="give()">分配任务</button>
 <span id="dmsg" class="mu"></span></div></div>
<div id="evs"></div>
<div class="grid" id="cards"></div>
<div id="log">日志…</div>
<script>
let S=null;
const K='hub_token';
function tk(){return document.getElementById('tk').value||localStorage.getItem(K)||''}
function save(){localStorage.setItem(K,tk().trim());load()}
async function load(){
 try{const r=await fetch('/api/state?token='+encodeURIComponent(tk()));
  if(!r.ok){document.getElementById('err').textContent='令牌不对';return}
  S=await r.json();document.getElementById('err').textContent='';render()}
 catch(e){document.getElementById('err').textContent='中心没响应'}}
function fmt(s){return s<60?s+'秒前':s<3600?Math.floor(s/60)+'分钟前':Math.floor(s/3600)+'小时前'}
function render(){
 if(!S)return;
 const f=(document.getElementById('flt').value||'').trim();
 const CAN=S.control==1;
 document.getElementById('ctl').checked=CAN;
 document.getElementById('disp').style.display=CAN?'':'none';
 document.getElementById('mode').textContent=CAN?'控制已开':'只监控';
 document.getElementById('sum').textContent=
  `在线 ${S.machines.filter(m=>m.online).length}/${S.machines.length}`+
  `｜中心库里 ${S.totals.rows} 行 · ${S.totals.phones} 个号（跨机重复 ${S.totals.dup}）`;
 document.getElementById('cards').innerHTML=S.machines.map(m=>{
  const eg=Object.entries(m.egress||{}).map(([k,v])=>`${k}:${v}`).join(' ')||'—';
  const pc=(S.pending_cmds||{})[m.machine]||0;
  const rw=(S.per_machine_rows||{})[m.machine]||{rows:0,phones:0};
  return `<div class="card ${m.online?'':'off'}"><h3><span class="dot ${m.online?'on':'grey'}"></span>
   ${m.machine}<span class="badge">${m.core||'?'}</span><span class="badge">${m.state||'?'}</span>
   <span class="badge">${m.upload?'交号码':'只报数'}</span><span class="badge">${m.ctl?'听控制':'不听控制'}</span>
   ${m.online?'':'<span class="badge">'+fmt(m.ago)+'掉线</span>'}</h3>
   <table>
   <tr><td class="k">车道/并发</td><td>${m.lanes} / ${m.concurrency}
     <span class="mu">${m.ip||''} v${m.ver||'?'}</span></td></tr>
   <tr><td class="k">人数 / 号码</td><td><b>${m.people}</b> / <b>${m.phones}</b>
     <span class="mu">已推中心 ${rw.rows} 行·${rw.phones} 号</span></td></tr>
   <tr><td class="k">地址</td><td>完成 ${m.done}｜待跑 ${m.pending}｜失败 ${m.failed}
     <span class="mu">${(S.tasks||{})[m.machine]?`中心已派 ${S.tasks[m.machine]['条']} 条（${S.tasks[m.machine]['未取']} 条待取）`:''}</span></td></tr>
   <tr><td class="k">出口国家分布</td><td>${eg}</td></tr>
   <tr><td class="k">内存 / 心跳</td><td>${m.mem_mb} MB / ${fmt(m.ago)}</td></tr>
   ${m.reason?`<tr><td class="k">最后一条</td><td class="mu">${m.reason}</td></tr>`:''}
   </table>
   <div>${CAN?(pc?`<span class="badge">待发指令 ${pc}</span>`:''
   +`<button onclick="cmd('${m.machine}','start')">开始</button>
   <button class="p" onclick="cmd('${m.machine}','pause')">暂停</button>
   <button class="g" onclick="cmd('${m.machine}','resume')">恢复</button>
   <button class="s" onclick="cmd('${m.machine}','stop')">停止</button>
   <button onclick="cmd('${m.machine}','export')">导出到该机</button>
   <button onclick="cmd('${m.machine}','retry_failed')">重试失败</button>
   <button onclick="cmd('${m.machine}','clear_exported')">清除已导出</button>
   <button class="s" onclick="cmd('${m.machine}','delete_done')">删已完成</button>
   <input id="c_${m.machine}" size="2" placeholder="并发">
   <button onclick="conc('${m.machine}')">改</button>`):
   '<span class="mu">只监控：进度、日志、出口都在看；想点按钮就在右上角勾「允许远程控制」</span>'}
   </div></div>`}).join('')
  ||'<p class="mu" style="padding:14px">还没有机器连上来。在那几台电脑的程序里填中心地址＋令牌即可。</p>';
 const lg=S.logs.filter(l=>!f||l.machine.includes(f));
 document.getElementById('log').innerHTML=lg.map(l=>{
  const t=new Date(l.ts*1000).toTimeString().slice(0,8);
  const cls=/没读到|错误|失败|拦|超|崩/.test(l.line)?'ev':(/完成|导出|入库/.test(l.line)?'ok':'');
  return `<span class="mu">[${t}] ${l.machine}</span> <span class="${cls}">${
    l.line.replace(/[<&]/g,c=>({'<':'&lt;','&':'&amp;'}[c]))}</span>`}).join('\n')
  ||'<span class="mu">（还没有日志）</span>';
 const sel=document.getElementById('dst'),cur=sel.value;
 sel.innerHTML='<option value="*">平均分给在线的几台（'+(S.online||[]).length+'台）</option>'+
  S.machines.map(m=>`<option value="${m.machine}">${m.machine}${m.online?'':'（掉线）'}</option>`).join('');
 sel.value=[...sel.options].some(o=>o.value===cur)?cur:'*';
 document.getElementById('evs').innerHTML=(S.events||[]).slice(0,12).map(e=>
  `<span class="mu">${new Date(e.ts*1000).toTimeString().slice(0,8)} ${e.machine}</span> `+
  `<span class="${e.kind==='指令'?'mu':'ev'}">[${e.kind}]</span> ${e.msg}`).join('<br>')
  ||'<span class="mu">（暂无事件：掉线、停滞、派发都会写在这里）</span>';
 document.title=`(${S.machines.filter(m=>m.online).length}台在线) 抓号机器中心`}
async function give(){
 const t=document.getElementById('addrs').value.trim();
 if(!t){document.getElementById('dmsg').textContent='先粘地址';return}
 const r=await fetch('/api/dispatch?token='+encodeURIComponent(tk()),
  {method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({machine:document.getElementById('dst').value,
    text:t,allow_dup:document.getElementById('dup').checked})});
 const j=await r.json();
 document.getElementById('dmsg').innerHTML=j.ok?
  `<span class="ok">已派：${Object.entries(j.per).filter(([k,v])=>v).map(([k,v])=>k+' '+v+' 条').join('，')||'无'}${(j.skipped||j.dup_in_batch)?`，跳过：已派过 ${j.skipped||0} · 这批重复 ${j.dup_in_batch||0}`:''}；那台取走后会自动导入队列</span>`:
  `<span class="ev">${j.msg||'失败'}</span>`;
 if(j.ok)document.getElementById('addrs').value='';
 load()}
// 会删数据的那两个按钮：面板这里连问两次，第二次过了才带 force 下发，
// 那台程序还要求 arg 必须是 force 才动手（单方面点错一下不至于把成果清了）。
const RISK={clear_exported:'清除已导出：会把那台库里已经导出过的结果数据清掉',
            delete_done:'删已完成：会把那台队列里状态=完成的地址删掉'};
async function cmd(m,c,a){
 if(RISK[c]){if(!confirm(RISK[c]+'？'))return;
             if(!confirm('再确认一次：这一步做完，那台上的数据回不来。'))return;
             a='force'}
 const r=await fetch('/api/cmd?token='+encodeURIComponent(tk()),
 {method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({machine:m,cmd:c,arg:a||''})});const j=await r.json();
 if(!j.ok)alert(j.msg||'失败');setTimeout(load,900)}
async function conc(m){const v=document.getElementById('c_'+m).value.trim();
 if(!v)return;await fetch('/api/cmd?token='+encodeURIComponent(tk()),
 {method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({machine:m,cmd:'set_concurrency',arg:v})});setTimeout(load,900)}
function dl(){location='/api/export?token='+encodeURIComponent(tk())}
async function toctl(on){
 const r=await fetch('/api/control?token='+encodeURIComponent(tk()),
  {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:on?1:0})});
 const j=await r.json();
 if(!j.ok){alert(j.msg||'没改成');document.getElementById('ctl').checked=!on}
 load()}
document.getElementById('tk').value=localStorage.getItem(K)||'';
load();setInterval(load,3000);
</script></body></html>"""


def watchdog(stop_flag, every=20):
    """后台每 20 秒巡一遍：谁掉线、谁回来了、哪条指令下发后 2 分钟没回执 ⇒ 记事件。
    他不可能盯屏幕，所以「出事要在页面上留字」比什么都重要。
    第一遍只记账不报事（中心刚重启时不该把本来就关着的机器全刷成掉线）。"""
    was_online = {}
    while not stop_flag.is_set():
        try:
            t = now()
            with LOCK:
                con = db()
                for r in con.execute("SELECT machine,state,hb_at FROM machines").fetchall():
                    on = (t - int(r["hb_at"] or 0)) <= HB_STALE_S
                    if was_online.get(r["machine"]) is True and not on:
                        con.execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                                    (r["machine"], "掉线",
                                     f"{(t - int(r['hb_at'] or 0)) // 60} 分钟没心跳，最后状态 "
                                     f"{r['state']}（中心收不到日志也发不进指令）", t))
                    elif was_online.get(r["machine"]) is False and on:
                        con.execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                                    (r["machine"], "上线", "心跳回来了", t))
                    was_online[r["machine"]] = on
                n = con.execute("""UPDATE cmds SET ack_ok=0,ack_msg='下发后没回执（那台要么掉线要么程序没开）'
                                   WHERE ack_ok IS NULL AND sent_at IS NOT NULL AND sent_at<?""",
                                (t - CMD_TIMEOUT_S,)).rowcount
                if n:
                    con.execute("INSERT INTO events(machine,kind,msg,ts) VALUES(?,?,?,?)",
                                ("*", "指令超时", f"{n} 条指令发下去没回执", t))
                con.commit()
        except Exception as exc:
            print(f"[中心巡检异常] {type(exc).__name__}: {exc}")
        stop_flag.wait(every)


def serve(port, tk=None):
    conf_set("port", port)
    if tk:
        conf_set("token", clean_token(tk))
    t = token()
    httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), H)
    # 每一条都 flush：exe 里 stdout 是被块缓冲的（11-18 冒烟实测：重定向到文件 0 字节），
    # 这屏是他判断「地址填什么、令牌是什么」的唯一来源，堵在缓冲区里等于没打。
    def say(*a):
        print(*a, flush=True)

    say("=" * 68)
    say("  抓号机器中心已经起来了（这台电脑就是中心）")
    say(f"  别的电脑在程序「中心/远程」里填：  http://{lan_ip()}:{port}")
    say(f"  令牌（两栏要一样）：               {t}")
    say(f"  你自己开面板：                     http://127.0.0.1:{port}/  （令牌同上）")
    mode = ("远程控制已开（面板上的按钮能用）" if control_on()
            else "只监控（默认）：能看日志/进度/出口，按钮要点右上角那个勾才出来")
    say(f"  当前模式：{mode}")
    say(f"  库文件：{DB}")
    say("  这个窗口别关（关了机器们就没中心了）。停止：Ctrl+C")
    say("=" * 68)
    stop_flag = threading.Event()
    threading.Thread(target=watchdog, args=(stop_flag,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop_flag.set()
        print("中心已停止。")


def selftest():
    """打包自检（hub.exe --selftest）：临时库 + 临时端口真起一次服务、真发一趟
    心跳/日志/取状态，把结果写成 hub_selftest.txt —— 验证打进去的模块和捆绑的
    标准库都在，不碰正在用的那个中心库。"""
    import tempfile
    from http.server import ThreadingHTTPServer as TWS

    global DB, CON
    out = []
    tmp = Path(tempfile.gettempdir()) / "hub_selftest"
    tmp.mkdir(exist_ok=True)
    DB = tmp / "hub_data.db"
    for p in [DB] + [Path(str(DB) + s) for s in ("-wal", "-shm")]:
        if p.exists():
            p.unlink()
    CON = None
    try:
        db()
        conf_set("token", "SELFTEST-TOKEN")
        # 端口给 0 让系统挑：11-18 吃过亏 —— 写死 8799 时那台机器上别的一个进程
        # （oh_sink.py）正听着同一个端口，Windows 上 HTTPServer 允许重绑，自检的包
        # 就发给别人了，回的不是 JSON，看着像「中心坏了」。
        srv = TWS(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        out.append(f"port={port}（临时端口，不写死）")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        body = json.dumps({"machine": "SELFTEST", "state": "running", "people": 1,
                           "egress": {"美国": 1}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/hb", data=body,
                                     headers={"X-Hub-Token": "SELFTEST-TOKEN",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            hb = json.loads(r.read().decode("utf-8"))
        out.append(f"hb ok={hb.get('ok')}")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/logs",
            data=json.dumps({"machine": "SELFTEST", "items": [{"seq": 1, "ts": now(), "line": "自检一行"}]}
                            ).encode(),
            headers={"X-Hub-Token": "SELFTEST-TOKEN", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            lg = json.loads(r.read().decode("utf-8"))
        out.append(f"logs stored={lg.get('stored')}")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state?token=SELFTEST-TOKEN")
        with urllib.request.urlopen(req, timeout=8) as r:
            st = json.loads(r.read().decode("utf-8"))
        m0 = [m for m in st["machines"] if m["machine"] == "SELFTEST"][0]
        out.append(f"state machines={len(st['machines'])} control={st.get('control')} "
                   f"egress={m0.get('egress')}")
        srv.shutdown()
        srv.server_close()
        ok = bool(hb.get("ok")) and lg.get("stored") == 1 and len(st["machines"]) == 1
        out.append("SELFTEST_OK" if ok else "SELFTEST_FAIL")
    except Exception:
        import traceback
        out.append(traceback.format_exc())
        out.append("SELFTEST_ERROR")
    (BASE / "hub_selftest.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(conf_get("port", PORT_DEFAULT)))
    ap.add_argument("--token", default="", help="固定令牌；不给就随机生成一个并记住")
    a = ap.parse_args()
    db()
    serve(a.port, a.token or None)


if __name__ == "__main__":
    main()
