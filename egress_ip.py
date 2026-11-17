# -*- coding: utf-8 -*-
r"""给日志用：读「这条车道当前走的是哪个出口 IP、在哪个国家/州」，并缓存归属地。

三条实测口径（11-18 探针 Temp\\sourcescan\\egress_probe3_1118.py 的原始数字）：

1) 出口 IP 只能走「这条车道自己的那条隧道」读。本机直连读到的永远是本机出口
   （实测 203.77.172.98 = 泰国曼谷，7 个 echo 站全都回这个），代理按 sid 绑住的那个 IP
   不走这条隧道根本看不见。首选读法：拿同一条车道的上游代理串（同一个 sid）用 Python 自己
   CONNECT 一次 —— 厂家按 sid 粘 IP，所以这就是这条车道在用的出口，而 socket 超时说话算数
   （11-18 实测：隧道死口时 Python 0.4 秒就回 407/错，浏览器同一个请求能挂 200 多秒，
   探活绝不能把车道拖成那样）。Python 读不到时才退回浏览器自己读（nodriver 临时标签页 /
   Playwright page.request），并顺带把两条读法对照过（见 Temp\sourcescan\egress_live_1118.txt）。
   另外 11-16 实测「浏览器内换 sid 换不动出口（隧道复用）」，所以 browser 侧读到的也是
   这条车道真正在用的出口，两边都算权威。
2) 归属地反过来走本机直连：ip-api 只按 URL 里的参数查，不看谁来问，所以绕开代理
   ——不占厂家并发、不计流量。实测单条 79~1100ms，免费档限 ~45 次/分；
   /batch 一次 100 条实测 73ms、限 ~10 次/分。lang=zh-CN 时国家/州是中文（曼谷/美国/
   弗吉尼亚州），城市是英文，照原样拼。
3) 查过的都落库缓存（ip_geo 表，7 天），并且顺带按 /24 记一条：住宅池新号多半还落在
   同几个 /24 里，后面直接命中缓存，几乎不再打接口。

这行日志是附加信息：任何一步取不到（echo 站不通、接口限流、库写不进）都只写成
「没取到」，绝不许把正在跑的车道带崩，所以全部函数都吞异常。
"""
import asyncio
import json
import re
import threading
import time
import urllib.request

# echo 站：只回一行纯文本 IP，按实测延迟从快到慢排（本机直连 63ms / 390ms / 141ms）
ECHO_URLS = ("https://ipv4.icanhazip.com", "https://checkip.amazonaws.com",
             "https://api.ipify.org")
GEO_ONE = ("http://ip-api.com/json/{ip}"
            "?fields=status,country,regionName,city&lang=zh-CN")
GEO_ONE = ("http://ip-api.com/json/{ip}"
            "?fields=status,country,regionName,city&lang=zh-CN")
# 批量必须把 query 写进 fields：11-18 实测（Temp\sourcescan\batch_keys_1118.txt）
# fields 不带 query 时，/batch 回的一行里压根没有 query 字段 —— 100 条结果对不上是谁的，
# 整个批量就成了废请求。带上 query 才回得到 "query":"8.8.8.8"。
GEO_BATCH = "http://ip-api.com/batch?fields=status,country,regionName,city,query&lang=zh-CN"

GEO_TTL = 7 * 86400          # 查到过的位置记 7 天
NEG_TTL = 900                # 明确查不到（status=fail）也记 15 分钟，免得反复撞
SINGLE_PER_MIN = 40          # 留 5 次余量给手动查询
BATCH_PER_MIN = 8
PROBE_TIMEOUT_S = 6.0        # 整条车道探活的时间预算（读 IP + 查归属地），超了就只写「没取到」

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")

_core_mod = [None]
_db_ready = [False]
_rate_lock = threading.Lock()
_single_stamps = []
_batch_stamps = []
_pool_lock = threading.Lock()
_pool = {}                    # 等批量查的 IP
_batch_lock = threading.Lock()  # 同一时刻只允许一条车道发批量
_opener = [None]
_local_ip = [""]


