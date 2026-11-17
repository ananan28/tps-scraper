# -*- coding: utf-8 -*-
"""TPS 抓取引擎"""
import base64
import csv
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

import egress_ip as egress     # 只给日志用：这条车道当前出口 IP + 归属地（标准库，不拖依赖）

# 打包(frozen)时：Windows 数据放 EXE 同级目录；macOS 放 ~/Library/Application Support（.app 包内不宜写库）；源码运行放脚本目录
if getattr(sys, "frozen", False):
    if sys.platform == "darwin":
        BASE = Path.home() / "Library" / "Application Support" / "TPS抓取器"
        BASE.mkdir(parents=True, exist_ok=True)
    else:
        BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "tps_data.db"

TWOCAPTCHA_API_URL = "https://2captcha.com"
TURNSTILE_SITEKEY = "0x4AAAAAAAmywfqBst8n7ro5"
TPS_HOST = "https://www.truepeoplesearch.com"
PROXY_SERVER = "http://niceproxy.io:17521"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PLACE_RE = re.compile(r'^(.*?),\s*([^,]+),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$')

# 只采集 50-85 岁；年龄未知的照抓（无法判断是否在范围内）
CF_SELF_PASS_S = 180
CF_CHALLENGE_MARKERS = ("just a moment", "attention required", "are you a robot",
                        "verifying you are human", "checking your browser", "ddos-guard",
                        "security challenge", "captcha", "too many requests", "access denied")


MIN_AGE = 50
MAX_AGE = 85


def age_ok(age):
    try:
        a = int(str(age).strip())
    except Exception:
        return True
    return MIN_AGE <= a <= MAX_AGE


