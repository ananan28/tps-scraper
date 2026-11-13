# -*- coding: utf-8 -*-
"""TPS 抓取器 - 主界面（新2.0版本）"""
import sys
import os
import csv
import time
import threading
from collections import deque
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

# 打包(frozen)运行时：浏览器目录指向 EXE 同级的 ms-playwright（随包携带，目标电脑离线可用）
if getattr(sys, "frozen", False):
    _browsers = Path(sys.executable).resolve().parent / "ms-playwright"
    if _browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers)

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QAbstractItemView,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QVBoxLayout, QWidget
)

import engine_core as eng

BASE = Path(__file__).resolve().parent

STATUS_CN = {"pending": "未用", "running": "跑着", "done": "完成", "failed": "失败"}


def _tip(w, text):
    """每个控件都挂一句中文说明，鼠标停上去就看得到。"""
    w.setToolTip(text)
    return w


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


class Main(QMainWindow):
    # 一次最多往列表里塞这么多行（地址有几万条时，全塞进表格就是卡死）
    TABLE_LIMIT = 500
    # 点一次「开始」最多排多少个地址
    START_LIMIT = 50000

    def __init__(self):
        super().__init__()
        eng.init_db()
        self.setWindowTitle("TPS 抓取器 新2.0版本")
        self.resize(1500, 900)
        self._log_fh = self._open_run_log()
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.refresh.connect(self.refresh)
        self.bridge.ui.connect(self._run_ui)
        self.running = False
        self.start_time = None
        self._concurrency = int(eng.get_setting("concurrency", 5))
        # 日志先攒进内存，150 毫秒批量上屏 + 落盘一次
        # （旧版每来一行就刷一次控件、还 flush 一次磁盘，并发一高整个界面就冻住）
        self._log_buf = []
        self._log_lock = threading.Lock()
        self._log_flushed = time.time()
        # 统计查询丢给后台线程，UI 线程只负责把结果写进标签
        self._stats_busy = False
        self._stats_again = False
        self._last_stats = {}
        # 搜索框一个字一个字敲，不能每个字母查一次库，攒 350 毫秒再查
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.load_addresses)
        self.build()
        self.refresh()
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self.refresh)
        self._stats_timer.start(3000)
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._drain_log)
        self._log_timer.start(150)
        self.log("就绪")
        self.log(f"日志文件: {getattr(self._log_fh, 'name', '')}")

    def build(self):
        central = QWidget()
        self.setCentralWidget(central)

        # 主布局：水平分割器（左：配置/统计，右：地址列表 + 日志）
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
        _tip(config, "跑之前先把这几项填好，改完点最下面的「保存配置」；"
                     "点开始也会自动按当前填的跑。不勾标题旁边的框就把整块收起。")
        config_layout = QVBoxLayout()

        # 代理
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(_tip(QLabel("代理模板:"),
                                 "填写代理服务商给的一整串地址，格式 http://用户名:密码@服务器:端口\n"
                                 "留空 = 用本机网络直连（省钱，但 TPS 的验证码拦了就过不去）。\n"
                                 "用户名里带 {session} 的话，每换一个出口 IP 会自动替换成随机串。"))
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("http://user:pass@host:port（用户名带 {session} 可自动换IP）")
        self.proxy_input.setText(eng.get_setting("proxy", ""))
        _tip(self.proxy_input, "代理服务器地址（一整串）。这一栏和密码栏都不会替你预填，要自己粘进去。")
        proxy_row.addWidget(self.proxy_input, 1)
        proxy_row.addWidget(_tip(QLabel("密码:"),
                                 "代理密码。只有代理地址里没写密码时才用这一栏。"))
        self.proxy_pass = QLineEdit()
        self.proxy_pass.setText(eng.get_setting("proxy_pass", ""))
        _tip(self.proxy_pass, "代理密码。填什么存什么，界面上一眼能核对。")
        proxy_row.addWidget(self.proxy_pass)
        config_layout.addLayout(proxy_row)

        # 2Captcha 验证码 Key
        capsolver_row = QHBoxLayout()
        capsolver_row.addWidget(_tip(QLabel("验证码 Key:"),
                                     "2Captcha 平台的 API Key。TPS 的详情页有验证码，"
                                     "被拦时程序把这串 Key 交给 2Captcha 自动解题，解出来才拿得到号码。\n"
                                     "Key 为空或平台没余额 → 一律抓不到电话（不是命中率低，是没解题）。"))
        self.capsolver_input = QLineEdit()
        self.capsolver_input.setMinimumWidth(300)
        self.capsolver_input.setMinimumHeight(25)
        self.capsolver_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.capsolver_input.setPlaceholderText("2Captcha Key（被拦时自动解码）")
        self.capsolver_input.setText(eng.get_setting("capsolver", ""))
        _tip(self.capsolver_input, "2Captcha 的 API Key（一串字母数字）。解题按次扣余额。")
        capsolver_row.addWidget(self.capsolver_input, 1)
        config_layout.addLayout(capsolver_row)

        # 并发 + 重试
        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(_tip(QLabel("并发数:"),
                                       "同时开几个浏览器一起抓。每开一路就是电脑上多一个独立的 Chromium，"
                                       "吃 CPU 和内存。\n"
                                       "调大不一定更快：电脑发热、界面发顿、代理掉速就是这个值太高。\n"
                                       "跑的过程中可以直接改，完成一路会按新值补路。"))
        self.concurrency_input = QSpinBox()
        self.concurrency_input.setRange(1, 9999)
        self.concurrency_input.setValue(self._concurrency)
        self.concurrency_input.valueChanged.connect(self._set_concurrency)
        _tip(self.concurrency_input, "建议先从 5 开始试；机器扛得住再加。程序最多同时开 128 路。")
        concurrency_row.addWidget(self.concurrency_input)
        concurrency_row.addWidget(_tip(QLabel("重试次数:"),
                                       "一个地址整体打不开时，换出口 IP 重来几遍。\n"
                                       "调太高只是白烧流量和时间；连续几次都是「Access Denied」就该停手换时段。"))
        self.retry_input = QSpinBox()
        self.retry_input.setRange(1, 20)
        self.retry_input.setValue(int(eng.get_setting("max_attempts", 12)))
        _tip(self.retry_input, "每个地址最多试几轮（一轮换一个出口 IP + 一个新浏览器）。默认 12。")
        concurrency_row.addWidget(self.retry_input)
        concurrency_row.addStretch()
        config_layout.addLayout(concurrency_row)

        # 采集范围：年龄上下限 + 换IP上限 + 每人重试
        range_row = QHBoxLayout()
        range_row.addWidget(_tip(QLabel("年龄下限:"),
                                 "只保留年龄 ≥ 这个值的人。列表页没写年龄的人照收（当成未知不卡）。\n"
                                 "这个值只影响「留不留」，不影响抓得动抓不动。"))
        self.min_age_input = QSpinBox()
        self.min_age_input.setRange(0, 120)
        self.min_age_input.setValue(int(eng.get_setting("min_age", 50)))
        _tip(self.min_age_input, "年龄下限，默认 50。改完要点「保存配置」，导出 CSV 时也按这个范围筛。")
        range_row.addWidget(self.min_age_input)
        range_row.addWidget(_tip(QLabel("年龄上限:"),
                                 "只保留年龄 ≤ 这个值的人。默认 85，再高一般是录错。"))
        self.max_age_input = QSpinBox()
        self.max_age_input.setRange(0, 120)
        self.max_age_input.setValue(int(eng.get_setting("max_age", 85)))
        _tip(self.max_age_input, "年龄上限，默认 85。")
        range_row.addWidget(self.max_age_input)
        range_row.addWidget(_tip(QLabel("换IP上限:"),
                                 "同一个地址抓人抓一半被拦，最多再换几次 IP 接着做详情"
                                 "（列表页不重抓，人已经记在内存里）。\n"
                                 "到上限剩下没做完的人计入日志里的「弃」。"))
        self.max_rotations_input = QSpinBox()
        self.max_rotations_input.setRange(1, 50)
        self.max_rotations_input.setToolTip("每个地址最多换几次 IP 续做详情；到上限剩下的人计入「放弃」\n"
                                            "换一次 IP = 重开一个浏览器，几秒种、一点流量，不重抓列表页。")
        self.max_rotations_input.setValue(int(eng.get_setting("max_rotations", 6)))
        range_row.addWidget(self.max_rotations_input)
        range_row.addWidget(_tip(QLabel("每人重试:"),
                                 "单个人的详情页被打回几次就放弃他，跳到下一个人。\n"
                                 "调大 = 在一个人身上反复碰运气；一般 2 次就够了。"))
        self.max_person_tries_input = QSpinBox()
        self.max_person_tries_input.setRange(1, 10)
        self.max_person_tries_input.setToolTip("同一个人被拦几次后跳过，计入「打不开」\n"
                                               "详情页是验证码重灾区，一个人死磕不划算。")
        self.max_person_tries_input.setValue(int(eng.get_setting("max_person_tries", 2)))
        range_row.addWidget(self.max_person_tries_input)
        range_row.addStretch()
        config_layout.addLayout(range_row)

        # 保留性别：勾选哪类就留哪类（哪天要女性就勾「女」）
        gender_row = QHBoxLayout()
        gender_row.addWidget(_tip(QLabel("保留性别:"),
                                  "只有勾了的类别才会进详情页花钱抓。类别是按离线姓名库（user_names.json）"
                                  "判出来的：男 / 女 / 中性（男女同名）/ 未知（库里查不到）。\n"
                                  "不勾「女」= 一看是女名直接跳过，不进详情页，省解题费。"))
        self.gender_checks = {}
        saved_genders = set(eng.get_setting("keep_genders", "male,andy,unknown").split(","))
        for key, label in (("male", "男"), ("female", "女"), ("andy", "中性"), ("unknown", "未知")):
            cb = QCheckBox(label)
            cb.setChecked(key in saved_genders)
            cb.setToolTip(f"保留「{label}」这一类人。\n按离线姓名库判定；只有勾选的类别才会进详情页抓取。")
            self.gender_checks[key] = cb
            gender_row.addWidget(cb)
        gender_row.addStretch()
        config_layout.addLayout(gender_row)

        # 防睡眠定时器
        sleep_row = QHBoxLayout()
        sleep_row.addWidget(_tip(QLabel("防睡眠:"),
                                 "勾选后电脑不会因为无人操作而睡眠（长跑必须勾，一睡眠浏览器全断）。"))
        self.prevent_sleep_check = QCheckBox("保持唤醒")
        self.prevent_sleep_check.setChecked(eng.get_setting("prevent_sleep", "0") == "1")
        self.prevent_sleep_check.setToolTip("用 Windows 的 SetThreadExecutionState 挡住自动睡眠。\n"
                                           "只挡系统睡眠，不动你的屏幕关闭设置；取消勾选就恢复原样。")
        self.prevent_sleep_check.stateChanged.connect(self.toggle_sleep_prevention)
        sleep_row.addWidget(self.prevent_sleep_check)
        sleep_row.addWidget(_tip(QLabel("定时(小时):"),
                                 "保持唤醒多久之后自动放行睡眠。0 = 一直挡着不放行。"))
        self.sleep_timer_input = QSpinBox()
        self.sleep_timer_input.setRange(0, 168)
        self.sleep_timer_input.setValue(int(eng.get_setting("sleep_timer_hours", "0")))
        self.sleep_timer_input.setToolTip("到点自动允许睡眠，免得电脑整夜醒着。填 0 表示不设时限。")
        self.sleep_timer_input.valueChanged.connect(self.save_config)
        sleep_row.addWidget(self.sleep_timer_input)
        sleep_row.addStretch()
        config_layout.addLayout(sleep_row)

        # 保存按钮
        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(self.save_config)
        _tip(save_btn, "把上面这些写进数据库的 settings 表，下次打开还在。\n"
                       "注意：代理和密码会以明文存在同一个软件目录下的数据库文件里，别把整个文件夹发给别人。")
        config_layout.addWidget(save_btn)

        config.setLayout(config_layout)
        left_layout.addWidget(config)

        # 使用说明（默认收起，不占地方）
        help_group = QGroupBox("使用说明（点开看）")
        help_group.setCheckable(True)
        help_group.setChecked(False)
        help_layout = QVBoxLayout()
        help_box = QPlainTextEdit()
        help_box.setReadOnly(True)
        help_box.setMaximumHeight(260)
        help_box.setPlainText(
            "整个流程就四步：\n"
            "1 地址导入：把 TPS 的地址一行一个粘进框里点「导入粘贴框」，或「从文件导入」txt/csv。\n"
            "   导入后默认都是「未用」，重复的地址不会重复入库。\n"
            "2 配置：填代理 + 2Captcha Key，选好年龄和保留性别，勾上「保持唤醒」，点保存配置。\n"
            "3 开始：点 ▶ 开始。右边日志一行一个地址，✓ 那行的数字就是这个地址的战果：\n"
            "   网站总数=站点里挂在这个地址下的人头数；存X人=年龄性别都合格进了详情并入库；\n"
            "   无号码=这人入库了但确实没留电话；跳过那一串是被过滤掉的人，加起来应该等于网站总数。\n"
            "   对不上就是引擎有问题，把日志截给我。\n"
            "4 收尾：「导出 CSV」拿号码；「导出未用地址」拿还剩的排队地址给另一台电脑接着跑；\n"
            "   「导出已用地址」拿已经跑过的（含失败的），换电脑时先删这批就不会重复劳动。\n"
            "要删某一条地址：右边「地址管理」搜索框填路名/城市/邮编，筛出来点行选中再「删除选中」；\n"
            "   要清一批就先用「筛选」选状态再「删除筛选出的」（删除都只删排队记录，已采的号码保留）。\n"
            "「⏸ 暂停」= 手里这一路跑完就不再派新地址，地址状态仍是未用，再点开始接着跑。\n"
            "「⏹ 停止」= 同上，但整批就此收工。\n"
            "「重试失败」= 把失败的排回未用队尾。\n"
            "「清除已导出数据」= 只清已采到的号码结果，地址排队记录保留。")
        help_layout.addWidget(help_box)
        help_group.setLayout(help_layout)
        left_layout.addWidget(help_group)

        # 地址导入（可折叠）
        import_group = QGroupBox("地址导入")
        import_group.setCheckable(True)
        import_group.setChecked(True)
        _tip(import_group, "只管往里加。要看已有的地址、删地址、导地址，用下面的「地址管理」。")
        import_layout = QVBoxLayout()
        self.address_input = QPlainTextEdit()
        self.address_input.setPlaceholderText("每行一个地址 URL 或原始地址...\n"
                                             "例：26 Helena Ave, Bethpage, NY 11714")
        self.address_input.setMaximumHeight(120)
        self.address_input.setToolTip("一行一个地址，空行和重复的自动忽略。\n"
                                      "粘进来点「导入粘贴框」才真正入库；这一步不联网、不花钱。")
        import_layout.addWidget(self.address_input)

        import_btn_row = QHBoxLayout()
        import_btn = QPushButton("导入粘贴框")
        import_btn.clicked.connect(self.import_paste)
        _tip(import_btn, "把上面文本框里的地址逐行存进数据库，状态=未用。")
        import_btn_row.addWidget(import_btn)

        file_btn = QPushButton("从文件导入")
        file_btn.clicked.connect(self.import_file)
        _tip(file_btn, "选一个 txt 或 csv，一行一个地址读进来。\n"
                       "「导出未用地址」出来的文件就是这种格式，可以直接在这里导入。")
        import_btn_row.addWidget(file_btn)
        import_btn_row.addStretch()
        import_layout.addLayout(import_btn_row)
        import_group.setLayout(import_layout)
        left_layout.addWidget(import_group)

        # 控制区（可折叠）
        control = QGroupBox("控制")
        control.setCheckable(True)
        control.setChecked(True)
        control_layout = QHBoxLayout()

        self.start_btn = QPushButton("▶ 开始")
        self.start_btn.clicked.connect(self.start)
        _tip(self.start_btn, "开跑未使用的地址（一次最多排 5 万个）。开跑前会先检查代理和验证码 Key 有没有填。")
        control_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.clicked.connect(self.pause)
        self.pause_btn.setEnabled(False)
        _tip(self.pause_btn, "手里在跑的让它跑完，之后不再派新地址；剩下的还是「未用」，再点开始就接着跑。")
        control_layout.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        _tip(self.stop_btn, "同上，但整批就此结束，按钮恢复成可点「开始」。")
        control_layout.addWidget(self.stop_btn)

        export_btn = QPushButton("导出 CSV")
        export_btn.clicked.connect(self.export)
        _tip(export_btn, "导出抓到的号码结果（姓名/性别/年龄/居住地/电话/查询地址/采集时间），"
                         "按上面设的年龄上下限筛，UTF-8 带 BOM，Excel 直接打开不乱码。")
        control_layout.addWidget(export_btn)

        clear_btn = QPushButton("清除已导出数据")
        clear_btn.clicked.connect(self.clear_data)
        _tip(clear_btn, "删掉已经采到的结果数据（先导出再点！），地址排队记录保留并重置为未用。")
        control_layout.addWidget(clear_btn)

        retry_btn = QPushButton("重试失败")
        retry_btn.clicked.connect(self.retry_failed)
        _tip(retry_btn, "把状态=失败的地址改回未用，排到队尾，下次开始再跑一遍。")
        control_layout.addWidget(retry_btn)

        del_btn = QPushButton("删除成功")
        del_btn.clicked.connect(self.delete_done)
        _tip(del_btn, "批量删掉状态=完成的地址排队记录（结果数据不动）。\n"
                      "要删任意/选中的地址，去右边「地址管理」。")
        control_layout.addWidget(del_btn)

        control_layout.addStretch()
        control.setLayout(control_layout)
        left_layout.addWidget(control)

        # 统计（可折叠）
        stats_group = QGroupBox("统计")
        stats_group.setCheckable(True)
        stats_group.setChecked(True)
        stats_layout = QGridLayout()
        self.stats_labels = {}
        stat_help = {
            "pending": "还在排队、一个浏览器都没碰过的地址数。「导出未用地址」导的就是这批。",
            "running": "正在跑（或上次被强行中断）的地址数。",
            "done": "已经跑完的地址数。",
            "failed": "试满「重试次数」还是打不开的地址数。「重试失败」会把这批放回未用。",
            "results": "入库的人数（已按姓名+年龄去重）。",
            "phones": "这些人里留了电话的人数。远低于结果数 = 解题没成功或对方就没留号。",
            "traffic_kb": "过代理的流量合计（各地址累加）。填了付费代理就盯这个数。",
        }
        for i, (key, label) in enumerate([
            ("pending", "待处理"),
            ("running", "运行中"),
            ("done", "已完成"),
            ("failed", "失败"),
            ("results", "结果数"),
            ("phones", "电话数"),
            ("traffic_kb", "流量"),
        ]):
            tl = QLabel(f"{label}:")
            tl.setToolTip(stat_help[key])
            stats_layout.addWidget(tl, i // 3, (i % 3) * 2)
            lbl = QLabel("0")
            lbl.setStyleSheet("font-weight:bold; font-size:14px;")
            lbl.setToolTip(stat_help[key])
            stats_layout.addWidget(lbl, i // 3, (i % 3) * 2 + 1)
            self.stats_labels[key] = lbl
        # 运行时长 / 每小时速度 / 电话速度
        h = QLabel("运行时长:")
        h.setToolTip("从点「开始」算到现在，不是数据库总耗时。")
        stats_layout.addWidget(h, 3, 0)
        self.runtime_lbl = QLabel("00:00:00")
        self.runtime_lbl.setStyleSheet("font-weight:bold; font-size:14px;")
        self.runtime_lbl.setToolTip(h.toolTip())
        stats_layout.addWidget(self.runtime_lbl, 3, 1)
        stats_layout.addWidget(_tip(QLabel("每小时速度:"),
                                    "已完成地址数 ÷ 运行时长。掉到很低 = 被拦得多，在退避重试。"), 3, 2)
        self.hourly_lbl = QLabel("0 地址/小时")
        self.hourly_lbl.setStyleSheet("font-weight:bold; font-size:14px;")
        self.hourly_lbl.setToolTip("每小时能跑完几个地址。健康值看站点大小，一般几到几十。")
        stats_layout.addWidget(self.hourly_lbl, 3, 3)
        stats_layout.addWidget(_tip(QLabel("电话速度:"),
                                    "有电话的人数 ÷ 运行分钟数。这是真正衡量产值的数。"), 4, 0)
        self.phone_speed_lbl = QLabel("0 电话/分钟")
        self.phone_speed_lbl.setStyleSheet("font-weight:bold; font-size:14px;")
        self.phone_speed_lbl.setToolTip("每分钟到手几个带电话的人。掉到 0 就是被拦或 Key 没余额了。")
        stats_layout.addWidget(self.phone_speed_lbl, 4, 1)
        stats_group.setLayout(stats_layout)
        left_layout.addWidget(stats_group)

        left_layout.addStretch()
        splitter.addWidget(left_widget)

        # ===== 右侧：上=地址管理，下=日志 =====
        right_split = QSplitter(Qt.Vertical)

        addr_group = QGroupBox("地址管理（看 / 删 / 导）")
        addr_group.setCheckable(True)
        addr_group.setChecked(True)
        _tip(addr_group, "导入进来的地址都在这里。默认显示最新的 500 条，用「筛选」缩小范围。\n"
                         "多选：按住 Ctrl 点，或 Shift 点首尾；删除、导出都按筛选条件或选中行走。")
        addr_layout = QVBoxLayout()

        bar = QHBoxLayout()
        bar.addWidget(_tip(QLabel("筛选:"), "只影响下面列表显示哪一批，以及「删除筛选出的」删哪一批。"))
        self.filter_combo = QComboBox()
        for label, key in (("全部", "all"), ("未用（待处理）", "pending"), ("跑着", "running"),
                           ("完成", "done"), ("失败", "failed")):
            self.filter_combo.addItem(label, key)
        self.filter_combo.setToolTip("选一类状态。"
                                     "「未用」= 还没跑过的地址，可以导出去给别的电脑跑。")
        self.filter_combo.currentIndexChanged.connect(self.load_addresses)
        bar.addWidget(self.filter_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜地址关键词…")
        self.search_input.setMaximumWidth(200)
        self.search_input.setToolTip(
            "地址有几万条时用它定位。填路名、城市或邮编都行，输入内容按原样匹配（不区分大小写）。\n"
            "搜索和上面的「筛选」是叠加的：先筛状态、再按关键词；「删除筛选出的」也只删符合这两条的。")
        self.search_input.textChanged.connect(self._on_search)
        bar.addWidget(self.search_input)

        reload_btn = QPushButton("刷新列表")
        reload_btn.clicked.connect(self.load_addresses)
        reload_btn.setToolTip("重读数据库刷新这张表（跑起来之后状态列不会自己变，点这里）。")
        bar.addWidget(reload_btn)

        self.addr_count_lbl = QLabel("共 0 条")
        self.addr_count_lbl.setToolTip("显示的是「本表显示条数 / 符合筛选的总条数」。")
        bar.addWidget(self.addr_count_lbl)
        bar.addStretch()
        addr_layout.addLayout(bar)

        self.addr_table = QTableWidget(0, 8)
        self.addr_table.setHorizontalHeaderLabels(
            ["状态", "地址", "网站总数", "人数", "电话", "流量", "最后更新", "错误"])
        self.addr_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.addr_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.addr_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.addr_table.setAlternatingRowColors(True)
        self.addr_table.setWordWrap(False)
        self.addr_table.verticalHeader().setVisible(False)
        head = self.addr_table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.Stretch)
        self.addr_table.setColumnWidth(1, 420)
        self.addr_table.setToolTip(
            "网站总数 = 这个地址下站点挂的人数头；人数 = 过滤后入库的；电话 = 其中留了电话的。\n"
            "错误 = 失败的原因（被拦 / 打不开 / 无结果）。")
        addr_layout.addWidget(self.addr_table)

        ops = QHBoxLayout()
        del_sel_btn = QPushButton("删除选中")
        del_sel_btn.clicked.connect(self.delete_selected)
        _tip(del_sel_btn, "只删你在表里点选的那几行地址（结果数据保留）。")
        ops.addWidget(del_sel_btn)

        del_filter_btn = QPushButton("删除筛选出的")
        del_filter_btn.clicked.connect(self.delete_filtered)
        _tip(del_filter_btn, "按上面「筛选」选中的那一类状态整批删。\n"
                             "例：筛「失败」→ 点这个，把踩不动的地址一次清掉。会先弹确认。")
        ops.addWidget(del_filter_btn)

        used_btn = QPushButton("导出已用地址")
        used_btn.clicked.connect(self.export_used)
        _tip(used_btn, "导出跑过的地址（完成 + 失败 + 跑着的）成 CSV，带状态、人数、电话数、错误原因。\n"
                       "用途：留档、看哪些地址已经烧过流量、给另一台电脑当「别再跑这些」的清单。")
        ops.addWidget(used_btn)

        unused_btn = QPushButton("导出未用地址")
        unused_btn.clicked.connect(self.export_unused)
        _tip(unused_btn, "导出还没跑过的地址，纯文本、一行一个、不带表头。\n"
                         "拷到另一台电脑，在它的「从文件导入」里选这个文件就能接着跑，两边不会重复劳动。\n"
                         "（只有状态=未用的才出去，已跑过的不会重复派给别的机器）")
        ops.addWidget(unused_btn)

        ops.addStretch()
        addr_layout.addLayout(ops)
        addr_group.setLayout(addr_layout)
        right_split.addWidget(addr_group)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout()
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 10))
        # 窗口内只留 300 行（Qt 原生淘汰最旧行，O(1)）；完整日志写到 logs/run_*.log
        self.log_box.setMaximumBlockCount(300)
        self.log_box.setToolTip("窗口里只留最近 300 行，全部历史在软件目录 logs\\run_日期_时间.log 里，"
                                "「清除日志」只清屏不删文件。")
        log_layout.addWidget(self.log_box)

        clear_log_btn = QPushButton("清除日志")
        clear_log_btn.clicked.connect(self.clear_log)
        clear_log_btn.setMaximumWidth(100)
        clear_log_btn.setToolTip("只把屏幕上的日志擦掉。磁盘上的完整日志文件不动。")
        log_layout.addWidget(clear_log_btn, alignment=Qt.AlignRight)

        log_group.setLayout(log_layout)
        right_split.addWidget(log_group)

        right_split.setSizes([430, 470])
        splitter.addWidget(right_split)

        # 设置初始分割比例（左45% 右55%：右边要放地址表）
        splitter.setSizes([660, 840])

    def _open_run_log(self):
        """每次启动一个日志文件；只保留最近 10 个，避免无限增长。"""
        try:
            log_dir = eng.BASE / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            for old in sorted(log_dir.glob("run_*.log"))[:-9]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return open(log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log",
                        "a", encoding="utf-8", buffering=1024 * 64)
        except OSError:
            return open(os.devnull, "w", encoding="utf-8")

    def log(self, msg):
        """任意线程可调用：只往内存塞一行，上屏和落盘交给 _drain_log 批量做。"""
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with self._log_lock:
            self._log_buf.append(line)
            if len(self._log_buf) > 20000:
                del self._log_buf[:-10000]

    def _drain_log(self):
        with self._log_lock:
            batch = self._log_buf[:]
            self._log_buf.clear()
        if not batch:
            return
        try:
            self.log_box.setUpdatesEnabled(False)
            for line in batch:
                self.log_box.appendPlainText(line)
        except RuntimeError:
            pass
        finally:
            try:
                self.log_box.setUpdatesEnabled(True)
            except RuntimeError:
                pass
        try:
            self._log_fh.write("\n".join(batch) + "\n")
            # 平时（一两条）立刻落盘，免得程序被强杀时日志啥也没留下；
            # 刷屏的那几百条只 write 不 flush，这才是原来卡的地方
            if len(batch) < 50 or time.time() - self._log_flushed > 1.0:
                self._log_fh.flush()
                self._log_flushed = time.time()
        except Exception:
            pass

    def clear_log(self):
        """只清屏，不删磁盘上的完整日志"""
        self.log_box.clear()

    def closeEvent(self, event):
        try:
            self._drain_log()
            self._log_fh.flush()
            self._log_fh.close()
        except Exception:
            pass
        super().closeEvent(event)

    def _run_ui(self, fn):
        try:
            fn()
        except RuntimeError:
            pass

    def _set_concurrency(self, value):
        """并发数变更（UI 线程），供后台调度线程安全读取。"""
        self._concurrency = int(value)

    def _selected_genders(self):
        return [k for k, cb in self.gender_checks.items() if cb.isChecked()]

    def save_config(self):
        eng.set_setting("proxy", self.proxy_input.text().strip())
        eng.set_setting("proxy_pass", self.proxy_pass.text().strip())
        eng.set_setting("capsolver", self.capsolver_input.text().strip())
        eng.set_setting("concurrency", self.concurrency_input.value())
        eng.set_setting("max_attempts", self.retry_input.value())
        eng.set_setting("min_age", self.min_age_input.value())
        eng.set_setting("max_age", self.max_age_input.value())
        eng.set_setting("max_rotations", self.max_rotations_input.value())
        eng.set_setting("max_person_tries", self.max_person_tries_input.value())
        eng.set_setting("keep_genders", ",".join(self._selected_genders()))
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

    # ---------------- 地址管理 ----------------
    def _on_search(self, _text=""):
        self._search_timer.start(350)

    def _filter_sql(self):
        """状态筛选 + 关键词搜索叠在一起，返回 (WHERE 子句, 参数)。"""
        conds, args = [], []
        key = self.filter_combo.currentData()
        if key and key != "all":
            conds.append("status=?")
            args.append(key)
        kw = self.search_input.text().strip()
        if kw:
            # 按原样匹配：把 % _ \ 三个 LIKE 通配符转义掉，免得搜 "100_Main" 命中一堆
            esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conds.append("address LIKE ? ESCAPE '\\'")
            args.append("%" + esc + "%")
        return ("WHERE " + " AND ".join(conds)) if conds else "", args

    def _filter_label(self):
        parts = []
        key = self.filter_combo.currentData()
        if key and key != "all":
            parts.append(f"「{self.filter_combo.currentText()}」")
        kw = self.search_input.text().strip()
        if kw:
            parts.append(f"含「{kw}」")
        return " ".join(parts) or "全部"

    def load_addresses(self):
        where, args = self._filter_sql()
        try:
            with eng.connect() as con:
                total = con.execute(f"SELECT COUNT(*) FROM addresses {where}", args).fetchone()[0]
                rows = con.execute(
                    f"SELECT id,status,address,records,people,phones,traffic_kb,updated_at,error"
                    f" FROM addresses {where} ORDER BY id DESC LIMIT ?",
                    (*args, self.TABLE_LIMIT)).fetchall()
        except Exception as e:
            self.log(f"读地址列表失败: {e}")
            return
        self.addr_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            state = QTableWidgetItem(STATUS_CN.get(r["status"], r["status"] or ""))
            state.setData(Qt.UserRole, r["id"])
            state.setToolTip(r["status"] or "")
            self.addr_table.setItem(i, 0, state)
            self.addr_table.setItem(i, 1, QTableWidgetItem(r["address"]))
            self.addr_table.setItem(i, 2, QTableWidgetItem(str(r["records"] or 0)))
            self.addr_table.setItem(i, 3, QTableWidgetItem(str(r["people"] or 0)))
            self.addr_table.setItem(i, 4, QTableWidgetItem(str(r["phones"] or 0)))
            self.addr_table.setItem(i, 5, QTableWidgetItem(format_traffic(r["traffic_kb"])))
            self.addr_table.setItem(i, 6, QTableWidgetItem(r["updated_at"] or ""))
            err = QTableWidgetItem((r["error"] or "")[:60])
            err.setToolTip(r["error"] or "")
            self.addr_table.setItem(i, 7, err)
        if self.search_input.text().strip() or (self.filter_combo.currentData() or "all") != "all":
            self.addr_count_lbl.setText(f"显示 {len(rows)} / 命中 {total} 条 · 条件：{self._filter_label()}"
                                        + (f"（只列最新 {self.TABLE_LIMIT} 条）" if total > len(rows) else ""))
        else:
            more = f"（共 {total} 条，表里只列最新 {self.TABLE_LIMIT} 条）" if total > len(rows) else ""
            self.addr_count_lbl.setText(f"显示 {len(rows)} / 共 {total} 条{more}")

    def refresh_in_place(self):
        """跑动中自动刷状态用：保住滚动位置，别把用户正看的那一屏弹回顶部。"""
        pos = self.addr_table.verticalScrollBar().value()
        self.load_addresses()
        self.addr_table.verticalScrollBar().setValue(pos)

    def _still_pending(self, address):
        """派活前再核对一次：排队期间被删掉/被改成别的状态的地址，不要再为它开浏览器烧流量。"""
        try:
            with eng.connect() as con:
                return con.execute("SELECT 1 FROM addresses WHERE address=? AND status='pending'",
                                   (address,)).fetchone() is not None
        except Exception:
            return True

    def _selected_ids(self):
        ids = []
        for item in self.addr_table.selectedItems():
            if item.column() == 0:
                v = item.data(Qt.UserRole)
                if isinstance(v, int):
                    ids.append(v)
        return sorted(set(ids))

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "删除",
                                    "先在表里点选要删的行。\n（按住 Ctrl 逐行加选，或 Shift 选一段）")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定删除选中的 {len(ids)} 个地址吗？\n（只删地址排队记录，已采到的号码结果保留）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        with eng.connect() as con:
            marks = ",".join("?" * len(ids))
            n = con.execute(f"DELETE FROM addresses WHERE id IN ({marks})", ids).rowcount
        self.log(f"已删除选中地址 {n} 个")
        self.load_addresses()
        self.refresh()

    def delete_filtered(self):
        where, args = self._filter_sql()
        label = self._filter_label()
        try:
            with eng.connect() as con:
                total = con.execute(f"SELECT COUNT(*) FROM addresses {where}", args).fetchone()[0]
        except Exception as e:
            self.log(f"统计失败: {e}")
            return
        if not total:
            QMessageBox.information(self, "删除", f"符合条件的地址 0 个（条件：{label}）。")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            f"要删除 条件「{label}」下的全部 {total} 个地址。\n"
            f"（只删地址排队记录，已采到的号码结果保留）\n\n建议先点「导出未用地址」或「导出已用地址」留个底。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        with eng.connect() as con:
            n = con.execute(f"DELETE FROM addresses {where}", args).rowcount
        self.log(f"已删除「{label}」地址 {n} 个")
        self.load_addresses()
        self.refresh()

    def export_used(self):
        """已用 = 跑过的地址（完成/失败/跑着），导出成 CSV 留档。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出已使用的地址", str(BASE / f"tps_已用地址_{datetime.now():%Y%m%d_%H%M}.csv"),
            "CSV (*.csv)")
        if not path:
            return
        try:
            with eng.connect() as con:
                rows = con.execute(
                    "SELECT status,address,records,people,phones,traffic_kb,error,updated_at"
                    " FROM addresses WHERE status<>'pending' ORDER BY updated_at DESC").fetchall()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["状态", "地址", "网站总数", "人数", "电话数", "流量KB", "错误", "最后更新"])
                for r in rows:
                    w.writerow([r["status"], r["address"], r["records"] or 0, r["people"] or 0,
                                r["phones"] or 0, r["traffic_kb"] or 0, r["error"] or "",
                                r["updated_at"] or ""])
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        self.log(f"导出已用地址 {len(rows)} 条 → {path}")
        QMessageBox.information(self, "导出", f"已用地址 {len(rows)} 条已导出\n{path}")

    def export_unused(self):
        """未用 = 还没跑过的地址，导出纯文本一行一个，直接给另一台电脑导入。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出未使用的地址", str(BASE / f"tps_未用地址_{datetime.now():%Y%m%d_%H%M}.txt"),
            "文本文件 (*.txt)")
        if not path:
            return
        try:
            with eng.connect() as con:
                rows = con.execute("SELECT address FROM addresses WHERE status='pending' ORDER BY id").fetchall()
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(str(r["address"]).replace("\n", " ") + "\n")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        self.log(f"导出未用地址 {len(rows)} 条 → {path}（一行一个，别的电脑「从文件导入」直接选它）")
        QMessageBox.information(
            self, "导出",
            f"未用地址 {len(rows)} 条已导出。\n{path}\n\n"
            f"拷到另一台电脑，打开那边的软件点「从文件导入」选这个文件，就能接着跑。")

    def import_paste(self):
        lines = self.address_input.toPlainText().splitlines()
        added = eng.import_addresses(lines)
        self.log(f"导入 {added} 条地址")
        self.address_input.clear()
        self.load_addresses()
        self.refresh()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择文件", str(BASE), "文本文件 (*.txt *.csv)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"读不了这个文件：{e}")
            return
        added = eng.import_addresses(lines)
        self.log(f"从文件导入 {added} 条地址")
        self.load_addresses()
        self.refresh()

    def start(self):
        proxy = self.proxy_input.text().strip()
        proxy_pass = self.proxy_pass.text().strip()
        capsolver = self.capsolver_input.text().strip()
        concurrency = self.concurrency_input.value()
        min_age = self.min_age_input.value()
        max_age = self.max_age_input.value()
        max_rotations = self.max_rotations_input.value()
        max_person_tries = self.max_person_tries_input.value()
        keep_genders = self._selected_genders()

        if not proxy or not proxy_pass:
            QMessageBox.warning(self, "配置不完整", "请填写代理模板和密码")
            return
        if not capsolver:
            QMessageBox.warning(self, "配置不完整", "请填写 2Captcha Key（被拦时必须解码才能拿到号码）")
            return

        eng.set_setting("proxy", proxy)
        eng.set_setting("proxy_pass", proxy_pass)
        eng.set_setting("capsolver", capsolver)
        eng.set_setting("concurrency", concurrency)
        max_attempts = self.retry_input.value()
        eng.set_setting("max_attempts", max_attempts)
        eng.set_setting("min_age", min_age)
        eng.set_setting("max_age", max_age)
        eng.set_setting("max_rotations", max_rotations)
        eng.set_setting("max_person_tries", max_person_tries)
        eng.set_setting("keep_genders", ",".join(keep_genders))

        self.running = True
        self.start_time = time.time()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        glabel = {"male": "男", "female": "女", "andy": "中性", "unknown": "未知"}
        gender_txt = ("全部性别" if len(keep_genders) == 4 else
                      "无（不抓任何人）" if not keep_genders else
                      "+".join(glabel[k] for k in keep_genders))
        self.log(f"启动，并发 {concurrency}（被拦自动解码，{min_age}–{max_age}岁，"
                 f"保留{gender_txt}，换IP上限{max_rotations}，每人重试{max_person_tries}）")

        def work():
            try:
                with eng.connect() as con:
                    rows = con.execute(
                        "SELECT address FROM addresses WHERE status='pending' ORDER BY id LIMIT ?",
                        (self.START_LIMIT,)).fetchall()
                    total_pending = con.execute(
                        "SELECT COUNT(*) FROM addresses WHERE status='pending'").fetchone()[0]
                addresses = [r["address"] for r in rows]

                if not addresses:
                    self.bridge.log.emit("没有未使用的地址（先导入，或点「重试失败」）")
                    return
                if total_pending > len(addresses):
                    self.bridge.log.emit(f"未用地址共 {total_pending} 个，本轮先派 {len(addresses)} 个，"
                                         f"跑完再点开始继续")

                self.bridge.log.emit(f"开始处理 {len(addresses)} 个地址...")

                def on_progress(msg):
                    self.bridge.log.emit(msg)

                # 队列式调度：只保持并发数那么多在跑，完成一个补一个（支持暂停）
                # 线程池按并发数的 2 倍预留（改大并发时不用重开），最多 128 路
                pool_cap = min(128, max(16, concurrency * 2))
                dropped = 0
                with ThreadPoolExecutor(max_workers=pool_cap, thread_name_prefix="tps") as executor:
                    futures = {}
                    idx = 0
                    queue = deque(addresses)

                    while queue or futures:
                        limit = min(max(1, self._concurrency), pool_cap)
                        while self.running and len(futures) < limit and queue:
                            addr = queue.popleft()
                            if not self._still_pending(addr):
                                dropped += 1
                                continue
                            future = executor.submit(
                                eng.process_address, addr, proxy, proxy_pass, capsolver,
                                worker_id=idx, progress_cb=on_progress,
                                max_attempts=max_attempts, min_age=min_age, max_age=max_age,
                                max_rotations=max_rotations, max_person_tries=max_person_tries,
                                keep_genders=(None if len(keep_genders) == 4 else keep_genders))
                            futures[future] = addr
                            idx += 1

                        if not futures:
                            break

                        # 阻塞等到「至少有一个跑完」，不再固定睡 0.5 秒空转
                        done_futures, _ = wait(list(futures), timeout=0.3,
                                               return_when=FIRST_COMPLETED)
                        if not done_futures:
                            continue

                        for future in done_futures:
                            url = futures.pop(future)
                            try:
                                success, data = future.result()
                                if success:
                                    self.bridge.log.emit(
                                        f"✓ {url[:34]}：网站总数{data['records']}条 → 卡片总数{data.get('cards_seen', 0)}张 → "
                                        f"性别年龄达标 存{data['people']}人（无号码{data.get('no_phone', 0)}）总数不重复{data['phones']}电话 / "
                                        f"跳过 年龄不符:{data.get('skipped_low', 0) + data.get('skipped_high', 0)}"
                                        f" 性别不符:{data.get('skipped_gender', 0)} 重复:{data.get('dup_skipped', 0)}"
                                        f" 故:{data.get('dead_skipped', 0)} 拦:{data.get('skipped_blocked', 0)}"
                                        f" 弃:{data.get('abandoned', 0)} / "
                                        f"{format_traffic(data.get('traffic_kb', 0))}")
                                else:
                                    self.bridge.log.emit(
                                        f"✗ {url[:34]}：{data.get('error') or '失败'}（{data.get('title', '')[:60]}）")
                            except Exception as e:
                                self.bridge.log.emit(f"错误 {url[:34]}：{e}")

                    if dropped:
                        self.bridge.log.emit(f"其中 {dropped} 个地址在排队期间已被删除/改状态，"
                                             f"没有为它们开浏览器")
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
                self.bridge.ui.emit(self.load_addresses)
                self.bridge.ui.emit(self.refresh)

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.running = False
        self.log("请求停止...")

    def pause(self):
        """暂停：跑完当前地址后不再接新地址，可继续。"""
        self.running = False
        self.log("请求暂停（跑完当前地址后停止派发）...")

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
        self.load_addresses()
        self.refresh()

    def retry_failed(self):
        """把失败的地址重置为待处理，排到队尾重新跑。"""
        with eng.connect() as con:
            count = con.execute("UPDATE addresses SET status='pending', error='' WHERE status='failed'").rowcount
        self.log(f"已重置 {count} 个失败地址为待处理")
        QMessageBox.information(self, "重试", f"{count} 个失败地址已排到队尾")
        self.load_addresses()
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
        self.load_addresses()
        self.refresh()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出", str(BASE), "CSV (*.csv)")
        if not path:
            return
        out, count = eng.export_csv(path)
        self.log(f"导出 {count} 条 → {out}")
        QMessageBox.information(self, "导出", f"已导出 {count} 条")

    def refresh(self):
        """统计不在 UI 线程算：几万个地址时，那几条 COUNT/GROUP BY 会把窗口冻住。"""
        if self._stats_busy:
            self._stats_again = True
            return
        self._stats_busy = True
        self._stats_again = False
        threading.Thread(target=self._stats_job, daemon=True).start()

    def _stats_job(self):
        try:
            s = eng.stats()
        except Exception:
            s = None
        self.bridge.ui.emit(lambda: self._apply_stats(s))

    def _apply_stats(self, s):
        self._stats_busy = False
        if s:
            self._last_stats = s
            for key, lbl in self.stats_labels.items():
                val = s.get(key, 0)
                if key == "traffic_kb":
                    lbl.setText(format_traffic(val))
                else:
                    lbl.setText(str(val))
        s = self._last_stats or {}
        # 运行时长 / 每小时速度 / 电话速度
        if self.start_time:
            elapsed = time.time() - self.start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            sec = int(elapsed % 60)
            self.runtime_lbl.setText(f"{h:02d}:{m:02d}:{sec:02d}")
            done = s.get("done", 0)
            phones = s.get("phones", 0)
            hours = elapsed / 3600
            minutes = elapsed / 60
            self.hourly_lbl.setText(f"{done / hours:.0f} 地址/小时" if hours > 0 else "0 地址/小时")
            self.phone_speed_lbl.setText(f"{phones / minutes:.1f} 电话/分钟" if minutes > 0 else "0 电话/分钟")
        elif not self.running:
            self.runtime_lbl.setText("00:00:00")
            self.hourly_lbl.setText("0 地址/小时")
            self.phone_speed_lbl.setText("0 电话/分钟")
        # 跑动中让地址表跟着状态变；正选着行就不动，免得打断手头的多选
        if self.running and not self.addr_table.selectionModel().hasSelection():
            self.refresh_in_place()
        if self._stats_again:
            self._stats_again = False
            self.refresh()


def _selftest():
    """打包自检：新2.0版本.exe --selftest → 跑一个真实地址，结果写 selftest_result.txt（验证捆绑的浏览器/驱动可用）。"""
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