def _core():
    """共用 1.8.2 那套库/设置（两个核心都吃这同一个 tps_data.db）。"""
    if _core_mod[0] is None:
        import engine_tps182 as m
        _core_mod[0] = m
    return _core_mod[0]


def get_setting(key, default=""):
    try:
        return _core().get_setting(key, default)
    except Exception:
        return default


def enabled():
    """界面「日志写出口IP」勾选框（默认开）。关掉就一次接口都不打、一个标签页都不开。"""
    return str(get_setting("log_egress_ip", "1")) == "1"


def clean_ip(text):
    """从正文里抠第一个合法的点分四段 IPv4；内网/回环地址说明没真出去，算没取到。"""
    for m in _IPV4.finditer(str(text or "")):
        octets = [int(g) for g in m.groups()]
        if any(o > 255 for o in octets):
            continue
        if octets[0] in (0, 10, 127) or octets[0] == 169 or (octets[0] == 192 and octets[1] == 168) \
                or (octets[0] == 172 and 16 <= octets[1] <= 31):
            continue
        return ".".join(str(o) for o in octets)
    return ""


def _net_key(ip):
    return ip.rsplit(".", 1)[0] + ".*"


def _opener_direct():
    """本机直连：显式空 ProxyHandler，不然系统/环境变量里的代理会把查询带进隧道。"""
    if _opener[0] is None:
        _opener[0] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener[0]


def _http(url, timeout=3.0, data=None):
    req = urllib.request.Request(url, data=data,
                                headers={"User-Agent": "tps-fps-egress-log/1.0",
                                         "Content-Type": "application/json"})
    with _opener_direct().open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def local_ip():
    """本机自己的出口 IP（一次进程只读一次）：用来认出「page.request 绕过代理」这种情况。"""
    if _local_ip[0] == "":
        ip = ""
        for url in ECHO_URLS:
            try:
                ip = clean_ip(_http(url, timeout=2.0))
            except Exception:
                continue
            if ip:
                break
        _local_ip[0] = ip
    return _local_ip[0]


# ---------------- 归属地缓存（ip_geo 表） ----------------

def _ensure_table(con):
    if _db_ready[0]:
        return
    con.execute("""CREATE TABLE IF NOT EXISTS ip_geo(
        key TEXT PRIMARY KEY,
        ip TEXT,
        loc TEXT,
        is_net INTEGER DEFAULT 0,
        ts INTEGER
    )""")
    _db_ready[0] = True


def _db_get(key):
    """返回 (loc, ts)；没记过返回 None。"""
    con = None
    try:
        con = _core().connect()
        _ensure_table(con)
        row = con.execute("SELECT loc,ts FROM ip_geo WHERE key=?", (key,)).fetchone()
        return (str(row["loc"] or ""), int(row["ts"] or 0)) if row else None
    except Exception:
        return None
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _db_put(pairs, loc, age_ts=None):
    """pairs = [(缓存键, 代表 IP), ...]；同一份答案同时记精确 IP 和它所属 /24。"""
    con = None
    try:
        now = int(age_ts if age_ts is not None else time.time())
        con = _core().connect()
        _ensure_table(con)
        for key, ip in pairs:
            con.execute("INSERT OR REPLACE INTO ip_geo(key,ip,loc,is_net,ts) VALUES(?,?,?,?,?)",
                        (key, ip, loc, 1 if key.endswith(".*") else 0, now))
        con.commit()
    except Exception:
        pass
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


def _fresh(hit):
    if not hit:
        return False
    loc, ts = hit
    return time.time() - ts < (GEO_TTL if loc else NEG_TTL)


def _allow(kind, limit):
    """一分钟滑动窗口；没额度就返回 False（不排队干等，日志宁缺）。"""
    now = time.time()
    with _rate_lock:
        box = _single_stamps if kind == "single" else _batch_stamps
        box[:] = [t for t in box if now - t < 60.0]
        if len(box) >= limit:
            return False
        box.append(now)
        return True


