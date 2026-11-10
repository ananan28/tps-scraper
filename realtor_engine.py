# -*- coding: utf-8 -*-
"""realtor.com 房地产经纪人抓取引擎。
nodriver 绕过 Kasada；本机直连，无需代理/验证码。
翻页用 URL 路径 /realestateagents/zip/{ZIP}/pg-{N}（?page=N 网站不认）。
"""
import asyncio
import csv
import random
import re
import sqlite3
import threading
import time
from datetime import datetime

import nodriver as uc

import engine_core as eng
from engine_core import BASE, is_obvious_female, name_gender

DB_PATH = BASE / "realtor_data.db"
REALTOR_HOST = "https://www.realtor.com"
ZIP_RE = re.compile(r"^\d{5}$")
PER_PAGE = 12
MAX_PAGE = 200
BLOCKS_PER_ROTATE = 3

_lock = threading.Lock()

# 列表页卡片：id+姓名来自 data-card-link 链接，经纪公司地址来自 trunc span
_CARD_SPLIT = re.compile(r'data-testid="agent-card-v2"')
_CARD_LINK = re.compile(
    r'href="/realestateagents/([0-9a-f]{20,})"[^>]*aria-label="View ([^"]*)"')
_CARD_BROKER = re.compile(r'trunc_true max-w_100% mt_100">([^<]{2,120})<')
_TOTAL = re.compile(r'"matching_rows":(\d+)')
_TEL = re.compile(r'href="tel:([^"]+)"')
_BLOCKED = ("taking longer", "Attention Required", "Access Denied")


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS zips (
                zip TEXT PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                agents_found INTEGER DEFAULT 0,
                agents_saved INTEGER DEFAULT 0,
                page_cursor INTEGER DEFAULT 1,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zip TEXT,
                agent_id TEXT,
                name TEXT,
                phone TEXT,
                brokerage TEXT,
                gender TEXT,
                url TEXT,
                saved_at TEXT,
                UNIQUE(agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_zip_status ON zips(status);
            CREATE INDEX IF NOT EXISTS idx_agent_phone ON agents(phone);
        """)
        cols = {r[1] for r in con.execute("PRAGMA table_info(zips)")}
        if "page_cursor" not in cols:
            con.execute("ALTER TABLE zips ADD COLUMN page_cursor INTEGER DEFAULT 1")


def zip_queue_items(zips):
    """被拦中断的 ZIP 带上次翻到的页码，重跑时从那一页继续。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = dict(con.execute("SELECT zip, page_cursor FROM zips"))
    return [(z, max(1, int(cur.get(z) or 1))) for z in zips]


def import_zips(zips):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(z.strip(), now) for z in zips if ZIP_RE.match(z.strip())]
    if not rows:
        return 0
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            con.executemany(
                "INSERT OR IGNORE INTO zips(zip, status, updated_at) VALUES(?, 'pending', ?)",
                rows)
    return len(rows)


def get_pending_zips(limit=1000000):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT zip FROM zips WHERE status='pending' ORDER BY zip LIMIT ?",
            (limit,)).fetchall()
    return [r[0] for r in rows]


def reset_incomplete():
    """把 failed / 中断的 ZIP 重新排队。"""
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            n = con.execute("SELECT COUNT(*) FROM zips WHERE status!='done'").fetchone()[0]
            con.execute("UPDATE zips SET status='pending' WHERE status!='done'")
    return n


def stats():
    with sqlite3.connect(DB_PATH) as con:
        def one(sql):
            return con.execute(sql).fetchone()[0]
        return {
            "pending": one("SELECT COUNT(*) FROM zips WHERE status='pending'"),
            "done": one("SELECT COUNT(*) FROM zips WHERE status='done'"),
            "failed": one("SELECT COUNT(*) FROM zips WHERE status='failed'"),
            "agents": one("SELECT COUNT(*) FROM agents"),
            "phones": one("SELECT COUNT(*) FROM agents WHERE phone!=''"),
        }