def connect():
    con = sqlite3.connect(str(DB_PATH), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# ---------------- 落库：抓取线程只往内存队列塞，一条专职线程攒批写 ----------------
# 实测（20 车道 × 每人一次 connect()+一次提交）：平均 26.85ms、p95 20.88ms、最差 4,348ms，
# 全是十几条车道抢 WAL 那一把写锁抢出来的；换成「一条常驻连接 + 攒 1 秒一次提交」后，
# 抓取线程这边排队等待 0 秒，写盘再慢也不挡个人页。
# 断电最多丢最后没提交的那一批（秒级），丢的人在表里还是 pending，重跑会重新采到，
# 语句本身带「只有真拿到号码才覆盖」，重采不会把已有号码抹掉。
_PERSON_SQL = ("INSERT INTO results(address,name,age,location,phones,emails,created_at,nphone,agenum)"
               " VALUES(?,?,?,?,?,?,?,?,?)"
               " ON CONFLICT(address,name,age) DO UPDATE SET"
               " location=excluded.location, phones=excluded.phones,"
               " emails=excluded.emails, created_at=excluded.created_at,"
               " nphone=excluded.nphone, agenum=excluded.agenum WHERE excluded.phones<>''")
WRITE_QUEUED = WRITE_OK = WRITE_BAD = 0
_WRITE_ERR = ""
_write_q = queue.Queue()
_write_stop = threading.Event()
_write_thread = None
_write_lock = threading.Lock()
WRITE_PERSIST_S = 1.0


def _writer_loop():
    con, con_db, batch = None, "", []
    try:
        while True:
            if not batch:
                try:
                    batch = [_write_q.get(timeout=WRITE_PERSIST_S)]
                except queue.Empty:
                    if _write_stop.is_set() and not _write_q.unfinished_tasks:
                        break
                    continue
                deadline = time.time() + WRITE_PERSIST_S
                while len(batch) < 5000:
                    try:
                        batch.append(_write_q.get(timeout=max(0.0, deadline - time.time())))
                    except queue.Empty:
                        break
            # 常驻连接不能钉死在第一次打开的那个库上：DB_PATH 一旦换了，还往旧文件写
            # 就是「界面读新库、人全在旧库」——两边都看不见，纯属丢数据（测试里真踩到了）。
            if con is not None and con_db != str(DB_PATH):
                con.close()
                con = None
            if con is None:
                try:
                    con = connect()
                    con_db = str(DB_PATH)
                except Exception as exc:
                    _fail(batch, f"写线程开库失败 {type(exc).__name__}: {str(exc)[:120]}")
                    batch = []
                    continue
            _commit(batch, con)
            batch = []
    finally:
        if con is not None:
            con.close()                 # 退出才真关，平时一条挂着不撒


def _fail(batch, err):
    """这批人没写进去：计数、留错误、把队列的账清掉，绝不让队列永远挂着。"""
    global WRITE_BAD, _WRITE_ERR
    WRITE_BAD += len(batch)
    _WRITE_ERR = err
    for _ in batch:
        _write_q.task_done()


def _commit(batch, con):
    global WRITE_OK
    try:
        with con:
            con.executemany(_PERSON_SQL, batch)
        WRITE_OK += len(batch)
    except Exception as exc:
        _fail(batch, f"{type(exc).__name__}: {str(exc)[:120]}")
        return
    for _ in batch:
        _write_q.task_done()


def start_writer():
    """幂等起写线程：线程还活着就别动它（尤其别在停止流程里把 stop 标志擦掉）。"""
    global _write_thread
    with _write_lock:
        if _write_thread and _write_thread.is_alive():
            return
        _write_stop.clear()
        _write_thread = threading.Thread(target=_writer_loop, daemon=True, name="dbwriter")
        _write_thread.start()


def write_person(args):
    """把一个人交给写线程，立刻返回（抓取路径不再等 SQLite）。
    入参 (地址, 姓名, 年龄, 居住地, 号码串, 时间[, 邮箱串])：邮箱只有 FPS 给，TPS 留空。
    顺手把「号码几个」「年龄几岁」两个整数存进表里：界面统计直接对整数列求和，
    不用再每 3 秒把整张表读出来逐行数（40 万行实测：逐行 1459ms/次 vs 整数列 70ms/次）。
    两个整数就是用导出同一套 _age_cell / 同一种切段算法算的，统计和导出必然一模一样。"""
    global WRITE_QUEUED
    start_writer()
    row = list(args)
    emails = ",".join(e for e in str(row[6] if len(row) > 6 else "").replace("|", ",").split(",")
                      if e.strip())
    row[4] = ",".join(p for p in str(row[4] or "").replace("|", ",").split(",") if p.strip())
    n, a = _tally(row[2], row[4])
    _write_q.put((row[0], row[1], row[2], row[3], row[4], emails, row[5], n, a))
    WRITE_QUEUED += 1


def _tally(age, phones):
    """(号码个数, 有效年龄或 NULL)：与 _passes_age / 导出用的判定同一个来源。
    号码个数照旧写法逐段数（'|' 也算分隔），老库回填时列里可能就是脏串。"""
    _, a = _age_cell(age)
    return len([p for p in str(phones or "").replace("|", ",").split(",") if p.strip()]), a


def flush_writes(timeout=30):
    """等队列真落盘（地址收尾、导出、停止、关窗之前都要叫一次）。返回是否排空。"""
    start_writer()      # 写线程万一早就死了（比如开库就报错），这里把它捞回来，别干等 30 秒
    deadline = time.time() + timeout
    while _write_q.unfinished_tasks and time.time() < deadline:
        time.sleep(0.02)
    return not _write_q.unfinished_tasks


def stop_writer(timeout=10):
    """收尾：先把在途的写完，再让线程退出（关窗、停止用）。返回是否排空。
    顺序不能反：先置 stop 的话，flush 里为捞线程会幂等 start_writer，反而把 stop 擦了。"""
    ok = flush_writes(timeout)
    _write_stop.set()
    t = _write_thread
    if t and t.is_alive():
        t.join(3)
    return ok


def _migrate_tally(con):
    """老库里补上「号码个数 / 有效年龄」两列，用的还是 _tally 那一套 Python 判定
    （SQL 自己数逗号会把 '|'、空段数错，所以逐行算一遍再写回）。只在开库时跑一次。"""
    have = [r[1] for r in con.execute("PRAGMA table_info(results)")]
    for col in ("nphone", "agenum"):
        if col not in have:
            con.execute(f"ALTER TABLE results ADD COLUMN {col} INTEGER")
    last_id = 0
    while True:
        rows = con.execute("SELECT id,age,phones FROM results WHERE id>? AND "
                           "(nphone IS NULL OR agenum IS NULL) ORDER BY id LIMIT 50000",
                           (last_id,)).fetchall()
        if not rows:
            break
        with con:
            con.executemany("UPDATE results SET nphone=?,agenum=? WHERE id=?",
                            [(n, a, r["id"]) for r in rows
                             for n, a in [_tally(r["age"], r["phones"])]])
        last_id = rows[-1]["id"]
    con.execute("CREATE INDEX IF NOT EXISTS idx_r_stat ON results(nphone,agenum)")


def init_db():
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS addresses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'pending',
            records INTEGER DEFAULT 0,
            people INTEGER DEFAULT 0,
            phones INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )""")
        # 迁移：添加 traffic_kb 列
        have = [r[1] for r in con.execute("PRAGMA table_info(addresses)")]
        if "traffic_kb" not in have:
            con.execute("ALTER TABLE addresses ADD COLUMN traffic_kb INTEGER DEFAULT 0")
        # 弹回首页的次数（每个出口 IP 一次）：够 fps_nodata_confirm 次才允许判「这地址没人」
        if "empty_passes" not in have:
            con.execute("ALTER TABLE addresses ADD COLUMN empty_passes INTEGER DEFAULT 0")
        # 被拦后退回队尾的次数：一条地址不该把车道钉死（实测同一地址连吃 5 个出口 6 分钟 0 进展）
        if "defer_passes" not in have:
            con.execute("ALTER TABLE addresses ADD COLUMN defer_passes INTEGER DEFAULT 0")
        con.execute("""CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT,
            name TEXT,
            age TEXT,
            location TEXT,
            phones TEXT,
            created_at TEXT,
            nphone INTEGER,
            agenum INTEGER,
            emails TEXT DEFAULT ''
        )""")
        # 迁移：老库补 emails 列（个人页 email_section 里的邮箱，一人最多存 2 个）
        if "emails" not in [r[1] for r in con.execute("PRAGMA table_info(results)")]:
            con.execute("ALTER TABLE results ADD COLUMN emails TEXT DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at DESC)")
        con.execute("""DELETE FROM results WHERE id NOT IN
                       (SELECT MIN(id) FROM results GROUP BY address,name,age)""")
        try:
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_unique ON results(address,name,age)")
        except sqlite3.Error:
            pass
        _migrate_tally(con)
        con.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        con.execute("UPDATE addresses SET status='pending' WHERE status='running'")


def get_setting(key, default=""):
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))


def import_addresses(lines):
    added = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as con:
        for line in lines:
            addr = str(line).strip().strip('"').strip()
            if not addr:
                continue
            try:
                cur = con.execute("INSERT OR IGNORE INTO addresses(address,status,created_at,updated_at) VALUES(?,?,?,?)",
                                  (addr, "pending", now, now))
                added += cur.rowcount
            except sqlite3.Error:
                pass
    return added


def stats():
    """统计全交给 SQLite 对两个整数列求和，一行都不往 Python 里拷。
    旧写法每 3 秒把整张 results fetchall 出来逐行数（40 万行实测 1459ms、峰值 77MB），
    库越大界面越卡；现在实测 70ms、峰值 0MB，口径由 _tally 与导出同源保证一致。"""
    flush_writes()       # 内存队列里还没落盘的人也得算进去：显示要等于到手
    lo = int(get_setting("min_age", MIN_AGE))
    hi = int(get_setting("max_age", MAX_AGE))
    with connect() as con:
        out = {}
        for st, n in con.execute("SELECT status,COUNT(*) FROM addresses GROUP BY status"):
            out[st] = n
        out["traffic_kb"] = int(con.execute(
            "SELECT COALESCE(SUM(traffic_kb),0) FROM addresses").fetchone()[0] or 0)
        r = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(nphone),0) phones, "
            "COALESCE(SUM(nphone>0),0) with_phone, "
            "COALESCE(SUM(CASE WHEN agenum IS NULL OR agenum BETWEEN ? AND ? "
            "THEN 1 ELSE 0 END),0) deliver FROM results", (lo, hi)).fetchone()
        # 「全部号码」= 所有人名下号码个数（一人能有十几个）；
        # 「导出条数」= 现在这版表真会写出的行数（一人一行，取第一个常用号）。
        # 两个都摆出来，别拿一个数当另一个用。
        out["results"] = r["n"]
        out["phones"] = int(r["phones"] or 0)
        out["with_phone"] = int(r["with_phone"] or 0)
        out["deliver"] = int(r["deliver"] or 0)
    for key in ("pending", "running", "done", "failed"):
        out.setdefault(key, 0)
    return out


def clean_phone(phone):
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(digits) == 10:
        digits = '1' + digits
    return digits


def _age_cell(age):
    """年龄格清洗：非数字留空；数字大得离谱（实测 413 = 电话区号被当年龄读）也留空。
    留空 = 不知道年龄，不参与范围拦截 —— 不能因为读错一个数就把整个人丢掉。"""
    text = str(age or "").strip()
    if not text.isdigit():
        return "", None
    a = int(text)
    if a < 1 or a > 120:
        return "", None
    return text, a


def _passes_age(age, lo, hi):
    """导出与统计共用同一个年龄判定，两边数出来必须一模一样。"""
    _, a = _age_cell(age)
    return a is None or lo <= a <= hi


def export_csv(path=None):
    """导出：一人一行，电话只给第一个（页面上排最前那个 = 常用号）。
    其余号码仍留在库里（界面「全部号码」数的是全部），要全导随时能再出。"""
    from engine_core import gender_bucket
    flush_writes()                      # 内存队列里还没落盘的人必须先写完再导，否则导出少行
    lo = int(get_setting("min_age", MIN_AGE))
    hi = int(get_setting("max_age", MAX_AGE))
    gender_label = {"male": "男", "female": "女", "andy": "中性", "unknown": "未知"}

    with connect() as con:
        rows = con.execute("SELECT * FROM results ORDER BY created_at DESC").fetchall()

    out = Path(path) if path else BASE / f"tps_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    lines = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["姓名", "性别", "年龄", "居住地", "电话", "查询地址", "采集时间", "邮箱"])
        for r in rows:
            if not _passes_age(r["age"], lo, hi):
                continue
            age_text, _ = _age_cell(r["age"])
            phones = [p for p in str(r["phones"] or "").replace("|", ",").split(",") if p.strip()]
            emails = [e for e in str(r["emails"] or "").replace("|", ",").split(",") if e.strip()]
            w.writerow([r["name"], gender_label.get(gender_bucket(r["name"]), "未知"),
                        age_text, r["location"], clean_phone(phones[0]) if phones else "",
                        r["address"], r["created_at"], ";".join(emails)])
            lines += 1
    return str(out), lines


def export_addresses(statuses, path=None):
    """按状态导出地址队列，导出的行直接粘回别的电脑就能续跑。"""
    marks = ",".join("?" * len(statuses))
    with connect() as con:
        rows = con.execute(
            f"SELECT address FROM addresses WHERE status IN ({marks}) ORDER BY created_at",
            tuple(statuses)).fetchall()
    out = Path(path) if path else BASE / f"addresses_{'-'.join(statuses)}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    out.write_text("".join(r["address"] + "\n" for r in rows), encoding="utf-8")
    return str(out), len(rows)


# 「没数据」不等于状态 nodata：列表页整批被弹光时照样写 done，所以只认存到 0 人。
# running/pending 不算——那是没跑完，不是没数据。
NO_DATA_SQL = ("SELECT address FROM addresses WHERE COALESCE(people,0)=0 "
               "AND status IN ('nodata','done','failed') ORDER BY status,created_at")


def export_no_data(path=None):
    """导出跑过但一个人都没落到的地址（判无住户 + 0 人收尾的 + 失败的）。"""
    with connect() as con:
        rows = con.execute(NO_DATA_SQL).fetchall()
    out = Path(path) if path else BASE / f"addresses_no_data_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    out.write_text("".join(r["address"] + "\n" for r in rows), encoding="utf-8")
    return str(out), len(rows)


def build_url(address):
    """'46 Pine Ave, Bethpage, NY 11714' → resultaddress?streetaddress=…&citystatezip=…"""
    text = str(address).strip()
    m = PLACE_RE.match(text)
    if m:
        street = m.group(1).strip()
        place = text[m.start(2):]
    else:
        street, place = text, ""
    query = {"streetaddress": street}
    if place:
        query["citystatezip"] = place
    return f"{TPS_HOST}/resultaddress?{urllib.parse.urlencode(query)}"


class LocalProxy:
    """本地 CONNECT 中继：Chromium 只连 127.0.0.1，由这里补代理账号认证。

    Playwright 自带的 proxy 用户名/密码在 Chromium 里会直接 ERR_TUNNEL_CONNECTION_FAILED，
    所以把认证挪到本地中继；同一浏览器实例的所有连接共用同一个 session（同一个出口 IP）。
    """

    def __init__(self, upstream_host, upstream_port, username, password, username_template=None):
        self._lock = threading.Lock()
        self._user_tpl = str(username_template or username)
        self._pwd = str(password)
        self._set_auth(username)
        self._upstream = (upstream_host, int(upstream_port))
        self._stopped = False
        self.auth_failed = False
        self.bytes_transferred = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self._sock.settimeout(1.0)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _set_auth(self, username):
        credentials = base64.b64encode(f"{username}:{self._pwd}".encode()).decode()
        with self._lock:
            self._auth = f"Basic {credentials}".encode()

    def set_session(self, session):
        """换出口 IP：只把用户名里的 {session} 换掉，浏览器不用重开（下一条新隧道生效）。"""
        self._set_auth(self._user_tpl.replace("{session}", str(session)))

    @property
    def server(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._stopped = True
        try:
            self._sock.close()
        except Exception:
            pass

    def _serve(self):
        while not self._stopped:
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _read_head(sock, limit=65536):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > limit:
                break
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return data, b""
        return data[:idx + 4], data[idx + 4:]

    @staticmethod
    def _drop_proxy_headers(head):
        lines = head.split(b"\r\n")
        kept = [lines[0]]
        for line in lines[1:]:
            low = line.lower()
            if low.startswith((b"proxy-authorization:", b"proxy-connection:")):
                continue
            if line:
                kept.append(line)
        return b"\r\n".join(kept)

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(45)
            head, pending = self._read_head(client)
            if not head:
                return
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin-1").split()
            if len(parts) < 3:
                return
            if parts[0].upper() != "CONNECT":
                return

            upstream = socket.create_connection(self._upstream, timeout=30)
            upstream.settimeout(45)
            request = (self._drop_proxy_headers(head)
                       + b"\r\nProxy-Authorization: " + self._auth + b"\r\n\r\n")
            upstream.sendall(request)

            response, leftover = self._read_head(upstream)
            client.sendall(response)
            if b" 200" not in response.split(b"\r\n")[0]:
                return

            client.settimeout(None)
            upstream.settimeout(None)
            self._tunnel(client, upstream, pending, leftover)
        except Exception:
            pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _tunnel(self, client, upstream, client_first=b"", upstream_first=b""):
        def pipe(src, dst, first):
            try:
                if first:
                    dst.sendall(first)
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
                    self.bytes_transferred += len(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        to_upstream = threading.Thread(target=pipe, args=(client, upstream, client_first), daemon=True)
        to_client = threading.Thread(target=pipe, args=(upstream, client, upstream_first), daemon=True)
        to_upstream.start()
        to_client.start()
        to_upstream.join()
        to_client.join()


def start_relay(proxy_template, proxy_pass, session):
    """支持两种代理写法：
    1) 完整链接  http://user:pass@host:port
    2) 模板+密码 anan1_xx-ssid-{session}-country-us + 单独密码
    """
    value = str(proxy_template or "").strip()
    if not value:
        raise RuntimeError("未填写代理")
    if "@" in value:
        if "://" not in value:
            value = "http://" + value
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port or 80
        user = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "") or str(proxy_pass or "")
    else:
        base = urllib.parse.urlsplit(PROXY_SERVER)
        host, port = base.hostname, base.port or 80
        user, password = value, str(proxy_pass or "")
    if not host or not user or not password:
        raise RuntimeError(f"代理格式错误：{proxy_template}（需要 user:pass@host:port）")
    return LocalProxy(host, port, user.replace("{session}", session), password,
                      username_template=user)


BLOCK_TITLE_MARKERS = ("just a moment", "attention required", "access denied",
                       "are you a robot", "verifying you are human", "ddos-guard",
                       "checking your browser", "rate limited", "too many requests",
                       "unusual traffic")


def _title(page):
    try:
        return page.title() or ""
    except Exception:
        return ""


def is_blocked_title(title):
    low = (title or "").lower()
    return any(marker in low for marker in BLOCK_TITLE_MARKERS)


def page_status(page):
    """返回 (是否干净结果页, 标题)。
    clean=True 才代表拿到真正的 TPS 页面；否则是 Cloudflare/验证码/被拦页。
    """
    title = _title(page)
    low = title.lower()
    if not title:
        return False, title
    if is_blocked_title(title) or "captcha" in low or "challenge" in low:
        return False, title
    return True, title


CF_CHL_OPT = "_cf_chl_opt"
RATE_BODY_MARKERS = ("rate limit exceeded", "you have exceeded the amount of requests",
                     "allotted time limit", "this ip has been rate limited")
HARD_BLOCK_BODY_MARKERS = ("sorry, you have been blocked", "you've been blocked",
                           "you are unable to access")


class DetailHardBlocked(RuntimeError):
    """详情页撞上限流/拉黑硬页：整条出口 IP 已经废了，
    剩下的人绝不能一个个「跳过此人」当成做完了，必须关掉浏览器重开换 IP。"""


def hard_page_kind(page):
    """标题已判不干净时再分一刀：带 _cf_chl_opt 的是会自过的挑战（等/解都对）；
    正文写「限流」或「你已经被拒」的是硬页 —— 同一条 IP 再等多久都原样返回，
    打码也解不了，只能换出口。返回 'rate' / 'block' / ""。
    签名来自归档原件（Temp\\sourcescan\\rate_hit*）：31 份硬页 0 份带 _cf_chl_opt，
    11 份真挑战页 11 份都带。"""
    try:
        low = (page.content() or "").lower()
    except Exception:
        return ""
    if CF_CHL_OPT in low:
        return ""
    if any(k in low for k in RATE_BODY_MARKERS):
        return "rate"
    if any(k in low for k in HARD_BLOCK_BODY_MARKERS):
        return "block"
    return ""


def build_upstream_proxy(proxy_template, proxy_pass, session):
    """构造与浏览器同一 session 的上游代理串（给 CapSolver 解验证码用）。"""
    value = str(proxy_template or "").strip()
    if "@" in value:
        if "://" not in value:
            value = "http://" + value
        p = urllib.parse.urlsplit(value)
        user = urllib.parse.unquote(p.username or "").replace("{session}", session)
        pw = urllib.parse.unquote(p.password or "") or str(proxy_pass or "")
        return f"http://{user}:{pw}@{p.hostname}:{p.port or 80}"
    base = urllib.parse.urlsplit(PROXY_SERVER)
    user = value.replace("{session}", session)
    return f"http://{user}:{proxy_pass}@{base.hostname}:{base.port or 80}"


def twocaptcha_turnstile_proxied(page_url, upstream_proxy, api_key, action=None):
    """2Captcha 带代理解 Turnstile：token 绑定我们的出口 IP。
    TPS 用 action='900' 渲染，必须带上否则服务端拒收。
    """
    parts = urllib.parse.urlsplit(upstream_proxy)
    proxy_str = f"{parts.username or ''}:{parts.password or ''}@{parts.hostname or ''}:{parts.port or 80}"
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": TURNSTILE_SITEKEY,
        "pageurl": page_url,
        "proxy": proxy_str,
        "proxytype": "http",
        "json": "1",
    }
    if action:
        params["data-s"] = str(action)
    try:
        result = requests.post(f"{TWOCAPTCHA_API_URL}/in.php", data=params, timeout=30).json()
    except Exception:
        return None
    if result.get("status") != 1:
        return None
    task_id = result["request"]
    for _ in range(40):
        time.sleep(2)
        try:
            result = requests.get(f"{TWOCAPTCHA_API_URL}/res.php",
                                  params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
                                  timeout=30).json()
        except Exception:
            return None
        if result.get("status") == 1:
            return result["request"]
    return None


CHALLENGE_INFO_JS = r"""
() => {
  const f = document.getElementById('captchaForm');
  if (!f) return null;
  let action = null, returnUrl = null;
  try { const u = new URL(f.action, location.origin);
        action = u.searchParams.get('rrstamp');
        returnUrl = u.searchParams.get('returnUrl'); } catch(e) {}
  return {action: action, returnUrl: returnUrl, formAction: f.action};
}
"""

SUBMIT_CAPTCHA_JS = r"""
async (token) => {
  const f = document.getElementById('captchaForm');
  if (!f) return {success: false, reason: 'no captchaForm'};
  const fd = new FormData(f);
  fd.append('captchaToken', token);
  try {
    const resp = await fetch(f.action, {method: 'POST', body: fd,
      headers: {'X-Requested-With': 'XMLHttpRequest'}});
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch (e) {
      return {success: false, reason: 'server returned HTML: ' + text.substring(0, 100)};
    }
  } catch (e) {
    return {success: false, reason: String(e)};
  }
}
"""


def try_solve_challenge(page, upstream_proxy, api_key, origin_url=None, log=None):
    """解 TPS 的两种拦截：
    1) TPS InternalCaptcha（有 #captchaForm）：带代理解 Turnstile(action) → captchaToken fetch 提交 → 跳转
    2) Cloudflare 挑战页（Just a moment/Captcha，无 #captchaForm）：不打码（解不了），原地等自过
    成功返回 True（页面已是正常结果页）。
    """
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    # TPS 验证码页的表单是异步渲染的（先显示 Loading content...），必须按 URL 判断并多等
    url_low = (page.url or "").lower()
    wait_s = 10 if "internalcaptcha" in url_low else 2
    has_form = False
    deadline = time.time() + wait_s
    while True:
        try:
            if page.evaluate("() => !!document.getElementById('captchaForm')"):
                has_form = True
                break
        except Exception:
            pass
        if time.time() >= deadline:
            break
        page.wait_for_timeout(300)

    if has_form:
        try:
            info = page.evaluate(CHALLENGE_INFO_JS)
        except Exception:
            info = None
        if not info:
            return False
        action = info.get("action")
        _log(f"    TPS 验证码表单 action={action}，开始解...")
        token = twocaptcha_turnstile_proxied(page.url, upstream_proxy, api_key, action=action)
        if not token:
            _log("    2Captcha 未返回 token")
            return False
        _log(f"    拿到 token ({len(token)} chars)，提交 captchaToken...")
        try:
            data = page.evaluate(SUBMIT_CAPTCHA_JS, token)
        except Exception as exc:
            _log(f"    提交异常: {exc}")
            return False
        if not isinstance(data, dict) or not data.get("success"):
            _log(f"    提交失败: {str(data)[:120]}")
            return False
        target = data.get("redirectUrl") or info.get("returnUrl") or origin_url or page.url
        _log(f"    提交成功，跳转: {str(target)[:70]}")
        try:
            goto_page(page, target, timeout=25000)
            page.wait_for_timeout(1200)
        except Exception:
            return False
        clean, _ = page_status(page)
        return clean

    # Cloudflare 挑战页：实测没有可提交的 sitekey，打码商全部拒单；
    # 拿到的只是 Turnstile token，喂不进 cf_clearance —— 注入假票会让每个地址反复重出题。
    # Firefox/Chrome 自己会过，实测 90~165 秒，所以原地等，不发解码费。
    _log(f"    Cloudflare 挑战：不打码（解不了），等自过最多 {CF_SELF_PASS_S} 秒...")
    deadline = time.time() + CF_SELF_PASS_S
    while time.time() < deadline:
        clean, title = page_status(page)
        if clean:
            _log("    挑战自过了，0 花费")
            return True
        if not any(k in (title or "").lower() for k in CF_CHALLENGE_MARKERS):
            _log(f"    页面变成别的状态({(title or '')[:28]})，交回上层判断")
            return False
        try:
            page.wait_for_timeout(3000)
        except Exception:
            time.sleep(2)
    _log(f"    等了 {CF_SELF_PASS_S} 秒仍未自过，换 IP")
    return False

PAGE_EXTRACT_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('[data-detail-link]'));
  return cards.map(c => {
    const link = c.getAttribute('data-detail-link') || '';
    const nameEl = c.querySelector('.content-header');
    const values = Array.from(c.querySelectorAll('.content-value'))
                       .map(e => (e.innerText || '').trim())
                       .filter(Boolean);
    const text = c.innerText || '';
    const ageMatch = text.match(/Age\\s+(\\d{1,3})/);
    return {
      link: link,
      name: nameEl ? nameEl.innerText.trim() : '',
      age: ageMatch ? ageMatch[1] : '',
      location: values.length > 1 ? values[1] : '',
      dead: /Death Record/i.test(text)
    };
  }).filter(p => p.name && p.link && !p.dead);
}
"""


