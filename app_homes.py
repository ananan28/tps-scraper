# -*- coding: utf-8 -*-
"""homes.com 经纪人抓取器 - 界面"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget
)

import homes_engine as eng
from browser_path import browser_kind, check_text, find_browser, set_override

BASE = Path(__file__).resolve().parent
CFG = BASE / "homes_config.json"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# homes.com 的 slug 一定以 2 位州缩写结尾（或直接是州名本身），靠这条挡住乱贴的文本
STATE_FULL = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new-hampshire": "nh", "new-jersey": "nj",
    "new-mexico": "nm", "new-york": "ny", "north-carolina": "nc",
    "north-dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode-island": "ri", "south-carolina": "sc",
    "south-dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west-virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district-of-columbia": "dc",
}
STATE_SLUGS = set(eng.US_STATES)
VALID_ABBR = set(STATE_FULL.values())


def _to_slug(raw):
    t = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not t or not SLUG_RE.match(t) or len(t) > 70:
        return None
    if t in STATE_SLUGS:
        return t
    parts = t.split("-")
    last = parts[-1]
    if last in STATE_FULL:
        parts[-1] = STATE_FULL[last]
        last = STATE_FULL[last]
    if len(last) != 2 or last not in VALID_ABBR:
        return None
    return "-".join(parts)


def _is_state_token(tok):
    t = re.sub(r"[^a-z0-9]+", "-", tok.strip().lower()).strip("-")
    return (len(t) == 2 and t in VALID_ABBR) or t in STATE_FULL


def extract_slugs(text):
    """支持 "Miami, FL" / "Los Angeles CA" / "Denver, Colorado" / 直接给 slug "miami-fl" / "florida"。
    逗号后面紧跟州名（缩写或全称）时并成一个 slug，不会被拆成两半。"""
    out = []
    for raw in re.split(r"[\n;；、]+", text or ""):
        toks = [t.strip(" .") for t in re.split(r"[,\t]+", raw) if t.strip(" .")]
        i = 0
        while i < len(toks):
            piece = toks[i]
            if i + 1 < len(toks) and _is_state_token(toks[i + 1]):
                piece = f"{piece}-{toks[i + 1]}"
                i += 2
            else:
                i += 1
            s = _to_slug(piece)
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


class Bridge(QObject):
    log = Signal(str)
    refresh = Signal()


class Main(QMainWindow):

    def __init__(self):
        super().__init__()
        eng.init_db()
        self.setWindowTitle("homes.com 经纪人抓取器")
        self.resize(1400, 900)
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.refresh.connect(self.refresh)
        self.running = False
        self.stop_flag = threading.Event()
        self._t0 = 0.0
        self._base_agents = 0
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
        self.concurrency.setRange(1, 24)
        self.concurrency.setValue(4)
        self.concurrency.setToolTip(
            "同时打开的标签页数。homes.com 一页 48 人，4 页并发就够吃掉几百人/次，\n"
            "开太大只会被 Akamai 打分，本机直连建议 4-8")
        g.addWidget(QLabel("并发标签页"), row, 0)
        g.addWidget(self.concurrency, row, 1)
        row += 1
        self.wait = QSpinBox()
        self.wait.setRange(2, 30)
        self.wait.setValue(6)
        self.wait.setSuffix(" 秒")
        self.wait.setToolTip("每翻一页后的等待时间，太短会读到还没渲染完的半页（少于 48 人）")
        g.addWidget(QLabel("每页等待"), row, 0)
        g.addWidget(self.wait, row, 1)
        row += 1
        self.pace = QSpinBox()
        self.pace.setRange(0, 20)
        self.pace.setValue(0)
        self.pace.setSuffix(" 秒")
        self.pace.setToolTip("同一地区内两次翻页之间的停顿")
        g.addWidget(QLabel("翻页间隔"), row, 0)
        g.addWidget(self.pace, row, 1)
        row += 1
        self.discover = QCheckBox("自动发现下级地区（州→县→市→相邻市，一直滚下去）")
        self.discover.setChecked(True)
        g.addWidget(QLabel("地区发现"), row, 0)
        g.addWidget(self.discover, row, 1)
        row += 1
        self.drop_female = QCheckBox("剔除女性姓名（按姓名库判定）")
        self.drop_female.setChecked(True)
        g.addWidget(QLabel("性别筛选"), row, 0)
        g.addWidget(self.drop_female, row, 1)
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
            "第 2 级优先是因为真 Chrome 的指纹比自带的干净 Chromium 更像普通用户，"
            "过 Akamai 更稳。目标机一个浏览器都没装也能直接跑，就是走第 3 级。")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self.pick_browser)
        btn_detect = QPushButton("自动检测")
        btn_detect.clicked.connect(self.detect_browser)
        hb.addWidget(self.browser_edit)
        hb.addWidget(btn_browse)
        hb.addWidget(btn_detect)
        g.addLayout(hb, row, 0, 1, 2)
        row += 1
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(
            "http://USER:PASS@global.rp.linkup.onl:10000  （留空=本机直连）")
        self.proxy_edit.setToolTip(
            "2026-11-10 实测：本机（泰国出口）直连 homes.com 已经被边缘 403，"
            "连 robots.txt 都取不到，必须从美国住宅 IP 出去。\n"
            "写法支持两种：完整链接 http://user:pass@host:port，"
            "或者只写用户名 + 下面单独填密码。\n"
            "留空就按本机直连跑，日志里会打印当前出口 IP 让你核对。")
        self.proxy_pass = QLineEdit()
        self.proxy_pass.setEchoMode(QLineEdit.Password)
        self.proxy_pass.setPlaceholderText("代理密码（单独填）")
        pv = QHBoxLayout()
        pv.addWidget(self.proxy_edit, 3)
        pv.addWidget(self.proxy_pass, 1)
        g.addWidget(QLabel("代理出口"), row, 0)
        g.addLayout(pv, row, 1)
        row += 1
        tip = QLabel("列表页就带电话，不进详情页。目录页会先回一张 2.4KB 的挑战页，"
                     "它自己 reload 后 4~25 秒变成真页面——新版引擎原地等它，不再误判成被拦。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666")
        g.addWidget(tip, row, 0, 1, 2)
        cfg.setLayout(g)
        form.addWidget(cfg)

        imp = QGroupBox("地区导入")
        iv = QVBoxLayout()
        self.import_box = QPlainTextEdit()
        self.import_box.setPlaceholderText(
            "一行一个地区，支持：\n"
            "  Miami, FL          → miami-fl\n"
            "  Los Angeles CA     → los-angeles-ca\n"
            "  florida            → 整个州\n"
            "  miami-dade-county-fl → 整个县\n"
            "注意：本站不认 ZIP 码，只认城市/县/州")
        self.import_box.setMaximumHeight(150)
        iv.addWidget(self.import_box)
        br = QHBoxLayout()
        b1 = QPushButton("导入文本框内容")
        b1.clicked.connect(self.import_paste)
        b2 = QPushButton("从文件导入…")
        b2.clicked.connect(self.import_file)
        b3 = QPushButton("填入 50 个州")
        b3.clicked.connect(self.fill_states)
        br.addWidget(b1)
        br.addWidget(b2)
        br.addWidget(b3)
        iv.addLayout(br)
        imp.setLayout(iv)
        form.addWidget(imp)

        ctl = QGroupBox("控制")
        cv = QGridLayout()
        self.btn_start = QPushButton("开始抓取（待处理地区）")
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
        pairs = [("pending", "待处理地区"), ("done", "已完成地区"),
                 ("failed", "被拦中断"), ("invalid", "无效地区"),
                 ("agents", "已存经纪人"),
                 ("phones", "有号码"), ("rate", "速度"), ("status", "状态")]
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
        splitter.setSizes([460, 940])
        self.setCentralWidget(splitter)

    def _open_run_log(self):
        try:
            d = BASE / "logs"
            d.mkdir(parents=True, exist_ok=True)
            for old in sorted(d.glob("homes_*.log"))[:-9]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return open(d / f"homes_{datetime.now():%Y%m%d_%H%M%S}.log",
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

    # ---------- config ----------
    def save_config(self):
        try:
            CFG.write_text(json.dumps({
                "concurrency": self.concurrency.value(),
                "wait": self.wait.value(),
                "pace": self.pace.value(),
                "discover": self.discover.isChecked(),
                "drop_female": self.drop_female.isChecked(),
                "browser_path": self.browser_edit.text().strip(),
                "proxy": self.proxy_edit.text().strip(),
                "proxy_pass": self.proxy_pass.text(),
            }), encoding="utf-8")
        except OSError:
            pass

    def load_config(self):
        try:
            c = json.loads(CFG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            c = {}
        self.concurrency.setValue(int(c.get("concurrency", 4)))
        self.wait.setValue(int(c.get("wait", 6)))
        self.pace.setValue(int(c.get("pace", 0)))
        self.discover.setChecked(bool(c.get("discover", True)))
        self.drop_female.setChecked(bool(c.get("drop_female", True)))
        self.browser_edit.setText(str(c.get("browser_path", "")))
        self.proxy_edit.setText(str(c.get("proxy", "")))
        self.proxy_pass.setText(str(c.get("proxy_pass", "")))
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
                "要么在「抓取设置 → 浏览器路径」里手动指到 chrome.exe。")
            self.log("启动失败：本机没浏览器，且 browser 目录里也没有自带的 Chromium。")
            return ""
        self.log(f"浏览器：{exe}（{browser_kind(exe)}）")
        return exe

    # ---------- actions ----------
    def fill_states(self):
        self.import_box.setPlainText("\n".join(eng.US_STATES))
        self.log("已填入 50 个州，点“导入文本框内容”入库")

    def import_paste(self):
        text = self.import_box.toPlainText()
        self._do_import(extract_slugs(text), text)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择地区文件", str(BASE), "文本/CSV (*.txt *.csv);;所有文件 (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            QMessageBox.warning(self, "读取失败", str(e))
            return
        self._do_import(extract_slugs(text), text)

    def _do_import(self, slugs, text=""):
        if re.search(r"\b\d{5}\b", text or "") and not slugs:
            QMessageBox.information(
                self, "ZIP 不认",
                "homes.com 的索引是城市/县/州，ZIP 码在这站无效。\n"
                "要按 ZIP 抓就用 realtor 那个抓取器。")
            return
        if not slugs:
            QMessageBox.information(self, "没有地区", "没解析出可用的城市/县/州名")
            return
        added = eng.import_slugs(slugs)
        self.import_box.clear()
        self.log(f"导入 {added} 个地区（解析到 {len(slugs)} 个不同地区）")
        self.refresh()

    def start(self):
        if self.running:
            return
        slugs = eng.get_pending_slugs()
        if not slugs:
            QMessageBox.information(self, "没有待处理地区", "先导入地区（或勾上自动发现后跑一轮）")
            return
        self.running = True
        self.stop_flag.clear()
        if not self.need_browser():
            self.running = False
            return
        self._t0 = time.time()
        self._base_agents = eng.stats()["agents"]
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        conc = int(self.concurrency.value())
        wait = float(self.wait.value())
        pace = float(self.pace.value())
        disc = self.discover.isChecked()
        female = self.drop_female.isChecked()
        proxy = self.proxy_edit.text().strip()
        proxy_pass = self.proxy_pass.text()
        self.save_config()
        self.log(f"启动：并发 {conc} 页，每页等 {wait:.0f}s，待处理 {len(slugs)} 个地区，"
                 f"{'自动发现' if disc else '不发现'}，{'剔女' if female else '男女都留'}，"
                 f"{'走代理' if proxy else '本机直连'}")

        def cb(msg):
            self.bridge.log.emit(msg)

        def work():
            try:
                eng.run_batch(slugs, concurrency=conc, progress_cb=cb, stop=self.stop_flag,
                              wait=wait, pace=pace, discover=disc, drop_female=female,
                              proxy=proxy, proxy_pass=proxy_pass)
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
        n = eng.reset_incomplete()
        self.log(f"未完成的地区已重新排队（{n} 个）")
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
        for key, val in (("pending", s["pending"]), ("done", s["done"]),
                         ("failed", s["failed"]), ("invalid", s["invalid"]),
                         ("agents", s["agents"]),
                         ("phones", s["phones"])):
            self.stat_labels[key].setText(str(val))
        if self.running and self._t0:
            mins = (time.time() - self._t0) / 60
            rate = (s["agents"] - self._base_agents) / mins if mins > 0 else 0
            self.stat_labels["rate"].setText(f"{rate:.1f} 人/分")
        else:
            self.stat_labels["rate"].setText("0 人/分")
        self.stat_labels["status"].setText("运行中" if self.running else "空闲")
        self.btn_start.setEnabled(not self.running)
        self.btn_stop.setEnabled(self.running)

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
