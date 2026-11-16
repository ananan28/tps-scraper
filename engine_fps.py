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
import ctypes
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
                  "err_proxy_connection_failed", "err_tunnel_connection_failed",
                  "err_too_many_redirects")

# 限流页/拉黑页签名（Temp\sourcescan\rate_hit 与 rate_hit2 归档的 31 份原件实扫定案）：
# 这类页一个都不带 _cf_chl_opt，而真挑战页 11/11 都带 —— 所以它们不是「等一等就自过」的页面，
# 同一条 IP 再等满 challenge_wait 也原样吐回来。旧判定把它们全认成 loading/challenge，
# 每页白等 200 秒 × (换 IP 次数+1) ≈ 10 分钟，这就是「别的机器一开机就卡死」的真相。
CHL_OPT_MARKER = "_cf_chl_opt"
RATE_TITLE_MARKERS = ("rate limited", "too many requests")
RATE_BODY_MARKERS = ("rate limit exceeded", "you have exceeded the amount of requests",
                     "allotted time limit", "this ip has been rate limited")
# CF 硬拒页：标题是 attention required 但正文写的是「你已经被拒」，不是「正在验证」
HARD_BLOCK_MARKERS = ("sorry, you have been blocked", "you've been blocked",
                      "you are unable to access")

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
                "InterestFeedContentSuggestions,PrivacySandboxSettings4,"
                "CalculateNativeWinOcclusion",
                # 界面语言设成英文：站点是英文页，中文界面的 Chrome 会在屏幕左上角弹「翻译此页？」
                # 小窗（实测挪出屏幕时 2 个地址弹出 11 个，--disable-features=Translate 压不住）。
                # 英文界面 + 英文页面就不弹，顺带 navigator.language 也和美国出口 IP 更一致。
                "--lang=en-US",
                "--window-size=1024,768", "--mute-audio"]

# 「隐藏窗口」= 把窗口挪到屏幕外面，不是无头。
# 无头实测过不了这个站的挑战（连换 3 个出口 IP 各等满 200 秒 0 张卡片），
# 挪出屏幕窗口状态仍是 normal，渲染不被降频；配合上面两条 backgrounding 参数才成立。
OFFSCREEN_ARGS = ["--window-position=-32000,-32000"]


class FpsBlocked(RuntimeError):
    """验证页反复出现，换 IP 仍不通过 —— 绝不能当成『该地址查无此人』。"""


class FpsNeedNewIp(RuntimeError):
    """撞上限流/拉黑这种「再等多久都原样返回」的硬页。
    实测同一副浏览器里 relay.set_session() 换 5 次 sid，出口 IP 一次没变（1/5 唯一，
    隧道是复用的，改用户名只对新建隧道生效）—— 就地换 IP 是假的，
    必须把这一轮整副浏览器废掉重开，才真换得到出口。"""


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


def _hard_page_state(text):
    """限流/拉黑页返回 ratelimited / hardblock，其它返回 ""。
    带 _cf_chl_opt 的一律不当硬页：那是会自过的真挑战，等是对的。"""
    low = (text or "").lower()
    if CHL_OPT_MARKER in low:
        return ""
    title = _title_of(low)
    if any(k in title for k in RATE_TITLE_MARKERS) or any(k in low for k in RATE_BODY_MARKERS):
        return "ratelimited"
    if any(k in low for k in HARD_BLOCK_MARKERS):
        return "hardblock"
    return ""


