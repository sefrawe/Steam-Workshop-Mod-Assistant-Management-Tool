"""备份目录重定位对话框
"""
r"""
T21④ 的界面半件：备份记录失联后的"指认新位置"对话框。

分工：core/backupRelocate 引擎只读预演命中率（唯一判定依据，
本文件不重复实现"对上/对不上"）；本对话框做四件事——展示现状、
收集新位置、实时预演、确认后写库（repo.update_game 只改
games.backup_dir 一个字段）。全程不动任何备份文件、不改任何
备份记录——R1 相对路径的红利：改一个指针，全部记录跟着对齐。

为什么不让程序自动修（决策 29）：备份目录是用户资产指针，
失准必须显式指认——预演 0 命中时确认按钮直接禁用，指认一个
对不上的位置等于把记录推得更远；与当前设置相同也禁用（无事
可做还写一遍，只会制造"看起来修过了"的错觉）。

入口两处（同一对话框）：
- gameSwitcher 第三按钮（主动修复；那边没有 LogBus 可注入，
  成功反馈由本对话框自己的弹窗兜底——决策 22③ 的可见反馈不断）
- backupPage 工具行按钮（看到失联记录顺手修；传入页面 LogBus）
两处确认成功后各自刷新：gameSwitcher.reload() 重播 set_game，
backupPage._reload()。本对话框只在写库成功后 accept()，
调用方拿到 Accepted 即可放心刷新，无需判断细节。

候选框预填 = 按当前 steamcmd 位置推导的默认备份位置
（backup_root_default）——最常见情形"steamcmd 挪走、备份跟着
搬"直接命中；搬去了别处就手动改。
"""
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import backupRelocate, steamPaths
from core.models import Game
from gui.consolePanel import LogBus

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"
_C_MUTED = "#8a8a8f"


def _norm(path: str) -> str:
    """路径比较的规整（R14）：normcase（Windows 不分大小写）
    + normpath（分隔符与冗余段归一）。两处都先去引号——资源
    管理器"复制文件地址"常带引号。"""
    return os.path.normcase(
        os.path.normpath(str(path or "").strip().strip('"').strip()))


