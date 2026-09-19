"""主窗口骨架
"""

"""
结构：左导航（游戏切换器 + 导航列表）+ 中央 QStackedWidget + 底部终端 Dock。
G1 阶段中央页面全部为占位；G2 起逐个替换 _pages 里的条目。
终端 Dock 的 QProcess 实现属于 M3，此处只放占位标签。
"""
from pathlib import Path

from gui.modListPage import ModListPage
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QListWidget, QMainWindow,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.gameSwitcher import GameSwitcher
from gui.placeholderPage import PlaceholderPage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "mods.db"

_NAV_WIDTH = 210


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Steam创意工坊Mod辅助管理工具")

        self.resize(1200, 800)

        # G1 阶段所有 DB 操作都在主线程（237 行毫秒级）；任务 4 起长操作才上 worker
        self._repo = SQLiteRepository(DEFAULT_DB_PATH)
        self._current_game: Game | None = None

        self._build_central()
        self._build_terminal_dock()
        self._build_menus()
        self._build_status_bar()

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

        self._switcher = GameSwitcher(self._repo, side)
        self._switcher.current_game_changed.connect(self._on_game_changed)
        side_layout.addWidget(self._switcher)

        self._nav = QListWidget(side)
        for name in ("mod 库", "基础功能", "备份管理", "设置"):
            self._nav.addItem(name)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        side_layout.addWidget(self._nav, 1)
        root.addWidget(side)

        self._stack = QStackedWidget(central)
        # G2 起逐个替换：第 0 页最先换成 ModListPage
        self._pages = [
            ModListPage(self._repo, self._stack),
            PlaceholderPage("基础功能", "网址批量导入、更新检测、下载命令生成\n将随对应功能模块完成逐个开放",
                            self._stack),
            PlaceholderPage("备份管理", "mod 备份与恢复功能开发中", self._stack),
            PlaceholderPage("设置", "全局设置（目录路径、steamcmd 位置等）开发中", self._stack),
        ]

        for page in self._pages:
            self._stack.addWidget(page)
        self._nav.setCurrentRow(0)
        root.addWidget(self._stack, 1)

        self.setCentralWidget(central)

    def _build_terminal_dock(self) -> None:
        self._terminal_dock = QDockWidget("steamcmd 终端（开发中）", self)
        placeholder = QLabel(
            "交互式终端将在后续版本接入。\n可从【视图】菜单关闭 / 恢复本面板。",
            self._terminal_dock,
        )

        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setWordWrap(True)
        self._terminal_dock.setWidget(placeholder)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._terminal_dock)

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
        m_view.addAction(self._terminal_dock.toggleViewAction())

    def _build_status_bar(self) -> None:
        self._status_game = QLabel(self)
        self._status_db = QLabel(f"数据库：{DEFAULT_DB_PATH}", self)
        self.statusBar().addWidget(self._status_game)
        self.statusBar().addPermanentWidget(self._status_db)
        # 构造期 switcher 已发射过信号（当时无人监听），补一次初始化状态栏
        self._on_game_changed(self._switcher.current_game())

    # ---------- 槽 ----------

    def _on_nav_changed(self, row: int) -> None:
        self._stack.setCurrentIndex(row)

    def _on_game_changed(self, game: Game | None) -> None:
        self._current_game = game
        if game is None:
            self._status_game.setText("当前游戏：（无）—— 请先添加档案")
        else:
            self._status_game.setText(f"当前游戏：{game.name}（{game.app_id}）")
        first = self._pages[0]
        if hasattr(first, "set_game"):
            first.set_game(game)

    def closeEvent(self, event) -> None:
        self._repo.close()
        super().closeEvent(event)
