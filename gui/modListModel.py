"""mod 列表模型
"""
"""
模型不持有查询条件——条件由 ModListPage 持有，reload 后 set_rows 灌入。
排序走 repo.list_mods 的 order_by（ALLOWED_ORDERS 白名单在契约层防注入），
表头点击只做"列号 → order_by 字符串"的翻译。

勾选列（T20b）：第 0 列是复选框，供批量下载挑条目。勾选状态按
mod_id 记在模型里（不按行号）——列表每次筛选/排序都会整表重建，
按行号记会在 reload 后错位；按 mod_id 记则天然跨 reload 保留，
set_rows 时顺手剪掉已不在清单里的 id，集合不积灰。
"""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from core.models import Mod
from gui.formatters import fmt_size, relative_time, status_zh
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor

from gui.theme import zebra_colors

COLUMNS = ["选", "编号", "标题", "状态", "远端版本", "本地版本", "更新",
           "大小", "标签", "关注", "备注"]

# 列号 → 白名单排序；None = 白名单只给了单向。不在表内的列点击不排序。
# 列号含勾选列：除勾选列外全部比无勾选版 +1
_SORT_MAP: dict[int, tuple[str, str | None]] = {
    1: ("mod_id ASC", "mod_id DESC"),
    2: ("title ASC", "title DESC"),
    3: ("status ASC", "status DESC"),
    4: ("time_updated DESC", "time_updated ASC"),
    5: ("local_timeupdated DESC", "local_timeupdated ASC"),
    7: ("local_size DESC", None),
    9: ("is_special DESC", None),
}


_CHECK_COL = 0  # 勾选列号：data/flags/setData 里反复用到，收拢成常量
# 颜色标记的唯一色表（v2.25 从 modListPage 上收）：右键取色器、
# 顶栏颜色筛选、渲染归一化全从这里取，不再两处各抄一份
COLOR_CHOICES = {
    "红": "#e5484d", "橙": "#f76b15", "黄": "#f5d90a",
    "绿": "#46a758", "蓝": "#0091ff", "紫": "#8e4ec6",
}


def normalize_color_tag(value: str | None) -> str | None:
    """把库里的 color_tag 归一成可用 hex；不可用 → None（= 无标记）。

    亮色主题修障（v2.25）：右键取色器如今只写 hex，但库里存有更早
    版本的遗留值（中文单字、手误字符串）——QColor 解析失败时 Qt 会
    静默跳过背景绘制，而"按底色亮度选黑/白字"的逻辑会从无效色读到
    全 0 分量 → 恒选白字：深色主题下白字落深底看不出异常（问题被
    掩盖的原因），亮色主题下就成了白字白底。别名表把旧版中文名
    救回成 hex；彻底无效的值按无标记渲染。显示与筛选两个消费端
    共用本函数（mod 库页的颜色筛选也从这里 import），单源不漂移。
    """
    if not value:
        return None
    v = str(value).strip()
    if v in COLOR_CHOICES:
        return COLOR_CHOICES[v]
    # v2.26 修订：isValid 通过 ≠ 可见——八位 #AARRGGBB（含全透明）与
    # 大写 hex 都算"合法"，但透明填充画不出底色、亮度读 0 恒选白字
    # （深色主题看着像普通行，亮色主题白字白底）。name() 一并拍平：
    # 转不透明 rgb、转小写，显示与筛选两侧比对从此大小写/透明度无关
    c = QColor(v)
    return c.name() if c.isValid() else None


