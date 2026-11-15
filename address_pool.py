"""全美地址池：按美国真实 ZIP 逐个查 Zillow 房产站，只要街道地址，按房价分两档入库。

抓取（本机网络，不用代理、不用 Apify）：
    python address_pool.py --states NJ,NY,PA --limit 200 --workers 5
    python address_pool.py --random --limit 200          # 随机抽 ZIP 试产量
    python address_pool.py                                # 全部 ZIP 跑完整遍

看库存：
    python address_pool.py --stat

导出给某台电脑（导出后这批自动标记，不会重复给别人）：
    python address_pool.py --export 10000 --tier 普通 --states NJ,NY --name 3号机

批次管理（已导出的地址可以放回池子，也可以彻底删掉）：
    python address_pool.py --list-batches
    python address_pool.py --recall-batch 3           # 放回池子（撤销导出标记）
    python address_pool.py --delete-batch 3 --yes     # 连地址一起删掉
    python address_pool.py --delete-exported --yes    # 删掉所有已导出的地址

导出文件一行一个地址「街道, 城市, 州 ZIP」，抓取器「从文件导入」直接可用。
"""
import argparse
import csv
import io
import json
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
GEONAMES = "https://download.geonames.org/export/zip/US.zip"
STATES = sorted("AK AL AR AZ CA CO CT DC DE FL GA HI IA ID IL IN KS KY LA MA MD ME MI MN MO MS "
                "MT NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY".split())
# 实测只有这几条筛选路径能切出与基础页不同的一批地址；不挑房型，纯粹为了每个 ZIP 多拿地址。
# apartments/（只有月租，没售价）和 land/（空地没人住）实测有效但没用，所以不放进默认。
SLICES = ["", "luxury-homes/", "waterfront/", "condos/", "townhomes/", "duplex/",
          "houses/", "foreclosures/"]

_local = threading.local()
LOCK = threading.Lock()


def now_ts():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


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
        " zip TEXT PRIMARY KEY, slices TEXT, seen INTEGER, keep INTEGER, ts TEXT, err TEXT);"
        "CREATE TABLE IF NOT EXISTS emitted("
        " address TEXT PRIMARY KEY, tier TEXT, value INTEGER, zip TEXT, state TEXT,"
        " ts TEXT, batch INTEGER);"
        "CREATE TABLE IF NOT EXISTS batches("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, tier TEXT, states TEXT,"
        " n INTEGER, file TEXT, ts TEXT);"
    )
    have = {r[1] for r in con.execute("PRAGMA table_info(emitted)")}
    for col, typ in [("state", "TEXT"), ("batch", "INTEGER")]:
        if col not in have:
            con.execute(f"ALTER TABLE emitted ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_em_batch ON emitted(batch)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_em_tier_state ON emitted(tier,state)")
    con.commit()
    con.close()


def curl_get(url, timeout=40):
    """本机 curl 直连。Zillow 对 Python 直连会挡，对 curl 放行（实测）。"""
    p = subprocess.run(
        ["curl", "-s", "-L", "-k", "--compressed", "--max-time", str(timeout), "-A", UA,
         "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "-H", "Accept-Language: en-US,en;q=0.9",
         "-w", "\n@@CODE=%{http_code} BYTES=%{size_download}", url],
        capture_output=True, text=True, timeout=timeout + 25,
        encoding="utf-8", errors="replace")
    body = p.stdout or ""
    m = re.search(r"@@CODE=(\d+) BYTES=(\d+)", body)
    if not m:
        return 0, 0, ""
    return int(m.group(1)), int(m.group(2)), body[:m.start()]


