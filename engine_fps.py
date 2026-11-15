# -*- coding: utf-8 -*-
"""FastPeopleSearch 核心（第二个核心，面板可切换）。

库、代理转发器、年龄/性别/去重口径全部沿用 1.8.2 那套（直接复用 engine_tps182 与 engine_core），
只有「查询网站」和「页面判定」是 FPS 的。FPS 个人页电话直出，不需要打码，解码费 0。

浏览器用的是 nodriver（真实 Chrome），不是 Playwright：
11-15 同地址同代理同时刻 A/B —— nodriver 7.7 秒出 10 张卡片；
Playwright Chromium 两次各等满 180 秒都停在 886 字节挑战页、0 张卡片。

页面判定规则全部来自 11-15 实测（同批 A/B 验过）：
- 地址页是流式下发的：标题先到（5KB），住户卡片后到（整页 149-242KB），
  所以只认标题或只认长度都会把「还没下完」误判成「这地址没人」。只认内容标记。
- 有一种 100KB 拦截页标题不带任何验证词，只认正文里的 _cf_chl_opt。
- challenge-platform 正常页里也有，不能当验证标记。
- 被弹回首页 ≠ 无住户：必须换 IP 复测，换完还是首页才允许记无数据。
"""
import asyncio
import html as htmlmod
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import engine_tps182 as tps          # 库/转发器/统计/导出全部复用 1.8.2 那套
from engine_core import gender_bucket

DB_PATH = tps.DB_PATH
connect = tps.connect
init_db = tps.init_db
import_addresses = tps.import_addresses
stats = tps.stats
export_csv = tps.export_csv
export_addresses = tps.export_addresses
clean_phone = tps.clean_phone
get_setting = tps.get_setting
set_setting = tps.set_setting
age_ok = tps.age_ok
start_relay = tps.start_relay

FPS_HOST = "https://www.fastpeoplesearch.com"
LISTING_PAGE_SIZE = 10

# 验证页标记：标题 + 正文双查（实测有一种 100KB 页标题不带任何词）
CF_TITLE_MARKERS = ("security challenge", "attention required", "just a moment",
                    "captcha", "too many requests", "access denied")
CF_BODY_MARKERS = ("_cf_chl_opt", "cf_chl_uk", "cf_chl_captcha_tk", "cf-turnstile")
# 被弹回首页：真无住户和被限流都长这样，靠换 IP 复测区分
HOME_TITLE = "free people search | fastpeoplesearch.com"
# Chrome 自己的断连报错页（实测元凶：厂家约 5~11% 死口 + 本地中继上游连不上时掐 socket）。
# 这种页没有验证标记也没有卡片，以前被当成 loading 白等满 200 秒 —— 必须立刻认出来换 IP。
NETERR_MARKERS = ("err_connection_closed", "err_connection_reset", "err_connection_refused",
                  "err_timed_out", "err_internet_disconnected", "err_socket_not_connected",
                  "err_network_changed", "err_empty_response", "err_name_not_resolved",
                  "err_proxy_connection_failed", "err_tunnel_connection_failed")

# 实测定案：只拦图片+媒体（按扩展名），字体/样式放行；全拦会把挑战摊薄弄坏
BLOCK_URL_PATTERNS = ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.ico",
                      "*.mp4", "*.webm", "*.mp3", "*.avi", "*.mov"]

# 已验证可用的 Chrome 启动参数（照抄独立版，缺 --disable-*-backgrounding 时挑战页会被限速卡死）
BROWSER_ARGS = ["--no-first-run", "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-component-update",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps", "--disable-extensions", "--disable-sync",
                "--disable-domain-reliability", "--disable-client-side-phishing-detection",
                "--disable-features=OptimizationHints,MediaRouter,Translate,"
                "InterestFeedContentSuggestions,PrivacySandboxSettings4",
                "--window-size=1024,768", "--mute-audio"]


class FpsBlocked(RuntimeError):
    """验证页反复出现，换 IP 仍不通过 —— 绝不能当成『该地址查无此人』。"""


# ---------------- 地址 → FPS 网址 ----------------

