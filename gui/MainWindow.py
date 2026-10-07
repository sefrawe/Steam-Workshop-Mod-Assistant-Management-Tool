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
import sqlite3
import sys
import threading
from pathlib import Path
from gui.backupOverviewPage import BackupOverviewPage
from gui.backupPage import BackupPage
from core import dataExporter
from gui.batchDownloadController import BatchDownloadController
from gui.confirmListDialog import ConfirmListDialog
from gui.accountCenterPage import AccountCenterPage
from gui.updateCheckPage import UpdateCheckPage
from gui.updateSelectDialog import UpdateSelectDialog
from gui.verifyPage import VerifyPage
from gui.junctionCheckPage import JunctionCheckPage
from gui.deletePage import DeletePage
from gui.purgedPage import PurgedPage
from gui.titleCheckPage import TitleCheckPage
from gui.remoteHealthPage import RemoteHealthPage
from gui.depCheckPage import DepCheckPage
from gui.exceptionPage import ExceptionPage
from gui.browserPickDialog import BrowserPickDialog
from gui.dailyUpdatePage import DailyUpdatePage
from gui.migrationPage import MigrationPage
from gui.rescuePage import RescuePage
from gui.uninstallPage import UninstallPage
from gui.addModPage import AddModPage
from gui.gameExitPage import GameExitPage
from gui.quickCommandPage import QuickCommandPage
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
    QWidget,QFileDialog,
)
from gui.browserTabPage import BrowserTabPage
from gui.browserPickDialog import BrowserPickDialog


from core import appPaths
from core import constants
from core.appSettings import AppSettings
from core.models import Game
from core.sqliteRepository import SQLiteRepository
from gui.consolePanel import ConsolePanel
from gui.gameSwitcher import GameSwitcher
from gui.logBus import LogBus
from gui.modListPage import ModListPage
from gui.updateComparePage import UpdateComparePage
from gui.commandGenPage import CommandGenPage
from gui.statsPage import StatsPage
from gui.settingsPage import SettingsPage

