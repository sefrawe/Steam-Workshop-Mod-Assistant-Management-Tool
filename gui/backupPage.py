"""备份管理页（雏形）
"""
r"""
当前版本 = 只读列表：展示当前档案的全部备份记录，供核对磁盘与账目。
恢复 / 钉住 / 删除 / 重定位等操作归正式备份管理页（M3），在本文件上
扩展——恢复流程有 R8 前置备份、失败回退等一串交互分支，值得完整设计，
不在雏形里塞半吊子恢复按钮。

数据来源：一次 list_mods（当前游戏的编号集合）+ 一次 list_backups
（全部记录），界面层按编号过滤。repo 没有按游戏列备份的查询，
雏形阶段不值得为它加接口（数据量百条级，毫秒完成）。

入口：mod 库页右键"手动备份"产生的记录出现在这里；
保留策略（每 mod 份数 / 总配额）在设置页配置。
"""
from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.models import Game
from gui.consolePanel import LogBus
from gui.formatters import abs_time, fmt_size

_COLUMNS = ["Mod 编号", "备份目录", "备份版本", "大小", "钉住", "备注"]


class BackupPage(QWidget):
    """只读备份列表。恢复等操作等 M3。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 留存：M3 的配额展示等要用，当前暂不读
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("备份管理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "当前档案的全部备份记录（新 → 旧）。备份来源：mod 库页右键"
            "「手动备份」；保留策略（每个 mod 留几份、总量上限）在设置页配置。\n"
            "恢复 / 钉住 / 删除操作将在后续版本提供。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        refresh = QPushButton("刷新", btn_row)
        refresh.setToolTip("从数据库重读当前档案的备份记录")
        refresh.clicked.connect(self._reload)
        open_dir = QPushButton("打开备份目录", btn_row)
        open_dir.setToolTip("在资源管理器中打开本档案的备份目录")
        open_dir.clicked.connect(self._open_backup_dir)
        h.addWidget(refresh)
        h.addWidget(open_dir)
        h.addStretch(1)
        root.addWidget(btn_row)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 110), (2, 150), (3, 100), (4, 60), (5, 220)):
            self._table.setColumnWidth(col, width)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        root.addWidget(self._count_label)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._reload()

    # ---------- 内部 ----------

    def _reload(self) -> None:
        if self._game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._table.setRowCount(0)
            self._count_label.setText("")
            return
        self._game_label.setText(
            f"当前游戏：{self._game.name}（{self._game.app_id}）")
        # 当前游戏的编号集合 + 全部备份记录，界面层过滤（理由见文件头）
        ids = {m.mod_id for m in self._repo.list_mods(self._game.app_id)}
        rows = [b for b in self._repo.list_backups(oldest_first=True)
                if b.mod_id in ids]
        self._table.setRowCount(len(rows))
        for r, b in enumerate(reversed(rows)):  # 新 → 旧
            self._cell(r, 0, str(b.mod_id))
            self._cell(r, 1, b.backup_path)
            self._cell(r, 2, abs_time(b.version_timeupdated))
            self._cell(r, 3, fmt_size(b.size_bytes))
            self._cell(r, 4, "是" if b.pinned else "")
            self._cell(r, 5, b.note or "")
        total = sum(b.size_bytes or 0 for b in rows)
        self._count_label.setText(
            f"共 {len(rows)} 份备份，合计 {fmt_size(total)}"
            "（配额按全部档案合计计算，见设置页）")

    def _cell(self, row: int, col: int, text: str) -> None:
        self._table.setItem(row, col, QTableWidgetItem(text))

    def _open_backup_dir(self) -> None:
        if self._game is None or not (self._game.backup_dir or "").strip():
            QMessageBox.information(
                self, "打开备份目录",
                "当前档案还没有备份目录。\n"
                "第一次备份时（mod 库页右键「手动备份」）会按 steamcmd "
                "位置自动推导并写入档案。")
            return
        Path(self._game.backup_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._game.backup_dir))
