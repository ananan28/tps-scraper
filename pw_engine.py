# -*- coding: utf-8 -*-
"""PropWire 地址池共用引擎（被「普通地址抓取.py」「高净值地址抓取.py」调用）。

只走本机网络 + 本机浏览器：PropWire 前面是 DataDome，本机 curl 带全套 cookie 也被打回
403 并甩 geo.captcha-delivery.com 验证码，只有真浏览器里的同源 fetch 能过，所以抓取必须在
nodriver 起的浏览器里发。不用代理、不用 Apify、不用流量。

翻页：size 实测上限 250（超过直接 422「The size must not be greater than 250」），
result_index 一路加到 result_count 为止，实测 30127 翻到第 40 页才见底。

代理：默认留空 = 走本机网络（不花钱）。填了代理就走本地 CONNECT 中继（proxy_relay），
浏览器只连 127.0.0.1，由中继补代理账号密码——nodriver 直接把账号密码放浏览器参数里
Chromium 是不认的。

有人住：类型只勾住宅六种（sfr/condo/mfh_2_to_4/mfh_5_plus/mobile/other），
land（空地）和 commercial（商业）根本不查；返回里 lead_type.vacant_home/vacant_lot
为真的再剔掉。
"""
import argparse
import asyncio
import csv
import gc
import io
import json
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.request
import warnings
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from proxy_relay import LocalProxy, parse_proxy_lines

LOCK = threading.Lock()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
GEONAMES = "https://download.geonames.org/export/zip/US.zip"
STATES = sorted("AK AL AR AZ CA CO CT DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS "
                "MT NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY".split())
PW_TYPES = ["sfr", "condo", "mfh_2_to_4", "mfh_5_plus", "mobile", "other"]
PW_SIZE = 250
PW_HOME = "https://propwire.com/search"

_local = threading.local()


def now_ts():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


# 同一栋房子在 PropWire 里可能写成 "123 Main Street" 也可能写成 "123 Main St"，
# 地址原样入库就会算成两条。去重用的键先把下面这些叫法归成一个，再拿整串当键。
_ABBR = {
    "street": "st", "streets": "st", "avenue": "ave", "avenues": "ave", "av": "ave",
    "boulevard": "blvd", "boulevarde": "blvd", "drive": "dr", "drives": "dr",
    "court": "ct", "courts": "ct", "place": "pl", "places": "pl", "lane": "ln",
    "lanes": "ln", "road": "rd", "roads": "rd", "trail": "trl", "trails": "trl",
    "terrace": "ter", "circle": "cir", "parkway": "pkwy", "highway": "hwy",
    "alley": "aly", "alleyway": "aly", "hollow": "holw", "square": "sq",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "apartment": "apt", "apt": "apt", "suite": "ste", "number": "no",
    "building": "bldg", "unit": "unit", "mobile": "mh", "manufactured": "mfr",
}
_ADDR_SPLIT = re.compile(r"^(.*?),\s*([^,]+),\s*([A-Za-z]{2})\s*(\d{5})(?:-\d{4})?$")
_ABBR_DROP = {"apt", "ste", "unit", "bldg", "fl", "floor", "rm", "room"}


def _tidy_text(s):
    """门牌/街名/城市统一长相：单空格、去掉多余的逗号点和首尾垃圾。
    全大写或全小写的才转词首大写（混写的原样保留，免得 McKinley 变成 Mckinley）。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    t = re.sub(r"#\s+", "#", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r"^[,;.\s]+|[,;.\s]+$", "", t)
    if t and (t.isupper() or t.islower()):
        t = t.title()
    return t


def fmt_addr(street, city, sty, zp):
    """导出给 TPS 的地址长这样：3105 Fernridge Dr, Albany, GA 31721"""
    st = re.sub(r"[^A-Za-z]", "", str(sty or ""))[:2].upper()
    z5 = re.sub(r"\D", "", str(zp or ""))[:5]
    return ", ".join(x for x in (_tidy_text(street), _tidy_text(city),
                                 f"{st} {z5}".strip()) if x)


def tidy_addr(addr):
    """老库里存的显示串也按同一个格式再洗一遍，导出时用。"""
    m = _ADDR_SPLIT.match(str(addr or "").strip())
    if not m:
        return _tidy_text(addr)
    return fmt_addr(m.group(1), m.group(2), m.group(3), m.group(4))


def norm_addr(text):
    """地址去重键：小写、去掉点划和多余符号，把 Street/St、N/North 这类同写法并成一条。

    单元号本身保留（#2B 和 #3C 是两户），只把「Apt / Unit / Ste」这种前缀词抹平，
    这样同一户写成 #2B 还是 Apt 2B 只算一条。
    """
    t = str(text or "").lower().replace("#", " ")
    t = re.sub(r"[.,;:/\\]+", " ", t)
    t = re.sub(r"[-\u2010-\u2015]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    parts = []
    for word in t.split(" "):
        w = _ABBR.get(word, word)
        if w in _ABBR_DROP:
            continue
        parts.append(w)
    return " ".join(parts)


def norm_key(street, city, sty, zp):
    """从 PropWire 返回的四个字段直接算键，别拿拼好的显示串再解析一遍。"""
    disp = f"{street}, {city}, {str(sty or '').upper()} {zp}"
    return norm_addr(disp)


def ensure_console():
    """界面版打的是 --windowed，本来没有控制台。

    双击时不该看到黑框，但带参数跑命令行时又必须看得到输出，所以只在真的
    没有控制台、而且带了参数时才现场分配一个，并把 stdout/stderr 重新接上去
    ——不接的话 print 全丢进虚空，界面 EXE 的命令行用法就成了摆设。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import msvcrt
        import os
        k = ctypes.windll.kernel32
        if k.GetConsoleWindow():
            return
        if not k.AllocConsole():
            return
        for handle, name in ((-11, "stdout"), (-12, "stderr")):
            fd = msvcrt.open_osfhandle(k.GetStdHandle(handle), os.O_WRONLY)
            setattr(sys, name, open(fd, "w", encoding="utf-8", errors="replace",
                                    closefd=False))
    except Exception:
        pass