def known_agent_ids():
    """已抓过的经纪人，全局去重（同一人出现在多个 ZIP 只抓一次）。"""
    with sqlite3.connect(DB_PATH) as con:
        return {r[0] for r in con.execute("SELECT agent_id FROM agents")}


def _set_zip_status(zip_code, status, found=0, saved=0, page=None):
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            if page is None:
                con.execute(
                    "UPDATE zips SET status=?, agents_found=?, agents_saved=?, updated_at=? WHERE zip=?",
                    (status, found, saved, now, zip_code))
            else:
                con.execute(
                    "UPDATE zips SET status=?, agents_found=?, agents_saved=?, "
                    "page_cursor=?, updated_at=? WHERE zip=?",
                    (status, found, saved, page, now, zip_code))


def _save_agent(agent_id, zip_code, name, phone, brokerage, gender, url):
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO agents(zip, agent_id, name, phone, brokerage, gender, url, saved_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (zip_code, agent_id, name, phone, brokerage, gender, url, now))
            return cur.rowcount > 0


def parse_cards(html):
    """返回 [{agent_id, name, brokerage}]，按页面出现顺序。"""
    out, seen = [], set()
    for chunk in _CARD_SPLIT.split(html)[1:]:
        m = _CARD_LINK.search(chunk)
        if not m:
            continue
        aid, name = m.group(1), m.group(2).strip()
        if name.startswith("View "):
            name = name[5:].strip()
        if name.lower().endswith(" profile"):
            name = name[:-len(" profile")].strip()
        if aid in seen:
            continue
        seen.add(aid)
        b = _CARD_BROKER.search(chunk)
        out.append({"agent_id": aid, "name": name,
                    "brokerage": (b.group(1).strip() if b else "")})
    return out


def parse_total(html):
    m = _TOTAL.search(html)
    return int(m.group(1)) if m else 0


def parse_phone(html):
    m = _TEL.search(html)
    return m.group(1).strip() if m else ""


def blocked(html):
    return any(k in html for k in _BLOCKED)


async def _load(tab, url, wait=8, retries=3, gate=None, stop=None):
    html = ""
    for attempt in range(retries):
        if gate:
            await gate.wait(stop)
        try:
            await tab.get(url)
        except Exception:
            await asyncio.sleep(2 + attempt * 3)
            continue
        await asyncio.sleep(wait)
        html = await tab.get_content()
        if not blocked(html) and len(html) > 40000:
            if gate:
                gate.recover()
            return html
        if gate:
            # 所有标签页共用一个闸门：一处被拦，全体停手等封锁自己过期
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


class Lane:
    """一条出口通道 = 一个浏览器实例 = 一个 IP。代理下换 session 就是换 IP。"""

    def __init__(self, idx, proxy_template="", proxy_pass="", tabs=4, session=None):
        self.idx = idx
        self.proxy_template = proxy_template
        self.proxy_pass = proxy_pass
        self.tabs = tabs
        self.session = session or str(random.randrange(100000, 999999))
        self.relay = None
        self.browser = None
        self.gate = Gate()
        self.blocked = 0
        self.busy = 0
        self.rotate_pending = False
        self.rotating = asyncio.Lock()

    def _args(self):
        if not self.proxy_template:
            return None
        self.relay = eng.start_relay(self.proxy_template, self.proxy_pass, self.session)
        return [f"--proxy-server=127.0.0.1:{self.relay.port}"]

    async def start(self):
        args = self._args()
        self.browser = (await uc.start(headless=False, browser_args=args)
                        if args else await uc.start(headless=False))

    async def do_rotate(self):
        """换新 session（新住宅 IP），浏览器实例重建。同一条通道上只允许一个人做。"""
        async with self.rotating:
            if not self.rotate_pending:
                return
            self.rotate_pending = False
            self.blocked = 0
            self.gate.streak = 0
            self.gate.until = 0.0
            try:
                self.browser.stop()
            except Exception:
                pass
            if self.relay:
                self.relay.stop()
                self.relay = None
            self.session = str(random.randrange(100000, 999999))
            await self.start()

    def stop(self):
        for closer in (getattr(self.browser, "stop", None),
                       getattr(self.relay, "stop", None) if self.relay else None):
            try:
                if closer:
                    closer()
            except Exception:
                pass


