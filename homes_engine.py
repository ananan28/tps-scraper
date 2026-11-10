# -*- coding: utf-8 -*-
"""homes.com 房地产经纪人抓取引擎。

 nodriver 起真浏览器过 Akamai；列表页 /real-estate-agents/{slug}/p{N}/ 一页 48 人自带电话，
 不进详情页。翻页只认 URL 路径里的 /p{N}/（?page=N 与 /N/ 网站都不认）。
 2026-11-10 实测：目录页现在先发一张 2.4KB 的 "Powered and protected by Privacy" 挑战页，
 它自己 location.reload 后 4~25 秒内变成 18 万字节的真页面；这期间绝不能当成被拦去退避。
 本机直连（泰国出口）已被边缘 403，连 robots.txt 都拒，必须走美国住宅代理。
"""
import asyncio
import csv
import random
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime

import nodriver as uc

from engine_core import BASE, is_obvious_female, name_gender, start_relay
from browser_path import browser_kind, describe_search, find_browser, is_bundled

DB_PATH = BASE / "homes_data.db"
HOMES_HOST = "https://www.homes.com"
DIRECTORY = "/real-estate-agents/"
HUB = "real-estate-agents"
PER_PAGE = 48
MAX_PAGE = 400

_lock = threading.Lock()

_SPLIT = re.compile(r'<li class="agent-results-item')
_KEY = re.compile(r'data-key="([0-9a-z]{4,})"')
_NAME = re.compile(r'class="agent-name[^"]*"[^>]*>([^<]{2,80})<')
_COMPANY = re.compile(r'class="company">([^<]{0,140})<')
_PHONE = re.compile(r'class="phone">([^<]{0,40})<')
_SALES = re.compile(r'class="total-sales"><span>([\d,]+)</span>')
_PAGE_RANGE = re.compile(r'class="pageRange">\s*Page[\s\d,]*of\s*([\d,]+)')
_DATA_PAGE = re.compile(r'data-page="(\d+)"')
_SLUG_HREF = re.compile(r'href="/real-estate-agents/([a-z0-9][a-z0-9\-]{1,70})/"')
_CANON = re.compile(r'canonical[^>]*href="https?://www\.homes\.com/real-estate-agents/([a-z0-9\-]*)"', re.I)
_BLOCKED = ("Access Denied", "edgesuite", "Attention Required", "Pardon Our Interruption")
_CHALLENGE = ("Powered and protected by", "chlgeId", "privacywall",
              "Checking your browser", "cf-chl")

# 目录页只要有 HTML 就能出人，图片/CSS/字体是白烧的流量和白送的请求数。
# 住宅代理按流量算钱，Akamai 按请求数限速，屏蔽它们同时省两样。
# 两个坑：① Chrome 只认 URLPattern 写法（*://*/*.jpg），裸 glob "*.jpg" 直接被拒；
# ② 绝不能屏蔽 homes.com 自己的 JS —— 那张 2.4KB 挑战页靠脚本 location.reload 才过得去，
#    把 *.js 全屏蔽会让每一页都变成永远过不去的挑战页。所以第三方脚本只按域名屏蔽。
_LITE_BLOCK = ["*://*/*.jpg", "*://*/*.jpeg", "*://*/*.png", "*://*/*.webp",
               "*://*/*.gif", "*://*/*.svg", "*://*/*.ico", "*://*/*.avif",
               "*://*/*.bmp", "*://*/*.css", "*://*/*.woff", "*://*/*.woff2",
               "*://*/*.ttf", "*://*/*.otf", "*://*/*.eot", "*://*/*.mp4",
               "*://*/*.webm", "*://*/*.map",
               "*://*.google-analytics.com/*", "*://*.googletagmanager.com/*",
               "*://*.doubleclick.net/*", "*://*.facebook.net/*",
               "*://*.amplitude.com/*", "*://*.segment.com/*", "*://*.hotjar.com/*",
               "*://*.clarity.ms/*", "*://*.newrelic.com/*", "*://*.adobedtm.com/*",
               "*://*.typekit.net/*", "*://*.gstatic.com/*", "*://*.chairsni.se/*"]


