# -*- coding: utf-8 -*-
"""TPS 抓取引擎"""
import base64
import csv
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


def capsolver_balance():
    """查 2Captcha 余额（美元），失败返回 None。"""
    key = get_setting("capsolver", "")
    if not key:
        return None
    try:
        r = requests.get(f"{TWOCAPTCHA_API_URL}/res.php",
                         params={"key": key, "action": "getbalance", "json": "1"}, timeout=10)
        data = r.json()
        if data.get("status") == 1:
            return float(data.get("request", 0))
    except Exception:
        pass
    return None


def clean_phone(phone):
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(digits) == 10:
        digits = '1' + digits
    return digits


def export_csv(path=None):
    with connect() as con:
        rows = [r for r in con.execute("SELECT * FROM results ORDER BY created_at DESC").fetchall()
                if age_ok(r["age"])]

    out = Path(path) if path else BASE / f"tps_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["姓名", "年龄", "居住地", "电话", "查询地址", "采集时间"])
        for r in rows:
            phones = [p for p in str(r["phones"] or "").split(",") if p.strip()]
            main_phone = clean_phone(phones[0]) if phones else ""
            w.writerow([r["name"], r["age"], r["location"], main_phone,
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
        except Exception as e:
            print(f"[LocalProxy] 连接错误：{e}", flush=True)
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


def twocaptcha_turnstile(page_url, api_key):
    """2Captcha Turnstile 无代理模式"""
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": TURNSTILE_SITEKEY,
        "pageurl": page_url,
        "json": "1"
    }
    try:
        result = requests.post(f"{TWOCAPTCHA_API_URL}/in.php", data=params, timeout=30).json()
    except Exception:
        return None
    if result.get("status") != 1:
        return None
    task_id = result["request"]
    for _ in range(30):
        time.sleep(2)
        try:
            result = requests.get(f"{TWOCAPTCHA_API_URL}/res.php",
                                  params={"key": api_key, "action": "get", "id": task_id, "json": "1"}, timeout=30).json()
        except Exception:
            return None
        if result.get("status") == 1:
            return result["request"]
    return None


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
    # 解析代理 URL 为 2Captcha 格式
    proxy_parts = urllib.parse.urlsplit(upstream_proxy)
    proxy_user = proxy_parts.username or ""
    proxy_pass = proxy_parts.password or ""
    proxy_host = proxy_parts.hostname or ""
    proxy_port = proxy_parts.port or 80
    proxy_str = f"{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": TURNSTILE_SITEKEY,
        "pageurl": page_url,
        "proxy": proxy_str,
        "proxytype": "http",
        "json": "1"
    }
    if action:
        params["data-s"] = str(action)
    
    try:
        result = requests.post(f"{TWOCAPTCHA_API_URL}/in.php", data=params, timeout=30).json()
        print(f"[2Captcha] 提交结果：{result}", flush=True)
    except Exception as e:
        print(f"[2Captcha] 提交异常：{e}", flush=True)
        return None
    if result.get("status") != 1:
        print(f"[2Captcha] 提交失败：{result}", flush=True)
        return None
    task_id = result["request"]
    print(f"[2Captcha] task_id={task_id}，开始轮询...", flush=True)
    for i in range(40):
        time.sleep(2)
        try:
            result = requests.get(f"{TWOCAPTCHA_API_URL}/res.php",
                                  params={"key": api_key, "action": "get", "id": task_id, "json": "1"}, timeout=30).json()
            if i % 5 == 0:
                print(f"[2Captcha] 轮询 {i+1}/40：{result}", flush=True)
        except Exception as e:
            print(f"[2Captcha] 轮询异常：{e}", flush=True)
            return None
        if result.get("status") == 1:
            return result["request"]
    return None
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