async def _worker(slot, lane, queue, progress_cb, stats, stop, delay, pace):
    tab = await lane.browser.get("about:blank", new_tab=True)
    while not stop.is_set():
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        zip_code, start_page = item
        if lane.rotate_pending:
            await lane.do_rotate()
            tab = await lane.browser.get("about:blank", new_tab=True)
        lane.busy += 1
        try:
            await _do_zip(tab, lane, zip_code, start_page, progress_cb, stats,
                           stop, delay, pace)
        except Exception as e:
            progress_cb(f"[W{slot}] {zip_code} 异常: {e}，已重新排队")
            _set_zip_status(zip_code, "failed", page=stats["cursor"].get(zip_code, 1))
            queue.put_nowait((zip_code, stats["cursor"].get(zip_code, 1)))
        finally:
            lane.busy -= 1
            stats["inflight"].discard(zip_code)
            queue.task_done()
    try:
        await tab.close()
    except Exception:
        pass


async def _do_zip(tab, lane, zip_code, page, progress_cb, stats, stop, delay, pace):
    saved = known = fem = found = nop = 0
    blocked_out = False
    total = None
    stats["inflight"].add(zip_code)
    while page <= MAX_PAGE and not stop.is_set():
        url = f"{REALTOR_HOST}/realestateagents/zip/{zip_code}"
        if page > 1:
            url += f"/pg-{page}"
        html = await _load(tab, url, wait=8, gate=lane.gate, stop=stop)
        if blocked(html):
            await _note_block(lane, stats, progress_cb, zip_code, page)
            _set_zip_status(zip_code, "failed", page=page)
            blocked_out = True
            break
        stats["cursor"][zip_code] = page + 1
        if total is None:
            total = parse_total(html)
        cards = parse_cards(html)
        if not cards:
            break
        found += len(cards)
        stats["pages"] += 1
        for c in cards:
            if stop.is_set():
                break
            aid = c["agent_id"]
            if aid in stats["seen"]:
                known += 1
                continue
            stats["seen"].add(aid)
            name = c["name"]
            if is_obvious_female(name):
                fem += 1
                continue
            durl = f"{REALTOR_HOST}/realestateagents/{aid}"
            dhtml = await _load(tab, durl, wait=delay, retries=2,
                                gate=lane.gate, stop=stop)
            if blocked(dhtml):
                await _note_block(lane, stats, progress_cb, zip_code, page)
                dhtml = ""
            else:
                lane.blocked = 0
            stats["bytes"] += len(dhtml)
            phone = parse_phone(dhtml) if dhtml else ""
            if _save_agent(aid, zip_code, name, phone, c["brokerage"],
                           name_gender(name), durl):
                saved += 1
                stats["agents"] += 1
                if phone:
                    stats["phones"] += 1
                else:
                    stats["nophone"] += 1
                    nop += 1
            await asyncio.sleep(pace)
        if total and found >= total:
            break
        if len(cards) < PER_PAGE:
            break
        page += 1
    stats["zips_done"] += 1
    if not blocked_out and not stop.is_set():
        _set_zip_status(zip_code, "done", found, saved, page=page)
        stats["cursor"].pop(zip_code, None)
    note = f" | 进度 {stats['zips_done']}/{stats['zips_total']} IP#{lane.idx}"
    if blocked_out:
        progress_cb(f"✗ {zip_code}: 第{page}页被拦中断 → 已存{saved}人"
                    f"（下次从第{page}页续）| 换IP{stats['rotations']}次{note}")
        return
    progress_cb(f"✓ {zip_code}: 网站{total or 0}条 → 卡片{found}张 → 存{saved}人"
                f"（无号码{nop}）| 跳过 女:{fem} 已抓过:{known}{note}")