from gui.shareListPage import ShareListPage
from gui.apiKeyPage import ApiKeyPage
from gui.firstUsePage import FirstUsePage

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

    constants.PAGE_BACKUP: "备份与恢复",
    constants.PAGE_BACKUP_OVERVIEW: "备份总览",
    constants.PAGE_TITLE_CHECK: "标题检测",
    constants.PAGE_REMOTE_HEALTH: "远端健康",
    constants.PAGE_DEP_CHECK: "依赖检测",
    constants.PAGE_EXCEPTION: "异常处置",
    constants.PAGE_VERIFY: "账实核验",
    constants.PAGE_JUNCTION_CHECK: "联接检测",
    constants.PAGE_CLEANUP: "清理与删除",
    constants.PAGE_PURGED: "已清账管理",

    constants.PAGE_MIGRATION: "换机迁移",
    constants.PAGE_RESCUE: "恢复旧版本",
    constants.PAGE_SHARE_LIST: "分享清单",
    constants.PAGE_STATS: "统计",
    constants.PAGE_API_KEY: "Steam API 密钥",
    constants.PAGE_SETTINGS: "设置",
    constants.PAGE_QUICK_CMD: "快速命令查询",
    constants.PAGE_BROWSER_TABS: "从浏览器取网址"
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
    # 分步向导升到第二位：新用户开荒路径（首次使用 → 加入 mod →
    # 日常更新）是最高频的引导需求，不该沉底
    ("分步向导", [constants.PAGE_FIRST_USE, constants.PAGE_ADD_MOD,
                  constants.PAGE_DAILY_UPDATE, constants.PAGE_GAME_EXIT,
                  constants.PAGE_MIGRATION, constants.PAGE_RESCUE,
                  constants.PAGE_UNINSTALL]),
    ("工作台", [constants.PAGE_MOD_LIST, constants.PAGE_ACCOUNT_CENTER, constants.PAGE_UPDATE_CHECK,
                constants.PAGE_UPDATE_COMPARE, constants.PAGE_BROWSER_TABS]),

    ("下载与备份", [constants.PAGE_COMMAND_GEN, constants.PAGE_QUICK_CMD,
                    constants.PAGE_BACKUP, constants.PAGE_BACKUP_OVERVIEW]),

    ("检测与异常", [constants.PAGE_TITLE_CHECK, constants.PAGE_REMOTE_HEALTH,
                    constants.PAGE_DEP_CHECK, constants.PAGE_EXCEPTION]),
    ("清理与账务", [constants.PAGE_VERIFY, constants.PAGE_JUNCTION_CHECK,
                    constants.PAGE_CLEANUP, constants.PAGE_PURGED]),
    ("档案与工具", [constants.PAGE_SHARE_LIST, constants.PAGE_STATS,
                    constants.PAGE_API_KEY, constants.PAGE_SETTINGS]),
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
    ③ constants 的 28 页全部入树、无遗漏。"""
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
        # 更新确认清单（备份轮）：非模态工作台，引用挂住防 GC；
        # _pending_triggers = 弹清单时的触发值暂存，随执行开批带给
        # 批次收尾的 D4 分支（清单关闭即清）
        self._update_dialog: UpdateSelectDialog | None = None
        self._pending_triggers: dict | None = None
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
        self._switcher.overview_requested.connect(self._on_switcher_overview)

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
        # mod 库页【备份选中项】（信号已留好；V2 的 mod 库页还没挂
        # 触发它的菜单项——先接线，落点就绪，谁发谁到）
        if hasattr(modlib, "backup_requested"):
            modlib.backup_requested.connect(self._on_backup_requested)
        # mod 库页右键「获取下载命令…」→ 命令生成页并聚焦（核验页/
        # 异常页同一落点：跳页 + focus_ids，缺聚焦能力时只跳页）
        if hasattr(modlib, "command_gen_requested"):
            modlib.command_gen_requested.connect(self._on_verify_command_gen)



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

    def _on_updates_found(self, app_id: int, updated_ids: list[int],
                          triggers: dict) -> None:
        """日常更新一条龙（D26 + D3①）：更新检测落库后，把「确有新
        版本」的编号连同触发值（mod_id → 检测到的远端 time_updated）
        交到这里。两条路：

        - 自动路（检测页勾了「发现更新后自动开始下载」）：不询问
          直接开批，一律不带备份——无人值守的批次不夹带备份阶段
          （版本未知条目会被备份守卫剔出批次，违背"自动下载"的
          直觉）；要备份，关掉自动勾选，走人工路。
        - 人工路：弹非模态「更新确认清单」——勾选条目、选动作
          （备份+更新 / 仅更新）、可分批反复执行。触发值随批携带
          （批次收尾按 D4 分支写确认基准，R18：绝不写高于触发值的数）。
        """
        if not updated_ids:
            return
        auto = self._settings.get_int("auto_download_after_check", 0) != 0
        if auto:
            # ——自动路（D16 语义原样：直接下载）——
            if self._batch_ctrl.is_busy():
                self._log.warn(
                    f"更新下载未开始：已有批次在跑——这 {len(updated_ids)} 个"
                    "编号已在账本，批次结束后到 mod 库页勾选下载即可")
                return
            ok = self._batch_ctrl.start_batch(
                app_id, updated_ids, backup_first=False,
                trigger_versions=triggers)
            if ok:
                self._console.show_batch_tab()  # 受理即切到批次页看进度
                self._forward_daily_updates(app_id, len(updated_ids), True)

            # start_batch False 时拒绝原因已进运行日志
            return
        # ——人工路：弹更新确认清单（备份轮点亮：⑳「备份并下载」
        #    由清单里选「备份+更新」动作承担）——
        # 非模态三件套之一：同刻至多一份，先关旧的再开新的（旧清单
        # 未执行的勾选随之作废，日志说一声）
        if self._update_dialog is not None:
            self._update_dialog.close()  # close 触发 finished → 引用清理
            self._update_dialog = None
            self._log.info("上一份更新清单被新结果替换（未执行的勾选已作废）")
        # 清单要 mod 整行（标题/版本/大小/链接）。逐个现取：检测刚
        # 写完库，取到的就是最新字段；顺序 = 检测报告顺序 = 库内顺序
        mods = []
        for mid in updated_ids:
            m = self._repo.get_mod(mid)
            if m is not None:
                mods.append(m)
        if not mods:
            return  # 编号都在而行都不在：账本被动过——不弹空清单
        # 触发值暂存到清单关闭为止：分批执行时每批都带全量，
        # 控制器按本批编号取用（多余键无害）
        self._pending_triggers = dict(triggers)
        self._forward_daily_updates(app_id, len(mods), False)

        dlg = UpdateSelectDialog(mods, app_id, parent=self)
        dlg.execute_requested.connect(self._on_update_execute_requested)
        dlg.finished.connect(self._on_update_dialog_finished)
        self._update_dialog = dlg
        dlg.show()
        self._log.info(
            f"检测到 {len(mods)} 个 mod 有新版本——更新确认清单已打开"
            "（勾选条目、选动作、点【执行选中】）")
    def _forward_daily_updates(self, app_id: int, n: int, auto: bool) -> None:
        """把「确有新版本」的结论同步给日常更新模块页的②卡。只在
        两条路真正走得通时调用（见 _on_updates_found 里的两处调用点）；
        hasattr 守卫：页面分支回退占位器时静默跳过。"""
        daily = self._pages.get(constants.PAGE_DAILY_UPDATE)
        forward = getattr(daily, "on_updates_found", None)
        if callable(forward):
            forward(app_id, n, auto)

    def _on_update_execute_requested(self, app_id: int, action: str,
                                     mod_ids: list[int]) -> None:
        """清单【执行选中】的落点（受理回执制）：开批成功 →
        mark_executed（留痕 + 清勾选，清单还开着可继续操作剩下的）；
        没成功 → notify_not_started（勾选原样保留，修好原因再点）。
        「备份+更新」= backup_first=True：备份阶段逐条先备份当前
        版本再下载；本地版本未确认的条目会被备份守卫整条剔出批次
        （清单行上有预告）——先到 mod 库页右键【设定本地版本…】。"""
        dlg = self._update_dialog
        if dlg is None:
            return  # 理论到不了：信号只会来自存活的清单
        # 档案一致性保险丝：清单存活期间档案不该被切（切档案即关
        # 清单），这里再校一道——对不上就拒绝，绝不往错档案开批
        if self._current_game is None or self._current_game.app_id != app_id:
            dlg.notify_not_started(
                "清单归属的档案已不是当前档案——请关掉清单重新检测")
            return
        if self._batch_ctrl.is_busy():
            dlg.notify_not_started(
                "已有批次在跑（底部控制台·下载批次）——等它结束再点执行")
            return
        ok = self._batch_ctrl.start_batch(
            app_id, mod_ids,
            backup_first=(action == UpdateSelectDialog.ACT_BACKUP),
            trigger_versions=self._pending_triggers or {})
        if ok:
            dlg.mark_executed(action, mod_ids)
            # 不强制切页：清单是工作台，可能还要继续勾下一组；进度
            # 在底部控制台·「下载批次」，想看随时切
        else:
            # 拒绝原因控制器已写进运行日志；清单给常见指引即可
            dlg.notify_not_started(
                "常见原因 = steamcmd 未启动/未登录、下载目录未设置"
                "——详见底部控制台·运行日志")

    def _on_update_dialog_finished(self, _result: int) -> None:
        """清单关闭（点【完成】/点 X/切档案时被关）的统一收尾：清
        引用与触发值暂存。close() 也走这里（QDialog 关闭即发
        finished），所以无论哪种关法引用都不会悬着。"""
        self._update_dialog = None
        self._pending_triggers = None

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
        if pid == constants.PAGE_UPDATE_CHECK:
            page = UpdateCheckPage(self._repo, self._settings,
                                   parent=self._stack, log=self._log)
            page.updates_found.connect(self._on_updates_found)
            return page
        if pid == constants.PAGE_BACKUP:
            return BackupPage(self._repo, self._settings,
                              parent=self._stack, log=self._log)
        if pid == constants.PAGE_BACKUP_OVERVIEW:
            return BackupOverviewPage(self._repo, self._settings,
                                      parent=self._stack, log=self._log)
        if pid == constants.PAGE_UPDATE_COMPARE:
            return UpdateComparePage(self._repo, parent=self._stack,
                                     log=self._log)
        if pid == constants.PAGE_COMMAND_GEN:
            page = CommandGenPage(self._repo, self._settings,
                                  parent=self._stack, log=self._log)
            # 「快速命令查询…」按钮 → 跳独立页（同组相邻，衔接自然）
            page.quick_command_requested.connect(
                lambda: self._goto_quick_cmd())
            return page

        if pid == constants.PAGE_STATS:
            return StatsPage(self._repo, parent=self._stack)
        if pid == constants.PAGE_SETTINGS:
            return SettingsPage(self._settings, parent=self._stack)
        if pid == constants.PAGE_VERIFY:
            page = VerifyPage(self._repo, self._settings,
                              parent=self._stack, log=self._log)
            page.command_gen_requested.connect(self._on_verify_command_gen)
            return page
        if pid == constants.PAGE_JUNCTION_CHECK:
            page = JunctionCheckPage(self._repo, self._settings, parent=self._stack, log=self._log)
            # 联接检测写完档案后，档案切换器手里的档案信息还停在写库前
            # ——不同步的话，"编辑档案"等从它取值的窗口会一直显示旧值，
            # 直到重启。这里接住通知，让切换器悄悄重读一次（不发全页
            # 广播：广播会清空检测页刚出的绿勾结论，体验反而变差）。
            page.game_dir_written.connect(
                lambda _app_id: self._switcher.reload(broadcast=False))
            return page

        if pid == constants.PAGE_CLEANUP:
            page = DeletePage(self._repo, self._settings,
                              parent=self._stack, log=self._log)
            page.goto_account_center_requested.connect(
                lambda: self._goto_page(constants.PAGE_ACCOUNT_CENTER))


            page.ledger_changed.connect(self._on_cleanup_ledger_changed)
            return page
        if pid == constants.PAGE_PURGED:
            return PurgedPage(self._repo, parent=self._stack,
                              log=self._log, settings=self._settings)

        if pid == constants.PAGE_SHARE_LIST:
            page = ShareListPage(self._repo, self._settings,
                                 parent=self._stack, log=self._log)
            page.share_out_requested.connect(self._export_sharepack)
            page.share_in_requested.connect(self._import_sharepack)
            page.open_update_requested.connect(
                lambda: self._goto_page(constants.PAGE_UPDATE_CHECK))
            return page
        if pid == constants.PAGE_API_KEY:
            return ApiKeyPage(self._settings, parent=self._stack,
                              log=self._log)
        if pid == constants.PAGE_TITLE_CHECK:
            return TitleCheckPage(self._repo, self._settings,
                                  parent=self._stack, log=self._log)
        if pid == constants.PAGE_REMOTE_HEALTH:
            return RemoteHealthPage(self._repo, self._settings,
                                    parent=self._stack, log=self._log)
        if pid == constants.PAGE_DEP_CHECK:
            return DepCheckPage(self._repo, self._settings,
                                parent=self._stack, log=self._log)
        if pid == constants.PAGE_EXCEPTION:
            page = ExceptionPage(self._repo, self._settings,
                                 parent=self._stack, log=self._log)
            # 与核验页同一个跳命令页落点（切页 + 只勾这些）
            page.command_gen_requested.connect(self._on_verify_command_gen)
            return page

        if pid == constants.PAGE_DAILY_UPDATE:
            # 日常更新页装配：检测页的三条信号直连进来（本页是第二块
            # 表盘，不复制检测逻辑）；开始检测按钮转主窗口落点。
            # 检测页编号(3)小于本页(6)，循环装到本页时它必已在 _pages
            page = DailyUpdatePage(self._repo, self._settings,
                                   parent=self._stack, log=self._log)
            check_page = self._pages.get(constants.PAGE_UPDATE_CHECK)
            if hasattr(check_page, "progress_changed"):
                check_page.progress_changed.connect(page.on_check_progress)
            if hasattr(check_page, "check_interrupted"):
                check_page.check_interrupted.connect(page.on_check_interrupted)
            if hasattr(check_page, "checks_finished"):
                check_page.checks_finished.connect(page.on_check_done)
            page.check_requested.connect(self._on_daily_check_requested)
            return page
        if pid == constants.PAGE_MIGRATION:
            # 换机迁移页：七个转调信号逐一落位——①②转既有导出/导入，
            # ③⑥a 跳页，④⑥b 转档案切换器的对话框，⑦转入账中心盘点
            page = MigrationPage(self._repo, self._settings,
                                 parent=self._stack, log=self._log)
            page.export_requested.connect(self._export_ledger)
            page.import_requested.connect(self._import_ledger)
            page.open_settings_requested.connect(
                lambda: self._goto_page(constants.PAGE_SETTINGS))
            page.open_edit_requested.connect(self._switcher.open_edit)
            # 与「游戏」菜单的连接指引同一落点：联接检测页接管
            page.open_link_requested.connect(
                lambda: self._goto_page(constants.PAGE_JUNCTION_CHECK))
            page.open_relocate_requested.connect(self._switcher.open_relocate)
            page.rescan_requested.connect(self._on_module_rescan_requested)
            return page
        if pid == constants.PAGE_RESCUE:
            # 恢复旧版本页：②跳备份页；③转接入账中心盘点认领
            page = RescuePage(self._repo, self._settings,
                              parent=self._stack, log=self._log)
            page.go_backup_requested.connect(
                lambda: self._goto_page(constants.PAGE_BACKUP))
            page.rescan_requested.connect(self._on_module_rescan_requested)
            return page
        if pid == constants.PAGE_UNINSTALL:
            # 卸载与清理页：三卡页内自理（盘点/会话/注册表都是只读），
            # 开软件目录转主窗口既有落点（与文件菜单同一入口）
            page = UninstallPage(self._repo, self._settings,
                                 parent=self._stack, log=self._log)
            page.open_software_dir_requested.connect(self._open_software_dir)
            return page
        if pid == constants.PAGE_ADD_MOD:
            # 加入新 mod 页：开批下载转批次控制器（与 mod 库页
            # 【下载选中项】同一落点）；解析/入库/命令/盘点全在
            # 页内调引擎，主窗口只接这一条跨页信号
            page = AddModPage(self._repo, self._settings,
                              parent=self._stack, log=self._log)
            page.download_requested.connect(self._on_download_requested)
            page.quick_command_requested.connect(self._goto_quick_cmd)
            page.open_browser_picker.connect(self._on_open_browser_picker)

            return page
        if pid == constants.PAGE_GAME_EXIT:
            # 游戏退场页：四个转调信号落位——①两条导出、②备份总览
            # 按档案过滤跳页（删除对话框同一落点）、⑤删除档案对话框
            # （「游戏」菜单同一份）。refresh/shutdown 由既有钩子
            # 循环自动发现，零接线
            page = GameExitPage(self._repo, self._settings,
                                parent=self._stack, log=self._log)
            page.share_out_requested.connect(self._export_sharepack)
            page.ledger_export_requested.connect(self._export_ledger)
            page.overview_requested.connect(self._on_switcher_overview)
            page.delete_archive_requested.connect(self._switcher.open_delete)
            return page
        if pid == constants.PAGE_FIRST_USE:
            # 首次使用向导：三个转调信号落位——设置页跳转、建档对话框
            # （与左上角「＋添加」同一份）、联接检测页（V2 落点：原连接
            # 指引对话框退役，与「游戏」菜单连接指引同一跳页落点）。
            # refresh 由进页钩子自动发现，零额外接线
            page = FirstUsePage(self._repo, self._settings,
                                parent=self._stack, log=self._log)
            page.settings_requested.connect(
                lambda: self._goto_page(constants.PAGE_SETTINGS))
            page.add_game_requested.connect(self._switcher.add_game_dialog)
            page.link_guide_requested.connect(
                lambda: self._goto_page(constants.PAGE_JUNCTION_CHECK))
            # ⑤ 步三跳（录入已有 mod 文件链）：认领 / 设基准 / 首轮检测
            page.goto_account_center.connect(
                lambda: self._goto_page(constants.PAGE_ACCOUNT_CENTER))
            page.goto_mod_list.connect(
                lambda: self._goto_page(constants.PAGE_MOD_LIST))
            page.goto_daily_update.connect(
                lambda: self._goto_page(constants.PAGE_DAILY_UPDATE))
            return page

        if pid == constants.PAGE_BROWSER_TABS:
            page = BrowserTabPage(parent=self._stack, log=self._log)
            # 独立页【送到「加入新 mod」】：清单交模块②自动填入并
            # 解析预览，随即跳页（模块②的页内选择器入口按 V2 原计划
            # "采集器迁入后恢复"，见改善项池，本轮不接）
            page.handoff_to_addmod.connect(self._on_browser_handoff)
            return page

        if pid == constants.PAGE_QUICK_CMD:
            return QuickCommandPage(self._repo, self._settings,
                                    parent=self._stack, log=self._log)

        return PlaceholderPage(_PAGE_TITLES[pid], pid)

    def _build_menus(self) -> None:

        m_file = self.menuBar().addMenu("文件(&F)")
        # —— 账本导出/导入 + 分享包两项（dataExporter 消费轮回填）。
        # 导出纯读不需确认；导入完整账本 = 清库重灌，破坏性确认 + 默认
        # 按钮"否"双保险 + 批次进行中直接拒绝；分享包导入纯增量
        # （已有 mod 跳过、现有数据一字不改），无需确认 ——
        act_exp_ledger = QAction("导出完整账本…", self)
        act_exp_ledger.setToolTip(
            "把全部档案与全部 mod 记录（含快照、备份登记、操作日志、"
            "判决史等 8 张表）导出为一个 JSON 文件，用于换机迁移 / "
            "整机备份")
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
        # act_link_guide = QAction("连接指引…", self)
        # act_link_guide.setToolTip(
        #     "打开「联接检测」页：判定游戏 mod 目录与下载目录的联接状态，"
        #     "按步骤接通（原连接指引对话框的页面版，能力一致）")
        # act_link_guide.triggered.connect(
        #     lambda: self._goto_page(constants.PAGE_JUNCTION_CHECK))
        # m_game.addAction(act_link_guide)
        act_edit_game = QAction("编辑档案…", self)
        act_edit_game.setToolTip(
            "编辑当前游戏档案（如显示名称）——保存成功后下拉框与"
            "各页同步更新")
        act_edit_game.triggered.connect(self._switcher.open_edit)
        m_game.addAction(act_edit_game)
        self._act_game_edit = act_edit_game

        act_relocate = QAction("备份目录重定位…", self)
        act_relocate.setToolTip(
            "备份文件夹被搬动/改名后，把当前档案的备份目录指针指回"
            "文件真正所在的位置——先预演命中率，确认后才写库"
            "（只改档案的备份目录一个字段，逐条备份记录零改动）")
        act_relocate.triggered.connect(self._switcher.open_relocate)
        m_game.addAction(act_relocate)
        self._act_game_relocate = act_relocate

        act_del_game = QAction("删除档案…", self)
        act_del_game.setToolTip(
            "删除当前游戏档案：先盘点名下账目，确认后自动做数据库"
            "快照兜底、一个事务清账；磁盘备份目录按勾选处理（默认保留）")
        act_del_game.triggered.connect(self._switcher.open_delete)
        m_game.addAction(act_del_game)
        self._act_game_delete = act_del_game

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
            # 进页刷新钩子（备份总览轮起启用）：页面自带 refresh()
            # 就调——总览页是跨档案大盘，在备份页做过备份/删除再进
            # 总览，不刷新看到的还是旧账；hasattr 守卫，谁有谁被调
            page = self._pages.get(pid)
            refresher = getattr(page, "refresh", None)
            if callable(refresher):
                refresher()

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
    def _on_daily_check_requested(self) -> None:
        """日常更新页【开始检测】的落点：转调更新检测页的后台检测
        （不跳页——检测页的三条信号已直连日常页，过程两页同见）。
        start_check 返回非空串 = 未受理（拒绝原因），返回空串 = 已
        受理——让日常页进度条先进忙态。hasattr 守卫：检测页分支万一
        回退占位器，只日志说一声，不炸。"""
        page = self._pages.get(constants.PAGE_UPDATE_CHECK)
        fn = getattr(page, "start_check", None)
        if not callable(fn):
            self._log.warn("更新检测页尚未就绪，无法开始检测")
            return
        reason = fn()
        if reason:
            self._log.warn(f"更新检测未开始：{reason}")
            return
        daily = self._pages.get(constants.PAGE_DAILY_UPDATE)
        started = getattr(daily, "on_check_started", None)
        if callable(started):
            started()

    def _on_module_rescan_requested(self) -> None:
        """模块页「盘点确认」（换机迁移⑦、恢复旧版本③）的落点：
        判决制下本地状态的对账唯一正门是入账中心的【扫描游戏目录】
        ——跳转过去并代点一次。页面缺该入口（理论不可达）时降级为
        只跳页，绝不炸。"""
        page = self._pages.get(constants.PAGE_ACCOUNT_CENTER)
        fn = getattr(page, "start_inventory_scan", None)
        self._goto_page(constants.PAGE_ACCOUNT_CENTER)
        if callable(fn):
            fn()
        else:
            self._log.info("请在本页点【扫描游戏目录】完成盘点确认")
    def _on_browser_handoff(self, urls: list) -> None:
        """独立页【送到「加入新 mod」】：清单交模块②的
        receive_external_lines（自动填入并解析预览），随即跳页。"""
        page = self._pages.get(constants.PAGE_ADD_MOD)
        receiver = getattr(page, "receive_external_lines", None)
        if not callable(receiver):
            self._log.warn("加入新 mod 页未就绪，网址未送出——可在"
                           "原页复制网址后到【入账中心 · 登记】")
            return
        receiver(urls)
        self._goto_page(constants.PAGE_ADD_MOD)

    def _on_open_browser_picker(self) -> None:
        """模块②【从浏览器取标签页…】的落点：弹页内选择器（内嵌
        同一个 BrowserTabPage，零复制）。勾选送回 = receive_external_
        lines 自动填入并解析预览，对话框即关——人本来就在模块②，
        全程无跳页（独立页的跳页方案与之并存，两条入口同一份代码）。"""
        page = self._pages.get(constants.PAGE_ADD_MOD)
        receiver = getattr(page, "receive_external_lines", None)
        if not callable(receiver):
            self._log.warn("加入新 mod 页未就绪，浏览器选择器未打开")
            return
        dlg = BrowserPickDialog(self, log=self._log)
        dlg.lines_picked.connect(receiver)
        dlg.exec()

    def _goto_quick_cmd(self, text: str = "") -> None:
        """跳到【快速命令查询】页，可选预填输入框（命令生成页按钮、
        加入新 mod 页【核验命令可行性…】的统一落点）。文本只填不跑
        ——查询仍由用户亲手点（与对话框时代的防呆一致）。"""
        self._goto_page(constants.PAGE_QUICK_CMD)
        page = self._pages.get(constants.PAGE_QUICK_CMD)
        setter = getattr(page, "set_input_text", None)
        if callable(setter) and text:
            setter(text)

    def _on_verify_command_gen(self, mod_ids: list[int]) -> None:
        """核验页修复三选「重新下载」：跳命令生成页并聚焦勾选。
        命令生成页没有 focus_ids 能力时只跳页不聚焦，绝不炸。"""
        self._goto_page(constants.PAGE_COMMAND_GEN)
        page = self._pages.get(constants.PAGE_COMMAND_GEN)
        focus = getattr(page, "focus_ids", None)
        if callable(focus):
            focus(mod_ids)

    def _on_cleanup_ledger_changed(self) -> None:
        """清理页处置落账 → mod 库页强制重读（set_game 重载整表）。
        只动 mod 库页，不走 _on_game_changed——那会顺手关掉更新/确认
        清单，处置落账不该连坐。"""
        if self._current_game is None:
            return
        page = self._pages.get(constants.PAGE_MOD_LIST)
        setter = getattr(page, "set_game", None)
        if callable(setter):
            setter(self._current_game)
    def _on_switcher_overview(self, app_id: int) -> None:
        """删除对话框「先去备份总览看看」的落点：先按档案设过滤再
        跳页——进页 refresh 钩子按新过滤刷新；总览页没有
        set_filter_game 时只跳页（hasattr 自适应，与广播循环同哲学）。"""
        page = self._pages.get(constants.PAGE_BACKUP_OVERVIEW)
        setter = getattr(page, "set_filter_game", None)
        if callable(setter):
            setter(app_id)
        self._goto_page(constants.PAGE_BACKUP_OVERVIEW)

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
        self._act_share_out.setEnabled(game is not None)  # 分享包按当前档案导出

        self._act_open_download.setEnabled(
            game is not None and bool(game.download_dir))
        self._act_game_edit.setEnabled(game is not None)
        self._act_game_relocate.setEnabled(game is not None)
        self._act_game_delete.setEnabled(game is not None)

        # 收尾确认清单跟着档案走：切档案即关（非模态三件套之二）。
        # 待确认队列按档案过滤，新档案的清单由下一次批次收尾再弹
        if self._confirm_dialog is not None:
            self._confirm_dialog.close()
            self._confirm_dialog = None
        # 更新确认清单同款处理（非模态三件套之二）：批次归属 = 清单
        # 打开那一刻的档案，档案能切但归属不能跟着变——干脆关掉重开
        if self._update_dialog is not None:
            self._update_dialog.close()
            self._update_dialog = None
        self._pending_triggers = None

    # ---------- 账本导出/导入 + 分享包（dataExporter 消费）----------
    # 全程主线程：导出/导入都是一轮 SQL（毫秒级），与"数据库操作全在
    # 主线程"的既有口径一致，不需要工作线程。

    def _ask_open_json(self, title: str) -> str | None:
        """选一个 JSON 文件；取消返回 None。起始目录固定为数据目录——
        便携模式下数据跟着软件走，从数据目录起步最可预期。"""
        path, _ = QFileDialog.getOpenFileName(
            self, title, str(appPaths.data_dir()), "JSON 文件 (*.json)")
        return path or None
    def _refresh_switcher_after_import(self) -> None:
        """账本/分享包导入后的档案下拉刷新：档案切换器重读 games 表
        （当前选择尽量保持；当前档案没了就落到剩余第一项并广播，
        各页自动重读导入后的新账）。"""
        self._switcher.reload()

    def _export_ledger(self) -> None:
        """文件 → 导出完整账本…：8 张表全量 → JSON。先在内存构建
        payload（纯读操作），再用它生成带时间戳的建议文件名弹保存框；
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
        """文件 → 导入完整账本…：清库重灌（引擎拍板，不做合并）。
        失败路径全部安全：load 的格式问题在动手前被拦下；import_ledger
        整体事务，任何失败回滚后原账无损——弹窗把这一点说清，别让
        用户以为账本被弄坏了。批次进行中拒绝：清库会跟批次收尾链
        互相踩脚，等一等没有坏处。"""
        if self._batch_ctrl.is_busy():
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
            dataExporter.ledger_summary(payload)
            + "\n\n当前账本将被整体替换，此操作无法撤销。\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)   # 破坏性操作：默认停在"否"
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
        self._refresh_switcher_after_import()
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
        except ValueError as exc:   # 理论不可达：档案来自同一 repo
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
        """文件 → 导入分享包…：增量并入——已有的 mod 跳过、现有数据
        一字不改，风险低，不做确认弹窗，结果报数。TypeError 也在拦的
        行列：分享包的 schema 闸靠 Game(**row)/Mod(**row) 的构造
        （dataExporter 文件头说明），形状不对会从这里冒出来，弹窗兜住
        照样"原账无损"。"""
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
        self._refresh_switcher_after_import()
        game = self._repo.get_game(report["app_id"])
        name = game.name if game else str(report["app_id"])
        elsewhere = (self._current_game is None
                     or self._current_game.app_id != report["app_id"])
        self._log.ok(f"分享包导入完成：「{name}」新增 {report['added']} 条、"
                     f"跳过 {report['skipped']} 条")
        self.statusBar().showMessage(
            f"分享包导入完成：「{name}」新增 {report['added']} 条、"
            f"跳过 {report['skipped']} 条"
            f"（导入条目均为「待下载」；本机已有文件的话，到【入账中心】"
            "点【扫描游戏目录】摆成待认领、确认补版本）"
            + ("；在左上角下拉切换到该游戏查看" if elsewhere else ""),
            8000)


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
    def _on_backup_requested(self, app_id: int, mod_ids: list) -> None:
        """mod 库页【备份选中项】的落点：转备份页执行并跳过去。
        备份页的 backup_ids_for 会自己切到对应档案、过滤出可备份
        （已下载）的 mod 再开批次——用户在 A 游戏的库页勾选、备份页
        正停在 B 游戏也不会备错家。"""
        page = self._pages.get(constants.PAGE_BACKUP)
        fn = getattr(page, "backup_ids_for", None)
        if callable(fn):
            fn(app_id, mod_ids)
            self._goto_page(constants.PAGE_BACKUP)

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