def page_state(content, kind="listing"):
    """challenge=验证页 / ratelimited=这 IP 被限流 / hardblock=这 IP 被拉黑 /
    bounced=被弹回首页 / neterror=出口断连报错页 / loading=标题对了但内容还在流 / ready=内容到位"""
    text = content or ""
    low = text.lower()
    if any(k in low for k in NETERR_MARKERS):
        return "neterror"
    hard = _hard_page_state(text)
    if hard:
        return hard
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
    """导航并等内容到位；等不住/被弹回首页就在同一副浏览器里重载重试（注意：实测
    relay.set_session 换 sid 换不动出口 IP，隧道复用，所以这里换的只是重载次数）。

    返回 ""：换着重载还是被弹回首页 —— 这才是「这地址真没人」的唯一证据。
    抛 FpsNeedNewIp：撞上限流/拉黑硬页，必须整副浏览器重开才换得到 IP。
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
                        f"，不等了直接重载重试（{relay.bytes_transferred // 1024}KB）")
                    break
                if state in ("ratelimited", "hardblock"):
                    # 立刻再看一眼确认（只有可能是半加载撞字样时才会翻案），是硬页就绝不等：
                    # 这两类页不带 _cf_chl_opt，不是会自过的挑战，等到天黑也原样返回。
                    again = await _content(tab)
                    state2 = page_state(again, kind)
                    if state2 == state:
                        raise FpsNeedNewIp(
                            f"出口 IP 被{'限流' if state == 'ratelimited' else '拉黑'}"
                            f"（标题『{_title_of(content)[:26]}』 {len(content or '')} 字节，"
                            f"{relay.bytes_transferred // 1024}KB）")
                    content, state = again, state2
                    if state == "ready":
                        return content
                if state != last_state:
                    log(f"这页内容还没到位（{state} {len(content or '')} 字节，"
                        f"标题『{_title_of(content)[:40]}』）")
                    last_state = state
                if time.time() - t0 >= timeout_s:
                    last_err = f"{int(time.time() - t0)} 秒没出内容（{state}）"
                    log(f"{last_err}，同出口重载重试（{relay.bytes_transferred // 1024}KB）")
                    break
                await asyncio.sleep(4)
        if rot >= rotations:
            if last_err == "被弹回首页":
                return ""
            raise FpsBlocked(f"{last_err}，同一条出口重载 {rot + 1} 次仍不行")
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
    # 实测坑：旧正则 -sid-[^-]*-t-\d+ 匹配不了带横杠的 sid（如 sid-AB-C123-t-30），
    # 整条原样留下 ⇒ 所有车道共用同一个写死的 sid ⇒ 厂商只给一个出口 IP ⇒ 一开机就成片限流。
    user = re.sub(r"-sid-.+?(?:-t-\d+|$)", f"-sid-{{session}}-t-{sticky_min}", user, count=1)
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


STOPPING = False


def begin_shutdown():
    """界面正在关：从现在起一个浏览器都不许再开，免得收尾那几秒又冒出孤儿进程。"""
    global STOPPING
    STOPPING = True


def _proc_table():
    """一次进程快照 → {pid: (小写进程名, 父pid)}。非 Windows 回空表。"""
    if os.name != "nt":
        return {}
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_ssize_t
    k32.Process32First.restype = ctypes.c_int
    k32.Process32Next.restype = ctypes.c_int

    class _E(ctypes.Structure):
        # 布局必须和 Windows 的 PROCESSENTRY32W 一模一样：dwSize 后面还有 cntUsage，
        # th32DefaultHeapID 是 ULONG_PTR（8 字节对齐把后面顶到偏移 44）。
        # 少写 cntUsage 或按 CHAR*260 填，Process32First 会直接返回 0（实测遍历到 0 个进程）。
        _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_uint32),
                    ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_int), ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * 260)]

    h = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
    if h == -1:
        return {}
    out = {}
    try:
        e = _E()
        e.dwSize = ctypes.sizeof(_E)
        ok = k32.Process32FirstW(h, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = ((e.szExeFile or "").lower(), e.th32ParentProcessID)
            ok = k32.Process32NextW(h, ctypes.byref(e))
    finally:
        k32.CloseHandle(h)
    return out


def _browser_descendants(table, root_pid):
    """返回 (root_pid 名下所有浏览器进程, 其中该立刻杀的「主进程」)。
    root_pid 自己就是 Chrome 时也算进来 —— 引擎收尾时传的就是那台浏览器自己的 pid，
    漏了它等于只杀子进程，主进程会把渲染进程重新拉起来。"""
    kids = {}
    for pid, (_n, par) in table.items():
        kids.setdefault(par, []).append(pid)
    found, heads, stack = [], [], [root_pid]
    if table.get(root_pid, ("", 0))[0].startswith(("chrome", "chromium")):
        found.append(root_pid)
        heads.append(root_pid)
    while stack:
        for c in kids.get(stack.pop(), []):
            if table[c][0].startswith(("chrome", "chromium")):
                found.append(c)
                if table[c][1] == root_pid:
                    heads.append(c)
            stack.append(c)
    return found, heads


def browser_tree(root_pid):
    """我名下（root_pid 往下）的所有浏览器进程 pid。
    他自己双击开的 Chrome 父进程是 explorer，永远不在这棵树里 ⇒ 这个函数碰不到他的窗口。"""
    return _browser_descendants(_proc_table(), root_pid)[0]


def _terminate(pids):
    k = ctypes.windll.kernel32
    gone = 0
    for pid in pids:
        h = k.OpenProcess(0x0001, False, pid)         # PROCESS_TERMINATE
        if not h:
            continue
        try:
            if k.TerminateProcess(h, 1):
                gone += 1
        finally:
            k.CloseHandle(h)
    return gone


def _alive(pid):
    """进程断气了没（PROCESS_QUERY_LIMITED_INFORMATION + STILL_ACTIVE）。"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == 259
    finally:
        k.CloseHandle(h)


def kill_browser_tree(root_pid):
    """收掉 root_pid 名下的浏览器进程。只按父子关系找，绝不按进程名满机器扫。
    顺序必须「先杀 Chrome 主进程」：主进程还活着会不停把渲染/GPU 进程重新拉起来，
    反过来由下往上杀就是打地鼠（实测杀 6 个反而又冒出 6 个）。
    主进程断气后 Chrome 子进程自己会退，再补两轮扫荡收尾。返回清掉的个数。"""
    if os.name != "nt":
        return 0
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    found, heads = _browser_descendants(_proc_table(), root_pid)
    if not found:
        return 0
    _terminate(heads)                                  # 主进程先走
    rest = [p for p in found if p not in set(heads)]
    for _ in range(2):
        if not rest:
            break
        time.sleep(0.3)
        _terminate(rest)
        rest = [p for p in rest if _alive(p)]
    return len(found) - len(rest)


