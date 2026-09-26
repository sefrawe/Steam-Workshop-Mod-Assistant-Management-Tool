"""备份总览页
"""
"""
备份管理的"明面"：跨档案列出全部备份登记，盘上状态一目了然，删不删、
删账还是连文件一起删，由用户在这里看清楚后自行决定（"数据和信息可见"
原则的落点；v2.x 新增，与删除档案对话框配套——账删之后留下的文件在
这里显示为失联/无登记，仍可继续清理）。
与备份管理页的分工：
- 备份页：管"当前档案"的备份全流程（备份/恢复/重定位/搬家/配额清理）
- 总览页：跨档案只读大盘 + 三个轻动作（删登记 / 删登记并删文件 / 钉住）；
  失效、软删除 mod 名下的备份也照列——恢复场景正需要看见它们
口径对齐：
- 盘上判定与备份页同一把尺：(档案备份根 / 记录相对路径).is_dir()，
  三态 ✓ / 失联 / —（档案未设备份目录时，不算失联）
- 删登记 = repo.delete_backup_record（只动账）；删登记并删文件 =
  BackupManager.delete_backup（先盘后账、R4 保险丝、返回人话）——
  本页自己绝不碰文件系统
- 耗时操作进后台线程（_CallWorker 单飞，与备份页同一套线程规矩）；
  shutdown() 供主窗口关闭时统一调用
只与 ModRepository / BackupManager 接口交互，GUI 层零 SQL（记事本架构约定）。
"""

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.backupManager import BackupManager
from core.modRepository import BackupOverviewRow, ModRepository
from core.models import Game
from gui.consolePanel import LogBus
from gui.formatters import abs_time, fmt_size, status_zh

# 与设置页核对过的真键名（与 backupPage 相同三件；改键名时两处要一起动）
_KEY_KEEP_PER_MOD = "backup_keep_per_mod"
_KEY_QUOTA_GB = "backup_total_quota_gb"
_KEY_STEAMCMD = "steamcmd_path"

_COLUMNS = ["游戏", "Mod 编号", "标题", "备份版本", "大小", "盘上",
            "钉住", "登记时间", "mod 状态"]


class _CallWorker(QThread):
    """单任务线程（与 backupPage 同款）：跑一个引擎调用，结果/异常发回主线程。"""
    done = Signal(object)
    crashed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.crashed.emit(str(exc))


