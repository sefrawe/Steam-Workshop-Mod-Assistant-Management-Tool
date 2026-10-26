"""可折叠分区（共享件） """
r"""gui/collapsibleSection.py · 一行「箭头＋标题」开关 + 明细区的折叠分区。
原是账实核验页的页内类（T19㉑）；清理与删除页（决策 69）沿用同一
交互后上收为共享件——多个页面一种手感，实现只有一份（单源纪律）。
用法：
- 构造时给标题；
- set_content(控件, 高度)：挂明细区，每分区只调一次。高度固定：
  收起/展开占同样空间，内容多时控件内部滚动，分区高度不随内容涨；
- set_expanded(...)：程序性收展（空桶自动收起用）；
- set_title(...)：换标题（带实时计数）；
- expand_changed(bool) 信号：用户点击与程序性收展都发（T15 批 3，
  账实核验页的折叠记忆靠它）。
【2026-10 标题富文本升级】标题行从「带文字的 QToolButton」改为
「纯箭头 QToolButton + 可点击 QLabel」：按钮家族不支持富文本
（setTextFormat 是 QLabel 的成员，QToolButton 没有）——带颜色/加粗
的标题在旧实现里只会原样显示 <b> 字样。QLabel 默认 AutoText：
文本像 HTML 就按富文本渲染、纯文本照常——既有各页的纯文本标题
（含 set_title 的实时计数）显示效果一字不变；点标题与点箭头同效。
对外 API 零变化，既有使用方一行不用动。
【2026-10 新增 make_guide】页面「先读我」折叠说明的单源工厂
（决策 34③ 三行说明块全面折叠化）：标题 + 若干段灰字，默认收起，
卡内滚动——各页一行接入，介绍再长也不撑爆页面。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)

from gui.theme import font_px  # 字号单源（D25）


class _RichTitleLabel(QLabel):
    """可点击标题标签：左键点下发 clicked；AutoText 富文本/纯文本通吃。"""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class CollapsibleSection(QWidget):
    # 收展状态变化（True=展开）。用户点击与程序性 set_expanded 都会发
    expand_changed = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        # 标题行 = 箭头按钮（只当开关）+ 标题标签（富文本可显、可点）
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self._toggle = QToolButton(row)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)  # 默认展开
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setFixedSize(20, 20)
        self._title = _RichTitleLabel(title, row)
        self._title.setTextFormat(
            Qt.TextFormat.AutoText)  # 显式声明（本就是默认值，防歧义）
        self._title.setCursor(
            Qt.CursorShape.PointingHandCursor)  # 可点暗示
        h.addWidget(self._toggle)
        h.addWidget(self._title, 1)
        v.addWidget(row)
        self._body = QWidget(self)
        self._body_v = QVBoxLayout(self._body)
        self._body_v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._body)
        # clicked(bool) 对 checkable 按钮传的就是新状态，直接用
        self._toggle.clicked.connect(self._on_toggle)
        # 点标题同效：新状态 = 箭头当前勾选态取反（此刻尚未翻转）
        self._title.clicked.connect(
            lambda: self._on_toggle(not self._toggle.isChecked()))

    def _on_toggle(self, expanded: bool) -> None:
        """收展的唯一出口：用户点箭头/标题走这里，程序性 set_expanded
        也手动调这里——所以对外信号只需要在这一处发。"""
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)
        self.expand_changed.emit(expanded)

    def set_expanded(self, expanded: bool) -> None:
        """程序性收展（空桶自动收起用）。setChecked 只发 toggled 不发
        clicked，而我们连的是 clicked——外观同步手动调一次 _on_toggle，
        不存在信号环路。"""
        self._toggle.setChecked(expanded)
        self._on_toggle(expanded)

    def set_title(self, title: str) -> None:
        """换标题（带实时计数）。不影响收展状态；富文本/纯文本由
        AutoText 自动识别。"""
        self._title.setText(title)

    def set_content(self, widget: QWidget, height: int) -> None:
        """挂明细区（每分区只调一次）。height = 明细区固定高度：
        内容多时靠控件内部滚动，分区高度不随内容涨。"""
        widget.setFixedHeight(height)
        self._body_v.addWidget(widget)


def make_guide(title: str, paras: tuple[str, ...], parent: QWidget,
               height: int = 200) -> CollapsibleSection:
    """页面「先读我」折叠说明（单源工厂，2026-10 起各页统一从这取）。
    默认收起——老用户不再每次滚过同一屏文字，新用户点一下就有完整
    指引；卡内滚动，段落再多也不撑爆页面。height 只定展开后的可视
    高度：给小了卡内滚动兜底，宁小勿大。"""
    body = QWidget(parent)
    bv = QVBoxLayout(body)
    bv.setContentsMargins(4, 4, 4, 4)
    bv.setSpacing(6)
    for text in paras:
        lbl = QLabel(text, body)
        lbl.setWordWrap(True)  # 可能变长的标签一律开换行（踩坑⑨）
        lbl.setStyleSheet(f"color: #8a8a8f; font-size: {font_px(12)}px;")
        bv.addWidget(lbl)
    bv.addStretch(1)
    wrap = QScrollArea(parent)
    wrap.setWidgetResizable(True)
    wrap.setFrameShape(QScrollArea.Shape.NoFrame)
    wrap.setWidget(body)
    sec = CollapsibleSection(title, parent)
    sec.set_content(wrap, height)
    sec.set_expanded(False)
    return sec
