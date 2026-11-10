# -*- coding: utf-8 -*-
"""TPS 抓取引擎"""
import base64
import csv
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import asyncio
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

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
MIN_AGE = 50
MAX_AGE = 85


def age_ok(age):
    try:
        a = int(str(age).strip())
    except Exception:
        return True
    return MIN_AGE <= a <= MAX_AGE


_NAME_GENDER = None
_USER_NAMES = None


def _user_names():
    """用户自己的姓名库（从 新筛姓名.exe 提取，45,443 名），放在 EXE/脚本同级目录。"""
    global _USER_NAMES
    if _USER_NAMES is None:
        try:
            _USER_NAMES = json.loads((BASE / "user_names.json").read_text(encoding="utf-8"))
        except Exception:
            _USER_NAMES = {}
    return _USER_NAMES


def name_gender(name):
    """按名字判断性别：male / female / andy(中性) / unknown。
    先查用户姓名库（女→砍，男/中性→留），查不到再用 gender-guesser 兜底。"""
    raw = str(name or "").strip()
    if not raw:
        return "unknown"
    token = re.split(r"[\s,.]+", raw, 1)[0]
    first = re.sub(r"[^A-Za-z'\-]", "", token)
    lib = _user_names()
    if lib:
        g = lib.get(token.upper()) or lib.get(first.upper())
        if g == "女":
            return "female"
        if g == "男":
            return "male"
        if g == "中性":
            return "andy"
    if not first:
        return "unknown"
    global _NAME_GENDER
    if _NAME_GENDER is None:
        try:
            from gender_guesser.detector import Detector
            _NAME_GENDER = Detector(case_sensitive=False)
        except Exception:
            _NAME_GENDER = False
    if not _NAME_GENDER:
        return "unknown"
    try:
        g = _NAME_GENDER.get_gender(first, "usa")
        if g == "unknown":
            g = _NAME_GENDER.get_gender(first)
        return g or "unknown"
    except Exception:
        return "unknown"


def is_obvious_female(name):
    """砍明确女性和大比例女性；中性/未知一律保留，避免误伤男性。"""
    return name_gender(name) in ("female", "mostly_female")


def gender_bucket(name):
    """把名字性别归到四类：male / female / andy(中性) / unknown，供勾选过滤。"""
    g = name_gender(name)
    if g in ("female", "mostly_female"):
        return "female"
    if g in ("male", "mostly_male"):
        return "male"
    if g == "andy":
        return "andy"
    return "unknown"


