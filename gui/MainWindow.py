"""主窗口
"""
"""
gui/MainWindow.py —— 左导航（档案切换器 + 导航树）+ 中央页面栈
+ 底部控制台停靠窗 + 菜单（含顶级「高级筛选」动作）+ 状态栏
+ 全局异常兜底 + 会话记忆。
V2 骨架轮建立；控制台轮补全底部停靠窗；mod 库轮接入工作台第一页
（列表 / 详情 / 批量操作 / 高级筛选）并接线其跨页入口。

三条结构铁律（动这个文件前先读）：
1. 页面编号住 core/constants（"改一词全项目跟"），树结构住本文件
   的 _NAV_SCHEMA；import 时 _check_nav 自检——漏登记、重复登记、
   不认识的编号当场炸，不带病进界面；
2. 未搬迁页面一律挂 PlaceholderPage 占位：每搬迁一轮真页面，在
   _build_page 加一个分支原位替换，导航树 / 会话记忆 / 档案广播
   全部零改动（"每轮可运行"策略）；
3. 页面编号即页面栈下标：程序化跳页一律走 _goto_page（会同步
   树高亮），不许裸调 stack——跳了树不跟，用户就迷路了。

跨页信号接线（set_game / 跳页请求 / 一条龙等）随对应页面搬迁逐条
回补，风格 = 页面发信号 → 主窗口转调或直连；set_game 广播循环与
closeEvent 收尾循环都靠 hasattr 自适应，页面搬迁零接线成本。

实装进度：欢迎页、档案切换器（侧栏）、控制台（底部停靠窗，
含 steamcmd 终端）、mod 库页（工作台第一页）——顶级「高级筛选」
动作、视图菜单详情面板项、对话框跳页请求均随 mod 库轮接线；
其余页面为占位器（见 _PAGE_TITLES 与 _build_page 的分支表）。
"""

import sys
import threading
from pathlib import Path
from gui.batchDownloadController import BatchDownloadController
from gui.confirmListDialog import ConfirmListDialog
from gui.accountCenterPage import AccountCenterPage

from gui.welcomePage import PROJECT_URL, WelcomePage
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import appPaths
from core import constants
from core.appSettings import AppSettings
from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.consolePanel import ConsolePanel
from gui.gameSwitcher import GameSwitcher
from gui.logBus import LogBus
from gui.modListPage import ModListPage

DEFAULT_DB_PATH = appPaths.db_path()  # T17：数据根单源（源码=项目根\data）
_NAV_WIDTH = 210


# ============================================================
# 页面显示名（单源：导航树与占位器共用，改名只动这一处）
# 带 ※ 的是骨架轮暂定名（该页未搬迁）：以各页搬迁时的自述为准，
# 改这里即可，树与占位器自动跟。
# ============================================================
_PAGE_TITLES: dict[int, str] = {
    constants.PAGE_WELCOME: "欢迎",
    constants.PAGE_MOD_LIST: "mod 库",
    constants.PAGE_ACCOUNT_CENTER: "入账中心",
    constants.PAGE_UPDATE_CHECK: "更新检测",
    constants.PAGE_UPDATE_COMPARE: "更新对照",
    constants.PAGE_ADD_MOD: "加入新 mod",
    constants.PAGE_DAILY_UPDATE: "日常更新",
    constants.PAGE_FIRST_USE: "首次使用",
    constants.PAGE_GAME_EXIT: "游戏退场",
    constants.PAGE_UNINSTALL: "卸载与清理",
    constants.PAGE_COMMAND_GEN: "下载命令生成",
    constants.PAGE_BATCH_OPEN: "批量下载",  # ※
    constants.PAGE_BACKUP: "备份与恢复",
    constants.PAGE_BACKUP_OVERVIEW: "备份总览",
    constants.PAGE_TITLE_CHECK: "标题检测",  # ※
    constants.PAGE_REMOTE_HEALTH: "远端健康",  # ※
    constants.PAGE_DEP_CHECK: "依赖检测",  # ※
    constants.PAGE_EXCEPTION: "异常处理",  # ※
    constants.PAGE_VERIFY: "账实核验",
    constants.PAGE_JUNCTION_CHECK: "联接检测",  # ※
    constants.PAGE_CLEANUP: "清理与删除",
    constants.PAGE_PURGED: "已清账管理",
    constants.PAGE_IMPORT: "网址批量导入",
    constants.PAGE_MIGRATION: "换机迁移",
    constants.PAGE_RESCUE: "恢复旧版本",
    constants.PAGE_SHARE_LIST: "分享清单",
    constants.PAGE_STATS: "统计",
    constants.PAGE_API_KEY: "Steam API 密钥",
    constants.PAGE_SETTINGS: "设置",
}


