# -*- coding: utf-8 -*-
"""homes.com 房地产经纪人抓取引擎。

 nodriver 绕过 Akamai Bot Manager（curl_cffi/requests 对深层页一律 403）；本机直连，无代理无验证码。
 翻页用 URL 路径 /real-estate-agents/{slug}/p{N}/（?page=N 与 /N/ 网站都不认）。
 列表页自带电话，一页 48 人，不需要进详情页。
"""
import asyncio
import csv
import re
import sqlite3
import threading
import time
from datetime import datetime

import nodriver as uc

from engine_core import BASE, is_obvious_female, name_gender
from browser_path import describe_search, find_browser

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


async def _load(tab, url, wait, retries, gate, stop):
    html = ""
    for attempt in range(retries):
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
            gate.recover()
            return html
        gate.trip()
        await asyncio.sleep(3 + attempt * 4)
    return html


class Gate:
    """被拦时把并发压成串行退避；恢复后逐步缩短冷却。"""

    def __init__(self, base=45.0, cap=600.0):
        self.base = base
        self.cap = cap
        self.until = 0.0
        self.streak = 0
        self.trips = 0

    def trip(self):
        self.trips += 1
        self.streak += 1
        cool = min(self.cap, self.base * (2 ** min(self.streak - 1, 4)))
        self.until = max(self.until, time.monotonic() + cool)
        return cool

    def recover(self):
        if self.streak:
            self.streak = max(0, self.streak - 1)

    async def wait(self, stop=None):
        while self.until > time.monotonic():
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(min(3.0, self.until - time.monotonic()))


async def _worker(slot, browser, queue, progress_cb, st, stop, wait, pace,
                  drop_female, discover):
    tab = await browser.get("about:blank", new_tab=True)
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
    blocked_out = stopped = invalid = False
    new_slugs = set()
    while page <= MAX_PAGE and not stop.is_set():
        html = await _load(tab, slug_url(slug, page), wait, 3, st["gate"], stop)
        if html is None:
            stopped = True
            break
        if blocked(html):
            st["blocks"] += 1
            _set_slug_status(slug, "failed", pages_total, found, saved, page=page)
            blocked_out = True
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
        if not cards:
            break
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
        if page >= pages_total or len(cards) < PER_PAGE:
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
    if stopped:
        _set_slug_status(slug, "pending", pages_total, found, saved, page=page)
    elif not (blocked_out or invalid):
        _set_slug_status(slug, "done", pages_total, found, saved, page=1)
    note = (f" | 进度 {st['slugs_done']}/{st['slugs_total']} "
            f"新发现{added}个地区 队列剩{st['queue'].qsize()}")
    if stopped:
        progress_cb(f"■ {slug}: 按要求停止于第{page}页 → 已存{saved}人（下次从第{page}页续）{note}")
        return
    if blocked_out:
        progress_cb(f"✗ {slug}: 第{page}页被拦中断 → 已存{saved}人（下次从第{page}页续）{note}")
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
        left = st["gate"].until - time.monotonic()
        cool = f" 冷却中{left:.0f}s" if left > 0 else ""
        progress_cb(f"心跳: 地区 {st['slugs_done']}/{st['slugs_total']} "
                    f"页{st['pages']} 存{st['agents']}人（有号{st['phones']} 无号{st['nophone']}）"
                    f" {rate:.1f}人/分 并发{len(st['inflight'])}个{cool} "
                    f"被拦{st['blocks']}次 退避{st['gate'].trips}次 新地区{st['new_slugs']}个")


async def _run_async(slugs, concurrency, progress_cb, stop, wait, pace,
                     discover, drop_female):
    exe = find_browser()
    if not exe:
        raise RuntimeError("本机没找到 Chrome/Edge，起不了浏览器。装个 Chrome 再跑（查过 "
                           + describe_search() + "）")
    progress_cb(f"浏览器：{exe}")
    browser = await uc.start(headless=False, browser_executable_path=exe)
    queue = asyncio.Queue()
    items = queue_items(slugs)
    for item in items:
        queue.put_nowait(item)
    st = {"browser": browser, "queue": queue, "seen": known_keys(), "slugs_done": 0,
          "slugs_total": len(items), "agents": 0, "phones": 0, "nophone": 0,
          "pages": 0, "blocks": 0, "new_slugs": 0, "queued": {s for s, _ in items},
          "inflight": set(), "cursor": {}, "gate": Gate()}
    started = time.monotonic()
    beat = asyncio.ensure_future(_heartbeat(st, progress_cb, stop, started))
    n_workers = max(1, int(concurrency))
    progress_cb(f"引擎就绪：并发 {n_workers} 个标签页，本机直连，"
                f"{'自动发现下级地区' if discover else '只跑给定地区'}，"
                f"{'剔除女性姓名' if drop_female else '男女都留'}")
    try:
        tasks = [_worker(i, browser, queue, progress_cb, st, stop, wait, pace,
                         drop_female, discover) for i in range(n_workers)]
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        beat.cancel()
        try:
            browser.stop()
        except Exception:
            pass


def run_batch(slugs, concurrency=6, progress_cb=None, stop=None, wait=6.0, pace=0.4,
              discover=True, drop_female=True):
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
            bool(discover), bool(drop_female)))
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
