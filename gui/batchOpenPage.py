"""批量下载
"""
r"""gui/batchOpenPage.py · 「批量下载」导航项的薄壳页（D61 配套）。

D61 拍板：批量打开对话框的唯一功能入口在 mod 库页勾选操作菜单；
本导航项保留（不动 _check_nav 的导航自检语义），页内做两件事：
说明两条路的分工 + 一颗按钮拉起 BatchOpenDialog（粘贴清单批量
开工坊页面）。对话框零改动照用——两条路一本账。
"""
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from gui.batchOpenDialog import BatchOpenDialog
from gui.logBus import LogBus

_C_MUTED = "#8a8a8f"


class BatchOpenPage(QWidget):
    def __init__(self, repo, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._log = log or LogBus()
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("批量下载", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        note = QFrame(self)
        note.setObjectName("batchopen_note")
        note.setStyleSheet("QFrame#batchopen_note { border: 1px solid "
                           "#3a3a3a; border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "两个入口，各管一摊：\n"
            "· mod 库页勾选 mod → 操作菜单「打开工坊页面」——面向"
            "已在库的 mod；\n"
            "· 下面按钮——面向「手里只有一份清单」的场景：粘贴工坊"
            "网址/编号，逐个在浏览器打开（未收录的照样能开，打开"
            "页面不依赖账本）。",

            "典型用法：从朋友/旧清单拿到一批工坊网址，在 Steam 客户端"
            "里逐个点订阅。本页纯读不写账本；订阅完想收进本工具管理，"
            "走【入账中心】（盘上已下载的用【扫描游戏目录】认领）。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("批量打开工坊页面…", row)
        btn.setToolTip("粘贴清单 → 解析预览 → 确认后逐个在浏览器打开")
        btn.clicked.connect(self._open_dialog)
        h.addWidget(btn)
        h.addStretch(1)
        root.addWidget(row)
        tip = QLabel("先预览再动手：N 个编号就是 N 个标签页，"
                     "打开前有确认弹窗。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {_C_MUTED};")
        root.addWidget(tip)
        root.addStretch(1)

    def _open_dialog(self) -> None:
        BatchOpenDialog(self._repo, self, log=self._log).exec()
