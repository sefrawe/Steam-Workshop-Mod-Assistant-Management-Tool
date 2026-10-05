"""备份总览页
"""
"""
gui/backupOverviewPage.py · 跨档案的备份大盘（只读 + 轻量清理）。

分工：备份页管"当前档案"的备份全流程（备份/恢复/重定位/搬家/
清理）；本页是跨档案大盘 + 轻量清理——失效、软删除 mod 名下的
备份也照列（恢复场景正需要看见它们）。

交互模型（与备份页同一套，学会一个就会两个）：
- 操作对象一律来自「选」列勾选框，勾选记备份 id 不记行号；
- 「对选中」下拉 + 表格右键菜单（同一组 QAction，两个入口）：
  钉住/取消钉住（一个事务批量改，可逆）；删登记…（只删账，盘上
  文件原样保留）；删除登记并删除文件…（先盘后账，后台线程逐份
  执行，某份失败不影响其余；失联记录如实报告"仅删除了登记"——
  这正是批量清理失联登记的正规通道）；打开所在文件夹（恰好勾
  1 份）；全选/清空选择；
- 表头点击排序：显示可读文本、比较用库内原值；排序选择记住；
- 档案下拉与「只看失联」两个筛选 + 即时搜索（不命中的行隐藏，
  已勾选的原样保留）；
- 恢复不在本页：恢复前要自动备份当前版本、还要盯下载目录，归
  备份页管——本页只读大盘 + 清理。

线程规矩（与备份页同一套）：批量删文件后台线程逐份执行；worker
引用只在 finished 收尾函数里释放；删除没有停止点，等它跑完；
shutdown() 供主窗口退出统一调用。只与 ModRepository /
BackupManager 接口交互，零 SQL。
"""

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal,QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMenu, QMessageBox, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QCheckBox, QLineEdit, QScrollArea
)

from core.backupManager import BackupManager
from core.modRepository import BackupOverviewRow
from core.models import Game
from gui.logBus import LogBus
from core.formatters import abs_time, fmt_size, status_zh
from core.urlParser import WORKSHOP_URL_TEMPLATE
from gui.theme import font_px  # 字号单源（D25）

# 与设置页核对过的真键名（与 backupPage 相同三件；改键名两处一起动）
_KEY_KEEP_PER_MOD = "backup_keep_per_mod"
_KEY_QUOTA_GB = "backup_total_quota_gb"
_KEY_STEAMCMD = "steamcmd_path"

_C_OK = "#46a758"
_C_FAIL = "#e5484d"

# 列号显式起名（与备份页同一纪律），填充代码一律用列号
COL_CHECK = 0
COL_GAME = 1
COL_MOD_ID = 2
COL_TITLE = 3
COL_URL = 4
COL_VER_TIME = 5
COL_SIZE = 6
COL_DISK = 7
COL_PIN = 8
COL_CREATED = 9
COL_STATUS = 10
_COL_HEADERS = ["选", "游戏", "Mod 编号", "标题", "↗", "备份版本",
                "大小", "盘上", "钉", "登记时间", "mod 状态"]

# 盘上列的排序次序：正常的在前、失联居中、没档案垫底
_DISK_RANK = {"✓": 0, "失联": 1, "—": 2}


class _SortItem(QTableWidgetItem):
    """带数值排序键的单元格：显示文本给人看，比较大小用库内原值。

    与备份页同款——点表头排序默认按显示文本比，"9.9 MiB" 会排到
    "13.7 MiB" 后面；挂上原始数值后排序才符合直觉。
    """

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem):
            return self._key < other._key
        return super().__lt__(other)


