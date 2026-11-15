# -*- coding: utf-8 -*-
"""TPS 抓取器 - 主界面"""
import sys
import os
import time
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# 打包(frozen)运行时：浏览器目录指向 EXE 同级的 ms-playwright（随包携带，目标电脑离线可用）
if getattr(sys, "frozen", False):
    _browsers = Path(sys.executable).resolve().parent / "ms-playwright"
    if _browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers)

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QTextEdit, QVBoxLayout, QWidget, QComboBox, QMenu
)

import engine_tps182 as eng_tps      # 核心一：TruePeopleSearch（1.8.2 逐字节逻辑，被拦要解码）
import engine_fps as eng_fps         # 核心二：FastPeopleSearch（个人页电话直出，解码费 0）

eng = eng_tps                         # 面板「查询核心」决定它指向哪个引擎


def pick_core(name):
    """按面板选择切换查询核心：fps=FastPeopleSearch，其它=TruePeopleSearch。"""
    global eng
    eng = eng_fps if str(name).lower().startswith("fps") else eng_tps
    eng.init_db()
    return eng


def core_name():
    return "fps" if eng is eng_fps else "tps"

BASE = Path(__file__).resolve().parent


class Bridge(QObject):
    log = Signal(str)
    refresh = Signal()
    ui = Signal(object)


