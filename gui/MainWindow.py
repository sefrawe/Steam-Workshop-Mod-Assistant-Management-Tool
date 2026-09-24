"""主窗口骨架
"""
"""
结构：左导航（游戏切换器 + 导航列表）+ 中央页面栈 + 底部控制台 Dock。
控制台含"运行日志 / steamcmd 终端 / 下载批次"三个标签页，各页面通过
LogBus 打日志。

日常更新一条龙（决策 26）的接线也在这里：
更新检测页发现新版本 → 按设置弹窗询问（或勾选了自动就直接开批）
→ 复用批量下载全链 → 批次结束自动复扫入账。两个开关存
GlobalSettings.json（界面就地开关，不进设置页 _FIELDS）：
- auto_download_after_check（默认 0 = 弹窗确认；勾选框在更新检测页）
- auto_rescan_after_batch（默认 1 = 批次结束后自动复扫）
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QMainWindow, QStackedWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QMessageBox,
)

from core.appSettings import AppSettings
from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.backupPage import BackupPage
from gui.commandGenPage import CommandGenPage
from gui.exceptionPage import ExceptionPage

from gui.consolePanel import ConsolePanel, LogBus
from gui.batchDownloadController import BatchDownloadController
from gui.gameSwitcher import GameSwitcher
from gui.importPage import ImportPage
from gui.modListPage import ModListPage
from gui.settingsPage import SettingsPage
from gui.updateCheckPage import UpdateCheckPage
from gui.verifyPage import VerifyPage

import sys
import threading


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "mods.db"

_NAV_WIDTH = 210

# 日常更新一条龙的两个开关键名（值一律 "1"/"0"，经 get_int 读）。
# 与 console_auto_show 同款口径：界面就地开关，只进 appSettings.DEFAULTS，
# 不进设置页 _FIELDS（决策 12 的键集口径是单向的：_FIELDS ⊆ DEFAULTS）
_KEY_AUTO_DOWNLOAD = "auto_download_after_check"   # 检测到更新后跳过询问直接下载
_KEY_AUTO_RESCAN = "auto_rescan_after_batch"       # 批次结束后自动扫描本地入账

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
        ("异常处理", 7),

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
            DEFAULT_DB_PATH, snapshot_keep=self._settings.get_int("snapshot_keep", 5))

        self._current_game: Game | None = None

        self._build_central()
        self._build_console_dock()
        self._build_menus()
        self._build_status_bar()
        # 构造期 switcher 已发射过信号（当时无人监听），补一次初始化状态栏
        self._on_game_changed(self._switcher.current_game())

        # —— 日常更新一条龙（决策 26）：检测 → 确认/自动 → 下载 → 复扫 ——
        # 更新检测页发来"确有新版本"的清单（app_id = 开始检测那一刻的档案，
        # 检测进行中切过档案也不会记错家）
        self._pages[4].updates_found.connect(self._on_updates_found)
        # 批次收尾广播 → 自动复扫入账（M3 验收线"复扫确认自动化"，决策 23⑤）
        self._batch_controller.batch_done.connect(self._on_batch_done)
        # 正在跑（或最近一跑）的批次属于哪个档案：_start_batch 记、
        # _on_batch_done 读后清。批次全软件单实例，不会串
        self._batch_app_id: int | None = None
        self._install_excepthook()

    # ---------- UI 构建 ----------

    def _build_central(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        side = QWidget(central)
        side.setFixedWidth(_NAV_WIDTH)
        self._side = side  # 视图菜单显隐用（T19⑭）：局部变量跨方法必须挂 self

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
            ExceptionPage(self._repo, self._settings, self._stack, log=self._log),  # 7

        ]
        for page in self._pages:
            self._stack.addWidget(page)

        self._pages[1].imported.connect(self._on_imported)
        self._pages[4].checks_finished.connect(self._on_checks_finished)
        # mod 库页与账实核验页共用同一份跳转契约：
        # 发出 mod id 列表 → 切到命令生成页并只勾选这些 mod
        self._pages[0].command_gen_requested.connect(self._on_command_gen_requested)
        self._pages[6].command_gen_requested.connect(self._on_command_gen_requested)
        # 前缀照抄上一行
        self._pages[0].download_requested.connect(self._start_batch)
        # 前缀照抄上一行
        self._pages[0].backup_requested.connect(self._backup_checked)
        self._pages[7].command_gen_requested.connect(self._on_command_gen_requested)  # 前缀照抄上一行


        self._nav.setCurrentItem(self._nav_items[0])
        root.addWidget(side)
        root.addWidget(self._stack, 1)
        self.setCentralWidget(central)

    def _build_console_dock(self) -> None:
        self._console_dock = QDockWidget("控制台", self)
        console_panel = ConsolePanel(self._log, self._console_dock, settings=self._settings)
        self._console_dock.setWidget(console_panel)
        # 新消息自动弹出：面板发现控制台被关着又有新日志时发信号，
        # 这里负责把停靠窗拉回屏幕（开关在运行日志页的勾选框）
        console_panel.show_requested.connect(self._pop_console)
        # 批量下载控制器：整软件一个实例（steamcmd 单实例 → 单批次），
        # 把终端信号、批次卡片和流程状态机缝在一起
        self._batch_controller = BatchDownloadController(
            console_panel.terminal, console_panel.step_list,
            self._log, self._settings, self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._console_dock)

    def _build_menus(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")

        act_quit = QAction("退出", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_quit)

        # 游戏(&G)：原侧栏四颗按钮的操作收进这里（按钮撤出后侧栏
        # 只剩"切换游戏"下拉）。三个档案相关项无档案时置灰，
        # 置灰统一走 _on_game_changed
        m_game = self.menuBar().addMenu("游戏(&G)")
        act_add_game = QAction("添加游戏档案（临时）…", self)
        act_add_game.setToolTip("正式建档向导做好前的临时建档入口")
        act_add_game.triggered.connect(self._switcher.add_game_dialog)
        m_game.addAction(act_add_game)
        m_game.addSeparator()
        self._act_edit = QAction("编辑档案…", self)
        self._act_edit.setToolTip(
            "修改当前档案的名称、游戏 mod 目录、备份目录；"
            "下载目录不开放手填，只提供一键改回推导值")
        self._act_edit.triggered.connect(self._switcher.open_edit)
        m_game.addAction(self._act_edit)
        self._act_link = QAction("连接指引…", self)
        self._act_link.setToolTip(
            "把游戏自己的 mod 目录联接到下载目录，让游戏读到 steamcmd "
            "下载的 mod。生成命令与步骤，命令由你自己在 cmd 里执行")
        self._act_link.triggered.connect(self._switcher.open_link_guide)
        m_game.addAction(self._act_link)
        self._act_relocate = QAction("重定位备份目录…", self)
        self._act_relocate.setToolTip(
            "备份记录在「盘上」列失联时用：指认备份文件夹现在的位置，"
            "先预演能对回多少条，确认后只改档案字段，不动文件")
        self._act_relocate.triggered.connect(self._switcher.open_relocate)
        m_game.addAction(self._act_relocate)

        # 视图(&V)（T19⑭⑮）：两块面板的显隐开关，checkable 状态即现状。
        # 与控制台显隐同口径——不记忆跨重启（要记的话往 appSettings
        # DEFAULTS 加键，随时可补）
        m_view = self.menuBar().addMenu("视图(&V)")
        self._act_side = QAction("显示 / 隐藏左侧导航栏", self)
        self._act_side.setCheckable(True)
        self._act_side.setChecked(True)
        self._act_side.toggled.connect(self._toggle_side)
        m_view.addAction(self._act_side)
        self._act_detail = QAction("显示 / 隐藏详情面板（mod 库页）", self)
        self._act_detail.setCheckable(True)
        self._act_detail.setChecked(True)
        self._act_detail.toggled.connect(self._toggle_detail)
        m_view.addAction(self._act_detail)

        # 控制台显隐单独成项（T21②）：原先塞在"视图"菜单里，而视图菜单
        # 只有这一项——腾空后整个删掉（拍板：留一个空菜单是坏体验）。
        # 这里给两件事：开关（显隐切换）+ 弹出（开着但被挡住时置前）
        m_console = self.menuBar().addMenu("控制台(&C)")
        act_toggle = self._console_dock.toggleViewAction()
        act_toggle.setText("显示 / 隐藏控制台")  # 默认文案是停靠窗标题"控制台"
        m_console.addAction(act_toggle)
        act_pop = QAction("弹出控制台并置前", self)
        act_pop.setToolTip("控制台开着但不在前台时，把它拉到最前")
        act_pop.triggered.connect(self._pop_console)
        m_console.addAction(act_pop)

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
        # 无档案时三个档案入口没有操作对象；添加档案不在其列——空库也能加
        has_game = game is not None
        self._act_edit.setEnabled(has_game)
        self._act_link.setEnabled(has_game)
        self._act_relocate.setEnabled(has_game)


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

    def _on_updates_found(self, app_id: int, mod_ids: list) -> None:
        """更新检测发现新版本 → 按设置弹窗确认，或勾选了自动就直接开批。

        app_id 是"开始检测那一刻"的档案——检测进行中用户切过档案的话，
        下载仍归旧档案，与更新检测页的写库归属同一口径（决策 26②）。
        注意：一条龙不会替用户启动 steamcmd（登录这关必须人来）；
        没启动时 _start_batch 会弹控制台并在日志里指路。
        """
        if not mod_ids:
            return
        if self._batch_controller.is_active():
            self._log.info(
                f"检测到 {len(mod_ids)} 个 mod 有新版本，但已有批次在进行："
                "本次不自动开批（需要时到 mod 库页勾选后手动下载）")
            return
        auto = self._settings.get_int(_KEY_AUTO_DOWNLOAD, 0) != 0
        if not auto:
            ret = QMessageBox.question(
                self, "发现更新",
                f"检测到 {len(mod_ids)} 个 mod 有新版本。\n\n"
                "立即开始批量下载吗？\n"
                "（需要 steamcmd 已在终端里启动并登录；勾选更新检测页的"
                "「发现更新后自动开始下载」可跳过本询问）")
            if ret != QMessageBox.StandardButton.Yes:
                self._log.info("已跳过自动下载：需要时到 mod 库页或命令生成页手动下载")
                return
        self._start_batch(app_id, mod_ids)

    def _on_batch_done(self, summary: dict) -> None:
        """批次结束 → 按设置自动复扫入账。

        复扫对准"这一批所属的档案"（_start_batch 记下的 app_id），
        与批次进行中用户是否切过档案无关；quiet 版全程只写日志，
        不会在无人值守时弹窗卡住流程。
        温和停止 / 出错收尾的批次同样复扫：已下载的那几条一样要入账，
        复扫本身只读 acf、幂等无害（R7 + 决策 23⑤）。
        """
        if self._settings.get_int(_KEY_AUTO_RESCAN, 1) == 0:
            return
        game = (self._repo.get_game(self._batch_app_id)
                if self._batch_app_id is not None else None)
        self._batch_app_id = None
        if game is None:
            return
        if self._current_game is None or \
                game.app_id != self._current_game.app_id:
            self._log.info(
                f"批次属于档案「{game.name}」，后台为其复扫入账"
                "（当前界面显示的是别的档案，不受影响）")
        self._pages[0].scan_local_quiet(game)

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

    def _toggle_side(self, visible: bool) -> None:
        """显示/隐藏左侧导航栏（T19⑭）：整个侧栏（切换下拉 + 导航树）
        一起收起，中央页面拿到全部宽度；回程走本菜单项。"""
        self._side.setVisible(visible)

    def _toggle_detail(self, visible: bool) -> None:
        """显示/隐藏 mod 库页详情面板（T19⑮）：主窗口只转发开关，
        布局归页面自己管（QSplitter 不给隐藏的子件分空间，
        表格自动占满整行）。"""
        self._pages[0].set_detail_visible(visible)


    def _start_batch(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【下载选中项】→ 开批量下载批次。
        无论成败先弹出控制台并切到「下载批次」标签：失败原因写在
        运行日志里，不能让用户对着没反应的按钮猜。
        """
        # 批次结束后自动复扫要对上档案（决策 26）；start_batch 拒绝时
        # 不会有 batch_done，残留值无害（下次 _start_batch 会覆盖）
        self._batch_app_id = app_id
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
        # 控制台停靠窗不在 _pages 里，下面的页面循环管不到它——
        # steamcmd 还在跑时在这里收尾（quit → 3 秒 → 强杀 → 等读线程），
        # "优雅退出保住登录缓存"靠的就是这一步。
        # 没启动过 steamcmd 时 shutdown() 直接跳过，安全（已核实幂等）
        self._console_dock.widget().terminal.shutdown()
        for page in self._pages:
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        self._repo.close()
        super().closeEvent(event)

    # ---------- 全局异常兜底 ----------

    def _install_excepthook(self) -> None:
        """让"漏出来的意外错误"被用户看见。
        错误处理原则不变：照旧完整暴露、绝不吞——终端 traceback 照印
        （sys.__excepthook__ 就是原来那个打印器，原样转发给它）；
        改变的只有"报到地点"：运行日志同步多一条红字，主线程的意外
        再弹一个框。像 steamcmd_exe 那样的 NameError，有这层就
        不会只躺在终端里了。
        后台线程的钩子只发日志、不弹框——工作线程严禁碰控件（R11），
        而 LogBus 是信号总线，跨线程安全（consolePanel 文件头保证）。
        """
        def main_hook(exc_type, exc_value, exc_tb):
            self._log.error(
                f"未处理的异常：{exc_type.__name__}: {exc_value}"
                "（完整 traceback 见终端）")
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            QMessageBox.critical(
                self, "程序内部错误",
                "发生了一个程序内部错误，详情已写入运行日志。\n\n"
                f"{exc_type.__name__}: {exc_value}")
        sys.excepthook = main_hook

        def thread_hook(args) -> None:
            self._log.error(
                f"后台线程异常：{args.exc_type.__name__}: {args.exc_value}"
                "（完整 traceback 见终端）")
            threading.__excepthook__(args)
        threading.excepthook = thread_hook

