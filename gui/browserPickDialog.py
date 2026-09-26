"""浏览器取网址选择器
"""
"""
模块②【从浏览器取标签页…】的页内选择器（T23）：内嵌
BrowserTabPage 实例——采集、勾选、工坊预勾选判定全部是同一份
代码（零复制），勾好后点【送到「加入新 mod」】：网址送回模块②
第②步自动解析预览，本窗口随即关闭——人本来就在模块②，全程无跳页。

与【从浏览器取网址】基础功能页的关系：同一个类用两遍——基础
功能页是独立入口（复制网址走老流程也行），本对话框是模块②的
顺路入口。两条路一本账：解析、入库都在模块②。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget,
)

from gui.browserTabPage import BrowserTabPage


class BrowserPickDialog(QDialog):
    """内嵌浏览器标签选择器：送到模块② = 填入并关窗；关闭 = 不送。"""

    # 勾选的网址清单 → 主窗口转交「加入新 mod」第②步
    lines_picked = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("从浏览器取标签页")
        self.resize(780, 560)
        root = QVBoxLayout(self)
        # 页面本体原样内嵌（零复制）：它的按钮、tooltip、空态引导
        # 在对话框里语义不变
        self._page = BrowserTabPage(self)
        self._page.handoff_to_addmod.connect(self._on_handoff)
        root.addWidget(self._page, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        btn_close = QPushButton("关闭", self)
        btn_close.setToolTip("关闭本窗口，不送出任何网址")
        btn_close.clicked.connect(self.reject)
        bottom.addWidget(btn_close)
        root.addLayout(bottom)

    def _on_handoff(self, lines: list) -> None:
        """页面里的【送到「加入新 mod」】：转发清单并关窗——
        填入与解析由主窗口→模块②完成（本对话框不碰业务）。"""
        self.lines_picked.emit(lines)
        self.accept()
