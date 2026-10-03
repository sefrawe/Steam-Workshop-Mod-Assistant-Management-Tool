"""游戏档案切换器
"""
r"""gui/gameSwitcher.py —— 左导航顶部组件：下拉选择当前游戏 + 添加档案入口。

本组件只做两件事，越界的事一件不做：
- 选档案：下拉框显示"游戏名（AppID）"，用户一换选择就发信号——
  主窗口接到信号后负责刷新各页面、更新状态栏等一切后续反应；
  本组件只负责"喊一嗓子"（发信号），不关心谁在听、不直接驱动
  任何页面。
- 建档案：一颗【添加游戏档案】按钮，打开一屏 GameAddDialog
  （AppID 查重 → 自动查名 → 下载目录推导预览 → 确认创建）。
  这是"临时入口"：等首次使用向导做好，这里升级成向导第一步，
  组件其余部分不动——两条路一本账，不冲突。

为什么"临时"够用：建档本身已是一屏完整对话框（查重防撞车、
查名免打字、目录推导防手填错），向导只是把它排进更大的引导
流程，不重做任何逻辑。

档案工具方法（编辑 / 连接指引 / 重定位 / 删除）按旧版分工住在
主窗口「游戏」菜单；等 GameEditDialog / LinkGuideDialog /
BackupRelocateDialog / GameDeleteDialog 搬迁后，在本组件补
open_* 方法（同一形状：拿当前档案 → 开对话框 → 成功后
reload() 重新广播）。本轮刻意不写——写了就要 import 还没
搬来的对话框，启动即炸。

架构约定：
- 只与 ModRepository 接口交互，GUI 层零 SQL（记事本架构约定）；
- repo 调用全部在主线程（list_games / get_game 都是毫秒级单查询；
  项目红线 R11 是"工作线程绝不碰 SQLite"，反过来主线程直调
  契约层是允许的）；
- 日志走注入的 LogBus（None 时功能照常，只是动作不进日志面板）。

下载目录推导：core/steamPaths.workshop_content_dir 是全项目唯一
的推导公式落点。本组件用它拼一个函数注入给对话框——对话框自己
不 import steamPaths，推导细节永远只在 core 一处（公式写两遍，
早晚改出不一致）。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QPushButton, QVBoxLayout, QWidget

from core import steamPaths
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.gameAddDialog import GameAddDialog


class GameSwitcher(QWidget):
    """信号：current_game_changed(Game | None)——选择变化时发射，
    包括"档案全被删光"清空为 None 的情况。监听方（主窗口）据此
    刷新各页面；本组件不直接驱动任何页面。"""

    current_game_changed = Signal(object)

    def __init__(self, repo, parent: QWidget | None = None,
                 settings=None, log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 读"steamcmd 程序路径"做目录推导用
        # 终端日志总线（主窗口注入，与其他页面同一实例）。
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

        self.reload()

    # ---------- 对外 ----------

    def reload(self) -> None:
        """重读 games 表。添加/删除档案、导入账本后由调用方触发；
        尽量保持当前选择不变。当前档案被删时 findData 落空 →
        停在剩余第一项并广播（删档收尾正好靠这个语义：界面各页
        自动切到新当前档案）。"""
        previous = self.current_game()
        self._games = self._repo.list_games()

        # 先掐断信号（blockSignals）再重建选项，防止重建过程中
        # currentIndexChanged 每加一项就乱发一次；重建完手动补发
        # 一次（_emit_current），保证外部拿到的是最终状态
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
        self._combo.blockSignals(False)
        self._emit_current()

    def current_game(self) -> Game | None:
        """当前选中的档案；空库（或占位项）返回 None。"""
        app_id = self._combo.currentData()
        for game in self._games:
            if game.app_id == app_id:
                return game
        return None

    def set_current_by_app_id(self, app_id: int) -> bool:
        """程序化选中某档案（启动恢复"上次打开的档案"用，主窗口在
        会话恢复时调用）。找到并选中返回 True；下拉里没有（档案被
        删过/账本导入替换过）返回 False——调用方静默跳过，维持
        "落到第一项"的现状，不报错。
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
        dlg = GameAddDialog(
            self._repo,
            self._make_api(),
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

    # ---------- 内部 ----------

    def _make_api(self) -> SteamApiClient:
        """按次构造查名用的 Steam 客户端（参数现取、用完即弃）。
        查名走 Steam 商店 appdetails 接口，无需 API key。"""
        return SteamApiClient()

    def _emit_current(self) -> None:
        """把当前选择广播出去。下拉框变化、reload() 末尾都会走到这里。"""
        self.current_game_changed.emit(self.current_game())
