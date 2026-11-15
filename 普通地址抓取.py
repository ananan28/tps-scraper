# -*- coding: utf-8 -*-
"""普通房屋地址抓取器：估价落在「下限~上限」之间、且有人住的房屋地址。

双击就开界面（选州、填价格、并发、导出、删已导出、失败 ZIP 重跑都在里面）。
带参数就跑命令行，两种用法互不干扰：
    普通地址抓取.py --states TX --min 400000 --max 1200000 --workers 4
    普通地址抓取.py --stat / --export 10000 / --list-failed / --retry-failed --yes
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_engine

ARGS = dict(label="普通房屋", default_out=r"D:\普通房屋地址",
            has_max=True, dmin=400000, dmax=1200000)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        pw_engine.main(**ARGS, script="普通地址抓取.py")
    else:
        import app_addr
        app_addr.run(**ARGS)
