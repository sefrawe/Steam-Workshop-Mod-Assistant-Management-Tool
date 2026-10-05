"""卸载与清理功能模块
"""
"""
gui/uninstallPage.py · 左导航「分步向导」组：绿色软件的"反安装"页。

本软件是便携形态：账本、设置、日志、会话记忆全住在软件自己的文件夹
里（data/ 与 config/，定位单源 core/appPaths）。所以"卸载"只有两个
动作：资源管理器里删掉整个文件夹（一步到位），以及（仅旧版本用户）
清一下注册表里的界面记忆键。

本页不执行任何删除——它做三件事：
  ① 盘点软件文件夹：把 data/ 与 config/ 逐文件列出，每个文件给一句
    "它是什么"（文案单源 workflows/uninstallFlow.describe_file，
    界面显示与引擎测试共用同一份）。纯只读；
  ② 会话记忆指路：窗口大小、面板显隐这类界面记忆存在哪个文件、
    还在不在——指给用户看；
  ③ 注册表指路：旧版本留的键现在还有没有、路径在哪。删不删用户
    拍板，本软件绝不代删（与"备份位置指针不静默自愈"同一哲学：
    说清楚，让用户决定）。

零引擎新写：盘点走 workflows/uninstallFlow.scan_folder（引擎原样
迁入 V2，本轮已落树），注册表与会话的判断走 gui/sessionStore 的
现成函数。本页只管呈现与指路。

本页与游戏档案无关：不定义 set_game（广播循环按 hasattr 自动跳过，
零接线成本）。盘点只 stat 文件元数据不读内容，毫秒级，主线程直调
（与退场盘点 survey 同一口径，不值得开线程）。
"""
from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QScrollArea, QTableWidget, QVBoxLayout, QWidget, QTableWidgetItem,
)

from core import appPaths
from core.appSettings import AppSettings
from core.formatters import fmt_size
from gui.consolePanel import LogBus
from gui.sessionStore import (
    registry_has_keys, registry_key_display, session_file_exists,
    session_file_path,
)
from workflows.uninstallFlow import scan_folder

# 与其余模块页一致的配色，按用途命名
_C_OK = "#46a758"     # 一切正常（干净 / 键不存在）
_C_WARN = "#f5a623"   # 需要注意（有残留键）
_C_MUTED = "#8a8a8f"  # 说明文字


