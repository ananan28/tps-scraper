# -*- coding: utf-8 -*-
"""11-18 晚：他 8 张截图 → 「确认页/白页/出口死口」三档判定的离线断言。
每档必须 assert，不许只 print；_fetch 三条路径用假 tab 验真会抛、真会等多久。
"""
import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import engine_fps as fps

bad = 0


def check(name, cond, detail=""):
    global bad
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('｜' + str(detail)[:120]) if detail else ''}")
    if not cond:
        bad += 1


# ---- 他截图的三种「Are you human?」姿态（正文三句签名至少中一句） ----
HC_STUCK = ("<html><title>Free People Search | FastPeopleSearch.com</title><body>"
            "Are you human?<p>We've noticed some strange activity from your ip "
            "address and would like to confirm that you are not a bot. Please fill "
            "out the re-captcha and then hit submit.</p>"
            "<div>Verifying... Stuck? Troubleshoot CLOUDFLARE</div></body></html>")
HC_CHECKBOX = HC_STUCK.replace("Verifying... Stuck? Troubleshoot",
                               "Verify you are human CLOUDFLARE")
HC_FAILED = HC_STUCK.replace("Verifying... Stuck? Troubleshoot",
                             "Verification failed Troubleshoot CLOUDFLARE")
# ---- 图4 拉黑页（这版本来就认得，确认搬完没退化） ----
BLOCKED = ("<html><title>Attention Required! | Cloudflare</title><body>"
           "Sorry, you have been blocked. You are unable to access "
           "fastpeoplesearch.com</body></html>")
# ---- 真挑战页：带 _cf_chl_opt，绝不能被新档吞掉 ----
CHALLENGE = ("<html><title>Just a moment...</title><body>"
             "<script>window._cf_chl_opt={'cType':'managed'}</script></body></html>")
# ---- 半截挑战页：没标题但带 _cf_chl_opt ----
HALF_CHALLENGE = "<html><body><script>var _cf_chl_uk='x';</script></body></html>"
# ---- 真到位但 0 住户的地址页（11-17 他截图 14312 Hiseville Ct） ----
EMPTY_PAGE = ('<html><title>120 Clark Ln | FastPeopleSearch.com</title><body>'
              '<div id="residents"></div><div>Property Details</div></body></html>')
CARD_PAGE = ('<html><title>100 Main St | FastPeopleSearch.com</title><body>'
             + '<div class="card" id="card_1">John Smith 55 (214) 555-0100</div>' * 5
             + '<div>People Living at 100 Main St</div></body></html>')
NETERR_PAGE = "<html><body>ERR_CONNECTION_CLOSED</body></html>"


class FakeTab:
    def __init__(self, payload):
        self._p = payload

    async def get(self, url):
        return None

    async def get_content(self):
        return self._p


class FakeRelay:
    bytes_transferred = 40960

    def set_session(self, sid):
        pass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


print("== 一、page_state 分档 ==")
for name, html, want in [
    ("图1 Verifying 卡死", HC_STUCK, "humancheck"),
    ("图3 复选框", HC_CHECKBOX, "humancheck"),
    ("图6 Verification failed", HC_FAILED, "humancheck"),
    ("图4 拉黑页", BLOCKED, "hardblock"),
    ("真挑战带 _cf_chl_opt", CHALLENGE, "challenge"),
    ("半截挑战无标题", HALF_CHALLENGE, "challenge"),
    ("图2/5 全白页", "", "blank"),
    ("空壳页", "<html></html>", "blank"),
    ("0 住户真地址页", EMPTY_PAGE, "ready"),
    ("正常卡片页", CARD_PAGE, "ready"),
]:
    check(f"{name} → {want}", fps.page_state(html) == want)

print("== 二、白页不吞验证页：有验证标记的短页必须还是 challenge ==")
check("短页含 _cf_chl_opt 不判白",
      fps.page_state("<script>_cf_chl_opt</script>") == "challenge")

print("== 三、_fetch 真会抛/真会返回 ==")


