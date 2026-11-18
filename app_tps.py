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
from PySide6.QtGui import QFont, QTextCursor
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

UI_LOG_LINES = 200           # 屏上日志框最多只显最近 200 行（多了自动收掉旧的，省内存也防越滚越长）
PULL_BATCH = 200             # 每轮从库里捞一批待处理地址；捞完接着捞，绝不「取满 1000 就收摊」
POLL_IDLE_S = 0.5            # 调度线程空转一次的间隔（没有地址完成时）


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
        self.setWindowTitle("TPS 抓取器 v3.1.1")
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
        self._stop_kind = ""            # "" / "pause" / "stop"：结束那句要照这个说清原因
        self._concurrency = int(eng.get_setting("concurrency", 5))
        # 车道号池：并发开几副浏览器就有 0..N-1 几个号，一个号同一时刻只挂一条地址，
        # 这样日志里的 [W3] 才是「第三条车道」，而不是「第 397 条地址」。
        self._lane_lock = threading.Lock()
        self._lane_busy = set()
        # 下面两个标志是给调度线程读的快照（界面线程写、抓取线程只读）：
        # 「解验证码」「日志写出口IP」必须在跑动中一改，下一条地址立刻按新的走 ——
        # 以前这两个只在点「开始」那一刻取一次，跑到一半取消勾选等于没取消（照旧花钱）。
        self._solve_captcha = eng.get_setting("solve_captcha", "1") == "1"
        self._log_egress = eng.get_setting("log_egress_ip", "1") == "1"
        self.build()
        # 勾选框一点就落库、立刻生效。以前只有按「保存配置」或点「开始」才写库，
        # 于是取消勾选当场不顶用、下次开机又给它勾回来了。
        self.solve_captcha_check.stateChanged.connect(self._on_solve_toggled)
        self.egress_check.stateChanged.connect(self._on_egress_toggled)
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
        # 出口 IP 落日志：每条车道一开起来就写明「现在这个出口是哪个 IP、落在哪个国家/州」，
        # 排查「这台机器打不开页」到底怪线路还是怪本机就看这一行。归属地接口 ip-api 免费档
        # 限 ~45 次/分，所以查过的进 ip_geo 表缓存（7 天）并按 /24 复用。
        self.egress_check = QCheckBox("日志写出口IP")
        self.egress_check.setToolTip(
            "每副浏览器一开起来，就在它自己的代理隧道里读一次出口 IP，并查它落在哪个国家/州，"
            "写进行首 [W3] 出口 …\n"
            "只能读浏览器隧道里的 IP —— 本机直连读到的永远是这台电脑自己的出口，"
            "看不到代理按 session 绑住的那个。\n"
            "代理栏留空（直连）时不找隧道，直接写本机出口＋本机归属地，一个额外标签页都不开。\n"
            "取不到只写「没读到」，不影响抓取；探活有硬上限，实测正常 1.4~2.1 秒一条车道，"
            "出口死口时最多 6 秒（11-18 六地址对撞：31 人/296 号与关掉时一字不差）。\n"
            "关了就一个额外请求都不发、一个额外标签页都不开。改这一勾不用重启，下一条地址就变。")
        self.egress_check.setChecked(eng.get_setting("log_egress_ip", "1") == "1")
        concurrency_row.addWidget(self.egress_check)
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
        line = ""
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            line = f"[{timestamp}] {msg}"
            self.log_box.append(line)
            self._trim_log_box()
        except Exception as e:
            # 屏上出问题时也别把抓取线程带崩一句异常都不能往外抛
            try:
                self._trim_warned = True
                self.log_box.append(f"[界面日志异常] {type(e).__name__}: {e}"
                                    f"（这句本来要写：{str(line or msg)[:80]}）")
            except Exception:
                pass

    def _trim_log_box(self):
        """屏上只留最近 UI_LOG_LINES 行，超了的旧行自动收掉。

        为什么单独写一个方法、而且枚举全都要写全限定：
        PySide6 6.12 实测 QTextCursor 的*实例*上拿不到 cursor.Start / cursor.Down / cursor.KeepAnchor
        这种简写（只有类上有），底版那句 `cursor.movePosition(cursor.Start)` 一超 200 行就抛
        AttributeError('QTextCursor' object has no attribute 'Start')，异常被 log() 里的
        `except RuntimeError` 漏掉、截行整段作废 —— 屏上日志就是这么一路滚到几千行的。
        另外末尾那句 deleteChar() 会把留下那行的首字符吃掉，也不能留。"""
        doc = self.log_box.document()
        extra = doc.blockCount() - UI_LOG_LINES
        if extra <= 0:
            return
        cursor = self.log_box.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(QTextCursor.MoveOperation.Down,
                            QTextCursor.MoveMode.KeepAnchor, extra)
        cursor.removeSelectedText()
        # 文档末尾那个空块算不算一行，Qt 有时给有时不给，补几刀截到位为止
        for _ in range(4):
            if doc.blockCount() <= UI_LOG_LINES:
                break
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down,
                                QTextCursor.MoveMode.KeepAnchor, 1)
            cursor.removeSelectedText()

    def clear_log(self):
        """手动清除日志"""
        self.log_box.clear()

    def _on_solve_toggled(self, _state):
        """「解验证码」一改就落库、就生效：跑动中取消勾选，下一条地址起一分钱解码费都不花。"""
        on = bool(self.solve_captcha_check.isChecked())
        self._solve_captcha = on
        eng.set_setting("solve_captcha", "1" if on else "0")
        core = self.core_combo.currentData()
        self.log(f"解验证码已改为：{'开（被拦就花钱解码）' if on else '关（被拦只换 IP，解码费 0）'}"
                 f"｜当前核心={core.upper()}｜下一条地址起生效")

    def _on_egress_toggled(self, _state):
        """「日志写出口IP」同理：引擎每次起浏览器都现读这个键，改完不用重启。"""
        on = bool(self.egress_check.isChecked())
        self._log_egress = on
        eng.set_setting("log_egress_ip", "1" if on else "0")
        self.log(f"出口IP日志已改为：{'开（每车道一条）' if on else '关（0 额外请求、0 额外标签页）'}")

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
        eng.set_setting("log_egress_ip", "1" if self.egress_check.isChecked() else "0")
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

    # ---------------------------------------------------------------- 调度用的库口子
    # 这几条都是「一次一小步」的读写：调度线程一边跑一边回库里捞，绝不一次性把列表读死。
    def _pull_size(self):
        """一批只捞「并发×2」条，不是一口气把整库待处理全占住。
        占位标太狠有两个后果（11-19 早上他这场就是）：屏上「运行中」变成排队数
        （94 条里 92 条压根没派发，关窗口时整批退回待处理），而且这一批排空之前
        新导入的、点「重试失败」捡回来的地址都进不了队。"""
        try:
            n = int(self._concurrency) * 2
        except (TypeError, ValueError):
            n = PULL_BATCH
        return max(2, min(PULL_BATCH, n))

    def _claim_pending(self, n):
        """从库里取 n 条待处理并当场标成「运行中」占住。
        为什么要占位：批批回库捞时，正在跑的那几条必须别再被下一批捞回去重复跑
        （重复跑＝同样的地址再花一遍解码费）。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with eng.connect() as con:
            rows = con.execute(
                "SELECT address FROM addresses WHERE status='pending' ORDER BY id LIMIT ?",
                (n,)).fetchall()
            addrs = [r["address"] for r in rows]
            if addrs:
                con.executemany("UPDATE addresses SET status='running', updated_at=? WHERE address=?",
                                [(ts, a) for a in addrs])
        return addrs

    def _release_pending(self, addrs):
        """暂停/停止时把还没派出去的那批放回待处理（它们只是被占住，一条都没跑）。"""
        if not addrs:
            return 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with eng.connect() as con:
            cur = con.executemany(
                "UPDATE addresses SET status='pending', updated_at=? "
                "WHERE address=? AND status='running'", [(ts, a) for a in addrs])
        return len(addrs)

    def _pending_left(self):
        try:
            with eng.connect() as con:
                return int(con.execute(
                    "SELECT COUNT(*) c FROM addresses WHERE status IN ('pending','running')"
                ).fetchone()["c"])
        except Exception:
            return -1

    def _recover_running(self):
        """把上一轮遗留的「运行中」捡回「待处理」。只在本轮还没派发任何东西时调用。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with eng.connect() as con:
                return con.execute(
                    "UPDATE addresses SET status='pending', updated_at=? WHERE status='running'",
                    (ts,)).rowcount
        except Exception:
            return 0

    def _mark_failed(self, address, err):
        """一条地址抛异常也要给它落个状态，绝不允许它永远挂在「运行中」。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with eng.connect() as con:
                con.execute("UPDATE addresses SET status='failed', error=?, updated_at=?"
                            " WHERE address=? AND status IN ('running','pending')",
                            (str(err)[:180], ts, address))
        except Exception:
            pass

    def _take_lane(self):
        """领一个空车道号（0 起）。日志里的 [W3] 从此就是「第三条车道」，不再是一条地址一个号。"""
        with self._lane_lock:
            i = 0
            while i in self._lane_busy:
                i += 1
            self._lane_busy.add(i)
            return i

    def _free_lane(self, lane):
        with self._lane_lock:
            self._lane_busy.discard(lane)

    def start(self):
        # 开跑前按面板重绑一次核心，后台线程提交的就是这个 eng
        pick_core(self.core_combo.currentData())
        proxy = self.proxy_input.text().strip()
        proxy_pass = self.proxy_pass.text().strip()
        capsolver = self.capsolver_input.text().strip()
        solve_captcha = self.solve_captcha_check.isChecked()
        concurrency = self.concurrency_input.value()
        self._solve_captcha = solve_captcha          # 开跑这一瞬间的对平（之后每次派发现读）
        self._log_egress = self.egress_check.isChecked()

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
        eng.set_setting("log_egress_ip", "1" if self._log_egress else "0")
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
        self.log(f"启动，并发 {concurrency}，解验证码={'开（花钱解码）' if solve_captcha else '关（被拦换IP，解码费0）'}，"
                 f"出口IP日志={'开' if self._log_egress else '关'}")

        def work():
            dispatched = 0
            done_n = fail_n = err_n = 0
            reason = ""
            queue = []
            futures = {}
            try:
                # 上次崩/被强杀时停在「运行中」的地址先捡回来 —— 不然界面一打开
                # 统计里那几条永远挂着「运行中」，而待处理=0，点开始只会回一句「没有待处理地址」，
                # 看上去就像程序自己停了。
                recovered = self._recover_running()
                if recovered:
                    self.bridge.log.emit(f"捡回上次卡在「运行中」的地址 {recovered} 条，重新排队")

                def on_progress(msg):
                    self.bridge.log.emit(msg)

                with ThreadPoolExecutor(max_workers=9999) as executor:
                    while True:
                        if not self.running:
                            reason = ("已暂停（跑完当前这批就停，点「开始」接着跑）"
                                      if self._stop_kind == "pause" else
                                      "已停止（点了停止，在跑的收尾，没跑的留在库里）")
                            break

                        # 队列空了就回库里再捞一批 —— 这就是「会自动停止」的根：
                        # 底版只在开跑时读一次 `status='pending' LIMIT 1000`，
                        # 地址超过 1000 条时后面的这条根本没人管，跑满 1000 就报「完成」。
                        if not queue:
                            queue.extend(self._claim_pending(self._pull_size()))

                        while self.running and len(futures) < max(1, self._concurrency) and queue:
                            addr = queue.pop(0)
                            lane = self._take_lane()
                            solve = bool(self._solve_captcha)      # 跑动中改勾选，下一条就按新的走
                            try:
                                fut = executor.submit(
                                    eng.process_address, addr, proxy, proxy_pass, capsolver,
                                    worker_id=lane, progress_cb=on_progress,
                                    max_attempts=max_attempts, solve_captcha=solve)
                            except Exception as e:
                                # submit 都能炸（线程池关了、内存枯竭），一条炸不许带崩整轮
                                self._free_lane(lane)
                                err_n += 1
                                self._mark_failed(addr, f"派发失败 {type(e).__name__}: {e}"[:120])
                                self.bridge.log.emit(f"⚠ 派发失败 {addr[:34]}：{type(e).__name__}: {e}"[:150])
                                continue
                            futures[fut] = (addr, lane, solve)
                            dispatched += 1

                        if not futures and not queue:
                            left = self._pending_left()
                            if left:
                                reason = (f"停下了（库里还剩 {left} 条待处理，却一条都没捞到 —— "
                                          f"这是异常，不是跑完）")
                            else:
                                reason = "跑完了（库里已无待处理地址）"
                            break

                        finished = [f for f in futures if f.done()]
                        if not finished:
                            time.sleep(POLL_IDLE_S)
                            continue

                        for fut in finished:
                            url, lane, solve = futures.pop(fut)
                            self._free_lane(lane)
                            try:
                                success, data = fut.result()
                            except Exception as e:
                                # 单条地址把整轮带崩是底版最要命的毛病：引擎在 finally 之外抛出来时
                                # 那条地址还挂在「运行中」，这里必须给它一个交代（落 failed），
                                # 然后继续跑下一条。
                                err_n += 1
                                self._mark_failed(url, f"异常 {type(e).__name__}: {e}"[:120])
                                self.bridge.log.emit(f"错误 {url[:34]}：{type(e).__name__}: {e}"[:150])
                                self.bridge.refresh.emit()
                                continue
                            if success:
                                done_n += 1
                                self.bridge.log.emit(
                                    f"✓ [W{lane}] {url[:34]}：入库{data['people']}人 / 出表{data['people']}条"
                                    f"（每人1个常用号）/ 库内全部号{data['phones']} / "
                                    f"{format_traffic(data.get('traffic_kb', 0))}"
                                    f"｜解码={'开' if solve else '关（费0）'}")
                            else:
                                fail_n += 1
                                self.bridge.log.emit(
                                    f"✗ [W{lane}] {url[:34]}：{data.get('error') or '失败'}"
                                    f"（{(data.get('title') or '')[:60]}）"
                                    f"｜解码={'开' if solve else '关（费0）'}")
                            self.bridge.refresh.emit()

                # 暂停/停止时手上还没派出去的那批，把它们从「运行中」放回「待处理」，
                # 不然下次开始就找不到它们了（底版不会碰到，因为它根本不预先占位）。
                if queue:
                    back = self._release_pending(queue)
                    self.bridge.log.emit(f"收回还没派发的地址 {back} 条，放回待处理")
                    queue = []

                left = self._pending_left()
                self.bridge.log.emit(
                    f"{reason or '结束'}｜本轮派发 {dispatched} 条：成功 {done_n}、失败 {fail_n}、"
                    f"异常 {err_n}｜库里还剩待处理 {left}"
                )
                if not self.running and left:
                    self.bridge.log.emit(f"想接着跑就点「开始」（还剩 {left} 条）")

            except Exception as e:
                # 调度线程自己出意外也绝不能悄悄收摊：把原因和还剩多少一起报出来
                self.bridge.log.emit(
                    f"调度出意外：{type(e).__name__}: {e}"[:200]
                    + f"｜本轮派发 {dispatched} 条，还剩待处理 {self._pending_left()}")
            finally:
                self.running = False
                self._stop_kind = ""
                for _fut, (_url, _lane, _solve) in list(futures.items()):
                    self._free_lane(_lane)
                with self._lane_lock:
                    self._lane_busy.clear()
                self.bridge.ui.emit(lambda: self.start_btn.setEnabled(True))
                self.bridge.ui.emit(lambda: self.pause_btn.setEnabled(False))
                self.bridge.ui.emit(lambda: self.stop_btn.setEnabled(False))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self._stop_kind = "stop"
        self.running = False
        self.log("请求停止...")

    def pause(self):
        """暂停：跑完当前地址后不再接新地址，可继续。"""
        self._stop_kind = "pause"
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
        # 自检只是验「捆绑的浏览器起不起得来」，绝不替主人花钱：
        # 解不解码只看库里这一勾，库里没填 Key 就一定是 0 次解码请求。
        do_solve = eng.get_setting("solve_captcha", "0") == "1" and bool(cap)
        lines.append(f" 解验证码={do_solve}（Key {'已填' if cap else '空'}）")
        ok, data = eng.process_address(addr, proxy, pw, cap, worker_id=0,
                                       max_pages=2, max_attempts=5,
                                       solve_captcha=do_solve,
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
