"""游戏档案切换器
"""
"""左导航顶部组件：下拉选择当前游戏 + 临时"添加档案"入口。
档案工具方法（编辑档案 / 连接指引 / 重定位备份目录）保留在本组件，
但入口按钮已迁往主窗口「游戏」菜单，由主窗口调用。
只与 ModRepository 接口交互，GUI 层零 SQL（记事本架构约定）。

"添加游戏档案"目前是输入框串联的临时对话框，够调试期用；
正式的建档向导做好后，把按钮指到向导即可，本组件其余部分不用动。

下载目录的来历（决策 21）：不再让用户手填，而是从设置页的 steamcmd
程序路径自动推导——<steamcmd根>\\steamapps\\workshop\\content\\<appid>
（推导公式在 core/steamPaths.py，建档、扫描、备份引擎共用）。
steamcmd 程序路径没填时推导不出来，此时降级为手填——
宁可让用户填一次，也不能把空值存进档案；万一填错了，
【编辑档案】的"改为推导值"就是兜底通道（T19⑪）。

连接指引的落账（T19㉒ 第 1 步）：指引里检测通过（正向/反向都算）
时，把"游戏实际读取 mod 的目录"写入档案 game_mod_dir 字段——
见 open_link_guide 与 _on_link_check_passed 的注释。
"""
import sqlite3

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.backupRelocateDialog import BackupRelocateDialog
from gui.gameEditDialog import GameEditDialog
from core import steamPaths
from core.models import Game
from gui.linkGuideDialog import LinkGuideDialog


class GameSwitcher(QWidget):
    """current_game_changed(Game | None)：选择变化（含清空为空库态）时发射。"""
    current_game_changed = Signal(object)

    def __init__(self, repo, parent: QWidget | None = None,
                 settings=None, log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 读"steamcmd 程序路径"做目录推导用，可为 None
        # 终端日志总线（MainWindow 注入，与其他页面同一实例）。
        # None 时功能照常，只是写入动作不进日志面板——所以 MainWindow
        # 务必把 log 传进来（见交付说明）
        self._log = log
        # T19㉒：连接指引检测通过后置位，对话框关闭后统一 reload()
        self._guide_dirty = False
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

        # 档案操作按钮（添加/编辑/连接指引/重定位）已迁往主窗口
        # 「游戏」菜单，本组件只负责切换游戏
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
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
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
    def open_edit(self) -> None:
        """打开档案编辑对话框（T19⑪）。空库时按钮本就禁用，这里双保险。
        保存成功（对话框 accept）后 reload()：重读 games 表并把当前档案
        重新广播给各页——改名后下拉框立即换新名字，持有旧 Game 对象的
        页面（备份页等）同步拿到新字段（与重定位按钮同一套收尾）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = GameEditDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()

    def open_link_guide(self) -> None:
        """打开连接指引对话框（T13）。空库时按钮本就禁用，这里双保险。
        联接应指向的"实体"优先按 steamcmd 当前位置现推导（决策 21 的
        权威公式），推导不出才退回档案记录值——历史建档时手填的下载
        目录可能记的是游戏侧联接位置，现推导让指引与这类历史数据解耦。

        T19㉒：检测通过（正向/反向）时对话框发 check_passed(目录)，
        本组件据此把游戏读取目录写入档案 game_mod_dir（见
        _on_link_check_passed）。写库在检测通过瞬间就发生（用户看到
        绿勾时账已落）；界面刷新等对话框关闭后统一 reload()——
        把带上新 game_mod_dir 的当前档案重新广播给各页，
        与 open_edit / open_relocate 同一套收尾。
        """
        game = self.current_game()
        if game is None:
            return
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
        target = steamPaths.workshop_content_dir(steamcmd_exe, game.app_id) \
                 or game.download_dir
        self._guide_dirty = False
        dlg = LinkGuideDialog(game, self, target_dir=target)
        dlg.check_passed.connect(self._on_link_check_passed)
        dlg.exec()
        if self._guide_dirty:
            self._guide_dirty = False
            self.reload()

    def _on_link_check_passed(self, path: str) -> None:
        """连接指引检测通过（T19㉒ 第 1 步）：把游戏实际读取 mod 的
        目录写入档案 game_mod_dir。

        为什么可以不问直接写：这个值就是用户刚在指引里填、且被判定为
        "已连接"的目录，写入只是把刚确认的事实落进现有字段
        （update_game 正门，不加字段、schema 不动）；填错了随时可在
        【编辑档案】改回，不是危险操作，所以不弹窗、只记一行日志。

        去重：档案里已是同一目录（casefold 比较，R14 口径）时不写库
        不记日志——用户反复点【检测】不应产生反复的写入记录。

        这里只写库 + 置"待刷新"标记，不动界面；reload() 由
        open_link_guide 在对话框关闭后统一做（不在模态对话框的
        事件循环里改下拉框、广播档案，避免刷新穿插在用户操作中间）。
        """
        game = self.current_game()
        if game is None:  # 理论不可达：对话框是按当前档案打开的
            return
        new = str(path or "").strip().strip('"').strip()
        if not new:
            return
        recorded = str(game.game_mod_dir or "").strip().strip('"').strip()
        if new.casefold() == recorded.casefold():
            return  # 档案里已经是这个值：不写、不刷、不记
        self._repo.update_game(game.app_id, game_mod_dir=new)
        self._guide_dirty = True
        if self._log is not None:
            self._log.ok(
                f"连接指引检测通过：已把游戏读取目录写入档案"
                f"「{game.name}」：{new}")

    def _emit_current(self) -> None:
        """把当前选择广播出去。下拉框变化、reload() 末尾都会走到这里。"""
        self.current_game_changed.emit(self.current_game())

    def open_relocate(self) -> None:
        """打开备份目录重定位对话框（T21④）。空库时按钮本就禁用，
        这里双保险。确认成功后 reload()：重读 games 表并把当前档案
        重新广播给各页——backupPage 等持有旧 Game 对象的页面据此拿到
        新的 backup_dir（set_game → 各自重载）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = BackupRelocateDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()