def parse_records(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        sr = d["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]
    except Exception:
        return None
    recs = []
    for name in ("listResults", "relaxedResults"):
        for it in sr.get(name) or []:
            if not isinstance(it, dict):
                continue
            addr = it.get("address")
            if not addr or it.get("isUndisclosedAddress"):
                continue
            hi = (it.get("hdpData") or {}).get("homeInfo") or {}
            status = str(it.get("statusType") or hi.get("homeStatus") or "").upper()
            pgapt = str(it.get("pgapt") or "").upper()
            if "RENT" in status or "RENT" in pgapt:
                continue
            price = it.get("unformattedPrice") or hi.get("price") or hi.get("priceForHDP")
            zesti = it.get("zestimate") or hi.get("zestimate")
            value = price or zesti
            addr = re.sub(r"\s+", " ", str(addr)).strip().rstrip(",")
            if not addr or not value:
                continue
            st = hi.get("state") or (it.get("addressState") or "")
            recs.append({"address": addr, "value": int(value), "state": str(st).upper()[:2],
                         "zestimate": zesti, "sold_price_only": not bool(price)})
    return recs


def zip_catalog(directory):
    """返回 [(zip, state, city)]；目录里没有就从 GeoNames 现取（免费、直连）。"""
    directory = Path(directory)
    p = directory / "us_zip_codes.csv"
    rows = []
    if p.exists():
        for r in csv.reader(open(p, encoding="utf-8-sig", newline="")):
            if r and re.fullmatch(r"\d{5}", r[0] or ""):
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


# ---------------------------------------------------------------- 抓取
class Harvest:
    def __init__(self, a):
        self.a = a
        self.dir = Path(a.out)
        self.dir.mkdir(parents=True, exist_ok=True)
        init_db(self.dir / "pool_state.db")
        self.db = self.dir / "pool_state.db"
        self.log = open(self.dir / f"运行日志_{datetime.now():%Y%m%d}.txt", "a",
                        encoding="utf-8", buffering=1 << 16)
        self.s = {"zip": 0, "req": 0, "bytes": 0, "seen": 0, "uniq": 0, "keep": 0,
                  "普通": 0, "富人": 0, "blocked": 0, "err": 0, "empty": 0}
        self.zip_state = {}

    def wlog(self, msg):
        self.log.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")

    def one_slice(self, z, sl):
        url = f"https://www.zillow.com/{z}/{sl}"
        for attempt in (1, 2, 3):
            try:
                code, byts, html = curl_get(url)
                with LOCK:
                    self.s["req"] += 1
                    self.s["bytes"] += byts
                if code in (403, 429):
                    with LOCK:
                        self.s["blocked"] += 1
                    time.sleep(random.uniform(8, 20))
                    continue
                if code != 200:
                    time.sleep(random.uniform(2, 5))
                    continue
                recs = parse_records(html)
                if recs is None:
                    time.sleep(random.uniform(2, 5))
                    continue
                return recs
            except Exception as e:
                with LOCK:
                    self.s["err"] += 1
                if attempt == 3:
                    self.wlog(f"ZIP{z}/{sl} 异常 {type(e).__name__}")
                time.sleep(random.uniform(2, 6))
        return []

    def do_zip(self, z):
        state = self.zip_state.get(z, "")
        uniq, seen, errs = {}, 0, []
        for sl in self.a.slices:
            recs = self.one_slice(z, sl)
            seen += len(recs)
            for r in recs:
                uniq.setdefault(r["address"], r)
            time.sleep(random.uniform(self.a.sleep, self.a.sleep * 2))
        tiers = {"普通": 0, "富人": 0}
        con = connect(self.db)
        with LOCK:
            for addr, r in uniq.items():
                v = r["value"]
                tier = "富人" if v >= self.a.mid2 else ("普通" if v >= self.a.mid1 else None)
                if not tier:
                    continue
                cur = con.execute("INSERT OR IGNORE INTO emitted(address,tier,value,zip,state,ts,batch)"
                                  " VALUES(?,?,?,?,?,?,NULL)",
                                  (addr, tier, v, z, r["state"] or state, now_ts()))
                if cur.rowcount:
                    tiers[tier] += 1
            con.execute("INSERT OR REPLACE INTO zip_progress(zip,slices,seen,keep,ts,err)"
                        " VALUES(?,?,?,?,?,?)",
                        (z, "|".join(self.a.slices), seen, tiers["普通"] + tiers["富人"],
                         now_ts(), ";".join(errs)[:120]))
            con.commit()
        with LOCK:
            self.s["zip"] += 1
            self.s["seen"] += seen
            self.s["uniq"] += len(uniq)
            self.s["keep"] += tiers["普通"] + tiers["富人"]
            self.s["普通"] += tiers["普通"]
            self.s["富人"] += tiers["富人"]
            self.s["empty"] += 1 if not seen else 0
        return z, seen, len(uniq), tiers

    def run(self):
        rows, src = zip_catalog(self.dir)
        self.zip_state = {r[0]: r[1] for r in rows}
        picks = {r[0]: r[1] for r in rows}
        want = set(s.strip().upper() for s in self.a.states.split(",") if s.strip()) if self.a.states else None
        bad = want - set(STATES) if want else set()
        if bad:
            raise SystemExit(f"这些州代码不认得：{' '.join(sorted(bad))}；全美是 {', '.join(STATES)}")
        con = sqlite3.connect(str(self.db))
        done = {r[0] for r in con.execute("SELECT zip FROM zip_progress WHERE seen>0")}
        con.close()
        todo = [z for z, st in picks.items()
                if (self.a.redo or z not in done) and (not want or st in want)]
        if self.a.random:
            random.shuffle(todo)
        else:
            todo.sort()
        if self.a.limit:
            todo = todo[:self.a.limit]
        print(f"ZIP 目录 {len(rows)} 个（{src}）；已跑过 {len(done)}；本轮 {len(todo)} 个 ZIP × "
              f"{len(self.a.slices)} 条筛选路径；州范围 {want or '全美'}；并发 {self.a.workers}")
        self.wlog(f"开始：目录{len(rows)} 已跑{len(done)} 本轮{len(todo)} 州{want or '全美'} "
                  f"路径{self.a.slices} 档 普通{self.a.mid1}-{self.a.mid2} 富人>={self.a.mid2}")
        if not todo:
            return
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.a.workers, thread_name_prefix="z") as ex:
            futs = {ex.submit(self.do_zip, z): z for z in todo}
            n = 0
            for fut in as_completed(futs):
                z, seen, uq, tiers = fut.result()
                n += 1
                if n % 20 == 0 or n == len(todo):
                    el = time.time() - t0
                    rate = n / el if el else 0
                    left = (len(todo) - n) / rate / 3600 if rate else 0
                    s = self.s
                    line = (f"[{n}/{len(todo)}] 每ZIP平均看到{s['seen']/n:.0f}条/"
                            f"去重{s['uniq']/n:.0f}条 入库{s['keep']}（普通{s['普通']} 富人{s['富人']}）"
                            f" 流量{s['bytes']/1048576:.0f}MB 被拦{s['blocked']} 还需{left:.1f}小时")
                    print(line)
                    self.wlog(line)
        el = time.time() - t0
        s = self.s
        msg = (f"本轮 {s['zip']} 个 ZIP，用时 {el/3600:.2f} 小时；抓到记录 {s['seen']} 条，"
               f"去重 {s['uniq']} 条，新入库 {s['keep']} 条（普通 {s['普通']}、富人 {s['富人']}）；"
               f"请求 {s['req']} 次，被拦 {s['blocked']} 次，异常 {s['err']} 次，"
               f"零结果 ZIP {s['empty']} 个；下载流量 {s['bytes']/1048576:.1f} MB")
        print("\n" + msg)
        self.wlog(msg)
        connect(self.db).close()
        self.log.close()


# ---------------------------------------------------------------- 管理
def db_handle(a):
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    init_db(d / "pool_state.db")
    return sqlite3.connect(str(d / "pool_state.db"), timeout=120)


def where(a, tier=None, states=None, unexported_only=False, exported_only=False):
    conds, args = [], []
    t = tier if tier is not None else (a.tier if hasattr(a, "tier") else None)
    if t and t != "全部":
        conds.append("tier=?")
        args.append(t)
    s = states if states is not None else (getattr(a, "states", "") or "")
    sset = {x.strip().upper() for x in s.split(",") if x.strip()}
    if sset:
        bad = sset - set(STATES)
        if bad:
            raise SystemExit(f"这些州代码不认得：{' '.join(sorted(bad))}")
        conds.append("state IN (" + ",".join("?" * len(sset)) + ")")
        args += sorted(sset)
    if unexported_only:
        conds.append("batch IS NULL")
    if exported_only:
        conds.append("batch IS NOT NULL")
    return ("WHERE " + " AND ".join(conds)) if conds else "", args


def cmd_stat(a):
    con = db_handle(a)
    tot = con.execute("SELECT COUNT(*) FROM emitted").fetchone()[0]
    print(f"池子里地址总数: {tot:,}")
    print("  分档 / 已导出情况:")
    for tier, batched, n in con.execute(
            "SELECT tier, CASE WHEN batch IS NULL THEN '未导出' ELSE '已导出' END, COUNT(*) "
            "FROM emitted GROUP BY tier, 2 ORDER BY 1, 2"):
        print(f"    {tier:4s} {batched:6s} {n:>12,}")
    print("  按州（前 20，未导出的）:")
    rows = con.execute("SELECT state, COUNT(*) FROM emitted WHERE batch IS NULL "
                       "GROUP BY state ORDER BY 2 DESC LIMIT 20").fetchall()
    print("    " + "  ".join(f"{s or '?'}:{n:,}" for s, n in rows))
    done = con.execute("SELECT COUNT(*) FROM zip_progress").fetchone()[0]
    empty = con.execute("SELECT COUNT(*) FROM zip_progress WHERE seen=0").fetchone()[0]
    print(f"  已跑过的 ZIP: {done:,}（其中零结果 {empty:,}）")
    try:
        b = con.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        print(f"  导出批次: {b} 个")
    except Exception:
        pass
    con.close()


def cmd_export(a):
    if a.export <= 0:
        raise SystemExit("导出数量要给正数")
    con = db_handle(a)
    sql_where, args = where(a, unexported_only=True)
    order = "ORDER BY value DESC" if a.by == "价格" else "ORDER BY RANDOM()"
    rows = con.execute(f"SELECT address, tier, value, state FROM emitted "
                       f"{sql_where} {order} LIMIT ?", args + [a.export]).fetchall()
    if not rows:
        con.close()
        raise SystemExit("没有符合「档位+州+未导出」的地址，先跑抓取或放宽条件")
    outdir = Path(a.out) / "导出"
    outdir.mkdir(exist_ok=True)
    name = a.name or datetime.now().strftime("%H%M%S")
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", name)
    tier_tag = a.tier if a.tier and a.tier != "全部" else "混合"
    fp = outdir / f"{datetime.now():%m%d_%H%M}_{tier_tag}_{len(rows)}条_{safe}.txt"
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        for addr, tier, v, st in rows:
            f.write(addr + "\n")
    cur = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM batches")
    bid = cur.fetchone()[0]
    con.execute("INSERT INTO batches(id,name,tier,states,n,file,ts) VALUES(?,?,?,?,?,?,?)",
                (bid, name, tier_tag, (a.states or "").upper(), len(rows), str(fp), now_ts()))
    addrs = [r[0] for r in rows]
    con.executemany("UPDATE emitted SET batch=? WHERE address=?", [(bid, x) for x in addrs])
    con.commit()
    con.close()
    print(f"批次 {bid}：导出 {len(rows)} 条 → {fp}")
    print("  这批已标记为已导出，下次导出不会重复给你；想放回池子： "
          f"python address_pool.py --recall-batch {bid}")


def cmd_batches(a):
    con = db_handle(a)
    rows = con.execute("SELECT id,name,tier,states,n,file,ts FROM batches ORDER BY id").fetchall()
    if not rows:
        print("还没有导出批次")
        return
    for bid, name, tier, states, n, fp, ts in rows:
        live = con.execute("SELECT COUNT(*) FROM emitted WHERE batch=?", (bid,)).fetchone()[0]
        print(f"  批次{bid:>3} {ts}  {tier} 州[{states or '全美'}] 当时{n}条 现在还剩{live}条  "
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


def main():
    ap = argparse.ArgumentParser(description="全美地址池（Zillow ZIP 遍历，本机网络）")
    ap.add_argument("--out", default=r"D:\地址池")
    ap.add_argument("--states", default="", help="州代码，逗号分隔，如 NJ,NY,PA；留空=全美")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多跑几个 ZIP（0=全部）")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--random", action="store_true", help="随机抽 ZIP（试产量用，别总从头几个州跑）")
    ap.add_argument("--redo", action="store_true", help="已跑过的 ZIP 再跑一遍（补新挂牌）")
    ap.add_argument("--sleep", type=float, default=0.8, help="同一个 ZIP 内两次请求的间隔秒数")
    ap.add_argument("--slices", nargs="+", default=SLICES, help="筛选路径集合，越多每 ZIP 拿得越多")
    ap.add_argument("--mid1", type=int, default=400000, help="普通档下限（默认 40 万）")
    ap.add_argument("--mid2", type=int, default=1200000, help="富人档下限（默认 120 万）")
    ap.add_argument("--stat", action="store_true", help="看库存")
    ap.add_argument("--export", type=int, default=0, help="导出多少条未导出的地址")
    ap.add_argument("--tier", default="全部", choices=["全部", "普通", "富人"])
    ap.add_argument("--by", default="随机", choices=["随机", "价格"], help="导出顺序")
    ap.add_argument("--name", default="", help="批次备注，比如机器号")
    ap.add_argument("--list-batches", action="store_true")
    ap.add_argument("--recall-batch", type=int, default=0, help="把某批已导出地址放回池子")
    ap.add_argument("--delete-batch", type=int, default=0, help="彻底删掉某批地址")
    ap.add_argument("--delete-exported", action="store_true", help="删掉所有已导出地址")
    ap.add_argument("--yes", action="store_true", help="确认删除")
    a = ap.parse_args()
    if a.stat:
        return cmd_stat(a)
    if a.list_batches:
        return cmd_batches(a)
    if a.export:
        return cmd_export(a)
    if a.recall_batch:
        return cmd_recall(a)
    if a.delete_batch or a.delete_exported:
        return cmd_delete(a)
    Harvest(a).run()


if __name__ == "__main__":
    main()
