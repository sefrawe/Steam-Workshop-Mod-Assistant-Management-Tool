"""mod 库页
"""

r"""后端调试主战场。
布局：顶部筛选条（搜索 / 状态 / 特别关注 / 排序下拉 /统计见统计页/
下载 / 备份选中项；"刷新"与"扫描本地"收进【刷新 ▾】下拉）+ 水平分割
（左表格 / 右详情）。

高级筛选（T12）：菜单栏顶级项「高级筛选(S)」弹出非模态对话框，与
顶栏筛选叠加生效；条件生效期间工具条出现"高级筛选 ✕"指示，点它
一键清空。

右键菜单：repo 已支持的直接可用；"获取下载命令"发信号给 MainWindow
跳到命令生成页并聚焦该 mod；"手动备份"已点亮——调 core.backupManager
把该 mod 的下载内容复制进备份区（单 mod 手动备份；批量备份、恢复、
钉住、删除等归备份管理页 M3）。

操作▾（T27）：设为/取消特别关注（勾选行整批）、批量特别关注…
（贴清单对话框，解析走 urlParser 单源）。

分工与边界（记事本架构约定）：
- 只通过 ModRepository 接口读写，GUI 层零 SQL；
- 查询条件（搜索词 / 状态筛选 / 排序）由本页持有，模型只管展示，
  模型不回头反问查询条件；
- "扫描本地"直接调 core.localScanner 且同步执行——本地只是读一个
  小文本文件，毫秒级完成，不值得为它开线程；
- "手动备份"同样同步执行（已知限制）：引擎内部要写库，repo 的连接
  有线程亲缘性不能跨线程共用，线程化需要给引擎配独立连接，
  归 M3 备份管理页一并做。GB 级 mod 备份期间界面会冻几十秒，
  备份前有日志预告。
- 扫描的定位钥匙是设置页的 steamcmd 程序路径（决策 21）：
  工坊内容与账本文件（acf）都在 steamcmd 自己管理的目录树里，
  按 core/steamPaths.py 的公式推导，不再让用户手填 Steam 库路径。
  扫描前顺带核对档案下载目录是否因 steamcmd 挪位失效
  （失效则重推导并写回，见 _on_scan_local 内注释）。

T19⑯⑰⑱ 一批：
- 列显隐开关在设置页（_COL_SETTING），编号与标题两列恒显示；
  每次刷新重读设置，改完保存、回本页点【刷新】即生效，无需重启；
- 排序唯一入口 = 工具条下拉（决策 99 撤销表头点击排序）；列表当前
  顺序 = 下载/备份批次的执行顺序；
- 【刷新 ▾】= 主按钮刷新（重读数据库）+ 小箭头弹菜单（扫描本地），
  腾出的位置给统计条；统计条 = 档案全体概览，不受搜索/筛选影响
  （筛选结果数仍是右侧"共 N 个 mod"）。

T19⑤ 收官（v2.29）：会话记忆补全——左表格/右详情分割比例、各列
列宽（标题列是伸展列，宽度=剩余空间，不单独存）、排序状态（列+方向，
恢复前过 _SORT_MAP 白名单校验，白名单变更时旧值自动作废回默认）。
连同 MainWindow 侧的导航组折叠，T19⑤ 至此全部收口。键都住 QSettings
的 session/ 命名空间（会话状态≠用户配置，与面板显隐同款口径）。

T15 批 1 顺手修复（v2.29）：无档案时操作菜单的入口从"静默没反应"
改为弹窗指路（与批量特别关注同一口径）；切档案显式清详情面板
（不依赖模型重置碰巧发来的 currentRowChanged）；文件头两处"控制台
菜单"旧文案校准为顶级菜单（决策 63 修订后没跟上的尾巴）。
"""
from gui.modFolderOpener import open_mod_folder
from pathlib import Path
from gui.advancedSearchDialog import AdvancedSearchDialog
from gui.batchSpecialDialog import BatchSpecialDialog
from PySide6.QtCore import QModelIndex, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSplitter, QTableView, QToolButton, QVBoxLayout, QWidget,
    QDialog,
)
from core import localScanner, steamPaths
from core.appSettings import AppSettings
from core.backupManager import BackupManager, steamcmd_running
from core.models import Game
from gui.manualConfirm import confirm_one
from gui.consolePanel import LogBus
from gui.formatters import fmt_size
from gui.modDetailPanel import ModDetailPanel
from gui.modListModel import (_SORT_MAP, COLOR_CHOICES, ModListModel,
                              normalize_color_tag)
# 颜色标记：色表唯一源在 modListModel.COLOR_CHOICES（v2.25 上收），
# 这里留旧名做本地别名——右键菜单与颜色筛选都从它取
_COLORS = COLOR_CHOICES

# 列显隐开关（T19⑯）：设置键 → 列号。勾选/编号/标题三列是批量操作
# 与主键/标题的结构列，不提供开关（编号与标题永远显示）
_COL_SETTING = {
    3: "mod_col_status",
    4: "mod_col_remote_ver",
    5: "mod_col_local_ver",
    6: "mod_col_update",
    7: "mod_col_size",
    8: "mod_col_tags",
    9: "mod_col_special",
    10: "mod_col_note",
}

# 排序下拉（T19⑰）的措辞：列号 → 中文名 / 方向说法。
# 排序串本身一律走 modListModel._SORT_MAP 白名单（防注入单源），这里
# 只负责把白名单条目翻译成人话；方向布尔 = 排序串是否以 DESC 结尾
_SORT_COL_NAME = {1: "编号", 2: "标题", 3: "状态", 4: "远端版本",
                  5: "本地版本", 7: "大小", 9: "特别关注"}
_SORT_DIR_LABEL = {
    (1, False): "小→大", (1, True): "大→小",
    (2, False): "A→Z", (2, True): "Z→A",
    (4, False): "旧→新", (4, True): "新→旧",
    (7, True): "大→小",
    (3, False): "升序", (3, True): "降序",
    (5, False): "旧→新", (5, True): "新→旧",
    (9, True): "关注的在前",
}

# ---- 会话记忆键（T19⑤）：QSettings 而非 AppSettings——会话状态≠用户
# 配置：记住"上次窗口关掉时什么样"，不进设置页、不进 GlobalSettings.json，
# 与 MainWindow 的 _SES_* 同一套口径（session/ 命名空间）。
_SES_SPLIT = "session/modlib_split"      # 左表格/右详情分割比例："左宽,右宽"
_SES_SORT = "session/modlib_sort"        # 排序状态：/col 列号、/order_by 排序串
_SES_COLW = "session/modlib_colwidths"   # 列宽：/列号 → 像素（标题列除外）


