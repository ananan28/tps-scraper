# -*- coding: utf-8 -*-
"""铺「随时监控版」工人端覆盖包到桌面顶层（11-18）。

口径（历史上被说过的那些，一次满足）：
· 只换 exe —— 本地新模块（hub_client/egress_ip）打进 exe 内部的 PYZ，_internal 不用动；
  所以包里就 exe + bat + README + 实测 txt，一个数据文件都不带（tps_data.db / *.db 一律不许出现）；
· bat 用 GBK、单 CRLF（double-CRLF 以前把 cmd 顶报错）；
· 逐文件 SHA256 + 字节数和 dist 对平才叫交付；
· 打包前必须先跑过 dist 内冒烟（smoke_worker_exe_1118.py），没冒烟过不许铺。
"""
import hashlib
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
DIST = BASE / "dist" / "TPS抓取器"
EXE = DIST / "TPS抓取器.exe"
OUT = Path.home() / "Desktop" / "随时监控版"
MEAS = BASE / "实测输出"          # run_checks_1118.py 落的那几份 txt

BAT = r"""@echo off
setlocal
rem ============================================================
rem  随时监控版 覆盖升级 —— 只换 TPS抓取器.exe 这一个文件
rem  你的 tps_data.db（地址队列+已采号码）、logs、_internal、浏览器全都不动
rem  这一版多了：界面下方「中心/远程（随时监控这台电脑）」一区（默认收起、默认全关）
rem  用法：先关掉正在跑的程序窗口，再双击本 bat；目标路径直接回车=包里同级那个
rem ============================================================
set PKG=%~dp0
set /p DST=目标文件夹路径（直接回车=本包同级的 新年龄修复版_两核心覆盖包_常用号版）: 
if not defined DST set DST=%PKG%..\新年龄修复版_两核心覆盖包_常用号版
if not exist "%DST%" (
  echo [!] 目标文件夹不存在：%DST%
  echo     把本包和程序文件夹放同级，或者把路径填全。
  pause & exit /b 1
)
if not exist "%DST%\TPS抓取器.exe" (
  echo [!] 这个文件夹里没有 TPS抓取器.exe，不是覆盖目标，已停手（你的文件一个没动）。
  echo     要看清楚你日常用的是哪个文件夹：里面得有 TPS抓取器.exe 和 tps_data.db。
  pause & exit /b 1
)
powershell -NoProfile -Command "$p=@(Get-Process -Name 'TPS抓取器' -ErrorAction SilentlyContinue); if($p.Count -eq 0){exit 0}; $t='%DST%\TPS抓取器.exe'; if(@($p | Where-Object { -not $_.Path -or $_.Path -ieq $t }).Count -gt 0){exit 3}; '  提示：只看到别处在跑，不影响本目标：' + (($p.Path | Select-Object -Unique) -join ' ; ')"
if errorlevel 3 (
  echo [!] 目标里的 TPS抓取器.exe 正在运行，文件被占用。
  echo     先关掉那个程序窗口（点停止或退出），再双击本 bat。
  pause & exit /b 1
)
for %%F in ("%PKG%TPS抓取器.exe") do set SRCZ=%%~zF
copy /y "%PKG%TPS抓取器.exe" "%DST%\TPS抓取器.exe" >nul
if errorlevel 1 (
  echo [!] 拷贝失败（多半是程序还在跑或路径没权限），已停手，原来的 exe 还在。
  pause & exit /b 1
)
for %%F in ("%DST%\TPS抓取器.exe") do set DSTZ=%%~zF
if not "%SRCZ%"=="%DSTZ%" (
  echo [!] exe 大小对不上（包里 %SRCZ% / 目标 %DSTZ%），覆盖没成功，关掉程序再来一遍。
  pause & exit /b 1
)
echo   校验：目标 exe %DSTZ% 字节，与包里一致
echo.
echo [完成] 只换了程序，tps_data.db 和 logs 一个字没动。
echo        打开程序后看界面下方「中心/远程（随时监控这台电脑）」，做法见 README_先看.txt。
pause
"""

