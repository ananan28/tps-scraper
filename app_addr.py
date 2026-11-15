# -*- coding: utf-8 -*-
"""地址抓取器界面（普通房屋 / 高净值房屋 共用，由入口脚本传档位）。

三个页签：抓取 / 库存导出 / 失败台账。抓取在后台线程里跑 pw_engine.Pool，
日志和统计靠 Qt 信号回主线程，界面不会跟着浏览器一起卡住。
"""
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QAbstractItemView, QPlainTextEdit,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget)

import pw_engine

STATES = pw_engine.STATES


class Bridge(QObject):
    log = Signal(str)
    done = Signal(str)


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def traffic(mb):
    return f"{mb:.1f} MB" if mb < 1024 else f"{mb/1024:.2f} GB"


class Main(QMainWindow):
    def __init__(self, label, default_out, has_max, dmin, dmax):
        super().__init__()
        self.label = label
        self.default_out = default_out
        self.has_max = has_max
        self.dmin = dmin
        self.dmax = dmax
        self.pool = None
        self.worker = None
        self.running = False
        self.stopping = False
        self.hinted = False
        self.stop_at = 0.0
        self.gen = 0
        self.bridge = Bridge()
        self.bridge.log.connect(self.on_log)
        self.bridge.done.connect(self.on_done)
        self.setWindowTitle(f"{label}地址抓取器")
        self.fnt = QFont()
        self.fnt.setPointSize(11)
        self.fnt.setBold(True)
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.poll_stats)
        self.tick.start(1000)
        self.build()

    # ------------------------------------------------------------ 界面
    def build(self):
        tabs = QTabWidget()
        tabs.addTab(self.tab_harvest(), "① 抓取")
        tabs.addTab(self.tab_stock(), "② 库存 / 导出")
        tabs.addTab(self.tab_failed(), "③ 失败台账")
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(tabs)
        self.setCentralWidget(central)
        self.resize(1120, 760)

    def tab_harvest(self):
        w = QWidget()
        root = QVBoxLayout(w)

        box = QGroupBox(f"跑哪些州（一个都不勾 = 全美 50 州 + DC）")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        for text, keys in (("清空", []), ("全选", STATES),
                           ("德州", ["TX"]), ("东岸", ["NJ", "NY", "PA", "MA", "CT", "MD", "VA", "FL"]),
                           ("西岸", ["CA", "WA", "OR", "CO", "AZ", "NV"]),
                           ("中部", ["IL", "OH", "MI", "MN", "MO", "WI", "IN", "IA", "NE", "KS"])):
            b = QPushButton(text)
            b.clicked.connect(lambda _=0, k=keys: self.set_states(k))
            row.addWidget(b)
        row.addStretch(1)
        bl.addLayout(row)
        grid = QHBoxLayout()
        self.state_cb = {}
        cols = 9
        chunk = [STATES[i:i + cols] for i in range(0, len(STATES), cols)]
        for colgroup in chunk:
            col = QVBoxLayout()
            for s in colgroup:
                cb = QCheckBox(s)
                self.state_cb[s] = cb
                col.addWidget(cb)
            col.addStretch(0)
            grid.addLayout(col)
        bl.addLayout(grid)
        root.addWidget(box)

        line = QHBoxLayout()
        self.ed_min = QLineEdit(str(self.dmin))
        self.ed_min.setToolTip("房屋估价下限（美元）。低于这个价的一律不要。")
        line.addWidget(QLabel("估价下限 $"))
        line.addWidget(self.ed_min)
        if self.has_max:
            self.ed_max = QLineEdit(str(self.dmax))
            self.ed_max.setToolTip("房屋估价上限（美元）。达到这个价就归高净值档，这边不收。")
            line.addWidget(QLabel("　估价上限 $"))
            line.addWidget(self.ed_max)
        else:
            self.ed_max = None
            line.addWidget(QLabel("　上限：不设（高净值档上不封顶）"))
        line.addWidget(QLabel("　并发标签页"))
        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 8)
        self.sp_workers.setValue(4)
        self.sp_workers.setToolTip("3-6 比较稳。开太多 PropWire 那边的 DataDome 容易翻脸。")
        line.addWidget(self.sp_workers)
        line.addWidget(QLabel("　本轮 ZIP 数"))
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(0, 999999)
        self.sp_limit.setValue(0)
        self.sp_limit.setSpecialValueText("全部跑完")
        self.sp_limit.setToolTip("填 40 就是先抽 40 个 ZIP 试产量；0 = 所选州的 ZIP 全跑。")
        line.addWidget(self.sp_limit)
        self.cb_random = QCheckBox("随机抽")
        self.cb_random.setToolTip("勾上=从所选州的 ZIP 里随机抽，试产量用这个，别总从头几个州跑。")
        line.addWidget(self.cb_random)
        self.cb_redo = QCheckBox("重跑已跑过的")
        self.cb_redo.setToolTip("勾上=已跑过的 ZIP 再来一遍，补新挂牌。不勾就自动跳过，断点续跑。")
        line.addWidget(self.cb_redo)
        line.addStretch(1)
        root.addLayout(line)

        pbox = QGroupBox("网络出口（留空 = 走这台机器自己的网络，不花钱）")
        pl = QHBoxLayout(pbox)
        pl.addWidget(QLabel("代理"))
        # 用多行框不用单行：一行一条 = 几十条固定 IP 粘贴进来轮流用
        self.ed_proxy = QPlainTextEdit()
        self.ed_proxy.setPlaceholderText(
            "一行一条，几种写法可以混着粘：\n"
            "IP:端口:账号:密码　或　http://账号:密码@IP:端口　或　IP:端口（免认证）")
        self.ed_proxy.setToolTip(
            "一行一条 = 多个出口轮流用。被挡（403/429/会话掉）就自动换下一条。\n"
            "只填一条也行：轮换型代理商重新握手就是新 IP。")
        self.ed_proxy.setFixedHeight(64)
        pl.addWidget(self.ed_proxy, 3)
        pl.addWidget(QLabel("　密码"))
        self.ed_proxy_pass = QLineEdit()
        self.ed_proxy_pass.setPlaceholderText("代理串里已带密码就不用填")
        self.ed_proxy_pass.setToolTip("只有代理串里没带密码时才需要填这一格。")
        pl.addWidget(self.ed_proxy_pass, 1)
        self.lbl_exit = QLabel("　本机直连（不花钱）")
        self.lbl_exit.setWordWrap(True)
        pl.addWidget(self.lbl_exit, 1)
        self.ed_proxy.textChanged.connect(self.sync_exit_label)
        root.addWidget(pbox)

        run = QHBoxLayout()
        self.btn_run = QPushButton("开始抓取")
        self.btn_run.setMinimumHeight(38)
        self.btn_run.clicked.connect(self.start_harvest)
        run.addWidget(self.btn_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setMinimumHeight(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_harvest)
        run.addWidget(self.btn_stop)
        self.lbl_dir = QLabel()
        self.lbl_dir.setWordWrap(True)
        run.addWidget(self.lbl_dir, 1)
        root.addLayout(run)
        self.lbl_prog = QLabel("还没开始。")
        self.lbl_prog.setFont(self.fnt)
        self.lbl_prog.setWordWrap(True)
        root.addWidget(self.lbl_prog)

        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setFont(QFont("Consolas", 9))
        root.addWidget(self.txt, 1)
        return w

    def tab_stock(self):
        w = QWidget()
        root = QVBoxLayout(w)
        self.lbl_stock = QLabel()
        self.lbl_stock.setFont(self.fnt)
        self.lbl_stock.setWordWrap(True)
        root.addWidget(self.lbl_stock)
        btn = QPushButton("刷新库存")
        btn.clicked.connect(self.refresh_stock)
        root.addWidget(btn)

        g = QGroupBox("导出 TXT（一行一个「街道, 城市, 州 ZIP」，TPS 抓取器「从文件导入」直接吃）")
        hl = QHBoxLayout(g)
        hl.addWidget(QLabel("条数"))
        self.sp_exp = QSpinBox()
        self.sp_exp.setRange(1, 9999999)
        self.sp_exp.setValue(10000)
        hl.addWidget(self.sp_exp)
        hl.addWidget(QLabel("顺序"))
        self.cmb_by = QComboBox()
        self.cmb_by.addItems(["随机", "价格（贵的先给）"])
        hl.addWidget(self.cmb_by)
        hl.addWidget(QLabel("批次备注"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("比如 3号机")
        hl.addWidget(self.ed_name)
        self.btn_exp = QPushButton("导出（用①页勾的州）")
        self.btn_exp.clicked.connect(self.do_export)
        hl.addWidget(self.btn_exp)
        b2 = QPushButton("打开导出目录")
        b2.clicked.connect(self.open_outdir)
        hl.addWidget(b2)
        hl.addStretch(1)
        root.addWidget(g)

        root.addWidget(QLabel("导出过的批次（选中一个再点下面的按钮）："))
        self.tb_batch = QTableWidget(0, 6)
        self.tb_batch.setHorizontalHeaderLabels(
            ["批次", "时间", "州", "当时条数", "现在还剩", "文件"])
        self.tb_batch.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tb_batch.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tb_batch.setEditTriggers(QAbstractItemView.NoEditTriggers)
        root.addWidget(self.tb_batch, 1)
        hl2 = QHBoxLayout()
        for text, fn in (("放回池子（撤销导出标记）", self.recall_batch),
                         ("删除这批地址", self.delete_batch),
                         ("删除所有已导出", self.delete_exported)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            hl2.addWidget(b)
        hl2.addStretch(1)
        root.addLayout(hl2)
        return w

    def tab_failed(self):
        w = QWidget()
        root = QVBoxLayout(w)
        self.lbl_fail = QLabel()
        self.lbl_fail.setFont(self.fnt)
        self.lbl_fail.setWordWrap(True)
        root.addWidget(self.lbl_fail)
        hl = QHBoxLayout()
        b = QPushButton("刷新失败台账")
        b.clicked.connect(self.refresh_failed)
        hl.addWidget(b)
        for text, kind in (("只重跑 报错/被挡", "err"), ("只重跑 没翻到底", "trunc"),
                           ("只重跑 查到0条", "empty"), ("三类全部重跑", "")):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=0, k=kind: self.retry_failed(k))
            hl.addWidget(btn)
        hl.addStretch(1)
        root.addLayout(hl)
        self.tb_fail = QTableWidget(0, 6)
        self.tb_fail.setHorizontalHeaderLabels(
            ["ZIP", "返回条数", "PropWire 底数", "缺多少", "原因", "时间"])
        self.tb_fail.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tb_fail.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tb_fail.setEditTriggers(QAbstractItemView.NoEditTriggers)
        root.addWidget(self.tb_fail, 1)
        root.addWidget(QLabel("「查到 0 条」多数是真的小 ZIP（乡村 / 只放邮政信箱），"
                              "PropWire 全库可能就两三条，重跑也榨不出东西。"))
        return w

    # ------------------------------------------------------------ 小工具
    def set_states(self, keys):
        for s, cb in self.state_cb.items():
            cb.setChecked(s in keys)

    def selected_states(self):
        return ",".join(s for s, cb in self.state_cb.items() if cb.isChecked())

    def num(self, ed, what):
        v = ed.text().strip().replace(",", "").replace("$", "")
        if not v.isdigit():
            raise ValueError(f"{what}「{ed.text()}」不是数字")
        return int(v)

    def make_args(self):
        ap = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                    self.dmin, self.dmax)
        a = ap.parse_args([])
        a.script = (Path(sys.executable).name if getattr(sys, "frozen", False)
                    else f"{self.label}地址抓取.py")
        a.states = self.selected_states()
        a.pmin = self.num(self.ed_min, "估价下限")
        if self.has_max:
            a.pmax = self.num(self.ed_max, "估价上限")
            if a.pmin >= a.pmax:
                raise ValueError(f"下限 {a.pmin:,} 必须小于上限 {a.pmax:,}")
        a.workers = self.sp_workers.value()
        a.limit = self.sp_limit.value()
        a.random = self.cb_random.isChecked()
        a.redo = self.cb_redo.isChecked()
        a.proxy = self.proxy_text()
        a.proxy_pass = self.ed_proxy_pass.text().strip()
        a.only_zips = []
        return a

    # ------------------------------------------------------------ 网络出口设置
    def proxy_text(self):
        return self.ed_proxy.toPlainText().strip()

    def load_net(self):
        """上次填的代理从库里读回来；读出来是空的就留空 = 走本机网络（不花钱）。"""
        try:
            con = pw_engine.db_handle(self.make_args())
        except Exception:
            return
        try:
            self.ed_proxy.setPlainText(pw_engine.get_setting(con, "proxy"))
            self.ed_proxy_pass.setText(pw_engine.get_setting(con, "proxy_pass"))
        finally:
            con.close()
        self.sync_exit_label()

    def save_net(self):
        try:
            con = pw_engine.db_handle(self.make_args())
        except Exception:
            return
        try:
            pw_engine.set_setting(con, "proxy", self.proxy_text())
            pw_engine.set_setting(con, "proxy_pass", self.ed_proxy_pass.text().strip())
        finally:
            con.close()

    def sync_exit_label(self):
        text = self.proxy_text()
        if not text:
            self.lbl_exit.setText("　本机直连（不花钱）")
            return
        try:
            n = len(pw_engine.parse_proxy_lines(text, self.ed_proxy_pass.text().strip()))
        except Exception:
            n = 0
        self.lbl_exit.setText("　代理解析失败" if not n else f"　代理 {n} 条出口，被挡就换下一条")

    def on_log(self, msg):
        self.txt.appendPlainText(f"[{datetime.now():%H:%M:%S}] {msg}")
        sb = self.txt.verticalScrollBar()
        sb.setValue(sb.maximum())

    def warn(self, text):
        QMessageBox.warning(self, "看一下", text)

    # ------------------------------------------------------------ 抓取
    def start_harvest(self, only_zips=None, title=None):
        if self.running:
            if self.worker and self.worker.is_alive():
                return self.warn("已经在跑了，先按停止。")
            # 线程已经没了却没收到「跑完」信号（异常退出被强杀之类）：先把按钮救回来
            self.finish("上一轮已经结束，可以重新开始。")
        try:
            a = self.make_args()
        except ValueError as e:
            return self.warn(str(e))
        if only_zips:
            a.only_zips = only_zips
            a.limit = 0
            a.random = False
        self.save_net()
        self.pool = pw_engine.Pool(a)
        self.pool.log_cb = lambda m: self.bridge.log.emit(m)
        self.total_hint = title or ""
        self.txt.clear()
        self.on_log(f"=== {a.label} 起跑：州[{a.states or '全美'}] "
                    f"价格 {a.pmin:,}"
                    f"{'-'+format(a.pmax, ',') if a.has_max else ' 以上'} "
                    f"并发 {a.workers} "
                    f"{'点名 '+str(len(a.only_zips))+' 个ZIP' if a.only_zips else ('本轮 '+str(a.limit)+' 个ZIP' if a.limit else '该州全部ZIP')} "
                    f"{'代理'+str(len(self.proxy_text().splitlines()))+'条' if a.proxy else '本机直连'} ===")
        self.lbl_dir.setText(f"数据目录：{a.out}")
        self.gen += 1
        self.running = True
        self.stopping = False
        self.hinted = False
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setText("停止")

        mygen = self.gen

        def go():
            try:
                self.pool.run()
                msg = "本轮跑完"
            except SystemExit as e:
                msg = f"起跑失败：{e}"
            except BaseException as e:
                msg = f"抓取异常退出：{type(e).__name__}: {e}"
            self.bridge.done.emit(f"{mygen}|{msg}")

        self.worker = threading.Thread(target=go, daemon=True)
        self.worker.start()

    def stop_harvest(self):
        if not self.running:
            return
        if not self.stopping:
            if self.pool:
                self.pool.stop.set()
            self.stopping = True
            self.stop_at = time.monotonic()
            self.btn_stop.setText("正在收尾…再点一下=不等了")
            self.on_log("收到停止：当前 ZIP 落库就停，已入库的地址不会丢，"
                        "下次点开始自动接着跑……")
            return
        # 收尾太慢（浏览器卡在页面里）时这条是他的出口：界面交还，按钮别锁死
        self.gen += 1
        self.running = self.stopping = False
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("停止")
        self.on_log("已不等了：旧浏览器可能还在后台收尾，想干净就手动关掉那个窗口。"
                    "「开始抓取」现在能点——跑过的 ZIP 会跳过、地址按规范化去重，"
                    "两边撞车也不会存出两份。")

    def on_done(self, payload):
        g, _, msg = str(payload).partition("|")
        if g.isdigit() and int(g) != self.gen:
            return
        self.finish(msg)

    def finish(self, msg):
        """把「开始」按钮交回来的唯一出口：正常跑完、报错、线程被杀都走这里。"""
        self.running = self.stopping = False
        self.on_log(msg)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("停止")
        self.lbl_prog.setText(msg)
        self.refresh_stock()
        self.refresh_failed()

    def poll_stats(self):
        if self.running and not (self.worker and self.worker.is_alive()):
            return self.finish("本轮已结束（线程退出）。")
        if self.stopping and not self.hinted and time.monotonic() - self.stop_at > 60:
            self.hinted = True            # 提示只说一次，别每秒钟刷屏
            self.on_log("收尾超过 60 秒：再点一下「不等了」那个按钮就能直接重新开始。")
        if not self.pool or not self.running:
            return
        s = self.pool.snapshot()
        mins = self.pool.elapsed()
        z = s["zip"]
        rate = z / mins * 60 if mins > 5 else 0
        self.lbl_prog.setText(
            f"ZIP {fmt(z)} 个跑完 / 请求 {fmt(s['req'])} 次 / 流量 {traffic(s['bytes']/1048576)} / "
            f"新增入库 {fmt(s['new'])}（符合 {fmt(s['keep'])}，剔空置 {fmt(s['vacant'])}） / "
            f"被挡 {s['blocked']} 异常 {s['err']} 截断 {s['trunc']} / "
            f"{rate:.0f} 个ZIP每分钟")

    # ------------------------------------------------------------ 库存/导出
    def refresh_stock(self):
        try:
            a = self.make_args()
        except ValueError:
            a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                       self.dmin, self.dmax).parse_args([])
        con = pw_engine.db_handle(a)
        tot = con.execute("SELECT COUNT(*) FROM emitted").fetchone()[0]
        rng = con.execute("SELECT MIN(value), MAX(value) FROM emitted").fetchone()
        un = con.execute("SELECT COUNT(*) FROM emitted WHERE batch IS NULL").fetchone()[0]
        ex = tot - un
        bystate = con.execute("SELECT state, COUNT(*) FROM emitted WHERE batch IS NULL "
                              "GROUP BY state ORDER BY 2 DESC LIMIT 12").fetchall()
        self.lbl_stock.setText(
            f"[{self.label}] 库存 {fmt(tot)} 条　未导出 {fmt(un)}　已导出 {fmt(ex)}"
            + (f"　估价 {fmt(rng[0])} ~ {fmt(rng[1])}" if tot else ""))
        self.txt_states = "　".join(f"{s or '?'}:{fmt(n)}" for s, n in bystate)
        rows = con.execute("SELECT id,name,states,n,file,ts FROM batches ORDER BY id").fetchall()
        self.tb_batch.setRowCount(len(rows))
        for i, (bid, name, states, n, fp, ts) in enumerate(rows):
            live = con.execute("SELECT COUNT(*) FROM emitted WHERE batch=?", (bid,)).fetchone()[0]
            for j, v in enumerate([bid, ts, states or "全美", n, live, fp]):
                it = QTableWidgetItem(fmt(v) if isinstance(v, int) else str(v))
                if j == 0:
                    it.setData(Qt.UserRole, bid)
                self.tb_batch.setItem(i, j, it)
        self.tb_batch.resizeColumnsToContents()
        con.close()

    def do_export(self):
        try:
            a = self.make_args()
        except ValueError as e:
            return self.warn(str(e))
        a.export = self.sp_exp.value()
        a.by = "价格" if self.cmb_by.currentIndex() == 1 else "随机"
        a.name = self.ed_name.text().strip()
        try:
            pw_engine.cmd_export(a)
        except SystemExit as e:
            return self.warn(str(e))
        self.on_log(f"导出 {a.export:,} 条完成，州[{a.states or '全美'}]")
        self.refresh_stock()

    def pick_batch(self):
        r = self.tb_batch.currentRow()
        if r < 0:
            self.warn("先在表里选一个批次。")
            return None
        return self.tb_batch.item(r, 0).data(Qt.UserRole)

    def recall_batch(self):
        bid = self.pick_batch()
        if bid is None:
            return
        a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                   self.dmin, self.dmax).parse_args([])
        a.recall_batch = bid
        pw_engine.cmd_recall(a)
        self.on_log(f"批次 {bid} 已放回池子")
        self.refresh_stock()

    def delete_batch(self):
        bid = self.pick_batch()
        if bid is None:
            return
        if self.ask_yes(f"要彻底删掉批次 {bid} 的地址？删了就回不来了（导出 TXT 文件本身不动）。"):
            a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                       self.dmin, self.dmax).parse_args([])
            a.delete_batch = bid
            a.yes = True
            pw_engine.cmd_delete(a)
            self.on_log(f"批次 {bid} 已删除")
            self.refresh_stock()

    def delete_exported(self):
        if self.ask_yes("要删掉所有「已导出」的地址？未导出的不动。"):
            a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                       self.dmin, self.dmax).parse_args([])
            a.delete_exported = True
            a.yes = True
            pw_engine.cmd_delete(a)
            self.on_log("已删掉全部已导出地址")
            self.refresh_stock()

    def ask_yes(self, text):
        return QMessageBox.question(self, "确认", text,
                                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def open_outdir(self):
        import subprocess
        d = Path(self.default_out)
        if getattr(sys, "frozen", False):
            d = Path(sys.executable).resolve().parent
        (d / "导出").mkdir(exist_ok=True)
        subprocess.Popen(["explorer", str(d / "导出")])

    # ------------------------------------------------------------ 失败台账
    def refresh_failed(self):
        a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                   self.dmin, self.dmax).parse_args([])
        con = pw_engine.db_handle(a)
        c = pw_engine.fail_counts(con)
        tot = sum(c.values())
        self.lbl_fail.setText(
            f"zip_progress 共 {fmt(tot)} 个　跑完正常 {fmt(c['ok'])}　"
            f"报错/被挡 {fmt(c['err'])}　没翻到底 {fmt(c['trunc'])}　查到 0 条 {fmt(c['empty'])}")
        rows = con.execute(
            f"SELECT zip, seen, rc, err, ts FROM zip_progress WHERE "
            f"{pw_engine.FAIL_CASE} != 'ok' ORDER BY ts DESC LIMIT 400").fetchall()
        self.tb_fail.setRowCount(len(rows))
        for i, (zp, seen, rc, err, ts) in enumerate(rows):
            kind = ("截断" if err.startswith("截断") else err if err else "0条")
            miss = (rc - (seen or 0)) if (rc and rc > (seen or 0)) else 0
            for j, v in enumerate([zp, seen or 0, rc if rc is not None else "?",
                                   miss or "", kind, ts]):
                self.tb_fail.setItem(i, j, QTableWidgetItem(fmt(v) if isinstance(v, int) else str(v)))
        self.tb_fail.resizeColumnsToContents()
        con.close()

    def retry_failed(self, kind):
        if self.running and self.worker and self.worker.is_alive():
            return self.warn("抓取正在跑，先停下再重跑失败的。")
        a = pw_engine.build_parser(self.label, self.default_out, self.has_max,
                                   self.dmin, self.dmax).parse_args([])
        con = pw_engine.db_handle(a)
        kinds = [k.strip() for k in (kind or "err,trunc,empty").split(",") if k.strip()]
        ph = ",".join("?" * len(kinds))
        zips = [r[0] for r in con.execute(
            f"SELECT zip FROM zip_progress WHERE {pw_engine.FAIL_CASE} IN ({ph})",
            kinds).fetchall()]
        con.close()
        if not zips:
            return self.on_log("这一类没有失败的 ZIP，不用重跑。")
        if not self.ask_yes(f"要重跑这 {len(zips):,} 个 ZIP？\n"
                            "（已入库的地址不会重复，池子里那份照旧）"):
            return
        self.start_harvest(only_zips=zips)


def run(label, default_out, has_max, dmin, dmax):
    pw_engine.fix_console()
    app = QApplication(sys.argv)
    w = Main(label, default_out, has_max, dmin, dmax)
    w.show()
    w.refresh_stock()
    w.refresh_failed()
    w.load_net()
    sys.exit(app.exec())
