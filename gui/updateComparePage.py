"""更新对照页
"""
r"""gui/updateComparePage.py · 当前档案的版本对照与更新历史。

【v2.43.5 "一条龙"大改（用户拍板）】
- "先读我"折叠卡（collapsibleSection 共享件，importPage 同款）：
  六段说明，含"更新 = 整个替换"机制——steamcmd 更新一个 mod =
  把旧的内容文件夹整个删掉、重新下载完整一份（实测事实），旧版本
  随之消失；想留后路先备份（日常更新选"备份+更新"或 mod 库右键
  手动备份），更新后可到【备份与恢复】页恢复；
- 分组视图（默认）升级为可折叠卡片：每 mod 一张 CollapsibleSection，
  组头 = 标题（编号）· 落后天数 · 快照数 · 最近时间；卡内 = 动作行
  （↗ 工坊 / 📁 文件夹 / 📝 备注 / 📋 复制编号）+ 备注行 + 快照小表。
  收展记忆存页面级字典（commandGenPage 同款，跨刷新/筛选重建存活）；
  容器末尾 addStretch + 高度按内容精确算（v2.41.1 两条教训照抄；
  高度不设上限只设下限——快照保留条数是用户可调项，设大了宁可
  卡片高、绝不裁行）；
- 平铺视图加"备注"列；两视图共用筛选：状态下拉 + 关键词（300ms
  防抖，即时只隐藏，搜标题/编号/备注）+ 只看落后（仅平铺）+
  排序下拉（仅分组：最近快照/标题/编号）；平铺保留表头点击排序；
- 右键菜单（平铺表格）：打开工坊页面 / 打开 mod 文件夹 / 复制编号 /
  编辑备注；分组卡片把同一组动作做成常驻按钮（卡上右键体验差，
  按钮更直白——动作口径与平铺右键完全一致）；
- 唯一新写操作 = 编辑备注（repo.set_note 正门，决策 8；mod 库页
  右键同款语义），其余零写库零线程。

【打开文件夹的路径推导】与 modListPage._open_mod_folder 同款两路
  候选（local_path 历史预留恒空 → download_dir/编号 实际唯一机制，
  决策 21③）；此处为镜像实现，收敛归打包前收官轮
  （WORKSHOP_URL_TEMPLATE 八份归一先例）。
 【v2.44 · 0 快照条目不显示（用户拍板）】分组视图只为有版本快照的
 mod 出卡片——0 快照条目没有"历史"可看，空卡只有一句指引，纯属噪音；
 隐藏数量进汇总行（"另有 N 个…"），全被过滤时空态另有专门文案。
 平铺视图不受影响：现状对照比的是两本版本账，不是快照——刚检测过
 一次的 mod（首次检测只立基线不拍快照）在平铺照样能看落后。

【契约不变】set_game / refresh；MainWindow 零改动。
"""
import time
from gui.modFolderOpener import open_mod_folder
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import Game
from gui.collapsibleSection import CollapsibleSection
from gui.consolePanel import LogBus
from gui.formatters import abs_time, status_zh

# 状态色（备份/删除页同源口径）
_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"

_SEC_MIN_H = 150          # 分区高度下限（只设下限不设上限：快照条数用户可调）
_ROW_H = 26               # 快照小表每行
_DEBOUNCE_MS = 300

# ---- 现状对照（平铺）列 ----
(C_URL, C_TITLE, C_MID, C_STATUS, C_NOTE,
 C_LOCAL, C_REMOTE, C_LAG) = range(8)
_COLS_FLAT = ["↗", "mod 标题", "mod 编号", "状态", "备注",
              "本地版本", "远端更新", "落后"]