def extract_people(page):
    try:
        return page.evaluate(PAGE_EXTRACT_JS)
    except Exception:
        return []


def goto_page(page, url, timeout=30000):
    """TPS 页面加载：commit + 等待标题。"""
    try:
        page.goto(url, wait_until="commit", timeout=timeout)
    except Exception:
        try:
            if not page.title():
                raise
        except Exception:
            return False
    for _ in range(20):
        try:
            if page.title():
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def next_page_url(page):
    try:
        href = page.evaluate("""() => {
            const a = document.getElementById('btnNextPage');
            return a ? a.getAttribute('href') : null;
        }""")
    except Exception:
        return None
    if not href:
        return None
    return href if href.startswith("http") else TPS_HOST + href


PHONE_RE = re.compile(r'\(?\d{3}\)?[\s\.-]?\d{3}[\s\.-]?\d{4}')

PRIMARY_PHONE_JS = r"""
() => {
  const src = '\\(?\\d{3}\\)?[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}';
  const phoneRe = new RegExp(src, 'g');
  const out = {mobile: '', landline: '', unlabeled: '', has: false};

  let seg = '';
  const bio = document.getElementById('toc-bio');
  if (bio) {
    const clone = bio.cloneNode(true);
    clone.querySelectorAll('p').forEach(p => p.remove());
    const text = (clone.textContent || '').replace(/\s+/g, ' ');
    const m = text.match(/(?:can be reached at|phone number is|phone numbers are)([^.]{0,300})/i);
    seg = m ? m[1] : '';
  }

  const found = [];
  let hit;
  while ((hit = phoneRe.exec(seg)) !== null) {
    found.push({value: hit[0], start: hit.index, end: hit.index + hit[0].length});
  }
  for (let i = 0; i < found.length; i++) {
    const ctx = seg.slice(found[i].end, i + 1 < found.length ? found[i + 1].start : found[i].end + 60).toLowerCase();
    if (/mobile|wireless|cell/.test(ctx)) {
      if (!out.mobile) out.mobile = found[i].value;
    } else if (/landline|voip|wireline/.test(ctx)) {
      if (!out.landline) out.landline = found[i].value;
    } else if (!out.unlabeled) {
      out.unlabeled = found[i].value;
    }
    out.has = true;
  }

  // 简介里只有一个没标类型的号码 → 就是本人的
  if (!out.mobile && out.unlabeled) out.mobile = out.unlabeled;

  // 简介完全没有号码 → 用详情页顶部号码
  if (!out.mobile && !out.has) {
    const card = document.getElementById('personDetails');
    if (card) {
      const mm = (card.textContent || '').match(new RegExp(src));
      if (mm) out.mobile = mm[0];
    }
    if (!out.mobile) {
      const tm = (document.title || '').match(new RegExp(src + '\\s*$'));
      if (tm) out.mobile = tm[0].trim();
    }
  }
  return out;
}
"""