def with_session(proxy, session):
    """同一个代理账号换个 session 参数就是另一个住宅 IP（LinkUp 实测：
    ...-session-a1b2c3 → 47.141.187.7，...-session-d4e5f6 → 73.137.13.162）。
    写一次 URL 就能一直换，不用另外买账号、也不额外按次收费。"""
    value = str(proxy or "").strip()
    if not value or "@" not in value:
        return value
    parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
    user = urllib.parse.unquote(parsed.username or "")
    if not user:
        return value
    if "{session}" in user:
        return value
    if "session-" in user:
        new_user = re.sub(r"session-[0-9a-zA-Z]{1,32}", "session-" + session, user)
    else:
        new_user = f"{user}-session-{session}"
    pw = urllib.parse.quote(parsed.password or "", safe="")
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme or 'http'}://{urllib.parse.quote(new_user, safe='')}:{pw}@{host}"


# homes.com 的州页 slug 就是州名小写；加上 DC 共 51 个入口
US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new-hampshire", "new-jersey", "new-mexico", "new-york",
    "north-carolina", "north-dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode-island", "south-carolina", "south-dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west-virginia", "wisconsin", "wyoming", "district-of-columbia",
]


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS slugs (
                slug TEXT PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                pages_total INTEGER DEFAULT 0,
                page_cursor INTEGER DEFAULT 1,
                agents_found INTEGER DEFAULT 0,
                agents_saved INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                name TEXT,
                phone TEXT,
                brokerage TEXT,
                sales INTEGER,
                gender TEXT,
                slug TEXT,
                url TEXT,
                saved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_slug_status ON slugs(status);
            CREATE INDEX IF NOT EXISTS idx_agent_phone ON agents(phone);
        """)


def norm_phone(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return raw.strip()


def slug_url(slug, page=1):
    base = f"{HOMES_HOST}{DIRECTORY}{slug}/"
    return base if page <= 1 else f"{base}p{page}/"


def parse_cards(html):
    """一页 48 张卡，返回 [{key,name,phone,brokerage,sales}]，按页面顺序。"""
    out, seen = [], set()
    for chunk in _SPLIT.split(html)[1:]:
        k = _KEY.search(chunk)
        n = _NAME.search(chunk)
        if not k or not n:
            continue
        key = k.group(1)
        if key in seen:
            continue
        seen.add(key)
        c = _COMPANY.search(chunk)
        p = _PHONE.search(chunk)
        s = _SALES.search(chunk)
        phone = norm_phone(p.group(1)) if p else ""
        out.append({"key": key, "name": n.group(1).strip(), "phone": phone,
                    "brokerage": (c.group(1).strip() if c else ""),
                    "sales": int(s.group(1).replace(",", "")) if s else 0})
    return out


def parse_pages_total(html):
    m = _PAGE_RANGE.search(html)
    if m:
        return int(m.group(1).replace(",", ""))
    nums = [int(x) for x in _DATA_PAGE.findall(html)]
    return max(nums) if nums else 1


def parse_slugs(html):
    return [s for s in dict.fromkeys(_SLUG_HREF.findall(html))
            if s != HUB and not s.endswith(".xml")]


def blocked(html):
    return any(k in html for k in _BLOCKED)


def challenged(html):
    return len(html) < 60000 and any(k in html for k in _CHALLENGE)


class Gate:
    """只在真被拒绝时才退避；冷却随时长自动衰减，并发请求错开时间点。

    旧写法有两个死结：streak 只有“成功一页”才降，可被拦时永远不会有成功，
    于是冷却一路顶到 600 秒下不来；而所有标签页共用同一个 until，到点一起冲，
    正好是最像机器人的突发流量。
    """

    def __init__(self, base=20.0, cap=150.0, decay=180.0, jitter=3.0):
        self.base = base
        self.cap = cap
        self.decay = decay
        self.jitter = jitter
        self.until = 0.0
        self.streak = 0
        self.trips = 0
        self.last_trip = 0.0

    def trip(self):
        now = time.monotonic()
        if now - self.last_trip > self.decay:
            self.streak = 0
        self.trips += 1
        self.streak += 1
        self.last_trip = now
        cool = min(self.cap, self.base * (2 ** min(self.streak - 1, 3)))
        self.until = max(self.until, now + cool)
        return cool

    def pass_(self):
        if time.monotonic() - self.last_trip > self.decay:
            self.streak = 0
        else:
            self.streak = max(0, self.streak - 1)

    async def wait(self, stop=None):
        while self.until > time.monotonic():
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(min(3.0, self.until - time.monotonic()))
        await asyncio.sleep(random.uniform(0, self.jitter))


async def _load(tab, url, wait, tries, gate, stop, st=None):
    """取一页。挑战页不算被拦：原地等它自己 reload（实测 4~25 秒），最多 tries 次。

    连续被硬拒绝到 st["rotate_after"] 次就不再干等冷却——同一个出口 IP 等下去也不会通，
    直接举旗让上层重开浏览器换 session（= 换出口 IP）。
    """
    html = ""
    for attempt in range(tries):
        if stop is not None and stop.is_set():
            return None
        await gate.wait(stop)
        if stop is not None and stop.is_set():
            return None
        try:
            await tab.get(url)
        except Exception:
            await asyncio.sleep(2 + attempt * 3)
            continue
        await asyncio.sleep(wait)
        html = await tab.get_content()
        if not blocked(html) and len(html) > 60000:
            gate.pass_()
            if st is not None:
                st["block_streak"] = 0
            return html
        if blocked(html):
            gate.trip()
            if st is not None:
                st["block_streak"] += 1
                if st["block_streak"] >= st["rotate_after"]:
                    st["rotate"].set()
                    return html
            await asyncio.sleep(3 + attempt * 4)
            continue
        # 没到 6 万字节又不是硬拒绝 = 挑战页或还在渲染，等它自己过，不 trip
        await asyncio.sleep(wait)
    return html


def is_fallback(html):
    """纯数字/畸形路径不会 404，网站返回通用搜索页（canonical 指回目录根），
    那 24 张卡不属于任何地区，直接存会把别人的经纪人记到这个 slug 名下。"""
    m = _CANON.search(html)
    return bool(m) and not m.group(1)


def import_slugs(slugs):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(s.strip().lower().strip("/"), now) for s in slugs
            if re.match(r"^[a-z0-9][a-z0-9\-]{1,70}$", s.strip().lower().strip("/"))]
    if not rows:
        return 0
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            con.executemany(
                "INSERT OR IGNORE INTO slugs(slug, status, updated_at) VALUES(?, 'pending', ?)",
                rows)
    return len(rows)


def _add_discovered(found):
    """新出现的 slug 写入待抓队列；已在表里（含已抓完的）一律跳过，
    所以 州->市->相邻市 不会互相死循环。返回真正新增的 slug 列表。"""
    if not found:
        return []
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            have = {r[0] for r in con.execute("SELECT slug FROM slugs")}
            new = [s for s in found if s not in have]
            con.executemany(
                "INSERT OR IGNORE INTO slugs(slug, status, updated_at) VALUES(?, 'pending', ?)",
                [(s, now) for s in new])
    return new


def get_pending_slugs(limit=1000000):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT slug FROM slugs WHERE status='pending' ORDER BY slug LIMIT ?",
            (limit,)).fetchall()
    return [r[0] for r in rows]


def reset_incomplete():
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            keep = "status='done' OR status='invalid'"
            n = con.execute(
                f"SELECT COUNT(*) FROM slugs WHERE NOT ({keep})").fetchone()[0]
            con.execute(f"UPDATE slugs SET status='pending' WHERE NOT ({keep})")
    return n


def queue_items(slugs):
    """被拦中断的 slug 带上次翻到的页码，重跑时从那一页继续。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = dict(con.execute("SELECT slug, page_cursor FROM slugs"))
    return [(s, max(1, int(cur.get(s) or 1))) for s in slugs]


def stats():
    with sqlite3.connect(DB_PATH) as con:
        def one(sql):
            return con.execute(sql).fetchone()[0]
        return {
            "pending": one("SELECT COUNT(*) FROM slugs WHERE status='pending'"),
            "done": one("SELECT COUNT(*) FROM slugs WHERE status='done'"),
            "failed": one("SELECT COUNT(*) FROM slugs WHERE status='failed'"),
            "invalid": one("SELECT COUNT(*) FROM slugs WHERE status='invalid'"),
            "agents": one("SELECT COUNT(*) FROM agents"),
            "phones": one("SELECT COUNT(*) FROM agents WHERE phone!=''"),
        }


def known_keys():
    with sqlite3.connect(DB_PATH) as con:
        return {r[0] for r in con.execute("SELECT key FROM agents")}


def _save_many(rows):
    """返回真正新插入的 key 集合（并发下同一人可能已被别的标签页写进去）。"""
    if not rows:
        return set()
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            fresh = set()
            for r in rows:
                cur = con.execute(
                    "INSERT OR IGNORE INTO agents(key, name, phone, brokerage, sales,"
                    " gender, slug, url, saved_at) VALUES(?,?,?,?,?,?,?,?,?)", r)
                if cur.rowcount:
                    fresh.add(r[0])
            return fresh


def _set_slug_status(slug, status, pages_total=0, found=0, saved=0, page=None):
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "UPDATE slugs SET status=?, pages_total=?, agents_found=?, agents_saved=?, "
                "page_cursor=?, updated_at=? WHERE slug=?",
                (status, pages_total, found, saved, page or 1, now, slug))