def fix_console():
    """Windows 控制台默认 GBK，不切 UTF-8 的话 EXE 打出来的中文全是乱码。

    光在 .bat 里 chcp 不够：从别的程序、计划任务或者双击 EXE 本身启动时不走那个 bat，
    所以在程序一开头自己把控制台码页改过来，顺带把 stdout/stdin 也钉成 utf-8，
    这样问答输入（州代码、价格）也不会被码页吃掉。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def data_dir(fallback):
    """打包成 EXE 后，数据一律写在 EXE 同目录里。

    交付出去的文件夹要能整包拷走自洽：库存、ZIP 目录、导出文件、日志全在
    EXE 边上，换台机器、换个盘符都不用改参数。本机开发时才用 fallback。
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return fallback


def connect(db_path):
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(str(db_path), timeout=120, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        _local.con = con
    return con


def init_db(db_path):
    con = sqlite3.connect(str(db_path), timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS zip_progress("
        " zip TEXT PRIMARY KEY, seen INTEGER, keep INTEGER, rc INTEGER, ts TEXT, err TEXT);"
        "CREATE TABLE IF NOT EXISTS emitted("
        " address TEXT PRIMARY KEY, value INTEGER, zip TEXT, state TEXT, ts TEXT,"
        " batch INTEGER, norm TEXT);"
        "CREATE TABLE IF NOT EXISTS batches("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, states TEXT,"
        " n INTEGER, file TEXT, ts TEXT);"
        "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);"
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_em_batch ON emitted(batch)")
    con.commit()
    merge_norm_keys(con)
    con.close()


def merge_norm_keys(con):
    """老库补 norm 列 + 把「同一栋房不同写法」并成一条，返回合并掉几条。

    保留哪一条有讲究：已经导出过的（batch 非空）优先留，否则同一栋房会以另一条
    身份被再导出一次；都导出过就留最早入库的那条。
    """
    cols = [c[1] for c in con.execute("PRAGMA table_info(emitted)")]
    if "norm" not in cols:
        con.execute("ALTER TABLE emitted ADD COLUMN norm TEXT")
    if get_setting(con, "norm_migrated") == "1":
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_em_norm ON emitted(norm)")
        con.commit()
        return 0
    rows = con.execute("SELECT rowid, address, norm FROM emitted").fetchall()
    for rid, addr, nm in rows:
        if not nm:
            con.execute("UPDATE emitted SET norm=? WHERE rowid=?", (norm_addr(addr), rid))
    dup = con.execute("SELECT norm, COUNT(*) c FROM emitted WHERE norm IS NOT NULL "
                      "GROUP BY norm HAVING c>1").fetchall()
    gone = 0
    for nm, c in dup:
        keep = con.execute(
            "SELECT rowid FROM emitted WHERE norm=? ORDER BY "
            "CASE WHEN batch IS NULL THEN 1 ELSE 0 END, rowid LIMIT 1", (nm,)).fetchone()[0]
        n = con.execute("DELETE FROM emitted WHERE norm=? AND rowid!=?", (nm, keep)).rowcount
        gone += n
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('norm_migrated','1')")
    con.commit()
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_em_norm ON emitted(norm)")
    con.commit()
    return gone


def get_setting(con, key, default=""):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def set_setting(con, key, value):
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value or "")))
    con.commit()


def zip_catalog(directory):
    """返回 ([(zip, state, city)], 来源说明)；目录里没有就从 GeoNames 现取（免费、直连）。"""
    directory = Path(directory)
    p = directory / "us_zip_codes.csv"
    rows = []
    if p.exists():
        got = set()
        for r in csv.reader(open(p, encoding="utf-8-sig", newline="")):
            if r and re.fullmatch(r"\d{5}", r[0] or "") and r[0] not in got:
                got.add(r[0])
                rows.append((r[0], r[1], r[2] if len(r) > 2 else ""))
    if len(rows) >= 30000 and {r[1] for r in rows} >= set(STATES):
        return rows, "本机已有 us_zip_codes.csv"
    raw = urllib.request.urlopen(urllib.request.Request(
        GEONAMES, headers={"User-Agent": UA}), timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read("US.txt").decode("utf-8-sig")
    rows, seen = [], set()
    for r in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(r) >= 7 and r[0] == "US" and r[4] in STATES and r[1] not in seen:
            seen.add(r[1])
            rows.append((r[1], r[4], r[2]))
    if len(rows) < 30000:
        raise SystemExit(f"ZIP 目录只有 {len(rows)} 条，不像全美，已停手")
    directory.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["zip", "state", "city", "county"])
        for r in csv.reader(io.StringIO(text), delimiter="\t"):
            if len(r) >= 7 and r[0] == "US" and r[4] in STATES:
                w.writerow([r[1], r[4], r[2], r[5]])
    return rows, "GeoNames US.zip 现取"


JS_FETCH = """
(async () => {
  const p = JSON.parse(%s);
  const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
  if (!m) return JSON.stringify({err: 'no-xsrf', href: location.href});
  const t0 = Date.now();
  let r, txt;
  try {
    r = await fetch('/search-results', {method: 'POST', credentials: 'include',
      headers: {Accept: 'application/json', 'Content-Type': 'application/json',
                'X-XSRF-Token': decodeURIComponent(m[1])},
      body: JSON.stringify(p.body)});
    txt = await r.text();
  } catch (e) {
    return JSON.stringify({err: String(e).slice(0, 160), ms: Date.now() - t0});
  }
  let j = null;
  try { j = JSON.parse(txt); } catch (e) {}
  if (!j) return JSON.stringify({st: r.status, err: txt.slice(0, 200), ms: Date.now() - t0});
  const rows = (j.response || []).map(x => {
    const a = x.address || {}, lt = x.lead_type || {};
    return [a.address || '', a.city || '', a.state || '', a.zip || '',
            x.estimated_value || 0, x.property_type || '',
            lt.vacant_home ? 1 : 0, lt.vacant_lot ? 1 : 0].join('|');
  });
  return JSON.stringify({st: r.status, rc: j.result_count, n: rows.length,
                         ms: Date.now() - t0, rows: rows, msg: j.message || ''});
})()
"""


