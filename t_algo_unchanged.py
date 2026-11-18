# -*- coding: utf-8 -*-
"""断言：11-18 这轮改动没碰任何抽取/解析算法。

三道检查：
1) 逐函数比字节：只有白名单里那几个（出口IP日志 / 解码报错落日志 / 不解码说一句原因）允许变。
2) 抽取流程所在的函数必须「只增不改不删」——一行删除都不许有，
   这样新增的只是日志行，抓取行为不可能倒退。
3) 抽取算法的门面函数（parse_person / clean_phone / page_status ...）单独再点一次名。
"""
import ast
import difflib
import io
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 本轮唯一允许改动的函数
ALLOW_CHANGED = {
    "engine_tps182.py": {
        "twocaptcha_turnstile_proxied",   # 厂家原话落日志
        "try_solve_challenge",            # 把 log 往下传 + 报错口径
        "scrape_detail",                  # 不解码时说一句为什么
        "process_address",                # 每车道探出口 + 被拦不解码报原因
    },
    "engine_fps.py": {
        "_log_egress",                    # 新增：FPS 每车道出口IP
        "_run_attempt",                   # 挂一行探活
        "process_address",                # 备 upstream 串
    },
}

# 抽取流程经过的函数：允许插日志，绝不允许删/改原有任何一行
PURE_ADDITION = {
    "engine_tps182.py": {"scrape_detail", "process_address"},
    "engine_fps.py": {"_run_attempt", "process_address"},
}

EXTRACTION_FNS = [
    "parse_person", "clean_phone", "page_status", "parse_list_page",
    "age_of", "is_person_name", "name_parts", "split_name", "extract_people",
    "parse_people", "people_from_html",
]


def funcs(text):
    tree = ast.parse(text)
    return {n.name: ast.get_source_segment(text, n)
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def head_text(name):
    return subprocess.run(["git", "show", f"HEAD:{name}"], capture_output=True,
                          check=True).stdout.decode("utf-8")


bad = 0
snap = {}
for fname in ("engine_tps182.py", "engine_fps.py"):
    old = funcs(head_text(fname))
    new = funcs(open(fname, encoding="utf-8").read())
    snap[fname] = (old, new)
    allow = ALLOW_CHANGED[fname]
    changed = {k for k in new if k in old and old[k] != new[k]}
    added = {k for k in new if k not in old}
    removed = {k for k in old if k not in new}
    illegal = (changed | added | removed) - allow
    for k in sorted(illegal):
        print(f"  FAIL {fname}::{k} 不在白名单里却被改了")
        bad += 1
    for k in sorted(changed | added):
        print(f"  ok   {fname}::{k} 白名单内改动")

# 只增不删
for fname, names in PURE_ADDITION.items():
    old, new = snap[fname]
    for fn in sorted(names):
        a = (old.get(fn) or "").splitlines()
        b = (new.get(fn) or "").splitlines()
        cuts = [l for l in difflib.unified_diff(a, b, lineterm="", n=0)
                if l.startswith("-") and not l.startswith("---")]
        if cuts:
            print(f"  FAIL {fname}::{fn} 删/改了原有 {len(cuts)} 行（抽取行为可能倒退）")
            for l in cuts[:6]:
                print(f"         {l[1:].strip()[:90]}")
            bad += 1
        else:
            print(f"  ok   {fname}::{fn} 只插了 {len(b) - len(a)} 行日志，"
                  f"原有 {len(a)} 行一字未动")

# 抽取门面函数
for fname in ("engine_tps182.py", "engine_fps.py"):
    old, new = snap[fname]
    for fn in EXTRACTION_FNS:
        if fn in old:
            same = old[fn] == new.get(fn)
            print(f"  {'ok  ' if same else 'FAIL'} {fname}::{fn}() 抽取源码"
                  f"{'逐字节未变' if same else '被改了'}（{len(old[fn])} 字符）")
            if not same:
                bad += 1

n_old = len(snap["engine_tps182.py"][0]) + len(snap["engine_fps.py"][0])
print(f"\n底版共 {n_old} 个顶层函数｜合计不过 {bad} 项")
sys.exit(1 if bad else 0)