# ---- 分组卡片里的快照小表列 ----
_COLS_SNAP = ["快照时间", "当时远端版本", "当时本地版本"]
# ---- 排序下拉 → 平铺表格 (列, 方向) 映射（v2.43.7：下拉两视图通用）----
# "recent"（快照时间）在平铺没有对应列 → 以远端更新近似（tooltip 注明）
_FLAT_SORT_MAP = {
    "recent": (C_REMOTE, Qt.SortOrder.DescendingOrder),
    "remote_desc": (C_REMOTE, Qt.SortOrder.DescendingOrder),
    "remote_asc": (C_REMOTE, Qt.SortOrder.AscendingOrder),
    "local_desc": (C_LOCAL, Qt.SortOrder.DescendingOrder),
    "local_asc": (C_LOCAL, Qt.SortOrder.AscendingOrder),
    "title": (C_TITLE, Qt.SortOrder.AscendingOrder),
    "id": (C_MID, Qt.SortOrder.AscendingOrder),
}

_GUIDE_PARAS = (
    "这页是什么：当前档案全部 mod 的版本对照与更新历史。两种看法——"
    "【按 mod 分组】= 每个 mod 一张卡，卡里是它最近几次版本变化的"
    "时间线；【现状对照】= 每个 mod 一行，看本地版本比远端旧了多久。",

    "「更新 = 整个替换」：steamcmd 更新一个 mod 时不是打补丁，而是把"
    "旧的内容文件夹整个删掉、重新下载完整一份——更新完成后，旧版本"
    "就不在磁盘上了。想留后路：用【日常更新】模块选“备份+更新”，或在"
    "【mod 库】页右键手动备份；更新后随时可到【备份与恢复】页恢复旧"
    "版本。所以“落后天数”越大，越值得先备份再更新。",

    "「落后」怎么看：现状对照里，落后 = 远端更新时间比本地版本时间早"
    "了几天（0 天显示“最新”）；“—” = 版本未知（还没下载过、或还没跑过"
    "【更新检测】）。分组卡片的标题上也标了每个 mod 目前的落后天数。",
    "想找“某天更新了哪些”：卡片标题上写着两个日期——「远端」= 作者"
    "发布现在这版的日子，「本地」= 你手里这份的版本日子。排序下拉选"
    "「远端更新 新→旧」从新往旧翻，或直接在过滤框输入日期（如 "
    "2026-09-26；输 2026-09 就是整个 9 月）。注意这是“版本是哪天的”"
    "——你哪天跑的检测/下载，看卡片里快照表的「快照时间」。",

    "数据从哪来：快照 = 每次【更新检测】/【扫描本地】落库时自动拍下的"
    "“当时的远端版本 + 当时的本地版本”。每个 mod 只滚动保留最近几条"
    "（设置页「快照保留条数」可调）——所以这里是最近的更新史，不是全史。",

    "能做什么动作：平铺视图在行上右键；分组卡片上是常驻按钮——"
    "打开工坊页面、打开 mod 文件夹、复制编号、编辑备注（备注保存后"
    "立即入库，【mod 库】页同步可见）。",

    "本页除了备注以外全只读。要更新请去【下载命令生成】勾选复制；"
    "更新完回来点【刷新】看新快照。",
)


class _SortItem(QTableWidgetItem):
    """显示给人看的文本、比较用原始值（备份页同款）。"""

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem):
            return self._key < other._key
        return super().__lt__(other)