JS_READY = """
(() => {
  const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
  return JSON.stringify({ok: !!m, href: location.href,
                         title: document.title.slice(0, 60),
                         cf: /challenge|just a moment|verify|access denied/i.test(document.title)});
})()
"""


class Pool:
    def __init__(self, a):
        self.a = a
        self.dir = Path(a.out)
        self.dir.mkdir(parents=True, exist_ok=True)
        init_db(self.dir / "pool_state.db")
        self.db = str(self.dir / "pool_state.db")
        self.log = open(self.dir / f"运行日志_{datetime.now():%Y%m%d}.txt", "a",
                        encoding="utf-8", buffering=1 << 16)
        self.s = {"zip": 0, "req": 0, "seen": 0, "keep": 0, "new": 0, "bytes": 0,
                  "vacant": 0, "cheap": 0, "blocked": 0, "err": 0, "empty": 0,
                  "retry": 0, "trunc": 0}
        self.started = 0.0
        # 界面版靠这两个回调把日志和统计推回 Qt 主线程；命令行版留 None 不受影响
        self.log_cb = None
        self.stop = threading.Event()
        self.browser = None
        self.relay = None
        self.proxies = []
        self.pidx = 0

    async def nap(self, seconds):
        """能被打断的等待：退避和翻页间隔都走这里，按了停止不会还傻等几十秒。"""
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end:
            if self.stop.is_set():
                return
            await asyncio.sleep(min(0.5, max(0.05, end - time.monotonic())))

    def proxy_relay(self):
        """填了代理就起一个本地 CONNECT 中继，浏览器只连 127.0.0.1。

        代理账号密码不能直接塞给 Chromium：nodriver/Chromium 的 --proxy-server 没有
        带认证的地方，必须本地中继补 Proxy-Authorization，跟 TPS 引擎那套一模一样。
        """
        text = str(getattr(self.a, "proxy", "") or "").strip()
        if not text:
            return None
        entries = parse_proxy_lines(text, getattr(self.a, "proxy_pass", "") or "")
        if not entries:
            raise SystemExit(f"代理看不懂：{text[:40]}（要 IP:端口:账号:密码 或 user:pass@host:port）")
        self.proxies = entries
        self.pidx = 0
        e = entries[0]
        r = LocalProxy(e["host"], int(e["port"] or 80), e["user"] or "", e["pass"] or "")
        self.wlog(f"代理已接：{len(entries)} 条出口，先用 {e['host']}:{e['port']} "
                  f"→ 本地中继 127.0.0.1:{r.port}")
        return r

    def rotate_proxy(self, why=""):
        """被挡了就换出口：多条则切下一条，只有一条也重新握手（轮换型代理商换个新 IP）。

        本地端口不动，浏览器不用重开；中继会掐掉在用的隧道逼它重新 CONNECT。
        """
        if self.relay is None or not getattr(self, "proxies", None):
            return False
        self.pidx = (self.pidx + 1) % len(self.proxies) if len(self.proxies) > 1 else self.pidx
        e = self.proxies[self.pidx]
        try:
            self.relay.rebind(e["host"], int(e["port"] or 80), e["user"] or "", e["pass"] or "")
        except Exception as ex:
            self.wlog(f"[换出口] 失败 {type(ex).__name__}:{ex}"[:140])
            return False
        self.wlog(f"[换出口] {why} → 第{self.pidx + 1}/{len(self.proxies)}条 "
                  f"{e['host']}:{e['port']}")
        return True

    async def start_browser(self, uc, exe):
        from browser_path import is_bundled
        args = []
        self.relay = self.proxy_relay()
        if self.relay is not None:
            args.append(f"--proxy-server=127.0.0.1:{self.relay.port}")
        try:
            return await uc.start(headless=False, browser_executable_path=exe,
                                  sandbox=not is_bundled(exe), browser_args=args)
        except Exception:
            if self.relay is not None:
                try:
                    self.relay.stop()
                except Exception:
                    pass
            self.relay = None
            raise

    def wlog(self, msg):
        self.log.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def say(self, msg):
        """既要控制台看得到（命令行版），也要界面日志看得到（EXE 版）。"""
        try:
            print(msg)
        except Exception:
            pass
        self.wlog(msg)

    def bump(self, **kw):
        with LOCK:
            for k, v in kw.items():
                self.s[k] = self.s.get(k, 0) + v

    def snapshot(self):
        with LOCK:
            return dict(self.s)

    def elapsed(self):
        """起跑至今的秒数，界面那一行的「每分钟几个 ZIP」要用。"""
        return time.monotonic() - self.started if self.started else 0.0

    def body(self, zipc, st, idx):
        ev = {"min": self.a.pmin}
        if self.a.has_max:
            ev["max"] = self.a.pmax
        return {"size": PW_SIZE, "result_index": idx, "house": True,
                "locations": [{"searchType": "Z", "zip": zipc, "state": st,
                               "title": f"{zipc}, {st}"}],
                "property_type": PW_TYPES, "estimated_value": ev}

    def js(self, zipc, st, idx):
        return JS_FETCH % json.dumps(json.dumps({"body": self.body(zipc, st, idx)},
                                                ensure_ascii=False))

    def in_range(self, v):
        if v < self.a.pmin:
            return False
        return not (self.a.has_max and v >= self.a.pmax)

    def store(self, zipc, st, rows):
        """返回 (返回行数, 符合条件行数, 本次新增, 剔空置, 剔超价)。

        「新增」必须插库前先查一遍已存在的地址集合才能算准——INSERT OR IGNORE 的
        rowcount 在 executemany 上是整批数，拿不到「真正新增了几条」。
        """
        seen = vac = cheap = 0
        out = []
        for line in rows:
            f = line.split("|")
            if len(f) < 8:
                continue
            street, city, sty, zp, val, _ptype, vh, vl = f
            seen += 1
            if not street or not city:
                continue
            if vh == "1" or vl == "1":
                vac += 1
                continue
            try:
                v = int(val)
            except (TypeError, ValueError):
                continue
            if not self.in_range(v):
                cheap += 1
                continue
            addr = fmt_addr(street, city, sty, zp or zipc)
            out.append((addr, norm_addr(addr), v, zp or zipc, (sty or st).upper()))
        keep = len(out)
        new = 0
        if out:
            con = connect(self.db)
            norms = [x[1] for x in out]
            pre = set()
            for i in range(0, len(norms), 500):
                chunk = norms[i:i + 500]
                ph = ",".join("?" * len(chunk))
                pre |= {r[0] for r in con.execute(
                    f"SELECT norm FROM emitted WHERE norm IN ({ph})", chunk)}
            con.executemany("INSERT OR IGNORE INTO emitted(address,value,zip,state,ts,norm) "
                            "VALUES(?,?,?,?,?,?)",
                            [(x[0], x[2], x[3], x[4], now_ts(), x[1]) for x in out])
            con.commit()
            new = len(set(norms) - pre)
        return seen, keep, new, vac, cheap

    async def do_zip(self, tab, zipc, st):
        a = self.a
        idx, pages, seen = 0, 0, 0
        keep = new = vac = cheap = retries = ttries = btries = 0
        rc = None
        err = ""
        while pages < a.maxpage and not self.stop.is_set():
            try:
                raw = await tab.evaluate(self.js(zipc, st, idx),
                                         await_promise=True, return_by_value=True)
                d = json.loads(raw) if isinstance(raw, str) else {}
            except Exception as e:
                # 「Inspected target navigated or closed」这类 CDP 断连是瞬时的：
                # 标签页重新过一遍检、同一页再打一次，别把这个 ZIP 直接判失败
                self.bump(err=1)
                if ttries < 2:
                    ttries += 1
                    self.wlog(f"{zipc} {st} 第{idx}页 传输断了（{type(e).__name__}），重开标签页重试")
                    try:
                        tab = await self.browser.get(PW_HOME, new_tab=True)
                    except Exception:
                        pass
                    if await self.warmup(tab, zipc):
                        continue
                err = f"{type(e).__name__}:{e}"[:120]
                break
            self.bump(req=1, bytes=len(raw or "") if isinstance(raw, str) else 0)
            if d.get("err") == "no-xsrf":
                # 会话半路掉了（DataDome 重新出题）：有代理就先换出口再重开首页
                self.bump(blocked=1)
                if retries < 2:
                    retries += 1
                    if self.relay is not None:
                        self.rotate_proxy(f"{zipc} 会话掉了")
                    if await self.rewarm(tab, zipc):
                        continue
                err = "no-xsrf"
                self.bump(err=1)
                break
            if d.get("st") != 200:
                if d.get("st") in (403, 429) and pages == 0:
                    self.bump(blocked=1, retry=1)
                    err = f"http{d.get('st')}"
                    # 以前这里不设次数：一个 ZIP 被挡就绕着圈一直撞，撞到天荒地老
                    btries += 1
                    if btries > 3:
                        self.bump(err=1)
                        err = f"http{d.get('st')} 连撞{btries}次"
                        break
                    moved = self.rotate_proxy(f"{zipc} http{d.get('st')}")
                    await self.nap(a.backoff if moved else random.uniform(a.backoff, a.backoff * 2.5))
                    if not await self.rewarm(tab, zipc):
                        self.bump(err=1)
                        err = f"http{d.get('st')} 换出口后没过检"
                        break
                    continue
                err = f"http{d.get('st')} {str(d.get('err') or d.get('msg'))[:80]}"
                self.bump(blocked=1 if d.get("st") in (403, 429) else 0)
                break
            rc = d.get("rc")
            n = d.get("n", 0)
            s, k, u, v, c = self.store(zipc, st, d.get("rows") or [])
            seen += s
            keep += k
            new += u
            vac += v
            cheap += c
            pages += 1
            idx += PW_SIZE
            if n < PW_SIZE or (rc and idx >= rc):
                break
            if a.sleep:
                await self.nap(a.sleep)
        if pages >= a.maxpage and rc and idx < rc:
            err = f"截断：{rc:,} 条只翻到 {idx:,}，加 --maxpage"
            self.bump(trunc=1)
        with LOCK:
            for k2, v2 in (("zip", 1), ("seen", seen), ("keep", keep), ("new", new),
                           ("vacant", vac), ("cheap", cheap)):
                self.s[k2] += v2
            if not seen:
                self.s["empty"] += 1
            con = connect(self.db)
            con.execute("INSERT OR REPLACE INTO zip_progress(zip,seen,keep,rc,ts,err) "
                        "VALUES(?,?,?,?,?,?)", (zipc, seen, keep, rc, now_ts(), err))
            con.commit()
        self.wlog(f"{zipc} {st} 翻页{pages} 返回{seen} 符合{keep} 新增{new} "
                  f"剔空置{vac} 剔超价{cheap} rc={rc} {err}")
        return tab

    async def warmup(self, tab, slot):
        """新标签页必须等 XSRF-TOKEN 真的落到 document.cookie 里才算能用。

        直接开火会拿到 no-xsrf：PropWire 前面是 DataDome，页面还在过检时 Laravel 的
        session cookie 根本没下发，实测 3 秒远远不够，慢的时候要到 20 多秒。
        """
        deadline = time.monotonic() + self.a.warm
        last = {}
        while time.monotonic() < deadline and not self.stop.is_set():
            try:
                raw = await tab.evaluate(JS_READY, await_promise=False, return_by_value=True)
                last = json.loads(raw) if isinstance(raw, str) else {}
            except Exception as e:
                last = {"err": f"{type(e).__name__}"}
            if last.get("ok"):
                return True
            if last.get("cf"):
                self.wlog(f"[W{slot}] 卡在验证页 {last.get('title')}，等 {self.a.warm} 秒")
            await self.nap(2.0)
        self.wlog(f"[W{slot}] 就绪超时 {last}")
        return False

    async def rewarm(self, tab, slot):
        """重新开一次首页再等过检。

        换了出口 IP 之后页面里那套 cookie（DataDome + Laravel session）全是上个 IP 的，
        光等不动页面是不会自己变好的。
        """
        try:
            await tab.navigate(PW_HOME)
        except Exception:
            pass
        return await self.warmup(tab, slot)

    async def worker(self, slot, browser, queue, stop):
        try:
            tab = await browser.get(PW_HOME, new_tab=True)
        except Exception as e:
            self.wlog(f"[W{slot}] 开标签页失败 {e}")
            return
        if not await self.warmup(tab, slot):
            self.bump(err=1)
            return
        while not stop.is_set():
            try:
                zipc, st = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                tab = await self.do_zip(tab, zipc, st)
            except Exception as e:
                self.wlog(f"[W{slot}] {zipc} 异常 {type(e).__name__}:{e}"[:160])
                self.bump(err=1)
                try:
                    tab = await browser.get(PW_HOME, new_tab=True)
                    if not await self.warmup(tab, slot):
                        break
                except Exception:
                    pass
            queue.task_done()

    async def run_async(self):
        import nodriver as uc
        from browser_path import describe_search, find_browser, is_bundled
        a = self.a
        exe = find_browser()
        if not exe:
            raise SystemExit("本机没找到 Chrome/Edge/Brave，browser 目录里也没有自带的 Chromium："
                             + describe_search())
        rows, src = zip_catalog(a.out)
        sset = {x.strip().upper() for x in (a.states or "").split(",") if x.strip()}
        if getattr(a, "only_zips", None):
            want = {z.strip() for z in a.only_zips if z.strip()}
            rows = [r for r in rows if r[0] in want]
            found = {r[0] for r in rows}
            miss = want - found
            if miss:
                self.wlog(f"点名重跑里有 {len(miss)} 个 ZIP 不在全美目录：{sorted(miss)[:5]}")
        elif sset:
            bad = sset - set(STATES)
            if bad:
                raise SystemExit(f"这些州代码不认得：{' '.join(sorted(bad))}")
            rows = [r for r in rows if r[1] in sset]
        con = sqlite3.connect(self.db, timeout=120)
        # 「跑过」= 这个 ZIP 自己翻到底了（或者本来就 0 条），只看有没有干净收尾。
        # err / 截断 的不算跑过——那是中途被打断或没翻完，下次自动接着补。
        ran = {r[0] for r in con.execute(
            f"SELECT zip FROM zip_progress WHERE {FAIL_CASE} IN ('ok','empty')")}
        half = {r[0] for r in con.execute(
            f"SELECT zip FROM zip_progress WHERE {FAIL_CASE} IN ('err','trunc')")}
        con.close()
        if not a.redo:
            rows = [r for r in rows if r[0] not in ran]
        if a.random:
            random.shuffle(rows)
        if a.limit:
            rows = rows[:a.limit]
        price = (f"{a.pmin:,}-{a.pmax:,}" if a.has_max else f"{a.pmin:,} 以上")
        if not rows:
            self.say(f"本轮没有要跑的 ZIP：已跑过 {len(ran):,} 个（跑过的不会重跑），"
                     f"另有 {len(half):,} 个上次没跑完的会自动补——这次全被跳过说明它们排在后面。"
                     "要补新挂牌就勾「重跑已跑过的」。")
            return
        self.say(f"ZIP 目录 {src}；本轮 {len(rows):,} 个 ZIP，"
                 f"{'州 ' + ','.join(sorted(sset)) if sset else '全美 50 州+DC'}，"
                 f"并发 {a.workers} 个标签页，价格 {price}；"
                 f"已跑过自动跳过 {len(ran):,} 个，上次没跑完的 {len(half):,} 个这次接着补")
        browser = await self.start_browser(uc, exe)
        self.browser = browser
        stop = self.stop
        if stop.is_set():
            # 浏览器起来这段时间里已经被点了停止，别再 clear 把信号抹掉
            browser.stop()
            return
        queue = asyncio.Queue()
        for r in rows:
            queue.put_nowait((r[0], r[1]))
        self.started = time.monotonic()
        try:
            beat = asyncio.ensure_future(self.heartbeat(len(rows), stop))
            await asyncio.gather(*[self.worker(i + 1, browser, queue, stop)
                                   for i in range(a.workers)])
            beat.cancel()
        finally:
            stop.set()
            try:
                browser.stop()
            except Exception:
                pass
            if self.relay is not None:
                try:
                    self.relay.stop()
                except Exception:
                    pass
                self.relay = None
        s = self.snapshot()
        mins = (time.monotonic() - self.started) / 60 or 1
        self.say(f"本轮：ZIP {s['zip']:,} 个 / 请求 {s['req']:,} 次 / 流量 {s['bytes']/1048576:.1f}MB / "
                 f"返回 {s['seen']:,} 条 / 符合价格+有人住 {s['keep']:,} / 新增入库 {s['new']:,}"
                 f"（重复 {s['keep']-s['new']:,}，剔空置 {s['vacant']:,}、剔超价 {s['cheap']:,}）")
        self.say(f"      被挡 {s['blocked']} 次 / 异常 {s['err']} 次 / 零结果 ZIP {s['empty']:,} 个 / "
                 f"平均 {s['zip']/mins:.1f} 个ZIP每分钟")
        if s["trunc"]:
            self.say(f"      注意：{s['trunc']} 个 ZIP 大到翻页没翻到底，"
                     f"用 --maxpage 提高上限再跑一次（已跑过的加 --redo）")
        self.say(f"      库存查看：{a.script} --stat")

    async def heartbeat(self, total, stop):
        last = 0
        while not stop.is_set():
            await asyncio.sleep(30)
            s = self.snapshot()
            z, mins = s["zip"], (time.monotonic() - self.started) / 60 or 1
            rate = (z - last) / 0.5
            eta = (total - z) / rate * 60 if rate > 0 else 0
            line = (f"ZIP {z:,}/{total:,} 新增 {s['new']:,} 请求 {s['req']:,} "
                    f"流量{s['bytes']/1048576:.0f}MB 被挡 {s['blocked']} 异常 {s['err']} "
                    f"{rate:.0f}个/分钟 剩约{eta/60:.1f}小时")
            self.wlog("心跳 " + line)
            print(f"[{datetime.now():%H:%M:%S}] {line}", flush=True)
            last = z

    def run(self):
        # nodriver 退出时那些 "I/O operation on closed pipe" 是 Windows 管道析构顺序问题，
        # 不影响已经落库的数据；不静音的话日志会被几十屏 traceback 刷掉，看不清真实产量。
        warnings.filterwarnings("ignore", category=ResourceWarning)
        try:
            asyncio.run(self.run_async())
        finally:
            gc.collect()


