"""mod 库列表模型
"""
"""
gui/modListModel.py · mod 库页表格的数据模型（QTableView + 本模型）。

职责边界：本模型只管"一行 Mod → 一行格子"的显示转换（文字、颜色、
悬浮说明、排序请求），不查数据库、不弹窗——取数与右键操作都在
mod 库页（gui/modListPage），操作完把新数据喂回 set_rows / update_row。

V2 口径（动这里之前先读）：
- 本地版本 = 你确认过的版本（Mod.confirmed_version / confirmed_source）。
  V1 的 local_timeupdated 已随扫描机制退役——_SORT_MAP 里若还留着
  旧键名，点排序会撞 repo 白名单的 ValueError（这是本轮换过的刀口）；
- 「需更新」= D2 唯一公式的显示侧：已下载、且远端更新时间比你确认过
  的本地版本新。只有已下载的条目参与，待下载/已删除一律不参与；
- 来源显示词一律取 core/constants.CONFIRMED_SOURCE_ZH（单源），
  unverified / inherited_acf 两档灰色弱化；
- 斑马纹与颜色标记都在本模型画（BackgroundRole）：颜色标记 = 整行
  淡染、优先；未标记的奇数行用 gui/theme 的交替色——两处背景一个
  出处，不会互相打架。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QStandardItem,
    QStandardItemModel,
)

from core.constants import (
    COLOR_CHOICES,
    CONFIRMED_SOURCE_ZH,
    STATUS_DOWNLOADED,
)
from core.formatters import fmt_size, relative_time, status_zh
from gui.theme import zebra_colors

# ---- 列布局（单源：页面的列显隐、宽度记忆、详情联动都按这份编号）----
COL_CHECK = 0   # 勾选（下载用；勾选集合供命令生成/批量下载消费）
COL_COLOR = 1   # 颜色标记（整行淡染，此格显示色块）
COL_ID = 2      # 编号
COL_TITLE = 3   # 标题（特别关注加 ★ 前缀）
COL_STATUS = 4  # 状态
COL_LOCAL = 5   # 本地版本（确认时间 · 来源文字短词）
COL_REMOTE = 6  # 远端版本（最近一次检测到的工坊更新时间）
COL_SIZE = 7    # 大小（确认入账时回填的本地大小）
COL_NOTE = 8    # 备注
COLUMN_COUNT = 9

# 视图列 → 数据库排序键（必须是 repo.list_mods 的 order_by 白名单成员，
# 一字不差）。★ 本地版本的排序键 = confirmed_version。
_SORT_MAP = {
    COL_ID: "mod_id",
    COL_TITLE: "title",
    COL_STATUS: "status",
    COL_LOCAL: "confirmed_version",
    COL_REMOTE: "remote_timeupdated",
    COL_SIZE: "local_size",
}

# 状态 → 前景色（只有本模型在用，按 D37 准入规则留模块顶部不进
# constants）。取中性色，深浅主题都看得清。
_STATUS_FG = {
    "failed": "#e5484d",   # 已失败：红
    "deleted": "#8a8a8a",  # 已删除：灰
}
_NEED_UPDATE_FG = "#e5484d"  # 需更新（D2 成立）→ 远端版本格红字加粗
_DIM_FG = "#8a8a8a"          # 未验证/旧账 与"未知"统一灰

# 颜色值的宽容读取：库里的颜色可能是中文色名（V2 写入口径），也可能是
# 旧账迁移来的 hex 色值——统一归一成色名再消费，取色器/筛选/渲染三处
# 口径不因新旧库差异漂移。页面（modListPage）同源 import 本函数。
_HEX_TO_NAME = {v.lower(): k for k, v in COLOR_CHOICES.items()}


def _color_name(mod) -> str | None:
    """Mod 的颜色属性 → 色名（"红"…）；无标记 / 无法识别 → None。
    兼容 color 与 color_tag 两种字段名（V1/V2 过渡期双读）。"""
    raw = getattr(mod, "color", None) or getattr(mod, "color_tag", None)
    if not raw:
        return None
    raw = str(raw).strip()
    if raw in COLOR_CHOICES:
        return raw
    return _HEX_TO_NAME.get(raw.lower())

def _zebra_alt() -> QColor | None:
    """交替行底色：gui/theme.zebra_colors() 约定返回（基准色, 交替色）
    双色对（v2.28 颜色修复链）。取不到（异常/签名不符）返回 None =
    没有斑马纹，用系统默认表格底色，功能不受损。"""
    try:
        return QColor(zebra_colors()[1])
    except Exception:
        return None


class ModListModel(QStandardItemModel):
    """mod 库表格模型。

    信号：
    - sort_requested(列, 升降)：点表头时发出——排序在数据库做
      （repo.list_mods 的 order_by），模型绝不本地 sort。本地排序
      会打乱"筛选+排序"的口径，页面 _reload 是唯一取数口；
    - checked_changed(编号集合)：勾选变化即广播，页面刷新按钮可用性、
      转发给命令生成/批量下载等消费方。
    """

    sort_requested = Signal(int, int)
    checked_changed = Signal(set)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(COLUMN_COUNT)
        self.setHorizontalHeaderLabels(
            ["勾", "", "编号", "标题", "状态",
             "本地版本", "远端版本", "大小", "备注"])
        self._checked: set[int] = set()       # 勾选的 mod 编号集合
        self._row_by_id: dict[int, int] = {}  # 编号 → 行号（原位刷新用）
        self._alt = _zebra_alt()
        self.itemChanged.connect(self._on_item_changed)

    # ---------- 排序（点表头 → 数据库排序）----------

    def sort(self, column: int, order: int) -> None:
        """表头点击的落点：只发请求，不本地排序（理由见类 docstring）。
        视图的排序指示器本来就停在用户点的位置，页面重查后无需回设。"""
        self.sort_requested.emit(column, order)

    @staticmethod
    def sort_key(column: int) -> str | None:
        """列 → repo 的 order_by 键；该列不可排序返回 None。
        点排序报 ValueError = 这里和 repo 白名单脱钩了，先对这里。"""
        return _SORT_MAP.get(column)

    # ---------- 取数落点 ----------

    def set_rows(self, mods: list) -> None:
        """整表重建（页面 _reload 的落点）。勾选集合独立保存——被筛
        出去的勾选仍在集合里，筛回来勾选还在：用户勾的是 mod，不是行。"""
        self.setRowCount(0)
        self._row_by_id.clear()
        for mod in mods:
            self._append_row(mod)

    def update_row(self, mod) -> None:
        """单行原位刷新（右键操作后用，省一次整表重查）。"""
        row = self._row_by_id.get(mod.mod_id)
        if row is None:
            return
        for col, it in enumerate(self._build_items(mod, row)):
            self.setItem(row, col, it)

    def mod_id_at(self, row: int) -> int | None:
        """行号 → mod 编号（页面右键/选中联动用）。"""
        it = self.item(row, COL_ID)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def checked_ids(self) -> set[int]:
        return set(self._checked)

    def set_checked(self, ids: set[int]) -> None:
        """外部重设勾选集合（命令生成页回填用）。集合整体替换；
        在场的行同步勾选框，不在场的等筛回来自然显示。"""
        self._checked = set(ids)
        for r in range(self.rowCount()):
            it = self.item(r, COL_CHECK)
            mid = self.mod_id_at(r)
            if it is None or mid is None:
                continue
            want = (Qt.CheckState.Checked if mid in self._checked
                    else Qt.CheckState.Unchecked)
            if it.checkState() != want:
                it.setCheckState(want)  # 触发 _on_item_changed，幂等不重播
        self.checked_changed.emit(set(self._checked))

    # ---------- 行构建 ----------

    def _append_row(self, mod) -> None:
        row = self.rowCount()
        for col, it in enumerate(self._build_items(mod, row)):
            self.setItem(row, col, it)
        self._row_by_id[mod.mod_id] = row

    def _build_items(self, mod, row: int) -> list[QStandardItem]:
        """一行的全部格子。列顺序必须与 COL_* 编号一致（列布局单源）。"""
        bg = self._row_background(mod, row)
        items = [
            self._mk_check_item(mod),
            self._mk_color_item(mod, bg),
            self._mk_id_item(mod, bg),
            self._mk_title_item(mod, bg),
            self._mk_status_item(mod, bg),
            self._mk_local_item(mod, bg),
            self._mk_remote_item(mod, bg),
            self._mk_size_item(mod, bg),
            self._mk_note_item(mod, bg),
        ]
        return items

    def _row_background(self, mod, row: int) -> QBrush | None:
        """整行底色：颜色标记淡染优先；未标记的奇数行用交替色（斑马纹）。
        返回 None = 保持默认（偶数行）。"""
        name = _color_name(mod)

        if name in COLOR_CHOICES:
            c = QColor(COLOR_CHOICES[name])
            c.setAlpha(46)  # 淡染：色相在、字还看得清，选中高亮不受影响
            return QBrush(c)
        if self._alt is not None and row % 2 == 1:
            return QBrush(self._alt)
        return None

    @staticmethod
    def _plain(text: str, bg: QBrush | None, *,
               fg: QColor | None = None, tooltip: str = "") -> QStandardItem:
        """普通格子：不可编辑、可选中；底色/前景色/悬浮说明按需。"""
        it = QStandardItem(text)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable)
        if bg is not None:
            it.setBackground(bg)
        if fg is not None:
            it.setForeground(fg)
        if tooltip:
            it.setToolTip(tooltip)
        return it

    def _mk_check_item(self, mod) -> QStandardItem:
        it = QStandardItem()
        it.setFlags(Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked
                         if mod.mod_id in self._checked
                         else Qt.CheckState.Unchecked)
        it.setToolTip("勾选后供「下载命令生成」「批量下载」使用；"
                      "筛选、排序后勾选保持不变")
        return it

    def _mk_color_item(self, mod, bg: QBrush | None) -> QStandardItem:
        name = _color_name(mod)

        it = self._plain("", bg)  # 底色 = 整行淡染，本格跟着行走即可
        if name in COLOR_CHOICES:
            it.setToolTip(f"颜色标记：{name}（右键可改/清除）")
        return it

    def _mk_id_item(self, mod, bg: QBrush | None) -> QStandardItem:
        it = self._plain(str(mod.mod_id), bg)
        # 编号挂 UserRole：行 → 编号的一切查找都从这格取
        it.setData(mod.mod_id, Qt.ItemDataRole.UserRole)
        return it

    def _mk_title_item(self, mod, bg: QBrush | None) -> QStandardItem:
        title = mod.title or "（无标题）"
        special = bool(getattr(mod, "special",getattr(mod, "is_special", False)))

        text = ("★ " + title) if special else title
        tip = ("特别关注：" if special else "") + (mod.title or "")
        return self._plain(text, bg, tooltip=tip)

    def _mk_status_item(self, mod, bg: QBrush | None) -> QStandardItem:
        return self._plain(
            status_zh(mod.status), bg,
            fg=QColor(_STATUS_FG[mod.status])
            if mod.status in _STATUS_FG else None,
            tooltip="状态只随账本变：确认入账、软删除、恢复都改账本；"
                    "磁盘文件的增删归「清理与删除」页管")

    def _mk_local_item(self, mod, bg: QBrush | None) -> QStandardItem:
        """本地版本格："3 天前 · 已验证" 一格读完（D21 修订版）。
        未验证/旧账灰色弱化——灰色含义见 constants.CONFIRMED_SOURCE_ZH。"""
        cv = getattr(mod, "confirmed_version", None)
        if not cv:
            return self._plain(
                "未知", bg, fg=QColor(_DIM_FG),
                tooltip="还没有确认过本地版本：下载成功的批次收尾清单里"
                        "勾选确认，或右键「设定本地版本…」手工认定")
        source = getattr(mod, "confirmed_source", "") or ""
        word = CONFIRMED_SOURCE_ZH.get(source, source)
        dim = source in ("unverified", "inherited_acf")
        it = self._plain(f"{relative_time(cv)} · {word}", bg,
                         fg=QColor(_DIM_FG) if dim else None)
        it.setToolTip(_SOURCE_TIPS.get(source, "本地版本的确认来源"))
        return it

    def _mk_remote_item(self, mod, bg: QBrush | None) -> QStandardItem:
        rt = getattr(mod, "remote_timeupdated", None)
        if not rt:
            return self._plain(
                "—", bg,
                tooltip="还没做过更新检测：远端版本要点【更新检测】"
                        "才写进账本")
        if self._needs_update(mod):
            it = self._plain(relative_time(rt), bg,
                             fg=QColor(_NEED_UPDATE_FG))
            f = QFont()
            f.setBold(True)
            it.setFont(f)
            it.setToolTip("远端有更新：工坊更新时间比你确认过的"
                          "本地版本新")
        else:
            it = self._plain(relative_time(rt), bg,
                             tooltip=f"远端最近更新：{relative_time(rt)}")
        return it

    def _mk_size_item(self, mod, bg: QBrush | None) -> QStandardItem:
        it = self._plain(fmt_size(getattr(mod, "local_size", None)), bg)
        it.setToolTip("本地内容大小——确认入账时随确认回填；未知不阻塞"
                      "使用，只影响备份前的磁盘空间预检")
        return it

    def _mk_note_item(self, mod, bg: QBrush | None) -> QStandardItem:
        note = getattr(mod, "note", "") or ""
        return self._plain(note, bg, tooltip=note or "")

    # ---------- 判定与事件 ----------

    @staticmethod
    def _needs_update(mod) -> bool:
        """D2 唯一公式的显示侧：已下载 且 远端比本地确认版本新。
        公式全项目只此一份实现（显示侧），检测/入账各层各有自己的
        消费点但都引用同一判据，改口径先改这里。"""
        if mod.status != STATUS_DOWNLOADED:
            return False
        cv = getattr(mod, "confirmed_version", None)
        rt = getattr(mod, "remote_timeupdated", None)
        return bool(cv and rt and rt > cv)

    def _on_item_changed(self, item: QStandardItem) -> None:
        """勾选框变化 → 维护集合 → 广播。幂等保护：集合没实际变化的
        重复事件（整行原位重造等）不广播，消费方不会被空转打扰。"""
        if item.column() != COL_CHECK:
            return
        mid = self.mod_id_at(item.row())
        if mid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            if mid not in self._checked:
                self._checked.add(mid)
                self.checked_changed.emit(set(self._checked))
        else:
            if mid in self._checked:
                self._checked.discard(mid)
                self.checked_changed.emit(set(self._checked))


# 来源悬浮说明（一次性文案，留本模块——D37 准入规则：单文件使用）
_SOURCE_TIPS = {
    "verified": "本地版本来自终端「下载成功」的判决，可放心参与"
                "更新判定与备份。",
    "claim": "本地版本来自「认领」：盘点发现盘上已有这个 mod，"
             "你确认收录。",
    "manual": "本地版本由你右键手工设定（最重的人工背书）。",
    "unverified": "这一版没有终端「下载成功」判决背书（批查失败但"
                  "照常确认）——建议右键「设定本地版本…」重新认定，"
                  "或等下一次成功判决。",
    "inherited_acf": "旧账：一次性迁移继承的记录——右键「设定本地"
                     "版本…」可重新认定。",
}