# ============================================================
# 导航树结构（组名只在这里出现一次——要改组名也是这一处，
# 并到 _LEGACY_GROUP_NAMES 登记一行旧名，老用户折叠记忆才不丢）
#
# 分组逻辑（控制台轮定稿）：上半棵树按"一页一个功能"分领域，
# 唯独最底一组"分步向导"按使用方式分——收编规则：操作链长、
# 步骤有先后、做错有代价、熟手不常用的复杂实操（首次使用开荒、
# 五步游戏退场、七步换机迁移、恢复旧版本这类），进向导；
# 一页一工具、随手可用的，留各领域组。原先"功能模块"组排第二位，
# 与"下载与备份"等平级，读者会误以为"加入新 mod"和"下载命令
# 生成"是并列的两种入口（其实是引导与单页的关系）；"主循环"
# 则是开发者视角的词，用户看不懂——两者一并改掉。
# ============================================================
_NAV_SCHEMA: list[tuple[str, int | list[int]]] = [
    ("欢迎", constants.PAGE_WELCOME),
    ("工作台", [constants.PAGE_MOD_LIST, constants.PAGE_ACCOUNT_CENTER,
                constants.PAGE_UPDATE_CHECK, constants.PAGE_UPDATE_COMPARE]),
    ("下载与备份", [constants.PAGE_COMMAND_GEN, constants.PAGE_BATCH_OPEN,
                    constants.PAGE_BACKUP, constants.PAGE_BACKUP_OVERVIEW]),
    ("检测与异常", [constants.PAGE_TITLE_CHECK, constants.PAGE_REMOTE_HEALTH,
                    constants.PAGE_DEP_CHECK, constants.PAGE_EXCEPTION]),
    ("清理与账务", [constants.PAGE_VERIFY, constants.PAGE_JUNCTION_CHECK,
                    constants.PAGE_CLEANUP, constants.PAGE_PURGED]),
    ("档案与工具", [constants.PAGE_IMPORT, constants.PAGE_SHARE_LIST,
                    constants.PAGE_STATS, constants.PAGE_API_KEY,
                    constants.PAGE_SETTINGS]),
    ("分步向导", [constants.PAGE_ADD_MOD, constants.PAGE_DAILY_UPDATE,
                  constants.PAGE_FIRST_USE, constants.PAGE_GAME_EXIT,
                  constants.PAGE_UNINSTALL, constants.PAGE_MIGRATION,
                  constants.PAGE_RESCUE]),
]

# 导航组改名对照（一次性迁移，只读不写）：折叠记忆按组名存盘，
# 组改名后旧记录里的旧名靠这张表映射到新名；迁移不回写，记录里
# 旧名长期存在也无害。今后再改组名，在此追加一行即可。
_LEGACY_GROUP_NAMES: dict[str, str] = {
    "主循环": "工作台",
    "功能模块": "分步向导",
}


def _check_nav() -> None:
    """建树前的结构自检（显式爆炸，别让漏登记流到运行期才发现）：
    ① 树里每个编号都认识（在 _PAGE_TITLES 里）；
    ② 没有重复登记；
    ③ constants 的 29 页全部入树、无遗漏。"""
    seen: list[int] = []
    for _name, spec in _NAV_SCHEMA:
        if isinstance(spec, int):
            seen.append(spec)
        else:
            seen.extend(spec)
    unknown = [i for i in seen if i not in _PAGE_TITLES]
    if unknown:
        raise RuntimeError(f"导航树里有不认识的页面编号：{unknown}")
    dup = sorted({i for i in seen if seen.count(i) > 1})
    if dup:
        raise RuntimeError(f"导航树里有重复登记的页面：{dup}")
    missing = sorted(set(_PAGE_TITLES) - set(seen))
    if missing:
        raise RuntimeError(f"constants 里的页面没进导航树：{missing}")


_check_nav()


# 面板显隐与几何的会话记忆（QSettings 而非 AppSettings——会话状态
# ≠用户配置；值一律 "1"/"0" 字符串或 QByteArray）
_SES_SIDE = "session/side_visible"        # 左侧导航栏
_SES_CONSOLE = "session/console_visible"  # 底部控制台（控制台轮补回）
_SES_DETAIL = "session/modlib_detail_visible"  # mod 库详情面板（mod 库轮补回）
_SES_GEOM = "session/window_geometry"     # 窗口大小与位置
_SES_STATE = "session/window_state"       # 停靠窗布局（位置/大小/浮动）
_SES_NAV = "session/nav_expanded"         # 导航组折叠（展开的组名逗号串）
_SES_LAST_GAME = "session/last_game_app_id"  # 上次打开的游戏档案


