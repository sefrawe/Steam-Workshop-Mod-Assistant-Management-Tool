"""mod 库页
"""
"""
gui/modListPage.py · mod 库页——工作台第一页，账本的日常操作台。

布局：顶部筛选条（搜索范围 / 搜索 / 状态 / 颜色 / 特别关注 / 排序 /
刷新 / 操作▾ / 高级筛选生效指示）+ 水平分割（左表格 / 右详情）。
高级筛选：菜单栏顶级项「高级筛选(&S)」（MainWindow 挂，任何页随时
拉开）+ 本页持有的非模态对话框，与顶栏筛选叠加生效。

【判决制口径（本页的几条底线）】
- 本地版本 = 你确认过的版本；右键「设定本地版本…」是第三扇门
  （另两扇：批次收尾确认清单、入账中心认领）——最重的人工背书，
  判决史记一条「手动设定」；
- 本页没有任何"扫描/自动回填/自动入账"：本地盘点已整体移交
  【入账中心】的「盘点本地」（D29）——在本页找不到扫描入口是
  设计如此，不是缺功能；
- 「恢复」走 repo.mark_restored：目标状态由账本按"有没有确认版本"
  回推（有 → 已下载；无 → 待下载），页面不做推断；
- 彻底清账成功后自动"通知 steamcmd 忘记这些条目"（断根，防止它把
  已清账的 mod 重新装配回来占盘），实现单源 core/steamPaths；
 - 「手动备份」「备份选中项」「下载选中项」「获取/复制下载命令」
   均已恢复：三个落点页（备份与恢复/批量下载/命令生成）已全部
   搬迁接线——跨页的经信号转主窗口对应页面，命令文本直取
   core.commandBuilder 单源；点击有真实去处（界面三问过关）。


分工与边界（记事本架构约定）：
- 只通过 ModRepository 接口读写，GUI 层零 SQL；
- 查询条件由本页持有，模型只管展示、不回头反问查询条件；
- 排序唯一入口 = 工具条下拉（决策 99 沿用），排序串源自
  modListModel._SORT_MAP 白名单；_reload 对 ValueError 兜底——
  排序串被账本白名单拒绝时回退无排序重查并留日志，页面不查死；
- 颜色筛选/搜索范围细分/作者与本地版本时间（高级筛选内存侧条件）
  都在取数后的 Python 过滤——几百行量级成本可忽略，repo 契约零改动；
- 勾选纪律：模型保存全局勾选集合（筛选、排序后勾选不丢）；
  一切批量动作只收"当前看得见的勾选"（_visible_checked_ids），
  被筛选藏起来的不隐形参与。

会话记忆（T19⑤ 沿用）：分割比例、排序状态、列宽，键住 QSettings
session/ 命名空间；排序恢复先过白名单校验，不合法回默认。
"""

from pathlib import Path

from PySide6.QtCore import (QDateTime, QModelIndex, QSettings, Qt, QTime, QTimer, QUrl, Signal)

from PySide6.QtGui import QAction, QColor,  QDesktopServices

from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,QColorDialog,
)

from core import steamPaths
from core.appSettings import AppSettings
from core.backupManager import steamcmd_running
from core.constants import STATUS_DOWNLOADED, STATUS_TRACKED
from core.formatters import abs_time
from core.urlParser import WORKSHOP_URL_TEMPLATE  # 工坊链接模板单源（决策 61④）

from core.models import Game
from gui.advancedSearchDialog import AdvancedSearchDialog
from gui.batchSpecialDialog import BatchSpecialDialog
from gui.consolePanel import LogBus
from gui.modDetailPanel import ModDetailPanel
from gui.modFolderOpener import open_mod_folder
from gui.modListModel import (
    COL_COLOR, COL_ID, COL_LOCAL, COL_NOTE, COL_REMOTE, COL_SIZE,
    COL_SPECIAL, COL_STATUS, COL_TAGS, COL_TITLE, COL_UPDATE,
    COLOR_CHOICES, ModListModel, _SORT_MAP, _color_name, _color_qcolor,
)


from core.commandBuilder import build_copy_text  # 下载命令文本单源（core 纯函数，不碰库不弹窗）

# 列显隐开关（T19⑯ 沿用，键名不变：设置页的旧偏好语义相同时仍生效）。
# 勾选/颜色/编号/标题是结构列，不提供开关
# 列显隐开关：设置页 8 个键全数接线。此前 update/tags/special 三键
# 无列可管（列在瘦身中没搬）= 拨了没反应的死开关；列补齐后全活
_COL_SETTING = {
    COL_COLOR: "mod_col_color",

    COL_STATUS: "mod_col_status",
    COL_LOCAL: "mod_col_local_ver",
    COL_REMOTE: "mod_col_remote_ver",
    COL_UPDATE: "mod_col_update",
    COL_SIZE: "mod_col_size",
    COL_TAGS: "mod_col_tags",
    COL_SPECIAL: "mod_col_special",
    COL_NOTE: "mod_col_note",
}

# 排序下拉的人话（T19⑰ 沿用）：列号 → 中文名 / 方向说法。
# 排序串一律 _SORT_MAP 白名单 + " DESC" 后缀，本页不发明新排序串
_SORT_COL_NAME = {
    COL_ID: "编号", COL_TITLE: "标题", COL_STATUS: "状态",
    COL_LOCAL: "本地版本", COL_REMOTE: "远端版本", COL_SIZE: "大小",
}
_SORT_ASC = {
    COL_ID: "小→大", COL_TITLE: "A→Z", COL_STATUS: "升序",
    COL_LOCAL: "旧→新", COL_REMOTE: "旧→新", COL_SIZE: "小→大",
}
_SORT_DESC = {
    COL_ID: "大→小", COL_TITLE: "Z→A", COL_STATUS: "降序",
    COL_LOCAL: "新→旧", COL_REMOTE: "新→旧", COL_SIZE: "大→小",
}

# ---- 会话记忆键（T19⑤）：QSettings 而非 AppSettings，与 MainWindow
# 的 _SES_* 同一套口径（session/ 命名空间）----
_SES_SPLIT = "session/modlib_split"     # 左表格/右详情分割比例："左宽,右宽"
_SES_SORT = "session/modlib_sort"       # 排序状态：/col 列号、/order_by 串
_SES_COLW = "session/modlib_colwidths_v2"  # 列数 9→12 换血：旧键按列号错位，整体作废重记


# ---- 「设定本地版本」对话框（单条右键与批量操作共用一张）----

# 常用时段下拉：文字 → 从当前时刻往回推的算法。天数按天回退；
# "半个月"取 15 天；月/年以上按日历月/年回退（addMonths/addYears）
# ——"1 个月前"按日历算才符合直觉，固定 30 天会漂。
_VERSION_PRESETS: tuple[tuple[str, object], ...] = (
    ("1 天前",   lambda dt: dt.addDays(-1)),
    ("3 天前",   lambda dt: dt.addDays(-3)),
    ("1 周前",   lambda dt: dt.addDays(-7)),
    ("半个月前", lambda dt: dt.addDays(-15)),
    ("1 个月前", lambda dt: dt.addMonths(-1)),
    ("3 个月前", lambda dt: dt.addMonths(-3)),
    ("半年前",   lambda dt: dt.addMonths(-6)),
    ("1 年前",   lambda dt: dt.addYears(-1)),
)