def format_traffic(kb):
    """流量自动 KB/MB/GB 显示。"""
    try:
        kb = int(kb)
    except (TypeError, ValueError):
        return "0 KB"
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.2f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def mount(group, inner_layout):
    """把一段内容装进「可折叠」框里，并让那个勾选框真的能折叠。

    PySide6 6.12 实测：checkable 的 QGroupBox 取消勾选后子控件仍然可见、高度一点没变，
    也就是界面里那四个「配置/地址导入/控制/统计」的勾选框点了没反应。这里把内容装进一个
    容器 widget，勾选状态直接切容器的可见性，折叠才是真的。
    """
    holder = QWidget()
    holder.setLayout(inner_layout)
    box = QVBoxLayout(group)
    box.setContentsMargins(2, 0, 2, 2)
    box.addWidget(holder)
    holder.setVisible(group.isChecked())

    def _toggle(on):
        holder.setVisible(on)
        # 光 setVisible 不够：外层布局不会重算，框子会继续占着展开时的 208px 空白。
        # 实测补一句 updateGeometry() 后四个框才真降到 31px。
        group.updateGeometry()

    group.toggled.connect(_toggle)
    return holder


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        eng.init_db()
        self.setWindowTitle("TPS 抓取器 v3.1.0")
        # 默认尺寸跟着屏幕可用区走：以前写死 1400x900，在小屏上窗口比屏幕还高，
        # 日志和下面的按钮被顶到屏幕外，得拖半天。
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        self.resize(min(1240, avail.width() if avail else 1240),
                    min(700, (avail.height() - 20) if avail else 700))
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.refresh.connect(self.refresh)
        self.bridge.ui.connect(self._run_ui)
        self.running = False
        self._closing = False
        self.workers = []
        self.start_time = None
        self._concurrency = int(eng.get_setting("concurrency", 5))
        self.build()
        self.refresh()
        # 每 3 秒自动刷新统计（流量/完成数/速度等），运行时可见
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self.refresh)
        self._stats_timer.start(3000)
        self.log("就绪")

    def build(self):
        central = QWidget()
        self.setCentralWidget(central)

        # 主布局：水平分割器（左：配置/统计，右：日志）
        splitter = QSplitter(Qt.Horizontal)
        root = QHBoxLayout(central)
        root.addWidget(splitter)

        # ===== 左侧：配置 + 地址 + 控制 + 统计 =====
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        # 配置区（可折叠）
        config = QGroupBox("配置")
        config.setCheckable(True)
        config.setChecked(True)
        config_layout = QVBoxLayout()

        # 查询核心（网站）二选一
        core_row = QHBoxLayout()
        core_row.addWidget(QLabel("查询核心:"))
        self.core_combo = QComboBox()
        self.core_combo.addItem("TruePeopleSearch（1.8.2 原逻辑，被拦需解码）", "tps")
        self.core_combo.addItem("FastPeopleSearch（个人页直出号码，解码费 0）", "fps")
        self.core_combo.setCurrentIndex(1 if eng_tps.get_setting("core", "tps") == "fps" else 0)
        self.core_combo.currentIndexChanged.connect(self._apply_core)
        core_row.addWidget(self.core_combo, 1)
        config_layout.addLayout(core_row)
        # 两个核心的区别说明不占界面：鼠标停在上面那格才显示
        self.core_tips = {
            "tps": "TruePeopleSearch：详情页的验证码必须解码才能拿到号码（走验证码 Key，按次花钱）。",
            "fps": ("FastPeopleSearch：个人页电话直出，不需要解码 Key，解码费 0；遇到 Cloudflare 验证等它自己过"
                    "（实测有的 IP 几秒、有的要 2~3 分钟），压不住就自动换出口 IP。用真实 Chrome（nodriver）跑，"
                    "Playwright 版实测过不了。人数默认不封顶（0），翻到列表没人才停。"
                    "注意：勾掉「显示浏览器窗口」=无头，实测无头一直卡在 security challenge，"
                    "连换 3 个出口 IP 各等满 200 秒都是 0 张卡片（同地址有头 5.9 秒出卡），别关窗口。"),
        }

        # 代理
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(QLabel("代理模板:"))
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("http://user:pass@host:port（用户名带 {session} 可自动换IP）")
        self.proxy_input.setText(eng.get_setting("proxy", ""))
        proxy_row.addWidget(self.proxy_input, 1)
        proxy_row.addWidget(QLabel("密码:"))
        self.proxy_pass = QLineEdit()
        self.proxy_pass.setText(eng.get_setting("proxy_pass", ""))
        proxy_row.addWidget(self.proxy_pass)
        config_layout.addLayout(proxy_row)

        # 验证码 Key（仅在勾选“解验证码”时需要）
        capsolver_row = QHBoxLayout()
        capsolver_row.addWidget(QLabel("验证码 Key:"))
        self.capsolver_input = QLineEdit()
        self.capsolver_input.setPlaceholderText("2Captcha Key（留空则只换 IP 不解码）")
        self.capsolver_input.setText(eng.get_setting("capsolver", ""))
        capsolver_row.addWidget(self.capsolver_input, 1)
        config_layout.addLayout(capsolver_row)

        # 并发 + 重试 + 解验证码开关挤同一行；FPS 三项挤同一行；防睡眠 + 保存挤同一行
        # —— 以前一行只放两三个控件，界面被拖得比屏幕还高
        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(QLabel("并发:"))
        self.concurrency_input = QSpinBox()
        self.concurrency_input.setRange(1, 9999)
        self.concurrency_input.setValue(self._concurrency)
        self.concurrency_input.valueChanged.connect(self._set_concurrency)
        concurrency_row.addWidget(self.concurrency_input)
        concurrency_row.addWidget(QLabel("重试:"))
        self.retry_input = QSpinBox()
        self.retry_input.setRange(1, 20)
        self.retry_input.setValue(int(eng.get_setting("max_attempts", 12)))
        concurrency_row.addWidget(self.retry_input)
        self.solve_captcha_check = QCheckBox("解验证码")
        self.solve_captcha_check.setToolTip(
            "被拦时解验证码（默认开）。\n关＝只换 IP 不解码，TPS 核心大概率拿不到号码；FPS 核心用不上这个。")
        self.solve_captcha_check.setChecked(eng.get_setting("solve_captcha", "1") == "1")
        concurrency_row.addWidget(self.solve_captcha_check)
        concurrency_row.addStretch()
        config_layout.addLayout(concurrency_row)

        # FPS 抓取范围（只影响 FPS 核心）
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("人数:"))
        self.fps_persons_input = QSpinBox()
        self.fps_persons_input.setRange(0, 500)
        self.fps_persons_input.setValue(int(eng.get_setting("fps_max_persons", "0")))
        self.fps_persons_input.setToolTip("0 = 不封顶，翻页翻到没人为止（抓全面）")
        fps_row.addWidget(self.fps_persons_input)
        fps_row.addWidget(QLabel("页数:"))
        self.fps_pages_input = QSpinBox()
        self.fps_pages_input.setRange(1, 20)
        self.fps_pages_input.setValue(int(eng.get_setting("fps_max_pages", "10")))
        fps_row.addWidget(self.fps_pages_input)
        self.fps_window_check = QCheckBox("显示窗口")
        self.fps_window_check.setToolTip(
            "勾上＝FPS 用真实 Chrome 有头跑（实测有头 5.9 秒出卡片）。\n"
            "不勾＝无头，实测无头一直卡在 security challenge，连换 3 个出口 IP 各等满 200 秒都是 0 张卡片，别关。")
        self.fps_window_check.setChecked(eng.get_setting("fps_headless", "0") != "1")
        fps_row.addWidget(self.fps_window_check)
        self.fps_hide_check = QCheckBox("挪出屏幕")
        self.fps_hide_check.setToolTip(
            "窗口照开、只是挪到屏幕外面看不见 —— 不是无头，挑战照样自己过。\n"
            "一个车道一个 Chrome 窗口，实测每个吃 1.2~1.7GB 内存；并发开多了屏幕被占满、机器发飘，"
            "就勾这个。并发数自己往下调才是真解决。")
        self.fps_hide_check.setChecked(eng.get_setting("fps_hide_window", "0") == "1")
        fps_row.addWidget(self.fps_hide_check)
        fps_row.addStretch()
        config_layout.addLayout(fps_row)
        self.fps_inputs = (self.fps_persons_input, self.fps_pages_input,
                           self.fps_window_check, self.fps_hide_check)

        # 防睡眠 + 保存
        sleep_row = QHBoxLayout()
        sleep_row.addWidget(QLabel("防睡眠:"))
        self.prevent_sleep_check = QCheckBox("保持唤醒")
        self.prevent_sleep_check.setChecked(eng.get_setting("prevent_sleep", "0") == "1")
        self.prevent_sleep_check.stateChanged.connect(self.toggle_sleep_prevention)
        sleep_row.addWidget(self.prevent_sleep_check)
        self.sleep_timer_input = QSpinBox()
        self.sleep_timer_input.setRange(0, 168)
        self.sleep_timer_input.setValue(int(eng.get_setting("sleep_timer_hours", "0")))
        self.sleep_timer_input.valueChanged.connect(self.save_config)
        self.sleep_timer_input.setToolTip("定时（小时）：0 = 不定时，一直唤醒到手动关")
        sleep_row.addWidget(self.sleep_timer_input)
        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(self.save_config)
        sleep_row.addWidget(save_btn)
        config_layout.addLayout(sleep_row)

        mount(config, config_layout)
        left_layout.addWidget(config)

        # 地址导入（默认收起：只在导那一次才用，展开着白占 200 多 px）
        import_group = QGroupBox("地址导入")
        import_group.setCheckable(True)
        import_group.setChecked(False)
        import_layout = QVBoxLayout()
        self.address_input = QPlainTextEdit()
        self.address_input.setPlaceholderText("每行一个地址 URL 或原始地址...")
        self.address_input.setMaximumHeight(90)
        import_layout.addWidget(self.address_input)

        import_btn_row = QHBoxLayout()
        import_btn = QPushButton("导入粘贴框")
        import_btn.clicked.connect(self.import_paste)
        import_btn_row.addWidget(import_btn)

        file_btn = QPushButton("从文件导入")
        file_btn.clicked.connect(self.import_file)
        import_btn_row.addWidget(file_btn)
        import_btn_row.addStretch()
        import_layout.addLayout(import_btn_row)
        mount(import_group, import_layout)
        left_layout.addWidget(import_group)

        # 控制区（可折叠）—— 8 个按钮分两行，一行摆 8 个会把左栏顶到 760px 以上
        control = QGroupBox("控制")
        control.setCheckable(True)
        control.setChecked(True)
        control_layout = QVBoxLayout()
        run_row = QHBoxLayout()
        data_row = QHBoxLayout()

        self.start_btn = QPushButton("▶ 开始")
        self.start_btn.clicked.connect(self.start)
        run_row.addWidget(self.start_btn)

        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.clicked.connect(self.pause)
        self.pause_btn.setEnabled(False)
        run_row.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        run_row.addWidget(self.stop_btn)

        export_btn = QPushButton("导出 CSV")
        export_btn.clicked.connect(self.export)
        run_row.addWidget(export_btn)

        self.addr_btn = QPushButton("导出地址 ▾")
        menu = QMenu(self.addr_btn)
        for label, statuses in (("未使用（没跑过的）", ("pending", "running")),
                                ("失败（换 IP 可救）", ("failed",)),
                                ("已完成", ("done",))):
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, s=statuses, t=label: self.export_addrs(s, t))
        self.addr_btn.setMenu(menu)
        data_row.addWidget(self.addr_btn)

        clear_btn = QPushButton("清除已导出数据")
        clear_btn.clicked.connect(self.clear_data)
        data_row.addWidget(clear_btn)

        retry_btn = QPushButton("重试失败")
        retry_btn.clicked.connect(self.retry_failed)
        data_row.addWidget(retry_btn)

        del_btn = QPushButton("删除成功")
        del_btn.clicked.connect(self.delete_done)
        data_row.addWidget(del_btn)

        data_row.addStretch()
        control_layout.addLayout(run_row)
        control_layout.addLayout(data_row)
        mount(control, control_layout)
        left_layout.addWidget(control)

        # 统计（可折叠）—— 一行 3 组：摆 4 组会把左栏顶到 700px 以上，宽度比高度更金贵
        stats_group = QGroupBox("统计")
        stats_group.setCheckable(True)
        stats_group.setChecked(True)
        stats_layout = QGridLayout()
        stats_layout.setHorizontalSpacing(6)
        self.stats_labels = {}
        bold = "font-weight:bold;"
        for i, (key, label) in enumerate([
            ("pending", "待处理"),
            ("running", "运行中"),
            ("done", "已完成"),
            ("failed", "失败"),
            ("results", "人数"),
            ("deliver", "导出条数"),
            ("phones", "全部号码"),
            ("traffic_kb", "流量"),
            ("runtime", "运行时长"),
            ("hourly", "每小时"),
            ("speed", "导出速度"),
        ]):
            r, c = divmod(i, 3)
            stats_layout.addWidget(QLabel(f"{label}:"), r, c * 2)
            lbl = QLabel("0")
            lbl.setStyleSheet(bold)
            stats_layout.addWidget(lbl, r, c * 2 + 1)
            self.stats_labels[key] = lbl
        stats_layout.setColumnStretch(6, 1)
        mount(stats_group, stats_layout)
        left_layout.addWidget(stats_group)

        # 末尾这条必须给 stretch=1：全是 0 时 Qt 把多余高度平均分给每个框，
        # 取消勾选后框里的内容虽然藏了，框本身还占着 200 多 px 空白。
        left_layout.addStretch(1)

        # 左栏定宽 + 可滚动：以前窗口一最大化，配置那几栏跟着被拉到 1900px 宽，
        # 一行说明直接被屏幕切掉；现在左栏最宽 680，窗口再宽也是日志吃多出来的空间。
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(430)
        left_scroll.setMaximumWidth(680)
        splitter.addWidget(left_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # ===== 右侧：日志 =====
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout()
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        log_layout.addWidget(self.log_box)

        clear_log_btn = QPushButton("清除日志")
        clear_log_btn.clicked.connect(self.clear_log)
        clear_log_btn.setMaximumWidth(100)
        log_layout.addWidget(clear_log_btn, alignment=Qt.AlignRight)

        log_group.setLayout(log_layout)
        right_layout.addWidget(log_group)

        splitter.addWidget(right_widget)

        # 设置初始分割比例（左60% 右40%）
        splitter.setSizes([840, 560])
        self._apply_core()

    def log(self, msg):
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_box.append(f"[{timestamp}] {msg}")
            # 日志最多保留 200 行，超了自动删旧的
            doc = self.log_box.document()
            if doc.blockCount() > 200:
                cursor = self.log_box.textCursor()
                cursor.movePosition(cursor.Start)
                for _ in range(doc.blockCount() - 200):
                    cursor.movePosition(cursor.Down, cursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.deleteChar()
        except RuntimeError:
            pass

    def clear_log(self):
        """手动清除日志"""
        self.log_box.clear()

    def _run_ui(self, fn):
        try:
            fn()
        except RuntimeError:
            pass

    def _set_concurrency(self, value):
        """并发数变更（UI 线程），供后台调度线程安全读取。"""
        self._concurrency = int(value)

    def _apply_core(self, *_):
        """面板切核心：换查询网站，同时提醒各自的成本口径。"""
        name = self.core_combo.currentData()
        eng.set_setting("core", name)
        pick_core(name)
        self.core_combo.setToolTip(self.core_tips.get(name, ""))
        if name == "fps":
            self.solve_captcha_check.setEnabled(False)
        else:
            self.solve_captcha_check.setEnabled(True)
        for wid in getattr(self, "fps_inputs", ()):
            wid.setEnabled(name == "fps")
        self.refresh()

    def save_config(self):
        self._apply_core()
        eng.set_setting("proxy", self.proxy_input.text().strip())
        eng.set_setting("proxy_pass", self.proxy_pass.text().strip())
        eng.set_setting("capsolver", self.capsolver_input.text().strip())
        eng.set_setting("concurrency", self.concurrency_input.value())
        eng.set_setting("max_attempts", self.retry_input.value())
        eng.set_setting("solve_captcha", "1" if self.solve_captcha_check.isChecked() else "0")
        eng.set_setting("fps_max_persons", self.fps_persons_input.value())
        eng.set_setting("fps_max_pages", self.fps_pages_input.value())
        eng.set_setting("fps_headless", "0" if self.fps_window_check.isChecked() else "1")
        eng.set_setting("fps_hide_window", "1" if self.fps_hide_check.isChecked() else "0")
        eng.set_setting("prevent_sleep", "1" if self.prevent_sleep_check.isChecked() else "0")
        eng.set_setting("sleep_timer_hours", self.sleep_timer_input.value())
        self.log("配置已保存")
        if self.sender() and isinstance(self.sender(), QPushButton):
            QMessageBox.information(self, "保存", "配置已保存")

    def toggle_sleep_prevention(self, state):
        """切换防睡眠状态"""
        if state == Qt.Checked:
            self._prevent_sleep()
            hours = self.sleep_timer_input.value()
            if hours > 0:
                self._sleep_timer = threading.Timer(hours * 3600, self._allow_sleep)
                self._sleep_timer.daemon = True
                self._sleep_timer.start()
                self.log(f"防睡眠已启用，{hours}小时后自动允许睡眠")
            else:
                self.log("防睡眠已启用（无定时）")
        else:
            self._allow_sleep()
            if hasattr(self, '_sleep_timer'):
                self._sleep_timer.cancel()
            self.log("防睡眠已关闭")

    def _prevent_sleep(self):
        """阻止系统睡眠"""
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000002)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        except Exception as e:
            self.log(f"防睡眠设置失败: {e}")

    def _allow_sleep(self):
        """允许系统睡眠"""
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only
        except Exception:
            pass

    def import_paste(self):
        lines = self.address_input.toPlainText().splitlines()
        added = eng.import_addresses(lines)
        self.log(f"导入 {added} 条地址")
        self.address_input.clear()
        self.refresh()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", str(BASE), "文本文件 (*.txt *.csv)")
        if not path:
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        added = eng.import_addresses(lines)
        self.log(f"从文件导入 {added} 条地址")
        self.refresh()

    def start(self):
        # 开跑前按面板重绑一次核心，后台线程提交的就是这个 eng
        pick_core(self.core_combo.currentData())
        proxy = self.proxy_input.text().strip()
        proxy_pass = self.proxy_pass.text().strip()
        capsolver = self.capsolver_input.text().strip()
        solve_captcha = self.solve_captcha_check.isChecked()
        concurrency = self.concurrency_input.value()

        if not proxy or not proxy_pass:
            QMessageBox.warning(self, "配置不完整", "请填写代理模板和密码")
            return
        if solve_captcha and not capsolver:
            QMessageBox.warning(self, "配置不完整", "勾选了“解验证码”但未填写验证码 Key")
            return

        eng.set_setting("proxy", proxy)
        eng.set_setting("proxy_pass", proxy_pass)
        eng.set_setting("capsolver", capsolver)
        eng.set_setting("concurrency", concurrency)
        eng.set_setting("solve_captcha", "1" if solve_captcha else "0")
        max_attempts = self.retry_input.value()
        eng.set_setting("max_attempts", max_attempts)
        # FPS 那几格引擎是直接从库里读的，不点「保存配置」也得按面板走
        eng.set_setting("fps_max_persons", self.fps_persons_input.value())
        eng.set_setting("fps_max_pages", self.fps_pages_input.value())
        eng.set_setting("fps_headless", "0" if self.fps_window_check.isChecked() else "1")
        eng.set_setting("fps_hide_window", "1" if self.fps_hide_check.isChecked() else "0")

        self.running = True
        self.start_time = time.time()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.log(f"启动，并发 {concurrency}，解验证码={'开' if solve_captcha else '关（被拦换IP）'}")

        def work():
            try:
                with eng.connect() as con:
                    addresses = [row["address"] for row in
                                 con.execute("SELECT address FROM addresses WHERE status='pending' LIMIT 1000").fetchall()]

                if not addresses:
                    self.bridge.log.emit("没有待处理地址")
                    return

                self.bridge.log.emit(f"开始处理 {len(addresses)} 个地址...")

                def on_progress(msg):
                    self.bridge.log.emit(msg)

                # 队列式调度：只保持 _concurrency 个在跑，完成一个补一个（支持暂停）
                with ThreadPoolExecutor(max_workers=9999) as executor:
                    futures = {}
                    idx = 0
                    queue = list(addresses)

                    while queue or futures:
                        while self.running and len(futures) < max(1, self._concurrency) and queue:
                            addr = queue.pop(0)
                            future = executor.submit(
                                eng.process_address, addr, proxy, proxy_pass, capsolver,
                                worker_id=idx, progress_cb=on_progress,
                                max_attempts=max_attempts, solve_captcha=solve_captcha)
                            futures[future] = addr
                            idx += 1

                        if not futures:
                            break

                        done_futures = [f for f in futures if f.done()]
                        if not done_futures:
                            time.sleep(0.5)
                            continue

                        for future in done_futures:
                            url = futures.pop(future)
                            try:
                                success, data = future.result()
                                if success:
                                    self.bridge.log.emit(
                                        f"✓ {url[:34]}：入库{data['people']}人 / 出表{data['people']}条"
                                        f"（每人1个常用号）/ 库内全部号{data['phones']} / "
                                        f"{format_traffic(data.get('traffic_kb', 0))}")
                                else:
                                    self.bridge.log.emit(
                                        f"✗ {url[:34]}：{data.get('error') or '失败'}（{data.get('title', '')[:60]}）")
                            except Exception as e:
                                self.bridge.log.emit(f"错误 {url[:34]}：{e}")
                            self.bridge.refresh.emit()

                    if not self.running:
                        self.bridge.log.emit("已暂停（当前任务已完成，点击开始继续）")
                    else:
                        self.bridge.log.emit("完成")

            except Exception as e:
                self.bridge.log.emit(f"启动失败: {e}")
            finally:
                self.running = False
                self.bridge.ui.emit(lambda: self.start_btn.setEnabled(True))
                self.bridge.ui.emit(lambda: self.pause_btn.setEnabled(False))
                self.bridge.ui.emit(lambda: self.stop_btn.setEnabled(False))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.running = False
        self.log("请求停止...")

    def pause(self):
        """暂停：跑完当前地址后不再接新地址，可继续。"""
        self.running = False
        self.log("请求暂停（跑完当前地址后停止派发）...")

    def closeEvent(self, ev):
        """关窗口 = 程序真要走，得自己把浏览器收干净。
        少了这一步：工作线程是 daemon，解释器收尾时它们正卡在浏览器里，engine 那层的
        finally 根本跑不到 —— 11-16 实测他关掉界面之后还有 84 个 chrome.exe / 5.9GB
        挂在程序进程底下，整机卡死就是这么来的。
        只按父子关系收自己名下的浏览器；他自己双击开的 Chrome 挂在 explorer 底下，碰不到。"""
        if self._closing:
            ev.accept()
            return
        self._closing = True
        self.running = False
        self._stats_timer.stop()
        eng_fps.begin_shutdown()
        self._allow_sleep()
        killed = eng_fps.kill_browser_tree(os.getpid())
        if killed:
            self.log(f"关闭：收掉 {killed} 个浏览器进程")
        ev.accept()

    def clear_data(self):
        """清除已导出的结果数据（地址保留）。"""
        reply = QMessageBox.question(
            self, "确认清除",
            "确定要清除所有已采集的结果数据吗？\n（地址列表保留，仅清除结果）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        with eng.connect() as con:
            deleted = con.execute("DELETE FROM results").rowcount
            con.execute("UPDATE addresses SET status='pending',records=0,people=0,phones=0,traffic_kb=0,error='' WHERE status='done'")
        self.log(f"已清除 {deleted} 条结果，地址状态已重置")
        QMessageBox.information(self, "清除", f"已清除 {deleted} 条结果")
        self.refresh()

    def retry_failed(self):
        """把失败的地址重置为待处理，排到队尾重新跑。"""
        with eng.connect() as con:
            count = con.execute("UPDATE addresses SET status='pending', error='' WHERE status='failed'").rowcount
        self.log(f"已重置 {count} 个失败地址为待处理")
        QMessageBox.information(self, "重试", f"{count} 个失败地址已排到队尾")
        self.refresh()

    def delete_done(self):
        """删除已成功采集的地址（结果保留在 CSV 里）。"""
        reply = QMessageBox.question(
            self, "确认删除",
            "确定要删除所有已完成的地址吗？\n（结果数据保留，仅删除地址记录）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        with eng.connect() as con:
            count = con.execute("DELETE FROM addresses WHERE status='done'").rowcount
        self.log(f"已删除 {count} 个已完成地址")
        QMessageBox.information(self, "删除", f"已删除 {count} 个地址")
        self.refresh()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出", str(BASE), "CSV (*.csv)")
        if not path:
            return
        out, count = eng.export_csv(path)
        self.log(f"导出 {count} 条 → {out}")
        QMessageBox.information(self, "导出", f"已导出 {count} 条")

    def export_addrs(self, statuses, label):
        path, _ = QFileDialog.getSaveFileName(self, f"导出{label}地址", str(BASE), "Text (*.txt)")
        if not path:
            return
        out, count = eng.export_addresses(statuses, path)
        self.log(f"导出{label}地址 {count} 条 → {out}")
        QMessageBox.information(self, "导出", f"{label} {count} 条地址")

    def refresh(self):
        s = eng.stats()
        for key, lbl in self.stats_labels.items():
            if key in ("runtime", "hourly", "speed"):
                continue
            val = s.get(key, 0)
            lbl.setText(format_traffic(val) if key == "traffic_kb" else str(val))
        # 运行时长 / 每小时速度 / 导出速度
        if self.start_time:
            elapsed = time.time() - self.start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            sec = int(elapsed % 60)
            self.stats_labels["runtime"].setText(f"{h:02d}:{m:02d}:{sec:02d}")
            hours = elapsed / 3600
            minutes = elapsed / 60
            self.stats_labels["hourly"].setText(
                f"{s.get('done', 0) / hours:.0f} 地址/时" if hours > 0 else "0 地址/时")
            self.stats_labels["speed"].setText(
                f"{s.get('deliver', 0) / minutes:.1f} 条/分" if minutes > 0 else "0 条/分")
        elif not self.running:
            self.stats_labels["runtime"].setText("00:00:00")
            self.stats_labels["hourly"].setText("0 地址/时")
            self.stats_labels["speed"].setText("0 条/分")


def _selftest():
    """打包自检：TPS抓取器.exe --selftest → 跑一个真实地址，结果写 selftest_result.txt（验证捆绑的浏览器/驱动可用）。"""
    import traceback
    lines = []
    try:
        eng.init_db()
        addr = "26 Helena Ave, Bethpage, NY 11714"
        eng.import_addresses([addr])
        with eng.connect() as con:
            con.execute("UPDATE addresses SET status='pending' WHERE address=?", (addr,))
        proxy = eng.get_setting("proxy", "")
        pw = eng.get_setting("proxy_pass", "")
        cap = eng.get_setting("capsolver", "")
        lines.append(f" browsers_path={os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '(default)')}")
        ok, data = eng.process_address(addr, proxy, pw, cap, worker_id=0,
                                       max_pages=2, max_attempts=5,
                                       progress_cb=lambda m: lines.append(str(m)))
        lines.append(f"RESULT ok={ok} data={data}")
        lines.append("SELFTEST_OK" if ok else "SELFTEST_FAIL")
    except Exception:
        lines.append(traceback.format_exc())
        lines.append("SELFTEST_ERROR")
    (eng.BASE / "selftest_result.txt").write_text("\n".join(lines), encoding="utf-8")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    app = QApplication(sys.argv)
    win = Main()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
