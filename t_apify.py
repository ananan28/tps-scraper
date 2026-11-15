import urllib.request, urllib.parse, json, sys, re

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def find_handle(kw, want, n=14):
    d = get(f"https://api.apify.com/v2/store?search={urllib.parse.quote(kw)}&limit={n}&sort=relevance")["data"]
    out = []
    for it in d["items"]:
        title = it.get("title", "")
        for w in want:
            if w.lower() in title.lower():
                out.append((title, it.get("username"), it.get("name"), it.get("stats", {}).get("totalUsers")))
                break
    return out


cands = []
for kw, want in [("apollo", ["1/1k", "$0.8", "1.5", "NO COOKIES", "Email & Phone"]),
                 ("lusha alternative", ["Mobile", "Phone", "Waterfall"]),
                 ("sales navigator", ["NO COOKIE", "By Filters", "with Emails"])]:
    cands += find_handle(kw, want)

seen = set()
for title, user, name, users in cands:
    if not user or not name or name in seen:
        continue
    seen.add(name)
    try:
        a = get(f"https://api.apify.com/v2/acts/{urllib.parse.quote(user)}~{urllib.parse.quote(name)}")
    except Exception as e:
        print(f"[{title[:44]}] 详情失败 {getattr(e,'code','')}{type(e).__name__}")
        continue
    p = a.get("currentPricingInfo") or {}
    price = p.get("pricingModel")
    if price == "PAY_PER_VIEW":
        txt = json.dumps(p.get("pvpPricing", {}))
        nums = sorted({float(x) for x in re.findall(r'"priceUsd":\s*([\d.]+)', txt)})[:3]
        price = "PVP " + "/".join(f"${n}" for n in nums)
    elif price == "PRICE_PER_DATASET_ITEM":
        price = f"${p.get('pricePerDatasetItemUsd')}/条"
    sch = json.dumps(a.get("inputSchema") or {})
    rm = (a.get("readme") or "")[:20000]
    phone = ("phone" in sch.lower()) or bool(re.search(r"phone|mobile|dial", rm, re.I))
    needs = []
    for k in ("apiKey", "token", "cookie", "session", "login"):
        if re.search(k, sch, re.I):
            needs.append(k)
    maxrec = re.findall(r'"maxItemsPerRun[^"]*":\s*(\d+)', sch)
    print(f"- {a.get('title','')[:62]}")
    print(f"    用户 {users}  价 {price}  输出含电话={phone}  要凭据={needs or '不需要'}  单次上限={maxrec[:1]}")