class BackupRelocateDialog(QDialog):
    """备份目录重定位：现状 → 新位置 → 实时预演 → 确认写库。"""

    def __init__(self, repo, game: Game, settings,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game = game
        self._settings = settings  # 可为 None（gameSwitcher 未注入）
        self._log = log or LogBus()
        # 备份记录的相对路径清单：与 backupPage._reload 同款过滤
        # （先取本档案 mod 集合，再从全量备份里挑本档案的）
        ids = {m.mod_id for m in self._repo.list_mods(game.app_id)}
        self._rels = [b.backup_path
                      for b in self._repo.list_backups(oldest_first=True)
                      if b.mod_id in ids]
        self._current = (game.backup_dir or "").strip()
        self._last: backupRelocate.RelocatePreview | None = None
        self._ok_enabled = False
        self._build_ui()
        self._update_preview()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.setWindowTitle("备份目录重定位")
        v = QVBoxLayout(self)

        title = QLabel("重定位备份目录", self)
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        v.addWidget(title)

        cur_text = self._current if self._current else "（未设置）"
        if self._current:
            state = "盘上存在" if Path(self._current).is_dir() \
                else "盘上不存在"
            cur_text += f"（{state}）"
        head = QLabel(
            f"档案「{self._game.name}」当前备份目录：\n{cur_text}\n"
            f"该档案现有 {len(self._rels)} 份备份记录。"
            "重定位只改这个指针，不动任何备份文件、不改任何记录。",
            self)
        head.setWordWrap(True)  # 踩坑 ⑨
        v.addWidget(head)

        v.addWidget(QLabel("备份文件夹现在的位置：", self))
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self._edit = QLineEdit(row)
        self._edit.textChanged.connect(self._update_preview)
        browse = QPushButton("浏览…", row)
        browse.clicked.connect(self._browse)
        h.addWidget(self._edit, 1)
        h.addWidget(browse)
        v.addWidget(row)

        hint = QLabel(
            "预填的是按当前 steamcmd 位置推导的默认备份位置"
            "（steamcmd 挪走、备份跟着搬的情形直接命中）；"
            "备份文件夹搬去了别处，就把它现在真正在的路径填进来。",
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(hint)

        self._preview = QLabel("", self)
        self._preview.setWordWrap(True)
        v.addWidget(self._preview, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self._ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("确认重定位")
        self._ok_btn.setEnabled(False)
        bb.accepted.connect(self._confirm)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.resize(580, 340)

        # 预填默认推导位置（steamcmd 未配置则为空，预演区会说清）
        steamcmd_exe = (self._settings.get("steamcmd_path")
                        if self._settings else "")
        prefill = steamPaths.backup_root_default(
            steamcmd_exe, self._game.app_id) or ""
        self._edit.setText(prefill)

    def _browse(self) -> None:
        start = self._edit.text().strip() or self._current
        d = QFileDialog.getExistingDirectory(
            self, "选择备份文件夹现在的位置", start)
        if d:
            self._edit.setText(str(d))

    # ---------- 预演 ----------
    def _update_preview(self) -> None:
        """文本一变就重跑引擎预演。确认按钮的启用 = 预演给出
        "值得确认"的结论（根存在、至少命中一条、且与当前不同）。"""
        rep = backupRelocate.preview(self._edit.text(), self._rels)
        self._last = rep
        lines: list[str] = []
        color = _C_MUTED
        ok = False
        if not self._rels:
            lines.append("该档案没有备份记录，无需重定位。")
        elif not rep.root_exists:
            color = _C_FAIL
            lines.append("候选目录在盘上不存在（或为空）——无法预演。")
        elif _norm(rep.candidate_root) == _norm(self._current):
            lines.append("与档案当前备份目录相同——无需重定位。")
        elif rep.non_relative_count == rep.total:
            # 全是绝对路径行：重定位既救不了也影响不了，不放开确认
            color = _C_WARN
            lines.append("该档案的备份记录存的都是绝对路径（旧版遗留），"
                         "重定位帮不上——请人工核对。")
        else:
            hit_line = (f"可对回 {rep.hit} / {rep.relocatable}"
                        " 份备份记录。")
            if rep.all_hit:
                color = _C_OK
                lines.append("✓ " + hit_line
                             + "全部记录都能在这个位置找到。")
                ok = True
            elif rep.any_hit:
                color = _C_WARN
                lines.append("△ " + hit_line + "部分对不上，例如：")
                for s in rep.missed_samples:
                    lines.append(f"　　找不到：{s}")
                if rep.missed > len(rep.missed_samples):
                    lines.append(f"　　……以及另外 {rep.missed - len(rep.missed_samples)} 条")
                lines.append("部分命中 = 位置像新家但不完整。确认前想清楚："
                             "指认错了会让记录离真相更远。")
                ok = True
            else:
                color = _C_FAIL
                lines.append("✗ 一条都对不上——请重新指认"
                             "（0 命中时不能确认）。")
        if 0 < rep.non_relative_count < rep.total:
            lines.append(f"另有 {rep.non_relative_count} 条记录存的是"
                         "绝对路径，不受重定位影响。")
        self._preview.setText("\n".join(lines))
        self._preview.setStyleSheet(f"color: {color};")
        self._ok_enabled = ok
        self._ok_btn.setEnabled(ok)

    # ---------- 确认 ----------
    def _confirm(self) -> None:
        rep = self._last
        if rep is None or not self._ok_enabled:
            return  # 双保险：按钮禁用时本就走不到这里
        ret = QMessageBox.question(
            self, "确认重定位",
            f"把档案「{self._game.name}」的备份目录改为：\n"
            f"{rep.candidate_root}\n\n"
            f"预演结果：可对回 {rep.hit} / {rep.relocatable}"
            " 份备份记录。\n\n"
            "只修改档案的备份目录一个字段，不动任何备份文件；\n"
            "确认后备份页的「盘上」列会按新位置重新核对。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._repo.update_game(self._game.app_id,
                               backup_dir=rep.candidate_root)
        self._log.ok(
            f"备份目录已重定位（{self._game.name}）→ "
            f"{rep.candidate_root}"
            f"（对回 {rep.hit}/{rep.relocatable} 份记录）")
        QMessageBox.information(
            self, "重定位完成",
            f"备份目录已更新为：\n{rep.candidate_root}\n\n"
            f"对回 {rep.hit} / {rep.relocatable} 份备份记录。")
        self.accept()