def _fmt_loc(d):
    parts = []
    for k in ("country", "regionName", "city"):
        v = str(d.get(k) or "").strip()
        if v and v not in parts:
            parts.append(v)
    return " ".join(parts)


def _lookup_single(ip):
    """返回 (loc, 是否确定查过)。确定查过但没结果 → ("", True)，也值得缓存住。"""
    try:
        d = json.loads(_http(GEO_ONE.format(ip=ip), timeout=3.0))
    except Exception:
        return "", False
    if str(d.get("status") or "") != "success":
        return "", True
    return _fmt_loc(d), True


def _post_batch(ips):
    """一次 /batch 最多 100 条；返回 {ip: loc}，网络失败返回 None。"""
    try:
        payload = json.dumps([{"query": ip} for ip in ips]).encode()
        raw = _http(GEO_BATCH, timeout=3.0, data=payload)
        rows = json.loads(raw)
    except Exception:
        return None
    out = {}
    for row in rows if isinstance(rows, list) else []:
        key = str(row.get("query") or "")
        if str(row.get("status") or "") == "success":
            out[key] = _fmt_loc(row)
        else:
            out[key] = ""
    return out


def _via_batch(ip):
    """单条额度用完时走这里：抢锁发一次批量，顺带把别的车道攒的 IP 一起查了。
    抢不到锁（说明别的车道正在发）就把自己的 IP 留在池里，这条先只写 IP。"""
    with _pool_lock:
        _pool[ip] = True
    if not _batch_lock.acquire(blocking=False):
        return ""
    try:
        if not _allow("batch", BATCH_PER_MIN):
            return ""
        with _pool_lock:
            ips = list(_pool)[:100]
        if not ips:
            return ""
        res = _post_batch(ips)
        if res is None:
            return ""
        for one in ips:
            with _pool_lock:
                _pool.pop(one, None)
            loc = res.get(one, "")
            if loc:
                _db_put([(one, one), (_net_key(one), one)], loc)
            else:
                _db_put([(one, one)], "")
        return res.get(ip, "")
    finally:
        _batch_lock.release()


def geo(ip):
    """出口 IP 的中文归属地（「美国 俄勒冈州 Portland」）；取不到返回空串。"""
    if not ip:
        return ""
    hit = _db_get(ip)
    if _fresh(hit):
        return hit[0]
    net = _db_get(_net_key(ip))          # 同一个 /24 查过就直接用，一次接口都不打
    if _fresh(net) and net[0]:
        _db_put([(ip, ip)], net[0])
        return net[0]
    if _allow("single", SINGLE_PER_MIN):
        loc, decided = _lookup_single(ip)
        if loc:
            _db_put([(ip, ip), (_net_key(ip), ip)], loc)
            return loc
        if decided:
            _db_put([(ip, ip)], "")
            return ""
    return _via_batch(ip)


# ---------------- 读出口 IP（三条路，按「最不容易把车道拖死」排顺序） ----------------

_ERR_MARK = re.compile(r"(ERR_[A-Z_]{4,40})")


def why_from_body(text):
    """echo 站没回 IP 时，把页面里的 Chromium 错码挑出来当原因（11-18 实测隧道死口就长这样）。"""
    m = _ERR_MARK.search(str(text or ""))
    return m.group(1) if m else "echo 站无响应"