def twocaptcha_cloudflare_proxied(page_url, upstream_proxy, api_key, html, user_agent):
    """2Captcha Cloudflare 挑战页求解（使用 Turnstile 方式）。"""
    # 解析代理 URL
    proxy_parts = urllib.parse.urlsplit(upstream_proxy)
    proxy_user = proxy_parts.username or ""
    proxy_pass = proxy_parts.password or ""
    proxy_host = proxy_parts.hostname or ""
    proxy_port = proxy_parts.port or 80
    proxy_str = f"{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    
    params = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": "0x4AAAAAAAAkg0s3VIW10P9P",  # Cloudflare 通用 sitekey
        "pageurl": page_url,
        "proxy": proxy_str,
        "proxytype": "http",
        "json": "1"
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
                              params={"key": api_key, "action": "get", "id": task_id, "json": "1"}, timeout=30).json()
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
        # 2Captcha 返回 {"token": "..."}，提取实际 token 字符串
        token_str = token.get("token", "") if isinstance(token, dict) else str(token)
        if not token_str:
            _log("    token 为空")
            return False
        _log(f"    拿到 token ({len(token_str)} chars)，提交 captchaToken...")
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


def scrape_detail(page, person, address, now):
    """打开个人页，取主号码。"""
    try:
        detail_url = person["link"] if person["link"].startswith("http") else TPS_HOST + person["link"]
        if not goto_page(page, detail_url, timeout=30000):
            return None, 0
        page.wait_for_timeout(1500)

        # 详情页若弹验证码，就地解掉（号码只能从详情页拿）
        clean, _dt = page_status(page)
        if not clean:
            up = getattr(scrape_detail, "upstream_proxy", None)
            if up and try_solve_challenge(page, up, scrape_detail.capsolver_key, origin_url=detail_url, log=getattr(scrape_detail, "progress", None)):
                clean, _dt = page_status(page)
            if not clean:
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
        with connect() as con:
            con.execute("INSERT OR REPLACE INTO results(address,name,age,location,phones,created_at) VALUES(?,?,?,?,?,?)",
                        (address, record["name"], record["age"], record["location"],
                         record["phones"], now))
        return record, len(phones)
    except Exception:
        return None, 0


def process_address(address, proxy_template, proxy_pass, capsolver_key, worker_id=0,
                    max_pages=10, progress_cb=None, max_attempts=12):
    """处理一个地址：列表页（含翻页）→ 每个人详情页取电话。
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
                        _progress(f"[W{worker_id}] 被拦({(title or 'challenge')[:30]})，尝试解验证码...")
                        if try_solve_challenge(page, upstream_proxy, capsolver_key, log=_progress):
                            clean, title = page_status(page)
                            _progress(f"[W{worker_id}] 验证码通过，继续采集")
                        if not clean:
                            last_error = f"blocked: {title or 'challenge'}"
                            _progress(f"[W{worker_id}] 解不出/换 IP 重试")
                            browser.close()
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
                        if record:
                            saved += 1
                            phone_count += n
                        if eligible % 3 == 0 or i == len(people) - 1:
                            _progress(f"[W{worker_id}] 详情 {i+1}/{len(people)}: "
                                      f"已存{saved}人 {phone_count}个电话 跳过<50岁{skipped_young}人 流量:{relay.bytes_transferred // 1024}KB")
                        time.sleep(0.1)

                    browser.close()

                    with connect() as con:
                        con.execute(
                            "UPDATE addresses SET status='done',records=?,people=?,phones=?,traffic_kb=?,error='',updated_at=? WHERE address=?",
                            (total_records, saved, phone_count, relay.bytes_transferred // 1024, now, address))
                    _progress(f"[W{worker_id}] ✓ 完成: {saved}人 {phone_count}电话 总流量:{relay.bytes_transferred // 1024}KB")
                    return True, {"records": total_records, "people": saved,
                                  "phones": phone_count, "list": len(people), "title": last_title,
                                  "traffic_kb": relay.bytes_transferred // 1024}

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
