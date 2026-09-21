"""占位页
"""
"""
统一占位样式：居中的大标题 + 一行灰字说明。
还没做好的页面先用它顶上；页面做好后到 MainWindow._pages 里
把对应占位页换成真实页面即可，其余接线都不用动。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    def __init__(self, title: str, hint: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        # 上下弹性留白 1:2，标题块整体略偏上
        layout.addStretch(1)
        title_label = QLabel(title, self)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(title_label)
        hint_label = QLabel(hint, self)
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_label.setStyleSheet("color: gray;")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)
        layout.addStretch(2)
