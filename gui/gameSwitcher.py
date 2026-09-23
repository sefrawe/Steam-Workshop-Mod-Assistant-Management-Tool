"""游戏档案切换器
"""
r"""
左导航顶部组件：下拉选择当前游戏 + 临时"添加档案"入口 + 连接指引入口（T13）。

只与 ModRepository 接口交互，GUI 层零 SQL（记事本架构约定）。

"添加游戏档案"目前是输入框串联的临时对话框，够调试期用；
正式的建档向导做好后，把按钮指到向导即可，本组件其余部分不用动。

下载目录的来历（决策 21）：不再让用户手填，而是从设置页的
steamcmd 程序路径自动推导——<steamcmd根>\steamapps\workshop\content\<appid>
（推导公式在 core/steamPaths.py，建档、扫描、将来的备份引擎共用）。
steamcmd 程序路径没填时推导不出来，此时降级为手填——
宁可让用户填一次，也不能把空值存进档案：档案建成后目前
没有编辑界面，空下载目录没有任何补救通道。
"""
import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QInputDialog, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core import steamPaths
from core.models import Game
from gui.linkGuideDialog import LinkGuideDialog


class GameSwitcher(QWidget):
    """current_game_changed(Game | None)：选择变化（含清空为空库态）时发射。"""
    current_game_changed = Signal(object)

    def __init__(self, repo, parent: QWidget | None = None, settings=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 读"steamcmd 程序路径"做目录推导用，可为 None
        self._games: list[Game] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        # 下拉框：显示"名称（AppID）"，data 里存 app_id，current_game() 靠它反查
        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self._emit_current)
        layout.addWidget(self._combo)
        self._btn_add = QPushButton("＋ 添加游戏档案（临时）", self)
        self._btn_add.setToolTip(
            "正式的建档向导做好后此入口会被替换；当前仅供调试期手工建档")
        self._btn_add.clicked.connect(self.add_game_dialog)
        layout.addWidget(self._btn_add)
        self._btn_link = QPushButton("连接指引…", self)
        self._btn_link.setToolTip(
            "把游戏自己的 mod 目录联接（junction）到本档案的下载目录，"
            "让游戏读到 steamcmd 下载的 mod。\n"
            "只生成命令和步骤，命令由你自己在 cmd 里执行（对话框全程只读）")
        self._btn_link.clicked.connect(self._open_link_guide)
        layout.addWidget(self._btn_link)

        self.reload()

    # ---------- 对外 ----------
    def reload(self) -> None:
        """重读 games 表。添加档案后调用；尽量保持当前选择不变。"""
        previous = self.current_game()
        self._games = self._repo.list_games()
        # 先掐断信号再重建选项，防止中途 currentIndexChanged 乱发；
        # 重建完手动补发一次，保证外部拿到的是最终状态
        self._combo.blockSignals(True)
        self._combo.clear()
        for game in self._games:
            self._combo.addItem(f"{game.name}（{game.app_id}）", game.app_id)
        if not self._games:
            # 空库不崩：放一个占位项，data=None → current_game() 返回 None
            self._combo.addItem("（暂无游戏档案）", None)
            self._combo.setEnabled(False)
            self._btn_link.setEnabled(False)
        else:
            self._combo.setEnabled(True)
            self._btn_link.setEnabled(True)
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
        """临时建档入口。下载目录自动推导（决策 21），推导不出降级手填。"""
        # 第一问：AppID。工坊网址里的那串数字就是它，必须是整数（数据库主键）。
        text, ok = QInputDialog.getText(self, "添加游戏档案", "Steam AppID：")
        if not ok:
            return
        try:
            app_id = int(text.strip())
        except ValueError:
            QMessageBox.warning(self, "添加失败", "AppID 必须是整数。")
            return
        # 第二问：显示名称。
        name, ok = QInputDialog.getText(self, "添加游戏档案", "游戏名称：")
        if not ok or not name.strip():
            return

        # 第三步：确定下载目录。优先自动推导；推导不出（steamcmd 未配置）
        # 降级手填。这一步只确定值，提示统一放在建档成功之后。
        steamcmd_exe = self._settings.get("steamcmd_path") if self._settings else ""
        derived = steamPaths.workshop_content_dir(steamcmd_exe, app_id)
        if derived is not None:
            download_dir = derived
        else:
            download_dir, ok = QInputDialog.getText(
                self, "添加游戏档案",
                "尚未设置 steamcmd 程序路径，无法自动推导下载目录。\n"
                "请手动填写本档案的 mod 下载目录"
                f"（steamcmd 工坊内容目录，如 …\\steamapps\\workshop\\content\\{app_id}）：")
            if not ok or not download_dir.strip():
                return
            download_dir = download_dir.strip()

        try:
            self._repo.add_game(app_id, name.strip(), download_dir)
        except sqlite3.IntegrityError:
            # 重复主键数据库会直接报错（错误显式暴露、绝不静默吞掉），
            # 这里只负责把报错翻译成一句用户能懂的话。
            QMessageBox.warning(self, "添加失败", f"档案 {app_id} 已存在。")
            return
        self.reload()
        # 新建的档案直接设为当前：刚加完大概率马上要导 mod，
        # 停在旧档案上会让人以为"没加上"。
        # setCurrentIndex 会触发下拉框信号 → 正常广播新档案给所有页面
        idx = self._combo.findData(app_id)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        if derived is not None:
            # 建档成功后才告知推导结果：用户应当知道档案用到了哪个目录，
            # 也顺手把"content 树只放编号文件夹"的规矩带一句
            QMessageBox.information(
                self, "档案已创建",
                f"「{name.strip()}」（{app_id}）创建成功。\n"
                f"下载目录按 steamcmd 位置自动推导为：\n{derived}\n\n"
                "注意：该目录只放 mod 内容，请勿手动放入其他文件。")

    # ---------- 内部 ----------
    def _open_link_guide(self) -> None:
        """打开连接指引对话框（T13）。空库时按钮本就禁用，这里双保险。

        联接应指向的"实体"优先按 steamcmd 当前位置现推导（决策 21 的
        权威公式），推导不出才退回档案记录值——历史建档时手填的下载
        目录可能记的是游戏侧联接位置，现推导让指引与这类历史数据解耦。
        """
        game = self.current_game()
        if game is None:
            return
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
        target = steamPaths.workshop_content_dir(steamcmd_exe, game.app_id) \
                 or game.download_dir
        LinkGuideDialog(game, self, target_dir=target).exec()

    def _emit_current(self) -> None:
        """把当前选择广播出去。下拉框变化、reload() 末尾都会走到这里。"""
        self.current_game_changed.emit(self.current_game())
