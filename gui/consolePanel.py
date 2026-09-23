"""底部控制台面板
"""
"""
运行日志 + steamcmd 终端两个标签页。

LogBus：极小的日志总线，本体住在 gui/logBus.py（M3 为解开
循环导入搬的家，这里保留一条再导出，老写法不受影响）。
工作线程只调它的 info/ok/warn/error 发信号，绝不直接碰任何
控件——Qt 跨线程信号自动排队到主线程执行，线程安全白送。

ConsolePanel：唯一监听者。每行带时间戳、按级别着色；文本框设了
maximumBlockCount（上限 2000 行），超出自动丢弃最旧行，防止长时间
运行内存膨胀——"最多 N 行"的全部实现就这一行设置。

"新消息自动弹出"：控制台被整体关掉时来了新日志，用户根本无从
得知（红点只对开着控制台的人有意义）。所以运行日志页提供了一个
勾选框（默认开，持久化到设置的 console_auto_show 键）：勾着时
新消息会把控制台停靠窗拉回屏幕并跳到运行日志页——本面板只负责
"发现该弹了"并发 show_requested 信号，真正把停靠窗拉出来的是
MainWindow（谁建的停靠窗谁来管）。关掉勾选就回到老行为。

控制台内容关程序即清空；需要持久化的操作历史走 operations_log 表
（那是给程序读的结构化数据，这是给人看的结论），两层不混。

没有监听者时 LogBus 照样能发（信号发进空气）——所以页面可以
无条件打日志，不用判断"有没有人接"。

Tab 2 的 steamcmd 终端是 TerminalDock（M3）：终端看原文、
运行日志说人话，两层的分工见 terminalDock.py 文件头。
"""
import html
import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

# 再导出：LogBus 本体在 gui/logBus.py。这条 import 表面上
# 本文件没直接用它（构造终端时转发的是形参），作用是让既有代码
# 的 `from gui.consolePanel import LogBus` 原样可用——
# 搬家不改门牌，其他文件一行都不用动
from gui.logBus import LogBus  # noqa: F401

from gui.terminalDock import TerminalDock
from gui.stepCardList import StepCardList

_LEVEL_COLORS = {
    "info": "#d4d4d4",   # 普通信息
    "ok": "#46a758",     # 成功结论
    "warn": "#f5a623",   # 警告
    "error": "#e5484d",  # 错误
}

# "新消息自动弹出"在设置里的键名（数字/布尔一律按字符串存，项目约定）
_KEY_AUTO_SHOW = "console_auto_show"


class ConsolePanel(QWidget):
    """控制台停靠窗的内容；show_requested 在需要弹出时发出。"""

    show_requested = Signal()  # 请 MainWindow 把停靠窗拉回屏幕

    def __init__(self, log_bus: LogBus, parent: QWidget | None = None,
                 settings=None) -> None:
        super().__init__(parent)
        self._settings = settings  # AppSettings：持久化"自动弹出"开关
        self._log_dirty = False  # 运行日志有没看过的新消息时置 True（红点）

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

        # 日志页底部一排：清空按钮 + 自动弹出开关（左按钮右开关，
        # 中间用弹簧撑开，视觉上各占一头不打架）
        bottom_row = QHBoxLayout()
        clear = QPushButton("清空", log_page)
        clear.setFixedWidth(80)
        clear.clicked.connect(self._log_view.clear)
        bottom_row.addWidget(clear)

        self._auto_show = QCheckBox("新消息自动弹出", log_page)
        self._auto_show.setToolTip(
            "勾上：控制台被关闭期间来了新日志，自动把控制台拉回屏幕"
            "并跳到运行日志页；\n"
            "不勾：安静待着（红点只在控制台开着时才有意义）。")
        self._auto_show.setChecked(self._load_auto_show())
        self._auto_show.toggled.connect(self._on_auto_show_toggled)
        bottom_row.addWidget(self._auto_show)
        bottom_row.addStretch(1)  # T19⑥：与终端页同拍，按钮/开关一律靠左

        log_layout.addLayout(bottom_row)
        self._tabs.addTab(log_page, "运行日志")

        # Tab 2：steamcmd 终端（M3 真终端）。
        # settings 转发进去，终端靠它读 steamcmd 路径和登录命令；
        # 终端里出现的每个结论由 TerminalDock 自己送到本类监听的
        # LogBus，这里不需要任何额外接线
        self._terminal = TerminalDock(log_bus, settings, self._tabs)
        self._tabs.addTab(self._terminal, "steamcmd 终端")
        # Tab 3：下载批次（M3 收官）。只显示事件、发按钮信号，
        # 编排交给 MainWindow 里的控制器（见 batchDownloadController）
        self._step_list = StepCardList(self._tabs)
        self._tabs.addTab(self._step_list, "下载批次")

        # 切换标签页时检查要不要摘红点（先接 UI 信号，最后接日志流，
        # 保证首条日志到来时界面已就绪）
        self._tabs.currentChanged.connect(self._on_tab_changed)
        log_bus.line_emitted.connect(self._append_line)

    @property
    def terminal(self) -> TerminalDock:
        """终端实例：MainWindow 做关窗确认、批量编排接线时取用。"""
        return self._terminal
    def show_batch_tab(self) -> None:
        """切到「下载批次」标签（开批次时 MainWindow 调）。"""
        self._tabs.setCurrentIndex(2)

    @property
    def step_list(self) -> StepCardList:
        """批次卡片：控制器把流程事件画在这里。"""
        return self._step_list

    # ---------------- "新消息自动弹出" ----------------

    def _load_auto_show(self) -> bool:
        """读开关的持久化状态。没设置过/没给设置对象 → 默认开。"""
        if self._settings is None:
            return True
        value = str(self._settings.get(_KEY_AUTO_SHOW) or "").strip()
        return value != "0"  # 只有明确写 0 才算关，其余一律开

    def _on_auto_show_toggled(self, checked: bool) -> None:
        """开关变化即落盘（数字/布尔一律字符串，项目约定）。"""
        if self._settings is not None:
            self._settings.set(_KEY_AUTO_SHOW, "1" if checked else "0")

    # ---------------- 日志接收与显示 ----------------

    def _append_line(self, level: str, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["info"])
        # 消息里的 <>& 必须转义，防止内容被当成 HTML 吃掉
        safe = html.escape(text)
        self._log_view.appendHtml(
            f'<span style="color:#888">{ts}</span> '
            f'<span style="color:{color}">{safe}</span>')

        # 控制台整个被关掉（self 不可见）→ 弹出并跳到运行日志页。
        # 只发信号不动停靠窗——停靠窗是 MainWindow 建的，谁建谁管
        if self._auto_show.isChecked() and not self.isVisible():
            self._tabs.setCurrentIndex(0)
            self.show_requested.emit()

        # 当前不在"运行日志"标签页 → 挂红点提醒有新消息
        if self._tabs.currentIndex() != 0 and not self._log_dirty:
            self._log_dirty = True
            self._tabs.setTabText(0, "● 运行日志")

    def _on_tab_changed(self, index: int) -> None:
        """切到"运行日志"即视为已读：摘掉红点。"""
        if index == 0 and self._log_dirty:
            self._log_dirty = False
            self._tabs.setTabText(0, "运行日志")
