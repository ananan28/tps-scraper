# -*- coding: utf-8 -*-
"""交付前最后一次「按屏上说的等于手里拿的」核对（11-18）。

两张交付夹：桌面\抓号机器中心（中心端）、桌面\随时监控版（工人端覆盖包）。
逐文件出 SHA256 + 字节数，并核：
· 两个文件夹里一个数据文件都不许有；
· bat 是 GBK、单 CRLF（历史上 double-CRLF 把 cmd 顶报错）；
· README 里的关键句子真在里面（他照着装，缺一句就是坑）；
· 工人包里的 exe 和构建目录里那个是同一个字节。
"""
import hashlib
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HOME = Path.home()
DIST_EXE = Path(r"C:\Users\AQ28n\AppData\Local\Temp\sourcescan\tps-src\dist\TPS抓取器\TPS抓取器.exe")
CENT = HOME / "Desktop" / "抓号机器中心"
WORK = HOME / "Desktop" / "随时监控版"
n = 0


def check(name, cond, extra=""):
    global n
    assert cond, f"✗ {name} {extra}"
    n += 1
    print(f"  ok   {name}{('｜' + str(extra)[:74]) if extra else ''}", flush=True)


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


for D in (CENT, WORK):
    files = [p for p in sorted(D.rglob("*")) if p.is_file()]
    print(f"\n=== {D.name}｜{len(files)} 个文件｜"
          f"{sum(p.stat().st_size for p in files)/2**20:.1f}MB")
    for p in files:
        print(f"   {sha(p)}  {p.stat().st_size:>12,d}  {p.relative_to(D)}")
    bad = [str(p.relative_to(D)) for p in files
           if p.suffix.lower() in (".db", ".sqlite", ".csv", ".json") or ".db-" in p.name.lower()]
    check(f"{D.name}：一个数据/配置文件都没有", not bad, bad)

cfiles = {p.name: p for p in CENT.rglob("*") if p.is_file()}
check("中心夹里有 hub.exe / 启动中心.bat / README.txt",
      {"hub.exe", "启动中心.bat", "README.txt"} <= set(cfiles), sorted(cfiles)[:6])
bat = (CENT / "启动中心.bat").read_bytes()
check("中心 bat：GBK + 单 CRLF（没有 double-CRLF）",
      b"\r\r\n" not in bat and bat.count(b"\r\n") == bat.count(b"\n") and bat.decode("gbk").startswith("@echo off"),
      bat.decode("gbk", "replace").replace("\r\n", " | "))
rd = (CENT / "README.txt").read_bytes().decode("gbk")
for key in ("专用网络", "允许访问", "只监控", "连到中心", "把号码也交上去", "允许中心控制这台",
            "不要", "公网", "hub_data.db"):
    check(f"中心 README 写了「{key}」", key in rd)
check("中心 README 说的是默认只监控", "默认不勾" in rd or "默认全是关的" in rd)

wexe = WORK / "TPS抓取器.exe"
check("工人包里的 exe 和刚冒烟过的那个是同一份（SHA256 对平）",
      hashlib.sha256(wexe.read_bytes()).hexdigest() == hashlib.sha256(DIST_EXE.read_bytes()).hexdigest(),
      f"{wexe.stat().st_size:,} 字节")
wb = (WORK / "覆盖升级_双击我.bat").read_bytes()
check("工人 bat：GBK + 单 CRLF", b"\r\r\n" not in wb and wb.count(b"\r\n") == wb.count(b"\n"))
wt = wb.decode("gbk")
for key in ("只换 TPS抓取器.exe", "正在运行，文件被占用", "tps_data.db", "大小对不上"):
    check(f"工人 bat 有那道闸/那句：「{key}」", key in wt)
wr = (WORK / "README_先看.txt").read_bytes().decode("gbk")
for key in ("连到中心", "把号码也交上去", "允许中心控制这台", "测试一下", "专用网络",
            "默认收起", "一个号码都不外传", "8760", "公网"):
    check(f"工人 README 写了「{key}」", key in wr)
check("工人 README：只勾第一个就够监控（把他那句话写进文档）",
      "只想" in wr and "第一个" in wr)
meas = sorted(p.name for p in WORK.glob("实测_*.txt"))
check("实测记录 6 份都跟着走", len(meas) == 6, meas)
tot = 0
for p in WORK.glob("实测_*.txt"):
    t = p.read_text(encoding="utf-8-sig")
    check(f"{p.name} 结论是「全过」", "结论行：✓" in t and "判定：全过" in t,
          [l for l in t.splitlines() if l.startswith("结论行")][:1])
    tot += t.count("  ok   ")
print(f"\n实测记录里一共 {tot} 条 ok")
print(f"\n✓ 交付核对全过（{n} 项）")