def connect():
    con = sqlite3.connect(str(DB_PATH), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # 添加流量统计字段（如果不存在）
    try:
        con.execute("ALTER TABLE addresses ADD COLUMN traffic_kb INTEGER DEFAULT 0")
    except Exception:
        pass
    return con


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
        con.execute("""CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT,
            name TEXT,
            age TEXT,
            location TEXT,
            phones TEXT,
            created_at TEXT
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at DESC)")
        con.execute("""DELETE FROM results WHERE id NOT IN
                       (SELECT MIN(id) FROM results GROUP BY address,name,age)""")
        try:
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_unique ON results(address,name,age)")
        except sqlite3.Error:
            pass
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
    with connect() as con:
        out = {}
        for st, n in con.execute("SELECT status,COUNT(*) FROM addresses GROUP BY status"):
            out[st] = n
        out["results"] = con.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        out["phones"] = con.execute("SELECT COUNT(*) FROM results WHERE phones != ''").fetchone()[0]
        out["traffic_kb"] = int(con.execute("SELECT COALESCE(SUM(traffic_kb),0) FROM addresses").fetchone()[0] or 0)
    for key in ("pending", "running", "done", "failed"):
        out.setdefault(key, 0)
    return out


def clean_phone(phone):
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(digits) == 10:
        digits = '1' + digits
    return digits


def export_csv(path=None):
    lo = int(get_setting("min_age", MIN_AGE))
    hi = int(get_setting("max_age", MAX_AGE))

    def _in_range(age):
        try:
            a = int(str(age).strip())
        except Exception:
            return True
        return lo <= a <= hi

    with connect() as con:
        rows = [r for r in con.execute("SELECT * FROM results ORDER BY created_at DESC").fetchall()
                if _in_range(r["age"])]

    out = Path(path) if path else BASE / f"tps_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    gender_label = {"male": "男", "female": "女", "andy": "中性", "unknown": "未知"}

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["姓名", "性别", "年龄", "居住地", "电话", "查询地址", "采集时间"])
        for r in rows:
            phones = [p for p in str(r["phones"] or "").split(",") if p.strip()]
            main_phone = clean_phone(phones[0]) if phones else ""
            w.writerow([r["name"], gender_label.get(gender_bucket(r["name"]), "未知"),
                        r["age"], r["location"], main_phone,
                        r["address"], r["created_at"]])

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

    def __init__(self, upstream_host, upstream_port, username, password):
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {credentials}".encode()
        self._upstream = (upstream_host, int(upstream_port))
        self._stopped = False
        self.auth_failed = False
        self.conn_error = ""
        self.bytes_transferred = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self._sock.settimeout(1.0)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def server(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._stopped = True
        try:
            self._sock.close()
        except Exception:
            pass

    def _conn_err_text(self, exc):
        host, port = self._upstream
        if isinstance(exc, socket.gaierror):
            return f"DNS解析失败({host})"
        if isinstance(exc, ConnectionRefusedError):
            return f"连接被拒({host}:{port})"
        if isinstance(exc, ConnectionResetError):
            return f"连接被重置({host}:{port}，疑似并发线程超限)"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return f"连接超时({host}:{port})"
        return f"{type(exc).__name__}: {exc}"[:80]

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

            try:
                upstream = socket.create_connection(self._upstream, timeout=30)
            except Exception as exc:
                self.conn_error = self._conn_err_text(exc)
                return
            upstream.settimeout(45)
            request = (self._drop_proxy_headers(head)
                       + b"\r\nProxy-Authorization: " + self._auth + b"\r\n\r\n")
            upstream.sendall(request)

            response, leftover = self._read_head(upstream)
            client.sendall(response)
            status_line = response.split(b"\r\n")[0]
            if b" 200" not in status_line:
                if b" 407" in status_line:
                    self.auth_failed = True
                    self.conn_error = "代理407认证失败(账号/密码错或余额不足)"
                else:
                    self.conn_error = f"代理拒绝: {status_line.decode('latin-1', 'replace')[:60]}"
                return

            client.settimeout(None)
            upstream.settimeout(None)
            self._tunnel(client, upstream, pending, leftover)
        except Exception as exc:
            if not self.conn_error:
                self.conn_error = self._conn_err_text(exc)
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
    user = user.replace("{session}", session)
    return LocalProxy(host, port, user, password)


BLOCK_TITLE_MARKERS = ("just a moment", "attention required", "access denied",
                       "are you a robot", "verifying you are human", "ddos-guard",
                       "checking your browser", "rate limited", "too many requests",
                       "unusual traffic")
# 这些标题说明是限流/硬拒绝，页面上没有可解的挑战
UNSOLVABLE_MARKERS = ("rate limited", "too many requests", "access denied", "unusual traffic")
# 真正能用 2Captcha 解的 Cloudflare 挑战
SOLVABLE_CF_MARKERS = ("just a moment", "attention required", "are you a robot",
                       "verifying you are human", "checking your browser", "ddos-guard")


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
    const data = await resp.json();
    return data;
  } catch (e) {
    return {success: false, reason: String(e)};
  }
}
"""


def twocaptcha_cloudflare_proxied(page_url, upstream_proxy, api_key, html, user_agent):
    """2Captcha Cloudflare 挑战页求解（Turnstile 方式），返回 {"token": ...}。"""
    parts = urllib.parse.urlsplit(upstream_proxy)
    proxy_str = f"{parts.username or ''}:{parts.password or ''}@{parts.hostname or ''}:{parts.port or 80}"
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": "0x4AAAAAAAAkg0s3VIW10P9P",
        "pageurl": page_url,
        "proxy": proxy_str,
        "proxytype": "http",
        "json": "1",
    }
    try:
        r = requests.post(f"{TWOCAPTCHA_API_URL}/in.php", data=params, timeout=30).json()
    except Exception:
        return None
    if r.get("status") != 1:
        return None
    task_id = r["request"]
    for _ in range(60):
        time.sleep(2)
        try:
            rr = requests.get(f"{TWOCAPTCHA_API_URL}/res.php",
                              params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
                              timeout=30).json()
        except Exception:
            return None
        if rr.get("status") == 1:
            return {"token": rr["request"]}
    return None


def try_solve_challenge(page, upstream_proxy, api_key, origin_url=None, log=None):
    """解 TPS 的两种拦截：
    1) TPS InternalCaptcha（有 #captchaForm）：带代理解 Turnstile(action) → captchaToken fetch 提交 → 跳转
    2) Cloudflare 挑战页（Just a moment/Captcha，无 #captchaForm）：AntiCloudflareTask 拿 cf_clearance 注入再刷
    成功返回 True（页面已是正常结果页）。
    """
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    # 先判死刑：这类页面根本没有挑战（无表单、无 Turnstile），送 2Captcha 必失败还白等
    low_title = _title(page).lower()
    if any(k in low_title for k in UNSOLVABLE_MARKERS):
        _log(f"    判定不可解({low_title[:32]})，不送解码，直接换 IP")
        return False

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
        _log("    拿到 token，提交 captchaToken...")
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

    # 没有表单、标题也不是 Cloudflare 挑战 → 不是能解的页面，别浪费解码费
    if not any(k in low_title for k in SOLVABLE_CF_MARKERS):
        _log(f"    无表单且非 Cloudflare 挑战({low_title[:32] or '空标题'})，不送解码")
        return False

    # Cloudflare 挑战页
    _log("    疑似 Cloudflare 挑战，用 AntiCloudflareTask 取 cf_clearance...")
    try:
        html = page.content()
    except Exception:
        return False
    sol = twocaptcha_cloudflare_proxied(page.url, upstream_proxy, api_key, html, UA)
    if not sol:
        _log("    Cloudflare 解失败")
        return False
    cf = None
    cookies = sol.get("cookies")
    if isinstance(cookies, dict):
        cf = cookies.get("cf_clearance")
    elif isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and c.get("name") == "cf_clearance":
                cf = c.get("value")
                break
    if not cf:
        cf = sol.get("token")
    if not cf:
        _log(f"    未拿到 cf_clearance: {str(sol)[:120]}")
        return False
    _log("    拿到 cf_clearance，注入并重载...")
    try:
        host = urllib.parse.urlsplit(page.url).hostname or "www.truepeoplesearch.com"
        page.context.add_cookies([{"name": "cf_clearance", "value": cf, "url": f"https://{host}/"}])
    except Exception as exc:
        _log(f"    cookie 注入失败: {exc}")
        return False
    target = origin_url or page.url
    try:
        goto_page(page, target, timeout=25000)
        page.wait_for_timeout(1500)
    except Exception:
        return False
    clean, _ = page_status(page)
    return clean


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


def scrape_detail(page, person, address, now, upstream_proxy=None, api_key="",
                  log=None, worker_id=0):
    """打开个人页，取主号码。返回 (record, phone_count, status)。
    status: ok=已处理 / blocked=被拦或打不开 / dead=死亡记录
    """
    def _log(msg):
        if log:
            try:
                log(f"[W{worker_id}] {msg}")
            except Exception:
                pass

    try:
        detail_url = person["link"] if person["link"].startswith("http") else TPS_HOST + person["link"]
        if not goto_page(page, detail_url, timeout=30000):
            return None, 0, "hard_block"      # 打不开基本是出口 IP 问题，不算在人头上
        page.wait_for_timeout(1500)

        # 详情页若弹验证码，就地解掉（号码只能从详情页拿）
        clean, _dt = page_status(page)
        if not clean:
            _log(f"详情被拦({(_dt or 'challenge')[:28]})，尝试解码...")
            if upstream_proxy and try_solve_challenge(page, upstream_proxy, api_key,
                                                      origin_url=detail_url, log=_log):
                clean, _dt = page_status(page)
            if not clean:
                # 限流/拒绝是 IP 级封锁，换 IP 才能继续，不记在这个人头上
                if any(k in (_dt or "").lower() for k in UNSOLVABLE_MARKERS):
                    return None, 0, "hard_block"
                return None, 0, "blocked"

        try:
            if "Death Record" in page.title():
                return None, 0, "dead"
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
        with connect() as con:
            con.execute("INSERT OR REPLACE INTO results(address,name,age,location,phones,created_at) VALUES(?,?,?,?,?,?)",
                        (address, record["name"], record["age"], record["location"],
                         record["phones"], now))
        return record, len(phones), "ok"
    except Exception:
        return None, 0, "hard_block"


def process_address(address, proxy_template, proxy_pass, api_key, worker_id=0,
                    max_pages=10, progress_cb=None, max_attempts=12, male_only=False,
                    min_age=MIN_AGE, max_age=MAX_AGE, max_rotations=6, max_person_tries=2,
                    keep_genders=None):
    """处理一个地址：列表页（含翻页）→ 每个人详情页取电话。
    只抓 min_age–max_age 岁（默认 50–85）；keep_genders 指定保留的性别类别
    （male/female/andy/unknown 的子集），None 且 male_only=True 时保留 男+中性+未知。
    详情页连续被拦会换 IP 续做（不重抓列表，最多 max_rotations 次）；
    单人试 max_person_tries 次仍打不开就跳过该人。
    progress_cb(msg_str) 可选，用于实时回报进度。
    """
    def _progress(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    def _log(msg):
        _progress(f"[W{worker_id}] {msg}")

    MAX_DETAIL_ROTATIONS = max_rotations    # 每个地址最多换几次 IP 续做详情
    MAX_PERSON_TRIES = max_person_tries     # 同一个人最多试几次
    MAX_CONSECUTIVE_BLOCKED = 3             # 连续几次被拦就判定这个出口 IP 已死

    if keep_genders is not None:
        allowed_genders = set(keep_genders)
    elif male_only:
        allowed_genders = {"male", "andy", "unknown"}
    else:
        allowed_genders = None              # None = 不按性别过滤

    url = build_url(address)
    _log(f"开始: {address[:40]}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='running',updated_at=? WHERE address=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), address))
    last_title = ""
    last_error = ""

    people = None          # 列表采完后复用，换 IP 不重抓
    pending = []           # 还没做详情的人
    tries = {}             # 详情链接 -> 已试次数
    total_records = 0
    saved = 0
    phone_count = 0
    skipped_gender = 0
    skipped_blocked = 0
    skipped_low = 0
    skipped_high = 0
    dup_skipped = 0
    cards_seen = 0
    no_phone = 0
    dead_skipped = 0
    abandoned = 0
    done_count = 0
    total_kb = 0
    detail_rotations = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last_heartbeat = time.time()

    def _age_eligible(person):
        try:
            age_num = int(str(person.get("age", "") or "").strip())
        except ValueError:
            return True              # 年龄未知照抓
        return min_age <= age_num <= max_age

    for attempt in range(max_attempts):
        session = f"session_{int(time.time() * 1000) % 1000000}_{worker_id}"
        upstream_proxy = build_upstream_proxy(proxy_template, proxy_pass, session)
        relay = start_relay(proxy_template, proxy_pass, session)
        _log(f"第{attempt+1}/{max_attempts}次尝试，出口 session={session}")
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

                try:
                    if people is None:
                        # ===== 列表阶段（与 v1.8.2 一致）=====
                        _log("加载列表页...")
                        if not goto_page(page, url):
                            reason = relay.conn_error or "页面无法加载"
                            last_error = f"goto failed: {reason}"
                            total_kb += relay.bytes_transferred // 1024
                            _log(f"页面加载失败({reason})，换 IP 重试")
                            browser.close()
                            continue
                        page.wait_for_timeout(1500)
                        _log(f"页面已加载，检查验证码... 流量: {total_kb + relay.bytes_transferred // 1024} KB")

                        clean, title = page_status(page)
                        last_title = title
                        if not clean:
                            _log(f"被拦({(title or 'challenge')[:30]})，尝试解验证码...")
                            if try_solve_challenge(page, upstream_proxy, api_key, log=_log):
                                clean, title = page_status(page)
                                _log("验证码通过，继续采集")
                            if not clean:
                                last_error = f"blocked: {title or 'challenge'}"
                                total_kb += relay.bytes_transferred // 1024
                                _log("解不出/换 IP 重试")
                                browser.close()
                                continue

                        page.wait_for_timeout(1500)
                        body = page.inner_text("body")
                        body_low = body.lower()
                        if any(m in body_low for m in ("rate limit", "too many requests", "unusual traffic")):
                            last_error = "rate limited"
                            total_kb += relay.bytes_transferred // 1024
                            _log("被限流，换 IP 重试")
                            browser.close()
                            continue
                        if not re.search(r'records?\s+found', body, re.IGNORECASE):
                            last_error = f"blocked page: {(last_title or 'no records phrase')[:80]}"
                            total_kb += relay.bytes_transferred // 1024
                            _log("无结果页文案，被拦，换 IP")
                            browser.close()
                            continue
                        m = re.search(r'(\d+)\s+records?\s+found', body, re.IGNORECASE)
                        total_records = int(m.group(1)) if m else 0
                        _log(f"找到 {total_records} 条记录，开始翻页采集...")

                        people = []
                        seen = set()
                        page_num = 0
                        for _ in range(max_pages):
                            page_num += 1
                            found_this_page = extract_people(page)
                            new_count = 0
                            for person in found_this_page:
                                cards_seen += 1
                                try:
                                    _age = int(str(person.get("age", "") or "").strip())
                                except ValueError:
                                    _age = None
                                if _age is not None and _age < min_age:
                                    skipped_low += 1
                                    continue
                                if _age is not None and _age > max_age:
                                    skipped_high += 1
                                    continue
                                key = person["link"]
                                if key in seen:
                                    dup_skipped += 1
                                    continue
                                seen.add(key)
                                people.append(person)
                                new_count += 1
                            _log(f"第{page_num}页: +{new_count}人 (累计{len(people)}人)"
                                 f" 年龄不符合{skipped_low + skipped_high} 重复{dup_skipped}"
                                 f" 流量:{total_kb + relay.bytes_transferred // 1024}KB")
                            nxt = next_page_url(page)
                            if not nxt:
                                break
                            if not goto_page(page, nxt):
                                break
                            page.wait_for_timeout(1000)
                            clean, last_title = page_status(page)
                            if not clean:
                                _log("翻页被拦截，停止翻页")
                                break
                            page.wait_for_timeout(800)

                        pending = []
                        skipped_gender = 0
                        for pp in people:
                            if not _age_eligible(pp):
                                continue
                            if allowed_genders is not None and gender_bucket(pp.get("name", "")) not in allowed_genders:
                                skipped_gender += 1
                                continue
                            pending.append(pp)
                        _log(f"列表采集完毕: 网站报{total_records}条 → 解析{cards_seen}张卡片，"
                             f"年龄不符合{skipped_low + skipped_high}人、重复{dup_skipped}，"
                             f"留下{len(people)}人"
                             f"{'，性别不符再跳过%d人' % skipped_gender if allowed_genders is not None else ''}"
                             f" → 待抓{len(pending)}人")
                        tries = {pp["link"]: 0 for pp in pending}
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        _log(f"换 IP 完成，续做剩余 {len(pending)} 人（列表不重抓）")

                    # ===== 详情阶段：连续被拦就换 IP 续做 =====
                    rotate_needed = False
                    consecutive_blocked = 0
                    last_heartbeat = time.time()
                    while pending:
                        person = pending[0]
                        record, n, status = scrape_detail(
                            page, person, address, now,
                            upstream_proxy=upstream_proxy, api_key=api_key,
                            log=_progress, worker_id=worker_id)
                        if status in ("blocked", "hard_block"):
                            ip_dead = status == "hard_block"
                            if not ip_dead:
                                tries[person["link"]] = tries.get(person["link"], 0) + 1
                            consecutive_blocked += 1
                            if consecutive_blocked >= MAX_CONSECUTIVE_BLOCKED:
                                if detail_rotations >= MAX_DETAIL_ROTATIONS:
                                    abandoned = len(pending)
                                    _log(f"换 IP 已达上限 {MAX_DETAIL_ROTATIONS} 次，停止详情，已存 {saved} 人，"
                                         f"剩余 {abandoned} 人放弃")
                                    pending.clear()
                                    break
                                detail_rotations += 1
                                rotate_needed = True
                                last_error = "detail blocked"
                                _log(f"连续被拦 {consecutive_blocked} 次"
                                     f"（{'IP被限流/拒绝' if ip_dead else '验证码解不出'}），换 IP 续做"
                                     f"（第 {detail_rotations}/{MAX_DETAIL_ROTATIONS} 次），剩 {len(pending)} 人")
                                break
                            if not ip_dead and tries[person["link"]] >= MAX_PERSON_TRIES:
                                pending.pop(0)
                                done_count += 1
                                skipped_blocked += 1
                                _log(f"跳过打不开的人: {person['name']}（试 {MAX_PERSON_TRIES} 次都被拦）")
                        elif status == "dead":
                            pending.pop(0)
                            done_count += 1
                            consecutive_blocked = 0
                            dead_skipped += 1
                        else:
                            pending.pop(0)
                            done_count += 1
                            consecutive_blocked = 0
                            if record:
                                saved += 1
                                phone_count += n
                                if n == 0:
                                    no_phone += 1

                        if time.time() - last_heartbeat >= 30:
                            _log(f"心跳: 已处理{done_count}人 待做{len(pending)}人 已存{saved}人 "
                                 f"{phone_count}电话 连续被拦{consecutive_blocked} 换IP{detail_rotations}次 "
                                 f"流量:{total_kb + relay.bytes_transferred // 1024}KB")
                            last_heartbeat = time.time()

                    if rotate_needed:
                        total_kb += relay.bytes_transferred // 1024
                        try:
                            browser.close()
                        except Exception:
                            pass
                        continue

                    browser.close()
                    traffic_kb = total_kb + relay.bytes_transferred // 1024

                    with connect() as con:
                        con.execute(
                            "UPDATE addresses SET status='done',records=?,people=?,phones=?,traffic_kb=?,error='',updated_at=? WHERE address=?",
                            (total_records, saved, phone_count, traffic_kb, now, address))
                    _log(f"✓ 完成: 网站报{total_records}条 → 卡片{cards_seen}张 → 已存{saved}人（有号码{saved - no_phone} / "
                         f"无号码{no_phone}）{phone_count}电话；跳过: <{min_age}岁{skipped_low} >{max_age}岁{skipped_high} "
                         f"性别不符{skipped_gender} 重复{dup_skipped} 已故{dead_skipped} 打不开{skipped_blocked} 放弃{abandoned}；"
                         f"流量{traffic_kb}KB")
                    return True, {"records": total_records, "people": saved,
                                  "phones": phone_count, "list": len(people), "title": last_title,
                                  "traffic_kb": traffic_kb, "skipped_blocked": skipped_blocked,
                                  "skipped_gender": skipped_gender, "skipped_low": skipped_low,
                                  "skipped_high": skipped_high, "dup_skipped": dup_skipped,
                                  "no_phone": no_phone, "dead_skipped": dead_skipped,
                                  "cards_seen": cards_seen, "abandoned": abandoned}

                except Exception as exc:
                    if relay.auth_failed:
                        last_error = "代理 407：账号认证失败/余额不足，请检查代理或充值"
                    else:
                        last_error = f"{type(exc).__name__}: {exc}"
                    total_kb += relay.bytes_transferred // 1024
                    _log(f"异常: {last_error}")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    continue
        finally:
            relay.stop()

    _log(f"✗ {max_attempts}次全部失败: {last_error}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='failed',error=?,updated_at=? WHERE address=?",
                    (f"{last_error} | {last_title}"[:300],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), address))
    return False, {"error": last_error, "title": last_title}