def scrape_detail(page, person, address, now):
    """打开个人页，取主号码。"""
    scrape_detail.hard_blocked = ""
    try:
        detail_url = person["link"] if person["link"].startswith("http") else TPS_HOST + person["link"]
        if not goto_page(page, detail_url, timeout=30000):
            return None, 0
        page.wait_for_timeout(1500)

        # 详情页若弹验证码，就地解掉（号码只能从详情页拿）
        clean, _dt = page_status(page)
        if not clean:
            up = getattr(scrape_detail, "upstream_proxy", None)
            prog = getattr(scrape_detail, "progress", None)
            hard = hard_page_kind(page)
            # 硬页（限流/拉黑）打码解不了，白花钱还白等；会自过的挑战才值得解（同 1.8.2）
            can_solve = (up and getattr(scrape_detail, "solve_captcha", True) and not hard)
            if can_solve and try_solve_challenge(page, up, scrape_detail.capsolver_key,
                                                 origin_url=detail_url, log=prog):
                clean, _dt = page_status(page)
            if not clean:
                if hard:
                    # 整条出口 IP 已经废了，别把剩下的人一个个「跳过」当做完
                    scrape_detail.hard_blocked = hard
                    if prog:
                        try:
                            prog(f"    详情被{'限流' if hard == 'rate' else '拉黑'}"
                                 f"（{(_dt or 'blocked')[:30]}），这条出口 IP 废了")
                        except Exception:
                            pass
                    return None, 0
                if prog:
                    try:
                        prog(f"    详情被拦({(_dt or 'blocked')[:30]})，跳过此人")
                    except Exception:
                        pass
                return None, 0

        try:
            if "Death Record" in page.title():
                return None, 0
        except Exception:
            pass

        # 号码规则：取移动号码 + 没标注的号码；明确标注座机的不要；简介全空时 JS 已回退到最上面一个号码
        phones = []
        try:
            info = page.evaluate(PRIMARY_PHONE_JS) or {}
            if isinstance(info, dict):
                for key in ("mobile", "unlabeled"):
                    p = clean_phone(info.get(key, ""))
                    if p and p not in phones:
                        phones.append(p)
        except Exception:
            pass

        # 兜底：JS 没拿到任何号码 → 只取详情页最上面一个号码（不抓全文随机号码）
        if not phones:
            try:
                text = page.inner_text("#personDetails")
                m = PHONE_RE.search(text)
                if m:
                    p = clean_phone(m.group(0))
                    if p:
                        phones.append(p)
            except Exception:
                pass

        location = person["location"]
        try:
            text = page.inner_text("#personDetails")
            loc_m = re.search(r'Lives in\s+([^\n]+)', text)
            if loc_m:
                location = loc_m.group(1).strip()
        except Exception:
            pass

        record = {
            "name": person["name"],
            "age": person["age"],
            "location": location,
            "phones": ",".join(phones),
        }
        # 同一个人（地址+姓名+年龄）重复采到时：只有这次真拿到号码才覆盖，
        # 否则一次「没号码的重扫」会把已经存下的号码抹掉 —— 这就是电话数掉回 0 的机制。
        write_person((address, record["name"], record["age"], record["location"],
                      record["phones"], now))
        return record, len(phones)
    except Exception:
        return None, 0