# ---------------------------------------------------------------- 库存/导出命令
def db_handle(a):
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    init_db(d / "pool_state.db")
    return sqlite3.connect(str(d / "pool_state.db"), timeout=120)


def state_cond(a):
    sset = {x.strip().upper() for x in (a.states or "").split(",") if x.strip()}
    if not sset:
        return "", []
    bad = sset - set(STATES)
    if bad:
        raise SystemExit(f"这些州代码不认得：{' '.join(sorted(bad))}")
    return "state IN (" + ",".join("?" * len(sset)) + ")", sorted(sset)


def cmd_stat(a):
    con = db_handle(a)
    tot, vmin, vmax = con.execute(
        "SELECT COUNT(*), MIN(value), MAX(value) FROM emitted").fetchone()
    print(f"[{a.label}] 池子里地址总数: {tot:,}")
    if tot:
        print(f"  房屋估价区间: {vmin:,} ~ {vmax:,}")
    un = con.execute("SELECT COUNT(*) FROM emitted WHERE batch IS NULL").fetchone()[0]
    ex = con.execute("SELECT COUNT(*) FROM emitted WHERE batch IS NOT NULL").fetchone()[0]
    print(f"  未导出 {un:,} / 已导出 {ex:,}")
    cond, args = state_cond(a)
    rows = con.execute(f"SELECT state, COUNT(*) FROM emitted "
                       f"{'WHERE ' + cond + ' AND' if cond else 'WHERE'} batch IS NULL "
                       f"GROUP BY state ORDER BY 2 DESC LIMIT 20", args).fetchall()
    if rows:
        print("  按州（未导出，前 20）: " + "  ".join(f"{s or '?'}:{n:,}" for s, n in rows))
    done = con.execute("SELECT COUNT(*) FROM zip_progress").fetchone()[0]
    empty = con.execute("SELECT COUNT(*) FROM zip_progress WHERE seen=0").fetchone()[0]
    print(f"  已跑过的 ZIP: {done:,}（其中零结果 {empty:,}）")
    print(f"  导出批次: {con.execute('SELECT COUNT(*) FROM batches').fetchone()[0]} 个")
    con.close()


