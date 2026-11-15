# -*- coding: utf-8 -*-
"""高净值房屋地址抓取器：估价达到门槛的房屋地址，上不封顶。

双击就开界面（选州、填门槛、并发、导出、删已导出、失败 ZIP 重跑都在里面）。
带参数就跑命令行：
    高净值地址抓取.py --states TX --min 1200000 --workers 4
    高净值地址抓取.py --stat / --export 10000 / --list-failed / --retry-failed --yes
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pw_engine

ARGS = dict(label="高净值房屋", default_out=r"D:\高净值房屋地址",
            has_max=False, dmin=1200000, dmax=None)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        pw_engine.main(**ARGS, script="高净值地址抓取.py")
    else:
        import app_addr
        app_addr.run(**ARGS)
