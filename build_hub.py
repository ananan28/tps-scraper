# -*- coding: utf-8 -*-
"""打包中心端：hub_server.py 只有标准库，不用捆浏览器，打出来很小。

交付口径（11-18 定）：桌面顶层一个中文文件夹「抓号机器中心」，里面就 exe + 启动 bat
+ README，一个数据文件都不带（hub_data.db 是第一次双击时才在那台上生成的）。
bat 一律 GBK 编码、单 CRLF（历史上 double-CRLF 把 cmd 顶得报错）。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIST = BASE / "dist" / "hub"                  # PyInstaller 的产出（exe 叫 hub.exe）
DESKTOP = Path.home() / "Desktop" / "抓号机器中心"   # 交付文件夹：桌面顶层、中文名

README = """抓号机器中心（一台常开的电脑当中心）
================================================

这是「随时监控那几台抓号电脑」的中心端。哪台常开就装在哪台，双击即可。
工人（抓号的那几台）主动往外连它，所以：不用公网 IP、不用配域名、不用动路由器。

一、怎么用（三步）
------------------
1) 双击「启动中心.bat」→ 黑窗口会打出一行地址和一个令牌，长这样：
     别的电脑在程序「中心/远程」里填：  http://192.168.11.5:8760
     令牌（两栏要一样）：               xxxxxxxx
   这个窗口别关（关了机器们就没中心了）。
   ★ 第一次启动 Windows 会弹防火墙问"允许访问"：勾上「专用网络」再点「允许访问」。
     点了"取消"这台自己开面板没问题，但那几台抓号电脑就连不上来了
     （报错是"中心没连上"）。补救：控制面板 → Windows Defender 防火墙 →
     允许应用通过防火墙 → 找到 hub → 勾「专用」。

2) 每台抓号电脑：打开程序 →「中心/远程（随时监控这台电脑）」那一区（默认收起，点开）
   → 中心地址、令牌照抄上面那两行 → 只勾第一个「连到中心」→ 点「测试一下」。
   屏上说「中心连上了」就成了。另外两个勾先别打：
     · 把号码也交上去 —— 勾了才把号码传到中心（默认不传，只报"几个人几个号"这些数字）
     · 允许中心控制这台 —— 勾了才听网页上的开始/暂停按钮（默认不听）

3) 在这台中心电脑上开浏览器：http://127.0.0.1:8760/  → 填同一个令牌。
   手机上连同一个 WiFi/内网也能开，地址换成上面那行 http://192.168.x.x:8760/。

二、面板上能看到什么（这就是"随时监控"）
----------------------------------------
每台一张卡片：在不在跑、几个人/几个号、待跑多少条、失败多少、真在跑的车道数、
出口都在哪些国家（是从它屏上那行「[W2] 出口 …｜美国 …」直接读出来的，不额外发请求）、
还剩多少内存、多久没心跳（超过 95 秒标"掉线"）。
下面一条日志流：那几台电脑上屏的日志原样流过来（带机器名和时间）。
再下面一栏事件：谁掉线、谁回来了、派了多少条、哪条指令发下去两分钟没回执。

三、要控制才做的事（默认全是关的）
----------------------------------
· 面板右上角有个「允许远程控制和分配任务」勾，默认不勾 —— 这时按钮根本不出现，
  指令接口直接拒收。勾上才会有开始/暂停/恢复/停止/改并发/导出/重试/分配任务那些按钮。
· 就算中心这边勾了，那台电脑自己也得勾「允许中心控制这台」才会真动手。两头都开才算数，
  少一边它就只回一句"这台没开控制"，一个按钮都不碰。
· 「清除已导出」「删已完成」这两个会删东西的，网页上要连点两次确认才下发，
  而且报文里必须带 force，那台程序才真删。
· 「把号码也交上去」同样是那台机器自己勾了才交；中心这边按号码去重，
  跨机器同一个号只算一次净新增。

四、注意
--------
· 只在内网用。不要把 8760 端口往公网开放（等于把几百万条号码挂到全网扫描里）。
· 数据都在同文件夹的 hub_data.db（第一次运行才生成）。这个压缩包里不带任何数据。
· 端口要改：命令行 hub.exe --port 9000；令牌要固定：hub.exe --token 你想要的串。
· 换电脑：把整个文件夹拷过去，双击即可；老库 hub_data.db 一起拷就接着算。
· 出问题先跑 hub.exe --selftest，同文件夹会出 hub_selftest.txt（不碰真库）。
"""


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main():
    print("[1/4] PyInstaller 打 hub.exe（纯标准库，快）...", flush=True)
    if DIST.exists():
        shutil.rmtree(DIST)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
                    "--name", "hub",
                    "--distpath", str(BASE / "dist"),
                    "--workpath", str(BASE / "build"),
                    "--specpath", str(BASE),
                    str(BASE / "hub_server.py")], check=True, cwd=str(BASE))
    assert (DIST / "hub.exe").exists(), f"没打出 exe：{DIST}"

    print("[2/4] 生成启动器 + README...", flush=True)
    (DIST / "启动中心.bat").write_bytes(
        b'@echo off\r\ncd /d "%~dp0"\r\nhub.exe\r\npause\r\n')
    (DIST / "README.txt").write_bytes(README.encode("gbk", "replace"))
    for p in DIST.glob("*.db*"):
        p.unlink()                      # 交付包里一个数据文件都不许有

    print("[3/4] exe 自检（--selftest，临时库、临时端口，不碰真库）...", flush=True)
    r = subprocess.run([str(DIST / "hub.exe"), "--selftest"], capture_output=True, timeout=180)
    txt = (DIST / "hub_selftest.txt").read_text(encoding="utf-8") if (DIST / "hub_selftest.txt").exists() else ""
    print("  退出码", r.returncode, "|", txt.replace("\n", " / "), flush=True)
    # 失败就整个中止：桌面那份旧交付原样留着，不会被一个没验过的包盖掉
    assert r.returncode == 0 and "SELFTEST_OK" in txt, "exe 自检没过，别铺交付（原因见上面那行）"

    print("[4/4] 铺到桌面顶层（覆盖旧的同名文件夹）...", flush=True)
    if DESKTOP.exists():
        shutil.rmtree(DESKTOP)
    shutil.copytree(DIST, DESKTOP)
    bad = []
    for p in sorted(DIST.rglob("*")):
        if not p.is_file():
            continue
        q = DESKTOP / p.relative_to(DIST)
        if not q.exists() or sha(q) != sha(p) or q.stat().st_size != p.stat().st_size:
            bad.append(str(p.name))
    assert not bad, f"桌面那份对不上：{bad}"
    print(f"\n交付文件夹：{DESKTOP}（逐文件 SHA256 已和 dist 对平，共 "
          f"{sum(1 for p in DESKTOP.rglob('*') if p.is_file())} 个文件）")
    for p in sorted(DESKTOP.rglob("*")):
        if p.is_file():
            print(f"  {sha(p)}  {p.relative_to(DESKTOP)}")
    left = [str(p) for p in DESKTOP.rglob("*.db*")]
    print("数据文件残留：", left or "无（干净）")


if __name__ == "__main__":
    main()