def read_ip_via_proxy(upstream, timeout_s=3.0):
    """拿这条车道同一个 session 的上游代理串，用 Python 自己发一次 CONNECT 读出口。
    不渲染、不开标签页、socket 超时说话算数（实测隧道死口时它 0.4 秒就回 407/超时，
    而浏览器那边同一个请求能挂几十秒），所以把它放在最前面。
    粘性是厂家按 sid 绑的，同一个 sid 新开会话拿到的是同一个出口；下面 probe_* 会再用
    浏览器自己读一次做对照，两条路对不上就以浏览器为准记日志。
    返回 (ip, 失败原因)。"""
    if not upstream:
        return "", "没配代理"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": upstream, "https": upstream}))
    err = ""
    deadline = time.time() + timeout_s
    for url in ECHO_URLS:
        left = deadline - time.time()
        if left <= 0:
            err = "超时"
            break
        try:
            with opener.open(url, timeout=min(2.5, max(0.4, left))) as resp:
                body = resp.read(300).decode("utf-8", "replace")
            ip = clean_ip(body)
            if ip:
                return ip, ""
            err = why_from_body(body)
        except Exception as exc:
            detail = str(exc)
            mark = _ERR_MARK.search(detail)
            err = (mark.group(1) if mark else f"{type(exc).__name__}:{detail[:40]}")
            if "407" in detail:
                err = "代理407认证失败"
    return "", err[:60]


def read_ip_sync(page, timeout_s=PROBE_TIMEOUT_S, why=None):
    """TPS（Playwright）：先试 page.request.get（不渲染、最省），
    万一它绕过代理回的是本机 IP（那就等于没读到车道出口），退回真开一个临时标签页读。
    返回 (ip, 失败原因)。"""
    budget = time.time() + timeout_s
    mine = local_ip()
    err = ""
    try:
        for url in ECHO_URLS:
            left = budget - time.time()
            if left <= 0.5:
                err = err or "超时"
                break
            try:
                resp = page.request.get(url, timeout=int(min(3000, max(500, left * 1000))))
                body = resp.text()
                ip = clean_ip(body)
            except Exception as exc:
                err = f"{type(exc).__name__}"[:40]
                continue
            if ip and ip != mine:
                return ip, ""
            if ip and ip == mine:
                break          # 这条通道不走代理，换真标签页
            err = why_from_body(body)
    except Exception as exc:
        err = f"{type(exc).__name__}"[:40]
    tab = None
    try:
        tab = page.context.new_page()
        for url in ECHO_URLS:
            left = budget - time.time()
            if left <= 0.5:
                err = err or "超时"
                break
            try:
                tab.goto(url, wait_until="domcontentloaded",
                         timeout=int(min(3000, max(500, left * 1000))))
                body = tab.inner_text("body", timeout=1500)
                ip = clean_ip(body)
            except Exception as exc:
                err = f"{type(exc).__name__}"[:40]
                continue
            if ip:
                return ip, ""
            err = why_from_body(body)
    except Exception as exc:
        err = f"{type(exc).__name__}"[:40]
    finally:
        try:
            if tab is not None:
                tab.close()
        except Exception:
            pass
    return "", err or "echo 站无响应"


async def read_ip_async(browser, timeout_s=PROBE_TIMEOUT_S):
    """FPS（nodriver）：开一个用完就关的临时标签页读 echo（浏览器级代理，走的就是这条隧道）。
    11-18 实测坑：browser.get(url, new_tab=True) 只是把 target 建出来就返回（0.1 秒），
    正文还没落地，直接 get_content() 拿到的是空壳 —— 所以这里必须自己轮询到看见 IP 为止。
    返回 (ip, 失败原因)。"""
    budget = time.time() + timeout_s
    tab = None
    err = ""
    try:
        for url in ECHO_URLS:
            left = budget - time.time()
            if left <= 0.5:
                err = err or "超时"
                break
            ip = ""
            body = ""
            try:
                tab = await asyncio.wait_for(browser.get(url, new_tab=True),
                                             timeout=min(4.0, max(0.6, left)))
                deadline = time.time() + max(0.6, left - 1.0)
                while True:
                    try:
                        body = await asyncio.wait_for(tab.get_content(), timeout=2.0)
                    except Exception:
                        body = ""
                    ip = clean_ip(body)
                    if ip or time.time() >= deadline:
                        break
                    await asyncio.sleep(0.25)
            except Exception as exc:
                err = f"{type(exc).__name__}"[:40]
            finally:
                try:
                    if tab is not None:
                        await asyncio.wait_for(tab.close(), timeout=2.0)
                except Exception:
                    pass
                tab = None
            if ip:
                return ip, ""
            if not err:
                err = why_from_body(body)
    except Exception as exc:
        err = f"{type(exc).__name__}"[:40]
    return "", err or "echo 站无响应"