def _set_cursor(slug, page):
    """每翻完一页就落盘，中途关窗口也能从下一页续。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("UPDATE slugs SET page_cursor=?, updated_at=? WHERE slug=?",
                        (page, now, slug))


async def _lite_tab(tab):
    """目录页只要 HTML 就能出人，图片/CSS/字体/JS 全是白烧的流量和白送的请求数。"""
    from nodriver.cdp import network as nw
    try:
        await tab.send(nw.set_blocked_ur_ls(
            [nw.BlockPattern(url_pattern=p, block=True) for p in _LITE_BLOCK]))
        return True
    except Exception:
        return False


async def _worker(slot, browser, queue, progress_cb, st, stop, wait, pace,
                  drop_female, discover, lite=True):
    tab = await browser.get("about:blank", new_tab=True)
    if lite:
        ok = await _lite_tab(tab)
        if not ok and not st["lite_warned"]:
            st["lite_warned"] = True
            progress_cb("⚠ 屏蔽子资源没挂上（这台机器的浏览器不认 CDP 规则），"
                        "这一轮会连图片脚本一起拉，多花流量")
    while not stop.is_set():
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            if st["inflight"]:
                # 别人还在跑，它可能马上翻出新地区，先别退
                await asyncio.sleep(1.0)
                continue
            break
        slug, start_page = item
        st["inflight"].add(slug)
        try:
            await _do_slug(tab, st, slug, start_page, progress_cb, stop,
                           wait, pace, drop_female, discover)
        except Exception as e:
            progress_cb(f"[W{slot}] {slug} 异常: {e}")
            _set_slug_status(slug, "failed", page=st["cursor"].get(slug, start_page))
        finally:
            st["inflight"].discard(slug)
    try:
        await tab.close()
    except Exception:
        pass


async def _do_slug(tab, st, slug, page, progress_cb, stop, wait, pace, drop_female, discover):
    found = saved = known = fem = nop = dup = 0
    pages_total = 0
    empty_try = 0
    blocked_out = stopped = invalid = stuck = empty_out = False
    new_slugs = set()
    while page <= MAX_PAGE and not stop.is_set():
        html = await _load(tab, slug_url(slug, page), wait, 6, st["gate"], stop, st)
        if html is None:
            stopped = True
            break
        if blocked(html):
            if st["rotate"].is_set():
                # 这个出口 IP 已经废了，别把它记成 failed，回队尾等下一个 IP
                _set_slug_status(slug, "pending", pages_total, found, saved, page=page)
                stopped = True
                break
            st["blocks"] += 1
            _set_slug_status(slug, "failed", pages_total, found, saved, page=page)
            blocked_out = True
            break
        if len(html) <= 60000:
            # 挑战页始终没自己过：这不是限速，退避也没用，排回队尾等下一轮
            st["challenges"] += 1
            _set_slug_status(slug, "pending", pages_total, found, saved, page=page)
            stuck = True
            break
        if not pages_total:
            pages_total = parse_pages_total(html)
        if discover:
            new_slugs.update(parse_slugs(html))
        if is_fallback(html):
            _set_slug_status(slug, "invalid", 0, 0, 0, page=1)
            invalid = True
            break
        cards = parse_cards(html)
        if len(cards) < PER_PAGE and page < (pages_total or page):
            # 不满 48 张不一定是尾页：网站会把页面截断渲染，先重进两次
            if empty_try < 2:
                empty_try += 1
                await asyncio.sleep(wait + 2)
                continue
            # 重进完还是缺，而网站明明报了更多页 → 不能当抓完，排回队尾等下一轮
            st["empties"] += 1
            _set_slug_status(slug, "pending", pages_total, found, saved, page=page)
            empty_out = True
            break
        empty_try = 0
        found += len(cards)
        st["pages"] += 1
        st["cursor"][slug] = page + 1
        _set_cursor(slug, page + 1)
        url = slug_url(slug, page)
        rows = []
        for c in cards:
            key = c["key"]
            if key in st["seen"]:
                known += 1
                continue
            st["seen"].add(key)
            if drop_female and is_obvious_female(c["name"]):
                fem += 1
                continue
            rows.append((key, c["name"], c["phone"], c["brokerage"], c["sales"],
                         name_gender(c["name"]), slug, url,
                         datetime.now().isoformat(timespec="seconds")))
        cand = len(rows)
        fresh = _save_many(rows)
        saved += len(fresh)
        dup += cand - len(fresh)
        page_nop = sum(1 for r in rows if r[0] in fresh and not r[2])
        nop += page_nop
        st["agents"] += len(fresh)
        st["phones"] += sum(1 for r in rows if r[0] in fresh and r[2])
        st["nophone"] += page_nop
        if page >= (pages_total or 1):
            break
        page += 1
        await asyncio.sleep(pace)
    added = 0
    if discover:
        for s in _add_discovered(new_slugs - {slug}):
            if s in st["queued"]:
                continue
            st["queued"].add(s)
            st["queue"].put_nowait((s, 1))
            added += 1
        st["slugs_total"] += added
        st["new_slugs"] += added
    st["slugs_done"] += 1
    resumed = stopped or stuck or empty_out
    if stopped:
        _set_slug_status(slug, "pending", pages_total, found, saved, page=page)
    elif not (blocked_out or invalid or stuck or empty_out):
        _set_slug_status(slug, "done", pages_total, found, saved, page=1)
    if resumed and st["rotate"].is_set():
        # 这一轮是被"换出口 IP"打断的，活儿得还给队列，下一轮新 IP 接着跑
        st["queue"].put_nowait((slug, page))
    note = (f" | 进度 {st['slugs_done']}/{st['slugs_total']} "
            f"新发现{added}个地区 队列剩{st['queue'].qsize()}")
    if stopped:
        progress_cb(f"■ {slug}: 按要求停止于第{page}页 → 已存{saved}人（下次从第{page}页续）{note}")
        return
    if blocked_out:
        progress_cb(f"✗ {slug}: 第{page}页被硬拒绝（Access Denied）→ 已存{saved}人"
                    f"（下次从第{page}页续）{note}")
        return
    if stuck:
        progress_cb(f"⏳ {slug}: 第{page}页挑战页{int(wait)*6}秒内没自己过 → 已存{saved}人，"
                    f"已排回队尾不算失败{note}")
        return
    if empty_out:
        progress_cb(f"↪ {slug}: 第{page}页只有{len(cards)}张卡（网站说有{pages_total}页），"
                    f"重进2次还是缺 → 已存{saved}人，排回队尾下次从第{page}页续{note}")
        return
    if invalid:
        progress_cb(f"⚠ {slug}: 不是地区专页（网站返回通用搜索页）→ 存0人{note}")
        return
    progress_cb(f"✓ {slug}: 共{pages_total}页 卡片{found}张 → 存{saved}人（无号码{nop}）"
                f"| 跳过 女:{fem} 已抓过:{known} 库里重复:{dup}{note}")


async def _heartbeat(st, progress_cb, stop, started):
    while not stop.is_set():
        await asyncio.sleep(60)
        mins = (time.monotonic() - started) / 60 or 1
        rate = st["agents"] / mins
        mb = st["mb"] + (st["relay"].bytes_transferred / 1e6 if st["relay"] else 0)
        left = st["gate"].until - time.monotonic()
        cool = f" 冷却中{left:.0f}s" if left > 0 else ""
        progress_cb(f"心跳: 地区 {st['slugs_done']}/{st['slugs_total']} "
                    f"页{st['pages']} 存{st['agents']}人（有号{st['phones']} 无号{st['nophone']}）"
                    f" {rate:.1f}人/分 并发{len(st['inflight'])}个{cool} "
                    f"硬拒绝{st['blocks']}次 挑战没过{st['challenges']}次 空页{st['empties']}次 "
                    f"退避{st['gate'].trips}次 换出口IP{st['exits']}次 新地区{st['new_slugs']}个 "
                    f"过代理{mb:.0f}MB")


class _RoundStop:
    """一轮 = 一个浏览器 = 一个出口 IP。用户点停止或被判定该换 IP 都算这一轮结束。"""

    def __init__(self, user_stop, rotating):
        self.user_stop = user_stop
        self.rotating = rotating

    def is_set(self):
        return self.user_stop.is_set() or self.rotating.is_set()


async def _run_async(slugs, concurrency, progress_cb, stop, wait, pace,
                     discover, drop_female, proxy, proxy_pass, rotate_ip, lite,
                     rotate_after=6):
    exe = find_browser()
    if not exe:
        raise RuntimeError("既没在本机找到 Chrome/Edge/Brave，交付文件夹里的 browser 目录"
                           "也没有自带的 Chromium，起不了浏览器。在「抓取设置 → 浏览器路径」"
                           "里手动指一个（查过 " + describe_search() + "）")
    progress_cb(f"浏览器：{exe}（{browser_kind(exe)}）")
    queue = asyncio.Queue()
    items = queue_items(slugs)
    for item in items:
        queue.put_nowait((item[0], item[1]))
    st = {"browser": None, "queue": queue, "seen": known_keys(), "slugs_done": 0,
          "slugs_total": len(items), "agents": 0, "phones": 0, "nophone": 0,
          "pages": 0, "blocks": 0, "challenges": 0, "empties": 0, "new_slugs": 0,
          "queued": {s for s, _ in items},
          "inflight": set(), "cursor": {}, "gate": Gate(),
          "rotate": asyncio.Event(), "block_streak": 0,
          "rotate_after": rotate_after if (proxy.strip() and rotate_ip) else 1 << 30,
          "exits": 0, "dead_rounds": 0, "agents_at_round_start": 0,
          "lite_warned": False, "relay": None, "mb": 0.0}
    n_workers = max(1, int(concurrency))
    if proxy.strip():
        how = "被连续硬拒绝就重开浏览器换出口 IP" if rotate_ip else "出口 IP 固定不换"
    else:
        how = "本机直连，没有第二个出口 IP 可换"
    progress_cb(f"引擎就绪：并发 {n_workers} 个标签页，"
                f"{'自动发现下级地区' if discover else '只跑给定地区'}，"
                f"{'剔除女性姓名' if drop_female else '男女都留'}，{how}，"
                f"{'不拉图片样式' if lite else '图片样式照拉'}")
    started = time.monotonic()
    rnd_no = 0
    while not stop.is_set():
        rnd_no += 1
        st["rotate"].clear()
        st["block_streak"] = 0
        relay = None
        args = []
        if proxy.strip():
            sid = "%06x" % random.getrandbits(24)
            try:
                relay = start_relay(with_session(proxy, sid), proxy_pass, sid)
            except Exception as e:
                raise RuntimeError(f"代理填法不对：{e}（该写 http://user:pass@host:port）")
            args = [f"--proxy-server={relay.server}"]
            progress_cb(f"出口：走代理（本地中继补认证，session={sid}）"
                        if rnd_no == 1 else f"♻ 第{rnd_no}轮换出口 IP（session={sid}）")
        else:
            progress_cb("出口：本机直连（实测泰国出口已被 homes.com 边缘 403，"
                        "连 robots.txt 都拒；被拦只能停手等回血）"
                        if rnd_no == 1 else f"♻ 第{rnd_no}轮重开浏览器再试（还是本机出口）")
        browser = await uc.start(headless=False, browser_executable_path=exe,
                                 sandbox=not is_bundled(exe), browser_args=args)
        st["browser"] = browser
        st["relay"] = relay
        rnd = _RoundStop(stop, st["rotate"])
        await _warm_up(browser, progress_cb, rnd)
        beat = asyncio.ensure_future(_heartbeat(st, progress_cb, rnd, started))
        try:
            tasks = [_worker(i, browser, queue, progress_cb, st, rnd, wait, pace,
                             drop_female, discover, lite) for i in range(n_workers)]
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            beat.cancel()
            st["relay"] = None
            if relay:
                st["mb"] += relay.bytes_transferred / 1e6
            try:
                browser.stop()
            except Exception:
                pass
            if relay:
                relay.stop()
        if stop.is_set() or not st["rotate"].is_set():
            break
        if proxy.strip() and not rotate_ip:
            break
        if queue.empty():
            # 举了旗但没活儿了（被中断的地区都已回队列）→ 收工
            break
        # 换了 IP 一个人还是抢不到 = 不是某个 IP 的问题（代理到期/账号被打标），
        # 继续换就是白烧流量，到点收手。
        if st["agents"] == st.get("agents_at_round_start"):
            st["dead_rounds"] += 1
        else:
            st["dead_rounds"] = 0
        st["agents_at_round_start"] = st["agents"]
        if proxy.strip() and st["dead_rounds"] >= 8:
            progress_cb(f"⚠ 连换 {st['exits']} 个出口 IP 都一个人没抢到，先停手。"
                        "这不是单个 IP 的问题（多半是代理账号到期/被打标），"
                        "再换下去只是白烧流量")
            break
        st["exits"] += 1
        if proxy.strip():
            await asyncio.sleep(2)
            continue
        # 直连只有本机这一个出口 IP，换不了 —— 硬撞只会把封窗口越拖越长
        # （实测 1 个标签页撞 700 秒：0 页 0 人，退避从 2 次涨到 7 次）。
        if st["exits"] >= 4:
            progress_cb("⚠ 直连重开 4 轮浏览器还是进不去，先停在这里。"
                        "过几小时再点开始（会接着上次那页跑），或填上代理出口")
            break
        nap = 300 * st["exits"]
        progress_cb(f"⚠ 直连被硬拒绝且没有第二个出口 IP → 停 {nap//60} 分钟再重开浏览器试探"
                    f"（期间一个请求都不发，不烧流量）")
        left = nap
        while left > 0 and not stop.is_set():
            await asyncio.sleep(min(15, left))
            left -= 15
    mins = (time.monotonic() - started) / 60 or 1
    if proxy.strip():
        progress_cb(f"过代理流量合计 {st['mb']:.0f}MB：{st['pages']}页 → "
                    f"每页 {st['mb'] * 1000 / max(1, st['pages']):.0f}KB；"
                    f"新增 {st['agents']}人 → 每人 {st['mb'] * 1000 / max(1, st['agents']):.0f}KB"
                    f"（用时 {mins:.0f} 分钟）")
    else:
        progress_cb(f"本机直连不计流量：{st['pages']}页 / 新增{st['agents']}人"
                    f"（用时 {mins:.0f} 分钟）")


async def _warm_up(browser, progress_cb, stop):
    """先打一次首页：挑战页要浏览器先拿到 Akamai 的 cookie 才会自己 reload 过，
    实测冷启动直接打目录页要等 24 秒，先过首页后第 1 次轮询就出内容。"""
    tab = await browser.get(HOMES_HOST + "/")
    for _ in range(8):
        await asyncio.sleep(3)
        if stop.is_set():
            return
        html = await tab.get_content()
        if len(html) > 60000:
            progress_cb(f"首页预热通过（{len(html):,} 字节）")
            break
    else:
        progress_cb("⚠ 首页 24 秒没出内容，这一轮多半进不去，看日志末尾的出口 IP")
    try:
        await tab.get("https://ipinfo.io/json")
        await asyncio.sleep(4)
        body = (await tab.get_content())[:400]
        m = re.search(r'"ip"\s*:\s*"([^"]+)"', body)
        c = re.search(r'"country"\s*:\s*"([^"]+)"', body)
        progress_cb(f"当前出口 IP：{m.group(1) if m else '未知'} "
                    f"{c.group(1) if c else ''}")
    except Exception:
        pass
    await tab.get(HOMES_HOST + "/")


def run_batch(slugs, concurrency=6, progress_cb=None, stop=None, wait=6.0, pace=0.4,
              discover=True, drop_female=True, proxy="", proxy_pass="",
              rotate_ip=True, lite=True):
    """在后台线程里调用；1 个浏览器 + concurrency 个标签页。"""
    slugs = [s.strip().lower().strip("/") for s in dict.fromkeys(slugs)
             if re.match(r"^[a-z0-9][a-z0-9\-]{1,70}$", s.strip().lower().strip("/"))]
    if not slugs:
        return 0
    # 先落表，否则 _set_slug_status 的 UPDATE 打不到任何行，断点页码和统计全是空的
    import_slugs(slugs)
    progress_cb = progress_cb or (lambda m: None)

    def safe_cb(msg, _cb=progress_cb):
        try:
            _cb(msg)
        except Exception:
            pass

    stop = stop or threading.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(
            slugs, int(concurrency), safe_cb, stop, float(wait), float(pace),
            bool(discover), bool(drop_female), str(proxy or ""), str(proxy_pass or ""),
            bool(rotate_ip), bool(lite)))
    finally:
        loop.close()
    return len(slugs)


def export_csv(output_path=None):
    if output_path is None:
        output_path = BASE / f"homes_agents_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT slug, name, phone, brokerage, sales, gender, url, saved_at "
            "FROM agents ORDER BY slug, name").fetchall()
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["地区", "姓名", "电话", "经纪公司", "成交套数", "性别", "链接", "抓取时间"])
        for r in rows:
            w.writerow(list(r))
    return output_path, len(rows)
