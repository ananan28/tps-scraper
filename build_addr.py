# -*- coding: utf-8 -*-
"""打包两个地址抓取器 EXE（普通房屋 / 高净值房屋）：onedir + --windowed + 自带 Chromium。

--windowed 是为了双击只出界面、不出现那个黑控制台；带参数跑命令行时
pw_engine.ensure_console() 会现场开一个控制台，输出照样看得见。
浏览器两条路：目标机器装了 Chrome/Edge 就用它（指纹更像真人），
没装就用打包进去的 browser\\chrome-win64，所以一台没装过浏览器的电脑也能跑。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import bundle_browser

BASE = Path(__file__).resolve().parent

JOBS = [
    ("普通房屋地址抓取器", "普通地址抓取.py", True, 400000, 1200000),
    ("高净值房屋地址抓取器", "高净值地址抓取.py", False, 1200000, None),
]

README = """{name}
{desc}

一、双击「{exe}.exe」或「启动{name}.bat」就出界面，三个页签：

  ① 抓取
      上面是 50 州 + DC 的勾选框（有「全选 / 清空 / 德州 / 东岸 / 西岸 / 中部」快捷按钮，
      一个都不勾 = 全美）。中间填估价下限、上限、并发标签页数、本轮 ZIP 数，
      「随机抽」用来试产量，「重跑已跑过的」用来补新挂牌。按「开始抓取」。
      下面实时滚动每个 ZIP 翻了几页、拿到多少条、被挡几次。
      中途想停：按「停止」，当前 ZIP 落库就停，进度已经写进 pool_state.db。
      收尾太慢（浏览器卡在页面里）就再按一次那个按钮，文字是「正在收尾…再点一下=不等了」，
      按下去「开始抓取」立刻能点，不用等它。
      跑过的 ZIP 永久跳过，不会重复跑（要补新挂牌才勾「重跑已跑过的」）；
      地址按规范化去重，同一栋房的 Street/St、Ave/Avenue 写法只算一条。

  ①' 网络出口（那个框留空就是走这台机器自己的网络，不花钱）
      要挂代理就一行一条粘进去，几种写法可以混着填：
        IP:端口:账号:密码
        http://账号:密码@IP:端口
        IP:端口            （免认证，靠 IP 白名单的那种）
      填了密码栏才需要单独填「密码」那一格。被挡（403/429/会话掉了）就自动换下一条出口，
      只有一条也会重新握手拿新 IP。代理栏会被记住，下次打开还在，不用重填。
      默认留空 = 本机直连。

  ② 库存 / 导出
      库存总数、未导出、已导出、估价区间，以及导出过的批次表。
      导出：填条数、选顺序（随机 / 价格贵的先给）、写批次备注，按「导出」。
      导出用的州就是①页勾的那些；想导全美的就把①页州清空再导。
      选中批次表里一行，可以「放回池子」（撤销导出标记，能再次导出）
      或「删除这批地址」（彻底删）。也可以「删除所有已导出」。

  ③ 失败台账
      三类分开列，都能一键只重跑那一类：
        报错/被挡   http403、429、CDP 断连这类传输问题
        没翻到底    PropWire 报的底数比我们真拿到的条数多（一般是 --maxpage 到顶）
        查到 0 条    该 ZIP 在 PropWire 里就没房子——多数是真的小 ZIP（乡村/只放信箱），
                    重跑也榨不出东西，列在这里是让你知道为什么数量对不上

二、命令行用法（不想开界面时用，输出会另开一个黑框）
    {exe}.exe --states TX --min {dmin}{maxline} --workers 4
    {exe}.exe --random --limit 40 --workers 4      先抽 40 个 ZIP 试产量
    {exe}.exe --stat                               看库存
    {exe}.exe --export 10000 --states TX --name 3号机
    {exe}.exe --list-batches / --recall-batch 3 / --delete-batch 3 --yes
    {exe}.exe --delete-exported --yes              删掉所有已导出的地址
    {exe}.exe --list-failed                        看失败的 ZIP
    {exe}.exe --retry-failed --kind err --yes      只重跑被挡的那些

