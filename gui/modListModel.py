"""mod 列表模型
"""
"""
模型不持有查询条件——条件由 ModListPage 持有，reload 后 set_rows 灌入。
排序走 repo.list_mods 的 order_by（ALLOWED_ORDERS 白名单在契约层防注入），
表头点击只做"列号 → order_by 字符串"的翻译。
"""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from core.models import Mod

from gui.formatters import fmt_size, relative_time, status_zh

COLUMNS = ["编号", "标题", "状态", "远端版本", "本地版本", "更新", "大小", "标签", "关注", "备注"]


# 列号 → 白名单排序；None = 白名单只给了单向。不在表内的列点击不排序
_SORT_MAP: dict[int, tuple[str, str | None]] = {
    0: ("mod_id ASC", "mod_id DESC"),
    1: ("title ASC", "title DESC"),
    3: ("time_updated DESC", "time_updated ASC"),
    6: ("local_size DESC", None),
}


class ModListModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Mod] = []

    # ---------- 对外 ----------

    def set_rows(self, rows: list[Mod]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def mod_at(self, row: int) -> Mod:
        return self._rows[row]

    # ---------- Qt 必备 ----------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def flags(self, index: QModelIndex):  # 只读：不给 ItemIsEditable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        m = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(m, col)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (0, 3, 4, 6):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = m.title or "（无标题）"
            if m.note:
                tip += f"\n备注：{m.note}"
            return tip
        if role == Qt.ItemDataRole.BackgroundRole and col == 5:
            if self._update_state(m) == "需更新":
                return QColor(220, 60, 60, 46)  # 半透明红，深浅主题下都不刺眼
        if role == Qt.ItemDataRole.ForegroundRole and col == 4 and m.version_unknown:
            return QColor(128, 128, 128)
        return None

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
            # 本地已下载、远端从没查过——改名"远端未知"，
            # 与更新检测页汇总行的既有用词对齐，并和"版本未知"区分开
            return "远端未知"
        return "需更新" if m.time_updated > m.local_timeupdated else "最新"

    def _display(self, m: Mod, col: int) -> str:
        match col:
            case 0:
                return str(m.mod_id)
            case 1:
                return m.title or "（无标题）"
            case 2:
                return status_zh(m.status)
            case 3:
                return relative_time(m.time_updated)
            case 4:
                if m.local_timeupdated:
                    return relative_time(m.local_timeupdated)
                # 没有本地版本时按状态区分说法（T18）：手动确认的显示
                # "版本未知"，没下载的显示"未下载"，不再混为一谈
                return "版本未知" if m.status == "downloaded" else "未下载"

            case 5:
                return self._update_state(m)
            case 6:
                return fmt_size(m.local_size or m.file_size)  # acf 缺失时用 API 兜底
            case 7:
                return " ".join(m.tags) if m.tags else ""
            case 8:
                return "是" if m.is_special else ""
            case 9:
                return m.note or ""
        return ""
