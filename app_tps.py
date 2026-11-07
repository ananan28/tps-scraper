# -*- coding: utf-8 -*-
"""TPS 抓取器 - 主界面"""
import sys
import os
import time
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 打包(frozen)运行时：浏览器目录指向 EXE 同级的 ms-playwright（随包携带，目标电脑离线可用）
if getattr(sys, "frozen", False):
    _browsers = Path(sys.executable).resolve().parent / "ms-playwright"
    if _browsers.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers)

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
    QSystemTrayIcon, QMenu
)

import engine_tps as eng

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


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        eng.init_db()
        self.setWindowTitle("TPS 抓取器 v2.1.6")
        self.resize(1400, 900)
        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.refresh.connect(self.refresh)
        self.bridge.ui.connect(self._run_ui)
        self.running = False
        self.workers = []
        self.build()
        self.refresh()
        # 每 60 秒自动刷新统计（流量/完成数等），运行时可见
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self.refresh)
        self._stats_timer.start(60000)
        self.log("就绪")

    def build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 配置区
        config = QGroupBox("配置")
        config_layout = QVBoxLayout()

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

        # CapSolver
        capsolver_row = QHBoxLayout()
        capsolver_row.addWidget(QLabel("CapSolver Key:"))
        self.capsolver_input = QLineEdit()
        self.capsolver_input.setText(eng.get_setting("capsolver", ""))
        capsolver_row.addWidget(self.capsolver_input, 1)
        config_layout.addLayout(capsolver_row)

        # 并发
        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(QLabel("并发数:"))
        self.concurrency_input = QSpinBox()
        self.concurrency_input.setRange(1, 9999)
        self.concurrency_input.setValue(int(eng.get_setting("concurrency", 5)))
        concurrency_row.addWidget(self.concurrency_input)
        concurrency_row.addWidget(QLabel("重试次数:"))
        self.retry_input = QSpinBox()
        self.retry_input.setRange(1, 20)
        self.retry_input.setValue(int(eng.get_setting("max_attempts", 12)))
        concurrency_row.addWidget(self.retry_input)
        concurrency_row.addStretch()
        config_layout.addLayout(concurrency_row)

        # 保存按钮
        save_btn = QPushButton("保存配置")
        save_btn.clicked.connect(self.save_config)
        config_layout.addWidget(save_btn)

        config.setLayout(config_layout)
        root.addWidget(config)

        # 地址导入
        import_group = QGroupBox("地址导入")
        import_layout = QVBoxLayout()
        self.address_input = QPlainTextEdit()
        self.address_input.setPlaceholderText("每行一个地址 URL 或原始地址...")
        self.address_input.setMaximumHeight(150)
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
        import_group.setLayout(import_layout)
        root.addWidget(import_group)

        # 控制区
        control = QGroupBox("控制")
        control_layout = QHBoxLayout()

        self.start_btn = QPushButton("▶ 开始")
        self.start_btn.clicked.connect(self.start)
        control_layout.addWidget(self.start_btn)

        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.clicked.connect(self.pause)
        self.pause_btn.setEnabled(False)
        control_layout.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        control_layout.addWidget(self.stop_btn)

        export_btn = QPushButton("导出 CSV")
        export_btn.clicked.connect(self.export)
        control_layout.addWidget(export_btn)

        clear_btn = QPushButton("清除已导出数据")
        clear_btn.clicked.connect(self.clear_data)
        control_layout.addWidget(clear_btn)

        retry_btn = QPushButton("重试失败")
        retry_btn.clicked.connect(self.retry_failed)
        control_layout.addWidget(retry_btn)

        del_btn = QPushButton("删除成功")
        del_btn.clicked.connect(self.delete_done)
        control_layout.addWidget(del_btn)

        control_layout.addStretch()
        control.setLayout(control_layout)
        root.addWidget(control)

        # 统计
        stats_group = QGroupBox("统计")
        stats_layout = QGridLayout()
        self.stats_labels = {}
        for i, (key, label) in enumerate([
            ("pending", "待处理"),
            ("running", "运行中"),
            ("done", "已完成"),
            ("failed", "失败"),
            ("results", "结果数"),
            ("phones", "电话数"),
            ("traffic_kb", "流量"),
            ("capsolver_balance", "CapSolver余额")
        ]):
            stats_layout.addWidget(QLabel(f"{label}:"), i // 3, (i % 3) * 2)
            lbl = QLabel("0")
            lbl.setStyleSheet("font-weight:bold; font-size:14px;")
            stats_layout.addWidget(lbl, i // 3, (i % 3) * 2 + 1)
            self.stats_labels[key] = lbl
        # 运行时长和每小时速度
        stats_layout.addWidget(QLabel("运行时长:"), 3, 0)
        self.runtime_lbl = QLabel("00:00:00")
        self.runtime_lbl.setStyleSheet("font-weight:bold; font-size:14px;")
        stats_layout.addWidget(self.runtime_lbl, 3, 1)
        stats_layout.addWidget(QLabel("每小时速度:"), 3, 2)
        self.hourly_lbl = QLabel("0 地址/小时")
        self.hourly_lbl.setStyleSheet("font-weight:bold; font-size:14px;")
        stats_layout.addWidget(self.hourly_lbl, 3, 3)
        stats_group.setLayout(stats_layout)
        root.addWidget(stats_group)

        # 日志
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
        root.addWidget(log_group, 1)

    def log(self, msg):
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_box.append(f"[{timestamp}] {msg}")
            # 日志最多保留 200 行，超了自动删旧的
            doc = self.log_box.document()
            if doc.blockCount() > 200:
                cursor = self.log_box.textCursor()
                cursor.movePosition(cursor.Start)
                # 删除多余的行（包括换行符）
                for _ in range(doc.blockCount() - 200):
                    cursor.movePosition(cursor.Down, cursor.KeepAnchor)
                cursor.removeSelectedText()
                # 删除第一个换行符
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

    def save_config(self):
        eng.set_setting("proxy", self.proxy_input.text().strip())
        eng.set_setting("proxy_pass", self.proxy_pass.text().strip())
        eng.set_setting("capsolver", self.capsolver_input.text().strip())
        eng.set_setting("concurrency", self.concurrency_input.value())
        eng.set_setting("max_attempts", self.retry_input.value())
        self.log("配置已保存")
        QMessageBox.information(self, "保存", "配置已保存")

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
        self.log("点击开始...")
        proxy = self.proxy_input.text().strip()
        proxy_pass = self.proxy_pass.text().strip()
        capsolver = self.capsolver_input.text().strip()
        concurrency = self.concurrency_input.value()

        self.log(f"配置: proxy={bool(proxy)}, pass={bool(proxy_pass)}, capsolver={bool(capsolver)}, 并发={concurrency}")

        if not proxy or not proxy_pass or not capsolver:
            self.log("配置不完整")
            QMessageBox.warning(self, "错误", "请填写完整配置")
            return

        self.log("配置检查通过，保存配置...")
        eng.set_setting("proxy", proxy)
        eng.set_setting("proxy_pass", proxy_pass)
        eng.set_setting("capsolver", capsolver)
        eng.set_setting("concurrency", concurrency)
        max_attempts = self.retry_input.value()
        eng.set_setting("max_attempts", max_attempts)

        self.running = True
        self.start_time = time.time()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.log(f"启动，并发 {concurrency}")

        def work():
            try:
                with eng.connect() as con:
                    addresses = [row["address"] for row in con.execute("SELECT address FROM addresses WHERE status='pending' LIMIT 1000").fetchall()]

                if not addresses:
                    self.bridge.log.emit("没有待处理地址")
                    return

                self.bridge.log.emit(f"开始处理 {len(addresses)} 个地址...")

                def on_progress(msg):
                    self.bridge.log.emit(msg)

                with ThreadPoolExecutor(max_workers=9999) as executor:
                    futures = {}
                    idx = 0
                    queue = list(addresses)

                    # 调度器：只提交 concurrency 个，完成一个再拉下一个
                    while self.running and (queue or futures):
                        current_concurrency = self.concurrency_input.value()

                        while self.running and len(futures) < current_concurrency and queue:
                            addr = queue.pop(0)
                            future = executor.submit(eng.process_address, addr,
                                                     proxy, proxy_pass, capsolver,
                                                     worker_id=idx, progress_cb=on_progress,
                                                     max_attempts=max_attempts)
                            futures[future] = addr
                            idx += 1

                        if not futures:
                            break

                        done_futures = [f for f in futures if f.done()]
                        if not done_futures:
                            time.sleep(1)
                            continue

                        for future in done_futures:
                            url = futures.pop(future)
                            try:
                                success, data = future.result()
                                if success:
                                    self.bridge.log.emit(
                                        f"✓ {url[:34]}：{data['records']}条记录 / {data['people']}人 / {data['phones']}电话 / {format_traffic(data.get('traffic_kb',0))}")
                                else:
                                    self.bridge.log.emit(
                                        f"✗ {url[:34]}：{data.get('error') or '失败'}（{data.get('title','')[:60]}）")
                            except Exception as e:
                                self.bridge.log.emit(f"错误 {url[:34]}：{e}")

                            self.bridge.refresh.emit()

                    if not self.running:
                        self.bridge.log.emit("已停止/暂停")
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
        # 把运行中的地址重置为待处理，以便恢复
        with eng.connect() as con:
            con.execute("UPDATE addresses SET status='pending' WHERE status='running'")
        self.log("请求暂停（跑完当前地址后停止）...")

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
            f"确定要删除所有已完成的地址吗？\n（结果数据保留，仅删除地址记录）",
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

    def refresh(self):
        s = eng.stats()
        for key, lbl in self.stats_labels.items():
            val = s.get(key, 0)
            if key == "traffic_kb":
                lbl.setText(format_traffic(val))
            elif key == "capsolver_balance":
                bal = eng.capsolver_balance()
                if bal is not None:
                    lbl.setText(f"${bal:.2f}")
                else:
                    lbl.setText("--")
            else:
                lbl.setText(str(val))
        # 运行时长和每小时速度
        if hasattr(self, 'start_time') and self.start_time:
            elapsed = time.time() - self.start_time
            hours = elapsed / 3600
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            sec = int(elapsed % 60)
            self.runtime_lbl.setText(f"{h:02d}:{m:02d}:{sec:02d}")
            done = s.get("done", 0)
            if hours > 0:
                speed = done / hours
                self.hourly_lbl.setText(f"{speed:.0f} 地址/小时")
            else:
                self.hourly_lbl.setText(f"{done} 地址/小时")


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
        lines.append(f" browsers_path={os.environ.get('PLAYWRIGHT_BROWSERS_PATH','(default)')}")
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
