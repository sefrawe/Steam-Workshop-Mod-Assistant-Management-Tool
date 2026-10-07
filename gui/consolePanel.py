"""底部控制台面板
"""
"""
底部停靠窗的内容：运行日志 + steamcmd 终端两个标签页。
「下载批次」第三个标签随批次轮搬迁后回补（回补点见 _build_ui 注释）。
只通过 LogBus 信号与 TerminalDock 的公共方法对外交互，
工作线程零控件零 SQL（线程纪律：Qt 跨线程信号自动排队到主线程）。
"""

import html
import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# 再导出：LogBus 本体住在 gui/logBus.py（它原本就定义在本文件里，
# 为了解开"控制台 ↔ 终端"互相 import 的循环才搬的家）。这条 import
# 在本文件里没有直接使用（构造终端时转发的是构造参数），作用是让
# 既有代码的 `from gui.consolePanel import LogBus` 原样可用——
# 搬家不改门牌，别的文件一行不用动。
from gui.logBus import LogBus  # noqa: F401
from gui.terminalDock import TerminalDock
from gui.stepCardList import StepCardList
from gui.theme import log_colors  # 日志四级色 + 时间戳色（主题单源）

# 运行日志按级别着色：色表单源在 gui/theme.log_colors()——深浅主题
# 各一套，亮色档按白底对比度校准（旧硬编码 #d4d4d4 在亮色白底几乎
# 不可见）。键 = LogBus 四个方法约定的级别名，那头改名这里要跟；
# 颜色只管界面显示，与日志文件的级别名各管各的

# "有新消息时弹出控制台"的设置键名。项目约定：设置值一律按字符串存，
# 读的时候和 "0" 比较判断开没开。开关的唯一入口在设置页——日志页里
# 不放第二个开关（两处显示同一个设置、互相不同步的坑不再踩）。
_KEY_AUTO_SHOW = "console_auto_show"


class ConsolePanel(QWidget):
    """控制台停靠窗的内容；show_requested 在需要弹出时发出。"""

    show_requested = Signal()  # 请 MainWindow 把停靠窗拉回屏幕

    def __init__(self, log_bus: LogBus, parent: QWidget | None = None,
                 settings=None) -> None:
        super().__init__(parent)
        self._settings = settings   # AppSettings：读"自动弹出"开关
        self._log_dirty = False     # 运行日志有没看过的新消息时置 True（红点）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)

        # ---- Tab 1：运行日志（只读、自动裁剪到 2000 行）----
        log_page = QWidget(self._tabs)
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(4, 4, 4, 4)

        self._log_view = QPlainTextEdit(log_page)
        self._log_view.setReadOnly(True)
        # "最多 2000 行，超出丢最旧"的全部实现就这一行设置——
        # 防长时间运行内存膨胀，不需要任何手动裁剪代码
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth)
        log_layout.addWidget(self._log_view)

        # 日志页底部一排：清空按钮靠左，弹簧占中间，右侧留白——
        # 与终端页顶栏同拍（按钮一律靠左，视觉才不散）
        bottom_row = QHBoxLayout()
        clear = QPushButton("清空", log_page)
        clear.setToolTip(
            "清空运行日志的显示内容；不影响 operations_log 表"
            "（那是给程序读的操作史，两层分开）")
        clear.setFixedWidth(80)
        clear.clicked.connect(self._log_view.clear)
        bottom_row.addWidget(clear)
        bottom_row.addStretch(1)
        log_layout.addLayout(bottom_row)

        self._tabs.addTab(log_page, "运行日志")

        # ---- Tab 2：steamcmd 终端 ----
        # settings 转发进去，终端靠它读 steamcmd 路径和登录命令；
        # 终端里出现的每个结论由 TerminalDock 自己送到本类监听的
        # LogBus，这里不需要任何额外接线
        self._terminal = TerminalDock(log_bus, settings, self._tabs)
        self._tabs.addTab(self._terminal, "steamcmd 终端")

        # ---- Tab 3：下载批次（批次轮回补，原预留注释照此落地）----
        # 卡片只画界面：停止/继续/行右键动作以信号发出，实现全在
        # batchDownloadController（接线在那边做，本文件不认识控制器）
        self._step_list = StepCardList(self._tabs)
        self._tabs.addTab(self._step_list, "下载批次")

        # 切换标签页时检查要不要摘红点。注意接线顺序：先接 UI 信号、
        # 最后接日志流——保证第一条日志到来时界面已经就绪
        self._tabs.currentChanged.connect(self._on_tab_changed)
        log_bus.line_emitted.connect(self._append_line)

    @property
    def terminal(self) -> TerminalDock:
        """终端实例：MainWindow 做关窗收尾、批次编排接线时取用。"""
        return self._terminal
    @property
    def step_list(self) -> StepCardList:
        """批次步骤卡片实例：批次控制器接线、MainWindow 切标签时取用。"""
        return self._step_list

    def show_batch_tab(self) -> None:
        """切到「下载批次」标签：开批次时 MainWindow 调用，让用户
        第一眼看到进度卡片（红点逻辑只服务运行日志页，本页不用）。"""
        self._tabs.setCurrentWidget(self._step_list)


    # ---------------- "有新消息时弹出控制台" ----------------

    def _auto_show_enabled(self) -> bool:
        """自动弹出开关现读设置：唯一入口 = 设置页同名开关，这里每次
        现读不缓存——设置页改完保存立即生效，不存在第二处状态。
        没有设置对象 = 默认开。"""
        if self._settings is None:
            return True
        return str(self._settings.get(_KEY_AUTO_SHOW) or "").strip() != "0"

    # ---------------- 日志接收与显示 ----------------

    def _append_line(self, level: str, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        colors = log_colors()
        color = colors.get(level, colors["info"])
        ts_color = colors["ts"]
        # 消息里的 <>& 必须转义，防止内容被当成 HTML 吃掉
        safe = html.escape(text)
        self._log_view.appendHtml(
            f'<span style="color:{ts_color}">{ts}</span> '
            f'<span style="color:{color}">{safe}</span>')

    def _on_tab_changed(self, index: int) -> None:
        """切到"运行日志"即视为已读：摘掉红点。"""
        if index == 0 and self._log_dirty:
            self._log_dirty = False
            self._tabs.setTabText(0, "运行日志")