def cmd_export(a):
    if a.export <= 0:
        raise SystemExit("导出数量要给正数")
    con = db_handle(a)
    cond, args = state_cond(a)
    sql = f"SELECT address FROM emitted WHERE batch IS NULL{' AND ' + cond if cond else ''}"
    order = "ORDER BY value DESC" if a.by == "价格" else "ORDER BY RANDOM()"
    rows = con.execute(f"{sql} {order} LIMIT ?", args + [a.export]).fetchall()
    if not rows:
        con.close()
        raise SystemExit("没有符合「州 + 未导出」条件的地址，先跑抓取或放宽 --states")
    outdir = Path(a.out) / "导出"
    outdir.mkdir(exist_ok=True)
    name = a.name or datetime.now().strftime("%H%M%S")
    safe = re.sub(r"[^\w一-鿿-]", "_", name)
    price = f"{a.pmin}-{a.pmax}" if a.has_max else f"{a.pmin}以上"
    fp = outdir / f"{datetime.now():%m%d_%H%M}_{a.label}_{price}_{len(rows)}条_{safe}.txt"
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        for (addr,) in rows:
            f.write(tidy_addr(addr) + "\n")
    bid = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM batches").fetchone()[0]
    con.execute("INSERT INTO batches(id,name,states,n,file,ts) VALUES(?,?,?,?,?,?)",
                (bid, name, (a.states or "").upper(), len(rows), str(fp), now_ts()))
    con.executemany("UPDATE emitted SET batch=? WHERE address=?",
                    [(bid, r[0]) for r in rows])
    con.commit()
    con.close()
    print(f"批次 {bid}：导出 {len(rows)} 条 → {fp}")
    print(f"  这批已标记为已导出，下次不会重复给你；放回池子： "
          f"{a.script} --recall-batch {bid}")