三、抓出来的东西在哪
    pool_state.db            库存 + 断点进度 + 失败原因 + 批次标记，别手改
    us_zip_codes.csv         全美 40,977 个 ZIP 目录，抓取就按它逐个调度
    导出\\                    导出的 TXT，一行一个「街道, 城市, 州 ZIP」
                             TPS 抓取器「从文件导入」直接吃这个
    运行日志_日期.txt         每个 ZIP 的明细，出问题先看这个

四、它是怎么取数的
    数据源 PropWire。前面是 DataDome 反爬，本机 curl 带全套 cookie 也会被打回 403
    并甩验证码，只有真浏览器里的同源请求能过，所以一定要开浏览器窗口，别关。
    默认走本机网络：不用代理、不用 Apify、不用流量（实测每个 ZIP 约 10KB）。
    要挂代理就在①页「网络出口」里填，留空就是本机直连，程序不会自己去碰代理。
    每个 ZIP 一路翻页到 PropWire 自己报的底数为止（单页实测上限 250 条）。
    只要有人住的房子：类型只勾住宅六种（独栋 / 公寓联排 / 2-4户 / 5户以上 / 移动屋 / 其他），
    空地 Land 和商业 Commercial 根本不查；查回来再剔掉 vacant_home（空关房）、
    vacant_lot（空地）标记的。

五、实测产量，心里有数
    每个 ZIP 的数量差极大，别按平均数估。德州随机抽 40 个 ZIP（{sample}）：
      普通档：合计 33,382 条，中位数 260/ZIP，最大 7,844，12 个 ZIP 是 0
      高净值档：合计 2,618 条，中位数 12/ZIP，最大 555，15 个 ZIP 是 0
    已验证按 ZIP 查不会漏：8 个德州城市逐个对比「按城市查」和「该市所有 ZIP 相加」，
    差在 ±5% 以内，而且 8 个里有 5 个是 ZIP 侧更多（因为一个 ZIP 会跨到邻市）。
    另外实测估价字段从不缺失，所以价格门不会误杀没估价的房子。
"""


def main():
    for name, script, has_max, dmin, dmax in JOBS:
        dist = BASE / "dist_addr" / name
        print(f"\n===== 打包 {name} =====", flush=True)
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
            "--name", name,
            "--distpath", str(BASE / "dist_addr"),
            "--workpath", str(BASE / "build_addr"),
            "--specpath", str(BASE / "build_addr"),
            "--collect-all", "nodriver",
            "--collect-submodules", "PySide6",
            "--hidden-import", "pw_engine",
            "--hidden-import", "proxy_relay",
            "--hidden-import", "browser_path",
            "--hidden-import", "app_addr",
            str(BASE / script)], check=True)

        zip_src = Path(r"D:\普通房屋地址\us_zip_codes.csv")
        if not (dist / "us_zip_codes.csv").exists():
            if not zip_src.is_file():
                raise SystemExit(f"ZIP 目录 {zip_src} 不在，先跑一次抓取让它落地")
            shutil.copy2(zip_src, dist / "us_zip_codes.csv")

        how, size = bundle_browser.bundle(dist)
        print(f"      自带 Chromium：{how}，{size / 1e6:.0f} MB", flush=True)

        (dist / f"启动{name}.bat").write_text(
            f'@echo off\r\ncd /d "%~dp0"\r\nstart "" "{name}.exe"\r\n', encoding="gbk")
        desc = ("普通房屋地址：估价 40万~120万（上下限都能自己改）" if has_max
                else "高净值房屋地址：估价 120万以上，上不封顶（门槛能自己改）")
        sample = ("40万~120万" if has_max else "120万以上")
        (dist / "启动说明.txt").write_text(
            README.format(name=name, exe=name, desc=desc, dmin=f"{dmin:,}",
                          maxline=f" --max {dmax:,}" if has_max else "",
                          sample=sample), encoding="utf-8")
        print(f"      完成：{dist}", flush=True)


if __name__ == "__main__":
    main()
