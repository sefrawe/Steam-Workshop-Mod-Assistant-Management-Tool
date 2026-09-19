"""游戏档案切换器
"""
"""
左导航顶部组件：下拉选择当前游戏 + 临时"添加档案"入口。
只与 ModRepository 接口交互，GUI 层零 SQL（记事本架构约定）。
"""
import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QInputDialog, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.models import Game


class GameSwitcher(QWidget):
    """current_game_changed(Game | None)：选择变化（含清空为空库态）时发射。"""

    current_game_changed = Signal(object)

    def __init__(self, repo, parent: QWidget | None = None, settings=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._games: list[Game] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self._emit_current)
        layout.addWidget(self._combo)

        self._btn_add = QPushButton("＋ 添加游戏档案（临时）", self)
        self._btn_add.setToolTip("正式向导在 M4 提供；此入口仅调试期使用")
        self._btn_add.clicked.connect(self.add_game_dialog)
        layout.addWidget(self._btn_add)

        self.reload()

    # ---------- 对外 ----------

    def reload(self) -> None:
        """重读 games 表。添加档案后调用；尽量保持当前选择不变。"""
        previous = self.current_game()
        self._games = self._repo.list_games()

        self._combo.blockSignals(True)
        self._combo.clear()
        for game in self._games:
            self._combo.addItem(f"{game.name}（{game.app_id}）", game.app_id)
        if not self._games:
            # 空库不崩：占位项，data=None → current_game() 返回 None
            self._combo.addItem("（暂无游戏档案）", None)
            self._combo.setEnabled(False)
        else:
            self._combo.setEnabled(True)
            if previous is not None:
                idx = self._combo.findData(previous.app_id)
                if idx >= 0:
                    self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)
        self._emit_current()

    def current_game(self) -> Game | None:
        app_id = self._combo.currentData()
        for game in self._games:
            if game.app_id == app_id:
                return game
        return None

    def add_game_dialog(self) -> None:
        """临时入口：三个输入框串联，糙但够调试用，M4 向导替换。"""
        text, ok = QInputDialog.getText(self, "添加游戏档案", "Steam AppID：")
        if not ok:
            return
        try:
            app_id = int(text.strip())
        except ValueError:
            QMessageBox.warning(self, "添加失败", "AppID 必须是整数。")
            return

        name, ok = QInputDialog.getText(self, "添加游戏档案", "游戏名称：")
        if not ok or not name.strip():
            return

        default_dir = self._settings.get("default_download_dir") if self._settings else ""
        download_dir, ok = QInputDialog.getText(
            self, "添加游戏档案", "mod 下载目录（steamcmd force_install_dir）：",
            text=default_dir)

        try:
            self._repo.add_game(app_id, name.strip(), download_dir.strip())
        except sqlite3.IntegrityError:  # 主键冲突 = 档案已存在（决策 4：显式爆炸）
            QMessageBox.warning(self, "添加失败", f"档案 {app_id} 已存在。")
            return
        self.reload()

    # ---------- 内部 ----------

    def _emit_current(self) -> None:
        self.current_game_changed.emit(self.current_game())
