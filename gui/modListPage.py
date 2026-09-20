"""mod 库页 """
"""
后端调试主战场。
布局：顶部筛选条 + 水平分割（左表格 / 右详情）。
右键菜单：repo 已支持的直接可用；"手动备份"仍是置灰占位；
"获取下载命令"已点亮——发信号给 MainWindow 跳到命令生成页并聚焦该 mod。
"""

from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.models import Game
from gui.modDetailPanel import ModDetailPanel
from gui.modListModel import _SORT_MAP, ModListModel

# 颜色标记的键值：库里存 hex（将来徽标着色直接可用），菜单里显示中文名
_COLORS = {"红": "#e5484d", "橙": "#f76b15", "黄": "#f5d90a", "绿": "#46a758",
           "蓝": "#0091ff", "紫": "#8e4ec6"}


class ModListPage(QWidget):
    # 右键"获取下载命令"时发出，参数 = 要聚焦的 mod id 列表；MainWindow 负责跳转
    command_gen_requested = Signal(list)

    def __init__(self, repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game: Game | None = None
        self._status: str | None = None
        self._search = ""
        self._special_only = False
        self._order_by = "time_updated DESC"
        self._sort_col = 3
        self._selected_mod_id: int | None = None  # reload 后恢复选中
        self._build_ui()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        bar = QWidget(self)
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 6)
        row = QWidget(bar)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)

        self._search_edit = QLineEdit(row)
        self._search_edit.setPlaceholderText("搜索标题 / 备注…")
        self._search_edit.setClearButtonEnabled(True)
        h.addWidget(self._search_edit, 1)

        self._status_combo = QComboBox(row)
        for label, val in (("全部", None), ("已收录", "tracked"), ("已下载", "downloaded"),
                           ("已删除", "deleted"), ("已失败", "failed")):
            self._status_combo.addItem(label, val)  # 显示中文，currentData 仍是库内值
        h.addWidget(self._status_combo)

        self._special_check = QCheckBox("特别关注", row)
        h.addWidget(self._special_check)

        btn = QPushButton("刷新", row)
        btn.clicked.connect(self._reload)
        h.addWidget(btn)

        self._count_label = QLabel("", row)
        h.addWidget(self._count_label)
        bar_layout.addWidget(row)
        root.addWidget(bar)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        self._table = QTableView(split)
        self._model = ModListModel(self._table)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 90), (2, 96), (3, 92), (4, 92), (5, 72),
                           (6, 88), (7, 170), (8, 32), (9, 160)):
            self._table.setColumnWidth(col, width)
        header.setSortIndicator(self._sort_col, Qt.SortOrder.DescendingOrder)
        header.setSortIndicatorShown(True)

        self._detail = ModDetailPanel(split)
        split.addWidget(self._table)
        split.addWidget(self._detail)
        split.setSizes([820, 380])
        root.addWidget(split, 1)

        # ---- 信号 ----
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_edit.textChanged.connect(self._search_timer.start)
        self._status_combo.currentIndexChanged.connect(self._reload)
        self._special_check.toggled.connect(self._reload)
        header.sectionClicked.connect(self._on_header_clicked)
        self._table.selectionModel().currentRowChanged.connect(self._on_current_row)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._selected_mod_id = None
        self._reload()

    # ---------- 查询 ----------

    def _apply_search(self) -> None:
        self._search = self._search_edit.text().strip()
        self._reload()

    def _reload(self) -> None:
        if self._game is None:
            self._model.set_rows([])
            self._count_label.setText("请先在左上角选择游戏档案")
            return
        rows = self._repo.list_mods(
            self._game.app_id,
            status=self._status_combo.currentData(),
            special_only=self._special_check.isChecked(),
            search=self._search or None,
            order_by=self._order_by,
        )
        self._model.set_rows(rows)
        self._count_label.setText(f"共 {len(rows)} 个 mod")
        if self._selected_mod_id is not None:
            for i, m in enumerate(rows):
                if m.mod_id == self._selected_mod_id:
                    self._table.selectRow(i)
                    break

    # ---------- 槽 ----------

    def _on_header_clicked(self, col: int) -> None:
        pair = _SORT_MAP.get(col)
        if pair is None:
            return
        asc, desc = pair
        if self._order_by == asc and desc:
            self._order_by, order = desc, Qt.SortOrder.DescendingOrder
        else:
            self._order_by, order = asc, Qt.SortOrder.AscendingOrder
        self._sort_col = col
        self._table.horizontalHeader().setSortIndicator(col, order)
        self._reload()

    def _on_current_row(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            m = self._model.mod_at(current.row())
            self._selected_mod_id = m.mod_id
            self._detail.set_mod(m)
        else:
            self._detail.set_mod(None)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        m = self._model.mod_at(index.row())
        menu = QMenu(self)
        mid = m.mod_id

        act_open = QAction("打开工坊页面", menu)
        act_open.setEnabled(bool(m.url))
        act_open.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(m.url)))
        menu.addAction(act_open)

        menu.addSeparator()
        for text, cb in (
                ("编辑备注…", lambda: self._edit_note(m)),
                ("颜色标记…", lambda: self._pick_color(m)),
                ("切换特别关注", lambda: self._toggle_special(m)),
        ):
            act = QAction(text, menu)
            act.triggered.connect(cb)
            menu.addAction(act)

        menu.addSeparator()
        # 跳到命令生成页，让它只勾选这个 mod（不带页面参数，交给 MainWindow 接线）
        act_cmdgen = QAction("获取下载命令…", menu)
        act_cmdgen.triggered.connect(
            lambda: self.command_gen_requested.emit([mid]))
        menu.addAction(act_cmdgen)
        act_backup = QAction("手动备份（开发中）", menu)
        act_backup.setEnabled(False)
        menu.addAction(act_backup)

        menu.addSeparator()
        act_del = QAction("软删除…", menu)
        act_del.setEnabled(m.status != "deleted")
        act_del.triggered.connect(lambda: self._soft_delete(m))
        menu.addAction(act_del)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ---------- 动作 ----------

    def _edit_note(self, m) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "编辑备注", f"mod {m.mod_id} 的备注：", m.note or "")
        if ok:
            self._repo.set_note(m.mod_id, text.strip() or None)
            self._reload()

    def _pick_color(self, m) -> None:
        names = [*_COLORS.keys(), "（清除标记）"]
        name, ok = QInputDialog.getItem(
            self, "颜色标记", f"mod {m.mod_id}：",
            names, current=0, editable=False)
        if ok:
            self._repo.set_color_tag(
                m.mod_id, None if name.startswith("（") else _COLORS[name])
            self._reload()

    def _toggle_special(self, m) -> None:
        self._repo.set_special(m.mod_id, not m.is_special)
        self._reload()

    def _soft_delete(self, m) -> None:
        ret = QMessageBox.question(
            self, "软删除",
            f"确定将「{m.title or m.mod_id}」标记为已删除？\n"
            "记录会保留在库中（含删除前快照），可随时恢复。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # last_state 组装口径与 repo.mark_failed 保持一致（flow 层职责，暂由 GUI 代行）
        last_state = {"title": m.title, "url": m.url, "time_updated": m.time_updated,
                      "local_timeupdated": m.local_timeupdated, "manifest": m.manifest,
                      "local_size": m.local_size, "note": m.note,
                      "color_tag": m.color_tag, "is_special": m.is_special,
                      "local_path": m.local_path}
        self._repo.mark_deleted(m.mod_id, last_state)
        self._reload()
