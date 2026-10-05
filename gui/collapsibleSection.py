"""可折叠分区（共享件）
"""
r"""gui/collapsibleSection.py · 一行「箭头＋标题」开关 + 明细区的折叠分区。

原是账实核验页的页内类（T19㉑）；清理与删除页（决策 69）沿用同一
交互后上收为共享件——两个页面一种手感，实现只有一份（单源纪律）。

用法：
- 构造时给标题；
- set_content(控件, 高度)：挂明细区，每分区只调一次。高度固定：
  收起/展开占同样空间，内容多时控件内部滚动，分区高度不随内容涨
  （页面总高度交给整页滚动管）；
- set_expanded(...)：程序性收展（空桶自动收起用）；
- set_title(...)：换标题（带实时计数）。

【T15 批 3 审计补齐】新增 expand_changed(bool) 信号：无论用户点
箭头还是页面程序性收展（set_expanded），状态一变就发——账实核验
页靠它把三个分区的折叠状态记进 QSettings（T19⑤ 收官的折叠记忆，
与主窗口面板显隐同一套 session/ 口径）。已有的使用方（清理与
删除页）不连这个信号就完全不受影响：没人监听的信号是空操作。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    # 收展状态变化（True=展开）。用户点击与程序性 set_expanded 都会发
    expand_changed = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        self._toggle = QToolButton(self)
        self._toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)  # 默认展开
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setText(title)
        self._toggle.setFixedHeight(22)
        v.addWidget(self._toggle)
        self._body = QWidget(self)
        self._body_v = QVBoxLayout(self._body)
        self._body_v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._body)
        # clicked(bool) 对 checkable 按钮传的就是新状态，直接用
        self._toggle.clicked.connect(self._on_toggle)

    def _on_toggle(self, expanded: bool) -> None:
        """收展的唯一出口：用户点箭头走这里，程序性 set_expanded
        也手动调这里——所以对外信号只需要在这一处发。"""
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)
        self.expand_changed.emit(expanded)  # T15 批 3：折叠记忆用

    def set_expanded(self, expanded: bool) -> None:
        """程序性收展（空桶自动收起用）。setChecked 只发 toggled
        不发 clicked，而我们连的是 clicked——所以外观同步要手动调
        一次 _on_toggle，不存在信号环路。"""
        self._toggle.setChecked(expanded)
        self._on_toggle(expanded)

    def set_title(self, title: str) -> None:
        """换标题（带实时计数）。不影响收展状态。"""
        self._toggle.setText(title)

    def set_content(self, widget: QWidget, height: int) -> None:
        """挂明细区（每分区只调一次）。height = 明细区固定高度：
        内容多时靠控件内部滚动，分区高度不随内容涨。"""
        widget.setFixedHeight(height)
        self._body_v.addWidget(widget)
