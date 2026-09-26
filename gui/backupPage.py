"""备份管理页（完整版，M3）
"""
r"""
gui/backupPage.py · mod 内容备份的交互层。

分工：引擎 core/backupManager 拥有全部文件操作与账目逻辑（复制/登记/
清理/恢复/删除，内置 R4 保险丝）；本页只做四件事——勾选、确认、线程、
把引擎的报告翻成人话。本页不直接碰任何 mod 文件。

线程规矩（踩坑 ⑫，与更新检测页同一套）：
- 备份/恢复/删除都放后台线程；worker 引用只在 finished 收尾函数里
  释放（先 wait() 再置 None）；同一时刻只跑一个任务（单飞互斥）
- 页面提供 shutdown()，MainWindow.closeEvent 统一调用：备份线程
  批间停止；恢复/删除没有停止点（停在中途=危险），等它跑完
- 引擎在工作线程里写库 → 仓库连接必须 check_same_thread=False
  （本轮在 sqliteRepository.__init__ 回加）

按钮 → 引擎调用对应关系：
- 备份 mod…（勾选对话框）→ backup_mod 逐个执行
- 恢复所选 → restore_backup：确认框明示 R8（恢复前自动备份当前版本）
  与失败自动回退
- 钉住/取消钉住 → repo.set_pinned（纯账目，毫秒级，不进线程）
- 删除所选 → delete_backup（先盘后账）
- 备份数据库… → repo.backup_to（R16）——数据库快照与 mod 内容备份
  是两回事，文案与提示分开

已知限制（本轮收一半）：backup_dir 改址后旧记录按新位置解析而失联
（R1 相对路径，schema 不存旧根）→ 本版新增"盘上"列标得失联记录；
给失联记录重定位归打磨轮。
"""

from pathlib import Path
from gui.backupRelocateDialog import BackupRelocateDialog
from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QCheckBox,

)

from core.backupManager import BackupManager
from core.models import Game
from gui.consolePanel import LogBus
from gui.formatters import abs_time, fmt_size, status_zh
from gui.backupMoveDialog import BackupMoveDialog

# 与设置页 _FIELDS 核对过的真键名（settingsPage.py）。注意配额在设置页
# 以 GB 计（人好填），引擎以字节计（好比较），换算只在 _make_manager 做一次
_KEY_KEEP_PER_MOD = "backup_keep_per_mod"
_KEY_QUOTA_GB = "backup_total_quota_gb"


_COLUMNS = ["Mod 编号", "mod 标题", "备份目录", "备份版本", "大小", "盘上", "钉住", "备注"]


class _BackupWorker(QThread):
    """后台备份线程：逐个 mod 顺序调引擎（robocopy 自带 /MT 多线程，
    外层并行只会互抢磁盘）。每完成一个报一次进度。

    停止协议：批间生效（一个 mod 备完才停），进行中的 robocopy 不打断。
    """

    one_done = Signal(int, int, bool, str)  # 已完成数, 总数, 成功?, 一行摘要
    all_done = Signal(bool, list)           # (是否手动停止, [(编号, 成功?, 摘要)])
    crashed = Signal(str)                   # 预期外异常（bug 性质）

    def __init__(self, manager: BackupManager, mod_ids: list[int]) -> None:
        super().__init__()
        self._manager = manager
        self._ids = list(mod_ids)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        results: list[tuple[int, bool, str]] = []
        total = len(self._ids)
        for i, mid in enumerate(self._ids, 1):
            if self._stop:
                self.all_done.emit(True, results)
                return
            try:
                rep = self._manager.backup_mod(mid)
            except Exception as exc:  # 引擎约定操作层失败走报告；到这里=bug
                self.crashed.emit(f"备份 mod {mid} 时出现预期外错误：{exc}")
                return
            line = f"mod {mid} 备份成功" if rep.ok else f"mod {mid} 失败：{rep.error}"
            results.append((mid, rep.ok, line))
            self.one_done.emit(i, total, rep.ok, line)
        self.all_done.emit(False, results)


