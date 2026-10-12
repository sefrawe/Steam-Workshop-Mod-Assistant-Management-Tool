"""表格数值排序键（单源共享件）。

QTableWidget 点表头排序默认按显示文本比——"9.9 MiB" 会排到
"13.7 MiB" 后面、"10" 排到 "2" 前面。SortItem 给单元格挂一个
排序键：显示文本给人看，比较大小用库内原始值。

使用纪律（备份页定稿，各页同守）：
- 灌表期 setSortingEnabled(False)，灌完再开——带着排序灌数据，
  Qt 边灌边排，行和数据会错位；
- 排序搬的是条目、行隐藏挂的是行号——排序后重滤一次对齐
  （表头 sortIndicatorChanged → 页面的查找落地函数）；
- 勾选/编号等业务数据一律存条目的 UserRole 数据角色：排序搬
  条目时它们跟着走，按行号反查必错。
"""
from PySide6.QtWidgets import QTableWidgetItem


class SortItem(QTableWidgetItem):
    """带排序键的单元格：__lt__ 比较键、不比较显示文本。键同型
    即可比（int/str/tuple 都行——缺失依赖列用元组按首编号排）。"""

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, SortItem):
            return self._key < other._key
        return super().__lt__(other)
