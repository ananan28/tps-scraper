# -*- coding: utf-8 -*-
"""把「随时监控」这一批交付物铺到 U 盘（11-18）。

规矩（历史上被说过的那些）：
· 用 Python 拷 + 逐文件 SHA256 和字节数对平（robocopy 静默失败坑过）；
· U 盘根目录放中文产品名文件夹，别埋在子目录里；
· 一个数据文件都不许跟着走：tps_data.db / hub_data.db / *.csv 一律拦下，
  姓名库 user_names.json 属于「程序要用但不是号码」，本包也不带（那几台机器上已经有了）；
· 拷完把台账打在屏上，并写一份 U盘拷贝台账_1118.txt，回头对得上。

没插 U 盘就明说，不去猜盘符。
"""
import ctypes
import hashlib
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HOME = Path.home()
JOBS = [(HOME / "Desktop" / "抓号机器中心", "抓号机器中心"),
        (HOME / "Desktop" / "随时监控版", "随时监控版")]
BAN = (".db", ".db-wal", ".db-shm", ".sqlite", ".csv", ".json", ".xlsx")


def removable_letters():
    """GetDriveTypeW==2 才算可移动盘（1=C:、3=本地固定盘、5=光驱）。"""
    got = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if got & (1 << i):
            letter = chr(65 + i)
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 2:
                out.append(letter)
    return out


def sha(p: Path):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def main():
    for src, _ in JOBS:
        assert src.exists(), f"源不在：{src}（先跑 build_hub.py / build_worker_overlay.py）"
    letters = removable_letters()
    if not letters:
        print("没看到可移动盘（U 盘）。插上再跑本脚本 —— 我不猜盘符，也不往 C 盘别处塞。")
        print("现在挂着的盘：", [f"{chr(65+i)}:" for i in range(26)
                                if ctypes.windll.kernel32.GetDriveTypeW(f"{chr(65+i)}:\\") != 0])
        return 2
    dst_root = Path(letters[0] + ":/")
    print(f"U 盘：{dst_root}（可移动盘 {letters}）\n")
    ledger = []
    for src, name in JOBS:
        dst = dst_root / name
        dst.mkdir(parents=True, exist_ok=True)
        files = [p for p in sorted(src.rglob("*")) if p.is_file()]
        banned = [str(p.relative_to(src)) for p in files if p.suffix.lower() in BAN]
        assert not banned, f"{name} 里混了数据/配置文件，不许铺：{banned}"
        print(f"→ {name}（{len(files)} 个文件，"
              f"{sum(p.stat().st_size for p in files)/2**20:.1f}MB）")
        for p in files:
            q = dst / p.relative_to(src)
            q.parent.mkdir(parents=True, exist_ok=True)
            same = q.exists() and q.stat().st_size == p.stat().st_size and sha(q) == sha(p)
            if not same:
                import shutil
                shutil.copy2(p, q)
            d, s = sha(q), p.stat().st_size
            assert d == sha(p) and s == q.stat().st_size, f"{p.name} 拷完对不上"
            ledger.append((name, str(p.relative_to(src)), s, d))
            print(f"   ✓ {d[:16]}  {s:>12,d}  {p.relative_to(src)}")
    # U 盘上不该有上一次留下的同名数据文件
    stray = [str(x) for name in dict((n, 1) for n, _ in JOBS)
             for x in (dst_root / name).rglob("*") if x.is_file() and x.suffix.lower() in BAN]
    assert not stray, f"U 盘上发现数据文件：{stray}"
    txt = (f"随时监控交付 · U 盘拷贝台账　{datetime.now():%Y-%m-%d %H:%M}\n"
           f"U 盘：{dst_root}\n" + "=" * 66 + "\n")
    for name, rel, s, d in ledger:
        txt += f"{name}\\{rel}\t{s}\t{d}\n"
    txt += "\n逐文件 SHA256 + 字节数已与桌面那份对平；一个数据文件都没带。\n"
    (dst_root / "U盘拷贝台账_1118.txt").write_text(txt, encoding="utf-8-sig")
    print(f"\n✓ 铺完：{len(ledger)} 个文件全部 SHA256 对平 → {dst_root}"
          f"｜台账 U盘拷贝台账_1118.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