class ModListPage(QWidget):
    # 右键"获取下载命令"时发出，参数 = 要聚焦的 mod id 列表；MainWindow 负责跳转
    command_gen_requested = Signal(list)

    # 【下载选中项】（T20b）：参数 = (当前游戏 app_id, 勾选的 mod_id 列表)。
    # MainWindow 接线到批量下载控制器；本页不认识控制器，只发信号
    download_requested = Signal(int, list)

    # 【备份选中项】：参数 = (当前游戏 app_id, 勾选的 mod_id 列表)。
    # MainWindow 接线到备份页的 backup_ids_for——与 download_requested
    # 同一套信号模式，勾选列一处投入、下载/备份两处收益
    backup_requested = Signal(int, list)

    # 高级筛选对话框【到 mod 库查看结果】→ MainWindow 跳到本页。
    # 对话框的信号在本页转一手（与 command_gen_requested 同款契约）
    advanced_results_requested = Signal()

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo      # ModRepository：读 mod 清单，写备注/标记/状态
        self._settings = settings  # AppSettings：读 steamcmd 程序路径（扫描本地用）
        # 日志总线：MainWindow 注入；单独调试本页时兜底建一个
        # （消息没人显示但不崩，"发进空气 = 无操作"），所以后面可以放心直呼
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._search = ""  # 当前生效的搜索词（防抖后更新）
        # 排序状态（T19⑤）：启动时从会话记忆恢复；恢复值先过白名单校验，
        # 不合法回默认——历史遗留值绝不直接进查询
        self._order_by, self._sort_col = self._load_sort()
        self._selected_mod_id: int | None = None  # 重新载入后恢复选中用

        # 高级筛选对话框（T12）：非模态、页面持有实例防 GC（决策 36⑦
        # 同款——局部变量 + show() 出作用域会把包装回收，经典闪退）。
        # 必须先于 _build_ui 创建：工具条上的"生效指示"按钮要连它的
        # 构造即创建不显示。关窗即清空条件（对话框 done() 统一漏斗，
        # Esc / X / 关闭按钮三路全覆盖），重开为干净状态。
        # 条件防抖后广播 → 本页 _reload 重查；_reload 里再喂标签清单给
        # 对话框（set_available_tags 内部 blockSignals，不会回环触发）
        self._adv_dialog = AdvancedSearchDialog(self, settings=self._settings)
        self._adv_dialog.conditions_changed.connect(self._reload)
        # 对话框要跳页：转成页面级信号发出去，跳转归 MainWindow
        self._adv_dialog.go_to_results.connect(
            self.advanced_results_requested.emit)

        self._build_ui()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---- 顶部筛选条 ----
        bar = QWidget(self)
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 6)

        row = QWidget(bar)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)

        # 搜索范围下拉（决策 99，用户 todo）：决定搜索框查哪个字段。
        # 全部 = 标题+备注合查（原行为，走 SQL 的 search 参数）；
        # 标题/备注/编号 = 页面内存过滤（repo 契约零改动——与颜色
        # 筛选同一哲学）；编号 = 完整编号精确匹配
        self._search_field_combo = QComboBox(row)
        for label, val in (("全部", "all"), ("标题", "title"),
                           ("备注", "note"), ("编号", "id")):
            self._search_field_combo.addItem(label, val)
        self._search_field_combo.setToolTip(
            "搜索框查什么：\n"
            "全部：标题和备注都查（默认，原行为）；\n"
            "标题 / 备注：只查对应字段；\n"
            "编号：按完整编号精确匹配（输非数字 = 0 命中）")
        self._search_field_combo.currentIndexChanged.connect(
            self._on_search_field_changed)
        h.addWidget(self._search_field_combo)
        self._search_edit = QLineEdit(row)
        self._search_edit.setPlaceholderText("搜索标题 / 备注…")
        self._search_edit.setClearButtonEnabled(True)
        h.addWidget(self._search_edit, 1)

        # 状态下拉：界面显示中文，currentData 里存库内原始值，
        # 查询时直接把 currentData 递给 repo，不用做"中文 → 状态值"翻译
        self._status_combo = QComboBox(row)
        for label, val in (
                ("全部", None),
                ("已收录", "tracked"),
                ("已下载", "downloaded"),
                ("已删除", "deleted"),
                ("已失败", "failed"),
        ):
            self._status_combo.addItem(label, val)
        h.addWidget(self._status_combo)

        # 颜色筛选下拉（v2.25）：与状态下拉同款模式——显示中文名，
        # currentData 存比对值（None=全部 / "none"=无标记 / hex）。
        # 颜色不是库查询条件（repo 无此参数），在页面层对取回的行做
        # 内存过滤——几百行量级成本可忽略；归一化与表格渲染共用
        # normalize_color_tag 单源，库里遗留脏值的行也能按归一后的
        # 颜色筛到
        # 颜色筛选四态（v2.26 按拍板重构）：不筛选=不过滤（默认）；
        # 全部颜色=只看上过色的（排除"—"无标记行）；无标记=只看"—"；
        # 具体颜色=只看该色。显示中文名，currentData 存比对值
        self._color_combo = QComboBox(row)
        self._color_combo.addItem("不筛选", None)
        self._color_combo.addItem("全部颜色", "any")
        self._color_combo.addItem("无标记", "none")
        for _name, _hex in _COLORS.items():
            self._color_combo.addItem(_name, _hex)
        self._color_combo.setToolTip(
            "不筛选：不做颜色过滤；\n"
            "全部颜色：只看上过色的条目（排除\"—\"）；\n"
            "无标记：只看没上色（\"—\"）的条目；\n"
            "选具体颜色：只看该颜色的条目")
        self._color_combo.currentIndexChanged.connect(self._reload)
        h.addWidget(self._color_combo)

        self._special_check = QCheckBox("特别关注", row)
        h.addWidget(self._special_check)

        # 排序下拉（T19⑰）：与表头点击同一套白名单（_SORT_MAP），两者
        # 双向同步；列表当前顺序也是下载/备份批次的执行顺序
        self._sort_combo = QComboBox(row)
        for col in (1, 2, 3, 4, 5, 7, 9):
            first, second = _SORT_MAP[col]
            for order_by in (first, second):
                if order_by is None:
                    continue
                is_desc = order_by.upper().endswith(" DESC")
                self._sort_combo.addItem(
                    f"按{_SORT_COL_NAME[col]}排（{_SORT_DIR_LABEL[(col, is_desc)]}）",
                    (col, order_by))
        self._sort_combo.setToolTip(
            "排序方式（唯一排序入口）。\n"
            "下载/备份选中项按列表当前顺序执行")

        idx = self._sort_combo.findData((self._sort_col, self._order_by))
        self._sort_combo.setCurrentIndex(max(idx, 0))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_combo)
        h.addWidget(self._sort_combo)

        # 刷新 ▾（T19⑱）：点主按钮 = 刷新（重读数据库）；点右侧小箭头
        # 弹出菜单 = 扫描本地。原来两颗按钮收成一颗，位置让给统计条
        self._btn_refresh = QToolButton(row)
        self._btn_refresh.setText("刷新")
        self._btn_refresh.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_refresh.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._btn_refresh.setToolTip(
            "重新从数据库加载当前清单（点主按钮）。\n"
            "右侧小箭头 → 扫描本地：解析当前游戏的 appworkshop acf，"
            "回填本地版本三件套")
        refresh_menu = QMenu(self._btn_refresh)
        act_scan = QAction("扫描本地", refresh_menu)
        act_scan.setToolTip("解析当前游戏的 appworkshop acf，回填本地版本三件套；"
                            "可重复执行，已入库的只更新、不会重复登记")

        act_scan.triggered.connect(self._on_scan_local)
        refresh_menu.addAction(act_scan)
        # QMenu 的动作 tooltip 默认不显示（T19⑩ 同款坑），显式打开
        refresh_menu.setToolTipsVisible(True)
        self._btn_refresh.setMenu(refresh_menu)
        self._btn_refresh.clicked.connect(self._reload)
        h.addWidget(self._btn_refresh)

        # 统计条已撤（T19⑲）：全景数字搬去「统计」页，工具条只留筛选结果数

        # 操作 ▾（T19⑳）：下载/备份选中项收进下拉——继续瘦身。
        # InstantPopup = 点按钮就弹菜单（主按钮无默认动作）
        self._btn_actions = QToolButton(row)
        self._btn_actions.setText("操作 ▾")
        self._btn_actions.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_actions.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._actions_menu = QMenu(self._btn_actions)
        self._actions_menu.setMinimumWidth(240)  # T19①：中文菜单项在部分字体下宽度测量偏窄，硬性兜底
        # 全选/清空（用户 todo）：只作用当前显示的 mod——批量操作
        # 的"只对看得见的条目生效"纪律在模型层天然成立
        act_sel_all = QAction("全选当前显示", self._actions_menu)
        act_sel_all.setToolTip(
            "勾选当前筛选结果里的全部 mod（被搜索/筛选藏起来的不算）")
        act_sel_all.triggered.connect(
            lambda: self._model.set_all_checked(True))
        self._actions_menu.addAction(act_sel_all)
        act_sel_none = QAction("清除全部勾选", self._actions_menu)
        act_sel_none.setToolTip("取消本页所有勾选")
        act_sel_none.triggered.connect(
            lambda: self._model.set_all_checked(False))
        self._actions_menu.addAction(act_sel_none)
        self._actions_menu.addSeparator()

        self._act_download = QAction("下载选中项", self._actions_menu)
        self._act_download.setToolTip(
            "把勾选的 mod 交给 控制台 → 下载批次 逐条下载"
            "（顺序 = 列表当前排序）：\n需要 steamcmd 已在终端里启动并登录，"
            "未启动时会提示")
        self._act_download.triggered.connect(self._on_download_checked)
        self._actions_menu.addAction(self._act_download)

        self._act_backup = QAction("备份选中项", self._actions_menu)
        self._act_backup.setToolTip(
            "把勾选的 mod 内容复制进备份区（未下载的自动跳过）；\n"
            "保留份数与配额按设置页执行")
        self._act_backup.triggered.connect(self._on_backup_checked)
        self._actions_menu.addAction(self._act_backup)
        # 软删除选中项（决策 101）：对勾选行整批软删除——单条版在
        # 右键菜单；批量一次确认（软删除可恢复，不必逐条问）
        self._act_softdel = QAction("软删除选中项", self._actions_menu)
        self._act_softdel.setToolTip(
            "把勾选的 mod 整批标记为已删除：记录与「删除前快照」保留、"
            "随时可恢复；磁盘上一个文件都不动。\n"
            "已是删除状态的条目自动跳过")
        self._act_softdel.triggered.connect(self._on_softdel_checked)
        self._actions_menu.addAction(self._act_softdel)
        # 彻底清账选中项（右键单条版的批量形态）：对勾选行整批
        # 物理删除账本记录。「已下载」条目不收——它的完整出口
        # （删文件+清账/仅删文件）在「清理与删除」页，那里才有
        # 文件处置选项；这里只管账本侧的终结。
        self._act_purge = QAction("彻底清账选中项", self._actions_menu)
        self._act_purge.setToolTip(
            "把勾选 mod 的记录从账本里物理删除：记录、版本快照、"
            "特别关注提醒一并清除，不可恢复。\n"
            "磁盘上一个文件都不碰；自动登记「已清账」黑名单，"
            "今后扫描不再录入。\n"
            "「已下载」状态的条目会被跳过——它们的完整出口在"
            "「清理与删除」页（那里才有删文件的选项）")
        self._act_purge.triggered.connect(self._on_purge_checked)
        self._actions_menu.addAction(self._act_purge)

        # —— 批量特别关注（T27）——
        # 设/取消对勾选行整批写；批量入口贴清单（T27a）。菜单项文字带勾选数，
        # 由 _update_checked_label 同步（与下载/备份同款）
        self._act_special_on = QAction("设为特别关注", self._actions_menu)
        self._act_special_on.setToolTip(
            "把勾选的 mod 全部标上特别关注：更新检测会对它们记更新提醒"
            "（同一版本只提醒一次）")
        self._act_special_on.triggered.connect(
            lambda: self._set_special_checked(True))
        self._actions_menu.addAction(self._act_special_on)

        self._act_special_off = QAction("取消特别关注", self._actions_menu)
        self._act_special_off.setToolTip("把勾选的 mod 的特别关注全部取消")
        self._act_special_off.triggered.connect(
            lambda: self._set_special_checked(False))
        self._actions_menu.addAction(self._act_special_off)

        self._act_batch_special = QAction("批量特别关注…", self._actions_menu)
        self._act_batch_special.setToolTip(
            "贴一份清单（工坊网址 / 纯数字均可），解析后对当前档案对表，"
            "把已在库、还没标特别关注的条目一次标上")
        self._act_batch_special.triggered.connect(self._on_batch_special)
        self._actions_menu.addAction(self._act_batch_special)
        # 批量颜色标记（v2.49）：勾选行整批上色/清除——单条版右键有、
        # 批量版一直缺的最后一块，与批量特别关注同一纪律
        self._act_batch_color = QAction("批量颜色标记…", self._actions_menu)
        self._act_batch_color.setToolTip(
            "给勾选的 mod 整批设置同一种颜色标记（或清除标记）：\n"
            "只收当前清单里显示的勾选；颜色随时可改可清")
        self._act_batch_color.triggered.connect(self._on_batch_color)
        self._actions_menu.addAction(self._act_batch_color)
        # 批量编辑备注（维护版）：三模式 = 开头插入 / 结尾插入 / 替换
        # （替换留空 = 清空，危险档另有确认）。单条版在右键菜单
        self._act_batch_note = QAction("批量编辑备注…", self._actions_menu)
        self._act_batch_note.setToolTip(
            "对勾选的 mod 整批编辑备注：在现有备注的开头或结尾插入一段"
            "文字（不动原内容），或整批替换/清空。\n"
            "只收当前清单里显示的勾选")
        self._act_batch_note.triggered.connect(self._on_batch_note)
        self._actions_menu.addAction(self._act_batch_note)



        self._act_open_pages = QAction("打开工坊页面", self._actions_menu)
        self._act_open_pages.setToolTip(
            "把勾选 mod 的工坊页面逐个在默认浏览器打开（每个一页标签），"
            "便于逐页去点订阅；开前有确认弹窗报数")
        self._act_open_pages.triggered.connect(self._on_open_pages_checked)
        self._actions_menu.addAction(self._act_open_pages)

        self._act_open_pages_list = QAction("打开工坊页面（贴清单）…", self._actions_menu)
        self._act_open_pages_list.setToolTip(
            "粘贴网址/编号清单批量打开工坊页面——面向“手里只有清单”的订阅"
            "场景，未收录的编号照样能开；先预览再打开，全程不改账本")
        self._act_open_pages_list.triggered.connect(self._on_open_pages_paste)
        self._actions_menu.addAction(self._act_open_pages_list)
        # 复制勾选编号（小件）：每行一个进剪贴板——可直接贴给
        # 网址批量导入 / 快速命令查询 / 任何别的工具
        self._act_copy_ids = QAction("复制勾选编号", self._actions_menu)
        self._act_copy_ids.setToolTip(
            "把勾选 mod 的工坊编号逐行复制进剪贴板（每行一个）："
            "可直接贴进【网址批量导入】、快速命令查询或任何别的工具")
        self._act_copy_ids.triggered.connect(self._on_copy_ids_checked)
        self._actions_menu.addAction(self._act_copy_ids)

        # 菜单动作 tooltip 默认不显示，显式打开（T19⑩）
        self._actions_menu.setToolTipsVisible(True)
        self._btn_actions.setMenu(self._actions_menu)
        self._btn_actions.setToolTip(
            "对第一列勾选的 mod 批量操作；菜单项文字带当前勾选数")
        h.addWidget(self._btn_actions)

        self._count_label = QLabel("", row)
        h.addWidget(self._count_label)
        self._checked_label = QLabel("", row)
        h.addWidget(self._checked_label)

        # 高级筛选生效指示（T12）：条件生效期间才出现；
        # 点击 = 一键清空高级条件（clear_all 内部会广播刷新，
        # 本按钮随 _reload 自动隐藏）。悬浮显示条件摘要——
        # 不打开对话框也知道现在筛着什么
        self._adv_indicator = QToolButton(row)
        self._adv_indicator.setText("高级筛选 ✕")
        self._adv_indicator.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._adv_indicator.setToolTip("高级筛选条件生效中；点击全部清除")
        self._adv_indicator.clicked.connect(self._adv_dialog.clear_all)
        self._adv_indicator.setVisible(False)
        h.addWidget(self._adv_indicator)

        bar_layout.addWidget(row)
        root.addWidget(bar)

        # ---- 左表格 / 右详情 ----
        split = QSplitter(Qt.Orientation.Horizontal, self)
        self._table = QTableView(split)
        self._model = ModListModel(self._table)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        # 斑马纹（v2.27）：关闭 view 级交替行——qdarktheme 的 QSS 会在
        # 交替行盖掉模型背景（颜色标记奇偶位显隐的元凶）；斑马纹改由
        # 模型在 BackgroundRole 返回调色板色，观感不变、样式插不了手
        self._table.setAlternatingRowColors(False)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)  # 标题列占满剩余宽度
        # 列宽（T19⑤）：上次会话用户拖过就恢复，没记录用默认值。
        # 标题列是伸展列，宽度=总宽-其余列-分割比例，随窗口自适应——
        # 它不需要单独的记忆键，其他列宽+分割比例恢复了它自然跟着对上
        for col, width in ((0, 34), (1, 90), (3, 96), (4, 92), (5, 92),
                           (6, 72), (7, 88), (8, 170), (9, 32), (10, 160)):
            self._table.setColumnWidth(col, self._saved_col_width(col, width))
        # 排序指示器初值跟真实方向走（T19⑤：恢复的排序可能是升序），
        # 与 _on_header_clicked 里"箭头跟排序串方向"同一纪律
        # 表头点击排序已撤（决策 99，用户拍板）：点表头不改排序、
        # 下拉不跟着变的错位从此不存在——排序唯一入口 = 工具条下拉。
        # 指示箭头一并隐藏：留着会邀请点击，点了没反应（界面三问）。
        # 启动恢复的排序（_load_sort）照旧生效，只是不再显示箭头
        header.setSortIndicatorShown(False)
        header.setSectionsClickable(False)

        # 列宽落盘（T19⑤）：拖动期间 sectionResized 连发，用单发定时器
        # 合并成一次写（与搜索防抖同款思路）；启动恢复阶段的
        # setColumnWidth 不触发它（接线在设值之后），不会启动即写盘
        self._colw_save_timer = QTimer(self)
        self._colw_save_timer.setSingleShot(True)
        self._colw_save_timer.setInterval(400)
        self._colw_save_timer.timeout.connect(self._save_col_widths)
        header.sectionResized.connect(self._on_col_resized)
        self._apply_col_visibility()

        self._detail = ModDetailPanel(split)
        split.addWidget(self._table)
        split.addWidget(self._detail)
        # 分割比例（T19⑤）：恢复上次会话；拖完即落盘（splitterMoved
        # 只在拖动结束时发一次，不需要防抖）
        self._split = split  # 会话记忆要用（局部变量出不了这个方法）
        split.setSizes(self._load_split_sizes())
        split.splitterMoved.connect(lambda *_a: self._save_split_sizes())
        root.addWidget(split, 1)

        # ---- 信号 ----
        # 搜索防抖：每敲一个字都查库太浪费，停手 300ms 再查
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_edit.textChanged.connect(self._search_timer.start)
        self._status_combo.currentIndexChanged.connect(self._reload)
        self._special_check.toggled.connect(self._reload)

        self._table.selectionModel().currentRowChanged.connect(self._on_current_row)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # 勾选数变化 → "已勾选 N" 标签（模型发信号，本页只更新文字）
        self._model.checks_changed.connect(self._update_checked_label)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        # 切档案必须同时清掉旧选中：不清的话，下面的恢复选中逻辑
        # 会把上一个档案的 mod 详情带进新档案的界面，看着像串档。
        # 同档案的强制重载（导入后/检测后 set_game 当前档案）不清——
        # 用户正看着的详情不该被无谓抹掉，_reload 的恢复选中接手
        same_game = (self._game is not None and game is not None
                     and self._game.app_id == game.app_id)
        self._game = game
        if not same_game:
            self._selected_mod_id = None
            # 显式清详情（T15 批 1）：不等模型重置"碰巧"发来的
            # currentRowChanged——清不清不该押在 Qt 的实现细节上
            self._detail.set_mod(None)
        self._reload()

    def set_detail_visible(self, visible: bool) -> None:
        """显示/隐藏右侧详情面板（T19⑮）。视图菜单的接线归 MainWindow
        （待其原文到手后安装）；本页只负责自己的布局——QSplitter 不给
        隐藏的子控件分配空间，表格自动占满整行。"""
        self._detail.setVisible(visible)
    def _on_search_field_changed(self, _index: int) -> None:
        """搜索范围切换（决策 99）：换占位文案 + 立即按新范围重查。
        已输入的搜索词保留——换的是"查哪"，不是"查什么"。"""
        field = self._search_field_combo.currentData()
        self._search_edit.setPlaceholderText({
                                                 "all": "搜索标题 / 备注…",
                                                 "title": "搜索标题包含…",
                                                 "note": "搜索备注包含…",
                                                 "id": "输入完整编号（精确匹配）…",
                                             }[field])
        self._reload()

    # ---------- 查询 ----------
    def _apply_search(self) -> None:
        self._search = self._search_edit.text().strip()
        self._reload()

    def _reload(self) -> None:
        if self._game is None:
            self._model.set_rows([])
            self._count_label.setText("请先在左上角选择游戏档案")
            self._adv_dialog.set_hit_count(None)  # 没档案就没有"命中"可言
            return

        # 搜索范围分流（决策 99）：「全部」走 SQL 合查（原行为）；
        # 标题/备注/编号三档不下发 search=——SQL 合查会把只命中
        # 单侧的词先筛掉，内存过滤就无从谈起，所以这里必须放空，
        # 过滤挪到下方页面内存做
        search_field = self._search_field_combo.currentData()
        rows = self._repo.list_mods(
            self._game.app_id,
            status=self._status_combo.currentData(),
            special_only=self._special_check.isChecked(),
            search=(self._search or None) if search_field == "all" else None,
            order_by=self._order_by,
            **self._advanced_kwargs(),
        )

        # 颜色筛选（v2.25）：放在取数之后、其余一切（标签池 / 计数 /
        # 高级筛选命中数 / 喂表）之前——"共 N 个"与命中数看到的都是
        # 筛后口径，与状态/搜索下拉同一漏斗
        # 诊断（v2.26）：库里"非空但无法归一"的颜色值正常应为 0 条；
        # 大于 0 说明还有没见过的遗留格式，报数定位（UI 没接就落文件）
        _bad = [m.mod_id for m in rows
                if m.color_tag and normalize_color_tag(m.color_tag) is None]
        if _bad:
            self._log.warn(f"颜色标记无法识别 {len(_bad)} 条"
                           f"（按无标记显示）：{_bad[:10]}")

        # 颜色筛选四态（v2.26）：归一化与表格渲染共用 normalize 单源，
        # 拍平后的色值（不透明小写 hex）与下拉里的比对值直接相等
        _color_sel = self._color_combo.currentData()
        if _color_sel == "any":
            rows = [m for m in rows
                    if normalize_color_tag(m.color_tag) is not None]
        elif _color_sel == "none":
            rows = [m for m in rows if normalize_color_tag(m.color_tag) is None]
        elif _color_sel is not None:
            rows = [m for m in rows
                    if normalize_color_tag(m.color_tag) == _color_sel]

        # 搜索范围三档的内存过滤（决策 99；"全部"已在 SQL 合查，不进这里）。
        # 放在颜色筛选之后、标签池/命中数之前——"共 N 个"与高级筛选
        # 命中数看到的都是最终口径
        if search_field == "title":
            _q = self._search.casefold()
            rows = [m for m in rows if _q in (m.title or "").casefold()]
        elif search_field == "note":
            _q = self._search.casefold()
            rows = [m for m in rows if _q in (m.note or "").casefold()]
        elif search_field == "id":
            # 编号 = 精确匹配完整编号；非纯数字输入 = 0 命中
            # （占位文案已提示"输入完整编号"）
            _q = self._search.strip()
            rows = ([m for m in rows if str(m.mod_id) == _q]
                    if _q.isdigit() else [])

        # 作者 / 本地版本时间（v2.43）：高级筛选的"内存侧"条件——
        # repo 契约不动，取数后 Python 端过滤（颜色筛选同一哲学）。
        # 放在颜色筛选之后、标签池/命中数之前：它们看到的都是最终口径。
        # conditions() 是纯读，下方 adv_now 再取一次结果相同，无害
        _adv = self._adv_dialog.conditions()
        if _adv is not None:
            if _adv.creator_id:
                # 作者存的是上传者 SteamID64（更新检测从工坊 API 取回）；
                # str() 兜底防库里存成整数时 .strip() 炸掉
                rows = [m for m in rows
                        if str(m.creator or "").strip() == _adv.creator_id]
            if _adv.local_from is not None:
                rows = [m for m in rows if m.local_timeupdated is not None
                        and m.local_timeupdated >= _adv.local_from]
            if _adv.local_to is not None:
                rows = [m for m in rows if m.local_timeupdated is not None
                        and m.local_timeupdated <= _adv.local_to]

        # 喂标签清单给对话框：来自当前显示的 mod 的并集；已勾选的
        # 标签对话框自己会保留（哪怕清单收窄也悄悄丢条件不发生）
        tag_pool = sorted({t for m in rows for t in (m.tags or [])})
        self._adv_dialog.set_available_tags(tag_pool)

        # 高级筛选生效指示：条件生效才显示，悬浮给出人话摘要
        adv_now = self._adv_dialog.conditions()
        self._adv_indicator.setVisible(adv_now is not None)
        if adv_now is not None:
            self._adv_indicator.setToolTip(
                "高级筛选生效中，点击全部清除：\n" + adv_now.describe())

        # 命中数回填给对话框（rows 已含高级条件的过滤结果）
        self._adv_dialog.set_hit_count(
            len(rows) if adv_now is not None else None)

        self._model.set_rows(rows)
        self._count_label.setText(f"共 {len(rows)} 个 mod")
        # 每次刷新重读列开关：设置页改完保存，回来点【刷新】即生效
        self._apply_col_visibility()

        # 恢复选中：右键操作后的 reload 都会走到这里，
        # 之前选中的 mod 还在清单里就重新选上，详情面板不闪空
        if self._selected_mod_id is not None:
            for i, m in enumerate(rows):
                if m.mod_id == self._selected_mod_id:
                    self._table.selectRow(i)
                    break

    # ---------- 列显隐与统计（T19⑯ / T19⑱） ----------

    # ---------- 高级筛选（T12，决策 63） ----------
    def _advanced_kwargs(self) -> dict:
        """把对话框当前条件摊平成 list_mods 的关键字参数。
        全空（conditions() 返回 None）→ 空 dict：查询路径与没有
        高级筛选时完全一致。core 层不 import 对话框类——GUI→repo
        单向依赖，"翻译"住在本页，不倒挂。"""
        f = self._adv_dialog.conditions()
        if f is None:
            return {}
        return {
            "title_contains": f.title_contains,
            "note_contains": f.note_contains,
            "mod_id": f.mod_id,
            "size_min": f.size_min,
            "size_max": f.size_max,
            "updated_from": f.updated_from,
            "updated_to": f.updated_to,
            "tags_all": list(f.tags_all) or None,
        }

    def open_advanced_search(self) -> None:
        """菜单栏顶级项「高级筛选(&S)」的落点：非模态弹出——
        show() 不 exec()，开着不影响主窗口继续操作（边查边改）。
        已开着时置前，不重开双份。"""
        self._adv_dialog.show()
        self._adv_dialog.raise_()
        self._adv_dialog.activateWindow()

    def _apply_col_visibility(self) -> None:
        """按设置页的列开关显隐（T19⑯）。值 "0" = 隐藏；
        缺键/其他值 = 显示（默认全开）。视图级 setColumnHidden，
        模型列号不受影响，勾选/排序照常。"""
        for col, key in _COL_SETTING.items():
            self._table.setColumnHidden(col, self._settings.get(key) == "0")

    # ---------- 会话记忆（T19⑤）：QSettings 而非 AppSettings ----------
    # 会话状态 ≠ 用户配置：记住"上次窗口关掉时什么样"，重装即失，
    # 不进设置页、不进 AppSettings——与 MainWindow 的 _SES_* 同一套
    # 口径（键都住 session/ 命名空间；org/app 归属沿用 main 入口的
    # 设定，本页与 MainWindow 一样用裸 QSettings()，不自设）。

    @staticmethod
    def _load_sort() -> tuple[str, int]:
        """读上次会话的排序（排序串 + 列号）。白名单校验：排序串必须是
        _SORT_MAP 中该列登记过的串——版本升级白名单变了，旧值自动作废
        回默认（远端版本 新→旧），绝不让历史遗留值混进查询。"""
        q = QSettings()
        try:
            col = int(q.value(f"{_SES_SORT}/col", 4))
        except (TypeError, ValueError):
            col = 4
        order_by = str(q.value(f"{_SES_SORT}/order_by", "time_updated DESC"))
        pair = _SORT_MAP.get(col)
        if pair is not None and order_by in pair:
            return order_by, col
        return "time_updated DESC", 4

    def _save_sort(self) -> None:
        """排序状态落盘（_on_header_clicked / _on_sort_combo 收尾时调）。"""
        q = QSettings()
        q.setValue(f"{_SES_SORT}/col", self._sort_col)
        q.setValue(f"{_SES_SORT}/order_by", self._order_by)

    @staticmethod
    def _saved_col_width(col: int, default: int) -> int:
        """读某列的上次宽度；无记录/脏值/过窄 → 回默认。
        标题列不适用本函数（伸展列不存宽度，见 _save_col_widths）。"""
        q = QSettings()
        try:
            w = int(q.value(f"{_SES_COLW}/{col}", default))
        except (TypeError, ValueError):
            return default
        return w if w >= 20 else default  # 拖得只剩一条缝的当脏数据

    def _on_col_resized(self, _index: int, _old: int, _new: int) -> None:
        """列宽一变就重置落盘定时器：拖动连发合并成一次写（防抖）。"""
        self._colw_save_timer.start()

    def _save_col_widths(self) -> None:
        """全部非伸展列的当前宽度落盘（定时器到点调用）。
        标题列不存：伸展列宽度=总宽-其余列-分割比例，随窗口走，
        存了反而在不同窗口尺寸下打架。"""
        q = QSettings()
        for col in range(self._model.columnCount()):
            if col == 2:  # 标题列（伸展列）
                continue
            q.setValue(f"{_SES_COLW}/{col}", self._table.columnWidth(col))

    @staticmethod
    def _load_split_sizes() -> list[int]:
        """左表格/右详情的分割比例；无记录/格式坏 → 默认 820/380
        （旧版写死的同款比例，老用户无感）。"""
        q = QSettings()
        raw = q.value(_SES_SPLIT)
        if raw:
            try:
                sizes = [int(x) for x in str(raw).split(",")]
                if len(sizes) == 2 and all(s > 0 for s in sizes):
                    return sizes
            except ValueError:
                pass
        return [820, 380]

    def _save_split_sizes(self) -> None:
        """分割比例落盘（splitterMoved 直连，拖完才发一次）。"""
        q = QSettings()
        q.setValue(_SES_SPLIT, ",".join(str(s) for s in self._split.sizes()))

    # ---------- 槽 ----------


    def _on_sort_combo(self, index: int) -> None:
        """排序下拉（T19⑰）：（决策 99 起是唯一排序入口）——写 _order_by / _sort_col、
        同步表头箭头、重载。数据 = (列号, 排序串)，排序串源自 _SORT_MAP
        白名单，本方法不发明新排序串。"""
        data = self._sort_combo.itemData(index)
        if data is None:
            return
        col, order_by = data
        self._order_by = order_by
        self._sort_col = col
        order = (Qt.SortOrder.DescendingOrder
                 if order_by.upper().endswith(" DESC")
                 else Qt.SortOrder.AscendingOrder)
        self._table.horizontalHeader().setSortIndicator(col, order)
        self._save_sort()  # T19⑤：与表头点击同款落盘
        self._reload()


    def _on_current_row(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            m = self._model.mod_at(current.row())
            self._selected_mod_id = m.mod_id
            self._detail.set_mod(
                m, self._game.download_dir
                if self._game is not None else None)

        else:
            self._detail.set_mod(None)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        m = self._model.mod_at(index.row())
        menu = QMenu(self)
        menu.setMinimumWidth(240)  # T19①：同上，防"确认已下载（手动）…"被截
        # QMenu 的动作 tooltip 默认不显示（Qt 有意默认关）：右键说明要生效
        # 必须显式打开（T19⑩）——此前设过的 tooltip 其实一直没展示过
        menu.setToolTipsVisible(True)
        mid = m.mod_id

        # 打开工坊页面：链接理论上必有（导入时按模板生成），保险起见判一下
        act_open = QAction("打开工坊页面", menu)
        act_open.setToolTip(
            "在浏览器打开该 mod 的 Steam 创意工坊页面（链接来自导入时登记的网址）")
        act_open.setEnabled(bool(m.url))
        act_open.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(m.url)))
        menu.addAction(act_open)
        # 打开 mod 文件夹（v2.42 追补）：路径到点击时再解析——
        # 菜单构建阶段不碰磁盘；未下载过/已被清理时点击有说明弹窗
        act_folder = QAction("打开 mod 文件夹", menu)
        act_folder.setToolTip(
            "在文件管理器打开该 mod 的下载内容文件夹"
            "（steamcmd 工坊内容目录下以编号命名的文件夹）")
        act_folder.triggered.connect(lambda: self._open_mod_folder(m))
        menu.addAction(act_folder)
        act_copy = QAction("复制编号", menu)
        act_copy.setToolTip("把这个 mod 的工坊编号复制到剪贴板")
        act_copy.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act_copy)

        menu.addSeparator()

        for text, tip, cb in (
                ("编辑备注…",
                 "给自己看的备忘，只存本地数据库，与工坊无关",
                 lambda: self._edit_note(m)),
                ("颜色标记…",
                 "给条目着个色，方便扫一眼分类；重进本菜单可选「清除标记」",
                 lambda: self._pick_color(m)),
                ("切换特别关注",
                 "特别关注的 mod 出新版本时，更新检测会记一条提醒（同一版本只提醒一次）",
                 lambda: self._toggle_special(m)),
        ):
            act = QAction(text, menu)
            act.setToolTip(tip)
            act.triggered.connect(cb)
            menu.addAction(act)
        menu.addSeparator()

        # 跳到命令生成页，让它只勾选这个 mod（不带页面参数，交给 MainWindow 接线）
        # 手动确认入账（T18）：只对 tracked 开放——downloaded 已确认过，
        # deleted/failed 不许走此门（状态机单向，决策 20）
        act_confirm = QAction("确认已下载（手动）…", menu)
        act_confirm.setEnabled(m.status == "tracked")
        act_confirm.setToolTip(
            "不经 steamcmd 的手动下载（acf 永远无记录，扫描本地无法确认）："
            "确认后记为「已下载」，版本留空——备份将拒、更新检测列"
            "「版本未知」；重下并扫描本地可恢复")
        act_confirm.triggered.connect(lambda: self._manual_confirm(m))
        menu.addAction(act_confirm)

        act_cmdgen = QAction("获取下载命令…", menu)
        act_cmdgen.setToolTip(
            "跳到命令生成页并只勾选该 mod，生成可粘贴进 steamcmd 的下载命令")
        act_cmdgen.triggered.connect(
            lambda: self.command_gen_requested.emit([mid]))
        menu.addAction(act_cmdgen)

        act_backup = QAction("手动备份…", menu)
        act_backup.setToolTip(
            "把该 mod 的下载内容复制进备份区（robocopy）；"
            "保留份数与总配额按设置页执行")
        act_backup.triggered.connect(lambda: self._start_backup(m))
        menu.addAction(act_backup)
        menu.addSeparator()
        # 恢复（决策 69 回程票）：只对已软删除的条目出现。目标状态按
        # 事实回推：有本地版本记录 → downloaded（文件若已被清掉，下次
        # 盘点/核验会以「账有盘无」如实报出）；没有 → tracked
        if m.status == "deleted":
            act_restore = QAction("恢复（取消软删除）…", menu)
            act_restore.setToolTip(
                "把这条已删除记录放回正常管理：\n有本地版本记录 → 回到"
                "「已下载」（盘上文件缺了的话，盘点/核验会如实报出）；"
                "没有 → 回到「已收录」待确认")
            act_restore.triggered.connect(lambda: self._restore_deleted(m))
            menu.addAction(act_restore)

        act_del = QAction("软删除…", menu)
        act_del.setEnabled(m.status != "deleted")  # 已删除的不重复删
        act_del.setToolTip(
            "标记为已删除（记录与快照保留，可随时恢复）；"
            "删除后更新检测不再查询、命令生成不再列出")
        act_del.triggered.connect(lambda: self._soft_delete(m))
        menu.addAction(act_del)
        # 彻底清账（决策 106）：只对已删除/已收录开放——downloaded 的
        # 完整出口（删文件+清账/仅删文件）在【清理与删除】页，那里才有
        # 文件处置选项。这里的缝隙是"账上记着 deleted/tracked、盘上又
        # 没文件"的记录此前三个处置页都摸不到（v2.48 用户 todo）
        if m.status in ("deleted", "tracked"):
            act_purge = QAction("彻底清账（不可逆）…", menu)
            act_purge.setToolTip(
                "把这条记录从账本里物理删除：记录、版本快照、特别关注"
                "提醒一并清除，不可恢复。\n磁盘上的文件一个不碰"
                "（盘上还有内容目录的话，之后会以「孤儿目录」出现在"
                "【异常处理】页）。\n名下有备份登记时可勾选一并删除登记"
                "（磁盘备份文件夹仍保留）")
            act_purge.triggered.connect(lambda: self._purge_record(m))
            menu.addAction(act_purge)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ---------- 动作 ----------
    def _edit_note(self, m) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "编辑备注", f"mod {m.mod_id} 的备注：", m.note or "")
        if ok:
            # 空串 → None：备注清空等于"没有备注"，库里不留空串
            self._repo.set_note(m.mod_id, text.strip() or None)
            self._reload()

    def _open_mod_folder(self, m) -> None:
        """右键「打开 mod 文件夹」：实现单源 gui/modFolderOpener
        （与更新对照页共用，原镜像实现已收敛）。失败就地弹窗+日志。"""
        open_mod_folder(self, self._game, m, log=self._log)

    def _pick_color(self, m) -> None:
        names = [*_COLORS.keys(), "（清除标记）"]
        name, ok = QInputDialog.getItem(
            self, "颜色标记", f"mod {m.mod_id}：", names,
            current=0, editable=False)
        if ok:
            self._repo.set_color_tag(
                m.mod_id, None if name.startswith("（") else _COLORS[name])
            self._reload()

    def _toggle_special(self, m) -> None:
        self._repo.set_special(m.mod_id, not m.is_special)
        self._reload()

    def _soft_delete(self, m) -> None:
        ret = QMessageBox.question(
            self, "软删除",
            f"确定将「{m.title or m.mod_id}」标记为已删除？\n"
            "只动账本：记录与「删除前快照」保留，随时可恢复。\n"
            "硬盘不动：下载的内容文件夹、备份文件、备份登记全部原样。\n"
            "软删除后：更新检测不再查询它、下载命令生成页不再列出它、"
            "扫描本地也不再收录它；本页状态筛选选「已删除」仍能找到它。\n"
            "之后想腾空间：到「清理与删除」页盘点，可单独删它的内容"
            "文件夹（重新下载即可回来）；想恢复：本页右键「恢复」。\n"
            "（这里是可恢复的软删除；不可恢复的「彻底清账」在"
            "「清理与删除」页，执行前另有确认。）")

        if ret != QMessageBox.StandardButton.Yes:
            return
        last_state = self._last_state_of(m)

        self._repo.mark_deleted(m.mod_id, last_state)
        self._reload()
    @staticmethod
    def _last_state_of(m) -> dict:
        """软删除前快照的组装（单条右键 / 批量选中两路共用单源）。
        last_state 由调用方组装、repo 只管存取（repo 契约如此）；
        将来抽 flow 层时这段组装会一起搬走，GUI 目前代行。"""
        return {"title": m.title, "url": m.url,
                "time_updated": m.time_updated,
                "local_timeupdated": m.local_timeupdated,
                "manifest": m.manifest, "local_size": m.local_size,
                "note": m.note, "color_tag": m.color_tag,
                "is_special": m.is_special, "local_path": m.local_path}

    def _on_softdel_checked(self) -> None:
        """【软删除选中项】（决策 101）：对勾选的 mod 整批软删除。
        与下载/备份同一纪律：只收当前清单里显示的勾选；一次确认
        整批执行——软删除可恢复，不必逐条问；已删除的跳过（幂等，
        不重复写删除时间）。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "软删除选中项",
                "先在表格第一列勾选要软删除的 mod。")
            return
        targets = []
        skipped: list[int] = []
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue  # 勾选与落库之间刚被删（极端竞态）：跳过
            if m.status == "deleted":
                skipped.append(mid)
            else:
                targets.append(m)
        if not targets:
            QMessageBox.information(
                self, "软删除选中项",
                "勾选的条目都已处于「已删除」状态，没有可软删除的。")
            return
        ret = QMessageBox.question(
            self, "软删除选中项",
            f"确定将勾选的 {len(targets)} 个 mod 标记为已删除？\n"
            "只动账本：记录与「删除前快照」保留，随时可逐个恢复"
            "（本页状态筛选选「已删除」能找到它们，右键「恢复」）。\n"
            "硬盘不动：下载的内容文件夹、备份文件、备份登记全部原样。\n"
            "软删除后：更新检测不再查询、命令生成不再列出、扫描不再"
            "收录；想腾空间到「清理与删除」页盘点。\n"
            "（可恢复的软删除；不可恢复的「彻底清账」在清理与删除页。）")
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消批量软删除：没有做任何改动")
            return
        with self._repo.transaction():
            for m in targets:
                self._repo.mark_deleted(m.mod_id, self._last_state_of(m))
        self._log.ok(
            f"已软删除 {len(targets)} 个"
            + (f"（跳过已是删除状态 {len(skipped)} 个）" if skipped else ""))
        self._reload()
    def _on_purge_checked(self) -> None:
        """【彻底清账选中项】：对勾选的 mod 整批物理删除账本记录。
        与批量软删除同一纪律：只收当前清单里显示的勾选、一次确认
        整批执行。与软删除的执行差别要分清：
        - 软删除可恢复，敢整批包一个事务；本动作不可逆，执行时逐条
          走 repo.purge_mod 正门（每条自带独立事务），某一条被拒
          不影响已清的其他条目——物理删除不做"全有全无"。
        - 「已下载」条目刻意跳过：它的完整出口（删文件+清账/仅删文件）
          在「清理与删除」页，那里才有文件处置选项。
        名下有备份登记的条目：默认被引擎拒绝（备份去留必须先有决策），
        弹窗里勾选「同时删除备份登记」才连带删（磁盘备份文件夹仍保留，
        与右键单条同一口径）。被拒的逐条跳过、最后日志报数。
        """
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "彻底清账选中项",
                "先在表格第一列勾选要清账的 mod。")
            return
        # 逐个核对状态：只放行「已删除 / 已收录」（与右键单条同一口径）；
        # 「已下载」跳过并计数；勾选与执行之间刚被清过的（极端竞态）
        # get_mod 返回 None，自然跳过。
        targets = []
        skipped_downloaded = 0
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue
            if m.status == "downloaded":
                skipped_downloaded += 1
            else:
                targets.append(m)
        if not targets:
            QMessageBox.information(
                self, "彻底清账选中项",
                "勾选的条目没有可清账的（「已下载」状态的完整出口在"
                "「清理与删除」页）。")
            return
        # 确认弹窗：默认按钮 = 取消（默认永远保守）；复选框与右键
        # 单条同一句文案。
        box = QMessageBox(self)
        box.setWindowTitle("彻底清账选中项")
        box.setIcon(QMessageBox.Icon.Question)
        text = (
            f"确定将勾选的 {len(targets)} 个 mod 的记录彻底清除？\n\n"
            "· 记录、版本快照、特别关注提醒：物理删除，不可恢复；\n"
            "· 磁盘：一个字节不动——下载内容、备份文件夹全部原样"
            "（盘上还有内容目录的话，之后会以「孤儿目录」出现在"
            "【异常处理】页，可再处置）；\n"
            "· 自动登记「已清账」黑名单：今后扫描不会再把它们录入"
            "账本（想恢复收录，到「已清账」页允许录入）；\n"
            "· 失效归档与操作日志保留作历史证据。\n"
            "· 清账成功后自动从 steamcmd 工坊账本（acf）移除对应条目"
            "（防复活断根；steamcmd 正在运行时跳过，可稍后到"
            "「已清账管理」页执行）；\n"
        )
        if skipped_downloaded:
            text += (f"\n\n另有 {skipped_downloaded} 个「已下载」条目"
                     "已跳过——它们的完整出口（含删文件选项）在"
                     "「清理与删除」页。")
        box.setText(text)
        cb = QCheckBox("同时删除备份登记（磁盘上的备份文件夹仍保留）", box)
        cb.setToolTip("不勾：名下有备份登记的条目会被跳过（备份去留"
                      "必须先有决策）；\n勾：连带删除备份登记，"
                      "磁盘备份文件夹照旧保留")
        box.setCheckBox(cb)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        b_yes = box.addButton("彻底清账",
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_no)
        box.exec()
        if box.clickedButton() is not b_yes:
            self._log.info("已取消批量彻底清账：没有做任何改动")
            return
        # 逐条执行（每条独立事务）：被引擎的闸拒绝的跳过并报数，
        # 不让一条的失败挡住其余条目。
        done_ids = []
        blocked = []
        freed_regs = 0  # 连带删除的备份登记份数（日志用）
        for m in targets:
            try:
                n = self._repo.purge_mod(m.mod_id,
                                         purge_backups=cb.isChecked())
                done_ids.append(m.mod_id)
                freed_regs += n or 0
            except ValueError:
                # 引擎的保护性拒绝：名下有备份登记且没勾"同时删除"
                #（或条目刚被并发改动）——跳过，日志报数
                blocked.append(m.mod_id)
        self._log.warn(
            f"已彻底清账 {len(done_ids)} 个：记录物理删除、"
            "自动登记黑名单；磁盘文件未动"
            + (f"，连带删除备份登记 {freed_regs} 份" if freed_regs else "")
            + (f"；跳过 {len(blocked)} 个（名下有备份登记未一并删除——"
               "可到「清理与删除」页处置，或右键单条勾选后再来）"
               if blocked else ""))
        if skipped_downloaded:
            self._log.info(
                f"另有 {skipped_downloaded} 个「已下载」条目未清："
                "它们的完整出口在「清理与删除」页")
            self._acf_cleanup_after_purge(done_ids)  # 批量同样断根

        # 详情面板正显示的是被清条目 → 清掉（单条版同款收尾）
        if self._selected_mod_id in done_ids:
            self._selected_mod_id = None
        self._reload()
    def _acf_cleanup_after_purge(self, mod_ids: list[int]) -> None:
        """彻底清账成功后，把 steamcmd 工坊账本（acf）里的对应条目
        一并移除——复活的断根措施（黑名单只拦入账，拦不住 steamcmd
        反复校验装配已删条目、占着磁盘）。

        安全边界：
        - steamcmd 在跑 → 跳过（它退出时会把内存里的旧账本整个覆盖
          写回，现在改等于白改）；提示到【已清账管理】页补执行；
        - 没配 steamcmd / 该游戏没有 acf（从没下载过）→ 静默/说明跳过；
        - 移除实现（备份、两区块都删、解析失败不落笔）单源在
          core/localScanner.remove_items_from_acf。
        """
        if not mod_ids:
            return
        if steamcmd_running():
            self._log.warn(
                "steamcmd 正在运行：本次清账的条目暂未从工坊账本（acf）"
                "移除——运行中改它会在 steamcmd 退出时被整个覆盖回去；"
                "稍后到【已清账管理】页勾选对应条目执行"
                "「从 steamcmd 账本移除」")
            return
        root = steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))
        if root is None:
            self._log.warn("未设置 steamcmd 程序路径：acf 条目未清理"
                           "（可稍后到【已清账管理】页执行）")
            return
        game = self._game
        if game is None:
            return
        acf = localScanner.locate_acf(root, game.app_id)
        if acf is None:
            # 该游戏从没用本机 steamcmd 下载过：无账可清，不算异常
            return
        try:
            removed, _absent = localScanner.remove_items_from_acf(
                acf, mod_ids)
        except ValueError as exc:
            self._log.error(f"从工坊账本（acf）移除条目失败（条目未动）：{exc}")
            return
        if removed:
            self._log.ok(
                f"已从 steamcmd 工坊账本（acf）移除 {len(removed)} 个条目"
                "（复活彻底断根；原账本文件已自动备份在同目录）")

    def _copy_id(self, mid: int) -> None:
        """右键「复制编号」：剪贴板 + 日志回执（决策 22③ 口径）。"""
        QApplication.clipboard().setText(str(mid))
        self._log.info(f"已复制编号 {mid}")

    def _on_copy_ids_checked(self) -> None:
        """【复制勾选编号】：勾选的 mod 编号每行一个进剪贴板。
        与批量动作同一纪律：只收当前清单里显示的勾选。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "复制勾选编号", "先在表格第一列勾选要复制的 mod。")
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in ids) + "\n")
        self._log.ok(f"已复制 {len(ids)} 个编号（每行一个）")

    def _purge_record(self, m) -> None:
        """右键「彻底清账」（决策 106）：账本条目物理删除的终结出口，
        走 repo.purge_mod 正门。补齐的缝隙：状态 deleted/tracked 且
        盘上没文件的记录，此前清理页（只收盘上有目录的）、异常页
        桶④（只收失效归档/failed）、核验页（只对账 downloaded）都
        摸不到，唯一终点只能是永远躺在已删除筛选里。
        本动作只删账：failed_mods 归档与 operations_log 刻意存活
        （决策 69⑥）——异常页桶④会以「已清账」历史归档收档
        （决策 102/104 的闭环在这里接上）。"""
        box = QMessageBox(self)
        box.setWindowTitle("彻底清账")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"确定将「{m.title or m.mod_id}」（mod {m.mod_id}）的记录"
            "彻底清除？\n\n"
            "· 记录、版本快照、特别关注提醒：物理删除，不可恢复；\n"
            "· 磁盘：一个字节不动——下载内容、备份文件夹全部原样"
            "（盘上还有内容目录的话，之后会以「孤儿目录」出现在"
            "【异常处理】页，可再处置）；\n"
            "· 清账成功后自动把该条目从 steamcmd 的工坊账本（acf）里"
            "移除——steamcmd 从此不再校验装配它，复活彻底断根"
            "（原账本文件自动备份；steamcmd 正在运行时跳过，可稍后到"
            "【已清账管理】页执行）；\n"
            "· 失效归档与操作日志保留作历史证据（异常处理页状态筛"
            "「已清账」可查）。")

        cb = QCheckBox("同时删除备份登记（磁盘上的备份文件夹仍保留）", box)
        cb.setToolTip("不勾：备份登记保留，成为无主登记；\n"
                      "勾：只删登记，磁盘备份文件夹照旧保留")
        box.setCheckBox(cb)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        b_yes = box.addButton("彻底清账",
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_no)  # 默认永远保守（决策 69⒋）
        box.exec()
        if box.clickedButton() is not b_yes:
            self._log.info("已取消彻底清账：没有做任何改动")
            return
        try:
            n_backups = self._repo.purge_mod(
                m.mod_id, purge_backups=cb.isChecked())
        except ValueError as exc:
            # RESTRICT 闸：名下还有备份登记且没勾"同时删除"
            self._log.error(f"彻底清账失败（mod {m.mod_id}）：{exc}")
            QMessageBox.warning(
                self, "无法彻底清账",
                f"{exc}\n\n两条出路：\n"
                "① 回到刚才的弹窗勾选「同时删除备份登记」再来一次；\n"
                "② 到【清理与删除】页盘点处置（那里能连磁盘备份文件"
                "一起勾掉）。")
            return
        self._log.warn(
            f"mod {m.mod_id} 已彻底清账：记录物理删除"
            + (f"，随账清除备份登记 {n_backups} 份" if n_backups else "")
            + "；磁盘文件未动")
        self._acf_cleanup_after_purge([m.mod_id])  # 防复活断根（见方法注释）

        self._selected_mod_id = None  # 详情面板正显示的就是它，清掉
        self._reload()

    def _restore_deleted(self, m) -> None:
        """右键「恢复（取消软删除）」（决策 69 回程票的兑现项）。
        只动账面状态，不碰磁盘文件——文件在不在，交给盘点/核验如实
        报告。deleted_at 等末态字段保留原样：再次软删除时会被
        mark_deleted 整体覆盖，不影响任何判定。"""
        target = "downloaded" if m.local_timeupdated is not None else "tracked"
        label = "已下载" if target == "downloaded" else "已收录"
        ret = QMessageBox.question(
            self, "恢复记录",
            f"把「{m.title or m.mod_id}」恢复为正常条目？\n"
            f"记录将回到「{label}」；盘上文件不会被动，"
            "缺了的话盘点/核验会如实报出。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._repo.update_status(m.mod_id, target)
        self._reload()

    def _manual_confirm(self, m) -> None:
        """右键「确认已下载（手动）」（T18）：弹窗和写库都在共享 helper 里，
        确认成功后刷新列表（恢复选中逻辑 _reload 已有）。"""
        if confirm_one(self, self._repo, self._log, m.mod_id):
            self._reload()

    def _update_checked_label(self) -> None:
        """勾选数变化时更新"已勾选 N"（没有勾选就不显示），
        并把数字同步进操作菜单的菜单项——打开菜单前就有预期。"""
        n = self._model.checked_count_in_rows()
        self._checked_label.setText(f"已勾选 {n}" if n else "")
        label = f"（{n}）" if n else ""
        self._act_download.setText(f"下载选中项{label}")
        self._act_backup.setText(f"备份选中项{label}")
        self._act_softdel.setText(f"软删除选中项{label}")
        self._act_purge.setText(f"彻底清账选中项{label}")

        self._act_special_on.setText(f"设为特别关注{label}")
        self._act_special_off.setText(f"取消特别关注{label}")
        self._act_open_pages.setText(f"打开工坊页面{label}")
        self._act_copy_ids.setText(f"复制勾选编号{label}")
        self._act_batch_color.setText(f"批量颜色标记{label}")
        self._act_batch_note.setText(f"批量编辑备注{label}")




    def _require_game(self) -> bool:
        """操作菜单各入口的公共守卫（T15 批 1）：没选档案时弹窗指路，
        不再静默返回——按钮可点却毫无反应，等于界面三问的"我点了它
        理我吗"答不上来。口径与 _on_batch_special 的提示一致。"""
        if self._game is not None:
            return True
        QMessageBox.information(
            self, "请先选择档案",
            "这个操作按当前游戏档案执行——请先在左上角添加或选择游戏档案。")
        return False

    def _on_download_checked(self) -> None:
        """【下载选中项】：收集勾选的 mod，发信号让 MainWindow 开批次。
        只收集当前清单里仍显示的勾选——被筛选条件藏起来的不算数，
        点按钮的人看得见要下的是什么，不会发生"隐形下载"。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "下载选中项", "先在表格第一列勾选要下载的 mod。")
            return
        self.download_requested.emit(self._game.app_id, ids)

    def _on_backup_checked(self) -> None:
        """【备份选中项】：勾选的 mod 交给备份页开批次。
        与下载同一个纪律：只收当前清单里显示的勾选，不隐形备份。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "备份选中项", "先在表格第一列勾选要备份的 mod。")
            return
        self.backup_requested.emit(self._game.app_id, ids)

    def _set_special_checked(self, value: bool) -> None:
        """【设为/取消特别关注】（T27b）：对第一列勾选的行整批写。
        与下载/备份同一纪律：只收当前清单里显示的勾选，不隐形操作。
        对已是目标状态的条目重复写一遍无害（幂等），不值得先逐个查。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "设为特别关注" if value else "取消特别关注",
                "先在表格第一列勾选要操作的 mod。")
            return
        with self._repo.transaction():
            for mid in ids:
                self._repo.set_special(mid, value)
        self._log.ok(("已设为特别关注" if value else "已取消特别关注")
                     + f"：{len(ids)} 个")
        self._reload()

    def _on_batch_special(self) -> None:
        """【批量特别关注…】（T27a）：弹对话框，应用成功后写日志 + 刷新。
        对话框内部自带防错档（对表锁当前档案）、五桶预览与确认弹窗，
        本页只管入口、日志与刷新。"""
        if self._game is None:
            QMessageBox.information(self, "批量特别关注", "请先选择游戏档案。")
            return
        dlg = BatchSpecialDialog(self._repo, self._game, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.report is not None:
            r = dlg.report
            self._log.ok(
                f"批量特别关注完成（{r.game_name}）：新标 {len(r.to_set)} 个；"
                f"已是关注跳过 {len(r.already_special)} 个；"
                f"其他档案跳过 {len(r.in_other_games)} 个；"
                f"未收录 {len(r.missing)} 个未动")
            if r.missing:
                self._log.info(
                    "未收录条目需先入库才能标记：可到「功能模块 → 加入新 mod」"
                    "或「基础功能 → 网址批量导入」粘贴同一份清单")
            self._reload()

    def _on_batch_note(self) -> None:
        """【批量编辑备注…】（维护版）：三模式整批写备注。
        插入语义 = 新文本 + 换行 + 原备注（原备注为空时自然退化为
        直接写入）；替换/清空覆盖原内容——清空走确认且默认否
        （决策 69⒋）。与批量颜色同一纪律：只收当前显示的勾选；
        一个事务，要么全写要么不动。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "批量编辑备注", "先在表格第一列勾选要编辑的 mod。")
            return
        mode, ok = QInputDialog.getItem(
            self, "批量编辑备注",
            f"对勾选的 {len(ids)} 个 mod 的备注执行：",
            ("在开头插入…", "在结尾插入…", "替换为…", "清空备注"),
            current=0, editable=False)
        if not ok:
            return
        text = ""
        if mode != "清空备注":
            text, ok = QInputDialog.getMultiLineText(
                self, "批量编辑备注",
                f"{mode.rstrip('…')}（确定后对 {len(ids)} 个 mod 生效）：", "")
            if not ok:
                return
            text = text.strip()
            if not text and mode != "替换为…":
                QMessageBox.information(
                    self, "批量编辑备注", "插入内容为空：没有做任何改动。")
                return
        if mode == "清空备注" or (mode == "替换为…" and not text):
            ret = QMessageBox.question(
                self, "清空备注",
                f"确定清空 {len(ids)} 个 mod 的现有备注？原备注内容将被"
                "移除（不可撤销）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                self._log.info("已取消批量备注：没有做任何改动")
                return
        with self._repo.transaction():
            for mid in ids:
                m = self._repo.get_mod(mid)
                if m is None:
                    continue  # 勾选与落库之间刚被清账（极端竞态）：跳过
                old = m.note or ""
                if mode == "在开头插入…":
                    new = f"{text}\n{old}" if old else text
                elif mode == "在结尾插入…":
                    new = f"{old}\n{text}" if old else text
                else:  # 替换为… / 清空备注
                    new = text
                self._repo.set_note(mid, new.strip() or None)
        verb = {"在开头插入…": "已在备注开头插入",
                "在结尾插入…": "已在备注结尾插入",
                "替换为…": "已替换备注",
                "清空备注": "已清空备注"}[mode]
        self._log.ok(f"{verb}：{len(ids)} 个")
        self._reload()


    def _on_batch_color(self) -> None:
        """【批量颜色标记…】（v2.49）：对勾选行整批上色/清除。
        与批量特别关注同一纪律：只收当前清单里显示的勾选。
        颜色是纯展示属性、随时可改可清——不设确认弹窗，选完即写
        （先简单后复杂：真需要预览确认时再加）。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "批量颜色标记", "先在表格第一列勾选要标记的 mod。")
            return
        names = [*_COLORS.keys(), "（清除标记）"]
        name, ok = QInputDialog.getItem(
            self, "批量颜色标记",
            f"给勾选的 {len(ids)} 个 mod 设置颜色标记：",
            names, current=0, editable=False)
        if not ok:
            return
        tag = None if name.startswith("（") else _COLORS[name]
        with self._repo.transaction():
            for mid in ids:
                self._repo.set_color_tag(mid, tag)
        self._log.ok(("已清除颜色标记" if tag is None else f"已标记为「{name}」")
                     + f"：{len(ids)} 个")
        self._reload()

    def _on_open_pages_checked(self) -> None:
        """【打开工坊页面】（操作▾，批量订阅场景）：把勾选 mod 的工坊页
        逐个交给默认浏览器——每个 mod 一个标签页，用户逐页点订阅。
        与 T27b 同一纪律：只开当前清单里显示的勾选。网址取账本 url 字段
        （导入时按模板生成），不再发明第五份模板串；开前确认一次——
        N 个 mod 就是 N 个标签，误点不确认就是浏览器灾难。"""
        if not self._require_game():
            return
        ids = self._model.checked_ids_in_rows()
        if not ids:
            QMessageBox.information(
                self, "打开工坊页面", "先在表格第一列勾选要打开的 mod。")
            return
        ret = QMessageBox.question(
            self, "打开工坊页面",
            f"在浏览器打开 {len(ids)} 个工坊页面？\n"
            "每个 mod 一个标签页；数量多时浏览器会连开一片，"
            "可先关掉多余页面再逐个订阅。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        opened, missing = 0, []
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is not None and m.url:
                QDesktopServices.openUrl(QUrl(m.url))
                opened += 1
            else:
                missing.append(mid)
        self._log.ok(f"已请求浏览器打开 {opened} 个工坊页面")
        if missing:
            self._log.warn(f"这些编号没有网址记录，未打开：{missing}")

    def _on_open_pages_paste(self) -> None:
        """【打开工坊页面（贴清单）…】（操作▾）：三步对话框（贴 → 预览 →
        打开）。与勾选版互补：那边面向已在库的 mod（网址取账本字段），
        这边面向手里只有清单的场景（未收录也能开，网址按模板生成）。
        就地 import：本页唯一用点，免动文件头 import 区。"""
        from gui.batchOpenDialog import BatchOpenDialog
        dlg = BatchOpenDialog(self._repo, self, log=self._log)
        dlg.exec()

    def _steamcmd_root(self) -> Path | None:
        """设置页的 steamcmd 程序路径 → steamcmd 根目录（core/steamPaths 推导）。
        没配 steamcmd 程序路径 → None，调用方提示去设置页。"""
        return steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))

    def _make_backup_engine(self) -> BackupManager:
        """按当前设置构造备份引擎（参数注入模式，每次现读设置）。"""
        return BackupManager(
            self._repo,
            keep_per_mod=self._settings.get_int("backup_keep_per_mod", 1),
            quota_bytes=(self._settings.get_int("backup_total_quota_gb", 100)
                         * 2 ** 30),
            steamcmd_path=self._settings.get("steamcmd_path"),
        )

    def _start_backup(self, m) -> None:
        """右键"手动备份"：把该 mod 的下载内容复制进备份区。
        R7：steamcmd 在跑时备份可能拿到写了一半的文件——记事本定性为
        "建议等待"，弹不弹窗是 GUI 的事，这里问一句再动手。
        备份本身同步执行（已知限制见文件头），先预告再动手。"""
        if steamcmd_running():
            ret = QMessageBox.question(
                self, "steamcmd 正在运行",
                "检测到 steamcmd 正在运行：此刻备份可能拿到不完整的副本"
                "（下载仍在写入）。\n\n建议等下载结束后再备份。仍要现在备份吗？")
            if ret != QMessageBox.StandardButton.Yes:
                self._log.info("已取消备份：等 steamcmd 结束后再来")
                return
        self._log.info(
            f"开始备份 mod {m.mod_id}（robocopy 同步执行，"
            "大目录期间界面会冻结，请耐心等待）…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rep = self._make_backup_engine().backup_mod(m.mod_id)
            err = None
        except Exception as exc:
            # 引擎约定"操作层面不成立走报告"，能抛到这里的都是意外
            rep, err = None, str(exc)
        finally:
            QApplication.restoreOverrideCursor()

        if err is not None:
            self._log.error(f"备份未能完成：{err}")
            QMessageBox.critical(self, "备份失败", f"发生意外错误：{err}")
            return
        self._report_backup(rep, m)

    def _report_backup(self, rep, m) -> None:
        """把引擎报告翻译成控制台日志和必要的弹窗。
        备份记录本身已入 backups 表（数据库记结构化事实），
        这里只说人话结论（决策 18 两层分离）。"""
        for w in rep.warnings:
            self._log.warn(w)
        if rep.ok:
            rec = rep.backup
            self._log.ok(
                f"备份完成：mod {m.mod_id} → {rec.backup_path}"
                f"（{fmt_size(rec.size_bytes)}，版本 {rec.version_timeupdated}）")
            for name in rep.cleaned:
                self._log.info(f"保留策略清腾旧备份：{name}")
        else:
            self._log.error(f"备份失败：{rep.error}")
            QMessageBox.warning(self, "备份失败", rep.error)

    def shutdown(self) -> None:
        """关窗收尾（MainWindow.closeEvent 的页面循环自动发现并调用）：
        摘除高级筛选对话框 conditions_changed → _reload 的连接。
        为什么必须摘：主窗口退出时，本页持有的非模态对话框会被联动
        关闭，QDialog 的关闭路（closeEvent → reject → done）照走不误，
        done() 里的关窗清空会广播 conditions_changed——而退出路上
        数据库已经关闭，_reload 当场炸 ProgrammingError。退出路上
        没有"恢复全量列表"可言，摘信号即断链；正常使用中的关窗
        清空（Esc / × / 关闭按钮）不受影响，连接只在退出时摘。"""
        try:
            self._adv_dialog.conditions_changed.disconnect(self._reload)
        except (RuntimeError, TypeError):
            pass  # 本就没连上/已断开——两种都算"已断链"，无害


    def _on_scan_local(self) -> None:
        """【扫描本地】入口（现在住在【刷新 ▾】的小箭头菜单里，T19⑱）。
        缺前提（没选档案 / 没配 steamcmd 路径）时弹窗说明，
        其余交给 _scan_local_run。批次结束后的自动复扫走 scan_local_quiet，
        不经过这里。"""
        if self._game is None:
            QMessageBox.information(self, "扫描本地", "请先选择游戏档案。")
            return
        if self._steamcmd_root() is None:
            QMessageBox.warning(
                self, "扫描本地",
                "尚未设置 steamcmd 程序路径，无法定位工坊账本文件。\n"
                "请先到设置页填写 steamcmd 程序（steamcmd.exe）的完整路径。")
            return
        self._scan_local_run(self._game, quiet=False)

    def scan_local_quiet(self, game: Game) -> None:
        """对外入口（主窗口在批次结束后自动调用，决策 26）。
        与按钮入口同一条扫描链，差别只有两点：
        - 全程只写日志、不弹任何窗——自动化链路里弹窗会把
          无人值守的流程卡死；
        - 扫哪个档案由调用方指定（这一批为谁下载的就复扫谁），
          与界面当前选中的档案无关。"""
        if game is None:
            return
        if self._steamcmd_root() is None:
            self._log.warn(
                f"自动复扫跳过（{game.name}）：未设置 steamcmd 程序路径")
            return
        self._scan_local_run(game, quiet=True)

    def _scan_local_run(self, game: Game, quiet: bool) -> None:
        """扫描五步链（决策 23⑤）：核对下载目录 → 定位 acf → 解析条目 →
        与库内状态做差 → 按计划写库。
        quiet=False：各失败点弹窗说明（交互式，行为与旧版一致）；
        quiet=True：同样的信息只走 LogBus。
        steamcmd 此刻多半还在跑（批次刚结束、进程未退）——扫描只读 acf
        无害（R7），质量谓词会拦下不完整条目。"""

        def complain(title: str, text: str) -> None:
            """失败提示的双通道出口：日志必写；交互模式再弹窗。"""
            self._log.warn(text)
            if not quiet:
                QMessageBox.warning(self, title, text)

        root = self._steamcmd_root()
        # 顺手核对档案的下载目录（决策 21：它永远可由 steamcmd 位置推导）。
        # 档案建好后 steamcmd 若挪了位置，旧目录就成了死路径——现值在盘上
        # 不存在时按当前位置重新推导，推出不同值就写回档案。
        # 注意：扫描的不是界面当前档案时（自动复扫场景），只写数据库、
        # 不动 self._game，避免悄悄换掉用户正在看的界面。
        effective_dir, changed = steamPaths.refresh_download_dir(
            game.download_dir,
            self._settings.get("steamcmd_path"),
            game.app_id)
        if changed:
            self._repo.update_game(game.app_id, download_dir=effective_dir)
            if self._game is not None and game.app_id == self._game.app_id:
                self._game = self._repo.get_game(game.app_id)  # 内存同步
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective_dir}")

        acf = localScanner.locate_acf(root, game.app_id)
        if acf is None:
            complain(
                "扫描本地",
                f"在 steamcmd 的工坊目录（{root}\\steamapps\\workshop）下，"
                f"没有找到 appworkshop_{game.app_id}.acf。\n"
                "请检查：① 设置页的 steamcmd 程序路径是否填对；"
                "② 该游戏的 mod 是否用这台 steamcmd 下载过"
                "（Steam 客户端订阅下载的内容不在这个目录树里）。")
            return
        try:
            result = localScanner.scan_acf(acf)
        except ValueError as exc:
            # acf 内容结构性损坏（解析器约定用 ValueError 表达）
            self._log.error(f"扫描本地失败：{exc}")
            if not quiet:
                QMessageBox.critical(self, "扫描本地", str(exc))
            return

        if not result.items:
            note = (f"（跳过 {len(result.skipped)} 条不完整条目）"
                    if result.skipped else "")
            self._log.warn(f"扫描本地：acf 中没有任何工坊条目{note}")
            return

        # 先取库内状态做差，再统一写库——扫描器自己不碰数据库
        status_map = {m.mod_id: m.status
                      for m in self._repo.list_mods(game.app_id)}
        plan = localScanner.diff_plan(result.items, status_map,
                                      game_id=game.app_id)
        rep = localScanner.apply(self._repo, plan)
        self._reload()

        skipped = (f"，跳过 {len(result.skipped)} 条不完整条目"
                   if result.skipped else "")
        self._log.ok(
            f"扫描本地完成（{game.name}）：共 {len(result.items)} 条，"
            f"回填 {rep.updated}（首次确认下载 {rep.transitioned}），"
            f"新入库 {rep.inserted}{skipped}")

        # 决策 23 质量谓词的产出：疑似下载中断的条目没有入账，单独提醒
        # （与上面"不完整条目"分开说——格式类要排查工具/文件，
        # 中断类的修复动作就是重新下载）
        n_interrupted = len(result.interrupted)
        if n_interrupted:
            ids_text = "、".join(result.interrupted[:10]) + \
                       ("…" if n_interrupted > 10 else "")
            self._log.warn(
                f"发现 {n_interrupted} 条疑似下载中断的条目，未入账："
                f"{ids_text}；如需修复请重新下载（命令生成页勾选对应编号）")
        # v2.49：两道闸（已清账黑名单 + 账实核对）的拦截回执——被拦的
        # "复活"条目必须让人看见，否则用户只看到数字对不上。notes 是
        # apply 准备好的人话句子，原样转述即可；没有拦截时什么都不说
        for note in rep.notes:
            self._log.warn(f"扫描本地：{note}")

        # size 类异常不拦入账，扫描器聚合好的警告原样转述
        for w in result.warnings:
            self._log.warn(f"扫描本地：{w}")

        if rep.inserted:
            self._log.info(
                "新入库条目缺标题等远端信息，建议跑一次更新检测补齐")