class _BatchWorker(QThread):
    """后台批量删除线程（与备份页同款）：逐份调引擎 delete_backup
    （先删盘上目录、成功才删记录）。每份独立——某份失败不影响其余。
    删除没有停止点，所以没有停止协议；shutdown() 会等它跑完。
    """

    one_done = Signal(int, int, bool, str)   # 已完成数, 总数, 成功?, 一行摘要
    all_done = Signal(list)                  # [(备份id, 成功?, 说明)]
    crashed = Signal(str)                    # 预期外异常（bug 性质）

    def __init__(self, manager: BackupManager,
                 recs: list[BackupOverviewRow]) -> None:
        super().__init__()
        self._manager = manager
        self._recs = list(recs)

    def run(self) -> None:
        results: list[tuple[int, bool, str]] = []
        total = len(self._recs)
        for i, rec in enumerate(self._recs, 1):
            try:
                ok, msg = self._manager.delete_backup(rec.backup_id)
            except Exception as exc:  # 到这里=bug，不是操作层失败
                self.crashed.emit(
                    f"删除备份 {rec.backup_id}（mod {rec.mod_id}）"
                    f"时出现预期外错误：{exc}")
                return
            line = (f"[{rec.game_name}] 备份 {rec.backup_id}"
                    f"（mod {rec.mod_id}）："
                    + (msg if msg else ("已删除" if ok else "失败")))
            results.append((rec.backup_id, ok, line))
            self.one_done.emit(i, total, ok, line)
        self.all_done.emit(results)