class BackupOverviewPage(QWidget):
    """备份总览页。refresh() 供主窗口在切到本页时调用。"""

    def __init__(self, repo, settings, parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._rows: list[BackupOverviewRow] = []   # 与表格行一一对应
        self._disk: list[str] = []                 # 与 _rows 平行的盘上三态
        self._games: dict[int, Game] = {}
        self._games_in_order: list[Game] = []      # 组合框保序用
        self._filter_game: int | None = None       # None = 全部档案
        self._job_worker: _CallWorker | None = None
        self._busy = False
        self._build_ui()
        self._reload()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("备份总览", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "跨档案查看全部备份登记；删不删、删账还是连文件一起删，在这里看清楚再决定。\n"
            "「盘上」三态：✓ 在盘上｜失联 盘上已找不到（多半备份目录改过位置或已手动删除）"
            "｜— 该档案未设备份目录（不算失联）。\n"
            "与备份管理页的分工：备份/恢复/重定位/搬家在备份页（按当前档案）；"
            "这里是跨档案的大盘与轻量清理。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("档案：", self))
        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._combo)
        self._lost_only = QCheckBox("只看失联", self)
        self._lost_only.toggled.connect(lambda _checked: self._fill_table())
        filter_row.addWidget(self._lost_only)
        filter_row.addStretch(1)
        self._refresh_btn = QPushButton("刷新", self)
        self._refresh_btn.clicked.connect(self._reload)
        filter_row.addWidget(self._refresh_btn)
        self._open_btn = QPushButton("打开所在文件夹", self)
        self._open_btn.clicked.connect(self._open_folder)
        filter_row.addWidget(self._open_btn)
        root.addLayout(filter_row)

        op_row = QHBoxLayout()
        self._pin_btn = QPushButton("钉住/取消钉住", self)
        self._pin_btn.clicked.connect(self._toggle_pin)
        self._del_rec_btn = QPushButton("删除登记（只删账）", self)
        self._del_rec_btn.clicked.connect(self._delete_record)
        self._del_disk_btn = QPushButton("删除登记并删除文件", self)
        self._del_disk_btn.clicked.connect(self._delete_with_disk)
        for b in (self._pin_btn, self._del_rec_btn, self._del_disk_btn):
            b.setEnabled(False)
            op_row.addWidget(b)
        op_row.addStretch(1)
        root.addLayout(op_row)

        self._pin_btn.setToolTip("钉住豁免自动清理（保留策略不删它），但不挡手动删除")
        self._del_rec_btn.setToolTip(
            "只删除这条数据库登记，磁盘文件原样保留"
            "（之后成为没有登记的普通文件夹，可随时手动删除）")
        self._del_disk_btn.setToolTip(
            "先删磁盘目录、成功才删登记，不可撤销；与备份页删除同一引擎，"
            "R4 保险丝只认登记过的路径")
        self._open_btn.setToolTip("打开该备份所在（或所属）的文件夹")

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)  # 踩坑 ④
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 140), (1, 100), (3, 150), (4, 90), (5, 60),
                           (6, 50), (7, 150), (8, 90)):
            self._table.setColumnWidth(col, width)
        self._table.itemSelectionChanged.connect(self._refresh_op_buttons)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        root.addWidget(self._count_label)

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
        """盘上三态，与备份页同一把尺（R1：账本存相对 backup_dir 的目录名）。"""
        g = self._games.get(r.game_id)
        bdir = (g.backup_dir or "").strip() if g else ""
        if not bdir:
            return "—"
        return "✓" if (Path(bdir) / r.backup_path).is_dir() else "失联"

    def _fill_table(self) -> None:
        rows = self._repo.list_backups_overview()
        disk = [self._disk_state(r) for r in rows]
        pairs = list(zip(rows, disk))
        if self._filter_game is not None:
            pairs = [p for p in pairs if p[0].game_id == self._filter_game]
        if self._lost_only.isChecked():
            pairs = [p for p in pairs if p[1] == "失联"]
        self._rows = [r for r, _ in pairs]
        self._disk = [d for _, d in pairs]

        self._table.setRowCount(len(self._rows))
        for row, (r, d) in enumerate(zip(self._rows, self._disk)):
            self._cell(row, 0, r.game_name)
            self._cell(row, 1, str(r.mod_id))
            self._cell(row, 2, r.mod_title or "（无标题）")
            self._cell(row, 3, abs_time(r.version_timeupdated))
            self._cell(row, 4, fmt_size(r.size_bytes))
            item = QTableWidgetItem(d)
            if d == "失联":
                item.setForeground(Qt.GlobalColor.red)  # 失联要一眼可见
            self._table.setItem(row, 5, item)
            self._cell(row, 6, "是" if r.pinned else "")
            self._cell(row, 7, abs_time(r.created_at))
            self._cell(row, 8, status_zh(r.mod_status))

        total = sum(r.size_bytes or 0 for r in self._rows)
        lost = sum(1 for d in self._disk if d == "失联")
        text = f"共 {len(self._rows)} 份备份登记，合计 {fmt_size(total)}"
        if lost:
            text += f"，其中失联 {lost} 份"
        if not self._rows:
            text += "（还没有任何备份登记）"
        self._count_label.setText(text)
        self._refresh_op_buttons()

    def _cell(self, row: int, col: int, text: str) -> None:
        self._table.setItem(row, col, QTableWidgetItem(text))

    # ---------- 选择与忙碌 ----------

    def _selected(self) -> BackupOverviewRow | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        if self._table.item(row, 0) is None:
            return None  # 行已被重建，选择是陈旧的
        return self._rows[row]

    def _refresh_op_buttons(self) -> None:
        row = self._table.currentRow()
        ok_row = 0 <= row < len(self._rows) and self._table.item(row, 0) is not None
        enable = (not self._busy) and ok_row
        self._pin_btn.setEnabled(enable)
        self._del_rec_btn.setEnabled(enable)
        # 连文件一起删只对"盘上确在"的记录开放
        self._del_disk_btn.setEnabled(enable and ok_row and self._disk[row] == "✓")
        # 打开文件夹同样依赖选中行：与上面三颗同进退，别摆着一副可点的样子
        self._open_btn.setEnabled(enable)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._combo.setEnabled(not busy)
        self._lost_only.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)
        self._open_btn.setEnabled(not busy)
        self._refresh_op_buttons()

    # ---------- 三个轻动作 ----------

    def _toggle_pin(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        new = not rec.pinned
        self._repo.set_pinned(rec.backup_id, new)
        self._log.info(
            f"备份 {rec.backup_id}（mod {rec.mod_id}）"
            f"{'已钉住' if new else '已取消钉住'}")
        self._reload()

    def _delete_record(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        ret = QMessageBox.question(
            self, "删除备份登记",
            f"删除这条备份登记？\n\n{self._brief(rec)}\n\n"
            "只删除数据库登记：磁盘文件原样保留（此后成为没有登记的普通"
            "文件夹，可随时手动删除）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._repo.delete_backup_record(rec.backup_id)
        self._log.ok(
            f"备份登记 {rec.backup_id}（mod {rec.mod_id}）已删除；磁盘未动")
        self._reload()

    def _delete_with_disk(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        ret = QMessageBox.question(
            self, "删除备份",
            f"删除这条备份（含磁盘文件）？\n\n{self._brief(rec)}\n\n"
            "先删磁盘目录、成功才删登记，不可撤销；R4 保险丝只认登记过的路径。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        manager = self._make_manager()
        self._job_worker = _CallWorker(lambda: manager.delete_backup(rec.backup_id))
        self._job_worker.done.connect(self._on_job_done)
        self._job_worker.crashed.connect(self._on_job_crashed)
        self._job_worker.finished.connect(self._on_job_finished)
        self._job_worker.start()
        self._log.info(f"开始删除备份 {rec.backup_id}（mod {rec.mod_id}）…")

    def _on_job_done(self, result) -> None:
        ok, msg = result
        self._set_busy(False)
        if not ok:
            self._log.error(msg or "删除失败")
            QMessageBox.critical(self, "删除备份", msg or "未知错误")
        else:
            self._log.ok(msg or "备份已删除")
            if msg:  # "仅删除了记录"之类的说明，要让人看见
                QMessageBox.information(self, "删除备份", msg)
        self._reload()

    def _on_job_crashed(self, message: str) -> None:
        self._set_busy(False)
        self._log.error(message)
        QMessageBox.critical(self, "备份总览", message)

    def _on_job_finished(self) -> None:
        w = self._job_worker
        self._job_worker = None
        if w is not None:
            w.wait()

    def _open_folder(self) -> None:
        rec = self._selected()
        if rec is None:
            # 决策 22：绝不静默——按钮已随选中置灰，这是防陈旧状态漏网的
            # 双保险；真走到这里必须把话说出来
            QMessageBox.information(
                self, "打开所在文件夹",
                "请先在表格里点选一行备份（点该行任意位置），再打开所在文件夹。")
            return
        if self._busy:
            return
        g = self._games.get(rec.game_id)
        bdir_text = (g.backup_dir or "").strip() if g else ""
        if not bdir_text:
            QMessageBox.information(self, "打开所在文件夹",
                                    f"{rec.game_name} 未设置备份目录。")
            return
        target = Path(bdir_text) / rec.backup_path
        folder = target if target.is_dir() else Path(bdir_text)
        if not folder.exists():
            QMessageBox.information(self, "打开所在文件夹", f"盘上找不到：{folder}")
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
        # 与 backupPage._make_manager 同款：配额在设置页以 GB 计（人好填），
        # 引擎以字节计（好比较），换算只在边界做一次
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 1)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 100)
        return BackupManager(
            self._repo, keep_per_mod=keep,
            quota_bytes=(quota_gb * 1024 ** 3) if quota_gb > 0 else None,
            steamcmd_path=self._settings.get(_KEY_STEAMCMD))

    def _brief(self, r: BackupOverviewRow) -> str:
        title = r.mod_title or "（无标题）"
        return (f"{r.game_name}｜mod {r.mod_id} {title}\n"
                f"版本 {abs_time(r.version_timeupdated)}｜"
                f"{fmt_size(r.size_bytes)}｜登记于 {abs_time(r.created_at)}")

    # ---------- 对外（MainWindow / 切换器调用） ----------

    def refresh(self) -> None:
        """供主窗口在切到本页时调用（接线在主窗口小改里）。"""
        self._reload()

    def set_filter_game(self, app_id: int | None) -> None:
        """跳转入口（删除对话框 overview_requested 的落点）：按档案过滤。
        档案不存在（比如刚被删掉）→ _refresh_combo 会静默回到"全部"。"""
        self._filter_game = app_id
        self._refresh_combo()
        self._fill_table()

    def shutdown(self) -> None:
        """主窗口退出前调用（与备份页同一约定）：删除没有停止点，等它跑完。"""
        if self._job_worker is not None:
            self._job_worker.wait()
