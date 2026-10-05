"""删除游戏档案对话框
"""
"""
删档案不是删一行：档案名下挂着 mod、版本快照、特殊提醒、备份登记、
失效归档（数据库外键树）。本对话框把"将删除什么"全部摆在明面上
（数据先可见再动手），确认后按既定顺序执行：
  ① 数据库快照兜底（注入的 db_backup；未注入则明示跳过）
  ② 账本一个事务清空（repo.delete_game_deep，core 层保证要么全清要么原样）
  ③ 若勾选：逐个删除磁盘上的备份目录（默认不勾；只认账本登记过的
     路径，R4 保险丝护航——mod_backups 下没登记的目录绝不碰）
永不碰的东西（写进界面说明，代码里也根本没有对应调用）：
content 下载目录、游戏本体 mod 目录、目录联接。
顺序是刻意安排的：账先清、盘后删——磁盘删除中途失败，留下的是没有
登记的普通文件夹（无害，可手动删）；反过来若盘先删、账后删，账一旦
失败就成了"登记还在、文件没了"的假失联，污染备份总览。
删除在后台线程执行（大体量目录可能分钟级），没有停止点——停在中途
等于账实各删一半；进行期间窗口拒绝关闭，等它跑完（与备份页"删除
没有停止点"同一口径）。
只与 ModRepository / backupManager.safe_rmtree 接口交互，GUI 层零 SQL。
"""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout,
)

from core.backupManager import safe_rmtree
from core.modRepository import ModRepository
from core.formatters import fmt_size



class _DeleteWorker(QThread):
    """删除执行线程：快照 → 账本 → 逐目录删盘。没有停止点（见文件头）。"""
    step = Signal(str)                           # 阶段说明（进度标签）
    dir_done = Signal(int, int, str, bool, str)  # 第i/共n, 目录, 成功?, 失败原因
    done = Signal(object)                        # 报告 dict（结构见 _on_done）
    crashed = Signal(str)                        # 预期外异常（bug 性质）

    def __init__(self, repo: ModRepository, app_id: int,
                 bdir: Path | None, dirs: list[Path], *,
                 db_backup: Callable[[], str] | None = None) -> None:
        super().__init__()
        self._repo = repo
        self._app_id = app_id
        self._bdir = bdir
        self._dirs = dirs
        self._db_backup = db_backup

    def run(self) -> None:
        report: dict = {"db_backup": None, "dirs_deleted": [],
                        "dirs_failed": [], "summary": None}
        try:
            if self._db_backup is not None:
                self.step.emit("正在生成数据库快照（兜底）…")
                # 快照失败在此抛出：账没动、盘没动，是最安全的失败点
                report["db_backup"] = self._db_backup()
            self.step.emit("正在清除账本（一个事务）…")
            report["summary"] = self._repo.delete_game_deep(self._app_id)
            total = len(self._dirs)
            for i, full in enumerate(self._dirs, 1):
                try:
                    # R4 保险丝：只认 backup_dir 内、非链接的真实目录
                    safe_rmtree(full, self._bdir)
                except (RuntimeError, OSError) as exc:
                    report["dirs_failed"].append((str(full), str(exc)))
                    self.dir_done.emit(i, total, str(full), False, str(exc))
                else:
                    report["dirs_deleted"].append(str(full))
                    self.dir_done.emit(i, total, str(full), True, "")
            self.done.emit(report)
        except Exception as exc:
            self.crashed.emit(str(exc))