def process_address(address, proxy_template, proxy_pass, capsolver_key, worker_id=0,
                    max_pages=10, progress_cb=None, max_attempts=12, solve_captcha=True):
    """处理一个地址：列表页（含翻页）→ 每个人详情页取电话。
    solve_captcha=True（默认，同 1.8.2）：被拦就解验证码，号码才能拿到。
    solve_captcha=False：被拦直接换 IP（快但详情页大概率拿不到号码）。
    progress_cb(msg_str) 可选，用于实时回报进度。
    """
    def _progress(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    url = build_url(address)
    scrape_detail.capsolver_key = capsolver_key
    scrape_detail.solve_captcha = solve_captcha
    scrape_detail.progress = _progress
    _progress(f"[W{worker_id}] 开始: {address[:40]}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='running',updated_at=? WHERE address=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), address))
    last_title = ""
    last_error = ""

    for attempt in range(max_attempts):
        session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
        upstream_proxy = build_upstream_proxy(proxy_template, proxy_pass, session)
        scrape_detail.upstream_proxy = upstream_proxy
        relay = start_relay(proxy_template, proxy_pass, session)
        _progress(f"[W{worker_id}] 第{attempt+1}/{max_attempts}次尝试，出口 session={session}")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    proxy={"server": relay.server},
                    args=["--no-sandbox", "--disable-gpu"]
                )
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=UA,
                    locale="en-US",
                    timezone_id="America/New_York"
                )
                try:
                    Stealth().apply_stealth_sync(context)
                except Exception:
                    pass
                page = context.new_page()
                # 拦截无用资源：图片/字体/媒体/CSS样式表/杂项 + 广告分析脚本，省流量
                # 数据全在 HTML/DOM 里（用 inner_text/evaluate 取），CSS 只影响视觉不影响抓取
                _BLOCK_TYPES = ("image", "font", "media", "websocket",
                                "stylesheet", "manifest", "texttrack", "ping", "other")
                _BLOCK_DOMAINS = (
                    "googleads", "googlesyndication", "googletagmanager",
                    "doubleclick", "google-analytics", "adservice", "adsystem",
                    "facebook.net", "facebook.com/tr", "connect.facebook",
                    "amazon-adsystem", "adnxs", "adsrvr", "advertising",
                    "cloudflareinsights", "hotjar", "clarity.ms", "bat.bing",
                    "quantserve", "scorecardresearch", "chartbeat", "newrelic",
                    "nr-data.net", "segment.io", "segment.com", "mixpanel",
                    "optimizely", "crazyegg", "mouseflow", "fullstory",
                    "heapanalytics", "amplitude", "branch.io", "appsflyer",
                    "sentry.io", "bugsnag", "rollbar", "datadoghq", "loggly",
                    "mc.yandex", "metrika", "webvisor", "top-fwz1.mail.ru",
                    "analytics.twitter", "t.co/i/", "snap.licdn", "linkedin.com/px",
                    "reddit.com/pixel", "tiktok.com/i18n", "ct.pinterest",
                    "outbrain", "taboola", "criteo", "pubmatic", "rubiconproject",
                    "openx", "casalemedia", "contextweb", "indexww", "33across",
                    "sharethrough", "teads", "sonobi", "gumgum", "lijit", "sovrn",
                    "zemanta", "mgid", "revcontent", "adblade", "content.ad",
                    # 实测大头：Ezoic 广告平台（约占 42% 流量）
                    "ezojs.com", "ezoic", "ezodn", "ezdicdn", "go.ezoic",
                    # Clym cookie 同意弹窗（约占 17% 流量）
                    "clym-widget", "clym.io", "clym.com",
                    # 谷歌地图（数据不需要地图）
                    "maps.googleapis", "maps.google",
                )
                def _block_request(route):
                    if route.request.resource_type in _BLOCK_TYPES:
                        route.abort()
                        return
                    low_url = route.request.url.lower()
                    if any(d in low_url for d in _BLOCK_DOMAINS):
                        route.abort()
                        return
                    route.continue_()
                page.route("**/*", _block_request)
                # 每条车道的出口落到哪里：浏览器一起来就先读一次出口 IP + 归属地。
                # 代理写法带 sid（粘性）时先用 Python 走同一个 sid 的上游读（超时说话算数，
                # <=3.5 秒）；不粘性时厂家每个连接给随机号，那样读出来的不是这条车道在用的号，
                # 就只让浏览器自己在隧道里读。取不到最多损失一行日志，不影响这一轮抓取。
                try:
                    _egress = egress.probe_sync(
                        page, session,
                        upstream_proxy if egress.is_sticky(proxy_template) else "")
                    if _egress:
                        _progress(f"[W{worker_id}] {_egress}")
                except Exception:
                    pass

                try:
                    _progress(f"[W{worker_id}] 加载列表页...")
                    if not goto_page(page, url):
                        last_error = "goto failed"
                        _progress(f"[W{worker_id}] 页面加载失败，换 IP 重试")
                        browser.close()
                        session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
                        continue
                    page.wait_for_timeout(1500)
                    _progress(f"[W{worker_id}] 页面已加载，检查验证码... 流量: {relay.bytes_transferred // 1024} KB")

                    clean, title = page_status(page)
                    last_title = title
                    if not clean:
                        hard = hard_page_kind(page)
                        # 硬页（限流/拉黑）解不了也等不来，别浪费验证码钱
                        solvable = solve_captcha and capsolver_key and not hard
                        if solvable:
                            _progress(f"[W{worker_id}] 被拦({(title or 'challenge')[:30]})，尝试解验证码...")
                            if try_solve_challenge(page, upstream_proxy, capsolver_key, log=_progress):
                                clean, title = page_status(page)
                                _progress(f"[W{worker_id}] 验证码通过，继续采集")
                        if not clean:
                            what = {"rate": "限流", "block": "拉黑"}.get(hard, "拦")
                            last_error = f"{what}: {title or 'challenge'}"
                            _progress(f"[W{worker_id}] 被{what}({(title or 'challenge')[:24]})，"
                                      f"关掉浏览器重开换出口 IP")
                            browser.close()
                            session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
                            continue

                    page.wait_for_timeout(1500)
                    body = page.inner_text("body")
                    body_low = body.lower()
                    if any(m in body_low for m in ("rate limit", "too many requests", "unusual traffic")):
                        last_error = "rate limited"
                        _progress(f"[W{worker_id}] 被限流，换 IP 重试")
                        browser.close()
                        session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
                        continue
                    if not re.search(r'records?\s+found', body, re.IGNORECASE):
                        last_error = f"blocked page: {(last_title or 'no records phrase')[:80]}"
                        _progress(f"[W{worker_id}] 无结果页文案，被拦，换 IP")
                        browser.close()
                        session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
                        continue
                    m = re.search(r'(\d+)\s+records?\s+found', body, re.IGNORECASE)
                    total_records = int(m.group(1)) if m else 0
                    _progress(f"[W{worker_id}] 找到 {total_records} 条记录，开始翻页采集...")

                    people = []
                    seen = set()
                    page_num = 0
                    for _ in range(max_pages):
                        page_num += 1
                        found_this_page = extract_people(page)
                        new_count = 0
                        skipped_age = 0
                        for person in found_this_page:
                            if not age_ok(person.get("age")):
                                skipped_age += 1
                                continue
                            key = person["link"]
                            if key not in seen:
                                seen.add(key)
                                people.append(person)
                                new_count += 1
                        _progress(f"[W{worker_id}] 第{page_num}页: +{new_count}人 (累计{len(people)}人)"
                                  f" 年龄不符跳过{skipped_age}人 流量:{relay.bytes_transferred // 1024}KB")
                        nxt = next_page_url(page)
                        if not nxt:
                            break
                        if not goto_page(page, nxt):
                            break
                        page.wait_for_timeout(1000)
                        clean, last_title = page_status(page)
                        if not clean:
                            _progress(f"[W{worker_id}] 翻页被拦截，停止翻页")
                            break
                        page.wait_for_timeout(800)

                    _progress(f"[W{worker_id}] 列表采集完毕: {len(people)}人，开始获取≥50岁详情页电话（<50岁跳过省流量）...")
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    saved = 0
                    phone_count = 0
                    skipped_young = 0
                    eligible = 0
                    for i, person in enumerate(people):
                        try:
                            age_num = int(str(person.get("age", "") or "").strip())
                        except ValueError:
                            age_num = None
                        if age_num is not None and age_num < 50:
                            skipped_young += 1
                            continue
                        eligible += 1
                        record, n = scrape_detail(page, person, address, now)
                        if scrape_detail.hard_blocked:
                            what = "限流" if scrape_detail.hard_blocked == "rate" else "拉黑"
                            raise DetailHardBlocked(
                                f"详情页被{what}，剩 {len(people) - i} 人没做")
                        if record:
                            saved += 1
                            phone_count += n
                        if eligible % 3 == 0 or i == len(people) - 1:
                            _progress(f"[W{worker_id}] 详情 {i+1}/{len(people)}: "
                                      f"已存{saved}人 {phone_count}个电话 跳过<50岁{skipped_young}人 流量:{relay.bytes_transferred // 1024}KB")
                        time.sleep(0.1)

                    browser.close()
                    flush_writes()      # 排队的人先真落盘，再写地址状态：显示要等于到手
                    with connect() as con:
                        con.execute(
                            "UPDATE addresses SET status='done',records=?,people=?,phones=?,traffic_kb=?,error='',updated_at=? WHERE address=?",
                            (total_records, saved, phone_count, relay.bytes_transferred // 1024, now, address))
                    _progress(f"[W{worker_id}] ✓ 完成: {saved}人 {phone_count}电话 总流量:{relay.bytes_transferred // 1024}KB")
                    return True, {"records": total_records, "people": saved,
                                  "phones": phone_count, "list": len(people), "title": last_title,
                                  "traffic_kb": relay.bytes_transferred // 1024}

                except DetailHardBlocked as exc:
                    last_error = str(exc)
                    _progress(f"[W{worker_id}] {exc} ⇒ 就地换 session 换不动出口"
                              f"（隧道复用），关掉浏览器重开，从头做这个地址")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    continue
                except Exception as exc:
                    if relay.auth_failed:
                        last_error = "代理 407：niceproxy 账号余额不足/认证失败，请充值或换代理"
                    else:
                        last_error = f"{type(exc).__name__}: {exc}"
                    _progress(f"[W{worker_id}] 异常: {last_error}")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
                    continue
        finally:
            relay.stop()

    _progress(f"[W{worker_id}] ✗ {max_attempts}次全部失败: {last_error}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='failed',error=?,updated_at=? WHERE address=?",
                    (f"{last_error} | {last_title}"[:300],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), address))
    return False, {"error": last_error, "title": last_title}
