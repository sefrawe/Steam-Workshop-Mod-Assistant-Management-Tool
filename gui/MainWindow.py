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

功能模块「首次使用」（决策 54）的接线也在这里，三条信号全走转调、
零新实体（与模块页同一哲学：模块的每一步都是既有功能的入口）：
- 第①步 settings_requested → 跳「设置」页（_goto_page）；
- 第②步 add_game_requested → 档案切换器的建档对话框（与「游戏」
  菜单里是同一份）；
- 第③步 link_guide_requested → 连接指引（同上）。
第④步"纳入已有 mod"的逻辑住在 workflows/intakeFlow（零 Qt，
pytest 已覆盖），页面只管预览、确认、落库与复制命令。
"""
import sqlite3
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QSettings, QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import appPaths
from core import dataExporter
from core.appSettings import AppSettings
from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.addModPage import AddModPage
from gui.backupOverviewPage import BackupOverviewPage
from gui.backupPage import BackupPage
from gui.batchDownloadController import BatchDownloadController
from gui.browserPickDialog import BrowserPickDialog
from gui.browserTabPage import BrowserTabPage
from gui.commandGenPage import CommandGenPage
from gui.consolePanel import ConsolePanel, LogBus
from gui.dailyUpdatePage import DailyUpdatePage
from gui.deletePage import DeletePage
from gui.exceptionPage import ExceptionPage
from gui.firstUsePage import FirstUsePage
from gui.gameSwitcher import GameSwitcher
from gui.importPage import ImportPage
from gui.migrationPage import MigrationPage
from gui.modListPage import ModListPage
from gui.rescuePage import RescuePage
from gui.settingsPage import SettingsPage
from gui.shareListPage import ShareListPage
from gui.statsPage import StatsPage
from gui.uninstallPage import UninstallPage
from gui.updateCheckPage import UpdateCheckPage
from gui.updateComparePage import UpdateComparePage

from gui.updateSelectDialog import UpdateSelectDialog
from gui.verifyPage import VerifyPage
from gui.welcomePage import ICON_REL, PROJECT_URL, WelcomePage


DEFAULT_DB_PATH = appPaths.db_path()  # T17：数据根统一从 appPaths 定位（源码=项目根\data，打包=exe 旁\data）

_NAV_WIDTH = 210
_IDX_BACKUP_OVERVIEW = 13  # 备份总览页栈下标 = _pages 末尾 append 后落位（决策 13 模式）
_IDX_FIRST_USE = 14        # 首次使用模块页 = _pages 末尾 append 后落位（同决策 13 模式）
_IDX_RESCUE = 15  # 功能模块：恢复旧版本 = _pages 末尾 append 后落位（决策 13 模式）
_IDX_SHARE = 16   # 功能模块：分享清单 = 同上
_IDX_MIGRATION = 17  # 功能模块：换机迁移 = _pages 末尾 append 后落位（决策 13 模式）
_IDX_DELETE = 18   # 功能模块：清理与删除 = _pages 末尾 append 后落位（决策 13 模式）
_IDX_UNINSTALL = 19 # 功能模块：卸载与清理 = _pages 末尾 append 后落位（决策 13 模式）
_IDX_UPDATE_COMPARE = 20  # 基础功能：更新对照 = _pages 末尾 append 后落位



# 日常更新一条龙的两个开关键名（值一律 "1"/"0"，经 get_int 读）。
# 与 console_auto_show 同款口径：界面就地开关，只进 appSettings.DEFAULTS，
# 不进设置页 _FIELDS（决策 12 的键集口径是单向的：_FIELDS ⊆ DEFAULTS）
_KEY_AUTO_DOWNLOAD = "auto_download_after_check"  # 检测到更新后跳过询问直接下载
_KEY_AUTO_RESCAN = "auto_rescan_after_batch"      # 批次结束后自动扫描本地入账

# 面板显隐的会话记忆（T19⑤ 轻量子集）：QSettings 而非 AppSettings——
# 会话状态≠用户配置；键值一律存 "1"/"0" 字符串，读取行为跨平台确定
_SES_SIDE = "session/side_visible"
_SES_DETAIL = "session/detail_visible"
_SES_CONSOLE = "session/console_visible"
_SES_GEOM = "session/window_geometry"  # 窗口大小与位置（QByteArray）
_SES_STATE = "session/window_state"    # 停靠窗布局（控制台位置/高度等）
_SES_NAV = "session/nav_expanded"  # 导航树组折叠状态（展开的组名逗号串，T19⑤ 收官项）


# 左导航树：整数 = 页面栈下标（真实页面）；None = 未完成模块，灰色"开发中"。
# 完成模块时：在 _pages 末尾 append 新页面（栈顺序不再需要和导航一致），
# 然后把对应条目的 None 改成新下标、去掉后缀、setDisabled(False) 即点亮。
_NAV_SCHEMA: list[tuple[str, int | list[tuple[str, int | None]]]] = [
    ("欢迎", 9),
    ("功能模块", [
        ("加入新 mod", 10),
        ("首次使用", _IDX_FIRST_USE),  # 决策 54：四步向导已上线。
        # 建档/连接转调 gameSwitcher 既有对话框——原"临时入口届时退役"
        # 改判不退役：模块走转调、入口留给熟手直达（两条路一本账）
        ("日常更新", 12),  # T22：检测 → 确认清单 → 批量下载 → 复扫 的引导壳
        ("清理与删除", _IDX_DELETE),  # 决策 69：盘点→处置（软删除残留/彻底清账/孤儿目录）；恢复走 mod 库页右键

        ("恢复旧版本", _IDX_RESCUE),  # 决策 34 第二档：备份恢复的引导壳（零新引擎）
        ("换机迁移", _IDX_MIGRATION),  # 决策 34 第二档：账本+目录推导+联接+重定位 的编排壳

        ("分享清单", _IDX_SHARE),  # 决策 34 第二档：文件菜单两项的第二入口 + 收尾引导
        ("卸载与清理", _IDX_UNINSTALL),  # 决策 70：便携形态的收尾——记忆迁移/注册表指引/盘点

    ]),
    ("mod 库", 0),
    ("统计", 8),
    ("基础功能", [
        ("网址批量导入", 1),
        ("从浏览器取网址", 11),
        ("更新对照", _IDX_UPDATE_COMPARE),
        ("下载命令生成", 5),
        ("账实核验", 6),
        ("异常处理", 7),
    ]),
    ("备份管理", [
        ("备份与恢复", 2),
        ("备份总览", _IDX_BACKUP_OVERVIEW),
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

        # 更新确认清单（非模态）：MainWindow 持引用防 GC——局部变量 +
        # show() 出作用域会把 Python 包装回收而 C++ 对象悬空，经典闪退
        # （决策 36⑦）。清单关闭时由 finished 回调清引用
        self._update_dialog: UpdateSelectDialog | None = None

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
        self._restore_panels()


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

        self._switcher = GameSwitcher(self._repo, side, settings=self._settings,
                                      log=self._log)
        self._switcher.current_game_changed.connect(self._on_game_changed)
        self._switcher.overview_requested.connect(self._on_overview_requested)
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
        # 组折叠记忆（T19⑤）：点组名/箭头展开收起即落盘；启动恢复在
        # _restore_panels → _restore_nav_state。建树时的 setExpanded(True)
        # 发生在接线之前不会触发保存——无记录时维持"默认全展开"。
        self._nav.itemExpanded.connect(self._save_nav_state)
        self._nav.itemCollapsed.connect(self._save_nav_state)

        side_layout.addWidget(self._nav, 1)

        self._stack = QStackedWidget(central)
        self._pages = [
            ModListPage(self._repo, self._settings, self._stack, log=self._log),  # 0
            ImportPage(self._repo, self._stack, log=self._log),  # 1
            BackupPage(self._repo, self._settings, self._stack, log=self._log),  # 2
            SettingsPage(self._settings, self._stack),  # 3
            UpdateCheckPage(self._repo, self._settings, self._stack, log=self._log),  # 4
            CommandGenPage(self._repo, self._settings, self._stack, log=self._log),  # 5
            VerifyPage(self._repo, self._settings, self._stack, log=self._log),  # 6
            ExceptionPage(self._repo, self._settings, self._stack, log=self._log),  # 7
            StatsPage(self._repo, self._stack),  # 8
            WelcomePage(self._stack),  # 9 欢迎页：纯静态，无 repo 依赖
            AddModPage(self._repo, self._settings, self._stack, log=self._log),  # 10 功能模块：加入新 mod
            BrowserTabPage(self._stack, log=self._log),  # 11 基础功能：从浏览器取网址
            DailyUpdatePage(self._repo, self._settings, self._stack, log=self._log),  # 12 功能模块：日常更新
            BackupOverviewPage(self._repo, self._settings, self._stack, log=self._log),  # 13 备份总览（跨档案大盘）
            FirstUsePage(self._repo, self._settings, self._stack, log=self._log),  # 14 功能模块：首次使用（决策 54）
            RescuePage(self._repo, self._settings, self._stack, log=self._log),  # 15 功能模块：恢复旧版本
            ShareListPage(self._repo, self._settings, self._stack, log=self._log),  # 16 功能模块：分享清单
            MigrationPage(self._repo, self._settings, self._stack, log=self._log),  # 17 功能模块：换机迁移
            DeletePage(self._repo, self._settings, self._stack, log=self._log),  # 18 功能模块：清理与删除（决策 69）
            UninstallPage(self._repo, self._settings, self._stack, log=self._log),  # 19 功能模块：卸载与清理（决策 70）
            UpdateComparePage(self._repo, self._stack),  # 20 基础功能：更新对照（只读）


        ]
        for page in self._pages:
            self._stack.addWidget(page)

        self._pages[1].imported.connect(self._on_imported)
        self._pages[4].checks_finished.connect(self._on_checks_finished)

        # mod 库页与账实核验页共用同一份跳转契约：
        # 发出 mod id 列表 → 切到命令生成页并只勾选这些 mod
        self._pages[0].command_gen_requested.connect(self._on_command_gen_requested)
        self._pages[6].command_gen_requested.connect(self._on_command_gen_requested)  # 前缀照抄上一行
        self._pages[0].download_requested.connect(self._start_batch)   # 前缀照抄上一行
        self._pages[0].backup_requested.connect(self._backup_checked)

        self._pages[7].command_gen_requested.connect(self._on_command_gen_requested)  # 前缀照抄上一行

        # 功能模块「加入新 mod」第④步【扫描确认】：转调 mod 库页既有
        # 扫描链（quiet 版，只写日志不弹窗）；扫描同步完成后回叫模块
        # 盘点批次结果。归属口径与决策 26 复扫一致：扫批次所属档案，
        # 不是界面当前档案
        self._pages[10].scan_requested.connect(self._on_addmod_scan_requested)
        self._pages[10].download_requested.connect(self._start_batch)  # 前缀照抄 mod 库页 download_requested 那行
        self._pages[11].handoff_to_addmod.connect(self._on_handoff_to_addmod)

        # 模块②"从浏览器取标签页…"：弹出页内选择器（T23，不跳页）。
        # 对话框送来的清单由 _on_handoff_to_addmod 统一处理——只填入
        # 与解析，不代入库（防呆不省）
        self._pages[10].open_browser_picker.connect(self._open_browser_picker)

        # 功能模块「日常更新」第②步【开始检测】：后台开测不跳页；
        # 进度与中断由检测页信号直连模块页（决策 42 第二块表盘，
        # 无副作用所以直连，不经主窗口方法转手）
        self._pages[12].check_requested.connect(self._on_daily_check_requested)
        self._pages[4].progress_changed.connect(self._pages[12].on_check_progress)
        self._pages[4].check_interrupted.connect(self._pages[12].on_check_interrupted)

        # —— 功能模块「首次使用」（决策 54）：三张卡全是既有实体的入口，
        # 本窗口只做转调，零新实体 ——
        self._pages[14].settings_requested.connect(
            lambda: self._goto_page(3))                    # 第①步 → 「设置」页
        self._pages[14].add_game_requested.connect(
            self._switcher.add_game_dialog)                # 第②步 → 建档对话框（与「游戏」菜单同一份）
        self._pages[14].link_guide_requested.connect(
            self._switcher.open_link_guide)                # 第③步 → 连接指引（同上）
        # 第④步不需要接线：预览/纳入/复制命令都在页面内闭环；
        # set_game 广播由 _on_game_changed 的 hasattr 循环自动覆盖；
        # 进页刷新在 _on_nav_changed 里单独处理（见下）
        # 高级筛选对话框的「到 mod 库查看结果」：跨页跳转走 _goto_page
        # （树高亮同步包含在内，别裸调 stack）
        self._pages[0].advanced_results_requested.connect(
            lambda: self._goto_page(0))
        # —— 功能模块「恢复旧版本」（决策 34 第二档）：引导壳，零新引擎 ——
        # ②去备份页：备份页是唯一恢复入口（R8 前置备份在那里内置）
        self._pages[_IDX_RESCUE].go_backup_requested.connect(
            lambda: self._goto_page(2))
        # ③扫描确认：转调 mod 库页 quiet 扫描链（与"加入新 mod"第④步
        # 同一条链）；归属=当前档案（恢复动作发生在此档案名下）
        self._pages[_IDX_RESCUE].rescan_requested.connect(
            self._on_rescue_rescan)
        # —— 功能模块「分享清单」：文件菜单同款动作的第二入口 ——
        # 直接转调既有方法：确认弹窗、防呆、日志、状态栏回执全在原处
        self._pages[_IDX_SHARE].share_out_requested.connect(
            self._export_sharepack)
        self._pages[_IDX_SHARE].share_in_requested.connect(
            self._import_sharepack)
        self._pages[_IDX_SHARE].open_update_requested.connect(
            lambda: self._goto_page(4))
        # —— 功能模块「换机迁移」（决策 34 第二档）：七个转调，零新引擎 ——
        # ①②账本进出（破坏性确认、批次拒绝都在 _import_ledger 原处）
        self._pages[_IDX_MIGRATION].export_requested.connect(
            self._export_ledger)
        self._pages[_IDX_MIGRATION].import_requested.connect(
            self._import_ledger)
        # ③设置页 ④编辑档案 ⑥连接指引/重定位（后两个与「游戏」菜单同源）
        self._pages[_IDX_MIGRATION].open_settings_requested.connect(
            lambda: self._goto_page(3))
        self._pages[_IDX_MIGRATION].open_edit_requested.connect(
            self._switcher.open_edit)
        self._pages[_IDX_MIGRATION].open_link_requested.connect(
            self._switcher.open_link_guide)
        self._pages[_IDX_MIGRATION].open_relocate_requested.connect(
            self._switcher.open_relocate)
        # ⑦扫描确认：转调 mod 库页 quiet 扫描链（与"恢复旧版本"③同一条链）
        self._pages[_IDX_MIGRATION].rescan_requested.connect(
            self._on_migration_rescan)
        # —— 功能模块「清理与删除」（决策 69）：孤儿认领的两条跳转 ——
        self._pages[_IDX_DELETE].goto_ledger_requested.connect(
            lambda: self._goto_page(0))   # mod 库页：扫描本地自动认领 / 右键手动确认
        self._pages[_IDX_DELETE].goto_import_requested.connect(
            lambda: self._goto_page(1))   # 网址批量导入：登记网址认领
        self._pages[_IDX_DELETE].ledger_changed.connect(
            self._on_cleanup_ledger_changed)




        self._nav.setCurrentItem(self._nav_items[9])  # 启动默认落「欢迎」页

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
        # 具体引用：别再 widget() 取回 QWidget 硬调子类方法——类型链
        # 在那里断掉，IDE 黄线和将来的真 bug 都会藏在那后面
        self._console = console_panel

        # 批量下载控制器：整软件一个实例（steamcmd 单实例 → 单批次），
        # 把终端信号、批次卡片和流程状态机缝在一起。
        # repo 供"批次前备份阶段"读写备份账（决策 40）
        self._batch_controller = BatchDownloadController(
            console_panel.terminal, console_panel.step_list,
            self._log, self._settings, self._repo, self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                           self._console_dock)

    def _build_menus(self) -> None:
        m_file = self.menuBar().addMenu("文件(&F)")

        # —— 账本导出/导入（T19 dataExporter，决策 33）——
        # 导出是纯读操作，不需确认；导入完整账本=清库重灌，破坏性操作
        # 确认弹窗 + 默认按钮"否"双保险，批次进行中直接拒绝；分享包
        # 导入是纯增量（已有 mod 跳过、现有数据一字不改），无需确认
        act_exp_ledger = QAction("导出完整账本…", self)
        act_exp_ledger.setToolTip(
            "把全部档案与全部 mod 记录（含快照、备份登记、操作日志等"
            "7 张表）导出为一个 JSON 文件，用于换机迁移 / 整机备份")
        act_exp_ledger.triggered.connect(self._export_ledger)
        m_file.addAction(act_exp_ledger)

        act_imp_ledger = QAction("导入完整账本…", self)
        act_imp_ledger.setToolTip(
            "从导出的 JSON 文件恢复账本。注意：导入会整体替换当前账本"
            "（清库重灌），操作前会再次确认")
        act_imp_ledger.triggered.connect(self._import_ledger)
        m_file.addAction(act_imp_ledger)

        m_file.addSeparator()

        self._act_share_out = QAction("导出分享包（当前游戏）…", self)
        self._act_share_out.setToolTip(
            "把当前游戏的收录清单（含备注、标签、特别关注等整理成果）"
            "导出为 JSON 发给别人；不含下载状态等本机信息")
        self._act_share_out.triggered.connect(self._export_sharepack)
        m_file.addAction(self._act_share_out)

        act_imp_share = QAction("导入分享包…", self)
        act_imp_share.setToolTip(
            "把别人发来的分享包并入当前账本：已有的 mod 自动跳过，"
            "绝不改动现有数据")
        act_imp_share.triggered.connect(self._import_sharepack)
        m_file.addAction(act_imp_share)
        m_file.addSeparator()
        act_open_dl = QAction("打开 steamcmd 下载目录", self)
        act_open_dl.setToolTip(
            "在文件管理器打开当前档案的 mod 下载目录（steamcmd 工坊"
            "内容目录，里面是按编号命名的 mod 文件夹）。需要先在左上角"
            "选中游戏档案")
        act_open_dl.triggered.connect(self._open_download_dir)
        m_file.addAction(act_open_dl)
        self._act_open_download = act_open_dl  # 无档案时置灰（_on_game_changed）
        act_open_root = QAction("打开软件所在目录", self)
        act_open_root.setToolTip(
            "在文件管理器打开本软件的文件夹——绿色软件，账本（mods.db）"
            "等数据都在其中的 data 子目录")
        act_open_root.triggered.connect(self._open_software_dir)
        m_file.addAction(act_open_root)

        m_file.addSeparator()

        act_quit = QAction("退出", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        m_file.setToolTipsVisible(True)  # QMenu 默认不显示动作悬浮说明（T19⑩ 同款坑）——账本/分享包四项的说明此前一直没展示过

        m_file.addAction(act_quit)

        # 游戏(&G)：原侧栏四颗按钮的操作收进这里（按钮撤出后侧栏
        # 只剩"切换游戏"下拉）。三个档案相关项无档案时置灰，
        # 置灰统一走 _on_game_changed
        m_game = self.menuBar().addMenu("游戏(&G)")
        act_add_game = QAction("添加游戏档案…", self)
        act_add_game.setToolTip("输入 AppID 一屏建档：自动查名、查重、"
                                "下载目录按 steamcmd 位置推导并可预览")
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

        m_game.addSeparator()

        self._act_delete = QAction("删除当前档案…", self)
        self._act_delete.setToolTip(
            "删除当前游戏档案名下的全部账目（mod 记录、版本快照、备份登记、"
            "失效归档）。清账前自动生成数据库快照兜底；磁盘备份文件默认"
            "保留、可勾选一并删除。content 下载目录与游戏目录不受影响")
        self._act_delete.triggered.connect(self._switcher.open_delete)
        m_game.addAction(self._act_delete)
        m_game.setToolTipsVisible(True)  # 同上：编辑/连接/重定位/删除四项的说明一直没展示过


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
        m_console.setToolTipsVisible(True)  # 同上：「弹出控制台并置前」的说明

        # 高级筛选（T12 决策 63 修订）：独立菜单栏顶级项 = 全局搜索按钮。
        # QMenuBar.addAction 不带子菜单——点了就触发，天然是长在菜单栏
        # 上的按钮；软件任意页面一键唤出（非模态，实例归 mod 库页持有防 GC）
        act_adv = self.menuBar().addAction("高级筛选(&S)")
        act_adv.setToolTip(
            "按标题/备注/编号/大小/远端更新时间/标签组合筛选 mod 库；\n"
            "对话框里实时显示命中数，可一键跳到 mod 库看结果；\n"
            "与顶栏筛选叠加生效，条件生效期间库页工具条出现指示按钮")
        act_adv.triggered.connect(self._pages[0].open_advanced_search)
        # 帮助(&H)：关于对话框（T17「关于」拍板项收口）。菜单栏最后一项，
        # Windows 惯例位置；纯展示零写操作，模态安全（看它不需要同时碰
        # 窗口外的任何东西——踩坑㊱ 自查通过）。
        m_help = self.menuBar().addMenu("帮助(&H)")
        act_about = QAction("关于…", self)
        act_about.setToolTip("查看本工具的版本、简介与开源项目地址")
        act_about.triggered.connect(self._about)
        m_help.addAction(act_about)
        m_help.setToolTipsVisible(True)  # QMenu 默认不显示悬浮说明（T19⑩ 同款坑）



    def _about(self) -> None:
        """帮助 → 关于…：版本、简介与开源项目地址。
        两样信息全部单源、零硬编码：版本号读 QApplication（main.py
        setApplicationVersion 设定），项目地址读 gui.welcomePage.
        PROJECT_URL（欢迎页同一份常量）——将来改版本或改地址，
        这里自动跟上，永不出现两处显示打架。"""
        box = QDialog(self)
        box.setWindowTitle("关于")
        v = QVBoxLayout(box)
        v.setContentsMargins(20, 20, 20, 16)
        v.setSpacing(10)

        # 图标 + 名称/版本 横排。图标缺失就跳过（纯装饰不弹窗，
        # 与 main.py / 欢迎页同款兜底口径）。
        head = QHBoxLayout()
        pix = QPixmap(str(appPaths.resource_path(ICON_REL)))
        if not pix.isNull():
            icon = QLabel(box)
            icon.setPixmap(pix.scaled(
                64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        name_ver = QLabel(box)
        ver = QApplication.applicationVersion()
        name_ver.setText(
            "<b>Steam 创意工坊 Mod 辅助管理工具</b>"
            + (f"<br>版本 {ver}" if ver else ""))
        name_ver.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(name_ver, 1)
        v.addLayout(head)

        intro = QLabel(
            "帮助大量使用 mod 的 Steam 玩家，在不打开 Steam 客户端的"
            "情况下完成工坊 mod 的登记、更新检测、下载与备份恢复。"
            "绿色软件：账本、设置等全部数据保存在软件自己的文件夹里。",
            box)
        intro.setWordWrap(True)
        v.addWidget(intro)

        link = QLabel(
            f'开源项目主页：<a href="{PROJECT_URL}">{PROJECT_URL}</a>'
            "<br>（点击打开浏览器，或选中复制）", box)
        link.setWordWrap(True)
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        v.addWidget(link)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_close = QPushButton("关闭", box)
        btn_close.clicked.connect(box.accept)
        btn_row.addWidget(btn_close)
        v.addLayout(btn_row)

        box.exec()


    def _build_status_bar(self) -> None:
        self._status_game = QLabel(self)
        self._status_db = QLabel(f"数据库：{DEFAULT_DB_PATH}")
        self.statusBar().addWidget(self._status_game)
        self.statusBar().addPermanentWidget(self._status_db)

    # ---------- 槽 ----------
    def _on_nav_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        index = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            self._stack.setCurrentIndex(index)
            if index == _IDX_BACKUP_OVERVIEW:
                # 备份总览的数据源是全局账本，且没有 set_game——每次切
                # 进来重读一遍，别让别的页删改后这里显示旧账
                self._pages[index].refresh()
            if index == _IDX_FIRST_USE:
                # 「首次使用」每次进页重读设置刷新四张卡（典型场景：从
                # 设置页填完 steamcmd 回来即最新）。页面内部有守卫——
                # 空目录框才回填设置值，不会抢走用户正在输入的内容
                self._pages[index].refresh()
            if index == _IDX_UNINSTALL:
                # 进页重查会话文件/注册表状态：三张卡的文案随现状变化
                # （典型：在别处用过界面记忆后回来看，状态是新的）
                self._pages[index].refresh()
            if index == _IDX_UPDATE_COMPARE:
                # 检测/扫描在别的页跑完后切过来即最新（只读页，毫秒级）
                self._pages[index].refresh()

    def _on_overview_requested(self, app_id: int) -> None:
        """删除对话框「先去备份总览看看」→ 跳总览页并按该档案过滤。"""
        self._goto_page(_IDX_BACKUP_OVERVIEW)
        self._pages[_IDX_BACKUP_OVERVIEW].set_filter_game(app_id)

    def _on_nav_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.childCount():  # 点组名 = 折叠/展开，和点小箭头等效
            item.setExpanded(not item.isExpanded())

    def _on_game_changed(self, game: Game | None) -> None:
        self._current_game = game
        # 决策 36⑦：确认清单归属"检测那一刻"的档案——切档案即自动
        # 关掉还开着的清单，防止旧档案的条目被执行到新档案头上
        self._close_update_dialog()
        if game is None:
            self._status_game.setText("当前游戏：（无）—— 请先添加档案")
        else:
            self._status_game.setText(f"当前游戏：{game.name}（{game.app_id}）")
        # set_game 广播循环：FirstUsePage 也在 _pages 里，自动覆盖——
        # 页面内部会把第④步的预览作废并写日志（预览归属旧档案）
        for page in self._pages:
            if hasattr(page, "set_game"):
                page.set_game(game)
        # 无档案时档案入口没有操作对象；添加档案不在其列——空库也能加
        has_game = game is not None
        self._act_edit.setEnabled(has_game)
        self._act_link.setEnabled(has_game)
        self._act_relocate.setEnabled(has_game)
        self._act_delete.setEnabled(has_game)
        self._act_share_out.setEnabled(has_game)  # 分享包按当前档案导出
        self._act_open_download.setEnabled(
            has_game and bool(game.download_dir))


    def _on_imported(self, count: int) -> None:
        self._nav.setCurrentItem(self._nav_items[0])  # 跳到 mod 库页
        self._pages[0].set_game(self._switcher.current_game())  # 触发重载
        self.statusBar().showMessage(f"已导入 {count} 个 mod", 5000)

    def _on_checks_finished(self, updates: int) -> None:
        # 检测/合集登记改了库内数据，mod 库页必须重载才看得到新标题和红块
        self._pages[0].set_game(self._switcher.current_game())
        # 「日常更新」模块页第②步卡片同步检测结论（无论检测从哪发起）
        self._pages[12].on_check_done(updates)
        if updates:
            self.statusBar().showMessage(
                f"更新检测完成：发现 {updates} 个 mod 有新版本", 5000)
        else:
            self.statusBar().showMessage("更新检测完成", 5000)

    def _goto_page(self, page_index: int) -> None:
        """程序化跳页 + 导航树高亮同步：setCurrentIndex 不经过点击，
        树高亮不会自己跟上——跨页跳转一律走这里，别裸调 stack。"""
        self._stack.setCurrentIndex(page_index)
        item = self._nav_items[page_index]
        if item is not None:
            self._nav.setCurrentItem(item)

    def _on_handoff_to_addmod(self, lines: list) -> None:
        """「从浏览器取网址」→「加入新 mod」跨页交接：填入第②步
        输入框并自动解析预览，同时跳到模块页。只填与预览、不代入库
        （决策 22⑤：跳转后说明来意；入库防呆不省）。"""
        self._pages[10].receive_external_lines(lines)
        self._goto_page(10)

    def _on_addmod_scan_requested(self, app_id: int) -> None:
        """功能模块「加入新 mod」第④步【扫描确认】：
        转调 mod 库页的既有扫描链（quiet 版，全程只写日志不弹窗），
        扫描同步完成（本地只读一个 acf 文本，毫秒级）后回叫模块盘点。
        扫哪个档案由模块批次归属（入库那一刻的档案）决定，与界面
        当前档案无关——与决策 26 复扫归属同一口径。
        档案可能已被删除（game=None）：scan_local_quiet 自带守卫，
        盘点会把该批如实标成"状态异常"，不吞不瞒。"""
        game = self._repo.get_game(app_id)
        self._pages[0].scan_local_quiet(game)
        self._pages[10].on_scan_confirmed()

    def _on_rescue_rescan(self) -> None:
        """功能模块「恢复旧版本」第③步【扫描确认】：转调 mod 库页的
        quiet 扫描链（与"加入新 mod"第④步同一条链）。恢复动作发生在
        当前界面档案名下，就扫当前档案——无档案时页面按钮本就置灰，
        这里双保险直接返回。"""
        game = self._current_game
        if game is None:
            return
        self._pages[0].scan_local_quiet(game)
        self._pages[_IDX_RESCUE].on_rescan_done(game.name)

    def _on_cleanup_ledger_changed(self) -> None:
        """清理页处置落账后，强制 mod 库页重读（同 _on_checks_finished
        的先例）——软删除/彻底清账的结果不该等用户自己点刷新才看见。"""
        self._pages[0].set_game(self._switcher.current_game())


    def _on_migration_rescan(self) -> None:
        """功能模块「换机迁移」第⑦步【扫描确认】：转调 mod 库页的
        quiet 扫描链，扫当前档案（迁移按档案逐个推进）。无档案时
        页面按钮本就置灰，这里双保险直接返回。与 _on_rescue_rescan
        同链不同回叫对象，各留各的语义。"""
        game = self._current_game
        if game is None:
            return
        self._pages[0].scan_local_quiet(game)
        self._pages[_IDX_MIGRATION].on_rescan_done(game.name)


    def _on_daily_check_requested(self) -> None:
        """「日常更新」模块页第②步【开始检测】：不跳页。检测链仍
        单源住在更新检测页；受理成功后模块页进度条进忙态，进度由
        检测页的 progress_changed 直连转发（决策 42）。被拒绝（没
        档案 / 已在检测 / 没有可检测条目）时原因写运行日志，模块页
        卡片维持原状、按钮不锁——没开起来就不装作在跑。"""
        reason = self._pages[4].start_check()
        if reason:
            self._log.warn(f"检测没有开始：{reason}")
        else:
            self._pages[12].on_check_started()

    def _on_updates_found(self, app_id: int, mod_ids: list) -> None:
        """更新检测发现新版本 → 摆出确认清单（非模态，决策 36⑦）。
        开批全部走清单的 execute_requested 信号 → _on_update_execute_requested，本方法只负责把清单摆出来；
        设置勾了「发现更新后自动开始下载」则不摆清单，全部先备份
        再更新直接开批（决策 26 原判 + 决策 40）。app_id 是"开始
        检测那一刻"的档案（决策 26②）。
        非模态三件套（缺一即闪退/串档）：
        1) show() 不 exec()——清单的执行链依赖窗口外的底部控制台
        （steamcmd 没启动要去启动），模态 = 死锁（踩坑㊱）；
        2) MainWindow 持引用（self._update_dialog）——局部变量 +
        show() 出作用域即回收包装，经典闪退；
        3) 重开检测时旧单先关；切档案自动关单（_on_game_changed）。
        关闭清单不代表取消：已开的批次照跑，没执行的条目之后可在
        mod 库页手动下载。
        """
        if not mod_ids:
            return
        if self._batch_controller.is_active():
            self._log.info(
                f"检测到 {len(mod_ids)} 个 mod 有新版本，但已有批次在进行："
                "本次不弹清单（需要时到 mod 库页勾选后手动下载）")
            return
        auto = self._settings.get_int(_KEY_AUTO_DOWNLOAD, 0) != 0
        self._pages[12].on_updates_found(app_id, len(mod_ids), auto)
        if auto:
            self._start_batch(app_id, list(mod_ids), list(mod_ids))
            return
        mods = [m for m in (self._repo.get_mod(i) for i in mod_ids)
                if m is not None]
        if not mods:
            return
        self._close_update_dialog()  # 重开检测：旧单先关，不留双份
        dlg = UpdateSelectDialog(mods, app_id, self)
        dlg.execute_requested.connect(self._on_update_execute_requested)
        dlg.finished.connect(self._on_update_dialog_finished)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._update_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_update_dialog_finished(self, _result: int) -> None:
        """清单关闭（点【完成】/ 关窗 / 切档案被收）→ 放掉引用。
        WA_DeleteOnClose 会在关窗后析构 C++ 对象，此后不得再触碰
        dlg——本回调只清引用，安全。"""
        self._update_dialog = None

    def _close_update_dialog(self) -> None:
        """关掉还开着的确认清单（重开检测 / 切档案时调用）。
        close() 会触发 finished → 引用被清；这里再兜一次 None。"""
        dlg = self._update_dialog
        if dlg is not None:
            dlg.close()
            self._update_dialog = None

    def _on_update_execute_requested(self, app_id: int, action: str,
                                     mod_ids: list) -> None:
        """确认清单【执行选中】：清单非模态且执行不关窗，本槽可被
        反复调用——每次只处理当前这组勾选。app_id 由对话框随信号
        带回（构造时传入 = 检测那一刻的档案；清单在档案切换时会被
        主窗口自动关闭（决策 36⑦），存活期间归属不变）。
        「备份+更新」里没有旧版本可备份的条目（未下载 / 手动确认
        版本未知）自动降级为「仅更新」并写日志——备份引擎按决策 24
        拒绝无版本的备份，先降级就不用等备份失败再跳过。
        受理回执（决策 36⑧）：_start_batch 受理成功才回叫清单
        mark_executed（清勾选 + 留痕）；没受理则回叫 notify_not_
        started——勾选原样保留，没开起来不装作在跑（决策 42④）。"""
        if not mod_ids:
            return
        dlg = self._update_dialog
        if self._batch_controller.is_active():
            self._log.info("上一批还在进行：等它跑完再执行下一组"
                           "（进度见底部控制台 · 下载批次）")
            if dlg is not None:
                dlg.notify_not_started("上一批还在进行，等它跑完再执行"
                                       "下一组（进度见底部控制台 ·"
                                       "「下载批次」）")
            return
        if action == UpdateSelectDialog.ACT_BACKUP:
            to_backup: list[int] = []
            update_only: list[int] = []
            for i in mod_ids:
                m = self._repo.get_mod(i)
                if m is not None and m.status == "downloaded" \
                        and not m.version_unknown:
                    to_backup.append(i)
                else:
                    update_only.append(i)
            if update_only:
                self._log.info(
                    f"{len(update_only)} 个条目没有旧版本可备份"
                    "（未下载或版本未知），自动按「仅更新」执行")
            backup_first = to_backup or None
        else:
            backup_first = None
        accepted = self._start_batch(app_id, list(mod_ids), backup_first)
        if dlg is not None:
            if accepted:
                dlg.mark_executed(action, mod_ids)
            else:
                # 拒绝详情控制器已写运行日志（通常是 steamcmd 未启动）
                dlg.notify_not_started(
                    "原因见底部控制台 · 运行日志，通常是 steamcmd 未"
                    "启动——切过去启动并登录后，回来再点【执行选中】，"
                    "勾选已保留")

    def _open_browser_picker(self) -> None:
        """模块②【从浏览器取标签页…】：弹出内嵌浏览器页的选择器
        （T23：页内完成，不再跳基础功能页）。勾好后【送到「加入新
        mod」】→ 对话框关闭、内容填进第②步并自动解析预览。
        【从浏览器取网址】基础功能页原样保留（独立入口，同一套
        采集代码，两条路一本账）。"""
        dlg = BrowserPickDialog(self)
        dlg.lines_picked.connect(self._on_handoff_to_addmod)
        dlg.exec()

    def _on_batch_done(self, summary: dict) -> None:
        """批次结束 → 按设置自动复扫入账；无论批次从哪个页面发起
        （mod 库页 / 日常更新模块页 / 加入新 mod 的【开批下载】），
        收尾都回叫对应页面显示结果。复扫对准"这一批所属的档案"
        （_start_batch 记下的 app_id），与批次进行中用户是否切过档案
        无关；quiet 版全程只写日志，不会在无人值守时弹窗卡住流程。
        温和停止 / 出错收尾的批次同样复扫：已下载的那几条一样要入账，
        复扫本身只读 acf、幂等无害（R7 + 决策 23⑤）。
        """
        # 先把归属摘下来再清：后面两处回叫都要用它
        batch_app_id = self._batch_app_id
        game = (self._repo.get_game(batch_app_id)
                if batch_app_id is not None else None)
        self._batch_app_id = None  # 所有路径都清掉：残留值没有任何用处
        game_name: str | None = None
        rescanned = False
        if game is not None:
            game_name = game.name
            if self._settings.get_int(_KEY_AUTO_RESCAN, 1) != 0:
                if self._current_game is None or \
                        game.app_id != self._current_game.app_id:
                    self._log.info(
                        f"批次属于档案「{game.name}」，后台为其复扫入账"
                        "（当前界面显示的是别的档案，不受影响）")
                self._pages[0].scan_local_quiet(game)
                rescanned = True  # 复扫是同步的（本地只读一个 acf 文本，毫秒级），走完这行复扫已经结束
            # 「加入新 mod」③步【开批下载】发起的批次：复扫后自动盘点
            # 它的第④步（matches_batch 自带守卫：确属该模块的批次才回叫，
            # 别的批次不惊动它）
            if batch_app_id is not None and \
                    self._pages[10].matches_batch(batch_app_id):
                self._pages[10].on_scan_confirmed()
        # 「日常更新」模块页的"最近一批"仪表盘（无条件回叫）
        self._pages[12].on_batch_finished(game_name, summary, rescanned)

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

    def _open_download_dir(self) -> None:
        """文件 → 打开 steamcmd 下载目录：当前档案的工坊内容目录
        （mod 文件夹 = 下载目录\\编号，决策 21③）。目录还没建（一批
        都没下载过）时说明而不是静默；无档案时菜单项已置灰，双保险。"""
        game = self._current_game
        if game is None or not game.download_dir:
            return
        path = Path(game.download_dir)
        if not path.is_dir():
            QMessageBox.information(
                self, "打开下载目录",
                f"这个目录还不存在（可能还没下载过任何 mod）：\n{path}\n"
                "steamcmd 首次下载时会自动创建；可在【游戏】菜单 →"
                "编辑档案里核对路径。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            # openUrl 失败是静默的（v2.18 教训）：手动兜底提示
            QMessageBox.warning(
                self, "打开下载目录",
                f"文件管理器没有响应，请手动打开：\n{path}")

    def _open_software_dir(self) -> None:
        """文件 → 打开软件所在目录：绿色软件的"家"。路径走
        appPaths.app_root()（T17 单源：源码运行=项目根、打包运行=
        exe 所在文件夹——数据、配置、日志全在其下）。"""
        path = appPaths.app_root()
        if not path.is_dir():
            QMessageBox.warning(self, "打开软件目录",
                                f"目录不存在？请手动检查：\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(
                self, "打开软件目录",
                f"文件管理器没有响应，请手动打开：\n{path}")

    def _toggle_side(self, visible: bool) -> None:
        """显示/隐藏左侧导航栏（T19⑭）：整个侧栏（切换下拉 + 导航树）
        一起收起，中央页面拿到全部宽度；回程走本菜单项。"""
        self._side.setVisible(visible)

    def _toggle_detail(self, visible: bool) -> None:
        """显示/隐藏 mod 库页详情面板（T19⑮）：主窗口只转发开关，
        布局归页面自己管（QSplitter 不给隐藏的子件分空间，
        表格自动占满整行）。"""
        self._pages[0].set_detail_visible(visible)

    def _start_batch(self, app_id: int, mod_ids: list,
                     backup_first: list | None = None) -> bool:
        """开批量下载批次。backup_first = 要先备份旧版本再下载的条目
        （日常更新一条龙的"备份+更新"动作，决策 40）；mod 库页手动
        批次不传 = 维持现状不备份（那边有右键手动备份兜底）。
        返回是否受理成功（决策 36⑧ 受理回执用；信号槽直连的调用方
        忽略返回值，无影响）。
        无论成败先弹出控制台并切到「下载批次」标签：备份阶段与下载
        阶段的进度都写在运行日志里，不能让用户对着没反应的按钮猜。
        """
        # 批次结束后自动复扫要对上档案（决策 26）；start_batch 拒绝时
        # 不会有 batch_done，残留值无害（下次 _start_batch 会覆盖）
        self._batch_app_id = app_id
        self._pop_console()
        self._console.show_batch_tab()
        return self._batch_controller.start_batch(app_id, mod_ids,
                                                  backup_first)

    def _backup_checked(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【备份选中项】→ 备份页切档案并开批次。"""
        self._nav.setCurrentItem(self._nav_items[2])  # 跳到备份页看进度条
        self._pages[2].backup_ids_for(app_id, mod_ids)

    # ---------- 账本导出 / 导入（T19 dataExporter，决策 33） ----------
    # 全程主线程：导出/导入都是一轮 SQL（毫秒级），与"数据库操作全在
    # 主线程"的既有口径一致，不需要工作线程。

    def _ask_open_json(self, title: str) -> str | None:
        """选一个 JSON 文件；取消返回 None。起始目录固定为数据目录——
        便携模式（T17）下数据跟着软件走，从数据目录起步最可预期。"""
        path, _ = QFileDialog.getOpenFileName(
            self, title, str(appPaths.data_dir()), "JSON 文件 (*.json)")
        return path or None

    def _export_ledger(self) -> None:
        """文件 → 导出完整账本…：7 张表全量 → JSON。先在内存构建 payload（纯读操作），再用它生成带时间戳的建议文件名弹保存框；
        用户取消就什么都不写。"""
        payload = dataExporter.build_ledger(self._repo)
        suggested = appPaths.data_dir() / dataExporter.default_filename(payload)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出完整账本", str(suggested), "JSON 文件 (*.json)")
        if not path:
            return
        out = dataExporter.save(payload, path)
        self._log.ok(f"账本已导出：{out}")
        self.statusBar().showMessage(f"账本已导出：{out}", 8000)

    def _import_ledger(self) -> None:
        """文件 → 导入完整账本…：清库重灌（决策 33 拍板，不做合并）。
        失败路径全部安全：load 的格式问题在动手前被拦下；import_ledger
        整体事务，任何失败回滚后原账无损——弹窗把这一点说清，别让
        用户以为账本被弄坏了。批次进行中拒绝：清库会跟批次收尾的
        复扫入账互相踩脚，等一等没有坏处。"""
        if self._batch_controller.is_active():
            QMessageBox.warning(
                self, "下载批次进行中",
                "批量下载尚未完成，等批次结束后再导入账本。")
            return
        path = self._ask_open_json("导入完整账本")
        if path is None:
            return
        try:
            payload = dataExporter.load(path)
        except ValueError as exc:
            QMessageBox.warning(self, "无法导入", str(exc))
            return
        ret = QMessageBox.question(
            self, "导入完整账本",
            dataExporter.ledger_summary(payload) + "\n\n"
                                                   "当前账本将被整体替换，此操作无法撤销。\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)  # 破坏性操作：默认停在"否"
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消账本导入")
            return
        try:
            counts = dataExporter.import_ledger(payload, self._repo)
        except (ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "导入失败",
                f"导入没有完成，当前账本原样保留、未做任何改动。\n\n{exc}")
            return
        self._switcher.reload()  # 重读 games 表，把新档案广播给所有页面
        self._log.ok(f"账本导入完成：{counts}")
        self.statusBar().showMessage(
            f"账本导入完成：{counts['games']} 个档案、"
            f"{counts['mods']} 条 mod 记录", 8000)

    def _export_sharepack(self) -> None:
        """文件 → 导出分享包…：当前游戏的清单 + 整理成果 → JSON。
        无档案时菜单项已置灰，这里双保险直接返回。"""
        game = self._current_game
        if game is None:
            return
        try:
            payload = dataExporter.build_sharepack(self._repo, game.app_id)
        except ValueError as exc:  # 理论不可达：档案来自同一 repo
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        suggested = appPaths.data_dir() / dataExporter.default_filename(payload)
        path, _ = QFileDialog.getSaveFileName(
            self, f"导出分享包 — {game.name}", str(suggested),
            "JSON 文件 (*.json)")
        if not path:
            return
        out = dataExporter.save(payload, path)
        n = len(payload["tables"]["mods"])
        self._log.ok(f"分享包已导出：「{game.name}」共 {n} 条 mod → {out}")
        self.statusBar().showMessage(f"分享包已导出：{n} 条 mod", 5000)

    def _import_sharepack(self) -> None:
        """文件 → 导入分享包…：增量并入（决策 33 拍板）——已有的 mod
        跳过、现有数据一字不改，风险低，不做确认弹窗，结果报数。
        TypeError 也在拦的行列：分享包的 schema 闸靠 Game(**row)/
        Mod(**row) 的构造（dataExporter 文件头说明），形状不对会从
        这里冒出来，弹窗兜住照样"原账无损"。"""
        path = self._ask_open_json("导入分享包")
        if path is None:
            return
        try:
            payload = dataExporter.load(path)
        except ValueError as exc:
            QMessageBox.warning(self, "无法导入", str(exc))
            return
        try:
            report = dataExporter.import_sharepack(payload, self._repo)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self, "导入失败",
                f"分享包没有并入，当前账本未做任何改动。\n\n{exc}")
            return
        self._switcher.reload()
        game = self._repo.get_game(report["app_id"])
        name = game.name if game else str(report["app_id"])
        elsewhere = (self._current_game is None or
                     self._current_game.app_id != report["app_id"])
        self._log.ok(f"分享包导入完成：「{name}」新增 {report['added']} 条、"
                     f"跳过 {report['skipped']} 条")
        self.statusBar().showMessage(
            f"分享包导入完成：「{name}」新增 {report['added']} 条、"
            f"跳过 {report['skipped']} 条"
            f"（导入条目均为「已收录」；本机有文件的话，到【mod 库】页【扫描本地】自动补上已下载状态）"
            + ("（；在左上角下拉切换到该游戏查看）" if elsewhere else ""),
            8000)

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
        self._console.terminal.shutdown()
        # 批次前的备份阶段若在跑：批间停止并等它收尾，防止退出时
        # 销毁活线程（决策 40）
        self._batch_controller.shutdown()
        for page in self._pages:
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        self._save_panel_state()

        self._repo.close()
        super().closeEvent(event)

    # ---------- 面板显隐的会话记忆（T19⑤ 轻量子集） ----------
    def _save_nav_state(self, *_item) -> None:
        """导航树组折叠落盘（T19⑤）。信号带变化的那一条目，统一吞掉——
        要记的永远是"全部组的现状"，逐条传参反而不便。"""
        q = QSettings()
        expanded = [
            self._nav.topLevelItem(i).text(0)
            for i in range(self._nav.topLevelItemCount())
            if self._nav.topLevelItem(i).childCount() > 0
               and self._nav.topLevelItem(i).isExpanded()
        ]
        q.setValue(_SES_NAV, ",".join(expanded))

    def _restore_nav_state(self) -> None:
        """启动恢复组折叠（_restore_panels 调用，T19⑤）。无记录（首次
        运行）→ 直接返回，维持建树默认全展开；有记录 → 名单里的展开、
        其余收起——与关窗那一刻一模一样。"""
        q = QSettings()
        raw = q.value(_SES_NAV)
        if raw is None:
            return
        want = {s for s in str(raw).split(",") if s}
        for i in range(self._nav.topLevelItemCount()):
            it = self._nav.topLevelItem(i)
            if it.childCount() > 0:
                it.setExpanded(it.text(0) in want)

    def _restore_panels(self) -> None:
        """启动时恢复三块面板的上次显隐。侧栏/详情走菜单 QAction
        （setChecked 触发既有 toggled 处理，不重写逻辑）；控制台
        停靠窗直接 setVisible。全默认 = 全显示（老用户无感）。"""
        q = QSettings()
        # 先恢复几何与停靠布局（saveState 含 dock 位置与高度），再由
        # 下面的三个显隐键覆盖可见性——几何归新键、显隐归老键，双源不打架
        geom = q.value(_SES_GEOM)
        if geom:
            self.restoreGeometry(geom)
        state = q.value(_SES_STATE)
        if state:
            self.restoreState(state)

        self._act_side.setChecked(str(q.value(_SES_SIDE, "1")) != "0")
        self._act_detail.setChecked(str(q.value(_SES_DETAIL, "1")) != "0")
        self._console_dock.setVisible(str(q.value(_SES_CONSOLE, "1")) != "0")
        self._restore_nav_state()  # 导航组折叠（T19⑤ 收官）


    def _save_panel_state(self) -> None:
        """退出前落盘当前显隐（closeEvent 调用；QSettings 析构时也会
        同步，这里显式写一遍求稳）。"""
        q = QSettings()
        q.setValue(_SES_SIDE, "1" if self._act_side.isChecked() else "0")
        q.setValue(_SES_DETAIL, "1" if self._act_detail.isChecked() else "0")
        q.setValue(_SES_CONSOLE,
                   "1" if self._console_dock.isVisible() else "0")
        q.setValue(_SES_GEOM, self.saveGeometry())
        q.setValue(_SES_STATE, self.saveState())


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