class _CallWorker(QThread):
    """单任务线程（恢复/删除共用）：跑一个引擎调用，结果/异常发回主线程。"""

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

class _PickModsDialog(QDialog):
    """勾选要备份的 mod（T25 收官：每行带 ↗ 直达工坊页面——
    "选 mod 做某事"的窗口必须能顺手查详情；QListWidget 用
    setItemWidget 挂"勾选框 + ↗"的行部件）。"""

    def __init__(self, mods, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择要备份的 mod")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"共 {len(mods)} 个已下载 mod，勾选要备份的：", self))
        self._list = QListWidget(self)
        self._cbs: list[tuple[int, QCheckBox]] = []  # (mod_id, 勾选框)，selected_ids 按它收答案
        for m in mods:
            item = QListWidgetItem(self._list)
            row = QWidget(self._list)
            rh = QHBoxLayout(row)
            rh.setContentsMargins(8, 2, 4, 2)
            cb = QCheckBox(f"{m.mod_id} {m.title or '（无标题）'}", row)
            cb.setChecked(True)
            rh.addWidget(cb, 1)
            btn_open = QPushButton("↗", row)
            btn_open.setFixedWidth(28)
            btn_open.setToolTip("在浏览器打开该 mod 的创意工坊页面")
            btn_open.setEnabled(bool(m.url))
            if m.url:
                btn_open.clicked.connect(
                    lambda _=False, u=m.url: QDesktopServices.openUrl(QUrl(u)))
            rh.addWidget(btn_open)
            self._list.setItemWidget(item, row)
            item.setData(Qt.ItemDataRole.UserRole, m.mod_id)
            self._cbs.append((m.mod_id, cb))
        v.addWidget(self._list, 1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(460, 480)

    def selected_ids(self) -> list[int]:
        return [mid for mid, cb in self._cbs if cb.isChecked()]

class BackupPage(QWidget):
    """备份管理页。"""

    def __init__(self, repo, settings, parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._rows = []            # 展示顺序的记录（新 → 旧），与表格行对应
        self._backup_worker: _BackupWorker | None = None
        self._job_worker: _CallWorker | None = None
        self._busy = False
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("备份管理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "mod 内容备份：恢复前会自动备份当前版本，恢复失败自动回退，"
            "不会丢数据；删除是先删盘上目录、成功才删记录；钉住的备份"
            "豁免自动清理，但不挡手动删除。\n"
            "「备份数据库」是另一种备份（数据库快照），两者互不相干。"
            "「盘上」列标出失联记录（盘上已找不到，多半是备份目录改过位置），可用【重定位备份目录】指认新位置。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._backup_btn = QPushButton("备份 mod…", btn_row)
        self._backup_btn.clicked.connect(self._start_backup)
        self._db_btn = QPushButton("备份数据库…", btn_row)
        self._db_btn.clicked.connect(self._backup_database)
        self._refresh_btn = QPushButton("刷新", btn_row)
        self._refresh_btn.clicked.connect(self._reload)
        self._open_btn = QPushButton("打开备份目录", btn_row)
        self._open_btn.clicked.connect(self._open_backup_dir)
        self._relocate_btn = QPushButton("重定位备份目录…", btn_row)
        self._relocate_btn.clicked.connect(self._relocate)
        self._move_btn = QPushButton("备份搬家…", btn_row)
        self._move_btn.clicked.connect(self._move)

        self._stop_btn = QPushButton("停止备份", btn_row)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_backup)
        for b in (self._backup_btn, self._db_btn, self._refresh_btn, self._open_btn, self._relocate_btn, self._move_btn,
                  self._stop_btn):
            h.addWidget(b)

        h.addStretch(1)
        root.addWidget(btn_row)


        op_row = QWidget(self)
        h2 = QHBoxLayout(op_row)
        h2.setContentsMargins(0, 0, 0, 0)
        self._restore_btn = QPushButton("恢复所选", op_row)
        self._restore_btn.clicked.connect(self._start_restore)
        self._pin_btn = QPushButton("钉住/取消钉住", op_row)
        self._pin_btn.clicked.connect(self._toggle_pin)
        self._delete_btn = QPushButton("删除所选", op_row)
        self._delete_btn.clicked.connect(self._start_delete)
        for b in (self._restore_btn, self._pin_btn, self._delete_btn):
            b.setEnabled(False)
            h2.addWidget(b)
        h2.addStretch(1)
        root.addWidget(op_row)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)  # 踩坑 ④
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # mod 标题列伸展
        for col, width in ((0, 110), (2, 190), (3, 150), (4, 90), (5, 60), (6, 60), (7, 200)):
            self._table.setColumnWidth(col, width)

        self._table.itemSelectionChanged.connect(self._refresh_op_buttons)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        self._count_label.setWordWrap(True)
        root.addWidget(self._count_label)
        self._backup_btn.setToolTip(
            "把勾选的 mod 内容复制进备份区；保留份数与配额按设置页执行")
        self._db_btn.setToolTip(
            "生成数据库快照文件（与 mod 内容备份是两回事），滚动保留 3 份")
        self._refresh_btn.setToolTip("重新读取备份记录列表")
        self._open_btn.setToolTip("在资源管理器中打开当前游戏的备份目录")
        self._relocate_btn.setToolTip(
            "「盘上」列有失联记录时用：指认备份文件夹现在的位置，"
            "先预演能对回多少条，确认后只改档案的备份目录字段，不动文件")
        self._move_btn.setToolTip(
            "备份所在的盘快满时用：生成把备份整体搬到新位置并原地建联接的命令，"
            "记录一个不用改；与重定位的分工见窗口内说明")


        self._stop_btn.setToolTip("请求停止：当前 mod 备完后不再继续")
        self._restore_btn.setToolTip(
            "把所选备份复制回下载目录；恢复前自动备份当前版本，失败自动回退")
        self._pin_btn.setToolTip(
            "钉住豁免自动清理（保留策略不删它），但不挡手动删除")
        self._delete_btn.setToolTip(
            "删除磁盘目录和记录，不可撤销；先删盘、成功才删账")


    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._reload()

    def shutdown(self) -> None:
        """主窗口退出前调用（closeEvent 统一调，无需改 MainWindow）。

        备份线程有停止协议：批间生效，最多再等一个 mod 的时间；
        恢复/删除线程没有停止点，等它跑完（单个 robocopy 秒级~分钟级）。
        """
        if self._backup_worker is not None:
            self._backup_worker.stop()
            self._backup_worker.wait()
        if self._job_worker is not None:
            self._job_worker.wait()

    def backup_ids_for(self, app_id: int, mod_ids: list[int]) -> None:
        """mod 库页【备份选中项】的落点：切到对应档案，过滤出可备份
        （已下载）的 mod 后进批次。

        档案切换在这里做——用户可能在 A 游戏的库页勾选，而备份页
        正停在 B 游戏；不切的话批次会备错档案。
        """
        game = self._repo.get_game(app_id)
        if game is None:
            self._log.warn(f"备份取消：找不到游戏档案 {app_id}")
            return
        self.set_game(game)
        downloadable = {m.mod_id for m in self._repo.list_mods(app_id)
                        if m.status == "downloaded"}
        ids = [i for i in mod_ids if i in downloadable]
        if not ids:
            QMessageBox.information(
                self, "备份选中项", "勾选的 mod 里没有已下载的，无内容可备份。")
            return
        skipped = [i for i in mod_ids if i not in downloadable]
        if skipped:
            # 被跳过的按实际状态报（status_zh 原话）——库里无内容的状态
            # 不止一种，手写"未下载"会和列表里看到的词对不上号
            by_status: dict[str, int] = {}
            for i in skipped:
                m = self._repo.get_mod(i)
                name = status_zh(m.status) if m is not None else "记录缺失"
                by_status[name] = by_status.get(name, 0) + 1
            detail = "、".join(f"{k} {v} 个" for k, v in by_status.items())
            self._log.info(
                f"备份批次：跳过 {len(skipped)} 个无本地内容的 mod（{detail}）")
        self._run_backup(ids)


    # ---------- 内部：公共小件 ----------

    def _make_manager(self) -> BackupManager:
        # default 值与 appSettings.DEFAULTS 对齐（keep=1、配额=100GB）。
        # 实际上 get() 必命中 DEFAULTS，这里的 default 只是文档作用
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 1)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 100)
        return BackupManager(
            self._repo,
            keep_per_mod=keep,
            # 0 = 不限；GB → 字节只在边界换算一次
            quota_bytes=(quota_gb * 1024 ** 3) if quota_gb > 0 else None,
            steamcmd_path=self._settings.get("steamcmd_path"))

    def _selected(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        if self._table.item(row, 0) is None:
            return None  # 行已被重建，选择是陈旧的
        return self._rows[row]

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self._backup_btn, self._db_btn, self._refresh_btn,
                  self._open_btn, self._relocate_btn,self._move_btn):
            b.setEnabled(not busy)
        self._stop_btn.setEnabled(False)
        self._refresh_op_buttons()

    def _refresh_op_buttons(self) -> None:
        enable = (not self._busy) and self._selected() is not None
        for b in (self._restore_btn, self._pin_btn, self._delete_btn):
            b.setEnabled(enable)

    def _cell(self, row: int, col: int, text: str) -> None:
        self._table.setItem(row, col, QTableWidgetItem(text))

    # ---------- 列表 ----------

    def _reload(self) -> None:
        if self._game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._table.setRowCount(0)
            self._rows = []
            self._count_label.setText("")
            self._refresh_op_buttons()
            return
        self._game_label.setText(
            f"当前游戏：{self._game.name}（{self._game.app_id}）")
        # 标题对照表（件 11）：一次取齐当前档案的 mod，行循环里查标题
        mods_by_id = {m.mod_id: m
                      for m in self._repo.list_mods(self._game.app_id)}
        ids = set(mods_by_id)
        rows = [b for b in self._repo.list_backups(oldest_first=True)
                if b.mod_id in ids]
        bdir_text = (self._game.backup_dir or "").strip()
        bdir = Path(bdir_text) if bdir_text else None
        self._rows = list(reversed(rows))  # 新 → 旧
        self._table.setRowCount(len(self._rows))
        total, lost = 0, 0
        for r, b in enumerate(self._rows):
            total += b.size_bytes or 0
            on_disk = "—"
            if bdir is not None:
                if (bdir / b.backup_path).is_dir():
                    on_disk = "✓"
                else:
                    on_disk, lost = "失联", lost + 1
            mod = mods_by_id.get(b.mod_id)
            title = (mod.title if mod is not None else None) or "（无标题或记录缺失）"
            self._cell(r, 0, str(b.mod_id))
            self._cell(r, 1, title)
            self._cell(r, 2, b.backup_path)
            self._cell(r, 3, abs_time(b.version_timeupdated))
            self._cell(r, 4, fmt_size(b.size_bytes))
            self._cell(r, 5, on_disk)
            self._cell(r, 6, "是" if b.pinned else "")
            self._cell(r, 7, b.note or "")
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 1)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 100)
        policy = (f"每个 mod 保留最新 {keep} 份"
                  + (f"，总量上限 {quota_gb} GB" if quota_gb else "，总量不限")
                  + "（设置页修改）")
        lost_note = (f"，其中失联 {lost} 份（盘上已找不到——点【重定位备份目录】指认新位置）"
                     if lost else "")
        self._count_label.setText(
            f"共 {len(self._rows)} 份备份，合计 {fmt_size(total)}{lost_note}\n"
            f"保留策略：{policy}")
        self._refresh_op_buttons()

    # ---------- 备份 ----------
    def _start_backup(self) -> None:
        if self._busy or self._game is None:
            return
        mods = [m for m in self._repo.list_mods(self._game.app_id)
                if m.status == "downloaded"]
        if not mods:
            QMessageBox.information(self, "备份 mod", "当前档案没有已下载的 mod。")
            return
        dlg = _PickModsDialog(mods, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_backup(dlg.selected_ids())

    def _run_backup(self, ids: list[int]) -> None:
        """备份批次执行核心：勾选对话框与 mod 库页【备份选中项】共用。

        单飞互斥在这里统一把关——不管入口来自哪个按钮，同一时刻
        只允许一个备份/恢复任务。
        """
        if self._busy:
            self._log.warn("已有备份/恢复任务在进行：请等它结束再试")
            return
        if self._game is None or not ids:
            return
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._set_busy(True)
        self._backup_worker = _BackupWorker(self._make_manager(), ids)
        self._backup_worker.one_done.connect(self._on_one_backed)
        self._backup_worker.all_done.connect(self._on_backup_all_done)
        self._backup_worker.crashed.connect(self._on_worker_crashed)
        self._backup_worker.finished.connect(self._on_backup_thread_finished)
        self._backup_worker.start()
        self._stop_btn.setEnabled(True)
        self._log.info(f"开始备份 {len(ids)} 个 mod…")

    def _stop_backup(self) -> None:
        if self._backup_worker is not None:
            self._backup_worker.stop()
            self._stop_btn.setEnabled(False)

    def _on_one_backed(self, done: int, total: int, ok: bool, line: str) -> None:
        self._progress.setValue(done)
        (self._log.ok if ok else self._log.warn)(f"备份进度 {done}/{total}：{line}")

    def _on_backup_all_done(self, stopped: bool,
                            results: list) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        oks = [r for r in results if r[1]]
        bads = [r for r in results if not r[1]]
        tail = "（手动停止，剩余未执行）" if stopped else ""
        self._log.ok(f"备份结束：成功 {len(oks)}，失败 {len(bads)}{tail}")
        if bads or stopped:
            detail = "\n".join(r[2] for r in bads[:10])
            QMessageBox.warning(self, "备份结束",
                                f"成功 {len(oks)}，失败 {len(bads)}{tail}\n{detail}")
        self._reload()

    def _on_worker_crashed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        self._log.error(message)
        QMessageBox.critical(self, "备份管理", message)

    def _on_backup_thread_finished(self) -> None:
        w = self._backup_worker
        self._backup_worker = None
        if w is not None:
            w.wait()

    # ---------- 恢复 ----------

    def _start_restore(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        mod = self._repo.get_mod(rec.mod_id)
        title = (mod.title if mod else None) or "（无标题或记录缺失）"
        ret = QMessageBox.question(
            self, "恢复备份",
            f"把这份备份恢复到下载目录？\n\n"
            f"mod：{rec.mod_id} {title}\n"
            f"备份版本：{abs_time(rec.version_timeupdated)}｜"
            f"大小：{fmt_size(rec.size_bytes)}\n\n"
            "当前内容会被替换。恢复前会自动备份当前版本（R8），"
            "过程失败会自动回退。\n恢复期间请不要操作 steamcmd 或改动下载目录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        manager = self._make_manager()
        self._set_busy(True)
        self._job_worker = _CallWorker(lambda: manager.restore_backup(rec.id))
        self._job_worker.done.connect(self._on_restore_done)
        self._job_worker.crashed.connect(self._on_worker_crashed)
        self._job_worker.finished.connect(self._on_job_thread_finished)
        self._job_worker.start()
        self._log.info(f"开始恢复备份 {rec.id}（mod {rec.mod_id}）…")

    def _on_restore_done(self, report) -> None:
        self._set_busy(False)
        if report.ok:
            pre = ("，恢复前已自动备份当前版本" if report.pre_backup is not None
                   else "（此前无本地内容，未做恢复前备份）")
            self._log.ok(f"恢复完成{pre}")
            for w in report.warnings:
                self._log.warn(w)
            QMessageBox.information(self, "恢复备份", f"恢复完成{pre}。")
        else:
            self._log.error(f"恢复失败：{report.error}")
            QMessageBox.critical(self, "恢复备份", report.error or "未知错误")
        self._reload()

    def _on_job_thread_finished(self) -> None:
        w = self._job_worker
        self._job_worker = None
        if w is not None:
            w.wait()

    # ---------- 钉住 / 删除 ----------

    def _toggle_pin(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        new = not rec.pinned
        self._repo.set_pinned(rec.id, new)
        self._log.info(
            f"备份 {rec.id}（mod {rec.mod_id}）"
            f"{'已钉住' if new else '已取消钉住'}")
        self._reload()

    def _start_delete(self) -> None:
        rec = self._selected()
        if rec is None or self._busy:
            return
        pin_note = ("（已钉住——钉住只豁免自动清理，不挡手动删除）\n"
                    if rec.pinned else "")
        ret = QMessageBox.question(
            self, "删除备份",
            f"删除这份备份？{pin_note}\n"
            f"mod {rec.mod_id}｜{abs_time(rec.version_timeupdated)}｜"
            f"{fmt_size(rec.size_bytes)}\n\n"
            "将同时删除磁盘目录和记录，不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        manager = self._make_manager()
        self._set_busy(True)
        self._job_worker = _CallWorker(lambda: manager.delete_backup(rec.id))
        self._job_worker.done.connect(self._on_delete_done)
        self._job_worker.crashed.connect(self._on_worker_crashed)
        self._job_worker.finished.connect(self._on_job_thread_finished)
        self._job_worker.start()

    def _on_delete_done(self, result) -> None:
        ok, msg = result
        self._set_busy(False)
        if not ok:
            self._log.error(msg or "删除失败")
            QMessageBox.critical(self, "删除备份", msg or "未知错误")
        else:
            self._log.ok(msg or "备份已删除")
            if msg:  # "仅删除了记录"之类的情况，让用户看见
                QMessageBox.information(self, "删除备份", msg)
        self._reload()

    # ---------- 数据库备份 / 打开目录 ----------

    def _backup_database(self) -> None:
        start = (self._game.backup_dir or "") if self._game else ""
        d = QFileDialog.getExistingDirectory(
            self, "选择数据库备份存放目录", start)
        if not d:
            return
        try:
            out = self._repo.backup_to(Path(d))
        except Exception as exc:
            QMessageBox.critical(self, "备份数据库", f"失败：{exc}")
            return
        self._log.ok(f"数据库已备份：{out}")
        QMessageBox.information(self, "备份数据库", f"已生成：{out}")

    def _open_backup_dir(self) -> None:
        if self._game is None or not (self._game.backup_dir or "").strip():
            QMessageBox.information(
                self, "打开备份目录",
                "当前档案还没有备份目录。\n"
                "第一次备份时会按 steamcmd 位置自动推导并写入档案。")
            return
        # 建目录可能失败（盘满/无权限/路径被占用），拦住给人话提示
        try:
            Path(self._game.backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开备份目录", f"备份目录无法创建：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._game.backup_dir))

    # ---------- 备份目录重定位（T21④） ----------
    def _relocate(self) -> None:
        """打开重定位对话框：引擎只读预演（core/backupRelocate），
        确认后对话框只写 games.backup_dir 一个字段。成功后本页
        立即重载，「盘上」列按新位置重新核对。"""
        if self._game is None:
            QMessageBox.information(self, "重定位备份目录",
                                    "当前未选择游戏档案。")
            return
        dlg = BackupRelocateDialog(self._repo, self._game, self._settings,
                                   self, log=self._log)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    # ---------- 备份搬家指引（命令生成，全程只读） ----------

    def _move(self) -> None:
        """打开备份搬家指引：与【重定位备份目录】互补——备份都在、
        只是想换位置时用它（物理搬运 + 旧位置建联接，档案记录一个不动）；
        记录失联了才用重定位。
        对话框关闭后无条件重载一次：联接建好后旧路径恢复可达，
        「盘上」列按现状重新核对。"""
        if self._game is None:
            QMessageBox.information(self, "备份搬家", "当前未选择游戏档案。")
            return
        dlg = BackupMoveDialog(self._game, self._settings, self)
        dlg.exec()
        self._reload()