class GameDeleteDialog(QDialog):
    """删除确认 + 执行窗口。exec() 返回后可供调用方检查：
    - self.deleted：账本是否已清空（True=已删；False=未删或中断未知，
      两种情况调用方都应刷新界面以实际情况为准）
    - self.report：执行报告 dict（供上层写操作日志）
    overview_requested(app_id)：点「先去备份总览看看」时发出（随后本窗口
    自行关闭）——上层接页面跳转；没人接也不报错。
    """

    overview_requested = Signal(int)

    def __init__(self, repo: ModRepository, app_id: int, *,
                 db_backup: Callable[[], str] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._app_id = app_id
        self._db_backup = db_backup
        self._worker: _DeleteWorker | None = None
        self._running = False
        self.deleted = False
        self.report: dict | None = None

        game = repo.get_game(app_id)
        if game is None:
            # 正常流程到不了这里（切换器只对现存档案开门）；
            # 与 backupManager 同一口径：调用方传错按 bug 处理，直接炸
            raise ValueError(f"游戏档案 {app_id} 不存在")
        self._summary = repo.game_deletion_summary(app_id)

        stored = (game.backup_dir or "").strip()
        self._bdir = Path(stored) if stored else None
        # 盘上真实存在的备份目录：只认账本登记过的路径
        self._dirs = [self._bdir / p for p in self._summary.backup_paths
                      if self._bdir is not None and (self._bdir / p).is_dir()]

        self.setWindowTitle(f"删除游戏档案 — {game.name}（{app_id}）")
        self.setMinimumWidth(620)

        head = QLabel(f"将永久删除档案「{game.name}（{app_id}）」名下的全部账目：")
        head.setStyleSheet("font-weight: 600;")
        detail = QLabel(
            f"• mod 记录 {self._summary.mod_total} 条"
            f"（其中已软删除 {self._summary.mod_deleted} 条将一并物理清除）\n"
            f"• 失效归档 {self._summary.failed_count} 条\n"
            f"• 备份登记 {self._summary.backup_count} 条，"
            f"登记合计约 {fmt_size(self._summary.backup_bytes)}\n"
            "• 该档案的版本快照与特殊提醒\n"
            "操作日志（全局历史）保留不动；\n"
            "content 下载目录、游戏本体 mod 目录、目录联接——一律不碰。")
        detail.setWordWrap(True)

        if self._db_backup is not None:
            db_line = "① 清账前会自动生成一次数据库快照兜底（完成后显示位置）。"
        else:
            db_line = ("① 未配置数据库快照入口——将跳过兜底备份"
                       "（删账后无法整库回退，请再想一下）。")
        lbl_db = QLabel(db_line)
        lbl_db.setWordWrap(True)

        # 灰字说明按实际情况拼装（勾选框只在盘上确有目录时出现）
        notes: list[str] = []
        if self._dirs:
            if len(self._dirs) < self._summary.backup_count:
                notes.append(
                    f"盘上只找到 {len(self._dirs)}/{self._summary.backup_count} "
                    "个备份目录（其余多半已被手动删除）。")
            notes.append("不勾选：磁盘文件原样保留，成为没有登记的普通文件夹，"
                         "随时可手动删除。")
        elif self._summary.backup_count > 0:
            notes.append("备份目录在盘上无法定位（档案未设备份目录或已不在）——"
                         "账删后文件留在原地，不再被本工具跟踪。")
        self._check: QCheckBox | None = None
        if self._dirs:
            self._check = QCheckBox(
                f"同时删除磁盘上的备份目录"
                f"（盘上找到 {len(self._dirs)} 个，"
                f"登记合计约 {fmt_size(self._summary.backup_bytes)}）")
            self._check.setChecked(False)  # 默认保留文件（拍板 1）

        self._btn_overview = QPushButton("先去备份总览看看（关闭本窗口，稍后再来删）")
        self._btn_overview.clicked.connect(self._goto_overview)

        self._bar = QProgressBar(self)
        self._bar.setVisible(False)
        self._lbl_progress = QLabel("", self)
        self._lbl_progress.setWordWrap(True)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("确认删除")
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("取消")
        cancel.setDefault(True)   # 默认焦点落在"取消"：误触回车不会开删
        cancel.setFocus()
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(detail)
        lay.addWidget(lbl_db)
        if self._check is not None:
            lay.addWidget(self._check)
        for n in notes:
            lbl = QLabel(n)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: gray;")
            lay.addWidget(lbl)
        lay.addWidget(self._btn_overview)
        lay.addWidget(self._bar)
        lay.addWidget(self._lbl_progress)
        lay.addWidget(self._buttons)

    # ---------- 入口分流 / 跳转 ----------

    def _on_ok(self) -> None:
        if self.deleted:
            self.accept()      # 完成后的"关闭"键（文案已换）
            return
        self._confirm()        # 初始的"确认删除"

    def _goto_overview(self) -> None:
        self.overview_requested.emit(self._app_id)
        self.reject()          # 正常关闭，不算删除

    # ---------- 执行 ----------

    def _confirm(self) -> None:
        if self._worker is not None or self._running:
            return
        # 勾选了"带走文件"才列删盘清单；开窗期间文件可能被用户动过，再核一遍
        dirs = ([d for d in self._dirs if d.is_dir()]
                if (self._check is not None and self._check.isChecked()) else [])
        self._bar.setRange(0, max(len(dirs), 1))
        self._bar.setValue(0)
        self._bar.setVisible(bool(dirs))
        self._set_running(True)
        self._worker = _DeleteWorker(self._repo, self._app_id, self._bdir,
                                     dirs, db_backup=self._db_backup)
        self._worker.step.connect(self._lbl_progress.setText)
        self._worker.dir_done.connect(self._on_dir_done)
        self._worker.done.connect(self._on_done)
        self._worker.crashed.connect(self._on_crashed)
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def _on_dir_done(self, i: int, n: int, path: str, ok: bool, err: str) -> None:
        self._bar.setValue(i)
        tail = "" if ok else f" —— 失败：{err}"
        self._lbl_progress.setText(f"删除备份目录 {i}/{n}：{path}{tail}")

    def _on_done(self, report: dict) -> None:
        self.deleted = True
        self.report = report
        self._bar.setVisible(False)
        lines = ["删除完成。",
                 "数据库快照：" + (report["db_backup"] or "未生成")]
        s = report.get("summary")
        if s is not None:
            lines.append(
                f"账本已清空：mod {s.mod_total} 条（含软删除 {s.mod_deleted} 条）、"
                f"备份登记 {s.backup_count} 条、失效归档 {s.failed_count} 条。")
        if report["dirs_deleted"] or report["dirs_failed"]:
            lines.append(f"磁盘备份目录：删除 {len(report['dirs_deleted'])} 个、"
                         f"失败 {len(report['dirs_failed'])} 个。")
            for p, err in report["dirs_failed"]:
                lines.append(f"  未删掉（文件被占用等，稍后手动删即可）：{p}")
        self._lbl_progress.setText("\n".join(lines))
        self._ok.setText("关闭")
        self._set_running(False)

    def _on_crashed(self, message: str) -> None:
        self._bar.setVisible(False)
        self._set_running(False)
        self._ok.setEnabled(False)  # 中断后不许原地再删一遍：回档案列表核实
        QMessageBox.critical(
            self, "删除档案",
            f"执行中断：{message}\n\n"
            "若中断发生在清账之后，档案可能已删除——关闭后到档案列表确认；"
            "磁盘上可能留下未删的备份目录（总览页看得见）。")

    def _on_thread_finished(self) -> None:
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()

    def _set_running(self, running: bool) -> None:
        self._running = running
        # 没有停止点：跑动期间一切入口关闭，窗口也不许关（见 reject/closeEvent）
        self._ok.setEnabled(not running)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel).setEnabled(not running)
        self._btn_overview.setEnabled(not running)
        if self._check is not None:
            self._check.setEnabled(not running)

    # ---------- 进行期间拒绝关闭（没有安全的停止点） ----------

    def reject(self) -> None:
        if self._running:
            QMessageBox.information(self, "请稍候",
                                    "删除正在进行——没有安全的停止点，等它结束。")
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._running:
            event.ignore()
            QMessageBox.information(self, "请稍候",
                                    "删除正在进行——没有安全的停止点，等它结束。")
            return
        super().closeEvent(event)
