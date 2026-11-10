# -*- coding: utf-8 -*-
"""把配置好的 TPS 抓取器整包拷到 U 盘，数据库用 backup API 做一致性快照。"""
import shutil
import sqlite3
from pathlib import Path

SRC = Path(r"C:\Users\AQ28n\Desktop\TPS_v1.8.2_UI")
DST = Path(r"D:\TPS抓取器")
DST.mkdir(parents=True, exist_ok=True)

# 大目录 + 小文件（跳过运行时日志和活动数据库文件）
for name in ("_internal", "ms-playwright"):
    print(" copying", name, "...", flush=True)
    shutil.copytree(SRC / name, DST / name, dirs_exist_ok=True)
for name in ("TPS抓取器.exe", "user_names.json", "启动TPS.bat"):
    shutil.copy2(SRC / name, DST / name)
    print(" copied", name, flush=True)

# 一致性快照数据库（含全部配置 + 现有数据），即使源库正在被使用
print(" backing up database ...", flush=True)
src_con = sqlite3.connect(str(SRC / "tps_data.db"))
dst_con = sqlite3.connect(str(DST / "tps_data.db"))
src_con.backup(dst_con)
dst_con.close()
src_con.close()

# 验证 U 盘上的库可读、配置在
con = sqlite3.connect(str(DST / "tps_data.db"))
s = {r[0]: r[1] for r in con.execute("SELECT key,value FROM settings")}
n = con.execute("SELECT COUNT(*) FROM results").fetchone()[0]
con.close()
print("VERIFY exe:", (DST / "TPS抓取器.exe").stat().st_size, "bytes", flush=True)
print("VERIFY user_names:", (DST / "user_names.json").stat().st_size, "bytes", flush=True)
print("VERIFY chromium dirs:", [d.name for d in (DST / "ms-playwright").iterdir()], flush=True)
print("VERIFY settings proxy set:", bool(s.get("proxy")), "capsolver set:", bool(s.get("capsolver")),
      "age:", s.get("min_age"), "-", s.get("max_age"), "genders:", s.get("keep_genders"), flush=True)
print("VERIFY results rows:", n, flush=True)
print("USB_COPY_DONE", flush=True)