"""网址批量导入页
"""
"""
基础功能页：工坊网址（或纯数字编号）粘贴 / 从文本文件读入 → 解析预览 →
批量登记为已收录，归属当前选中的游戏档案。
只写编号与链接，标题等信息由更新检测补全。
解析结果在切换游戏后作废，防止归错档案。
"""
import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core.models import Game, Mod
from core.urlParser import parse_lines
from gui.consolePanel import LogBus

_URL_TEMPLATE = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


class ImportPage(QWidget):
    """imported(int)：成功导入后发射，主窗口借此跳转 mod 库页并刷新。"""
    imported = Signal(int)

    def __init__(self, repo, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._new_ids: list[int] = []
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("网址批量导入", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        tip = QLabel(
            "把工坊网址粘贴到下面（每行一个，也接受纯数字编号，整段空格分隔亦可），"
            "解析预览后批量登记。\n导入只登记编号与链接，"
            "标题等详细信息在更新检测时自动补全。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        self._input = QPlainTextEdit(self)
        self._input.setPlaceholderText(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213\n"
            "2216850785")
        root.addWidget(self._input, 1)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        load_btn = QPushButton("从文本文件读入…", btn_row)
        load_btn.clicked.connect(self._on_load_file)
        clear_btn = QPushButton("清空", btn_row)
        clear_btn.clicked.connect(self._on_clear)
        h.addWidget(load_btn)
        h.addWidget(clear_btn)
        h.addStretch(1)
        parse_btn = QPushButton("解析预览", btn_row)
        parse_btn.clicked.connect(self._on_parse)
        h.addWidget(parse_btn)
        root.addWidget(btn_row)

        self._report = QLabel("", self)
        self._report.setWordWrap(True)
        root.addWidget(self._report)

        self._import_btn = QPushButton("导入（0 个）", self)
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._on_import)
        root.addWidget(self._import_btn)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        # 解析结果归属旧游戏时必须作废，防止导入归错档案
        if (self._game is not None and game is not None
                and self._game.app_id != game.app_id and self._new_ids):
            self._report.setText("游戏已切换，请重新解析预览后再导入。")
            self._report.setStyleSheet("color: #f76b15;")
            self._game = game
            self._new_ids = []
            self._import_btn.setEnabled(False)
            self._import_btn.setText("导入（0 个）")
        self._game = game
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
        else:
            self._game_label.setText(f"当前游戏：{game.name}（{game.app_id}）")

    # ---------- 槽 ----------

    def _on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择网址文本文件", "", "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            self._log.warn(f"读取文件失败：{path}")
            QMessageBox.warning(self, "读取失败", f"无法读取文件：{exc}")
            return
        current = self._input.toPlainText().rstrip()
        self._input.setPlainText(
            f"{current}\n{text.strip()}" if current else text.strip())
        self._on_parse()  # 读入后立即预览

    def _on_clear(self) -> None:
        self._input.clear()
        self._report.clear()
        self._new_ids = []
        self._import_btn.setEnabled(False)
        self._import_btn.setText("导入（0 个）")

    def _on_parse(self) -> None:
        self._new_ids = []
        self._import_btn.setEnabled(False)
        self._import_btn.setText("导入（0 个）")
        if self._game is None:
            self._report.setText("请先在左上角选择游戏档案，再解析导入。")
            self._report.setStyleSheet("color: #e5484d;")
            return
        report = parse_lines(self._input.toPlainText().splitlines())
        if not report.mod_ids and not report.invalid:
            self._report.setText("（没有可解析的内容——粘贴工坊网址或纯数字编号）")
            self._report.setStyleSheet("color: gray;")
            return
        # 空列表不查询（防 IN () 语法问题）
        existing = (set(self._repo.filter_existing_ids(report.mod_ids))
                    if report.mod_ids else set())
        self._new_ids = [i for i in report.mod_ids if i not in existing]
        dup = len(report.mod_ids) - len(self._new_ids)
        lines = [f"识别 {len(report.mod_ids)} 个编号："
                 f"可导入 {len(self._new_ids)} 个，"
                 f"已在库中（跳过）{dup} 个。"]
        if report.invalid:
            shown = "；".join(report.invalid[:5])
            lines.append(f"无法识别 {len(report.invalid)} 片段：{shown}"
                         + ("…" if len(report.invalid) > 5 else ""))
        self._log.info(
            f"解析预览：识别 {len(report.mod_ids)} 个，"
            f"可导入 {len(self._new_ids)} 个，跳过 {dup} 个"
            + (f"，无法识别 {len(report.invalid)} 个" if report.invalid else ""))
        self._report.setText("\n".join(lines))
        self._report.setStyleSheet(
            "color: #46a758;" if self._new_ids else "color: gray;")
        if self._new_ids:
            self._import_btn.setEnabled(True)
            self._import_btn.setText(f"导入（{len(self._new_ids)} 个）")

    def _on_import(self) -> None:
        game = self._game
        if game is None or not self._new_ids:
            return
        now = int(time.time())
        try:
            with self._repo.transaction():
                # 整批一个事务，中途失败全部回滚
                for mid in self._new_ids:
                    self._repo.add_mod(Mod(
                        mod_id=mid, game_id=game.app_id,
                        url=_URL_TEMPLATE.format(mid),
                        status="tracked", first_tracked_at=now,
                    ))
        except sqlite3.IntegrityError as exc:
            self._log.error(f"导入失败（数据库冲突）：{exc}")
            QMessageBox.warning(self, "导入失败",
                                f"写入数据库时冲突：{exc}\n请重新解析后再试。")
            return
        count = len(self._new_ids)
        self._log.ok(f"已为「{game.name}」导入 {count} 个 mod")
        QMessageBox.information(
            self, "导入完成",
            f"已为「{game.name}」登记 {count} 个 mod（状态：已收录）。\n"
            "点【更新检测】即可补全标题等信息。")
        self._on_clear()
        self.imported.emit(count)
