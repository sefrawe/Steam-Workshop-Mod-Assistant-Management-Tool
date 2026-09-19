"""占位页
"""
"""
统一占位样式。G2 起被真实页面逐个替换（改 MainWindow._pages 即可）。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    def __init__(self, title: str, hint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
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