def cmd_batches(a):
    con = db_handle(a)
    rows = con.execute("SELECT id,name,states,n,file,ts FROM batches ORDER BY id").fetchall()
    if not rows:
        print("还没有导出批次")
        con.close()
        return
    for bid, name, states, n, fp, ts in rows:
        live = con.execute("SELECT COUNT(*) FROM emitted WHERE batch=?", (bid,)).fetchone()[0]
        print(f"  批次{bid:>3} {ts} 州[{states or '全美'}] 当时{n}条 现在还剩{live}条  "
              f"{name}  文件={fp}")
        print(f"          放回池子 --recall-batch {bid} / 彻底删 --delete-batch {bid} --yes")
    con.close()


def cmd_recall(a):
    con = db_handle(a)
    n = con.execute("SELECT COUNT(*) FROM emitted WHERE batch=?", (a.recall_batch,)).fetchone()[0]
    if not n:
        con.close()
        raise SystemExit(f"批次 {a.recall_batch} 里没有地址")
    con.execute("UPDATE emitted SET batch=NULL WHERE batch=?", (a.recall_batch,))
    con.execute("DELETE FROM batches WHERE id=?", (a.recall_batch,))
    con.commit()
    con.close()
    print(f"批次 {a.recall_batch} 的 {n} 条地址已放回池子（可以再次导出）")


