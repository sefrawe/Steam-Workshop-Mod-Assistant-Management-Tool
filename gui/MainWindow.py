"""主窗口骨架
"""
"""
结构：左导航（游戏切换器 + 导航列表）+ 中央页面栈 + 底部控制台 Dock。
控制台含"运行日志 / steamcmd 终端"两个标签页，各页面通过 LogBus 打日志。
中央页面从 mod 库页起逐个替换占位；终端的交互实现属于后续阶段。
"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget, QMessageBox,
)

from core.appSettings import AppSettings
from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.backupPage import BackupPage
from gui.commandGenPage import CommandGenPage
from gui.consolePanel import ConsolePanel, LogBus
from gui.batchDownloadController import BatchDownloadController

from gui.gameSwitcher import GameSwitcher
from gui.importPage import ImportPage
from gui.modListPage import ModListPage
from gui.settingsPage import SettingsPage
from gui.updateCheckPage import UpdateCheckPage
from gui.verifyPage import VerifyPage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "mods.db"

_NAV_WIDTH = 210

# 左导航树：整数 = 页面栈下标（真实页面）；None = 未完成模块，灰色"开发中"。
# 完成模块时：在 _pages 末尾 append 新页面（栈顺序不再需要和导航一致），
# 然后把对应条目的 None 改成新下标、去掉后缀、setDisabled(False) 即点亮。
_NAV_SCHEMA: list[tuple[str, int | list[tuple[str, int | None]]]] = [
    ("mod 库", 0),
    ("基础功能", [
        ("网址批量导入", 1),
        ("更新检测", 4),
        ("下载命令生成", 5),
        ("账实核验", 6),
    ]),
    ("备份管理", [
        ("备份与恢复", 2),
    ]),
    ("设置", 3),
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Steam创意工坊Mod辅助管理工具")
        self.resize(1200, 800)

        # 设置必须先建：数据库构造时要从中读"快照保留条数"注入
        self._settings = AppSettings()
        # 日志总线先于页面建好，构造页面时注入
        self._log = LogBus()
        # 数据库操作全在主线程（毫秒级）；联网/扫描等长操作走各自页面的工作线程
        self._repo = SQLiteRepository(
            DEFAULT_DB_PATH,
            snapshot_keep=self._settings.get_int("snapshot_keep", 5))
        self._current_game: Game | None = None

        self._build_central()
        self._build_console_dock()
        self._build_menus()
        self._build_status_bar()
        # 构造期 switcher 已发射过信号（当时无人监听），补一次初始化状态栏
        self._on_game_changed(self._switcher.current_game())

    # ---------- UI 构建 ----------

    def _build_central(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget(central)
        side.setFixedWidth(_NAV_WIDTH)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(8)

        self._switcher = GameSwitcher(self._repo, side, settings=self._settings)
        self._switcher.current_game_changed.connect(self._on_game_changed)
        side_layout.addWidget(self._switcher)

        self._nav = QTreeWidget(side)
        self._nav.setHeaderHidden(True)
        self._nav.setIndentation(14)
        self._nav_items: dict[int, QTreeWidgetItem] = {}  # 页面下标 → 树条目
        for name, spec in _NAV_SCHEMA:
            if isinstance(spec, int):
                item = QTreeWidgetItem([name])
                item.setData(0, Qt.ItemDataRole.UserRole, spec)
                self._nav.addTopLevelItem(item)
                self._nav_items[spec] = item
                continue
            group = QTreeWidgetItem([name])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)  # 组节点不可选中，只负责折叠
            for label, index in spec:
                child = QTreeWidgetItem(
                    [label if index is not None else f"{label}（开发中）"])
                if index is None:
                    child.setDisabled(True)  # 灰色、点不动
                else:
                    child.setData(0, Qt.ItemDataRole.UserRole, index)
                    self._nav_items[index] = child
                group.addChild(child)
            group.setExpanded(True)
            self._nav.addTopLevelItem(group)
        self._nav.currentItemChanged.connect(self._on_nav_changed)
        self._nav.itemClicked.connect(self._on_nav_clicked)
        side_layout.addWidget(self._nav, 1)

        self._stack = QStackedWidget(central)
        self._pages = [
            ModListPage(self._repo, self._settings, self._stack, log=self._log),  # 0
            ImportPage(self._repo, self._stack, log=self._log),                   # 1
            BackupPage(self._repo, self._settings, self._stack, log=self._log),   # 2
            SettingsPage(self._settings, self._stack),                            # 3
            UpdateCheckPage(self._repo, self._settings, self._stack, log=self._log),  # 4
            CommandGenPage(self._repo, self._settings, self._stack, log=self._log),   # 5
            VerifyPage(self._repo, self._settings, self._stack, log=self._log),   # 6
        ]
        for page in self._pages:
            self._stack.addWidget(page)

        self._pages[1].imported.connect(self._on_imported)
        self._pages[4].checks_finished.connect(self._on_checks_finished)
        # mod 库页与账实核验页共用同一份跳转契约：
        # 发出 mod id 列表 → 切到命令生成页并只勾选这些 mod
        self._pages[0].command_gen_requested.connect(self._on_command_gen_requested)
        self._pages[6].command_gen_requested.connect(self._on_command_gen_requested)
        self._pages[0].download_requested.connect(self._start_batch)  # 前缀照抄上一行
        self._pages[0].backup_requested.connect(self._backup_checked)  # 前缀照抄上一行



        self._nav.setCurrentItem(self._nav_items[0])

        root.addWidget(side)
        root.addWidget(self._stack, 1)
        self.setCentralWidget(central)

    def _build_console_dock(self) -> None:
        self._console_dock = QDockWidget("控制台", self)
        console_panel = ConsolePanel(self._log, self._console_dock,
                                     settings=self._settings)
        self._console_dock.setWidget(console_panel)
        # 新消息自动弹出：面板发现控制台被关着又有新日志时发信号，
        # 这里负责把停靠窗拉回屏幕（开关在运行日志页的勾选框）
        console_panel.show_requested.connect(self._pop_console)
        # 批量下载控制器：整软件一个实例（steamcmd 单实例 → 单批次），
        # 把终端信号、批次卡片和流程状态机缝在一起
        self._batch_controller = BatchDownloadController(
            console_panel.terminal, console_panel.step_list,
            self._log, self._settings, self)

        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                           self._console_dock)

    def _build_menus(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")
        act_add_game = QAction("添加游戏档案（临时）…", self)
        act_add_game.triggered.connect(self._switcher.add_game_dialog)
        m_file.addAction(act_add_game)
        m_file.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        m_view = self.menuBar().addMenu("视图(&V)")
        m_view.addAction(self._console_dock.toggleViewAction())

    def _build_status_bar(self) -> None:
        self._status_game = QLabel(self)
        self._status_db = QLabel(f"数据库：{DEFAULT_DB_PATH}", self)
        self.statusBar().addWidget(self._status_game)
        self.statusBar().addPermanentWidget(self._status_db)

    # ---------- 槽 ----------

    def _on_nav_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        index = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            self._stack.setCurrentIndex(index)

    def _on_nav_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.childCount():
            # 点组名 = 折叠/展开，和点小箭头等效
            item.setExpanded(not item.isExpanded())

    def _on_game_changed(self, game: Game | None) -> None:
        self._current_game = game
        if game is None:
            self._status_game.setText("当前游戏：（无）—— 请先添加档案")
        else:
            self._status_game.setText(f"当前游戏：{game.name}（{game.app_id}）")
        for page in self._pages:
            if hasattr(page, "set_game"):
                page.set_game(game)

    def _on_imported(self, count: int) -> None:
        self._nav.setCurrentItem(self._nav_items[0])  # 跳到 mod 库页
        self._pages[0].set_game(self._switcher.current_game())  # 触发重载
        self.statusBar().showMessage(f"已导入 {count} 个 mod", 5000)

    def _on_checks_finished(self, updates: int) -> None:
        # 检测/合集登记改了库内数据，mod 库页必须重载才看得到新标题和红块
        self._pages[0].set_game(self._switcher.current_game())
        if updates:
            self.statusBar().showMessage(
                f"更新检测完成：发现 {updates} 个 mod 有新版本", 5000)
        else:
            self.statusBar().showMessage("更新检测完成", 5000)

    def _on_command_gen_requested(self, mod_ids: list) -> None:
        # mod 库页右键"获取下载命令"（或核验页双击缺失行）跳过来：
        # 先切导航，再让命令页按当前档案强制重读一遍（防止清单还是旧数据），
        # 最后只勾选用户指定的那些 mod
        self._nav.setCurrentItem(self._nav_items[5])
        page = self._pages[5]
        page.set_game(self._switcher.current_game())
        if mod_ids:
            page.focus_ids(mod_ids)

    def _pop_console(self) -> None:
        """把已关闭的控制台停靠窗拉出来并置前（新消息自动弹出）。"""
        if self.isMinimized():
            self.showNormal()  # 程序最小化时先还原，否则弹了也看不见
        self._console_dock.show()
        self._console_dock.raise_()


    def _start_batch(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【下载选中项】→ 开批量下载批次。

        无论成败先弹出控制台并切到「下载批次」标签：失败原因写在
        运行日志里，不能让用户对着没反应的按钮猜。
        """
        self._pop_console()
        self._console_dock.widget().show_batch_tab()
        self._batch_controller.start_batch(app_id, mod_ids)

    def _backup_checked(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【备份选中项】→ 备份页切档案并开批次。"""
        self._nav.setCurrentItem(self._nav_items[2])  # 跳到备份页看进度条
        self._pages[2].backup_ids_for(app_id, mod_ids)

    def closeEvent(self, event) -> None:
        # 页面里若有后台线程还在跑（如更新检测），先请它们停下并等
        # 彻底退出，再关数据库——否则退出销毁线程对象时可能闪退
        if (getattr(self, "_batch_controller", None) is not None
                and self._batch_controller.is_active()):
            ret = QMessageBox.question(
                self, "批量下载进行中",
                "批量下载尚未完成。退出会中止剩余条目，"
                "正在下载的那条也会随 steamcmd 退出而中断。\n\n仍要退出吗？")
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        for page in self._pages:
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        self._repo.close()
        super().closeEvent(event)