def fps_slug(address):
    m = re.match(r'(.+?),\s*(.+?),\s*([A-Z]{2})\s*(\d{5})', str(address).strip())
    if not m:
        raise ValueError(f"地址格式无法解析：{address}")
    street, city, state, zipcode = m.groups()

    def slugify(value):
        value = value.lower().replace("&", "and")
        return re.sub(r"[^a-z0-9]+", "-", value).strip("-")

    return f"{slugify(street)}_{slugify(city)}-{state.lower()}-{zipcode}"


def build_url(address):
    return f"{FPS_HOST}/address/{fps_slug(address)}"


# ---------------- 页面判定 ----------------

def _title_of(content):
    m = re.search(r"<title[^>]*>(.*?)</title>", content or "", re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip().lower() if m else ""


def _is_challenge(content):
    text = content or ""
    title = _title_of(text)
    if not title:
        return True
    if any(k in title for k in CF_TITLE_MARKERS):
        return True
    return any(k in text for k in CF_BODY_MARKERS)


def page_state(content, kind="listing"):
    """challenge=验证页 / bounced=被弹回首页 / neterror=出口断连报错页 /
    loading=标题对了但内容还在流 / ready=内容到位"""
    text = content or ""
    low = text.lower()
    if any(k in low for k in NETERR_MARKERS):
        return "neterror"
    if _is_challenge(text):
        return "challenge"
    if _title_of(text) == HOME_TITLE:
        return "bounced"
    if kind == "person":
        ok = 'id="full_name_section"' in text or 'id="phone_number_section"' in text
    else:
        ok = '"card" id=' in text
    return "ready" if ok else "loading"


def _txt(s):
    s = re.sub(r"<br\s*/?>", ", ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(htmlmod.unescape(s).split()).strip(" ,")


def _absolute(url):
    url = str(url or "").strip()
    return FPS_HOST + url if url.startswith("/") else url


# ---------------- 解析（与已定案的 FPS 引擎逐条一致） ----------------

ANCHORS = ['id="full_name_section"', 'id="current_address_section"', 'id="phone_number_section"',
           'id="email_section"', 'id="aka-links"', 'id="previous-addresses"', 'id="relative-links"',
           'id="associate-links"', 'id="marital_status_section"', 'id="current_employment_section"',
           'id="work_experience_section"', 'id="background_report_section"', 'id="faq_section"']


def _section(page, anchor):
    i = page.find(anchor)
    if i < 0:
        return ""
    end = len(page)
    for a in ANCHORS:
        j = page.find(a, i + len(anchor))
        if 0 < j < end:
            end = j
    return page[i:end]


def parse_listing(content, url=""):
    people = []
    blocks = re.split(r'<div class="card" id="(G-?\d+)"', content)
    for i in range(1, len(blocks), 2):
        pid, body = blocks[i], blocks[i + 1]
        mh = re.match(r'\s*data-link="(/[^"]*)"', body)
        href = mh.group(1) if mh else ""
        m = re.search(r'<span class="larger">(.*?)</span>(.*?)</a>', body, re.S)
        if not m:
            continue
        name = _txt(m.group(1))
        meta = _txt(m.group(2))
        ma = re.search(r"Age\s+(\d{1,3})", meta)
        age = ma.group(1) if ma else ""
        loc = re.sub(r"Age\s+\d{1,3}", "", meta).strip(" •,")
        people.append({"id": pid, "link": _absolute(href), "name": name,
                       "age": age, "location": loc})
    return people


def parse_person(content, url=""):
    name = ""
    m = re.search(r'<span class="fullname">(.*?)</span>', content, re.S)
    if m:
        name = _txt(m.group(1))
    if not name:
        m = re.search(r"<title>(.*?)\(\d+\)", content, re.S)
        if m:
            name = _txt(m.group(1))

    age = ""
    mt = re.search(r"<title>(.*?)</title>", content, re.S)
    title = mt.group(1) if mt else content[:2000]
    # 标题里先找明写的 Age；找不到才退而看括号数字 —— 那个括号经常是电话区号
    # （实测 Chesterfield MA 两个人读出 413 岁 = (413) 区号），所以超过 120 一律不认。
    m = re.search(r"\bAge[:\s]*(\d{1,3})\b", title, re.I) or re.search(r"\((\d{1,3})\)", title)
    if m and 1 <= int(m.group(1)) <= 120:
        age = m.group(1)

    phones = []
    for m in re.finditer(r'href="/(\d{3})-(\d{3})-(\d{4})"', _section(content, 'id="phone_number_section"')):
        phone = "(%s) %s-%s" % m.groups()
        if phone not in phones:
            phones.append(phone)

    current = ""
    m = re.search(r'<a[^>]*href="/address/[^"]*"[^>]*>(.*?)</a>',
                  _section(content, 'id="current_address_section"'), re.S)
    if m:
        current = _txt(m.group(1))
    return {"name": name, "age": age, "location": current,
            "phones": ",".join(phones)}


def next_listing_url(content, current_url):
    m = re.search(r'href="([^"]+/page/\d+)"[^>]*title="Next page', content)
    if not m:
        return ""
    nxt = _absolute(m.group(1))
    return "" if nxt.rstrip("/") == current_url.rstrip("/") else nxt


# ---------------- 抓取（nodriver） ----------------

async def _content(tab):
    try:
        return await tab.get_content()
    except Exception:
        return ""


async def _clear_cookies(tab):
    """换 IP 后旧 cf_clearance 必须清掉，否则新 IP 带着旧票反而一直出验证页。"""
    try:
        from nodriver import cdp
        await tab.send(cdp.network.clear_browser_cookies())
    except Exception:
        pass


async def _fetch(tab, relay, url, kind, log, timeout_s, rotations, sid_base):
    """导航并等内容到位，等不住或被弹回首页就地换出口 IP 重试同一页（浏览器不重开）。

    返回 ""：换了 IP 还是被弹回首页 —— 这才是「这地址真没人」的唯一证据。
    抛 FpsBlocked：验证页压不住 —— 绝不能当成没人。
    """
    last_err = ""
    for rot in range(max(0, int(rotations)) + 1):
        try:
            await tab.get(url)
        except Exception as exc:
            last_err = f"导航失败 {type(exc).__name__}"
        else:
            t0 = time.time()
            last_state = ""
            while True:
                content = await _content(tab)
                state = page_state(content, kind)
                if state == "ready":
                    return content
                if state == "bounced":
                    last_err = "被弹回首页"
                    log(f"被弹回首页（不等于真无住户，{relay.bytes_transferred // 1024}KB）")
                    break
                if state == "neterror":
                    code = next((k.upper() for k in NETERR_MARKERS
                                 if k in (content or "").lower()), "NETERR")
                    last_err = f"出口断连 {code}"
                    log(f"页面报 {code}（出口死口，不是这地址没人）"
                        f"，不等了直接换新 IP（{relay.bytes_transferred // 1024}KB）")
                    break
                if state != last_state:
                    log(f"这页内容还没到位（{state} {len(content or '')} 字节，"
                        f"标题『{_title_of(content)[:40]}』）")
                    last_state = state
                if time.time() - t0 >= timeout_s:
                    last_err = f"{int(time.time() - t0)} 秒没出内容（{state}）"
                    log(f"{last_err}，换新 IP（{relay.bytes_transferred // 1024}KB）")
                    break
                await asyncio.sleep(4)
        if rot >= rotations:
            if last_err == "被弹回首页":
                return ""
            raise FpsBlocked(f"{last_err}，换了 {rot} 次 IP 仍不行")
        sid = f"{sid_base}_{rot + 1}"
        relay.set_session(sid)
        await _clear_cookies(tab)
        await asyncio.sleep(3)
    raise FpsBlocked("FPS 换 IP 循环异常退出")


def _open_relay(proxy_template, proxy_pass, session):
    return tps.start_relay(_sticky_template(proxy_template), proxy_pass, session)


def _sticky_template(proxy_template):
    """把用户粘的一整串代理改成带粘性会话的模板：用户名尾部补 -sid-{session}-t-<分钟>。
    实测：不带 sid = 每次连接随机换 IP（会把 cf_clearance 弄失效）；带 sid = 粘住一个 IP。
    每次尝试换一个 sid，就是「一个地址一个出口 IP」，换 IP 才真换得动。"""
    p = str(proxy_template or "").strip()
    if not p:
        raise RuntimeError("未填写代理")
    if "{session}" in p:
        return p
    sticky_min = int(get_setting("sticky_min", 30))
    scheme, rest = ("http", p) if "://" not in p else p.split("://", 1)
    if "@" not in rest:
        raise RuntimeError(f"代理格式错误：{proxy_template}（需要 user:pass@host:port）")
    cred, host = rest.rsplit("@", 1)
    if ":" not in cred:
        raise RuntimeError(f"代理缺密码：{proxy_template}")
    user, pwd = cred.split(":", 1)
    user = re.sub(r"-sid-[^-]*-t-\d+", f"-sid-{{session}}-t-{sticky_min}", user)
    if "-sid-" not in user:
        user = f"{user}-sid-{{session}}-t-{sticky_min}"
    return f"{scheme}://{user}:{pwd}@{host}"


def _chrome_candidates():
    """按优先级列出可用浏览器：配置指定的 → 系统真 Chrome → 随包带的 Chromium（最后兜底）。

    顺序不能反：实测 FPS 只有真实 Chrome 能自过托管挑战，Playwright 那份 Chromium
    两次各等满 180 秒都停在『attention required | cloudflare』、0 张卡片。
    冻结成 exe 后 root 就是交付夹，里面正有 ms-playwright\\chromium-*，摆前面会抢先选中它。
    """
    out = []
    p = str(get_setting("fps_chrome_path", "") or "").strip()
    if p:
        out.append(p)
    env = os.environ
    for base in (env.get("PROGRAMFILES", r"C:\Program Files"),
                 env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 env.get("LOCALAPPDATA", "")):
        if base:
            out.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    root = Path(getattr(sys, "frozen", False) and os.path.dirname(sys.executable)
                or os.path.dirname(os.path.abspath(__file__)))
    for pat in (r"ms-playwright\chromium-*\chrome-win\chrome.exe",
                r"ms-playwright\chromium-*\chrome-win64\chrome.exe"):
        out += sorted(str(x) for x in root.glob(pat))
    return [x for x in out if not x or Path(x).exists()] or [""]


async def _open_browser(relay, headless, log):
    """一个尝试 = 一个出口 IP = 一个浏览器：换 IP 靠重开，不用同一个浏览器里换 session。"""
    import nodriver as uc
    args = [f"--proxy-server={relay.server}"] + BROWSER_ARGS
    browser = None
    why = ""
    for path in _chrome_candidates():
        kwargs = {"headless": headless, "browser_args": args, "sandbox": False}
        if path:
            kwargs["browser_executable_path"] = path
        try:
            browser = await uc.start(**kwargs)
            log(f"浏览器：{path or 'nodriver 自动找的系统 Chrome'}")
            if "ms-playwright" in str(path):
                log("[!] 这台电脑没装 Google Chrome，只能用包内 Chromium 兜底 —— "
                    "实测 FPS 这个站过不了挑战（等满 180 秒 0 张卡片），装一下 Chrome 再跑")
            break
        except Exception as exc:
            why = f"{type(exc).__name__}: {str(exc)[:80]}"
    if browser is None:
        raise RuntimeError(f"启动 Chrome 失败（{why}）——装一下 Chrome，或在配置里填浏览器完整路径")
    tab = await browser.get("about:blank")
    try:
        from nodriver import cdp
        await tab.send(cdp.network.enable())
        await tab.send(cdp.network.set_blocked_urls(urls=list(BLOCK_URL_PATTERNS)))
    except Exception:
        pass
    return browser, tab


async def _run_attempt(address, url, relay, opt):
    """跑一轮（一个浏览器）。返回 (kind, 计数字典, 剩余待做的人, 错因)。
    kind: done/blocked/nodata/error"""
    _log = opt["log"]
    now = opt["now"]
    headless = opt["headless"]
    browser = tab = None
    c = {"saved": 0, "phones": 0, "cards": 0, "skipped_age": 0, "skipped_gender": 0,
         "dup": 0, "no_phone": 0, "pages": 0}
    targets, seen, pending = [], set(), []
    kind = "done"
    err = ""
    try:
        browser, tab = await _open_browser(relay, headless, _log)

        # ===== 列表页（含翻页）=====
        listing_url = url
        cap = int(opt["max_persons"])
        try:
            for _ in range(max(1, opt["max_pages"])):
                content = await _fetch(tab, relay, listing_url, "listing", _log,
                                       opt["challenge_wait"], opt["rotations"], opt["sid"])
                c["pages"] += 1
                if content == "":
                    # 换过 IP 还是被弹回首页：这才允许判这个地址没人
                    _log(f"空(FPS): 换了 {opt['rotations']} 次出口 IP 都被弹回首页"
                         f"（卡片{c['cards']}张，流量{relay.bytes_transferred // 1024}KB）")
                    return "nodata", c, [], "换IP复测仍弹回首页"
                found = parse_listing(content, listing_url)
                if not found:
                    break
                for person in found:
                    c["cards"] += 1
                    try:
                        a = int(str(person["age"] or "").strip())
                    except ValueError:
                        a = None
                    if a is not None and not (opt["min_age"] <= a <= opt["max_age"]):
                        c["skipped_age"] += 1
                        continue
                    if person["link"] in seen:
                        c["dup"] += 1
                        continue
                    seen.add(person["link"])
                    targets.append(person)
                    if cap and len(targets) >= cap:
                        break
                if len(found) < LISTING_PAGE_SIZE or (cap and len(targets) >= cap):
                    break
                nxt = next_listing_url(content, listing_url)
                if not nxt:
                    break
                listing_url = nxt
        except FpsBlocked as exc:
            kind, err = "blocked", str(exc)

        _log(f"列表：卡片{c['cards']}张 → 待做{len(targets)}人"
             f"（年龄不符{c['skipped_age']} 重复{c['dup']}）"
             f" 流量:{relay.bytes_transferred // 1024}KB")
        if kind != "done":
            return kind, c, list(targets), err

        # ===== 个人页取电话 =====
        pending = list(targets)
        done = opt.setdefault("done", set())
        while pending and kind == "done":
            person = pending[0]
            if person["link"] in done:
                pending.pop(0)
                continue
            if opt["keep_genders"] is not None and gender_bucket(person["name"]) not in opt["keep_genders"]:
                c["skipped_gender"] += 1
                pending.pop(0)
                continue
            try:
                content = await _fetch(tab, relay, person["link"], "person", _log,
                                       opt["challenge_wait"], opt["rotations"], opt["sid"])
            except FpsBlocked as exc:
                kind, err = "blocked", str(exc)
                _log(f"个人页验证压不住：{exc}，换新 IP 续做剩余 {len(pending)} 人")
                break
            if content == "":
                # 个人页被弹回首页：这既不是「地址没人」也不是「这人没号」，是出口 IP 被弹。
                # 这个人必须留在待做里，换新 IP 从这个人接着做，绝不能当成做完了。
                kind, err = "blocked", "个人页被弹回首页（出口 IP 被弹，不是没人）"
                _log(f"个人页被弹回首页：{person['name'][:20]} 起剩 {len(pending)} 人"
                     f"换新 IP 接着做（{relay.bytes_transferred // 1024}KB）")
                break
            pending.pop(0)
            done.add(person["link"])
            rec = parse_person(content, person["link"])
            if not rec["name"]:
                _log(f"个人页没解析出姓名，跳过不入库（{relay.bytes_transferred // 1024}KB）")
                continue
            if not rec["age"] and person.get("age"):
                # 个人页标题那个括号数字不可信时（区号 413 被当年龄），退回列表页读到的年龄，
                # 「能有年龄必须带年龄」，别因为一处解析脏就把年龄交空。
                rec["age"] = person["age"]
            try:
                page_age = int(str(rec["age"]).strip())
            except ValueError:
                page_age = None
            if page_age is not None and not (opt["min_age"] <= page_age <= opt["max_age"]):
                # 列表年龄和个人页年龄能差十好几岁（实测 103、113 都冒出来过），以个人页为准再拦一道
                c["skipped_age"] += 1
                _log(f"  {rec['name']} 个人页 {page_age}岁 不在 "
                     f"{opt['min_age']}-{opt['max_age']}，不入库")
                continue
            phones = [p for p in rec["phones"].split(",") if p.strip()]
            with connect() as con:
                # 同一个人重复采到：只有这次真拿到号码才覆盖，防止无号重扫把号码抹掉
                con.execute("INSERT INTO results"
                            "(address,name,age,location,phones,created_at) VALUES(?,?,?,?,?,?)"
                            " ON CONFLICT(address,name,age) DO UPDATE SET"
                            " location=excluded.location, phones=excluded.phones,"
                            " created_at=excluded.created_at"
                            " WHERE excluded.phones<>''",
                            (address, rec["name"], rec["age"], rec["location"],
                             rec["phones"], now))
            c["saved"] += 1
            c["phones"] += len(phones)
            if not phones:
                c["no_phone"] += 1
            _log(f"  {rec['name']} {rec['age']}岁 号码{len(phones)}个（出表只写第一个）"
                 f" 累计{c['saved']}人/全部号{c['phones']} 流量:{relay.bytes_transferred // 1024}KB")
        return kind, c, pending, err
    except Exception as exc:
        return "error", c, pending, f"{type(exc).__name__}: {exc}"
    finally:
        if browser is not None:
            try:
                browser.stop()
            except Exception:
                pass


def process_address(address, proxy_template, proxy_pass, capsolver_key, worker_id=0,
                    max_pages=None, progress_cb=None, max_attempts=12, solve_captcha=True,
                    max_persons=None, keep_genders=("male", "andy", "unknown")):
    """查询一个地址：列表页（含翻页）→ 逐个个人页取电话。
    年龄口径沿用 settings 的 min_age/max_age（默认 50-85），性别默认剔女（keep_genders=None 则不筛）。
    max_pages/max_persons 留空走设置里的 fps_max_pages / fps_max_persons（人数 0 = 不封顶）。
    返回 (True, 统计) / (False, 统计)，与 TPS 核心同形状，界面代码不用改。
    """
    def _log(msg):
        if progress_cb:
            try:
                progress_cb(f"[W{worker_id}] {msg}")
            except Exception:
                pass

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        url = build_url(address)
    except ValueError as exc:
        _log(f"✗ {exc}")
        with connect() as con:
            con.execute("UPDATE addresses SET status='failed',error=?,updated_at=? WHERE address=?",
                        (str(exc), now))
        return False, {"error": str(exc)}

    _log(f"开始(FPS): {address[:40]}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='running',updated_at=? WHERE address=?",
                    (now, address))

    if max_pages is None:
        max_pages = int(get_setting("fps_max_pages", 10))
    if max_persons is None:
        max_persons = int(get_setting("fps_max_persons", 0))
    # 弹回首页 ≠ 没人：线路被限流时好地址也整批弹首页（11-15 实测：两小时前抓到 26 人的
    # 地址，刚连吃 3 个出口 IP 全弹首页）。判「无人」必须换整副浏览器+IP 复测够轮数才算。
    confirm_nodata = max(1, int(get_setting("fps_nodata_confirm", 2)))
    nodata_rounds = 0

    opt = {"log": _log, "now": now, "min_age": int(get_setting("min_age", tps.MIN_AGE)),
           "max_age": int(get_setting("max_age", tps.MAX_AGE)),
           "challenge_wait": int(get_setting("challenge_wait", 200)),
           "rotations": int(get_setting("fps_rotations", 2)),
           "headless": get_setting("fps_headless", "0") == "1",
           "max_pages": max(1, int(max_pages)),
           "max_persons": max(0, int(max_persons)),
           "keep_genders": keep_genders}

    last_error = ""
    totals = {"saved": 0, "phones": 0, "cards": 0, "skipped_age": 0,
              "skipped_gender": 0, "dup": 0, "no_phone": 0}
    people_total = 0
    traffic_kb = 0

    for attempt in range(max_attempts):
        session = f"fps{int(time.time() * 1000) % 1000000}_{worker_id}_{attempt}"
        relay = _open_relay(proxy_template, proxy_pass, session)
        opt["sid"] = session
        _log(f"第{attempt + 1}/{max_attempts}次尝试，出口 session={session}")
        try:
            kind, c, pending, err = asyncio.run(_run_attempt(address, url, relay, opt))
        except Exception as exc:
            kind, c, pending, err = "error", {}, [], f"{type(exc).__name__}: {exc}"
        if err:
            last_error = err
        for k in totals:
            totals[k] += c.get(k, 0)
        traffic_kb = max(traffic_kb, relay.bytes_transferred // 1024)
        relay.stop()

        if kind == "nodata":
            nodata_rounds += 1
            if nodata_rounds < confirm_nodata:
                _log(f"这轮 {opt['rotations'] + 1} 个出口 IP 都被弹回首页——先不判「没人」，"
                     f"换新浏览器+新 IP 再复测（第 {nodata_rounds}/{confirm_nodata} 轮）")
                continue
            why = f"换 IP 复测 {nodata_rounds} 轮（共 {(opt['rotations'] + 1) * nodata_rounds} 个出口）仍弹回首页"
            with connect() as con:
                con.execute("UPDATE addresses SET status='nodata',records=0,people=0,"
                            "phones=0,traffic_kb=?,error=?,updated_at=?"
                            " WHERE address=?", (traffic_kb, why, now, address))
            return True, {"records": 0, "people": 0, "phones": 0, "list": 0,
                          "title": "fastpeoplesearch", "traffic_kb": traffic_kb,
                          "skipped_blocked": 0, "skipped_gender": totals["skipped_gender"],
                          "skipped_low": totals["skipped_age"], "skipped_high": 0,
                          "dup_skipped": totals["dup"], "no_phone": totals["no_phone"],
                          "dead_skipped": 0, "cards_seen": totals["cards"],
                          "abandoned": 0, "nodata": True}

        people_total = max(people_total, totals["saved"] + len(pending))

        if kind in ("blocked", "error"):
            _log(f"被拦/换 IP：{last_error or '验证压不住'}，剩余 {len(pending)} 人下一轮接着做")
            if not last_error:
                last_error = "FPS 验证页压不住"
            continue

        with connect() as con:
            con.execute("UPDATE addresses SET status='done',records=?,people=?,phones=?,"
                        "traffic_kb=?,error='',updated_at=? WHERE address=?",
                        (people_total, totals["saved"], totals["phones"], traffic_kb, now, address))
        _log(f"✓ 完成(FPS): 卡片{totals['cards']}张 → 存{totals['saved']}人"
             f"（有号{totals['saved'] - totals['no_phone']}/无号{totals['no_phone']}）"
             f"全部号{totals['phones']}（出表每人只写第一个）；跳过: 年龄不符{totals['skipped_age']} "
             f"性别不符{totals['skipped_gender']} 重复{totals['dup']}；流量{traffic_kb}KB")
        return True, {"records": people_total, "people": totals["saved"],
                      "phones": totals["phones"], "list": people_total,
                      "title": "fastpeoplesearch", "traffic_kb": traffic_kb,
                      "skipped_blocked": 0, "skipped_gender": totals["skipped_gender"],
                      "skipped_low": totals["skipped_age"], "skipped_high": 0,
                      "dup_skipped": totals["dup"], "no_phone": totals["no_phone"],
                      "dead_skipped": 0, "cards_seen": totals["cards"],
                      "abandoned": len(pending)}

    _log(f"✗ {max_attempts}次全部失败: {last_error}")
    with connect() as con:
        con.execute("UPDATE addresses SET status='failed',error=?,updated_at=? WHERE address=?",
                    (last_error[:300], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return False, {"error": last_error, "title": "fastpeoplesearch"}