def cmd_delete(a):
    con = db_handle(a)
    if a.delete_batch:
        bid = a.delete_batch
        n = con.execute("SELECT COUNT(*) FROM emitted WHERE batch=?", (bid,)).fetchone()[0]
        if not n:
            con.close()
            raise SystemExit(f"批次 {bid} 里没有地址")
        if not a.yes:
            con.close()
            raise SystemExit(f"要删批次 {bid} 的 {n} 条地址？加 --yes 才动手")
        con.execute("DELETE FROM emitted WHERE batch=?", (bid,))
        con.execute("DELETE FROM batches WHERE id=?", (bid,))
        con.commit()
        con.close()
        print(f"批次 {bid}：已删掉 {n} 条地址（不会自动回到池子）")
    elif a.delete_exported:
        n = con.execute("SELECT COUNT(*) FROM emitted WHERE batch IS NOT NULL").fetchone()[0]
        if not a.yes:
            con.close()
            raise SystemExit(f"要删掉全部已导出的 {n} 条地址？加 --yes 才动手")
        con.execute("DELETE FROM emitted WHERE batch IS NOT NULL")
        con.execute("DELETE FROM batches")
        con.commit()
        con.close()
        print(f"已删掉全部已导出地址 {n} 条")
    con.close()


FAIL_KINDS = {
    "err": "报错/被挡",
    "trunc": "没翻到底",
    "empty": "查到 0 条",
}

# 一个 ZIP 到底算不算跑完，只看两条硬证据：err 有没有内容、返回条数够不够 rc。
# 「没翻到底」特意不靠 --maxpage 那个标记来判——rc 是 PropWire 自己报的底数，
# seen 是我们真拿到的行数，seen<rc 就是漏了，比记 flag 可靠（中途崩了也算）。
FAIL_CASE = """
CASE
  WHEN err LIKE '截断%' THEN 'trunc'
  WHEN err IS NOT NULL AND err != '' THEN 'err'
  WHEN seen = 0 THEN 'empty'
  WHEN rc IS NOT NULL AND rc > 0 AND seen < rc THEN 'trunc'
  ELSE 'ok'
END
"""


def fail_counts(con):
    got = dict((k, 0) for k in FAIL_KINDS)
    got["ok"] = 0
    for kind, n in con.execute(f"SELECT {FAIL_CASE} AS k, COUNT(*) FROM zip_progress "
                               f"GROUP BY k"):
        got[kind] = n
    return got


def cmd_failed(a):
    con = db_handle(a)
    c = fail_counts(con)
    tot = sum(c.values())
    print(f"[{a.label}] zip_progress 共 {tot:,} 个")
    for k in ("ok", "err", "trunc", "empty"):
        print(f"  {FAIL_KINDS.get(k, '跑完正常'):10s} {c.get(k, 0):>8,}")
    for k in ("err", "trunc", "empty"):
        rows = con.execute(
            f"SELECT zip, seen, rc, err, ts FROM zip_progress WHERE {FAIL_CASE}=?"
            f" ORDER BY ts DESC LIMIT 15", (k,)).fetchall()
        if not rows:
            continue
        print(f"\n  —— {FAIL_KINDS[k]}（最多列 15 个，共 {c.get(k, 0):,} 个）")
        for zp, seen, rc, err, ts in rows:
            miss = (rc - (seen or 0)) if (rc and k == "trunc") else 0
            print(f"    {zp}  返回{seen or 0:>6,}  底数{rc if rc is not None else '?':>7}  "
                  f"{'缺' + format(miss, ',') + '条  ' if miss else ''}{err or ''}  {ts}")
    print(f"\n  只重跑这些：{a.script} --retry-failed"
          f"（想只重跑某一类：--retry-failed --kind err / trunc / empty）")
    con.close()


def cmd_retry_failed(a):
    con = db_handle(a)
    kinds = [x.strip() for x in (a.kind or "err,trunc,empty").split(",") if x.strip()]
    bad = [k for k in kinds if k not in FAIL_KINDS]
    if bad:
        con.close()
        raise SystemExit(f"--kind 只认 err / trunc / empty，你给了 {' '.join(bad)}")
    ph = ",".join("?" * len(kinds))
    rows = con.execute(f"SELECT zip FROM zip_progress WHERE {FAIL_CASE} IN ({ph})",
                       kinds).fetchall()
    if not rows:
        con.close()
        print("没有这类失败的 ZIP，不用重跑")
        return
    if not a.yes:
        con.close()
        raise SystemExit(f"要重跑这 {len(rows):,} 个失败的 ZIP？加 --yes 才动手"
                         "（已入库的地址不会重复，池子里那份照旧）")
    zips = [r[0] for r in rows]
    con.executemany("DELETE FROM zip_progress WHERE zip=?", [(z,) for z in zips])
    con.commit()
    con.close()
    print(f"已清掉 {len(zips):,} 个 ZIP 的进度记录，现在开始重跑……")
    # 必须点名只跑这批：清了进度后再走「全量挑未跑过的」会把全美没跑过的 ZIP 一起卷进来
    a.only_zips = zips
    a.states = ""
    a.limit = 0
    a.random = False
    a.redo = False
    Pool(a).run()