class ManualVersionDialog(QDialog):
    """「设定本地版本」对话框——单条右键与批量操作共用一张。

    只负责收集一个诚实的答案，写库一律由调用方走
    repo.set_manual_version 正门（R17 第三扇门），本对话框零数据库。

    两个固定口径（动这里前先读）：
    - 时间填到分钟即可：工坊页面「最后更新」只显示到分钟；
    - 秒一律按 59 记（result_version 里落刀）：判定公式是"远端版本
      比本地版本新才报需更新"，若存 00 秒，同一分钟内发布的版本
      会被误报成"有更新"；存 59 秒把整分钟盖住，不误报——真正
      更晚的更新落在下一分钟、工坊会如实显示，也不漏报。
    """

    def __init__(self, items: list[tuple[int, str | None]], parent) -> None:
        super().__init__(parent)
        n = len(items)
        batch = n > 1
        self.setWindowTitle("批量设定本地版本" if batch else "设定本地版本")
        lay = QVBoxLayout(self)

        if batch:
            names = "、".join((title or str(mid)) for mid, title in items[:5])
            head = QLabel(f"为勾选的 {n} 个 mod 设定同一个本地版本时间"
                          f"（{names}{' 等' if n > 5 else ''}）。", self)
            head.setToolTip("\n".join(
                f"{mid}  {title or ''}" for mid, title in items))
        else:
            mid, title = items[0]
            head = QLabel(
                f"为「{title or mid}」（mod {mid}）手工认定本地版本"
                "（最重的人工背书，判决史会记一条「手动设定」）。", self)
        head.setWordWrap(True)
        lay.addWidget(head)

        tip = QLabel(
            "版本时间 = 该版本在创意工坊的更新时间（工坊页面右侧"
            "「最后更新」可查，填到分钟就行）。", self)
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # 常用时段：选中即填进下方时刻框，仍可手动微调。用 activated
        # 而非 currentIndexChanged——同一个时段连选两次也要生效
        prow = QWidget(self)
        ph = QHBoxLayout(prow)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(QLabel("常用时段：", prow))
        self._preset = QComboBox(prow)
        self._preset.addItem("（选一个常用时段，或直接在下面改时间）")
        for text, _fn in _VERSION_PRESETS:
            self._preset.addItem(text)
        self._preset.activated.connect(self._on_preset)
        self._preset.setToolTip(
            "按当前时刻往回推：天数按天、月以上按日历月/年。"
            "选中后填进下面时刻框，可再微调")
        ph.addWidget(self._preset, 1)
        lay.addWidget(prow)

        self._dt = QDateTimeEdit(self)
        self._dt.setCalendarPopup(True)
        self._dt.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._dt.setDateTime(QDateTime.currentDateTime())
        self._dt.setToolTip(
            "不知道确切时刻就填那天的大致时间——它只影响「需更新」"
            "的判定基准、排序显示与备份目录名")
        lay.addWidget(self._dt)

        note = QLabel(
            "时间只填到分钟：软件按这一分钟的 59 秒记账——工坊的"
            "「最后更新」也只显示到分钟，这一分钟内发布的都算已覆盖。"
            "想强制让【更新检测】重新报某条的更新（回拨），把时间设到"
            "比工坊显示更早即可。", self)
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)

        self._unknown = QCheckBox("记为「版本未知」（不指定版本时间）", self)
        self._unknown.setToolTip(
            "本地版本记为未知：条目正常管理，但在确认出版本之前"
            "不能给它做备份")
        self._unknown.toggled.connect(lambda on: self._dt.setEnabled(not on))
        lay.addWidget(self._unknown)

        if batch:
            skip = QLabel(
                "已删除的条目自动跳过；会覆盖每条已有的本地版本记录；"
                "每条在判决史各记一条「手动设定」。", self)
            skip.setWordWrap(True)
            skip.setStyleSheet("color: gray;")
            lay.addWidget(skip)

        btns = QHBoxLayout()
        ok_btn = QPushButton(f"认定 {n} 个" if batch else "认定", self)
        ok_btn.setDefault(True)
        cancel_btn = QPushButton("取消", self)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)

    def _on_preset(self, index: int) -> None:
        """常用时段选中：把对应时刻填进时刻框（0 号是占位项，忽略）。"""
        if index <= 0:
            return
        _text, fn = _VERSION_PRESETS[index - 1]
        self._dt.setDateTime(fn(QDateTime.currentDateTime()))

    def result_version(self) -> int | None:
        """收集结果：勾了「版本未知」→ None；否则按 59 秒口径返回
        Unix 秒（显示格式只到分钟、秒一直停在 00，这里强制改成 59，
        理由见类 docstring）。"""
        if self._unknown.isChecked():
            return None
        dt = self._dt.dateTime()
        t = dt.time()
        dt.setTime(QTime(t.hour(), t.minute(), 59))
        return dt.toSecsSinceEpoch()