async def _note_block(lane, stats, progress_cb, zip_code, page):
    """被拦计数：有代理就攒够次数换 IP，没代理只能靠 Gate 冷却等封锁过期。"""
    lane.blocked += 1
    stats["blocks"] += 1
    if lane.proxy_template and lane.blocked >= BLOCKS_PER_ROTATE:
        lane.rotate_pending = True
        if lane.busy <= 1:
            await lane.do_rotate()
            stats["rotations"] += 1
            progress_cb(f"[IP#{lane.idx}] 连续被拦{BLOCKS_PER_ROTATE}次，换出口 session={lane.session}")


async def _heartbeat(stats, progress_cb, stop, started):
    while not stop.is_set():
        await asyncio.sleep(60)
        mins = (time.monotonic() - started) / 60 or 1
        rate = stats["agents"] / mins
        mb = stats["bytes"] / 1024 / 1024
        cool = sum(1 for lane in stats["lanes"] if lane.gate.until > time.monotonic())
        trips = sum(lane.gate.trips for lane in stats["lanes"])
        progress_cb(f"心跳: ZIP {stats['zips_done']}/{stats['zips_total']} "
                    f"列表页{stats['pages']} 存{stats['agents']}人"
                    f"（有号{stats['phones']} 无号{stats['nophone']}）"
                    f" {rate:.1f}人/分 出口{len(stats['lanes'])}个IP 冷却中{cool} "
                    f"被拦{stats['blocks']}次 换IP{stats['rotations']}次 退避{trips}次 流量{mb:.0f}MB")


async def _run_async(zips, concurrency, progress_cb, stop, delay, pace,
                     proxy_template="", proxy_pass="", tabs_per_ip=4):
    lanes = []
    n_lanes = max(1, -(-concurrency // max(1, tabs_per_ip))) if proxy_template else 1
    for i in range(n_lanes):
        lanes.append(Lane(i, proxy_template, proxy_pass, tabs_per_ip))
    for lane in lanes:
        await lane.start()
    queue = asyncio.Queue()
    for item in zip_queue_items(zips):
        queue.put_nowait(item)
    stats = {"lanes": lanes, "seen": known_agent_ids(), "zips_done": 0,
             "zips_total": len(zips), "agents": 0, "phones": 0, "nophone": 0,
             "pages": 0, "bytes": 0, "blocks": 0, "rotations": 0,
             "inflight": set(), "cursor": {}}
    started = time.monotonic()
    beat = asyncio.ensure_future(_heartbeat(stats, progress_cb, stop, started))
    mode = (f"代理 {n_lanes} 个出口 IP × 每 IP {tabs_per_ip} 页"
            if proxy_template else "本机直连（高并发会被 Kasada 临时封）")
    progress_cb(f"引擎就绪：并发 {min(concurrency, len(zips))} 页，{mode}")
    try:
        tasks = [_worker(i, lanes[i % len(lanes)], queue, progress_cb, stats,
                         stop, delay, pace)
                 for i in range(min(concurrency, len(zips)))]
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        beat.cancel()
        for lane in lanes:
            lane.stop()


def run_batch(zips, concurrency=8, progress_cb=None, stop=None, delay=4.0, pace=1.0,
              proxy_template="", proxy_pass="", tabs_per_ip=4):
    """在后台线程里调用。直连=1 个浏览器多标签；代理=多个浏览器，每个绑一个出口 IP。"""
    zips = [z for z in dict.fromkeys(zips) if ZIP_RE.match(z)]
    if not zips:
        return 0
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
        loop.run_until_complete(
            _run_async(zips, int(concurrency), safe_cb, stop, delay, pace,
                       str(proxy_template or "").strip(), str(proxy_pass or "").strip(),
                       int(tabs_per_ip)))
    finally:
        loop.close()
    return len(zips)


def export_csv(output_path=None):
    if output_path is None:
        output_path = BASE / f"realtor_agents_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT zip, name, phone, brokerage, gender, url, saved_at FROM agents ORDER BY zip, name"
        ).fetchall()
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ZIP", "姓名", "电话", "经纪公司", "性别", "链接", "抓取时间"])
        for r in rows:
            w.writerow(list(r))
    return output_path, len(rows)