def build_parser(label, default_out, has_max, dmin, dmax):
    ap = argparse.ArgumentParser(
        description=f"{label}：PropWire 按 ZIP 翻页取有人住的房屋地址（本机浏览器，不用流量）")
    ap.add_argument("--out", default=data_dir(default_out),
                    help="库存和导出文件放哪（打包成 EXE 后默认就是 EXE 同目录）")
    ap.add_argument("--states", default="",
                    help="州代码，逗号分隔，如 TX 或 NJ,NY,PA；留空=全美 50 州+DC")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多跑几个 ZIP（0=全部）")
    ap.add_argument("--workers", type=int, default=4, help="并发标签页数，建议 3-6")
    ap.add_argument("--random", action="store_true", help="随机抽 ZIP 试产量")
    ap.add_argument("--redo", action="store_true", help="已跑过的 ZIP 再跑一遍（补新挂牌）")
    ap.add_argument("--sleep", type=float, default=0.3, help="同一 ZIP 两次翻页的间隔秒数")
    ap.add_argument("--backoff", type=float, default=20.0, help="被挡后的退避秒数")
    ap.add_argument("--warm", type=float, default=90.0,
                    help="每个标签页等 DataDome 过检的最长秒数")
    ap.add_argument("--maxpage", type=int, default=400, help="单个 ZIP 最多翻几页（400 页=10 万条）")
    ap.add_argument("--min", dest="pmin", type=int, default=dmin,
                    help=f"房屋估价下限（美元，默认 {dmin:,}）")
    if has_max:
        ap.add_argument("--max", dest="pmax", type=int, default=dmax,
                        help=f"房屋估价上限（美元，默认 {dmax:,}；达到就不算{label}）")
    else:
        ap.set_defaults(pmax=None)
    ap.add_argument("--stat", action="store_true", help="看库存")
    ap.add_argument("--list-failed", action="store_true",
                    help="看失败的 ZIP：报错/被挡、没翻到底、查到 0 条")
    ap.add_argument("--retry-failed", action="store_true", help="只重跑失败的 ZIP")
    ap.add_argument("--kind", default="",
                    help="配合 --retry-failed：err / trunc / empty，逗号分隔；留空=三类全重")
    ap.add_argument("--zips", default="", help="只跑点名的这几个 ZIP，逗号分隔")
    ap.add_argument("--proxy", default="",
                    help="代理，一整串：IP:端口:账号:密码 或 http://账号:密码@IP:端口；留空=走本机网络")
    ap.add_argument("--proxy-pass", dest="proxy_pass", default="",
                    help="代理密码（代理串里没带密码时才需要）")
    ap.add_argument("--export", type=int, default=0, help="导出多少条未导出的地址")
    ap.add_argument("--by", default="随机", choices=["随机", "价格"], help="导出顺序")
    ap.add_argument("--name", default="", help="批次备注，比如机器号")
    ap.add_argument("--list-batches", action="store_true")
    ap.add_argument("--recall-batch", type=int, default=0, help="把某批已导出地址放回池子")
    ap.add_argument("--delete-batch", type=int, default=0, help="彻底删掉某批地址")
    ap.add_argument("--delete-exported", action="store_true", help="删掉所有已导出地址")
    ap.add_argument("--yes", action="store_true", help="确认删除")
    ap.set_defaults(label=label, has_max=has_max)
    return ap


def ask(a, label, has_max):
    """双击 EXE、一个参数都没给时，用问答把这一轮问出来，不用敲命令行。"""
    print(f"=== {label}地址抓取 ===")
    s = input(f"跑哪些州？写州代码（如 TX 或 NJ,NY,PA），直接回车 = 全美 50 州+DC：").strip()
    a.states = s.upper()
    if has_max:
        v = input(f"房屋估价下限（美元，回车 = {a.pmin:,}）：").strip().replace(",", "")
        if v.isdigit():
            a.pmin = int(v)
        v = input(f"房屋估价上限（美元，回车 = {a.pmax:,}）：").strip().replace(",", "")
        if v.isdigit():
            a.pmax = int(v)
    else:
        v = input(f"房屋估价门槛（美元，达到就算{label}，回车 = {a.pmin:,}）：").strip().replace(",", "")
        if v.isdigit():
            a.pmin = int(v)
    v = input("并发几个标签页？（回车 = 4，建议 3-6）：").strip()
    if v.isdigit():
        a.workers = max(1, min(8, int(v)))
    v = input("本轮跑多少个 ZIP？（回车 = 该州全部跑完）：").strip()
    if v.isdigit():
        a.limit = int(v)
    r = input("已跑过的 ZIP 要不要再跑一遍补新挂牌？(y/N)：").strip().lower()
    a.redo = r == "y"
    if a.has_max and a.pmin >= a.pmax:
        raise SystemExit(f"下限 {a.pmin:,} 不小于上限 {a.pmax:,}，这样是空的")
    print()


def main(label, default_out, has_max, dmin, dmax, argv=None, script=""):
    fix_console()
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        ensure_console()
    a = build_parser(label, default_out, has_max, dmin, dmax).parse_args(argv)
    if getattr(sys, "frozen", False):
        # 提示语要写 EXE 名，写 .py 名的话他照着敲会找不到文件
        a.script = Path(sys.executable).name
    else:
        a.script = script or f"{label}地址抓取.py"
    if not argv:
        ask(a, label, has_max)
    if a.stat:
        return cmd_stat(a)
    if a.list_failed:
        return cmd_failed(a)
    if a.retry_failed:
        return cmd_retry_failed(a)
    if a.zips:
        a.only_zips = [z.strip() for z in a.zips.split(",") if z.strip()]
    if a.export:
        return cmd_export(a)
    if a.list_batches:
        return cmd_batches(a)
    if a.recall_batch:
        return cmd_recall(a)
    if a.delete_batch or a.delete_exported:
        return cmd_delete(a)
    if has_max and a.pmin >= a.pmax:
        raise SystemExit(f"--min 必须小于 --max（现在 {a.pmin:,} 和 {a.pmax:,}）")
    Pool(a).run()