class UninstallPage(QWidget):
    """「卸载与清理」模块页：盘点 + 两张指路卡，全程只读零删除。"""

    # 打开软件所在目录：转主窗口既有落点（与文件菜单同一个入口，
    # 不在页内另写一份路径推导）
    open_software_dir_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo          # 保留统一构造签名（本页不读库）
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._build_ui()
        self.refresh()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 整页装进滚动区（与其余模块页同款）：窗口矮就出滚动条
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)
        self._cards_host = root  # 卡片宿主布局（_make_card 往里放）
        title = QLabel("卸载与清理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"）
        note = QFrame(self)
        note.setObjectName("uninstall_note")
        note.setStyleSheet(
            "QFrame#uninstall_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：本页只盘点与指路，不删任何东西。真要卸载——① 在"
            "资源管理器里删掉整个软件文件夹（账本、设置、日志都在"
            "里面，删了就是彻底卸载）→ ② 看第③卡的注册表残留，想清"
            "就手动删那个键。steamcmd、工坊内容、备份区是你的资产，"
            "本软件的卸载不碰它们。",
            "为什么这么做：绿色软件的卸载就该这么简单——数据不散落、"
            "注册表不留尸。本页把\"会留下什么\"提前列给你看，删之前"
            "心里有数。",
            "术语与关系：data\\ = 账本与日志的家；config\\ = 设置与"
            "界面记忆的家；会话记忆 = 窗口大小、面板显隐这类\"软件\""
            "记得住\"的状态；注册表键 = 旧版本留下的界面记忆（新版\""
            "已搬进 data\\，旧键只是残留）。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # ---- 卡①：软件文件夹盘点 ----
        self._d1, box1 = self._make_card("step1", "① 软件文件夹里有什么（只读盘点）")
        row1 = QHBoxLayout()
        self._btn_scan = QPushButton("盘点软件文件夹", self)
        self._btn_scan.setToolTip(
            "把 data\\ 与 config\\ 里的文件逐个列出，每个文件给一句"
            "\"它是什么\"。纯只读——不写、不删、不改任何东西")
        self._btn_scan.clicked.connect(self._on_inventory)
        row1.addWidget(self._btn_scan)
        row1.addStretch(1)
        box1.addLayout(row1)

        # 盘点结果表：文件 / 大小 / 它是什么（三列，说明列拉伸）
        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(["文件", "大小", "它是什么"])
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self._table.setMinimumHeight(180)
        self._table.setVisible(False)  # 没盘点前不占地方
        box1.addWidget(self._table)
        self._d1_total = QLabel("", self)
        self._d1_total.setStyleSheet(f"border:none; color:{_C_MUTED};")
        self._d1_total.setVisible(False)
        box1.addWidget(self._d1_total)

        # ---- 卡②：会话记忆指路 ----
        self._d2, box2 = self._make_card("step2", "② 会话记忆（界面状态存哪）")
        row2 = QHBoxLayout()
        self._btn_session_dir = QPushButton("打开所在文件夹", self)
        self._btn_session_dir.setToolTip(
            "在文件管理器打开会话记忆文件所在的文件夹（搬软件文件夹时"
            "它随行，删文件夹时它随之消失）")
        self._btn_session_dir.clicked.connect(self._open_session_dir)
        row2.addWidget(self._btn_session_dir)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ---- 卡③：注册表指路 ----
        self._d3, _ = self._make_card("step3", "③ 注册表残留（仅旧版本用户）")

        root.addStretch(1)

    # ---------- 进页钩子（_on_nav_changed 自动调，hasattr 自发现） ----------
    def refresh(self) -> None:
        """进页现查②③卡状态：注册表键可能被用户手动删了、会话文件
        可能刚生成——都是毫秒级读，不值得缓存。"""
        # 卡②：会话文件路径 + 在不在
        path = session_file_path()
        exists = session_file_exists()
        self._set_card(
            self._d2,
            f"界面记忆存在：{path}\n"
            + ("（文件已生成）" if exists
               else "（还没有这个文件——首次改动面板布局/窗口大小后才创建）"),
            _C_MUTED)
        # 卡③：注册表键路径 + 有无残留
        key_path = registry_key_display()
        if registry_has_keys():
            self._set_card(
                self._d3,
                f"注册表里还留着界面记忆键：{key_path}\n"
                "删除软件文件夹不会清它们——想清就打开注册表编辑器"
                "（Win+R 输入 regedit）删掉上面这个键；不删也不影响"
                "任何功能，只是残留。本软件不代删。",
                _C_WARN)
        else:
            self._set_card(
                self._d3,
                f"注册表里没有本软件的键（{key_path}）——干净，无需处理。",
                _C_OK)

    # ---------- 卡①动作 ----------
    def _on_inventory(self) -> None:
        """盘点 data\\ 与 config\\：引擎 scan_folder 毫秒级（只 stat
        元数据），主线程直调。表清空重填，重复点无害。"""
        report = scan_folder(appPaths.app_root())
        t = self._table
        t.setRowCount(len(report.entries))
        for i, e in enumerate(report.entries):
            t.setItem(i, 0, QTableWidgetItem(e.rel_path))
            t.setItem(i, 1, QTableWidgetItem(fmt_size(e.size_bytes)))
            t.setItem(i, 2, QTableWidgetItem(e.role))
        t.setVisible(True)
        self._d1_total.setVisible(True)
        if not report.entries:
            self._set_card(
                self._d1,
                "data\\ 与 config\\ 都不存在——软件数据已是干净状态，"
                "删除程序文件夹即完成卸载。",
                _C_OK)
            self._d1_total.setText("")
            return
        self._set_card(
            self._d1,
            f"共 {len(report.entries)} 个文件。这只是预演——真正删除"
            " = 资源管理器里删掉整个软件文件夹，一步到位。",
            _C_MUTED)
        self._d1_total.setText(
            f"合计：{fmt_size(report.total_bytes)}"
            f"（data\\ {'存在' if report.data_dir_exists else '不存在'}、"
            f"config\\ {'存在' if report.config_dir_exists else '不存在'}）")
        if self._log is not None:
            self._log.info(
                f"卸载盘点：{len(report.entries)} 个文件，"
                f"合计 {fmt_size(report.total_bytes)}")

    # ---------- 卡②动作 ----------
    def _open_session_dir(self) -> None:
        """打开会话记忆文件所在的文件夹（页内自理——appPaths 与
        sessionStore 都是单源，不需要绕主窗口）。"""
        path = session_file_path().parent
        if not path.exists():
            # 首次运行且没动过布局时可能还没生成——说明而不是静默失败
            if self._log is not None:
                self._log.info(f"文件夹还不存在（暂无会话记忆）：{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            # openUrl 失败是静默的（老坑）：手动兜底提示
            if self._log is not None:
                self._log.info(f"文件管理器没有响应，请手动打开：{path}")

    # ---------- 卡片基建（与其余模块页同款） ----------
    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张卡，返回（状态行, 内容布局）。样式选择器限定到本框
        （QFrame#card_xxx）：QLabel 也是 QFrame 子类，不限定会把边框
        画到卡里每行字上——既有页面的同款处理。"""
        frame = QFrame(self)
        frame.setObjectName(f"card_{name}")
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        detail = QLabel("", frame)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"border:none; color:{_C_MUTED};")
        box.addWidget(title_lbl)
        box.addWidget(detail)
        # 卡片直接进页面滚动区（本页不用 _cards_box 聚合）
        self._cards_host.addWidget(frame)
        return detail, box

    def _set_card(self, detail: QLabel, text: str, color: str) -> None:
        detail.setText(text)
        detail.setStyleSheet(f"border:none; color:{color};")
