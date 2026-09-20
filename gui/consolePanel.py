"""底部控制台面板
"""
"""
运行日志 + steamcmd 终端（占位）两个标签页。

LogBus：极小的日志总线。工作线程只调它的 info/ok/warn/error 发信号，
绝不直接碰任何控件——Qt 跨线程信号自动排队到主线程执行，线程安全
白送。这与"网络在线程、写库在主线程"是同一条分工原则。

ConsolePanel：唯一监听者。每行带时间戳、按级别着色；文本框设了
maximumBlockCount（上限 2000 行），超出自动丢弃最旧行，防止长时间
运行内存膨胀——"最多 N 行"的全部实现就这一行设置。

控制台内容关程序即清空；需要持久化的操作历史走 operations_log 表
（那是给程序读的结构化数据，这是给人看的结论），两层不混。
没有监听者时 LogBus 照样能发（信号发进空气）——所以页面可以无条件
打日志，不用判断"有没有人接"。
"""
import html
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

_LEVEL_COLORS = {
    "info": "#d4d4d4",   # 普通信息
    "ok": "#46a758",     # 成功结论
    "warn": "#f5a623",   # 警告
    "error": "#e5484d",  # 错误
}


class LogBus(QObject):
    """日志总线：line_emitted(级别, 文本)。任何线程都可安全调用。"""
    line_emitted = Signal(str, str)

    def info(self, text: str) -> None:
        self.line_emitted.emit("info", text)

    def ok(self, text: str) -> None:
        self.line_emitted.emit("ok", text)

    def warn(self, text: str) -> None:
        self.line_emitted.emit("warn", text)

    def error(self, text: str) -> None:
        self.line_emitted.emit("error", text)


class ConsolePanel(QWidget):
    def __init__(self, log_bus: LogBus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)

        # Tab 1：运行日志（只读、自动裁剪到 2000 行）
        log_page = QWidget(self._tabs)
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(4, 4, 4, 4)
        self._log_view = QPlainTextEdit(log_page)
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)  # 超限自动丢最旧的行
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self._log_view)
        clear = QPushButton("清空", log_page)
        clear.setFixedWidth(80)
        clear.clicked.connect(self._log_view.clear)
        log_layout.addWidget(clear, 0, Qt.AlignmentFlag.AlignLeft)
        self._tabs.addTab(log_page, "运行日志")

        # Tab 2：steamcmd 终端占位（后续版本接真终端）
        placeholder = QLabel("交互式 steamcmd 终端将在后续版本接入。",
                             self._tabs)
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setWordWrap(True)
        self._tabs.addTab(placeholder, "steamcmd 终端")

        log_bus.line_emitted.connect(self._append_line)

    def _append_line(self, level: str, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["info"])
        # 消息里的 <>& 必须转义，防止内容被当成 HTML 吃掉
        safe = html.escape(text)
        self._log_view.appendHtml(
            f'<span style="color:#888">{ts}</span> '
            f'<span style="color:{color}">{safe}</span>')
