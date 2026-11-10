# -*- coding: utf-8 -*-
"""realtor.com 经纪人抓取器 - 界面"""
import os
import json
import re
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QSplitter, QVBoxLayout, QWidget, QLineEdit
)

import realtor_engine as eng
from browser_path import browser_kind, check_text, find_browser, set_override

BASE = Path(__file__).resolve().parent
CFG = BASE / "realtor_config.json"
ZIP_TOKEN = re.compile(r"\b\d{5}\b")


class Bridge(QObject):
    log = Signal(str)
    refresh = Signal()


class Main(QMainWindow):

    def __init__(self):
        super().__init__()
        eng.init_db()
        self.setWindowTitle("realtor.com 经纪人抓取器")
        self.resize(1400, 900)
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.refresh.connect(self.refresh)
        self.running = False
        self.stop_flag = threading.Event()
        self._t0 = 0.0
        self._n_log = 0
        self._log_fh = self._open_run_log()
        self.build()
        self.load_config()
        self.refresh()
        self._timer = QTimer()
        self._timer.timeout.connect(self._heartbeat)
        self._timer.start(1000)

    # ---------- UI ----------
    def build(self):
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        form = QVBoxLayout(left)

        cfg = QGroupBox("抓取设置")
        g = QGridLayout()
        row = 0
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        self.concurrency.setValue(6)
        self.concurrency.setToolTip(
            "同时打开的标签页总数。直连时超过 6 页就会被 Kasada 硬封十几分钟；"
            "走住宅代理可以放心开大（每 IP 标签页数决定要开几个出口）")
        g.addWidget(QLabel("并发标签页"), row, 0)
        g.addWidget(self.concurrency, row, 1)
        row += 1
        self.delay = QSpinBox()
        self.delay.setRange(0, 30)
        self.delay.setValue(4)
        self.delay.setSuffix(" 秒")
        self.delay.setToolTip("每次进详情页后的等待时间，太短会读不到号码")
        g.addWidget(QLabel("详情页等待"), row, 0)
        g.addWidget(self.delay, row, 1)
        row += 1
        self.pace = QSpinBox()
        self.pace.setRange(0, 60)
        self.pace.setValue(1)
        self.pace.setSuffix(" 秒")
        self.pace.setToolTip("抓完一个经纪人后的停顿，用于降低被拦概率")
        g.addWidget(QLabel("每人停顿"), row, 0)
        g.addWidget(self.pace, row, 1)
        row += 1
        self.tabs_per_ip = QSpinBox()
        self.tabs_per_ip.setRange(1, 12)
        self.tabs_per_ip.setValue(4)
        self.tabs_per_ip.setToolTip(
            "填了代理才生效：每 1 个出口 IP（=1 个浏览器）开几个标签页。"
            "并发 32 / 每 IP 4 页 = 8 个 IP 同时跑")
        g.addWidget(QLabel("每 IP 标签页"), row, 0)
        g.addWidget(self.tabs_per_ip, row, 1)
        row += 1
        self.proxy_url = QLineEdit()
        self.proxy_url.setPlaceholderText(
            "留空=本机直连（会被 Kasada 临时封）；例：http://你的账号-zone-custom-region-US@代理主机:端口")
        g.addWidget(QLabel("代理地址"), row, 0)
        g.addWidget(self.proxy_url, row, 1)
        row += 1
        self.proxy_pass = QLineEdit()
        self.proxy_pass.setEchoMode(QLineEdit.Password)
        self.proxy_pass.setPlaceholderText("代理密码")
        g.addWidget(QLabel("代理密码"), row, 0)
        g.addWidget(self.proxy_pass, row, 1)
        row += 1
        hb = QHBoxLayout()
        self.browser_edit = QLineEdit()
        self.browser_edit.setPlaceholderText(
            "留空就行：本机 Chrome/Edge → 没有的话自动用软件自带的 Chromium")
        self.browser_edit.setToolTip(
            "抓取器要起一个真浏览器窗口。优先级：\n"
            "1) 这一栏手选的路径；\n"
            "2) 本机已装的 Chrome / Edge / Brave（注册表 App Paths → 各盘符 "
            "Program Files → 所有用户配置目录 → PATH）；\n"
            "3) 前两级都没有，就用交付文件夹里自带的 browser\\chrome-win64\\chrome.exe。\n"
            "真 Chrome 的指纹比自带的干净 Chromium 更像普通用户，所以本机装了就用本机那份。")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self.pick_browser)
        btn_detect = QPushButton("自动检测")
        btn_detect.clicked.connect(self.detect_browser)
        hb.addWidget(self.browser_edit)
        hb.addWidget(btn_browse)
        hb.addWidget(btn_detect)
        g.addLayout(hb, row, 0, 1, 2)
        cfg.setLayout(g)
        form.addWidget(cfg)

        imp = QGroupBox("ZIP 导入")
        iv = QVBoxLayout()
        self.import_box = QPlainTextEdit()
        self.import_box.setPlaceholderText("粘贴 ZIP，一行一个或用逗号/空格分隔；也支持整段文字里挑 5 位 ZIP")
        self.import_box.setMaximumHeight(120)
        iv.addWidget(self.import_box)
        br = QHBoxLayout()
        b1 = QPushButton("导入文本框内容")
        b1.clicked.connect(self.import_paste)
        b2 = QPushButton("从文件导入…")
        b2.clicked.connect(self.import_file)
        br.addWidget(b1)
        br.addWidget(b2)
        iv.addLayout(br)
        imp.setLayout(iv)
        form.addWidget(imp)

        ctl = QGroupBox("控制")
        cv = QGridLayout()
        self.btn_start = QPushButton("开始抓取（待处理 ZIP）")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self.export)
        self.btn_retry = QPushButton("重跑未完成")
        self.btn_retry.clicked.connect(self.retry_failed)
        cv.addWidget(self.btn_start, 0, 0)
        cv.addWidget(self.btn_stop, 0, 1)
        cv.addWidget(self.btn_export, 1, 0)
        cv.addWidget(self.btn_retry, 1, 1)
        ctl.setLayout(cv)
        form.addWidget(ctl)

        st = QGroupBox("统计")
        sg = QGridLayout()
        self.stat_labels = {}
        pairs = [("pending", "待处理 ZIP"), ("done", "已完成 ZIP"),
                 ("agents", "已存经纪人"), ("phones", "有号码"),
                 ("rate", "速度"), ("status", "状态")]
        for i, (key, label) in enumerate(pairs):
            sg.addWidget(QLabel(label), i, 0)
            v = QLabel("0")
            v.setFont(QFont("Consolas", 11, QFont.Bold))
            sg.addWidget(v, i, 1)
            self.stat_labels[key] = v
        st.setLayout(sg)
        form.addWidget(st)
        form.addStretch(1)

        right = QWidget()
        rv = QVBoxLayout(right)
        lg = QGroupBox("日志")
        lv = QVBoxLayout()
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        self.log_box.setMaximumBlockCount(300)
        lv.addWidget(self.log_box)
        clear = QPushButton("清除日志")
        clear.clicked.connect(self.log_box.clear)
        clear.setMaximumWidth(100)
        lv.addWidget(clear, alignment=Qt.AlignRight)
        lg.setLayout(lv)
        rv.addWidget(lg)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([420, 980])
        self.setCentralWidget(splitter)

    def _open_run_log(self):
        try:
            d = BASE / "logs"
            d.mkdir(parents=True, exist_ok=True)
            for old in sorted(d.glob("realtor_*.log"))[:-9]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return open(d / f"realtor_{datetime.now():%Y%m%d_%H%M%S}.log",
                        "a", encoding="utf-8")
        except OSError:
            return open(os.devnull, "w", encoding="utf-8")

    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        try:
            self.log_box.appendPlainText(line)
        except RuntimeError:
            pass
        try:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        except Exception:
            pass

    # ---------- actions ----------
    def save_config(self):
        try:
            CFG.write_text(json.dumps({
                "concurrency": self.concurrency.value(),
                "delay": self.delay.value(),
                "pace": self.pace.value(),
                "tabs_per_ip": self.tabs_per_ip.value(),
                "proxy_url": self.proxy_url.text().strip(),
                "proxy_pass": self.proxy_pass.text().strip(),
                "browser_path": self.browser_edit.text().strip(),
            }), encoding="utf-8")
        except OSError:
            pass

    def load_config(self):
        try:
            c = json.loads(CFG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        self.concurrency.setValue(int(c.get("concurrency", 6)))
        self.delay.setValue(int(c.get("delay", 4)))
        self.pace.setValue(int(c.get("pace", 1)))
        self.tabs_per_ip.setValue(int(c.get("tabs_per_ip", 4)))
        self.proxy_url.setText(c.get("proxy_url", ""))
        self.proxy_pass.setText(c.get("proxy_pass", ""))
        self.browser_edit.setText(str(c.get("browser_path", "")))
        set_override(self.browser_edit.text())

    # ---------- browser ----------
    def pick_browser(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选浏览器主程序", "",
            "浏览器 (chrome.exe msedge.exe brave.exe chromium.exe);;所有文件 (*.*)")
        if p:
            self.browser_edit.setText(p)
            self.log(f"浏览器已指到：{set_override(p) or '这个路径不存在，再选一次'}")

    def detect_browser(self):
        set_override("")
        self.browser_edit.clear()
        self.log("浏览器自检：\n" + check_text())

    def need_browser(self):
        """启动前的硬门槛：一个浏览器都起不了就别开线程，把话说清楚。"""
        set_override(self.browser_edit.text())
        exe = find_browser()
        if not exe:
            QMessageBox.warning(
                self, "起不了浏览器",
                "抓取器要起一个真浏览器窗口，但这台机器上找不到，"
                "软件自带的那份 Chromium 也不在（缺 browser 目录）。\n\n"
                + check_text() +
                "\n\n要么装个 Chrome，要么把交付文件夹里的 browser 目录补回来，"
                "要么在「浏览器路径」里手动指一个。")
            self.log("启动失败：本机没浏览器，且 browser 目录里也没有自带的 Chromium。")
            return ""
        self.log(f"浏览器：{exe}（{browser_kind(exe)}）")
        return exe

    def _extract_zips(self, text):
        return [z for z in ZIP_TOKEN.findall(text or "")]

    def import_paste(self):
        zips = self._extract_zips(self.import_box.toPlainText())
        self._do_import(zips)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ZIP 文件", str(BASE), "文本/CSV (*.txt *.csv);;所有文件 (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return
        self._do_import(self._extract_zips(text))

    def _do_import(self, zips):
        if not zips:
            QMessageBox.information(self, "没有 ZIP", "没找到 5 位 ZIP 码")
            return
        added = eng.import_zips(zips)
        self.import_box.clear()
        self.log(f"导入 {added} 个 ZIP（解析到 {len(set(zips))} 个不同 ZIP）")
        self.refresh()

    def start(self):
        if self.running:
            return
        zips = eng.get_pending_zips()
        if not zips:
            QMessageBox.information(self, "没有待处理地址", "先导入 ZIP")
            return
        self.running = True
        self.stop_flag.clear()
        if not self.need_browser():
            self.running = False
            return
        self._t0 = time.time()
        self._n_log = 0
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        conc = int(self.concurrency.value())
        delay = float(self.delay.value())
        pace = float(self.pace.value())
        purl = self.proxy_url.text().strip()
        ppass = self.proxy_pass.text().strip()
        tip = int(self.tabs_per_ip.value())
        mode = f"代理 {purl.split('@')[-1]} 每IP{tip}页" if purl else "本机直连"
        self.save_config()
        self.log(f"启动，并发 {conc}（{mode}），详情等待 {delay:.0f}s，每人停顿 {pace:.0f}s，待处理 {len(zips)} 个 ZIP")

        def cb(msg):
            self.bridge.log.emit(msg)

        def work():
            try:
                eng.run_batch(zips, concurrency=conc, progress_cb=cb,
                              stop=self.stop_flag, delay=delay, pace=pace,
                              proxy_template=purl, proxy_pass=ppass,
                              tabs_per_ip=tip)
                self.bridge.log.emit("全部完成" if not self.stop_flag.is_set() else "已停止")
            except Exception as e:
                self.bridge.log.emit(f"运行出错: {e}")
            finally:
                self.running = False
                self.bridge.log.emit(f"本次耗时 {(time.time()-self._t0)/60:.1f} 分钟")

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        self.log("请求停止…")

    def retry_failed(self):
        eng.reset_incomplete()
        self.log("未完成的 ZIP 已重新排队")
        self.refresh()

    def export(self):
        path, n = eng.export_csv()
        self.log(f"导出 {n} 条 → {path}")

    # ---------- stats ----------
    def _heartbeat(self):
        if self.running:
            self.refresh()

    def refresh(self):
        s = eng.stats()
        self.stat_labels["pending"].setText(str(s["pending"]))
        self.stat_labels["done"].setText(str(s["done"]))
        self.stat_labels["agents"].setText(str(s["agents"]))
        self.stat_labels["phones"].setText(str(s["phones"]))
        if self.running and self._t0:
            mins = (time.time() - self._t0) / 60
            rate = s["agents"] / mins if mins > 0 else 0
            self.stat_labels["rate"].setText(f"{rate:.1f} 人/分")
        else:
            self.stat_labels["rate"].setText("0 人/分")
        self.stat_labels["status"].setText("运行中" if self.running else "空闲")
        if self.running:
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(True)
        else:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def closeEvent(self, event):
        self.stop_flag.set()
        try:
            self._log_fh.close()
        except Exception:
            pass
        event.accept()


def main():
    if "--check-browser" in sys.argv:
        # 打包成 --windowed 后没有控制台，自检结果落到 exe 旁边的文件里
        txt = check_text()
        (Path(sys.argv[0]).resolve().parent / "browser_check.txt").write_text(
            txt, encoding="utf-8")
        print(txt)
        return
    app = QApplication(sys.argv)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