async def _close_browser(browser, log=None):
    """用完一个浏览器必须整棵树收掉。
    以前这里只调 nodriver 的 browser.stop()：事件循环还活着时它能把 Chrome 收干净（实测），
    但界面一关，工作线程是被解释器硬冻住的，finally 根本走不到 —— 11-16 实测他关掉界面之后
    还有 84 个 chrome.exe / 5.9GB 挂在程序进程底下。所以这里不指望 stop()，
    自己断开 CDP 再按 pid 把整棵 Chrome 收掉（含临时 profile）。"""
    if browser is None:
        return
    pid = getattr(browser, "_process_pid", None)
    try:
        await asyncio.wait_for(browser.aclose(), timeout=3)
    except Exception:
        pass
    if not pid:
        try:
            browser.stop()
        except Exception:
            pass
        return
    kill_browser_tree(pid)
    try:
        browser._process = None
        browser._process_pid = None
    except Exception:
        pass
    cfg = getattr(browser, "config", None)
    if cfg is not None and not getattr(cfg, "uses_custom_data_dir", True):
        import shutil

        for _ in range(2):
            try:
                shutil.rmtree(cfg.user_data_dir)
                break
            except Exception:
                await asyncio.sleep(0.2)


async def _open_browser(relay, headless, log, hide=False):
    """一个尝试 = 一个出口 IP = 一个浏览器：换 IP 靠重开，不用同一个浏览器里换 session。"""
    if STOPPING:
        raise RuntimeError("程序正在关闭，不再开新浏览器")
    import nodriver as uc
    args = [f"--proxy-server={relay.server}"] + BROWSER_ARGS
    if hide and not headless:
        args += OFFSCREEN_ARGS
    browser = None
    why = ""
    for path in _chrome_candidates():
        kwargs = {"headless": headless, "browser_args": args, "sandbox": False}
        if path:
            kwargs["browser_executable_path"] = path
        try:
            browser = await uc.start(**kwargs)
            mode = "无头" if headless else ("窗口挪到屏幕外（看不见）" if hide else "窗口可见")
            log(f"浏览器：{path or 'nodriver 自动找的系统 Chrome'}｜{mode}")
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
        browser, tab = await _open_browser(relay, headless, _log, opt.get("hide", False))

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
                    _log(f"空(FPS): 同一条出口重载 {opt['rotations'] + 1} 次都被弹回首页"
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
        except FpsNeedNewIp as exc:
            kind, err = "blocked", str(exc)
            _log(f"{exc} ⇒ 就地换 IP 换不动（隧道复用），整副浏览器重开换新出口")
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
            except FpsNeedNewIp as exc:
                kind, err = "blocked", str(exc)
                _log(f"{exc} ⇒ 整副浏览器重开换新出口，剩 {len(pending)} 人从这个人接着做")
                break
            except FpsBlocked as exc:
                kind, err = "blocked", str(exc)
                _log(f"个人页压不住：{exc}，换新 IP 续做剩余 {len(pending)} 人")
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
            # 同一个人重复采到：只有这次真拿到号码才覆盖，防止无号重扫把号码抹掉。
            # 抓取线程只把人交给专职写线程（实测每人开一次连接抢写锁，最差能拖到 4.3 秒）
            # 入库就用这个剔过空段的同一个列表拼的串：界面「号码N个」必须等于库里的段数
            tps.write_person((address, rec["name"], rec["age"], rec["location"],
                              ",".join(phones), now))
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
        await _close_browser(browser, _log)


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
                        (str(exc), now, address))
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
           "hide": get_setting("fps_hide_window", "0") == "1",
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
        bad0 = tps.WRITE_BAD
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
        # 这一轮采到的人得先真落盘，再往 addresses 写状态/计数：显示要等于到手
        if not tps.flush_writes():
            _log("[!] 写库队列 30 秒没排空，库里可能少于界面数")
        if tps.WRITE_BAD > bad0:
            _log(f"[!] 写库失败{tps.WRITE_BAD - bad0}条：{tps._WRITE_ERR}")

        if kind == "nodata":
            nodata_rounds += 1
            if nodata_rounds < confirm_nodata:
                _log(f"这轮同一条出口重载 {opt['rotations'] + 1} 次都被弹回首页——先不判「没人」，"
                     f"换新浏览器+新出口 IP 再复测（第 {nodata_rounds}/{confirm_nodata} 轮）")
                continue
            why = (f"换浏览器复测 {nodata_rounds} 轮（{nodata_rounds} 个出口 IP，"
                   f"每出口重载 {opt['rotations'] + 1} 次）仍弹回首页")
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
                    (last_error[:300], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), address))
    return False, {"error": last_error, "title": "fastpeoplesearch"}
