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
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
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
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)

    def set_expanded(self, expanded: bool) -> None:
        """程序性收展（空桶自动收起用）。setChecked 只发 toggled 不发
        clicked，而我们连的是 clicked——所以外观同步要手动调一次
        _on_toggle，不存在信号环路。"""
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
