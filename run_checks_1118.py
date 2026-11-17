# -*- coding: utf-8 -*-
"""把这次「随时监控」相关的自检全部真跑一遍，结果落成 实测输出\\实测_*.txt（11-18）。

为什么要这个：交付包里那份 README 说的每个数字，都得有出处文件能对得上，
不能我在聊天里报完就算数。跑失败任何一份就整个中止 —— 不许铺包。
"""
import datetime
import pathlib
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent
OUT = BASE / "实测输出"
OUT.mkdir(exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JOBS = [
    ("实测_中心端离线自检.txt", ["test_hub_server.py"],
     "中心端 hub_server：令牌/心跳/日志去重/指令一次/只监控闸门/派任务不重发"),
    ("实测_工人端离线自检.txt", ["test_hub_client.py"],
     "工人端 hub_client：零请求默认/成批推/断网补传不翻倍/号码单独一个勾/队列封顶"),
    ("实测_界面连中心自检.txt", ["test_hub_ui.py"],
     "真界面 ↔ 真中心：三个勾默认关/勾上不用重启/远端改并发动了真控件/日志配对 A/B 计时"),
    ("实测_中心EXE冒烟.txt", ["smoke_hub_exe.py"],
     "打出来的 hub.exe 真起服务：banner/401-200/默认只监控/面板 HTML/临时目录留库"),
    ("实测_exe内容核对.txt", ["check_exe_1118.py"],
     "TPS抓取器.exe 里真带着这次的新代码（PYZ 取 code object 比字符串）"),
    ("实测_工人EXE冒烟.txt", ["smoke_worker_exe_1118.py"],
     "打出来的抓取器 exe 真跑两趟：不勾零请求；勾了 2 秒内心跳进中心、0 行号码、控制 403"),
]


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for name, args, desc in JOBS:
        cmd = [sys.executable, "-X", "utf8"] + [str(BASE / a) for a in args]
        print(f"跑 {args[0]} ...", flush=True)
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, timeout=1800)
        body = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        last = [l for l in body.splitlines() if l.strip()]
        ok = r.returncode == 0 and bool(last) and last[-1].lstrip().startswith("✓")
        head = (f"随时监控 · 实测记录\n{'=' * 58}\n"
                f"验的是：{desc}\n\n"
                f"跑的时间：{stamp}\n"
                f"命令：{' '.join(a for a in args)}\n"
                f"退出码：{r.returncode}　判定：{'全过' if ok else '没过（别看下面了，先修）'}\n"
                f"结论行：{last[-1] if last else '（没输出）'}\n"
                + "=" * 58 + "\n\n")
        (OUT / name).write_text(head + body + (("\n--- stderr ---\n" + err) if err.strip() else ""),
                                encoding="utf-8-sig")
        print(f"  {'✓' if ok else '✗'} {name}｜{(last[-1] if last else '')[:64]}", flush=True)
        rows.append((name, ok))
        assert ok, f"{args[0]} 没过，不许铺包（详情看 {OUT / name}）"
    tot = sum(1 for _, o in rows if o)
    print(f"\n✓ {tot}/{len(JOBS)} 份实测记录已落 {OUT}")


if __name__ == "__main__":
    main()