class UpdateComparePage(QWidget):
    """更新对照页：除备注外只读；set_game 即重算，refresh 供进页钩子。"""

    def __init__(self, repo, parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._log = log or LogBus()
        self._game: Game | None = None
        self._view_mode = "grouped"          # 默认按 mod 分组
        self._group_sort = "recent"          # recent / title / id
        self._sort_state = (C_LAG, Qt.SortOrder.DescendingOrder)
        self._sections: dict[int, CollapsibleSection] = {}
        self._sec_state: dict[int, bool] = {}  # 收展记忆，跨重建存活
        self._flat_rows: list = []           # 平铺当前行（右键定位用）
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("更新对照", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        btn = QPushButton("刷新", self)
        btn.setToolTip("重新读取当前档案的 mod 与版本快照")
        btn.clicked.connect(self._reload)
        head.addWidget(btn)
        root.addLayout(head)

        tip = QLabel(
            "两种看法：按 mod 分组 = 每个 mod 的版本变化时间线（卡片可"
            "折叠）；现状对照 = 每个 mod 一行，看落后多少天。"
            "除备注外全页只读——详细说明点下方【先读我】。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        root.addWidget(self._make_guide())

        # ---- 工具条：视图 / 排序 / 状态 / 关键词 / 只看落后 ----
        bar = QWidget(self)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel("视图：", bar))
        self._view_combo = QComboBox(bar)
        self._view_combo.addItem("按 mod 分组（更新历史）")
        self._view_combo.addItem("现状对照（本地 ↔ 远端）")
        self._view_combo.setToolTip(
            "按 mod 分组 = 每个 mod 一张可折叠卡片，看它的版本变化；\n"
            "现状对照 = 每个 mod 一行，看本地比远端旧了多久")
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        h.addWidget(self._view_combo)

        self._sort_combo = QComboBox(bar)
        for label, data in (("最近记录 新→旧", "recent"),
                            ("远端更新 新→旧", "remote_desc"),
                            ("远端更新 旧→新", "remote_asc"),
                            ("本地版本 新→旧", "local_desc"),
                            ("本地版本 旧→新", "local_asc"),
                            ("标题 A→Z", "title"),
                            ("编号 从小到大", "id")):
            self._sort_combo.addItem(label, data)

        self._sort_combo.setToolTip(
            "两个视图都生效——分组视图：卡片排列顺序；\n"
            "平铺视图：表格排序（点表头可随时改）。\n"
            "「最近记录」在平铺按远端更新排（平铺没有快照列）")

        self._sort_combo.currentIndexChanged.connect(
            self._on_sort_combo_changed)
        h.addWidget(self._sort_combo)

        self._status_combo = QComboBox(bar)
        for label, data in (("全部状态", None), ("已下载", "downloaded"),
                            ("已收录", "tracked"), ("已失败", "failed")):
            self._status_combo.addItem(label, data)
        self._status_combo.setToolTip("只看某个状态的 mod（两个视图都生效）")

        self._status_combo.currentIndexChanged.connect(
            lambda _i: self._reload())
        h.addWidget(self._status_combo)

        self._kw_edit = QLineEdit(bar)
        self._kw_edit.setClearButtonEnabled(True)
        self._kw_edit.setPlaceholderText("按标题 / 编号 / 备注 / 日期（如 2026-09-26）过滤…（即时生效，只隐藏）")
        self._kw_edit.setToolTip(
            "标题、编号、备注、远端/本地版本日期，包含就保留；\n输 2026-09-26 查某天、输 2026-09 查整月；清空恢复全部")
        self._kw_edit.textChanged.connect(lambda *_: self._debounce.start())
        h.addWidget(self._kw_edit, 1)

        self._stale_check = QCheckBox("只看落后的", bar)
        self._stale_check.setToolTip(
            "现状对照视图下只显示本地版本落后于远端的 mod")
        self._stale_check.toggled.connect(lambda _v: self._reload())
        h.addWidget(self._stale_check)
        root.addWidget(bar)

        # ---- 两视图容器（QStackedWidget 切换；隐藏页不占布局份额，
        # 踩坑㉖ 的"隐藏不释放 stretch 份额"用 stack 天然规避）----
        self._stack = QStackedWidget(self)

        body = QWidget(self._stack)
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(6)
        scroll = QScrollArea(self._stack)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        self._stack.addWidget(scroll)

        self._table = QTableWidget(0, len(_COLS_FLAT), self._stack)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(False)
        self._table.horizontalHeader().sortIndicatorChanged.connect(
            self._on_sort_changed)
        # 右键菜单（平铺）：CustomContextMenu + 行定位按数据不按行号
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_menu)
        self._stack.addWidget(self._table)
        self._stack.setCurrentIndex(0)
        root.addWidget(self._stack, 1)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        # 关键词防抖：停手 300ms 才重建（连续打字不闪屏）
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._reload)

    def _make_guide(self) -> CollapsibleSection:
        """先读我折叠卡（importPage 同款契约：set_content 一次、高度
        固定、内容包 QScrollArea 卡内滚动）。默认收起。"""
        body = QWidget(self)
        bv = QVBoxLayout(body)
        bv.setContentsMargins(4, 4, 4, 4)
        bv.setSpacing(6)
        for text in _GUIDE_PARAS:
            lbl = QLabel(text, body)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: #8a8a8f; font-size:12px;")
            bv.addWidget(lbl)
        bv.addStretch(1)
        wrap = QScrollArea(self)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        wrap.setWidget(body)
        sec = CollapsibleSection(
            "先读我：两种视图 · 更新=整个替换 · 落后天数 · 动作与备注", self)
        sec.set_content(wrap, 250)
        sec.set_expanded(False)
        return sec

    # ---------- 对外 ----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._reload()

    def refresh(self) -> None:
        """主窗口进页钩子：检测/扫描跑完切过来即最新。"""
        self._reload()

    # ---------- 数据 ----------

    def _visible_mods(self) -> list:
        """筛选后的现役 mod（不含软删除）：状态下拉 + 关键词
        （标题/编号/备注包含，casefold）。两视图共用同一份口径。"""
        mods = [m for m in self._repo.list_mods(self._game.app_id)
                if m.status != "deleted"]
        st = self._status_combo.currentData()
        if st:
            mods = [m for m in mods if m.status == st]
        kw = self._kw_edit.text().strip().casefold()
        if kw:
            mods = [m for m in mods
                    if kw in (m.title or "").casefold()
                    or kw in str(m.mod_id)
                    or kw in (m.note or "").casefold()
                    or kw in self._date_strs(m)]

        return mods

    @staticmethod
    def _lag_days(m) -> int | None:
        """本地比远端落后几天；任一版本未知 → None。"""
        if m.local_timeupdated and m.time_updated:
            return max((m.time_updated - m.local_timeupdated) // 86400, 0)
        return None

    def _reload(self) -> None:
        if self._game is None:
            self._clear_sections()
            self._empty_hint = QLabel("请先在左上角选择游戏档案。", self)
            self._empty_hint.setStyleSheet("color: gray;")
            self._body_layout.addWidget(self._empty_hint)
            self._body_layout.addStretch(1)
            self._table.setRowCount(0)
            self._summary.setText("请先在左上角选择游戏档案。")
            return
        if self._view_mode == "grouped":
            self._fill_grouped()
        else:
            self._fill_flat()

    # ---------- 视图一：分组卡片 ----------

    def _clear_sections(self) -> None:
        """清空卡片容器准备重建。收展记忆 _sec_state 刻意不清。
        stretch 是 spacer，takeAt 顺带清走、由填充方重加。"""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._sections = {}

    def _fill_grouped(self) -> None:
        self._stack.setCurrentIndex(0)

        self._stale_check.setEnabled(False)
        mods = self._visible_mods()
        pairs = []
        no_snap: list = []  # 0 快照的 mod：分组视图不显示（v2.44）。
        # 过滤住本层，不进 _visible_mods——那是
        # 两视图共用口径，平铺不受影响
        for m in mods:
            snaps = self._repo.list_snapshots(m.mod_id)
            snaps = sorted(snaps, key=lambda s: s.snapshot_at or 0, reverse=True)
            if snaps:  # v2.44 起分组只喂有快照的 mod，此处恒真

                pairs.append((m, snaps))
            else:
                no_snap.append(m)

        if self._group_sort == "title":
            pairs.sort(key=lambda p: (p[0].title or "").casefold())
        elif self._group_sort == "id":
            pairs.sort(key=lambda p: p[0].mod_id)
        elif self._group_sort == "remote_desc":
            pairs.sort(key=lambda p: p[0].time_updated or 0, reverse=True)
        elif self._group_sort == "remote_asc":
            pairs.sort(key=lambda p: p[0].time_updated or 0)
        elif self._group_sort == "local_desc":
            pairs.sort(key=lambda p: p[0].local_timeupdated or 0,
                       reverse=True)
        elif self._group_sort == "local_asc":
            pairs.sort(key=lambda p: p[0].local_timeupdated or 0)
        else:  # recent：最近一次记录 新→旧（原默认）
            pairs.sort(key=lambda p: p[1][0].snapshot_at if p[1] else 0,
                       reverse=True)
        self._clear_sections()
        if not pairs:
            # 两种空要分开说（v2.44）：全被"无快照"过滤 = 有 mod 但都
            # 还没有版本记录；真没 mod = 筛选太严或空档案
            if no_snap:
                hint = ("（筛出的 mod 都还没有版本快照，这里不显示空卡。"
                        "版本记录来自【更新检测】与【扫描本地】，"
                        "有记录的 mod 会出现在这里）")
            else:
                hint = ("（没有匹配当前筛选的 mod——清空关键词或状态试试；"
                        "或先跑【更新检测】/【扫描本地】）")
            self._empty_hint = QLabel(hint, self)
            self._empty_hint.setWordWrap(True)
            self._empty_hint.setStyleSheet("color: gray;")
            self._body_layout.addWidget(self._empty_hint)
        for m, snaps in pairs:
            self._make_section(m, snaps)
        # v2.41.1 主修复同款：容器末尾必须补 stretch，否则整页滚动区
        # 把内容页拉高、卡片跟着被拉高悬空
        self._body_layout.addStretch(1)
        total = sum(len(s) for _, s in pairs)
        tail = ""
        if no_snap:
            tail = (f"｜另有 {len(no_snap)} 个 mod 还没有版本快照，未显示"
                    "（版本记录来自【更新检测】/【扫描本地】）")
        self._summary.setText(
            f"当前筛出 {len(pairs)} 个 mod · 共 {total} 条快照"
            "（每 mod 滚动保留最近若干条——设置页「快照保留条数」可调，"
            "这里是最近的更新史，不是全史）" + tail)

    def _section_height(self, n_snap: int, has_rows: bool) -> int:
        """卡片内容高度：动作行 + 备注行 + 快照小表（表头+行数×行高）
        + 余量。只设下限不设上限——快照条数是用户可调项，设大了宁可
        卡片高（整页滚动兜底），绝不把行裁掉（v2.41.1 类教训）。"""
        if has_rows:
            h = 30 + 24 + (34 + n_snap * _ROW_H + 6) + 20
        else:
            h = 30 + 24 + 26 + 20
        return max(_SEC_MIN_H, h)

    def _make_section(self, m, snaps) -> None:
        days = self._lag_days(m)
        if days is None:
            lag_txt = ""
        elif days == 0:
            lag_txt = "· 最新 "
        else:
            lag_txt = f"· 落后 {days} 天 "
        remote_txt = abs_time(m.time_updated) if m.time_updated else "—"
        local_txt = (abs_time(m.local_timeupdated)
                     if m.local_timeupdated else "—")
        head_txt = (f"{m.title or '（无标题）'}（{m.mod_id}）· {lag_txt}"
                    f" · 远端 {remote_txt} · 本地 {local_txt}"
                    f" · {len(snaps)} 条快照")

        content = QWidget(self)
        cv = QVBoxLayout(content)
        cv.setContentsMargins(4, 4, 4, 4)
        cv.setSpacing(4)

        # ---- 动作行：与平铺右键同一组动作的常驻版 ----
        acts = QWidget(content)
        ah = QHBoxLayout(acts)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(6)
        b_url = QPushButton("↗ 工坊页面", acts)
        b_url.setToolTip("在浏览器打开该 mod 的创意工坊页面")
        b_url.setEnabled(bool(m.url))
        if m.url:
            b_url.clicked.connect(
                lambda _=False, u=m.url: self._open_url(u))
        b_dir = QPushButton("📁 打开文件夹", acts)
        b_dir.setToolTip(
            "在文件管理器打开该 mod 的下载内容文件夹（下载目录\\编号）")
        b_dir.clicked.connect(
            lambda _=False, mm=m: self._open_mod_folder(mm))
        b_note = QPushButton("📝 编辑备注", acts)
        b_note.setToolTip(
            "写点给自己看的话（为什么装它/替换了谁/坑在哪）；"
            "保存后 mod 库页同步可见")
        b_note.clicked.connect(
            lambda _=False, mm=m: self._edit_note(mm))
        b_copy = QPushButton("📋 复制编号", acts)
        b_copy.setToolTip(f"复制 mod 编号 {m.mod_id} 进剪贴板")
        b_copy.clicked.connect(
            lambda _=False, mm=m: self._copy_id(mm))
        for b in (b_url, b_dir, b_note, b_copy):
            ah.addWidget(b)
        ah.addStretch(1)
        cv.addWidget(acts)

        # ---- 备注行（决策 22④ 空态引导：没有也给个"怎么加"的指引）----
        if m.note:
            note_lbl = QLabel(f"备注：{m.note}", content)
        else:
            note_lbl = QLabel(
                "备注：（无——点【📝 编辑备注】给自己留句话）", content)
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet("color: gray; font-size: 12px;")
        cv.addWidget(note_lbl)

        # ---- 快照小表 / 无快照指引 ----
        if snaps:
            tbl = QTableWidget(len(snaps), 3, content)
            tbl.setHorizontalHeaderLabels(_COLS_SNAP)
            tbl.verticalHeader().setVisible(False)
            tbl.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            tbl.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Stretch)
            tbl.setColumnWidth(0, 160)
            tbl.setColumnWidth(1, 170)
            tbl.setFixedHeight(34 + len(snaps) * _ROW_H + 6)
            for r, s in enumerate(snaps):
                tbl.setItem(r, 0, QTableWidgetItem(
                    abs_time(s.snapshot_at) if s.snapshot_at else "—"))
                tbl.setItem(r, 1, QTableWidgetItem(
                    abs_time(s.time_updated) if s.time_updated else "—"))
                tbl.setItem(r, 2, QTableWidgetItem(
                    abs_time(s.local_timeupdated)
                    if s.local_timeupdated else "—"))
            cv.addWidget(tbl)


        sec = CollapsibleSection(head_txt, self)
        sec.set_content(content, self._section_height(len(snaps),
                                                      bool(snaps)))
        # expand_changed 先连、再设初值（commandGenPage 同款时序）
        sec.expand_changed.connect(
            lambda on, k=m.mod_id: self._sec_state.__setitem__(k, on))
        sec.set_expanded(self._sec_state.get(m.mod_id, False))
        self._sections[m.mod_id] = sec
        self._body_layout.addWidget(sec)

    # ---------- 视图二：现状对照（平铺） ----------

    def _fill_flat(self) -> None:
        self._stack.setCurrentIndex(1)

        self._stale_check.setEnabled(True)
        t = self._table
        t.setSortingEnabled(False)
        t.clearSpans()
        t.setColumnCount(len(_COLS_FLAT))
        t.setHorizontalHeaderLabels(_COLS_FLAT)
        header = t.horizontalHeader()
        header.setSectionResizeMode(C_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(C_NOTE, QHeaderView.ResizeMode.Stretch)
        for col, width in ((C_URL, 36), (C_MID, 110), (C_STATUS, 80),
                           (C_NOTE, 160), (C_LOCAL, 150),
                           (C_REMOTE, 150), (C_LAG, 90)):
            t.setColumnWidth(col, width)

        rows = [(m, self._lag_days(m)) for m in self._visible_mods()]
        if self._stale_check.isChecked():
            rows = [r for r in rows if r[1] is not None and r[1] > 0]
        self._flat_rows = rows

        t.setRowCount(len(rows))
        for i, (m, days) in enumerate(rows):
            self._fill_flat_row(i, m, days)
        # 灌完再开排序（重建时序纪律：先关后开），默认落后大→小
        header.setSortIndicatorShown(True)
        t.setSortingEnabled(True)
        col, order = self._sort_state
        if col >= len(_COLS_FLAT):
            col, order = C_LAG, Qt.SortOrder.DescendingOrder
        t.sortItems(col, order)

        n_stale = sum(1 for _, d in rows if d is not None and d > 0)
        n_old = sum(1 for _, d in rows if d is not None and d > 30)
        self._summary.setText(
            f"当前筛出 {len(rows)} 个｜版本落后 {n_stale} 个"
            f"（其中超 30 天 {n_old} 个）"
            + ("｜「—」= 版本未知（未下载或没检测过）"
               if any(d is None for _, d in rows) else ""))

    def _fill_flat_row(self, i: int, m, days: int | None) -> None:
        t = self._table
        url_item = QTableWidgetItem("↗")
        url_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        url_item.setForeground(QBrush(QColor("#5aa9ff")))
        url_item.setToolTip("点击在浏览器打开该 mod 的创意工坊页面；"
                            "行上右键有更多动作")
        self._set_item(i, C_URL, url_item, m)
        title = _SortItem(m.title or "（无标题）",
                          (m.title or "").casefold())
        title.setToolTip(
            f"{m.mod_id}　{m.title or '（无标题）'}"
            + (f"\n备注：{m.note}" if m.note else ""))
        self._set_item(i, C_TITLE, title, m)
        self._set_item(i, C_MID, _SortItem(str(m.mod_id), m.mod_id), m)
        self._set_item(i, C_STATUS, _SortItem(
            status_zh(m.status), m.status), m)
        note_item = _SortItem(m.note or "—", (m.note or "").casefold())
        if not m.note:
            note_item.setForeground(QBrush(QColor("gray")))
        note_item.setToolTip(m.note or "（无备注——右键【编辑备注】添加）")
        self._set_item(i, C_NOTE, note_item, m)
        self._set_item(i, C_LOCAL, _SortItem(
            abs_time(m.local_timeupdated) if m.local_timeupdated else "—",
            m.local_timeupdated or 0), m)
        self._set_item(i, C_REMOTE, _SortItem(
            abs_time(m.time_updated) if m.time_updated else "—",
            m.time_updated or 0), m)
        if days is None:
            item = _SortItem("—", -1)
            item.setForeground(QBrush(QColor("gray")))
        elif days == 0:
            item = _SortItem("最新", 0)
            item.setForeground(QBrush(QColor(_C_OK)))
        else:
            item = _SortItem(f"{days} 天", days)
            item.setForeground(QBrush(QColor(
                _C_WARN if days <= 30 else _C_FAIL)))
        item.setToolTip("本地版本比远端落后的大约天数（按天取整）")
        self._set_item(i, C_LAG, item, m)

    def _set_item(self, row: int, col: int, item, m) -> None:
        """每格都带 mod_id（UserRole+1）——右键按数据定位，不按行号
        （排序/重建后行号漂移，备份页纪律）。"""
        item.setData(Qt.ItemDataRole.UserRole + 1, m.mod_id)
        self._table.setItem(row, col, item)

    # ---------- 动作（平铺右键与卡片按钮同一组口径） ----------

    def _on_table_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        item = self._table.item(row, C_TITLE)
        if item is None:
            return
        mod_id = item.data(Qt.ItemDataRole.UserRole + 1)
        m = next((mm for mm, _d in self._flat_rows
                  if mm.mod_id == mod_id), None)
        if m is None:
            return
        menu = QMenu(self)
        menu.setToolTipsVisible(True)  # T19① 教训
        a_url = QAction("打开工坊页面", menu)
        a_url.setEnabled(bool(m.url))
        a_dir = QAction("打开 mod 文件夹", menu)
        a_copy = QAction("复制编号", menu)
        a_note = QAction("编辑备注…", menu)
        for a in (a_url, a_dir, a_copy, a_note):
            menu.addAction(a)
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is a_url:
            self._open_url(m.url)
        elif chosen is a_dir:
            self._open_mod_folder(m)
        elif chosen is a_copy:
            self._copy_id(m)
        elif chosen is a_note:
            self._edit_note(m)

    def _open_url(self, url: str) -> None:
        if QDesktopServices.openUrl(QUrl(url)):
            pass  # 纯浏览动作；失败才弹窗（v2.18 教训：openUrl 静默）
        else:
            QMessageBox.warning(
                self, "打开工坊页面",
                f"系统没有响应打开请求：\n{url}\n"
                "可复制网址粘到浏览器地址栏打开。")

    def _open_mod_folder(self, m) -> None:
        """实现单源 gui/modFolderOpener（与 mod 库页共用）。"""
        open_mod_folder(self, self._game, m, log=self._log)

    def _copy_id(self, m) -> None:
        QApplication.clipboard().setText(str(m.mod_id))
        self._log.ok(f"已复制编号 {m.mod_id}")

    def _edit_note(self, m) -> None:
        """编辑备注 = 本页唯一写操作（repo.set_note 正门，决策 8；
        mod 库页右键同款语义）。保存后整页重算，备注行/列即时可见。"""
        text, ok = QInputDialog.getMultiLineText(
            self, "编辑备注",
            f"mod {m.mod_id}（{m.title or '（无标题）'}）的备注——"
            "写给自己看的话（为什么装它、替换了谁、坑在哪）：",
            m.note or "")
        if not ok:
            return
        try:
            self._repo.set_note(m.mod_id, text.strip())
        except Exception as exc:
            self._log.error(f"写备注失败（mod {m.mod_id}）：{exc}")
            QMessageBox.warning(self, "编辑备注", f"保存失败：{exc}")
            return
        self._log.ok(f"已更新 mod {m.mod_id} 的备注")
        self._reload()

    # ---------- 小件 ----------
    def _on_view_changed(self, idx: int) -> None:
        self._view_mode = "flat" if idx == 1 else "grouped"
        if self._view_mode == "flat":
            # 切进平铺：以下拉当前项重排（表头点击随时可改——两处写
            # 同一个 _sort_state，谁后动听谁的）
            self._sort_state = _FLAT_SORT_MAP.get(
                self._sort_combo.currentData(),
                (C_LAG, Qt.SortOrder.DescendingOrder))
        self._reload()

    def _on_sort_combo_changed(self, _idx: int) -> None:
        """排序下拉（v2.43.7 两视图通用）：分组记卡片排序键；平铺换算
        成 (列, 方向) 写进 _sort_state——与表头点击同一落点，最后
        操作者生效，不打架。"""
        self._group_sort = self._sort_combo.currentData()
        if self._view_mode == "flat":
            self._sort_state = _FLAT_SORT_MAP.get(
                self._group_sort,
                (C_LAG, Qt.SortOrder.DescendingOrder))
        self._reload()


    def _on_sort_changed(self, col: int, order: Qt.SortOrder) -> None:
        self._sort_state = (col, order)
    @staticmethod
    def _date_strs(m) -> str:
        """远端/本地两个版本日期的原始串（%Y-%m-%d，本机时区）。
        刻意不走 abs_time：它可能对老日期出相对文案，日期过滤必须
        永远有"年-月-日"字面可对。包含匹配 → "2026-09" 整月、
        "09-26" 同月日跨年都命中，顺手白赚。"""
        parts = []
        for ts in (m.time_updated, m.local_timeupdated):
            if ts:
                parts.append(time.strftime("%Y-%m-%d",
                                           time.localtime(ts)))
        return "\n".join(parts)
