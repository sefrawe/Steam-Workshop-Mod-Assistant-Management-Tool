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
from PySide6.QtGui import QColor

from core.models import Mod
from gui.formatters import fmt_size, relative_time, status_zh

COLUMNS = ["选", "编号", "标题", "状态", "远端版本", "本地版本", "更新",
           "大小", "标签", "关注", "备注"]

# 列号 → 白名单排序；None = 白名单只给了单向。不在表内的列点击不排序。
# 列号含勾选列：除勾选列外全部比无勾选版 +1
_SORT_MAP: dict[int, tuple[str, str | None]] = {
    1: ("mod_id ASC", "mod_id DESC"),
    2: ("title ASC", "title DESC"),
    4: ("time_updated DESC", "time_updated ASC"),
    7: ("local_size DESC", None),
}

_CHECK_COL = 0  # 勾选列号：data/flags/setData 里反复用到，收拢成常量


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
        if role == Qt.ItemDataRole.BackgroundRole and col == 6:
            if self._update_state(m) == "需更新":
                return QColor(220, 60, 60, 46)  # 半透明红，深浅主题下都不刺眼
        if role == Qt.ItemDataRole.ForegroundRole and col == 5 and m.version_unknown:
            return QColor(128, 128, 128)
        return None

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