async def fetch_raises(payload, why_part):
    t0 = time.time()
    try:
        await fps._fetch(FakeTab(payload), FakeRelay(), "u", "listing",
                         lambda s: None, 30, 0, "s")
    except fps.FpsNeedNewIp as exc:
        return why_part in str(exc), round(time.time() - t0, 1)
    return False, round(time.time() - t0, 1)


ok, secs = run(fetch_raises(HC_STUCK, "reCAPTCHA 确认页"))
check(f"确认页立刻抛 FpsNeedNewIp（{secs}s，不白等 200s）", ok and secs < 5)
ok, secs = run(fetch_raises(NETERR_PAGE, "出口断连"))
check(f"出口死口立刻抛（{secs}s，不重载硬扛）", ok and secs < 5)

# 白页：把 120s 预算临时改成 2s 验「等到点就抛」，跑完改回
fps.BLANK_WAIT_S = 2
ok, secs = run(fetch_raises("", "白页"))
check(f"白页等到点抛（{secs}s，改过预算 2s）", ok and 2 <= secs < 8)
fps.BLANK_WAIT_S = 120


async def fetch_returns(payload):
    return await fps._fetch(FakeTab(payload), FakeRelay(), "u", "listing",
                            lambda s: None, 30, 0, "s")


got = run(fetch_returns(CARD_PAGE))
check("正常页原样返回不折腾", got == CARD_PAGE)

print("== 四、确认页短文案也要中（防只匹配长句） ==")
check("光标题 are you human 也算", "humancheck" ==
      fps.page_state("<html><title>x</title>are you human</html>") or
      fps.page_state("<html><title>x</title>ARE YOU HUMAN?</html>") == "humancheck")

print("== 五、11-19：验证页等不动就换出口，不再同一条出口重载三次 ==")

BOUNCED = ('<html><title>Free People Search | FastPeopleSearch.com</title><body>'
           '<div>Find People, Phone Numbers, Address History</div></body></html>')


class CountTab(FakeTab):
    def __init__(self, payload):
        super().__init__(payload)
        self.gets = 0

    async def get(self, url):
        self.gets += 1
        return None


async def fetch_probe(payload, timeout_s, rotations, hb):
    """跑一次 _fetch，回报（抛的异常名/返回值, 秒, 加载次数, 日志行）"""
    tab = CountTab(payload)
    lines = []
    old_hb = fps.WAIT_HEARTBEAT_S
    fps.WAIT_HEARTBEAT_S = hb
    t0 = time.time()
    try:
        got = await fps._fetch(tab, FakeRelay(), "u", "listing", lines.append,
                               timeout_s, rotations, "s")
        out = ("return:" + ("空" if got == "" else "有内容"), round(time.time() - t0, 1))
    except Exception as exc:
        out = (type(exc).__name__, round(time.time() - t0, 1))
    finally:
        fps.WAIT_HEARTBEAT_S = old_hb
    return out[0], out[1], tab.gets, lines


name, secs, gets, lines = run(fetch_probe(CHALLENGE, 4, 2, 2))
check(f"挑战页等满就抛 FpsNeedNewIp（实测 {name} {secs}s）", name == "FpsNeedNewIp")
check(f"只加载 1 次，不再拿同一条出口重载 3 次（实测 gets={gets}）", gets == 1)
check(f"一条页的最坏占用从 4×3+重载=约 18s 降到 {secs}s（<8s 才算真不重载）", secs < 8)
check("等待期有心跳行（屏上能看出这条车道还活着）",
      any("还在等这一页" in l for l in lines), "｜".join(lines)[:120])

name, secs, gets, lines = run(fetch_probe(BOUNCED, 4, 2, 2))
check(f"被弹回首页仍走满 {2 + 1} 次重载才判没人（判无人的证据链没被我这刀砍坏）",
      name == "return:空" and gets == 3, f"{name} gets={gets}")

name, secs, gets, _ = run(fetch_probe(CARD_PAGE, 4, 2, 2))
check("正常页一次加载就返回，新判定没给它多加等待",
      name == "return:有内容" and gets == 1, f"{name} gets={gets}")

print(f"\n合计不过 {bad} 项")
sys.exit(1 if bad else 0)