class ModListModel(QAbstractTableModel):
    # 勾选集合变化（含被 reload 剪掉的情况），页面据此更新"已勾选 N"
    checks_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Mod] = []
        self._checked: set[int] = set()  # 勾选的 mod_id 集合

    # ---------- 对外 ----------

    def set_rows(self, rows: list[Mod]) -> None:
        self.beginResetModel()
        self._rows = rows
        # 剪掉已不在当前清单里的勾选（软删除/换档案后不留僵尸勾选）
        self._checked &= {m.mod_id for m in rows}
        self.endResetModel()
        self.checks_changed.emit()

    def mod_at(self, row: int) -> Mod:
        return self._rows[row]

    def checked_ids_in_rows(self) -> list[int]:
        """勾选且仍在当前清单里的 mod_id（按表格行序 = 下载顺序）。"""
        return [m.mod_id for m in self._rows if m.mod_id in self._checked]

    def checked_count_in_rows(self) -> int:
        return sum(1 for m in self._rows if m.mod_id in self._checked)
    def set_all_checked(self, on: bool) -> None:
        """全选/全不选（用户 todo：全选只作用当前显示的 mod——筛选/
        搜索后的可见行；隐藏行本来就不在 _rows 里，天然不会波及）。"""
        self._checked = {m.mod_id for m in self._rows} if on else set()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, _CHECK_COL),
                self.index(len(self._rows) - 1, _CHECK_COL),
                [Qt.ItemDataRole.CheckStateRole])
        self.checks_changed.emit()

    # ---------- Qt 必备 ----------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def flags(self, index: QModelIndex):
        # 只读表格：不给 ItemIsEditable；唯独勾选列可点（UserCheckable）
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _CHECK_COL:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def headerData(self, section: int, orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        m = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == _CHECK_COL:
            return (Qt.CheckState.Checked if m.mod_id in self._checked
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(m, col)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (1, 4, 5, 7):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = m.title or "（无标题）"
            if m.note:
                tip += f"\n备注：{m.note}"
            return tip
        # —— 背景与文字色（v2.27 重排收口）——
        # 修复案卷（v2.25→v2.27）：标记"时显时隐"的真正元凶在渲染层——
        # qdarktheme 的 QSS 接管 item 绘制后，交替行上的样式规则盖掉
        # 模型 BackgroundRole，同一标记色在斑马纹两色行上一显一隐
        # （实测规律：深浅位置决定显示）。v2.25/26 的 normalize（拍平
        # 透明/大写 hex）是真实防御，保留，但非本案主因。
        # 解法：view 关闭 alternatingRowColors（modListPage 侧），斑马纹
        # 改由模型对每格返回调色板色——:alternate 伪类不再命中，背景
        # 统一走 backgroundBrush 路径。副产品：更新列的半透明红此前
        # 在交替行同样会被盖掉，一并修复。
        if role == Qt.ItemDataRole.BackgroundRole:
            tag = normalize_color_tag(m.color_tag)
            if tag and col in (1, 2):
                return QColor(tag)
            if col == 6 and self._update_state(m) == "需更新":
                return QColor(220, 60, 60, 46)  # 半透明红，深浅主题都不刺眼
            # 斑马纹（v2.28）：色源 = gui/theme.zebra_colors() 自持双色——
            # QSS 主题库下 app.palette() 与样式表脱节（亮色下 Base 残留
            # 深色、AlternateBase 已换浅色，混血实证），palette 不再作
            # 颜色事实源；QSS 未生效返回 None → 无斑马纹，宁可不画不猜色
            _zebra = zebra_colors()
            return QColor(_zebra[index.row() & 1]) if _zebra else None

    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole) -> bool:
        """勾选列：用户点复选框时写入。只读表格里唯一可写的格子。"""
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != _CHECK_COL:
            return False
        state = value if isinstance(value, Qt.CheckState) else Qt.CheckState(int(value))
        m = self._rows[index.row()]
        if state == Qt.CheckState.Checked:
            self._checked.add(m.mod_id)
        else:
            self._checked.discard(m.mod_id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checks_changed.emit()
        return True

    # ---------- 内部 ----------

    @staticmethod
    def _update_state(m: Mod) -> str:
        if m.status == "deleted":
            return "已删除"
        if m.version_unknown:
            # 版本未知（T18）：downloaded 但没有 acf 本地版本
            # = 手动确认入账；tracked 没版本 = 还没下载
            return "版本未知" if m.status == "downloaded" else "未下载"
        if m.time_updated is None:
            # 本地已下载、远端从没查过——"远端未知"，与"版本未知"区分开
            return "远端未知"
        return "需更新" if m.time_updated > m.local_timeupdated else "最新"

    def _display(self, m: Mod, col: int) -> str:
        match col:
            case 0:
                return ""  # 勾选列：内容由 CheckStateRole 画
            case 1:
                return str(m.mod_id)
            case 2:
                return m.title or "（无标题）"
            case 3:
                return status_zh(m.status)
            case 4:
                return relative_time(m.time_updated)
            case 5:
                if m.local_timeupdated:
                    return relative_time(m.local_timeupdated)
                # 没有本地版本时按状态区分说法（T18）
                return "版本未知" if m.status == "downloaded" else "未下载"
            case 6:
                return self._update_state(m)
            case 7:
                return fmt_size(m.local_size or m.file_size)  # acf 缺失时用 API 兜底
            case 8:
                return " ".join(m.tags) if m.tags else ""
            case 9:
                return "是" if m.is_special else ""
            case 10:
                return m.note or ""
        return ""