README = r"""抓号程序「随时监控版」—— 更新 + 连中心（每台抓号电脑做一遍）
================================================================

这一版多了什么
--------------
· 界面下方多一区「中心/远程（随时监控这台电脑）」，默认收起、里面三个勾默认全不勾。
· 只勾第一个「连到中心」：这台电脑的进度（几个人/几个号/待跑多少/几条车道/还剩多少内存）、
  屏上日志（含每条车道出口 IP 在哪个国家）就一行行报到中心那台电脑。
  中心开了网页就能看到，手机连同一个网络也能看。
· 不勾就一个包都不发；程序照跑，抓号一点不受影响。中心那台没开机也没关系，
  日志在这台攒着（有上限，不会吃内存），中心一回来自己补上，顺序不乱、不翻倍。
· 另外两个勾默认关着，不动它们就：一个号码都不外传、网页上的按钮对这台也不起作用。
  「我只是想随时监控」= 只勾第一个，就够了。

一、更新程序（先关掉正在跑的那个窗口）
--------------------------------------
1) 双击「覆盖升级_双击我.bat」→ 目标路径直接回车（或填你日常双击的那个程序文件夹）。
   只换 TPS抓取器.exe 一个文件，tps_data.db、logs、_internal、浏览器全都不动。
2) 屏上出「[完成] 只换了程序」才算成。要是拦你：
   · 「正在运行，文件被占用」= 那个程序窗口还开着，关掉再双击一遍；
   · 「没有 TPS抓取器.exe」= 路径填错了，换成你日常用的那个文件夹。

二、连到中心（每台抓号电脑都做一遍）
------------------------------------
1) 中心那台电脑：双击「启动中心.bat」（在「抓号机器中心」文件夹里），黑窗口打两行 ——
     别的电脑填：http://192.168.x.x:8760
     令牌：xxxxxx
   第一次启动弹防火墙问话，勾「专用网络」再点「允许访问」，不然别的机器连不上。
2) 这台抓号电脑：打开程序 → 点开「中心/远程」→ 中心地址、令牌照抄那两行 →
   只勾「连到中心」→ 点「测试一下」。屏上说「通了｜这台叫 XXX」就成了。
   这三格会自动记住，重启程序还在，不用再点保存。
3) 回到中心那台开浏览器：http://127.0.0.1:8760/ ，填同一个令牌 → 每台一张卡片 + 日志流。

三、要控制才做的事（默认全是关的，只想监控就永远不用碰）
--------------------------------------------------------
· 面板右上角「允许远程控制和分配任务」默认不勾：按钮根本不出现，指令接口直接拒收。
· 就算中心勾了，这台自己还得勾「允许中心控制这台」才会真动手 —— 两头都开才算数，
  少一边它就只回一句"这台没开控制"。
· 「把号码也交上去」同理：这台勾了才交，中心按号码去重，跨机器同一个号只算一次净新增。
· 「清除已导出」「删已完成」这两个会删东西的：网页上要连点两次确认，
  而且下发的报文里必须带 force，程序才真删。
· 只在内网用。不要把 8760 端口往公网开放。

四、这批数字是怎么来的
----------------------
同目录 实测_*.txt 是打包前真跑出来的：中心端离线自检 46 项、工人端 67 项、
界面↔中心 40 项、中心 EXE 冒烟 18 项、exe 内容核对 30 项、抓取速度配对 A/B。
"""


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main():
    assert EXE.exists(), f"没有 {EXE}，先跑 build.py"
    # 构建目录里跑过冒烟会留下 tps_data.db —— 那不要紧（本包只拷 exe 一个文件），
    # 但要打出来让人看见；真正的硬闸在下面：交付文件夹 OUT 里不许有任何数据文件。
    seen = [str(p.relative_to(DIST)) for p in DIST.rglob("*")
            if p.is_file() and (p.suffix.lower() in (".db", ".sqlite") or ".db-" in p.name.lower())]
    print(f"  记：构建目录里有 {len(seen)} 个运行时库（{seen}），都不进包")

    print(f"[1/3] 组包 → {OUT}", flush=True)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copy2(EXE, OUT / EXE.name)
    (OUT / "覆盖升级_双击我.bat").write_bytes(BAT.replace("\n", "\r\n").encode("gbk", "replace"))
    (OUT / "README_先看.txt").write_bytes(README.replace("\n", "\r\n").encode("gbk", "replace"))
    n_meas = 0
    if MEAS.exists():
        for p in sorted(MEAS.glob("实测_*.txt")):
            shutil.copy2(p, OUT / p.name)
            n_meas += 1
    assert n_meas >= 4, f"实测 txt 只有 {n_meas} 份，先跑 run_checks_1118.py 再来铺包"

    print("[2/3] 逐文件 SHA256 对平（对不上就不算交付）...", flush=True)
    for p in sorted(OUT.iterdir(), key=lambda x: x.name):
        if p.name == EXE.name:
            q = EXE
        elif p.name == "覆盖升级_双击我.bat":
            assert b"\r\r\n" not in p.read_bytes(), "bat 里出现 double-CRLF"
            print(f"  {sha(p)}  {p.name}（bat：GBK + 单 CRLF 已核）")
            continue
        elif p.name == "README_先看.txt":
            print(f"  {sha(p)}  {p.name}")
            continue
        else:
            q = MEAS / p.name
        assert sha(p) == sha(q) and p.stat().st_size == q.stat().st_size, f"{p.name} 和源对不上"
        print(f"  {sha(p)}  {p.name}")

    print("[3/3] 包里不许有数据文件...", flush=True)
    left = [str(p.relative_to(OUT)) for p in OUT.rglob("*")
            if p.is_file() and (p.suffix.lower() in (".db", ".sqlite") or ".db-" in p.name.lower())]
    assert not left, f"包里混进数据文件：{left}"
    tot = sum(p.stat().st_size for p in OUT.iterdir() if p.is_file())
    print(f"\n交付文件夹：{OUT}｜{len([p for p in OUT.iterdir() if p.is_file()])} 个文件，"
          f"共 {tot/2**20:.1f}MB（exe {EXE.stat().st_size/2**20:.2f}MB + 说明 + 实测）")
    for p in sorted(OUT.iterdir(), key=lambda x: x.name):
        print("  ", p.name)


if __name__ == "__main__":
    main()