class PlaceholderPage(QWidget):
    """未搬迁页面的占位器：居中两行说明，零交互零依赖。
    该页搬迁轮在 _build_page 加分支原位替换，本类与导航树都不用动。"""

    def __init__(self, title: str, pid: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        big = QLabel(f"「{title}」页还没有搬迁", self)
        big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small = QLabel(
            f"页面编号 {pid}（core/constants PAGE_*）\n"
            "主窗口骨架轮的占位器——该页搬迁时原位替换，"
            "导航树与档案广播零改动",
            self)
        small.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small.setWordWrap(True)
        lay.addWidget(big)
        lay.addWidget(small)
        lay.addStretch(2)


class MainWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Steam创意工坊Mod辅助管理工具")
        self.resize(1200, 800)

        # 设置必须先建：数据库构造时要从中读"快照保留条数"注入
        self._settings = AppSettings()

        # 日志总线先于页面建好，构造页面时注入。控制台停靠窗
        # （_build_docks）接住总线后，日志同时去两处：文件旁路
        # （装了 loguru 时落 data/logs/app_*.log）+ 控制台"运行日志"页
        self._log = LogBus()

        # 数据库操作全在主线程（毫秒级）；联网/扫描等长操作走各自
        # 页面的工作线程
        self._repo = SQLiteRepository(
            DEFAULT_DB_PATH,
            snapshot_keep=self._settings.get_int("snapshot_keep", 5))

        self._current_game: Game | None = None

        self._build_central()
        self._build_docks()
        self._build_menus()
        self._build_status_bar()
        # 批次控制器（M2 判决闭环轮）：下载批次的大脑。注入控制台
        # 面板（自取终端与批次卡片）、账本、设置与日志；接线三条
        # 终端信号 + 一条收尾信号。先于 _on_game_changed 建：那里
        # 要按档案关闭确认清单
        self._confirm_dialog: ConfirmListDialog | None = None
        self._batch_ctrl = BatchDownloadController(
            self._repo, self._settings, self._log, console=self._console)
        self._console.terminal.download_batch_requested.connect(
            self._on_download_batch_requested)
        self._console.terminal.set_stop_guard(self._batch_ctrl.confirm_stop)
        self._console.terminal.process_exited.connect(
            self._batch_ctrl.on_process_exited)
        self._console.step_list.ids_action.connect(
            self._batch_ctrl.on_ids_action)
        self._batch_ctrl.confirmation_ready.connect(
            self._on_confirmation_ready)

        # 构造期 switcher 已发射过信号（当时无人监听），补一次初始化
        self._on_game_changed(self._switcher.current_game())

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
        self._side = side  # 视图菜单显隐用：局部变量跨方法必须挂 self
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(8)

        # 左上：档案切换器（已搬迁，真实可用）。settings 供建档时
        # 推导下载目录，log 供建档回执进日志
        self._switcher = GameSwitcher(self._repo, side,
                                      settings=self._settings,
                                      log=self._log)
        self._switcher.current_game_changed.connect(self._on_game_changed)
        side_layout.addWidget(self._switcher)

        # 左下：导航树——按 _NAV_SCHEMA 生成，条目挂 constants.PAGE_*
        self._nav = QTreeWidget(side)
        self._nav.setHeaderHidden(True)
        self._nav.setIndentation(14)
        self._nav_items: dict[int, QTreeWidgetItem] = {}  # 页面编号 → 树条目

        for name, spec in _NAV_SCHEMA:
            if isinstance(spec, int):
                item = QTreeWidgetItem([name])
                item.setData(0, Qt.ItemDataRole.UserRole, spec)
                self._nav.addTopLevelItem(item)
                self._nav_items[spec] = item
                continue
            group = QTreeWidgetItem([name])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)  # 组节点不可选中，只折叠
            for pid in spec:
                child = QTreeWidgetItem([_PAGE_TITLES[pid]])
                child.setData(0, Qt.ItemDataRole.UserRole, pid)
                self._nav_items[pid] = child
                group.addChild(child)
            group.setExpanded(True)
            self._nav.addTopLevelItem(group)

        self._nav.currentItemChanged.connect(self._on_nav_changed)
        self._nav.itemClicked.connect(self._on_nav_clicked)
        # 组折叠记忆（T19⑤）：展开收起即落盘；启动恢复在 _restore_panels。
        # 建树时的 setExpanded(True) 发生在接线之前，不会触发保存——
        # 无记录时维持"默认全展开"
        self._nav.itemExpanded.connect(self._save_nav_state)
        self._nav.itemCollapsed.connect(self._save_nav_state)
        side_layout.addWidget(self._nav, 1)

        # 中央：页面栈。编号即栈下标（constants 口径：编号 = 落位序），
        # 按编号顺序装入，程序化跳页直接用编号当下标
        self._stack = QStackedWidget(central)
        self._pages: dict[int, QWidget] = {}
        for pid in sorted(_PAGE_TITLES):
            page = self._build_page(pid)
            self._pages[pid] = page
            self._stack.addWidget(page)

        # mod 库页跨页接线（mod 库轮）：
        # ① 高级筛选对话框「到 mod 库查看结果」→ 程序化跳回本页
        #   （_goto_page 同步树高亮——铁律 3）；
        # ② 顶级「高级筛选」动作与视图菜单详情面板项的落点也指向本页
        #   （动作在本方法之后的 _build_menus 里连接，页面彼时已建好）。
        # hasattr 守卫：分支万一回退成占位器，只降级为无此入口，
        # 不让整窗起不来（与广播循环同一自适应哲学）
        modlib = self._pages[constants.PAGE_MOD_LIST]
        if hasattr(modlib, "advanced_results_requested"):
            modlib.advanced_results_requested.connect(
                lambda: self._goto_page(constants.PAGE_MOD_LIST))
        # mod 库页【下载选中项】→ 批次控制器开批（M2 轮接线：信号
        # 早已留好，落点到位）。备份优先入口（A1 决策）的宿主在
        # 检测页（M3），此处是普通下载批次
        if hasattr(modlib, "download_requested"):
            modlib.download_requested.connect(self._on_download_requested)

        # 启动默认落「欢迎」页（setCurrentItem 会触发 currentItemChanged，
        # 与手点同一条路，栈已就位）
        self._nav.setCurrentItem(self._nav_items[constants.PAGE_WELCOME])

        root.addWidget(side)
        root.addWidget(self._stack, 1)
        self.setCentralWidget(central)

    def _build_docks(self) -> None:
        """底部控制台停靠窗（控制台轮）：运行日志 + steamcmd 终端。
        objectName 必须设——saveState/restoreState 按 objectName
        记忆停靠布局，缺了它停靠位置/大小/浮动状态都记不住。"""
        self._dock_console = QDockWidget("控制台", self)
        self._dock_console.setObjectName("dock_console")
        self._console = ConsolePanel(self._log, self,
                                     settings=self._settings)
        # "有新消息时弹出"的请求由面板发、本窗口执行——停靠窗是
        # 这里建的，怎么弹归这里管（谁建谁管）
        self._console.show_requested.connect(self._show_console)
        self._dock_console.setWidget(self._console)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea,
                           self._dock_console)

    def _build_page(self, pid: int) -> QWidget:
        """按编号装配一页：已搬迁的真页面 / 未搬迁的占位器。
        每轮搬迁一个页面 = 文件头 import 区加一行 + 这里加一个分支，
        导航树、会话记忆、set_game 广播全部按编号工作，零改动。
        mod 库轮接入 ModListPage：repo/settings/log 三件注入，
        parent 住页面栈。"""
        if pid == constants.PAGE_WELCOME:
            return WelcomePage(self._stack)
        if pid == constants.PAGE_MOD_LIST:
            return ModListPage(self._repo, self._settings,
                               parent=self._stack, log=self._log)
        if pid == constants.PAGE_ACCOUNT_CENTER:
            return AccountCenterPage(self._repo, self._settings,
                                     parent=self._stack, log=self._log)

        return PlaceholderPage(_PAGE_TITLES[pid], pid)

    def _build_menus(self) -> None:
        # —— 文件(&F)：本轮只挂目录两项 + 退出。账本/分享包导出导入
        # 走 core/dataExporter（尚未搬迁，且 v2 要按判决制适配），
        # 随其轮次回填 ——
        m_file = self.menuBar().addMenu("文件(&F)")

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
        m_file.addAction(act_quit)

        # —— 游戏(&G)：档案工具四项（编辑/连接/重定位/删除）随四个
        # 对话框搬迁轮回填（switcher 的 open_* 方法届时一并补）；
        # 本轮建档入口真实可用 ——
        m_game = self.menuBar().addMenu("游戏(&G)")
        act_add_game = QAction("添加游戏档案…", self)
        act_add_game.setToolTip(
            "输入 AppID 一屏建档：自动查名、查重、下载目录按 steamcmd "
            "位置推导并可预览")
        act_add_game.triggered.connect(self._switcher.add_game_dialog)
        m_game.addAction(act_add_game)

        # —— 顶级动作「高级筛选(&S)」：不挂菜单、点字即开（拍板记录：
        # 主打随时搜索——人在任何页都能拉开就查）。Alt+S 或
        # Ctrl+Shift+F 同效；非模态窗口，开着不影响继续操作 ——
        act_adv = QAction("高级筛选(&S)", self)
        act_adv.setToolTip(
            "打开 mod 库高级筛选：标题/备注/编号/作者/大小/更新时间/"
            "标签多条件叠加，与 mod 库页顶栏筛选叠加生效。\n"
            "非模态窗口——开着可以边看结果边改；快捷键 Ctrl+Shift+F")
        act_adv.setShortcut(QKeySequence("Ctrl+Shift+F"))
        act_adv.triggered.connect(self._open_advanced_search)
        self.menuBar().addAction(act_adv)

        # —— 视图(&V)：侧栏 / 控制台 / 详情面板三个显隐开关
        #（末者随 mod 库轮回填）——
        m_view = self.menuBar().addMenu("视图(&V)")
        self._act_side = QAction("显示 / 隐藏左侧导航栏", self)
        self._act_side.setCheckable(True)
        self._act_side.setChecked(True)
        self._act_side.toggled.connect(self._toggle_side)
        m_view.addAction(self._act_side)

        # 控制台显隐：toggleViewAction 是 Qt 为停靠窗准备的标准开关——
        # 勾选状态与停靠窗显隐双向自动同步（点菜单关窗、手动关窗都会
        # 反映到勾选上），不用手写槽，也就没有反馈环要防。
        # 动作文字默认取窗口标题"控制台"，这里换成与侧栏项同款句式
        self._act_console = self._dock_console.toggleViewAction()
        self._act_console.setText("显示 / 隐藏控制台")
        self._act_console.setToolTip(
            "底部控制台面板（运行日志 + steamcmd 终端两个标签页）的"
            "显隐开关；隐藏期间有新日志到达时仍会按设置自动弹出")
        m_view.addAction(self._act_console)

        # 详情面板显隐（mod 库轮）：只影响 mod 库页右侧详情——
        # QSplitter 不给隐藏子件分配空间，表格自动占满整行
        self._act_detail = QAction("显示 / 隐藏详情面板", self)
        self._act_detail.setCheckable(True)
        self._act_detail.setChecked(True)
        self._act_detail.setToolTip(
            "mod 库页右侧的详情面板（选中条目的字段、确认来源与最近"
            "判决）显隐开关")
        self._act_detail.toggled.connect(self._toggle_detail)
        m_view.addAction(self._act_detail)

        # QMenu 默认不显示动作悬浮说明（T19⑩ 同款坑），逐菜单打开。
        # 现稿 m_view 漏了这行（当时视图只有一项），本轮补上
        m_file.setToolTipsVisible(True)
        m_game.setToolTipsVisible(True)
        m_view.setToolTipsVisible(True)

        # —— 帮助(&H)：关于。图标 + 项目主页链接依赖欢迎页的
        # 常量与 icon 资源——欢迎页已搬迁，富版已回填 ——
        m_help = self.menuBar().addMenu("帮助(&H)")
        act_about = QAction("关于…", self)
        act_about.setToolTip("查看本工具的版本与简介")
        act_about.triggered.connect(self._about)
        m_help.addAction(act_about)
        m_help.setToolTipsVisible(True)

    def _about(self) -> None:
        ver = QApplication.applicationVersion()
        QMessageBox.about(
            self, "关于",
            "<b>Steam 创意工坊 Mod 辅助管理工具</b>"
            + (f"（版本 {ver}）" if ver else "")
            + "<br><br>帮助大量使用 mod 的 Steam 玩家，在不打开 Steam 客户端"
              "的情况下完成工坊 mod 的登记、更新检测、下载与备份恢复。"
              "绿色软件：账本、设置等全部数据保存在软件自己的文件夹里。"
            + f"<br><br>项目主页：<a href=\"{PROJECT_URL}\">{PROJECT_URL}</a>"
              "（点击用浏览器打开）")

    def _build_status_bar(self) -> None:
        self._status_game = QLabel(self)
        self._status_db = QLabel(f"数据库：{DEFAULT_DB_PATH}")
        self.statusBar().addWidget(self._status_game)
        self.statusBar().addPermanentWidget(self._status_db)

    # ---------- 槽 ----------

    def _on_nav_changed(self, current: QTreeWidgetItem | None,
                        _previous) -> None:
        if current is None:
            return
        pid = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(pid, int):
            self._stack.setCurrentIndex(pid)
            # 各页"进页刷新"钩子（V1 的 refresh 系列）随页面搬迁逐个回补

    def _on_nav_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.childCount():
            # 点组名 = 折叠/展开，和点小箭头等效
            item.setExpanded(not item.isExpanded())

    def _goto_page(self, pid: int) -> None:
        """程序化跳页 + 导航树高亮同步：setCurrentIndex 不经过点击，
        树高亮不会自己跟上——跨页跳转一律走这里，别裸调 stack
        （决策 13 沿用）。"""
        self._stack.setCurrentIndex(pid)
        item = self._nav_items.get(pid)
        if item is not None:
            self._nav.setCurrentItem(item)

    def _open_advanced_search(self) -> None:
        """顶级「高级筛选」动作落点：对话框实例由 mod 库页持有，
        这里只转调（谁持有谁打开）。hasattr 守卫同广播循环——页面
        分支万一回退为占位器，动作静默降级不炸。"""
        page = self._pages.get(constants.PAGE_MOD_LIST)
        opener = getattr(page, "open_advanced_search", None)
        if callable(opener):
            opener()

    def _toggle_detail(self, visible: bool) -> None:
        """视图菜单「详情面板」开关落点：面板是 mod 库页 splitter 的
        子件，显隐归它管，这里转调。"""
        page = self._pages.get(constants.PAGE_MOD_LIST)
        setter = getattr(page, "set_detail_visible", None)
        if callable(setter):
            setter(visible)

    def _show_console(self) -> None:
        """自动弹出（ConsolePanel.show_requested）：控制台整个被关掉
        而新日志到来时，把停靠窗拉回屏幕并置前。是否弹由面板按设置
        判断，这里只管"怎么弹"。"""
        self._dock_console.show()
        self._dock_console.raise_()

    def _on_game_changed(self, game: Game | None) -> None:
        self._current_game = game
        if game is None:
            self._status_game.setText("当前游戏：（无）—— 请先添加档案")
        else:
            self._status_game.setText(
                f"当前游戏：{game.name}（{game.app_id}）")

        # set_game 广播循环：占位器没有 set_game，hasattr 自动跳过；
        # 页面搬迁后自动纳入广播，本循环零改动（V1 同款结构）。
        # mod 库轮起，切档案 = mod 库整表重载 + 详情清空（页面内自理）
        for page in self._pages.values():
            if hasattr(page, "set_game"):
                page.set_game(game)

        # 无档案时目录入口没有操作对象；添加档案不在其列——空库也能加
        # （更新确认清单"切档案自动关"随更新检测轮回补）
        self._act_open_download.setEnabled(
            game is not None and bool(game.download_dir))
        # 收尾确认清单跟着档案走：切档案即关（非模态三件套之二）。
        # 待确认队列按档案过滤，新档案的清单由下一次批次收尾再弹
        if self._confirm_dialog is not None:
            self._confirm_dialog.close()
            self._confirm_dialog = None


    def _open_download_dir(self) -> None:
        """文件 → 打开 steamcmd 下载目录：当前档案的工坊内容目录
        （mod 文件夹 = 下载目录\\编号）。目录还没建时说明而不是静默；
        无档案时菜单项已置灰，双保险。"""
        game = self._current_game
        if game is None or not game.download_dir:
            return
        path = Path(game.download_dir)
        if not path.is_dir():
            QMessageBox.information(
                self, "打开下载目录",
                f"这个目录还不存在（可能还没下载过任何 mod）：\n{path}\n"
                "steamcmd 首次下载时会自动创建。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            # openUrl 失败是静默的（v2.18 教训）：手动兜底提示
            QMessageBox.warning(
                self, "打开下载目录",
                f"文件管理器没有响应，请手动打开：\n{path}")

    def _open_software_dir(self) -> None:
        """文件 → 打开软件所在目录：绿色软件的"家"。路径走
        appPaths.app_root()（T17 单源：数据、配置、日志全在其下）。"""
        path = appPaths.app_root()
        if not path.is_dir():
            QMessageBox.warning(self, "打开软件目录",
                                f"目录不存在？请手动检查：\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "打开软件目录",
                                f"文件管理器没有响应，请手动打开：\n{path}")
    def _on_download_requested(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【下载选中项】的落点：转批次控制器开批。开工检查
        （批次互斥 / steamcmd 在跑 / 下载目录已设）控制器里都有，
        被拒只进日志——成功则把控制台切到「下载批次」标签看进度。"""
        if self._batch_ctrl.start_batch(
                app_id, mod_ids, backup_first=False, trigger_versions=None):
            self._console.show_batch_tab()

    def _on_download_batch_requested(self, app_ids: list, mod_ids: list) -> None:
        """终端转批次裁决（受理回执制）：终端只解析与发请求，
        受不受理这里说了算，结果经 batch_handoff_receipt 回话——
        受理才改写输入框，被拒不装作已处理。"""
        game = self._current_game
        accepted = False
        if game is None:
            self._log.warn("转批次被拒：还没有游戏档案——请先在左上角"
                           "添加或选择游戏档案")
        elif app_ids and set(app_ids) != {game.app_id}:
            shown = "、".join(str(a) for a in app_ids[:5])
            self._log.warn(f"转批次被拒：命令里的 AppID（{shown}）与当前"
                           f"档案（{game.app_id}）不一致——请按档案分开"
                           "粘贴，或先切换到对应档案")
        elif self._batch_ctrl.is_busy():
            self._log.warn("转批次被拒：已有批次在跑——等它结束或先停止")
        else:
            accepted = self._batch_ctrl.start_batch(
                game.app_id, mod_ids, backup_first=False,
                trigger_versions=None)
            if accepted:
                self._console.show_batch_tab()   # 受理即切到批次页看进度

            # start_batch False 时原因已进日志；回话按受理失败处理，
            # 输入框原文保留，用户可修正后重发
        self._console.terminal.batch_handoff_receipt(accepted)

    def _on_confirmation_ready(self, verdicts: list) -> None:
        """批次收尾 → 弹确认清单（非模态三件套之一：同刻至多一份，
        先关旧的再开新的；切档案自动关见 _on_game_changed）。"""
        if self._confirm_dialog is not None:
            self._confirm_dialog.close()
        # 清单归属 = 判决行自带的档案（批次锁定的是开批时的档案）。
        # 不能取"当前档案"：用户批次中途切了游戏，取当前档案会把
        # 清单按错档案过滤，弹出来的就是空表
        game = None
        if verdicts:
            game = self._repo.get_game(verdicts[0].game_id)
        self._confirm_dialog = ConfirmListDialog(
            self._repo, game, verdicts, self, log=self._log)
        self._confirm_dialog.show()

    def _toggle_side(self, visible: bool) -> None:
        """显示/隐藏左侧导航栏（T19⑭）：整个侧栏（切换下拉 + 导航树）
        一起收起，中央页面拿到全部宽度；回程走本菜单项。"""
        self._side.setVisible(visible)

    def closeEvent(self, event) -> None:
        # 控制台的 steamcmd 终端先收：在跑时优雅退出（发 quit 等 3 秒，
        # 不退才强杀）——保住 steamcmd 的登录缓存，下次启动免重新验证。
        # 它是外部子进程，退出越早越好，所以放在所有页面收尾之前。
        # （批次进行中的退出确认弹窗随批次轮回补：届时经 terminal 的
        # stop_guard 拦一道，再走本收尾）
        # 批次进行中先确认（M2）：退出 = 中止在途批次
        if self._batch_ctrl.is_busy() and not self._batch_ctrl.confirm_exit():
            event.ignore()
            return

        self._console.terminal.shutdown()
        self._batch_ctrl.shutdown()   # 批查/备份线程等 1.5 秒，等不到 park 保活

        # 页面若带后台线程 / 持有需断链的对话框（mod 库页的
        # shutdown 摘高级筛选广播，防退出路上炸已关闭的库），先请停
        # 再关库——hasattr 自动发现，页面搬迁零接线
        for page in self._pages.values():
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
        """启动恢复组折叠。无记录（首次运行）→ 维持建树默认全展开；
        有记录 → 名单里的展开、其余收起——与关窗那一刻一模一样。
        组改名走 _LEGACY_GROUP_NAMES 一次性映射，老用户折叠状态不丢。"""
        q = QSettings()
        raw = q.value(_SES_NAV)
        if raw is None:
            return
        want = {_LEGACY_GROUP_NAMES.get(s, s)
                for s in str(raw).split(",") if s}
        for i in range(self._nav.topLevelItemCount()):
            it = self._nav.topLevelItem(i)
            if it.childCount() > 0:
                it.setExpanded(it.text(0) in want)

    def _restore_panels(self) -> None:
        """启动恢复：窗口几何 / 停靠布局 / 侧栏 / 控制台 / 详情面板
        显隐 / 组折叠 / 上次档案。几何先恢复（saveState 含停靠布局），
        再由显隐键覆盖可见性。全默认 = 全显示。"""
        q = QSettings()
        geom = q.value(_SES_GEOM)
        if geom:
            self.restoreGeometry(geom)
        state = q.value(_SES_STATE)
        if state:
            self.restoreState(state)
        self._act_side.setChecked(str(q.value(_SES_SIDE, "1")) != "0")

        # 控制台显隐：restoreState 虽也会带显隐，这里仍用独立会话键
        # 覆盖——与侧栏同款口径（显隐状态不依赖 saveState 的二进制
        # 内容，软件换版本也能读）；无记录 = 默认显示（停靠窗本来就
        # 显示着，不动即可）
        vis = q.value(_SES_CONSOLE)
        if vis is not None:
            self._dock_console.setVisible(str(vis) != "0")

        # 详情面板显隐（mod 库轮）：无记录 = 默认显示
        self._act_detail.setChecked(str(q.value(_SES_DETAIL, "1")) != "0")

        self._restore_nav_state()

        # 恢复上次打开的档案：构造期已按"下拉第一项"补广播过一次，
        # 这里若目标档案不同会再广播一次——各页 set_game 是毫秒级
        # 重载，两次换来"启动即停在上次的档案"，值得。
        # 找不到（档案被删/账本换过）→ 静默跳过，不额外报错。
        raw = q.value(_SES_LAST_GAME)
        try:
            last_id = (int(raw) if raw is not None and str(raw) != ""
                       else None)
        except (TypeError, ValueError):
            last_id = None  # 脏值当没有：绝不让历史遗留值混进选择
        if last_id is not None:
            cur = self._switcher.current_game()
            if cur is None or cur.app_id != last_id:
                if self._switcher.set_current_by_app_id(last_id):
                    g = self._current_game
                    if g is not None:
                        self._log.info(f"已恢复上次打开的档案：「{g.name}」")

    def _save_panel_state(self) -> None:
        """退出前落盘当前显隐（closeEvent 调用；QSettings 析构时也会
        同步，这里显式写一遍求稳）。"""
        q = QSettings()
        q.setValue(_SES_SIDE, "1" if self._act_side.isChecked() else "0")
        q.setValue(_SES_CONSOLE,
                   "1" if self._dock_console.isVisible() else "0")
        q.setValue(_SES_DETAIL,
                   "1" if self._act_detail.isChecked() else "0")
        q.setValue(_SES_GEOM, self.saveGeometry())
        q.setValue(_SES_STATE, self.saveState())
        # 上次打开的档案：存 app_id 不存名字——档案改名、重名都不会
        # 错位；空库存空串，恢复侧按"没有"处理
        game = self._current_game
        q.setValue(_SES_LAST_GAME, game.app_id if game is not None else "")

    # ---------- 全局异常兜底 ----------

    def _install_excepthook(self) -> None:
        """让"漏出来的意外错误"被用户看见。错误处理原则：照旧完整
        暴露、绝不吞——终端 traceback 照印（sys.__excepthook__ 原样
        转发）；改变的只有"报到地点"：日志同步多一条红字，主线程的
        意外再弹一个框。后台线程的钩子只发日志、不弹框——工作线程
        严禁碰控件（R11），而 LogBus 是信号总线，跨线程安全。"""

        def main_hook(exc_type, exc_value, exc_tb):
            self._log.error(
                f"未处理的异常：{exc_type.__name__}: {exc_value}"
                "（完整 traceback 见终端）")
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            QMessageBox.critical(
                self, "程序内部错误",
                "发生了一个程序内部错误，详情已写入日志。\n\n"
                f"{exc_type.__name__}: {exc_value}")

        sys.excepthook = main_hook

        def thread_hook(args) -> None:
            self._log.error(
                f"后台线程异常：{args.exc_type.__name__}: {args.exc_value}"
                "（完整 traceback 见终端）")
            threading.__excepthook__(args)

        threading.excepthook = thread_hook
