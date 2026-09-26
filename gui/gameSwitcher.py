"""游戏档案切换器
"""
r"""gui/gameSwitcher.py · 左导航顶部组件：下拉选择当前游戏 + 建档入口。

档案工具方法（编辑 / 连接指引 / 重定位 / 删除）保留在本组件，入口
按钮在主窗口「游戏」菜单，由主窗口调用。

建档：一屏 GameAddDialog——AppID 查重 → 自动查名（后台线程，Steam
商店 appdetails 接口，无需 key）→ 下载目录推导预览 → 确认创建。
下载目录仍按决策 21 从 steamcmd 位置推导，推导不出存空串（steamcmd
配好后首次扫描经 refresh_download_dir 自动回填），建档不再被卡。
正式建档向导做好后本入口进一步升级为向导第一步，组件其余部分不动。

删除：open_delete 打开 GameDeleteDialog——先盘点名下账目、确认后
自动做数据库快照兜底、一个事务清账、按勾选删磁盘备份目录（默认
保留）。删的是当前档案时 reload() 自动落到剩余第一项并广播。

跳转转发：删除对话框点「先去备份总览看看」→ 本组件发
overview_requested(app_id)，主窗口据此跳备份总览页并按档案过滤。

只与 ModRepository / 三个对话框交互，GUI 层零 SQL（记事本架构约定）。
数据库快照调用 repo.backup_to（SQLiteRepository 的方法，R16 刻意不进
契约；滚动保留 3 份）——与备份页「备份数据库」同一出口同一目录。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core import appPaths
from core import steamPaths
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.backupRelocateDialog import BackupRelocateDialog
from gui.gameAddDialog import GameAddDialog
from gui.gameDeleteDialog import GameDeleteDialog
from gui.gameEditDialog import GameEditDialog
from gui.linkGuideDialog import LinkGuideDialog


class GameSwitcher(QWidget):
    """current_game_changed(Game | None)：选择变化（含清空为空库态）时发射。
    overview_requested(int)：删除对话框请求跳备份总览（主窗口接）。"""
    current_game_changed = Signal(object)
    overview_requested = Signal(int)

    def __init__(self, repo, parent: QWidget | None = None,
                 settings=None, log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 读"steamcmd 程序路径"做目录推导用
        # 终端日志总线（MainWindow 注入，与其他页面同一实例）。
        # None 时功能照常，只是写入动作不进日志面板
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

        self._btn_add = QPushButton("＋ 添加游戏档案", self)
        self._btn_add.setToolTip(
            "输入 AppID 一屏建档：自动查名、查重、下载目录推导预览；"
            "「游戏」菜单里也有同一入口")
        self._btn_add.clicked.connect(self.add_game_dialog)
        layout.addWidget(self._btn_add)

        # 档案操作入口（编辑/连接指引/重定位/删除）在主窗口「游戏」菜单，
        # 本组件负责切换游戏与建档
        self.reload()

    # ---------- 对外 ----------

    def reload(self) -> None:
        """重读 games 表。添加/删除档案后调用；尽量保持当前选择不变。
        当前档案被删时 findData 落空 → 停在剩余第一项并广播（删档收尾
        正好靠这个语义：界面各页自动切到新当前档案）。"""
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
        """建档入口：一屏对话框（查重 → 自动查名 → 目录推导预览 → 落账）。
        落账成功后 reload() 并把新档案设为当前——刚加完大概率马上要导
        mod，停在旧档案上会让人以为"没加上"（旧流程同一收尾语义）。"""
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
        dlg = GameAddDialog(
            self._repo, self._make_api(),
            # appid → 下载目录推导（core 的权威公式，core 不在对话框里 import）
            lambda aid: steamPaths.workshop_content_dir(steamcmd_exe, aid),
            self)
        dlg.exec()
        if dlg.created_app_id is None:
            return  # 用户取消，或建档未成（对话框内已报过原因）
        if self._log is not None:
            self._log.ok(f"游戏档案已创建（AppID {dlg.created_app_id}），"
                         "已设为当前档案")
        self.reload()
        idx = self._combo.findData(dlg.created_app_id)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)  # 触发下拉信号 → 广播新档案

    def _make_api(self) -> SteamApiClient:
        """按次构造查名用的 Steam 客户端（与 backupPage._make_manager
        同一思路：参数现取、用完即弃）。appdetails 商店接口无需 API key。"""
        return SteamApiClient()

    def open_delete(self) -> None:
        """删除当前档案全流程（游戏菜单入口）。空库时菜单项已置灰，
        这里双保险。盘点、确认、快照、清账、删盘全在对话框里；这里只
        负责收尾——reload() 把"档案没了"广播给所有页面（删的是当前
        档案时下拉自动落到剩余第一项，见 reload 注释）。"""
        game = self.current_game()
        if game is None:
            QMessageBox.information(self, "删除档案", "当前未选择游戏档案。")
            return

        def db_backup() -> str:
            # 清账前的数据库快照兜底：进既有的滚动备份目录 data/backup
            # （R16 在线备份 API + 滚动保留 3 份），与备份页「备份数据库」
            # 同一出口。backup_to 是 SQLiteRepository 的方法（R16 刻意不进
            # 契约），运行时实例就是它
            out = self._repo.backup_to(appPaths.data_dir() / "backup")
            return str(out)

        dlg = GameDeleteDialog(self._repo, game.app_id,
                               db_backup=db_backup, parent=self)
        dlg.overview_requested.connect(self.overview_requested)
        dlg.exec()
        if dlg.deleted:
            if self._log is not None:
                self._log.ok(f"档案「{game.name}（{game.app_id}）」已删除；"
                             "名下账目已清，磁盘文件按勾选处理")
            self.reload()

    # ---------- 内部 ----------

    def open_edit(self) -> None:
        """打开档案编辑对话框（T19⑪）。空库时菜单项已禁用，这里双保险。
        保存成功（对话框 accept）后 reload()：重读 games 表并把当前档案
        重新广播给各页——改名后下拉框立即换新名字，持有旧 Game 对象的
        页面（备份页等）同步拿到新字段（与重定位同一套收尾）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = GameEditDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()

    def open_link_guide(self) -> None:
        """打开连接指引对话框（T13）。空库时菜单项已禁用，这里双保险。
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
        if game is None:
            # 理论不可达：对话框是按当前档案打开的
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
        """打开备份目录重定位对话框（T21④）。空库时菜单项已禁用，
        这里双保险。确认成功后 reload()：重读 games 表并把当前档案
        重新广播给各页——backupPage 等持有旧 Game 对象的页面据此拿到
        新的 backup_dir（set_game → 各自重载）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = BackupRelocateDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()