class BackupOverviewPage(QWidget):
    """备份总览页。refresh() 供主窗口在切到本页时调用。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._rows: list[BackupOverviewRow] = []      # 当前显示的行
        self._row_by_id: dict[int, BackupOverviewRow] = {}  # 勾选记 id 不记行号
        self._disk: list[str] = []                    # 与 _rows 平行的盘上三态
        self._games: dict[int, Game] = {}
        self._games_in_order: list[Game] = []         # 组合框保序用
        self._filter_game: int | None = None          # None = 全部档案
        self._sort_state = (COL_CREATED, Qt.SortOrder.DescendingOrder)
        self._building = False   # 重建表格期间抑制 itemChanged
        self._busy = False
        # 搜索防抖（300ms，与更新对照页同一手感）
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)
        self._batch_worker: _BatchWorker | None = None

        self._build_ui()
        self._reload()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)
        title = QLabel("备份总览", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "跨档案查看全部备份登记；删不删、删账还是连文件一起删，"
            "在这里看清楚再决定。\n"
            "「盘上」三态：✓ 在盘上｜失联 盘上已找不到（多半备份目录改过位置"
            "或已手动删除）｜— 该档案未设备份目录（不算失联）。\n"
            "软删除、失效 mod 名下的备份也照列在这里——清账不等于备份消失，"
            "想找回旧版本时正需要它们。\n"
            "与备份管理页的分工：备份/恢复/重定位/搬家在备份页（按当前档案）；"
            "这里是跨档案的大盘与批量清理。操作对象来自「选」列勾选框"
            "（表格上点右键是同一组动作）；点表头可排序。",
            self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # ---- 筛选行：档案 / 只看失联 / 刷新 ----
        filter_row = QWidget(self)
        fh = QHBoxLayout(filter_row)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.addWidget(QLabel("档案：", filter_row))
        self._combo = QComboBox(filter_row)
        # T15 批 3：补 tooltip（决策 22①）
        self._combo.setToolTip(
            "把大盘过滤到某一个游戏档案的备份；「全部档案」看所有")
        self._combo.currentIndexChanged.connect(self._on_filter_changed)
        fh.addWidget(self._combo)
        self._lost_only = self._make_lost_check(filter_row)
        fh.addWidget(self._lost_only)
        self._search = QLineEdit(filter_row)
        self._search.setPlaceholderText("搜索：游戏 / 标题 / 编号…")
        self._search.setClearButtonEnabled(True)
        self._search.setToolTip(
            "即时过滤当前列表：不命中的行隐藏，已勾选的原样保留；\n"
            "匹配游戏名、mod 标题、编号、mod 状态、备份目录名。"
            "清空搜索框恢复全部")
        self._search.textChanged.connect(self._on_search_changed)
        fh.addWidget(self._search, 1)
        fh.addStretch(1)

        self._refresh_btn = QPushButton("刷新", filter_row)
        self._refresh_btn.clicked.connect(self._reload)
        self._refresh_btn.setToolTip("重新读取全部备份登记（会清空当前勾选）")
        fh.addWidget(self._refresh_btn)
        root.addWidget(filter_row)

        # ---- 工具行：对选中 ▾（与右键菜单同一组 QAction）----
        bar = QWidget(self)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn_sel_menu = QToolButton(bar)
        self._btn_sel_menu.setText("对选中")
        self._btn_sel_menu.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_sel_menu.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        # T15 批 3：按钮本体也补 tooltip（决策 22①；菜单项各有各的）
        self._btn_sel_menu.setToolTip(
            "对勾选的备份执行动作；菜单项与表格右键是同一组动作")
        m_sel = QMenu(self._btn_sel_menu)
        m_sel.setToolTipsVisible(True)  # QMenu 默认不显示悬浮说明，必须显式开
        self._act_check_all = QAction("全选", m_sel)
        self._act_check_all.triggered.connect(
            lambda: self._set_all_checks(True))
        m_sel.addAction(self._act_check_all)
        self._act_check_none = QAction("清空选择", m_sel)
        self._act_check_none.triggered.connect(
            lambda: self._set_all_checks(False))
        m_sel.addAction(self._act_check_none)
        m_sel.addSeparator()
        self._act_pin = QAction("钉住", m_sel)
        self._act_pin.setToolTip("勾选的备份全部钉住（豁免自动清理，可随时取消）")
        self._act_pin.triggered.connect(lambda: self._pin_checked(True))
        m_sel.addAction(self._act_pin)
        self._act_unpin = QAction("取消钉住", m_sel)
        self._act_unpin.setToolTip("勾选的备份全部取消钉住")
        self._act_unpin.triggered.connect(lambda: self._pin_checked(False))
        m_sel.addAction(self._act_unpin)
        m_sel.addSeparator()
        self._act_del_rec = QAction("删登记…（只删账）", m_sel)
        self._act_del_rec.setToolTip(
            "只删除数据库登记，磁盘文件原样保留"
            "（之后成为没有登记的普通文件夹，可随时手动删除）")
        self._act_del_rec.triggered.connect(self._delete_records)
        m_sel.addAction(self._act_del_rec)
        self._act_del_file = QAction("删除登记并删除文件…", m_sel)
        self._act_del_file.setToolTip(
            "先删磁盘目录、成功才删登记，不可撤销；逐份独立执行，"
            "某份失败不影响其余；失联记录会如实报告「仅删除了登记」")
        self._act_del_file.triggered.connect(self._delete_files)
        m_sel.addAction(self._act_del_file)
        m_sel.addSeparator()
        self._act_open_folder = QAction("打开所在文件夹（恰好勾 1 份）", m_sel)
        self._act_open_folder.setToolTip(
            "在资源管理器中打开勾选的那一份备份所在（或所属）的文件夹")
        self._act_open_folder.triggered.connect(self._open_selected_folder)
        m_sel.addAction(self._act_open_folder)
        self._btn_sel_menu.setMenu(m_sel)
        h.addWidget(self._btn_sel_menu)
        h.addStretch(1)
        root.addWidget(bar)

        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        # ---- 表格 ----
        self._table = QTableWidget(0, len(_COL_HEADERS), self)
        self._table.setHorizontalHeaderLabels(_COL_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_CHECK, 36), (COL_GAME, 130), (COL_MOD_ID, 100),
                           (COL_URL, 36), (COL_VER_TIME, 150), (COL_SIZE, 90),
                           (COL_DISK, 60), (COL_PIN, 40), (COL_CREATED, 150),
                           (COL_STATUS, 90)):
            self._table.setColumnWidth(col, width)
        # 重建表格的时序纪律：建表期关排序，_fill_table 末尾灌完数据再开
        self._table.setSortingEnabled(False)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        header.sortIndicatorChanged.connect(self._on_sort_changed)
        # 右键菜单：与「对选中 ▾」共用同一组动作
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        root.addWidget(self._count_label)
        self._refresh_op_buttons()

    def _make_lost_check(self, parent: QWidget) -> QCheckBox:
        """「只看失联」勾选框：切换时只重填表格（档案组合不用动）。"""
        box = QCheckBox("只看失联", parent)
        # T15 批 3：补 tooltip（决策 22①）
        box.setToolTip(
            "只显示「盘上」列为失联的备份登记（批量清理失联记录时先勾上它）")
        box.toggled.connect(lambda _checked: self._fill_table())
        return box

    # ---------- 数据 ----------
    def _reload(self) -> None:
        self._games = {g.app_id: g for g in self._repo.list_games()}
        self._games_in_order = list(self._repo.list_games())
        self._refresh_combo()
        self._fill_table()

    def _refresh_combo(self) -> None:
        """重建档案下拉并尽量保住当前筛选（列表可能增减）。"""
        self._combo.blockSignals(True)
        try:
            self._combo.clear()
            self._combo.addItem("全部档案", None)
            found = False
            for g in self._games_in_order:
                self._combo.addItem(f"{g.name}（{g.app_id}）", g.app_id)
                if g.app_id == self._filter_game:
                    found = True
            if not found:
                self._filter_game = None  # 档案被删了 → 静默回到"全部"
            idx = self._combo.findData(self._filter_game)
            self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._combo.blockSignals(False)

    def _on_filter_changed(self) -> None:
        self._filter_game = self._combo.currentData()
        self._fill_table()

    def _disk_state(self, r: BackupOverviewRow) -> str:
        """盘上三态，与备份页同一把尺（账本存相对 backup_dir 的目录名）。"""
        g = self._games.get(r.game_id)
        bdir = (g.backup_dir or "").strip() if g else ""
        if not bdir:
            return "—"
        return "✓" if (Path(bdir) / r.backup_path).is_dir() else "失联"

    def _fill_table(self) -> None:
        """重建表格。时序纪律与备份页相同：先关排序 → 灌数据 → 再开。"""
        self._building = True
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._row_by_id = {}
        rows = self._repo.list_backups_overview()  # 全部档案，新→旧
        disk = [self._disk_state(r) for r in rows]
        pairs = list(zip(rows, disk))
        if self._filter_game is not None:
            pairs = [p for p in pairs if p[0].game_id == self._filter_game]
        if self._lost_only.isChecked():
            pairs = [p for p in pairs if p[1] == "失联"]
        self._rows = [r for r, _ in pairs]
        self._disk = [d for _, d in pairs]
        self._row_by_id = {r.backup_id: r for r in self._rows}
        self._table.setRowCount(len(self._rows))
        for row, (r, d) in enumerate(zip(self._rows, self._disk)):
            self._fill_row(row, r, d)
        # 数据灌完再开排序，并按记住的列/方向排一次
        self._table.setSortingEnabled(True)
        col, order = self._sort_state
        if col >= len(_COL_HEADERS) or col == COL_CHECK:
            col, order = COL_CREATED, Qt.SortOrder.DescendingOrder
        self._table.sortItems(col, order)
        # 统计行
        total = sum(r.size_bytes or 0 for r in self._rows)
        lost = sum(1 for d in self._disk if d == "失联")
        text = f"共 {len(self._rows)} 份备份登记，合计 {fmt_size(total)}"
        if lost:
            text += f"，其中失联 {lost} 份"
        if not self._rows:
            text += "（还没有任何备份登记）"
        self._count_label.setText(text)
        self._building = False
        self._refresh_op_buttons()
        self._apply_filter()

    def _fill_row(self, row: int, r: BackupOverviewRow, d: str) -> None:
        """填一行数据。数值列全部用 _SortItem：显示可读文本、排序按原始值。"""
        # 勾选列：UserRole 存备份 id——排序/重建后靠它找回记录
        it = QTableWidgetItem()
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable)
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, r.backup_id)
        self._table.setItem(row, COL_CHECK, it)

        self._table.setItem(row, COL_GAME, _SortItem(r.game_name, r.game_name))
        self._table.setItem(row, COL_MOD_ID, _SortItem(str(r.mod_id), r.mod_id))
        title_item = _SortItem(r.mod_title or "（无标题）", r.mod_title or "")
        title_item.setToolTip(f"备份目录名：{r.backup_path}")
        self._table.setItem(row, COL_TITLE, title_item)

        # ↗ 列：总览行没有 url 字段，按模板拼（工坊页地址模板单源）。
        # UserRole 存网址；UserRole+1 再存一份 mod 编号——点击打开后
        # 写日志用（T15 批 3）
        url_item = QTableWidgetItem("↗")
        url_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        url_item.setForeground(QBrush(QColor("#5aa9ff")))  # 蓝字提示可点
        url_item.setData(Qt.ItemDataRole.UserRole,
                         WORKSHOP_URL_TEMPLATE.format(r.mod_id))
        url_item.setData(Qt.ItemDataRole.UserRole + 1, r.mod_id)
        url_item.setToolTip("点击在浏览器打开该 mod 的创意工坊页面")
        self._table.setItem(row, COL_URL, url_item)

        self._table.setItem(
            row, COL_VER_TIME,
            _SortItem(abs_time(r.version_timeupdated), r.version_timeupdated or 0))
        self._table.setItem(
            row, COL_SIZE, _SortItem(fmt_size(r.size_bytes), r.size_bytes or 0))
        disk_item = _SortItem(d, _DISK_RANK.get(d, 2))
        if d == "失联":
            disk_item.setForeground(QBrush(QColor(_C_FAIL)))
        elif d == "✓":
            disk_item.setForeground(QBrush(QColor(_C_OK)))
        self._table.setItem(row, COL_DISK, disk_item)
        self._table.setItem(
            row, COL_PIN, _SortItem("是" if r.pinned else "", 1 if r.pinned else 0))
        self._table.setItem(
            row, COL_CREATED, _SortItem(abs_time(r.created_at), r.created_at or 0))
        self._table.setItem(row, COL_STATUS,
                            _SortItem(status_zh(r.mod_status), r.mod_status))

    # ---------- 勾选与按钮状态 ----------
    def _checked(self) -> list[BackupOverviewRow]:
        """勾选的备份记录（按表格当前显示顺序）。

        通过条目上的"可勾选"标记识别数据行；记录本体用 id 从
        _row_by_id 取，不按行号反查——排序/重建后行号会漂，id 不会。
        """
        out: list[BackupOverviewRow] = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, COL_CHECK)
            if it is None:
                continue
            if not (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if it.checkState() == Qt.CheckState.Checked:
                rec = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
                if rec is not None:
                    out.append(rec)
        return out

    def _refresh_op_buttons(self) -> None:
        """按当前勾选数刷新"对选中"组动作的可用状态与按钮文字。

        菜单项与右键菜单共用同一组 QAction，改这里两处同时生效。
        """
        n = 0 if self._building else len(self._checked())
        has = (n > 0) and not self._busy
        single = (n == 1) and not self._busy
        self._act_pin.setEnabled(has)
        self._act_unpin.setEnabled(has)
        self._act_del_rec.setEnabled(has)
        self._act_del_file.setEnabled(has)
        self._act_open_folder.setEnabled(single)
        self._btn_sel_menu.setText(f"对选中({n})" if n else "对选中")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._combo.setEnabled(not busy)
        self._lost_only.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)
        self._btn_sel_menu.setEnabled(not busy)
        self._refresh_op_buttons()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        # 重建表格期间 setItem 会触发大量 itemChanged，全部忽略；
        # 只有关用户点勾选框时才需要刷新按钮状态
        if self._building:
            return
        if item.column() == COL_CHECK:
            self._refresh_op_buttons()

    def _set_all_checks(self, on: bool) -> None:
        self._building = True
        # 批量改勾选状态，别每行都刷一遍按钮。
        # 全选只勾可见行：搜索过滤在场时，勾进看不见的行=盲删隐患；
        # 清空选择仍作用所有行（只往安全方向走）
        for r in range(self._table.rowCount()):
            if on and self._table.isRowHidden(r):
                continue
            it = self._table.item(r, COL_CHECK)
            if it is not None and (it.flags()
                                   & Qt.ItemFlag.ItemIsUserCheckable):
                it.setCheckState(
                    Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._building = False
        self._refresh_op_buttons()

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """↗ 列：点单元格打开工坊页面（网址存条目数据里，
        排序后也不会错位）。

        T15 批 3：openUrl 失败是静默的（不弹系统错误，v2.18 追记
        同款教训），必须自己兜底说一声；成功也写一行日志（决策 22③）。
        """
        if col != COL_URL:
            return
        item = self._table.item(row, col)
        if item is None:
            return
        url = item.data(Qt.ItemDataRole.UserRole)
        if not url:
            return
        mod_id = item.data(Qt.ItemDataRole.UserRole + 1)
        if QDesktopServices.openUrl(QUrl(url)):
            self._log.info(f"已在浏览器打开 mod {mod_id} 的创意工坊页面")
        else:
            QMessageBox.warning(
                self, "打开工坊页面",
                f"系统没有响应打开请求：\n{url}\n"
                "可复制上面的网址，粘到浏览器地址栏打开。")

    def _on_sort_changed(self, col: int, order: Qt.SortOrder) -> None:
        # 记住用户的排序选择，刷新/重载后照旧。
        # 排序搬的是条目、行隐藏挂的是行号——排序后重滤一次对齐
        self._sort_state = (col, order)
        self._apply_filter()
    def _on_search_changed(self, _text: str) -> None:
        """搜索框防抖：停手 300ms 才真正过滤。"""
        self._search_timer.start()

    def _apply_filter(self) -> None:
        """把搜索框关键词落到表格：不命中的行隐藏（只隐藏不重建，
        勾选原样保留）。匹配游戏名 / mod 标题 / 编号 / mod 状态 /
        备份目录名；空关键词 = 全部显示。"""
        kw = self._search.text().strip().casefold()
        for r in range(self._table.rowCount()):
            it = self._table.item(r, COL_CHECK)
            if it is None or not (it.flags()
                                  & Qt.ItemFlag.ItemIsUserCheckable):
                self._table.setRowHidden(r, False)
                continue
            rec = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
            if rec is None:
                self._table.setRowHidden(r, bool(kw))
                continue
            hay = " ".join((
                rec.game_name, rec.mod_title or "", str(rec.mod_id),
                status_zh(rec.mod_status), rec.backup_path or "",
            )).casefold()
            self._table.setRowHidden(r, bool(kw) and kw not in hay)

    def _on_context_menu(self, pos) -> None:
        """表格右键菜单：与「对选中 ▾」完全同一组 QAction。"""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction(self._act_check_all)
        menu.addAction(self._act_check_none)
        menu.addSeparator()
        menu.addAction(self._act_pin)
        menu.addAction(self._act_unpin)
        menu.addSeparator()
        menu.addAction(self._act_del_rec)
        menu.addAction(self._act_del_file)
        menu.addSeparator()
        menu.addAction(self._act_open_folder)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ---------- 危险动作的统一确认框 ----------
    def _confirm_batch(self, recs: list[BackupOverviewRow], title: str,
                       what: str) -> bool:
        """把"将动多少份、合计多大、都涉及哪些"摆出来再让用户拍板。

        默认按钮是"否"——回车不会误触发。
        """
        total = sum(r.size_bytes or 0 for r in recs)
        pin_n = sum(1 for r in recs if r.pinned)
        lines = [f"对 {len(recs)} 份备份执行：{title}（合计 {fmt_size(total)}）"]
        if pin_n:
            lines.append("其中部分已钉住——钉住只豁免自动清理，不挡手动删除。")
        lines.append(what)
        lines.append("")
        lines.append("涉及：")
        for r in recs[:8]:
            lines.append(
                f" · [{r.game_name}] {r.mod_id} {r.mod_title or '（无标题）'}｜"
                f"{abs_time(r.version_timeupdated)}｜{fmt_size(r.size_bytes)}")
        if len(recs) > 8:
            lines.append(f" ……以及另外 {len(recs) - 8} 份")
        ret = QMessageBox.question(
            self, title, "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ret == QMessageBox.StandardButton.Yes

    # ---------- 批量动作 ----------
    def _pin_checked(self, pinned: bool) -> None:
        """批量钉住/取消钉住：纯账目、可逆，不打扰确认。

        整批包进一个事务——要么全改要么全不动。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        try:
            with self._repo.transaction():
                for rec in recs:
                    self._repo.set_pinned(rec.backup_id, pinned)
        except Exception as exc:
            QMessageBox.critical(self, "钉住", f"失败（账未改动）：{exc}")
            return
        word = "已钉住" if pinned else "已取消钉住"
        self._log.ok(f"{word} {len(recs)} 份备份登记")
        self._reload()

    def _delete_records(self) -> None:
        """批量删登记：只删数据库记录，盘上文件原样保留。

        删完后这些文件成了"账外之物"，本工具不再跟踪——确认框里
        必须把这话说清。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        if not self._confirm_batch(
                recs, "删登记",
                "只删除备份登记（数据库记录），磁盘上的备份目录原样保留；\n"
                "之后本工具不再跟踪这些文件，清理请自行到资源管理器操作。"):
            return
        try:
            with self._repo.transaction():
                for rec in recs:
                    self._repo.delete_backup_record(rec.backup_id)
        except Exception as exc:
            QMessageBox.critical(self, "删登记", f"失败（账未改动）：{exc}")
            return
        self._log.ok(f"已删除 {len(recs)} 条备份登记（盘上文件保留）")
        self._reload()

    def _delete_files(self) -> None:
        """批量"删登记并删文件"（先盘后账）：后台线程逐份执行，每份
        独立——某份失败（路径保险丝拦下/权限问题）不影响其余。

        失联记录也放开勾选：盘上没有东西可删时引擎如实返回
        "仅删除了登记"，这正是批量清理失联登记的通道。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        if not self._confirm_batch(
                recs, "删除登记并删除文件",
                "将逐份删除磁盘目录和数据库记录，不可撤销；逐份独立执行，"
                "某份失败不影响其余。\n"
                "盘上已不在的失联记录：没有东西可删，将只删除登记。"):
            return
        self._progress.setRange(0, len(recs))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._set_busy(True)
        self._batch_worker = _BatchWorker(self._make_manager(), recs)
        self._batch_worker.one_done.connect(self._on_batch_one_done)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.crashed.connect(self._on_batch_crashed)
        self._batch_worker.finished.connect(self._on_batch_finished)
        self._batch_worker.start()
        self._log.info(f"开始删除 {len(recs)} 份备份（磁盘目录+记录）…")

    def _on_batch_one_done(self, done: int, total: int, ok: bool,
                           line: str) -> None:
        self._progress.setValue(done)
        (self._log.ok if ok else self._log.warn)(
            f"删除进度 {done}/{total}：{line}")

    def _on_batch_all_done(self, results: list) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        oks = [r for r in results if r[1]]
        bads = [r for r in results if not r[1]]
        self._log.ok(f"删除结束：成功 {len(oks)}，失败 {len(bads)}")
        if bads:
            QMessageBox.warning(
                self, "删除登记并删除文件",
                f"成功 {len(oks)}，失败 {len(bads)}\n"
                + "\n".join(r[2] for r in bads[:10]))
        self._reload()

    def _on_batch_crashed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        self._log.error(message)
        QMessageBox.critical(self, "备份总览", message)

    def _on_batch_finished(self) -> None:
        w = self._batch_worker
        self._batch_worker = None
        if w is not None:
            w.wait()

    def _open_selected_folder(self) -> None:
        """恰好勾 1 份时可用。定位到那份备份的文件夹；盘上不在时
        退而打开档案的备份目录（总比没反应强），再不行就明说。
        """
        recs = self._checked()
        if self._busy or len(recs) != 1:
            return
        rec = recs[0]
        g = self._games.get(rec.game_id)
        bdir_text = (g.backup_dir or "").strip() if g else ""
        if not bdir_text:
            QMessageBox.information(
                self, "打开所在文件夹",
                f"{rec.game_name} 未设置备份目录。")
            return
        target = Path(bdir_text) / rec.backup_path
        folder = target if target.is_dir() else Path(bdir_text)
        if not folder.exists():
            QMessageBox.information(self, "打开所在文件夹",
                                    f"盘上找不到：{folder}")
            return
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._log.info(f"已打开文件夹：{folder}")
        else:
            # openUrl 失败是静默的（不弹系统错误），必须自己兜底说一声
            QMessageBox.warning(
                self, "打开所在文件夹",
                f"系统没有响应打开请求：\n{folder}\n"
                "可复制上面的路径，粘到资源管理器地址栏打开。")

    # ---------- 公共小件 ----------
    def _make_manager(self) -> BackupManager:
        # 与 backupPage 同款：配额在设置页以 GB 计（人好填），
        # 引擎以字节计（好比较），换算只在边界做一次
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 3)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 10)
        return BackupManager(
            self._repo, keep_per_mod=keep,
            quota_bytes=(quota_gb * 1024 ** 3) if quota_gb > 0 else None,
            steamcmd_path=self._settings.get(_KEY_STEAMCMD))

    # ---------- 对外（MainWindow / 切换器调用，签名不变）----------
    def refresh(self) -> None:
        """供主窗口在切到本页时调用。"""
        self._reload()

    def set_filter_game(self, app_id: int | None) -> None:
        """跳转入口（删除对话框 overview_requested 的落点）：按档案过滤。"""
        self._filter_game = app_id
        self._reload()

    def shutdown(self) -> None:
        """主窗口退出前调用：删除没有停止点，等它跑完。"""
        if self._batch_worker is not None:
            self._batch_worker.wait()