class ModListPage(QWidget):
    # 跨页信号（落点页未搬迁，暂无菜单项触发；定义保留待接线）：
    # command_gen_requested([mod_id]) → 命令生成页聚焦
    # download_requested(app_id, ids)  → 批量下载开批次
    # backup_requested(app_id, ids)    → 备份页开批次
    command_gen_requested = Signal(list)
    download_requested = Signal(int, list)
    backup_requested = Signal(int, list)
    # 高级筛选对话框【到 mod 库查看结果】→ MainWindow 跳到本页
    advanced_results_requested = Signal()

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        # 日志总线：MainWindow 注入；单独调试本页时兜底建一个
        # （消息没人显示但不崩——"发进空气 = 无操作"）
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._search = ""                     # 防抖后生效的搜索词
        self._selected_mod_id: int | None = None
        self._row_mods: list = []             # 行号 → Mod（当前清单的数据侧）

        # 排序状态：启动从会话记忆恢复，先过白名单校验
        self._order_by, self._sort_col = self._load_sort()

        # 高级筛选对话框（非模态、本页持有防 GC）。必须先于 _build_ui：
        # 工具条的"生效指示"按钮要连它。条件防抖广播 → 本页 _reload
        self._adv_dialog = AdvancedSearchDialog(self, settings=self._settings)
        self._adv_dialog.conditions_changed.connect(self._reload)
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

        # 搜索范围下拉（决策 99 沿用）：全部=标题+备注 SQL 合查；
        # 标题/备注/编号=页面内存过滤（repo 契约零改动）
        self._search_field_combo = QComboBox(row)
        for label, val in (("全部", "all"), ("标题", "title"),
                           ("备注", "note"), ("编号", "id")):
            self._search_field_combo.addItem(label, val)
        self._search_field_combo.setToolTip(
            "搜索框查什么：\n"
            "全部：标题和备注都查（默认）；\n"
            "标题 / 备注：只查对应字段；\n"
            "编号：按完整编号精确匹配（输非数字 = 0 命中）")
        self._search_field_combo.currentIndexChanged.connect(
            self._on_search_field_changed)
        h.addWidget(self._search_field_combo)

        self._search_edit = QLineEdit(row)
        self._search_edit.setPlaceholderText("搜索标题 / 备注…")
        self._search_edit.setClearButtonEnabled(True)
        h.addWidget(self._search_edit, 1)

        # 状态下拉：显示中文，currentData 存库内原始值
        self._status_combo = QComboBox(row)
        for label, val in (
                ("全部", None),
                ("待下载", STATUS_TRACKED),
                ("已下载", STATUS_DOWNLOADED),
                ("已删除", "deleted"),
                ("已失败", "failed"),
        ):
            self._status_combo.addItem(label, val)
        self._status_combo.setToolTip(
            "待下载 = 账上有编号、还没下载；已下载 = 你确认过下载成功")
        h.addWidget(self._status_combo)

        # 颜色筛选四态：不筛选 / 全部颜色 / 无标记 / 具体颜色。
        # 颜色不是库查询条件，页面内存过滤；比对值 = 色名
        #（_color_name 归一，旧账 hex 也能对上）
        self._color_combo = QComboBox(row)
        self._color_combo.addItem("不筛选", None)
        self._color_combo.addItem("全部颜色", "any")
        self._color_combo.addItem("无标记", "none")
        for _name in COLOR_CHOICES:
            self._color_combo.addItem(_name, _name)
        self._color_combo.setToolTip(
            "不筛选：不做颜色过滤；\n"
            "全部颜色：只看上过色的条目；\n"
            "无标记：只看没上色的条目；\n"
            "选具体颜色：只看该颜色的条目")
        self._color_combo.currentIndexChanged.connect(self._reload)
        h.addWidget(self._color_combo)

        self._special_check = QCheckBox("特别关注", row)
        h.addWidget(self._special_check)

        # 排序下拉（唯一排序入口，决策 99）：列表当前顺序也是
        # 将来下载/备份批次的执行顺序
        self._sort_combo = QComboBox(row)
        for col, key in _SORT_MAP.items():
            for suffix, lab in (("", _SORT_ASC[col]),
                                (" DESC", _SORT_DESC[col])):
                self._sort_combo.addItem(
                    f"按{_SORT_COL_NAME[col]}排（{lab}）", (col, key + suffix))
        self._sort_combo.setToolTip(
            "排序方式（唯一排序入口）。\n"
            "下载/备份选中项将来按列表当前顺序执行")
        idx = self._sort_combo.findData((self._sort_col, self._order_by))
        self._sort_combo.setCurrentIndex(max(idx, 0))
        self._sort_combo.currentIndexChanged.connect(self._on_sort_combo)
        h.addWidget(self._sort_combo)

        # 刷新（纯按钮——「扫描本地」已移交入账中心「盘点本地」，D29）
        self._btn_refresh = QPushButton("刷新", row)
        self._btn_refresh.setToolTip(
            "重新从数据库加载当前清单（改了列显隐等设置后回来点一下即生效）")
        self._btn_refresh.clicked.connect(self._reload)
        h.addWidget(self._btn_refresh)

        # 操作 ▾：对第一列勾选的 mod 批量操作（InstantPopup）
        self._btn_actions = QToolButton(row)
        self._btn_actions.setText("操作 ▾")
        self._btn_actions.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_actions.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self._actions_menu = QMenu(self._btn_actions)
        self._actions_menu.setMinimumWidth(240)  # 中文菜单项宽度兜底

        act_sel_all = QAction("全选当前显示", self._actions_menu)
        act_sel_all.setToolTip(
            "勾选当前筛选结果里的全部 mod（被搜索/筛选藏起来的不算；"
            "已有的勾选保持不变）")
        act_sel_all.triggered.connect(self._select_all_visible)
        self._actions_menu.addAction(act_sel_all)

        act_sel_none = QAction("清除全部勾选", self._actions_menu)
        act_sel_none.setToolTip("取消本页所有勾选")
        act_sel_none.triggered.connect(
            lambda: self._model.set_checked(set()))
        self._actions_menu.addAction(act_sel_none)
        self._actions_menu.addSeparator()

        self._act_softdel = QAction("软删除选中项", self._actions_menu)
        self._act_softdel.setToolTip(
            "把勾选的 mod 整批标记为已删除：记录保留、随时可恢复；"
            "磁盘上一个文件都不动。已是删除状态的条目自动跳过")
        self._act_softdel.triggered.connect(self._on_softdel_checked)
        self._actions_menu.addAction(self._act_softdel)
        self._act_batch_restore = QAction("批量恢复…", self._actions_menu)
        self._act_batch_restore.setToolTip(
            "把勾选的「已删除」条目整批恢复：有确认版本 → 回「已下载」，"
            "没有 → 回「待下载」（账本按确认版本回推）；非删除状态的"
            "自动跳过，盘上文件不会被动")
        self._act_batch_restore.triggered.connect(self._on_batch_restore)
        self._actions_menu.addAction(self._act_batch_restore)

        self._act_purge = QAction("彻底清账选中项", self._actions_menu)
        self._act_purge.setToolTip(
            "把勾选 mod 的记录从账本里物理删除：不可恢复；磁盘一个文件"
            "不碰，自动登记「已清账」黑名单（今后不会再录入账本）。\n"
            "「已下载」条目会被跳过——它们的完整出口（含删文件选项）在"
            "「清理与删除」页")
        self._act_purge.triggered.connect(self._on_purge_checked)
        self._actions_menu.addAction(self._act_purge)
        self._actions_menu.addSeparator()

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
        self._actions_menu.addSeparator()

        self._act_batch_color = QAction("批量颜色标记…", self._actions_menu)
        self._act_batch_color.setToolTip(
            "给勾选的 mod 整批设置同一种颜色标记（或清除标记）："
            "颜色随时可改可清")
        self._act_batch_color.triggered.connect(self._on_batch_color)
        self._actions_menu.addAction(self._act_batch_color)

        self._act_batch_note = QAction("批量编辑备注…", self._actions_menu)
        self._act_batch_note.setToolTip(
            "对勾选的 mod 整批编辑备注：在现有备注的开头或结尾插入一段"
            "文字（不动原内容），或整批替换/清空")
        self._act_batch_note.triggered.connect(self._on_batch_note)
        self._actions_menu.addAction(self._act_batch_note)
        self._act_batch_setver = QAction("批量设定本地版本…", self._actions_menu)
        self._act_batch_setver.setToolTip(
            "给勾选的 mod 整批设定同一个本地版本时间（最重的人工背书，"
            "逐条记进判决史）：常用时段一键填入，秒按 59 记。\n"
            "典型用途：批量补录后把一整批的版本基准对齐到工坊"
            "「最后更新」显示的时间；或整批回拨让检测重新报更新")
        self._act_batch_setver.triggered.connect(self._on_batch_set_version)
        self._actions_menu.addAction(self._act_batch_setver)
        self._act_batch_revoke = QAction("批量撤销确认…", self._actions_menu)
        self._act_batch_revoke.setToolTip(
            "把勾选条目的本地版本确认清空（撤销确认的批量版）：本地版本"
            "变「未知」、状态不变、判决史保留，下轮【更新检测】重新报告。"
            "已删除的跳过（恢复去向取决于有无确认版本，先撤销会改变"
            "恢复结果）、本就没确认的跳过")
        self._act_batch_revoke.triggered.connect(self._on_batch_revoke)
        self._actions_menu.addAction(self._act_batch_revoke)

        self._actions_menu.addSeparator()

        self._act_open_pages = QAction("打开工坊页面", self._actions_menu)
        self._act_open_pages.setToolTip(
            "把勾选 mod 的工坊页面逐个在默认浏览器打开（每个一页标签）；"
            "开前有确认弹窗报数")
        self._act_open_pages.triggered.connect(self._on_open_pages_checked)
        self._actions_menu.addAction(self._act_open_pages)

        self._act_open_pages_list = QAction(
            "打开工坊页面（贴清单）…", self._actions_menu)
        self._act_open_pages_list.setToolTip(
            "粘贴网址/编号清单批量打开工坊页面——未收录的编号照样能开；"
            "先预览再打开，全程不改账本")
        self._act_open_pages_list.triggered.connect(self._on_open_pages_paste)
        self._actions_menu.addAction(self._act_open_pages_list)
        self._act_open_folders = QAction("打开 mod 文件夹", self._actions_menu)
        self._act_open_folders.setToolTip(
            "把勾选 mod 的内容文件夹（下载目录\\编号）逐个在文件管理器"
            "打开；文件夹不在盘上的跳过并报数，不逐个弹说明框")
        self._act_open_folders.triggered.connect(self._on_open_folders_checked)
        self._actions_menu.addAction(self._act_open_folders)

        self._act_copy_ids = QAction("复制勾选编号", self._actions_menu)
        self._act_copy_ids.setToolTip(
            "把勾选 mod 的工坊编号逐行复制进剪贴板（每行一个）——"
            "可直接贴进【入账中心 · 登记】或其他工具")
        self._act_copy_ids.triggered.connect(self._on_copy_ids_checked)
        # 下载选中项（M2 判决闭环轮回补）：信号 mod 库轮就留好了
        # （download_requested），落点页如今到位——点击有真实去处，
        # 假入口的三问过关
        self._actions_menu.addSeparator()
        self._act_download = QAction("下载选中项", self._actions_menu)
        self._act_download.setToolTip(
            "把勾选的 mod 交给「下载批次」逐条下载（上一条下完再发"
            "下一条，有进度卡片）。\n批次结束自动弹收尾确认清单，"
            "核对勾选后【确认入账】，本地版本才落账。\n"
            "steamcmd 没启动时会提示先到终端页启动")
        self._act_download.triggered.connect(self._on_download_checked)
        self._actions_menu.addAction(self._act_download)
        self._act_backup = QAction("备份选中项", self._actions_menu)
        self._act_backup.setToolTip(
            "把勾选的 mod 交给【备份与恢复】页开备份批次：备份页自动切"
            "到对应档案、只备「已下载」条目（本地版本未知的会被剔出"
            "并说明）；磁盘空间预检与保留策略都在备份页")
        self._act_backup.triggered.connect(self._on_backup_checked)
        self._actions_menu.addAction(self._act_backup)

        self._actions_menu.addAction(self._act_copy_ids)
        self._act_copy_cmds = QAction("复制勾选项下载命令", self._actions_menu)
        self._act_copy_cmds.setToolTip(
            "把勾选 mod 的下载命令（workshop_download_item …）整批复制进"
            "剪贴板：一行一条、按列表当前顺序排列，贴进 steamcmd 终端"
            "回车即逐条执行。\n"
            "不含登录行——先在终端登录，或用命令生成页的「复制登录命令」。"
            "已删除/已失败不出命令（与右键置灰同口径），自动跳过并报数")
        self._act_copy_cmds.triggered.connect(self._on_copy_cmds_checked)
        self._actions_menu.addAction(self._act_copy_cmds)

        self._actions_menu.setToolTipsVisible(True)  # QMenu 默认不显示 tooltip
        self._btn_actions.setMenu(self._actions_menu)
        self._btn_actions.setToolTip(
            "对第一列勾选的 mod 批量操作；菜单项文字带当前勾选数")
        h.addWidget(self._btn_actions)

        self._count_label = QLabel("", row)
        h.addWidget(self._count_label)
        self._checked_label = QLabel("", row)
        h.addWidget(self._checked_label)

        # 高级筛选生效指示：条件生效期间出现；点击 = 一键清空
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
        # 斑马纹由模型在 BackgroundRole 画（qdarktheme 的 QSS 会在
        # 视图级交替行盖掉模型背景——颜色标记奇偶位显隐的元凶）
        self._table.setAlternatingRowColors(False)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        # 排序唯一入口 = 工具条下拉：表头不可点、指示箭头隐藏——
        # 留着会邀请点击，点了没反应（界面三问）
        header.setSortIndicatorShown(False)
        header.setSectionsClickable(False)
        for col, width in ((0, 34), (1, 72), (2, 92), (4, 96), (5, 150),
                           (6, 92), (7, 92), (8, 88), (9, 150),
                           (10, 32), (11, 170)):
            self._table.setColumnWidth(col, self._saved_col_width(col, width))
        # 列宽落盘：拖动连发用单发定时器合并成一次写（400ms 防抖）
        self._colw_save_timer = QTimer(self)
        self._colw_save_timer.setSingleShot(True)
        self._colw_save_timer.setInterval(400)
        self._colw_save_timer.timeout.connect(self._save_col_widths)
        header.sectionResized.connect(self._on_col_resized)
        self._apply_col_visibility()

        # 面板要查判决史，必须把账本仓库交给它——曾把分割器当 repo
        # 传进来，AttributeError 被 try/except 吞掉，"最近判决"永远暂无
        self._detail = ModDetailPanel(self._repo, split)

        self._detail.open_folder_requested.connect(self._open_folder_from_detail)
        split.addWidget(self._table)
        split.addWidget(self._detail)
        self._split = split
        split.setSizes(self._load_split_sizes())
        split.splitterMoved.connect(lambda *_a: self._save_split_sizes())
        root.addWidget(split, 1)

        # ---- 信号 ----
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_edit.textChanged.connect(self._search_timer.start)
        self._status_combo.currentIndexChanged.connect(self._reload)
        self._special_check.toggled.connect(self._reload)
        self._table.selectionModel().currentRowChanged.connect(
            self._on_current_row)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._model.checked_changed.connect(self._update_checked_label)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        # 切档案必须同时清旧选中，否则详情会把上个档案的 mod 带进
        # 新档案的界面（看着像串档）；同档案强制重载不清——用户正看
        # 的详情不该被无谓抹掉，_reload 的恢复选中接手
        same_game = (self._game is not None and game is not None
                     and self._game.app_id == game.app_id)
        self._game = game
        self._detail.set_download_dir(game.download_dir if game else None)

        if not same_game:
            self._selected_mod_id = None
            self._detail.show_mod(None)  # 显式清，不押在 Qt 实现细节上
        self._reload()

    def showEvent(self, event) -> None:
        """进页自动刷新（与入账中心同款站规）：检测落库/确认入账/认领
        之后切回本页即是新数据，不依赖别的页面发通知；「待确认」角标
        同步保持新鲜。"""
        super().showEvent(event)
        if self._game is not None:
            self._reload()

    def open_advanced_search(self) -> None:
        """菜单栏顶级项「高级筛选(&S)」的落点：非模态弹出——show()
        不 exec()，开着不影响主窗口继续操作；已开着时置前不重开。"""
        self._adv_dialog.show()
        self._adv_dialog.raise_()
        self._adv_dialog.activateWindow()

    # ---------- 查询 ----------

    def _on_search_field_changed(self, _index: int) -> None:
        """搜索范围切换：换占位文案 + 立即按新范围重查（搜索词保留）。"""
        field = self._search_field_combo.currentData()
        self._search_edit.setPlaceholderText({
                                                 "all": "搜索标题 / 备注…",
                                                 "title": "搜索标题包含…",
                                                 "note": "搜索备注包含…",
                                                 "id": "输入完整编号（精确匹配）…",
                                             }[field])
        self._reload()

    def _apply_search(self) -> None:
        self._search = self._search_edit.text().strip()
        self._reload()

    def _reload(self) -> None:
        if self._game is None:
            self._model.set_rows([])
            self._row_mods = []
            self._count_label.setText("请先在左上角选择游戏档案")
            self._adv_dialog.set_hit_count(None)
            return

        search_field = self._search_field_combo.currentData()
        kwargs = dict(
            status=self._status_combo.currentData(),
            special_only=self._special_check.isChecked(),
            search=(self._search or None) if search_field == "all" else None,
            order_by=self._order_by,
            **self._advanced_kwargs(),
        )
        try:
            rows = self._repo.list_mods(self._game.app_id, **kwargs)
        except ValueError as exc:
            # 排序串被账本白名单拒绝（版本升级换名等）：回退无排序重查。
            # 宁可丢一次排序也不能让整页查死；日志留痕定位
            self._log.warn(f"排序参数被账本拒绝，本次按默认排序显示：{exc}")
            self._order_by = "time_updated DESC"
            kwargs["order_by"] = self._order_by
            rows = self._repo.list_mods(self._game.app_id, **kwargs)
        # 先把当前清单里出现过的自定义颜色同步进筛选下拉（G 修复：
        # 下拉只在建页时填了预设色，自定义色永远刷不出来）
        self._sync_color_combo(rows)

        # ---- 页面内存过滤（顺序：颜色 → 搜索细分 → 高级筛选内存侧；
        # 它们与"共 N 个"/命中数看到的是同一最终口径）----
        _color_sel = self._color_combo.currentData()
        if _color_sel == "any":
            rows = [m for m in rows if _color_name(m) is not None]
        elif _color_sel == "none":
            rows = [m for m in rows if _color_name(m) is None]
        elif _color_sel is not None:
            rows = [m for m in rows if _color_name(m) == _color_sel]

        # 诊断：库里"有值但归一不出色名"的行正常应为 0；大于 0 = 有
        # 没见过的遗留格式，报数定位
        _bad = [m.mod_id for m in rows
                if (getattr(m, "color", None)
                    or getattr(m, "color_tag", None))
                and _color_name(m) is None]
        if _bad:
            self._log.warn(f"颜色标记无法识别 {len(_bad)} 条"
                           f"（按无标记显示）：{_bad[:10]}")

        if search_field == "title":
            _q = self._search.casefold()
            rows = [m for m in rows if _q in (m.title or "").casefold()]
        elif search_field == "note":
            _q = self._search.casefold()
            rows = [m for m in rows
                    if _q in (getattr(m, "note", "") or "").casefold()]
        elif search_field == "id":
            _q = self._search.strip()
            rows = ([m for m in rows if str(m.mod_id) == _q]
                    if _q.isdigit() else [])

        _adv = self._adv_dialog.conditions()
        if _adv is not None:
            if _adv.creator_id:
                # 作者 = 上传者 SteamID64；双字段名兜底（V1/V2 过渡）
                rows = [m for m in rows
                        if str(getattr(m, "creator_id",
                                       getattr(m, "creator", "")) or ""
                               ).strip() == _adv.creator_id]
            if _adv.local_from is not None:
                rows = [m for m in rows
                        if (getattr(m, "confirmed_version", None) or 0)
                        >= _adv.local_from]
            if _adv.local_to is not None:

                rows = [m for m in rows
                        if (getattr(m, "confirmed_version", None) is not None
                            and getattr(m, "confirmed_version")
                            <= _adv.local_to)]

        # 标签池：当前清单出现过的标签并集（对话框内部保留已勾选项）
        tag_pool = sorted({t for m in rows
                           for t in (getattr(m, "tags", None) or [])})
        self._adv_dialog.set_available_tags(tag_pool)

        # 生效指示 + 命中数回填
        adv_now = self._adv_dialog.conditions()
        self._adv_indicator.setVisible(adv_now is not None)
        if adv_now is not None:
            self._adv_indicator.setToolTip(
                "高级筛选生效中，点击全部清除：\n" + adv_now.describe())
        self._adv_dialog.set_hit_count(
            len(rows) if adv_now is not None else None)

        self._row_mods = rows
        self._model.set_rows(rows)
        # ⑪「账不平」信号：待确认角标随清单刷新（claim 归待认领区，
        # 不计入）。不确认它一直挂着——这是判决制的记账提醒
        _pend = [v for v in self._repo.pending_confirmations(
            self._game.app_id) if v.kind != "claim"]
        self._count_label.setText(
            f"共 {len(rows)} 个 mod"
            + (f" · 待确认 {len(_pend)}" if _pend else ""))

        self._apply_col_visibility()

        # 恢复选中：右键操作后的 reload 会走到这里，之前选中的还在
        # 清单里就重新选上，详情面板不闪空
        if self._selected_mod_id is not None:
            for i, m in enumerate(rows):
                if m.mod_id == self._selected_mod_id:
                    self._table.selectRow(i)
                    break
    def _sync_color_combo(self, rows) -> None:
        """把当前清单里出现过、预设色表没有的自定义 hex 追加进颜色
        筛选下拉；已不在清单的自定义项移除。原选中项尽量保持。
        blockSignals 防重建过程反手触发 _reload 死循环；无变化时不
        折腾下拉，避免每次刷新都重建。"""
        combo = self._color_combo
        want = sorted({
            code for m in rows
            if (code := _color_name(m)) is not None
               and code not in COLOR_CHOICES
        })
        have = [combo.itemData(i) for i in range(combo.count())
                if isinstance(combo.itemData(i), str)
                and combo.itemData(i).startswith("#")]
        if have == want:
            return
        cur = combo.currentData()
        combo.blockSignals(True)
        for i in range(combo.count() - 1, -1, -1):
            d = combo.itemData(i)
            if isinstance(d, str) and d.startswith("#"):
                combo.removeItem(i)
        for code in want:
            combo.addItem(code, code)
        idx = combo.findData(cur)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _advanced_kwargs(self) -> dict:
        """对话框条件 → list_mods 关键字参数（SQL 侧）。全空 → 空 dict：
        查询路径与没有高级筛选时完全一致。creator_id 与本地版本时间
        走内存侧过滤（repo 契约没有这两个参数），见 _reload。"""
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

    # ---------- 勾选（全局集合 × 可见行） ----------

    def _visible_checked_ids(self) -> list[int]:
        """当前清单里"看得见的勾选"——一切批量动作只收这份：
        被搜索/筛选藏起来的不隐形参与（点按钮的人看得见要动什么）。"""
        checked = self._model.checked_ids()
        out: list[int] = []
        for r in range(self._model.rowCount()):
            mid = self._model.mod_id_at(r)
            if mid is not None and mid in checked:
                out.append(mid)
        return out

    def _select_all_visible(self) -> None:
        """全选当前显示：把当前清单里的全部行并入勾选集合（已有勾选
        保持不变；被筛选藏起来的行不在清单里，天然不受影响）。
        旧实现并的是"可见的已勾选行"——已勾选集合的子集，等于把现状
        抄一遍，一个新勾都不会产生，按钮全程空转（实测被用户点名）。"""
        checked = self._model.checked_ids()
        for r in range(self._model.rowCount()):
            mid = self._model.mod_id_at(r)
            if mid is not None:
                checked.add(mid)
        self._model.set_checked(checked)

    def _update_checked_label(self) -> None:
        """勾选数变化 → "已勾选 N"（按可见口径），并同步进菜单项文字。"""
        n = len(self._visible_checked_ids())
        self._checked_label.setText(f"已勾选 {n}" if n else "")
        label = f"（{n}）" if n else ""
        self._act_softdel.setText(f"软删除选中项{label}")
        self._act_purge.setText(f"彻底清账选中项{label}")
        self._act_special_on.setText(f"设为特别关注{label}")
        self._act_special_off.setText(f"取消特别关注{label}")
        self._act_open_pages.setText(f"打开工坊页面{label}")
        self._act_copy_ids.setText(f"复制勾选编号{label}")
        self._act_download.setText(f"下载选中项{label}")
        self._act_batch_color.setText(f"批量颜色标记{label}")
        self._act_batch_note.setText(f"批量编辑备注{label}")
        self._act_batch_setver.setText(f"批量设定本地版本{label}")
        self._act_batch_restore.setText(f"批量恢复{label}")
        self._act_batch_revoke.setText(f"批量撤销确认{label}")
        self._act_open_folders.setText(f"打开 mod 文件夹{label}")
        self._act_backup.setText(f"备份选中项{label}")
        self._act_copy_cmds.setText(f"复制勾选项下载命令{label}")


    # ---------- 列显隐与会话记忆（T19⑤⑯） ----------

    def _apply_col_visibility(self) -> None:
        """按设置页的列开关显隐。值 "0" = 隐藏；缺键 = 显示（默认全开）。
        视图级 setColumnHidden，模型列号不受影响。"""
        for col, key in _COL_SETTING.items():
            self._table.setColumnHidden(col, self._settings.get(key) == "0")

    @staticmethod
    def _load_sort() -> tuple[str, int]:
        """读上次会话排序（排序串 + 列号），先过白名单校验：
        排序串必须是 _SORT_MAP 该列的串或其 + " DESC"；不合法回默认
        （远端版本 新→旧）——历史遗留值绝不直接进查询。"""
        q = QSettings()
        try:
            col = int(q.value(f"{_SES_SORT}/col", COL_REMOTE))
        except (TypeError, ValueError):
            col = COL_REMOTE
        order_by = str(q.value(f"{_SES_SORT}/order_by",
                               "time_updated DESC"))
        base = _SORT_MAP.get(col)
        if base is not None and order_by in (base, base + " DESC"):
            return order_by, col
        return "time_updated DESC", COL_REMOTE

    def _save_sort(self) -> None:
        q = QSettings()
        q.setValue(f"{_SES_SORT}/col", self._sort_col)
        q.setValue(f"{_SES_SORT}/order_by", self._order_by)

    @staticmethod
    def _saved_col_width(col: int, default: int) -> int:
        q = QSettings()
        try:
            w = int(q.value(f"{_SES_COLW}/{col}", default))
        except (TypeError, ValueError):
            return default
        return w if w >= 20 else default  # 拖得只剩一条缝的当脏数据

    def _on_col_resized(self, _index: int, _old: int, _new: int) -> None:
        self._colw_save_timer.start()

    def _save_col_widths(self) -> None:
        """非伸展列宽度落盘。标题列不存：伸展列宽度=剩余空间，
        存了反而在不同窗口尺寸下打架。"""
        q = QSettings()
        for col in range(self._model.columnCount()):
            if col == COL_TITLE:
                continue
            q.setValue(f"{_SES_COLW}/{col}", self._table.columnWidth(col))

    @staticmethod
    def _load_split_sizes() -> list[int]:
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
        q = QSettings()
        q.setValue(_SES_SPLIT,
                   ",".join(str(s) for s in self._split.sizes()))

    # ---------- 槽 ----------

    def _on_sort_combo(self, index: int) -> None:
        data = self._sort_combo.itemData(index)
        if data is None:
            return
        col, order_by = data
        self._order_by = order_by
        self._sort_col = col
        self._save_sort()
        self._reload()

    def _on_current_row(self, current: QModelIndex,
                        _prev: QModelIndex) -> None:
        m = (self._row_mods[current.row()]
             if current.isValid() and current.row() < len(self._row_mods)
             else None)
        self._selected_mod_id = m.mod_id if m is not None else None
        self._detail.show_mod(m)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid() or index.row() >= len(self._row_mods):
            return
        m = self._row_mods[index.row()]
        menu = QMenu(self)
        menu.setMinimumWidth(240)
        menu.setToolTipsVisible(True)  # Qt 默认不显示动作 tooltip
        mid = m.mod_id

        act_open = QAction("打开工坊页面", menu)
        act_open.setToolTip(
            "在浏览器打开该 mod 的 Steam 创意工坊页面（存了网址用存的；"
            "没存按编号现拼——工坊网址本来就是编号的确定函数）")
        act_open.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(self._resolve_url(m))))

        menu.addAction(act_open)

        act_folder = QAction("打开 mod 文件夹", menu)
        act_folder.setToolTip(
            "在文件管理器打开该 mod 的下载内容文件夹（下载目录\\编号）；"
            "目录不在时会有说明")
        act_folder.triggered.connect(lambda: self._open_mod_folder(m))
        menu.addAction(act_folder)

        act_copy = QAction("复制编号", menu)
        act_copy.setToolTip("把这个 mod 的工坊编号复制到剪贴板")
        act_copy.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act_copy)
        menu.addSeparator()

        for text, tip, cb in (
                ("编辑备注…", "给自己看的备忘，只存本地数据库，与工坊无关",
                 lambda: self._edit_note(m)),
                ("颜色标记…", "给条目着个色，方便扫一眼分类；重进本菜单可选"
                              "「清除标记」", lambda: self._pick_color(m)),
                ("切换特别关注", "特别关注的 mod 出新版本时，更新检测会记一条"
                                 "提醒（同一版本只提醒一次）", lambda: self._toggle_special(m)),
        ):
            act = QAction(text, menu)
            act.setToolTip(tip)
            act.triggered.connect(cb)
            menu.addAction(act)
        menu.addSeparator()

        # 设定本地版本（判决制第三扇门）：对账上在管的条目开放；
        # 已删除的先恢复再认定
        act_setver = QAction("设定本地版本…", menu)
        act_setver.setEnabled(m.status in (STATUS_TRACKED, STATUS_DOWNLOADED))
        act_setver.setToolTip(
            "不经终端判决、由你手工认定本地版本（最重的人工背书）："
            "补录确认、修正记错的版本时用。\n"
            "版本时间 = 该版本在工坊的更新时间（工坊页面「最后更新」"
            "可查，精确到分钟即可）。判决史会记一条「手动设定」")
        act_setver.triggered.connect(lambda: self._set_manual_version(m))
        menu.addAction(act_setver)
        # 手动备份（单条）：经既有 backup_requested 信号 → 主窗口转
        # 备份页开批次（备份页自理版本未知守卫）
        act_backup = QAction("手动备份…", menu)
        act_backup.setEnabled(m.status == STATUS_DOWNLOADED)
        act_backup.setToolTip(
            "为这个 mod 做一次备份（把当前已确认版本完整复制一份）："
            "交给【备份与恢复】页执行。只有「已下载」可备；本地版本"
            "还是「未知」的会被备份守卫拒绝——先设定版本")
        act_backup.triggered.connect(
            lambda: self.backup_requested.emit(self._game.app_id, [mid]))
        menu.addAction(act_backup)

        # 获取下载命令：跳命令生成页并聚焦本 mod——命令文本的单源在
        # 命令引擎，这里绝不手拼 steamcmd 行（防止两处口径漂移）
        act_cmd = QAction("获取下载命令…", menu)
        act_cmd.setEnabled(m.status in (STATUS_TRACKED, STATUS_DOWNLOADED))
        act_cmd.setToolTip(
            "跳到【下载命令生成】页并聚焦这个 mod——在那里复制它的"
            "下载命令（登录行 + 下载行，口径与批量命令完全一致）")
        act_cmd.triggered.connect(
            lambda: self.command_gen_requested.emit([mid]))
        menu.addAction(act_cmd)
        # 复制下载命令（单条直取）：文本单源 core.commandBuilder，
        # 与命令生成页同一拼装口径；含登录行的整段批量文本在命令生成页
        act_copy_cmd = QAction("复制下载命令", menu)
        act_copy_cmd.setEnabled(
            m.status in (STATUS_TRACKED, STATUS_DOWNLOADED))
        act_copy_cmd.setToolTip(
            "把这一条的下载命令（workshop_download_item …）直接复制进"
            "剪贴板——粘贴到底部终端（steamcmd）回车即可。\n"
            "不含登录行：先在终端登录，或用命令生成页的「复制登录命令」"
            "按钮。要整段批量命令，用上面的「获取下载命令…」")
        act_copy_cmd.triggered.connect(
            lambda: self._copy_download_command(m))
        menu.addAction(act_copy_cmd)

        if m.status == "deleted":
            act_restore = QAction("恢复（取消软删除）…", menu)
            act_restore.setToolTip(
                "把这条已删除记录放回正常管理：有确认版本 → 回「已下载」；"
                "没有 → 回「待下载」。盘上文件不会被动，缺了的话"
                "账实核验/盘点会如实报出")
            act_restore.triggered.connect(lambda: self._restore_deleted(m))
            menu.addAction(act_restore)

        act_del = QAction("软删除…", menu)
        act_del.setEnabled(m.status != "deleted")  # 已删除的不重复删
        act_del.setToolTip(
            "标记为已删除（记录保留，可随时恢复）；删除后更新检测不再"
            "查询、命令生成不再列出")
        act_del.triggered.connect(lambda: self._soft_delete(m))
        menu.addAction(act_del)

        # 彻底清账：只对已删除/待下载开放——「已下载」的完整出口
        # （删文件+清账/仅删文件）在【清理与删除】页
        if m.status in ("deleted", STATUS_TRACKED):
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

    # ---------- 单条动作 ----------
    @staticmethod
    def _resolve_url(m) -> str:
        """mod 的工坊网址：存了用存的；没存按 urlParser 模板现拼
        （决策 61④ 模板单源）。"""
        u = (getattr(m, "url", "") or "").strip()
        return u if u else WORKSHOP_URL_TEMPLATE.format(m.mod_id)

    def _edit_note(self, m) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "编辑备注", f"mod {m.mod_id} 的备注：",
            getattr(m, "note", "") or "")
        if ok:
            self._repo.set_note(m.mod_id, text.strip() or None)
            self._reload()

    def _open_mod_folder(self, m) -> None:
        """「打开 mod 文件夹」：实现单源 gui/modFolderOpener。"""
        open_mod_folder(self, self._game, m, log=self._log)

    def _open_folder_from_detail(self, mod_id: int) -> None:
        """详情面板「打开 mod 文件夹」请求：按编号取回 Mod 再走单源。"""
        m = self._repo.get_mod(mod_id)
        if m is not None:
            self._open_mod_folder(m)

    def _pick_color(self, m) -> None:
        names = [*COLOR_CHOICES.keys(), "（自定义颜色…）", "（清除标记）"]
        name, ok = QInputDialog.getItem(
            self, "颜色标记", f"mod {m.mod_id}：",
            names, current=0, editable=False)
        if not ok:
            return
        tag = None
        if name == "（自定义颜色…）":
            c = QColorDialog.getColor(
                _color_qcolor(m) or QColor("#808080"),
                self, "自定义颜色")
            if not c.isValid():
                return  # 取色器里取消了：什么都不改
            tag = c.name()  # 规范形 #rrggbb，D26：任意 hex 照认
        elif not name.startswith("（"):
            tag = COLOR_CHOICES.get(name)
        self._repo.set_color_tag(m.mod_id, tag)
        self._reload()

    def _toggle_special(self, m) -> None:
        cur = bool(getattr(m, "special", getattr(m, "is_special", False)))
        self._repo.set_special(m.mod_id, not cur)
        self._reload()

    def _copy_id(self, mid: int) -> None:
        QApplication.clipboard().setText(str(mid))
        self._log.info(f"已复制编号 {mid}")
    def _copy_download_command(self, m) -> None:
        """「复制下载命令」（右键单条）：命令文本走单源
        core.commandBuilder.build_copy_text——页面绝不手拼 steamcmd 行，
        今后改命令口径只动 core 一处，这里与命令生成页永远一致。"""
        text = build_copy_text(self._game.app_id, [m.mod_id])
        QApplication.clipboard().setText(text)
        self._log.ok(
            f"mod {m.mod_id} 的下载命令已复制进剪贴板：{text.strip()}"
            "（不含登录行）")

    def _set_manual_version(self, m) -> None:
        """「设定本地版本…」（单条）：共用对话框收集时间，写库走
        repo.set_manual_version 正门——来源与判决史由账本管，页面只管
        收集一个诚实的时间（秒口径 = 59，见 ManualVersionDialog）。"""
        dlg = ManualVersionDialog([(m.mod_id, m.title)], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        version = dlg.result_version()
        self._repo.set_manual_version(m.mod_id, version)
        self._log.ok(
            f"mod {m.mod_id} 已手工设定本地版本"
            + ("（版本未知）" if version is None else f"：{abs_time(version)}"))
        self._reload()

    def _soft_delete(self, m) -> None:
        ret = QMessageBox.question(
            self, "软删除",
            f"确定将「{m.title or m.mod_id}」标记为已删除？\n"
            "只动账本：记录保留，随时可恢复。\n"
            "硬盘不动：下载的内容文件夹、备份文件、备份登记全部原样。\n"
            "软删除后：更新检测不再查询它、下载命令生成不再列出它；"
            "本页状态筛选选「已删除」仍能找到它。\n"
            "（这里是可恢复的软删除；不可恢复的「彻底清账」在"
            "「清理与删除」页，执行前另有确认。）")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._repo.mark_deleted(m.mod_id, self._last_state_of(m))
        self._reload()

    @staticmethod
    def _last_state_of(m) -> dict:
        """软删除前快照的组装（单条/批量两路共用单源）。字段用 getattr
        宽容读取：repo 把它作证据存档，多给的键无害，少给的键才致命。"""
        return {
            "title": getattr(m, "title", None),
            "url": getattr(m, "url", None),
            "time_updated": getattr(m, "time_updated", None),
            "remote_timeupdated": getattr(m, "remote_timeupdated", None),
            "confirmed_version": getattr(m, "confirmed_version", None),
            "confirmed_source": getattr(m, "confirmed_source", None),
            "local_size": getattr(m, "local_size", None),
            "note": getattr(m, "note", None),
            "color": _color_name(m),
            "special": bool(getattr(m, "special",
                                    getattr(m, "is_special", False))),
        }

    def _restore_deleted(self, m) -> None:
        """「恢复」：走 repo.mark_restored——目标状态由账本按
        "有没有确认版本"回推，页面不做推断（V2 契约）。"""
        ret = QMessageBox.question(
            self, "恢复记录",
            f"把「{m.title or m.mod_id}」恢复为正常条目？\n"
            "有确认版本 → 回「已下载」；没有 → 回「待下载」。\n"
            "盘上文件不会被动，缺了的话账实核验/盘点会如实报出。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._repo.mark_restored(m.mod_id)
        self._log.info(f"mod {m.mod_id} 已恢复为正常条目")
        self._reload()

    # ---------- 批量动作 ----------

    def _require_game(self) -> bool:
        """操作菜单各入口的公共守卫：没选档案时弹窗指路，不静默。"""
        if self._game is not None:
            return True
        QMessageBox.information(
            self, "请先选择档案",
            "这个操作按当前游戏档案执行——请先在左上角添加或选择游戏档案。")
        return False

    def _on_softdel_checked(self) -> None:
        """【软删除选中项】：只收看得见的勾选，一次确认整批执行
        （软删除可恢复，不必逐条问）；已删除的跳过（幂等）。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "软删除选中项", "先在表格第一列勾选要软删除的 mod。")
            return
        targets, skipped = [], []
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue  # 勾选与落库之间刚被清（极端竞态）：跳过
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
            "只动账本：记录保留，随时可逐个恢复（状态筛选「已删除」"
            "能找到，右键「恢复」）。\n"
            "硬盘不动：下载内容、备份文件、备份登记全部原样。\n"
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
        """【彻底清账选中项】：物理删除账本记录，逐条走 repo.purge_mod
        正门（每条独立事务，一条被拒不挡其余）；「已下载」条目跳过
        （完整出口在清理与删除页）。成功后统一断根（通知 steamcmd
        忘记这些条目）。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "彻底清账选中项", "先在表格第一列勾选要清账的 mod。")
            return
        targets, skipped_downloaded = [], 0
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
        box = QMessageBox(self)
        box.setWindowTitle("彻底清账选中项")
        box.setIcon(QMessageBox.Icon.Question)
        text = (
            f"确定将勾选的 {len(targets)} 个 mod 的记录彻底清除？\n\n"
            "· 记录、版本快照、特别关注提醒：物理删除，不可恢复；\n"
            "· 磁盘：一个字节不动——下载内容、备份文件夹全部原样"
            "（盘上还有内容目录的话，之后会以「孤儿目录」出现在"
            "【异常处理】页，可再处置）；\n"
            "· 自动登记「已清账」黑名单：今后不会再把这些编号录入账本"
            "（想恢复收录，到【已清账管理】页允许录入）；\n"
            "· 清账成功后自动通知 steamcmd 忘记这些条目"
            "（防复活占盘；steamcmd 正在运行时跳过，可稍后到"
            "【已清账管理】页执行）；\n"
            "· 失效归档与操作日志保留作历史证据。\n")
        if skipped_downloaded:
            text += (f"\n另有 {skipped_downloaded} 个「已下载」条目已跳过"
                     "——它们的完整出口（含删文件选项）在「清理与删除」页。")
        box.setText(text)
        cb = QCheckBox("同时删除备份登记（磁盘上的备份文件夹仍保留）", box)
        cb.setToolTip("不勾：名下有备份登记的条目会被跳过（备份去留必须"
                      "先有决策）；\n勾：连带删除备份登记，磁盘备份文件夹"
                      "照旧保留")
        box.setCheckBox(cb)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        b_yes = box.addButton("彻底清账",
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_no)  # 默认永远保守
        box.exec()
        if box.clickedButton() is not b_yes:
            self._log.info("已取消批量彻底清账：没有做任何改动")
            return
        done_ids, blocked, freed_regs = [], [], 0
        for m in targets:
            try:
                n = self._repo.purge_mod(m.mod_id,
                                         purge_backups=cb.isChecked())
                done_ids.append(m.mod_id)
                freed_regs += n or 0
            except ValueError:
                # 引擎的保护性拒绝（名下有备份登记且没勾"同时删除"等）：
                # 跳过，不让一条挡住其余
                blocked.append(m.mod_id)
        self._log.warn(
            f"已彻底清账 {len(done_ids)} 个：记录物理删除、自动登记黑名单；"
            "磁盘文件未动"
            + (f"，连带删除备份登记 {freed_regs} 份" if freed_regs else "")
            + (f"；跳过 {len(blocked)} 个（名下有备份登记未一并删除——"
               "可到【清理与删除】页处置，或右键单条勾选后再来）"
               if blocked else ""))
        if skipped_downloaded:
            self._log.info(
                f"另有 {skipped_downloaded} 个「已下载」条目未清："
                "它们的完整出口在「清理与删除」页")
        self._acf_cleanup_after_purge(done_ids)
        if self._selected_mod_id in done_ids:
            self._selected_mod_id = None
        self._reload()

    def _purge_record(self, m) -> None:
        """右键「彻底清账」：账本条目物理删除的终结出口（单条版）。
        failed 归档与操作日志刻意存活——异常处理页会以「已清账」历史
        归档收档。清账成功后断根。"""
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
            "· 清账成功后自动通知 steamcmd 忘记这个条目——它从此不再"
            "把它装配回来，复活彻底断根（原记账文件自动备份；steamcmd "
            "正在运行时跳过，可稍后到【已清账管理】页执行）；\n"
            "· 失效归档与操作日志保留作历史证据。")
        cb = QCheckBox("同时删除备份登记（磁盘上的备份文件夹仍保留）", box)
        cb.setToolTip("不勾：备份登记保留，成为无主登记；\n"
                      "勾：只删登记，磁盘备份文件夹照旧保留")
        box.setCheckBox(cb)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        b_yes = box.addButton("彻底清账",
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_no)
        box.exec()
        if box.clickedButton() is not b_yes:
            self._log.info("已取消彻底清账：没有做任何改动")
            return
        try:
            n_backups = self._repo.purge_mod(m.mod_id,
                                             purge_backups=cb.isChecked())
        except ValueError as exc:
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
        self._acf_cleanup_after_purge([m.mod_id])
        self._selected_mod_id = None  # 详情面板正显示的就是它，清掉
        self._reload()

    def _acf_cleanup_after_purge(self, mod_ids: list[int]) -> None:
        """清账后的断根：通知 steamcmd 忘记这些条目，防止它把已清账的
        mod 重新装配回来占盘。实现单源 core/steamPaths
        （locate_acf + remove_items_from_acf，原记账文件自动备份）。
        安全边界：steamcmd 在跑 → 跳过（它退出时会把内存里的旧账整个
        写回，现在改等于白改）；没配路径 / 该游戏从没在这台 steamcmd
        下载过 → 静默或说明跳过。"""
        if not mod_ids:
            return
        if steamcmd_running():
            self._log.warn(
                "steamcmd 正在运行：本次清账的条目暂未通知它忘记"
                "——运行中改会在其退出时被整个覆盖回去；稍后到"
                "【已清账管理】页执行")
            return
        root = self._steamcmd_root()
        if root is None:
            self._log.warn(
                "未设置 steamcmd 程序路径：本次清账的条目未通知 steamcmd "
                "忘记（可稍后到【已清账管理】页执行）")
            return
        game = self._game
        if game is None:
            return
        acf = steamPaths.locate_acf(root, game.app_id)
        if acf is None:
            return  # 该游戏从没在这台 steamcmd 下载过：无账可清，不算异常
        try:
            res = steamPaths.remove_items_from_acf(acf, mod_ids)
            removed = res[0] if isinstance(res, tuple) else res
        except ValueError as exc:
            self._log.error(f"通知 steamcmd 忘记条目失败（条目未动）：{exc}")
            return
        if removed:
            self._log.ok(
                f"已通知 steamcmd 忘记 {len(removed)} 个已清账条目"
                "（复活彻底断根；原记账文件已自动备份在同目录）")

    def _steamcmd_root(self) -> Path | None:
        """设置页的 steamcmd 程序路径 → steamcmd 根目录（页内自算，
        兼容"填 exe 完整路径"与"填文件夹"两种口径）。"""
        raw = str(self._settings.get("steamcmd_path") or "").strip()
        raw = raw.strip('"').strip()
        if not raw:
            return None
        p = Path(raw)
        if p.suffix.lower() == ".exe":
            p = p.parent
        return p if p.is_dir() else None

    # ---------- 批量动作（其余） ----------

    def _set_special_checked(self, value: bool) -> None:
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
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
                    "未收录条目需先入库才能标记：可到「分步向导 → 加入新 "
                    "mod」或【入账中心 · 登记】粘贴同一份清单")
            self._reload()

    def _on_batch_note(self) -> None:
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
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
                f"{mode.rstrip('…')}（确定后对 {len(ids)} 个 mod 生效）：",
                "")
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
                old = getattr(m, "note", "") or ""
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
    def _on_batch_set_version(self) -> None:
        """【批量设定本地版本…】：勾选的 mod 整批设定同一个版本时间。
        逐条走 repo.set_manual_version 正门（R17 第三扇门），每条独立
        事务、一条被拒不挡其余（与批量彻底清账同一容错口径）；
        时间收集与 59 秒口径都在共用对话框 ManualVersionDialog。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "批量设定本地版本",
                "先在表格第一列勾选要设定的 mod。")
            return
        targets, skipped_deleted = [], []
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue  # 勾选与落库之间刚被清账（极端竞态）：跳过
            if m.status == "deleted":
                skipped_deleted.append(mid)
            else:
                targets.append(m)
        if not targets:
            QMessageBox.information(
                self, "批量设定本地版本",
                "勾选的条目都是「已删除」状态——设定版本前请先恢复"
                "（状态筛选选「已删除」→ 右键「恢复」）。")
            return
        dlg = ManualVersionDialog([(m.mod_id, m.title) for m in targets], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._log.info("已取消批量设定本地版本：没有做任何改动")
            return
        version = dlg.result_version()
        done, blocked = [], []
        for m in targets:
            try:
                self._repo.set_manual_version(m.mod_id, version)
                done.append(m.mod_id)
            except ValueError as exc:
                blocked.append((m.mod_id, str(exc)))
        self._log.ok(
            "已批量设定本地版本"
            + ("（版本未知）" if version is None else f"：{abs_time(version)}")
            + f"：{len(done)} 个"
            + (f"；跳过已删除 {len(skipped_deleted)} 个" if skipped_deleted else "")
            + (f"；被拒 {len(blocked)} 个" if blocked else ""))
        for mid, reason in blocked:
            self._log.warn(f"mod {mid} 设定被拒：{reason}")
        self._reload()
    def _on_batch_restore(self) -> None:
        """【批量恢复】：勾选的「已删除」条目整批恢复。走
        repo.mark_restored（目标状态由账本按"有没有确认版本"回推，
        页面不推断）；非删除状态跳过；一个事务，要么全成要么原样。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "批量恢复", "先在表格第一列勾选要恢复的 mod。")
            return
        targets, skipped = [], 0
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue  # 勾选与落库之间刚被清账（极端竞态）：跳过
            if m.status == "deleted":
                targets.append(m)
            else:
                skipped += 1
        if not targets:
            QMessageBox.information(
                self, "批量恢复",
                "勾选里没有「已删除」状态的条目（状态筛选选「已删除」"
                "能找到它们）。")
            return
        n_dl = sum(1 for m in targets if m.confirmed_version is not None)
        n_tr = len(targets) - n_dl
        ret = QMessageBox.question(
            self, "批量恢复",
            f"把 {len(targets)} 个已删除条目恢复为正常管理？\n"
            f"有确认版本的 {n_dl} 个 → 回「已下载」；"
            f"没有的 {n_tr} 个 → 回「待下载」。\n"
            "盘上文件不会被动，缺了文件的之后由账实核验/盘点如实报出。"
            + (f"\n另有 {skipped} 个非删除状态的条目已跳过。" if skipped else ""))
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消批量恢复：没有做任何改动")
            return
        with self._repo.transaction():
            for m in targets:
                self._repo.mark_restored(m.mod_id)
        self._log.ok(
            f"已恢复 {len(targets)} 个（回「已下载」{n_dl}、"
            f"「待下载」{n_tr}）"
            + (f"；跳过非删除状态 {skipped} 个" if skipped else ""))
        self._reload()

    def _on_batch_revoke(self) -> None:
        """【批量撤销确认…】：repo.revoke_confirmation 的批量版
        （D8 唯一回滚点：确认三件套清空、status 不动、判决史保留，
        下轮检测重新报更新）。已删除的跳过——恢复去向取决于有无
        确认版本，先撤销会改变恢复结果；本就没确认的跳过（空操作）。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "批量撤销确认", "先在表格第一列勾选要操作的 mod。")
            return
        targets, skipped_del, already = [], 0, 0
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue
            if m.status == "deleted":
                skipped_del += 1
            elif m.confirmed_version is None:
                already += 1
            else:
                targets.append(m)
        if not targets:
            QMessageBox.information(
                self, "批量撤销确认",
                "勾选里没有已确认本地版本的条目，没有可撤销的。")
            return
        ret = QMessageBox.question(
            self, "批量撤销确认",
            f"确定撤销 {len(targets)} 个 mod 的本地版本确认？\n"
            "本地版本变「未知」、状态不变、判决史保留；下轮"
            "【更新检测】会把它们重新报出来。想恢复：重新设定版本"
            "（右键单条或操作菜单批量设定均可）。\n"
            + (f"另有已删除 {skipped_del} 个、本就没确认 {already} 个，"
               "均不受影响。" if (skipped_del or already) else ""))
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消批量撤销确认：没有做任何改动")
            return
        with self._repo.transaction():
            for m in targets:
                self._repo.revoke_confirmation(m.mod_id)
        self._log.ok(f"已撤销确认：{len(targets)} 个（本地版本清空、"
                     "状态未动；下轮检测重新报告）")
        self._reload()

    def _on_open_folders_checked(self) -> None:
        """【打开 mod 文件夹】：勾选 mod 的内容文件夹逐个在文件
        管理器打开。先按"下载目录\\编号"的既定路径规则做存在性
        预检（路径规则单源见 gui/modFolderOpener，这里只做防弹窗
        轰炸的预检），在盘的才交给单源 opener。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "打开 mod 文件夹", "先在表格第一列勾选 mod。")
            return
        base = (self._game.download_dir or "").strip()
        if not base:
            QMessageBox.information(
                self, "打开 mod 文件夹",
                "当前档案还没设置下载目录——请先到【编辑档案】补上。")
            return
        existing, missing = [], []
        for mid in ids:
            on_disk = (Path(base) / str(mid)).is_dir()
            (existing if on_disk else missing).append(mid)
        if not existing:
            QMessageBox.information(
                self, "打开 mod 文件夹",
                f"勾选的 {len(missing)} 个 mod 的内容文件夹都不在盘上"
                "（可能还没下载过或已清理）。")
            return
        ret = QMessageBox.question(
            self, "打开 mod 文件夹",
            f"在文件管理器打开 {len(existing)} 个 mod 内容文件夹？"
            f"（会连开 {len(existing)} 个窗口，多的话建议分批）"
            + (f"\n另有 {len(missing)} 个文件夹不在盘上，将跳过。"
               if missing else ""))
        if ret != QMessageBox.StandardButton.Yes:
            return
        opened = 0
        for mid in existing:
            m = self._repo.get_mod(mid)
            if m is not None:
                self._open_mod_folder(m)
                opened += 1
        self._log.ok(
            f"已打开 {opened} 个 mod 文件夹"
            + (f"；{len(missing)} 个不在盘上已跳过" if missing else ""))

    def _on_backup_checked(self) -> None:
        """【备份选中项】：把勾选编号经 backup_requested 交给主窗口 →
        备份页开备份批次（信号 mod 库轮留好、主窗口早已接线；备份页
        自理切档案、过滤可备份条目与版本未知守卫）。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "备份选中项", "先在表格第一列勾选要备份的 mod。")
            return
        self.backup_requested.emit(self._game.app_id, ids)

    def _on_batch_color(self) -> None:
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "批量颜色标记", "先在表格第一列勾选要标记的 mod。")
            return
        names = [*COLOR_CHOICES.keys(), "（自定义颜色…）", "（清除标记）"]

        name, ok = QInputDialog.getItem(
            self, "批量颜色标记",
            f"给勾选的 {len(ids)} 个 mod 设置颜色标记：",
            names, current=0, editable=False)
        if not ok:
            return
        tag = None
        if name == "（自定义颜色…）":
            c = QColorDialog.getColor(parent=self, title="自定义颜色")
            if not c.isValid():
                return
            tag = c.name()
        elif not name.startswith("（"):
            tag = COLOR_CHOICES.get(name)

        with self._repo.transaction():
            for mid in ids:
                self._repo.set_color_tag(mid, tag)
        self._log.ok(("已清除颜色标记" if tag is None
                      else f"已标记为「{name}」") + f"：{len(ids)} 个")
        self._reload()

    def _on_open_pages_checked(self) -> None:
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "打开工坊页面", "先在表格第一列勾选要打开的 mod。")
            return
        ret = QMessageBox.question(
            self, "打开工坊页面",
            f"在浏览器打开 {len(ids)} 个工坊页面？\n"
            "每个 mod 一个标签页；数量多时浏览器会连开一片，"
            "可先关掉多余页面再逐个处理。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 编号 → 网址是确定规则（urlParser 模板单源）：认领/手动设定
        # 入账的条目没有存储 url 也一律能开，不再挂"未打开"警告
        for mid in ids:
            QDesktopServices.openUrl(
                QUrl(WORKSHOP_URL_TEMPLATE.format(mid)))
        self._log.ok(f"已请求浏览器打开 {len(ids)} 个工坊页面")

    def _on_open_pages_paste(self) -> None:
        """【打开工坊页面（贴清单）…】：三步对话框。就地 import：
        本页唯一用点，免动文件头 import 区。"""
        from gui.batchOpenDialog import BatchOpenDialog
        dlg = BatchOpenDialog(self._repo, self, log=self._log)
        dlg.exec()

    def _on_copy_ids_checked(self) -> None:
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "复制勾选编号", "先在表格第一列勾选要复制的 mod。")
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in ids) + "\n")
        self._log.ok(f"已复制 {len(ids)} 个编号（每行一个）")
    def _on_copy_cmds_checked(self) -> None:
        """【复制勾选项下载命令】：勾选 mod 的下载命令整批复制进剪贴板。
        文本单源 core.commandBuilder.build_copy_text——与单条右键、
        命令生成页同一拼装口径，页面绝不手拼 steamcmd 行。已删除/已失败
        不出命令（引擎口径，与右键置灰一致），跳过并报数。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "复制勾选项下载命令",
                "先在表格第一列勾选要复制命令的 mod。")
            return
        targets, skipped = [], 0
        for mid in ids:
            m = self._repo.get_mod(mid)
            if m is None:
                continue  # 勾选与落库之间刚被清账（极端竞态）：跳过
            if m.status in (STATUS_TRACKED, STATUS_DOWNLOADED):
                targets.append(mid)
            else:
                skipped += 1
        if not targets:
            QMessageBox.information(
                self, "复制勾选项下载命令",
                "勾选里没有可出命令的条目（已删除/已失败不出命令）。")
            return
        text = build_copy_text(self._game.app_id, targets)
        QApplication.clipboard().setText(text)
        self._log.ok(
            f"已复制 {len(targets)} 条下载命令进剪贴板"
            + (f"；跳过已删除/已失败 {skipped} 个" if skipped else "")
            + "（不含登录行）")


    # ---------- 收尾 ----------
    def _on_download_checked(self) -> None:
        """【下载选中项】：把勾选编号经 download_requested 交给主窗口
        → 批次控制器开批。本页不认识控制器（跨页信号单向发出，与
        command_gen_requested 同风格）；steamcmd 未启动、已有批次
        在跑等检查控制器里都有，被拒原因进运行日志。"""
        if not self._require_game():
            return
        ids = self._visible_checked_ids()
        if not ids:
            QMessageBox.information(
                self, "下载选中项", "先在表格第一列勾选要下载的 mod。")
            return
        self.download_requested.emit(self._game.app_id, ids)

    def shutdown(self) -> None:
        """关窗收尾（MainWindow.closeEvent 的页面循环自动发现并调用）：
        摘除高级筛选对话框 conditions_changed → _reload 的连接。
        为什么必须摘：主窗口退出时本页持有的非模态对话框会被联动关闭，
        done() 里的关窗清空会广播 conditions_changed——而退出路上数据库
        已关闭，_reload 当场炸 ProgrammingError。摘信号即断链；正常使用
        中的关窗清空不受影响。"""
        try:
            self._adv_dialog.conditions_changed.disconnect(self._reload)
        except (RuntimeError, TypeError):
            pass  # 本就没连上/已断开——两种都算"已断链"，无害