def is_sticky(template):
    """这条代理写法有没有真的做粘性（sid 绑住一个出口）。
    没粘性的话厂家每次新连接都给随机 IP，Python 那边读到的就不是这条车道在用的那个了，
    这种号只能让浏览器自己读（同一个隧道）。"""
    t = str(template or "")
    return "{session}" in t or "-sid-" in t


def line(ip, loc, secs, why="", sid=""):
    """拼成一行日志正文（车道前缀 [W3] 由引擎自己加）。"""
    if not ip:
        msg = f"出口 IP 没读到（{why or 'echo 站无响应'}）"
    else:
        msg = f"出口 {ip}｜{loc}" if loc else f"出口 {ip}｜归属地未查到"
    msg += f"｜探测 {secs:.1f}s"
    if sid:
        msg += f"｜session={sid}"
    return msg


def no_proxy_configured():
    """代理栏是空的 ⇒ 这条车道根本没走代理，出口就是这台电脑自己的出口。
    这种时候不必去隧道里找（压根没有隧道），直接报本机 IP + 归属地；
    而且本机 IP 一次进程只读一次，第二条车道起就 0 请求了。"""
    return not str(get_setting("proxy", "")).strip()


def probe_sync(page, sid="", upstream=""):
    """TPS 用：整条探活。先用 Python 走同一个 sid 的上游（超时说话算数），
    读不到再退回浏览器自己读；任何异常都吞掉，返回一行日志文本（开关关了返回空串）。"""
    if not enabled():
        return ""
    t0 = time.time()
    ip = why = ""
    try:
        if no_proxy_configured():
            ip = local_ip()
            return "本机直连（没配代理）｜" + line(ip, geo(ip) if ip else "",
                                                time.time() - t0,
                                                "" if ip else "本机 IP 都取不到", sid)
        if upstream:
            ip, why = read_ip_via_proxy(upstream, min(4.0, PROBE_TIMEOUT_S))
        if not ip and page is not None and time.time() - t0 < PROBE_TIMEOUT_S:
            left = max(0.5, PROBE_TIMEOUT_S - (time.time() - t0))
            ip, why2 = read_ip_sync(page, left)
            why = why or why2      # 两条都读不到时优先报上游那条：407/超时是准话，
            # 浏览器那边只会回一句「This site can't be reached」
        loc = geo(ip) if ip else ""
    except Exception as exc:
        return line("", "", time.time() - t0, f"{type(exc).__name__}"[:40], sid)
    return line(ip, loc, time.time() - t0, why, sid)


async def probe_async(browser, sid="", upstream=""):
    """FPS 用：协程版整条探活，任何异常都吞掉，返回一行日志文本（没开开关就返回空串）。"""
    if not enabled():
        return ""
    t0 = time.time()
    ip = why = ""
    try:
        if no_proxy_configured():
            ip = local_ip()
            return "本机直连（没配代理）｜" + line(ip, geo(ip) if ip else "",
                                                time.time() - t0,
                                                "" if ip else "本机 IP 都取不到", sid)
        if upstream:
            ip, why = read_ip_via_proxy(upstream, min(4.0, PROBE_TIMEOUT_S))
        if not ip and browser is not None and time.time() - t0 < PROBE_TIMEOUT_S:
            left = max(0.5, PROBE_TIMEOUT_S - (time.time() - t0))
            ip, why2 = await read_ip_async(browser, left)
            why = why or why2      # 同上：优先报上游那条准话
        loc = geo(ip) if ip else ""
    except Exception as exc:
        return line("", "", time.time() - t0, f"{type(exc).__name__}"[:40], sid)
    return line(ip, loc, time.time() - t0, why, sid)
