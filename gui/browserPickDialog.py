"""模块②「加入新 mod」的页内标签页选择器。
"""
"""
内嵌 BrowserTabPage（与独立页同一份代码，零复制）：勾选后点
【送到「加入新 mod」】= 对话框把清单发给 lines_picked 并关闭——
人本来就在模块②，全程无跳页；清单送回走 receive_external_lines
（自动填入并解析预览），入库仍要亲手点第③步。

（addModPage 文件头预告的"信号暂撤；采集器迁入后恢复"入口，
随 tabCollector 收编一并恢复——V1 完善功能归位。）
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout

from gui.browserTabPage import BrowserTabPage


class BrowserPickDialog(QDialog):
    """实时采集 Edge 标签页 → 勾选 → 送回模块②。模态对话框：
    采集期间键鼠本就被程序占用，模态正合适；三路中断（Esc /
    停止键 / FAILSAFE）由内嵌页自理，本壳零采集逻辑。"""

    # 勾选的网址清单 → addModPage.receive_external_lines
    lines_picked = Signal(list)

    def __init__(self, parent=None, *, log=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("从浏览器取标签页")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._page = BrowserTabPage(parent=self, log=log)
        # 内嵌页的【送到「加入新 mod」】在本壳语境里就是"确定"：
        # 清单上交、窗口即关——与独立页的"跳页交清单"同一信号、
        # 两种落法，同一份页面代码零复制
        self._page.handoff_to_addmod.connect(self._on_picked)
        lay.addWidget(self._page)

    def _on_picked(self, urls: list) -> None:
        self.lines_picked.emit(urls)
        self.accept()
