"""游戏档案切换器
"""
r"""gui/gameSwitcher.py · 左导航顶部组件：下拉选择当前游戏 + 建档入口 + 档案工具三入口。

档案工具方法（编辑 / 重定位 / 删除）由 V1 原样并入：入口在主窗口
「游戏」菜单，主窗口调用本组件的 open_* 方法，同一形状 = 拿当前
档案 → 开对话框 → 成功后 reload() 重新广播。
连接指引不迁：V2 的【联接检测】页已接管（原 LinkGuideDialog 退役，
检测通过写 game_mod_dir 的职责在页面内完成）——「游戏」菜单的
「连接指引」直接跳页，不经本组件。

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


class GameSwitcher(QWidget):
    """current_game_changed(Game | None)：选择变化（含清空为空库态）时发射。
    overview_requested(int)：删除对话框请求跳备份总览（主窗口接）。"""

    current_game_changed = Signal(object)
    overview_requested = Signal(int)

    def __init__(self, repo, parent: QWidget | None = None,
                 settings=None, log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings   # 读"steamcmd 程序路径"做目录推导用
        # 终端日志总线（MainWindow 注入，与其他页面同一实例）。
        # None 时功能照常，只是写入动作不进日志面板
        self._log = log
        # 当前 games 表的内存镜像：下拉框显示它，current_game() 靠它反查
        self._games: list[Game] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 下拉框：显示"名称（AppID）"，data 里存 app_id，
        # current_game() 靠它反查 Game 对象
        self._combo = QComboBox(self)
        self._combo.currentIndexChanged.connect(self._emit_current)
        layout.addWidget(self._combo)

        self._btn_add = QPushButton("＋ 添加游戏档案", self)
        self._btn_add.setToolTip(
            "输入 AppID 一屏建档：自动查名、查重、下载目录推导预览")
        self._btn_add.clicked.connect(self.add_game_dialog)
        layout.addWidget(self._btn_add)

        # 档案操作入口（编辑/重定位/删除）在主窗口「游戏」菜单，
        # 本组件负责切换游戏与建档；连接指引走【联接检测】页
        self.reload()

    # ---------- 对外 ----------
    def reload(self, *, broadcast: bool = True) -> None:
        """重读 games 表。添加/删除档案、导入账本后由调用方触发；
        尽量保持当前选择不变。当前档案被删时 findData 落空 → 停在
        剩余第一项并广播（删档收尾正好靠这个语义：界面各页自动切到
        新当前档案；档案被删光时广播 None，界面整体归零）。

        broadcast=False：只刷新内存里的档案清单和下拉框，不发广播——
        给"页面自己写完档案字段、把最新值同步进来"的场景用（联接
        检测页写完目录后调的就是它）：此刻各页都停在原地，广播反而
        会把人家页面上的现场清掉。"""
        previous = self.current_game()
        self._games = self._repo.list_games()
        # 先掐断信号再重建选项，防止重建过程中 currentIndexChanged
        # 每加一项就乱发一次；重建完手动补发一次（_emit_current），
        # 保证外部拿到的是最终状态
        self._combo.blockSignals(True)
        self._combo.clear()
        for game in self._games:
            self._combo.addItem(f"{game.name}（{game.app_id}）", game.app_id)
        if not self._games:
            # 空库不崩：放一个占位项（data=None → current_game() 返回
            # None），下拉置灰；【添加档案】按钮保持可用——空库也能
            # 建第一个档案
            self._combo.addItem("（暂无游戏档案）", None)
            self._combo.setEnabled(False)
        else:
            self._combo.setEnabled(True)
            if previous is not None:
                idx = self._combo.findData(previous.app_id)
                if idx >= 0:
                    self._combo.setCurrentIndex(idx)
        # 恢复信号 + 广播都无条件执行（放在 if/else 外面）：空库分支
        # 也要恢复信号屏蔽、也要广播"没有档案了"——档案全删光时，
        # 各页必须跟着归零，不能还拿着已删档案的旧值
        self._combo.blockSignals(False)
        if broadcast:
            self._emit_current()

    def current_game(self) -> Game | None:
        """当前选中的档案；空库（或占位项）返回 None。"""
        app_id = self._combo.currentData()
        for game in self._games:
            if game.app_id == app_id:
                return game
        return None

    def set_current_by_app_id(self, app_id: int) -> bool:
        """程序化选中某档案（启动恢复"上次打开的档案"用）。找到并
        选中返回 True；下拉里没有（档案被删过/账本导入替换过）返回
        False——调用方静默跳过，维持"落到第一项"的现状，不报错。
        setCurrentIndex 触发 currentIndexChanged → _emit_current 广播，
        与用户手点下拉同一条路；目标已是当前项时 Qt 不发信号，但那时
        当前档案本来就是它，正好无需广播。"""
        idx = self._combo.findData(app_id)
        if idx < 0:
            return False
        self._combo.setCurrentIndex(idx)
        return True

    def add_game_dialog(self) -> None:
        """建档入口：一屏对话框（查重 → 自动查名 → 目录推导预览 → 落账）。
        落账成功后 reload() 并把新档案设为当前——刚加完大概率马上要
        导 mod，停在旧档案上会让人以为"没加上"。"""
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
        dlg = GameAddDialog(self._repo, self._make_api(), lambda aid: steamPaths.workshop_content_dir(
            self._settings.get("steamcmd_path"), aid), self,
                            derive_backup_dir=lambda aid: steamPaths.backup_root_default(
                                self._settings.get("steamcmd_path"), aid))

        dlg.exec()
        if dlg.created_app_id is None:
            return   # 用户取消，或建档未成（对话框内已报过原因）
        if self._log is not None:
            self._log.ok(f"游戏档案已创建（AppID {dlg.created_app_id}），"
                         "已设为当前档案")
        self.reload()
        idx = self._combo.findData(dlg.created_app_id)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)   # 触发下拉信号 → 广播新档案

    # ---------- 档案工具（V1 原样并入；入口在主窗口「游戏」菜单）----------
    def open_edit(self) -> None:
        """打开档案编辑对话框。空库时菜单项已禁用，这里双保险。
        保存成功（对话框 accept）后 reload()：重读 games 表并把当前
        档案重新广播给各页——改名后下拉框立即换新名字，持有旧 Game
        对象的页面（备份页等）同步拿到新字段（与重定位同一套收尾）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = GameEditDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()

    def open_relocate(self) -> None:
        """打开备份目录重定位对话框。空库时菜单项已禁用，这里双保险。
        预演引擎 = core/backupRelocate.preview（只读数命中率）；确认
        成功后 reload()：重读 games 表并把当前档案重新广播给各页——
        backupPage 等持有旧 Game 对象的页面据此拿到新的 backup_dir
        （set_game → 各自重载）。"""
        game = self.current_game()
        if game is None:
            return
        dlg = BackupRelocateDialog(self._repo, game, self._settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.reload()

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
            # 同一出口。backup_to 是 SQLiteRepository 的方法（R16 刻意
            # 不进契约），运行时实例就是它
            out = self._repo.backup_to(appPaths.data_dir() / "backup")
            return str(out)

        dlg = GameDeleteDialog(self._repo, game.app_id, db_backup=db_backup,
                               parent=self)
        dlg.overview_requested.connect(self.overview_requested)
        dlg.exec()
        if dlg.deleted:
            if self._log is not None:
                self._log.ok(f"档案「{game.name}（{game.app_id}）」已删除；"
                             "名下账目已清，磁盘文件按勾选处理")
            self.reload()

    # ---------- 内部 ----------
    def _make_api(self) -> SteamApiClient:
        """按次构造查名用的 Steam 客户端（参数现取、用完即弃）。
        查名走 Steam 商店 appdetails 接口，无需 API key。"""
        return SteamApiClient()

    def _emit_current(self) -> None:
        """把当前选择广播出去。下拉框变化、reload() 末尾都会走到这里。"""
        self.current_game_changed.emit(self.current_game())
