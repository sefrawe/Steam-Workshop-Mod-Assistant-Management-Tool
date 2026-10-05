"""批量打开工坊页面对话框"""
r"""gui/batchOpenDialog.py · 贴一份清单，把工坊页面逐个在默认浏览器打开。
典型场景：从朋友 / 旧清单拿到一批工坊网址，要在 Steam 客户端里逐个点
订阅——本对话框把"逐个复制粘贴打开"变成一次批量。与「勾选项打开」
（mod 库页操作菜单）互补：那边面向已在库的 mod，这边面向"手里只有
清单"的场景，未收录条目照样能开（打开页面不依赖账本）。

三条纪律（与批量特别关注对话框同源的设计）：
1. 纯读操作：解析、对表、【打开】全程不写账本——订阅发生在 Steam
   客户端，本工具零干预；订阅完想收进账本，走【入账中心】粘贴登记（盘上已下载的用【扫描游戏目录】认领）
2. 解析走 core.urlParser 单源：工坊网址 / 纯数字 /
   steamcmd 命令行都认，认不出的行原样列出——不猜、不纠正、不静默丢弃。
3. 网址由模板生成（WORKSHOP_URL_TEMPLATE，单源），不发明第五份模板串。

先预览再动手：解析+对表只分类不打开（classify_open_targets，pytest
直接测它）；【打开】带确认弹窗——N 个编号就是 N 个标签页，误点不
确认就是浏览器灾难（与勾选版同一防线）。
"""
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QUrl  # QUrl 在 QtCore（v2.21 小坑：别再放 QtGui）
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.modRepository import ModRepository
from core.urlParser import parse_lines
from gui.logBus import LogBus

from core.urlParser import WORKSHOP_URL_TEMPLATE

@dataclass
class OpenPagesReport:
    """一次"解析 + 对表"的全部结论（纯读）。
    打开清单 = 全部解析出的编号：已在账本与否都开（已收录的页面照样
    可能要看）；对表结果只作预览里的信息提示，不作门槛。"""
    parsed_ids: list[int] = field(default_factory=list)     # 去重、按出现顺序
    invalid_lines: list[str] = field(default_factory=list)  # 认不出的原始行
    tracked_ids: list[int] = field(default_factory=list)    # 已在任何档案账本
    untracked_ids: list[int] = field(default_factory=list)  # 未收录（订阅主力）

    def urls(self) -> list[str]:
        """按解析顺序生成待开页面地址（模板单源）。"""
        return [WORKSHOP_URL_TEMPLATE.format(mid) for mid in self.parsed_ids]


def classify_open_targets(repo: ModRepository, text: str) -> OpenPagesReport:
    """解析文本并按"是否已在账本"分类。纯逻辑、零界面，预览与 pytest 共用。
    对表只做全库存在性检查、不分档案——打开页面与档案无关（这是与
    批量特别关注的本质区别：那张表要写入某个档案，必须锁档案；这里
    零写入，档案归属没有意义）。"""
    report = parse_lines(text.splitlines())
    rep = OpenPagesReport(parsed_ids=report.mod_ids,
                          invalid_lines=report.invalid)
    if rep.parsed_ids:
        existing = set(repo.filter_existing_ids(rep.parsed_ids))
        rep.tracked_ids = [mid for mid in rep.parsed_ids if mid in existing]
        rep.untracked_ids = [mid for mid in rep.parsed_ids
                             if mid not in existing]
    return rep


class BatchOpenDialog(QDialog):
    """贴清单 → 解析预览 → 打开 的三步模态对话框。打开的是浏览器页面
    不是账本事务——没有落库步骤，也就没有"要么全成要么全败"，
    openUrl 逐个发即可（浏览器拒收单个链接不影响其余，如实报数）。"""

    def __init__(self, repo: ModRepository, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._log = log  # 没人接也照常工作（打开动作不依赖日志）
        self._report: OpenPagesReport | None = None
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.setWindowTitle("批量打开工坊页面")
        self.resize(560, 540)
        root = QVBoxLayout(self)
        root.setSpacing(8)
        head = QLabel(
            "粘贴工坊网址 / 纯数字编号清单（每行一条，或空格分隔均可），"
            "先【解析预览】核对，再【打开】——每个编号一个浏览器标签页，"
            "便于逐页去点订阅。\n"
            "本对话框是纯读操作：不写账本。订阅完成后想收进本工具管理，"
            "走【入账中心】粘贴登记（盘上已下载的用【扫描游戏目录】认领）。", self)
        head.setWordWrap(True)  # 可能变长的标签一律开换行
        root.addWidget(head)
        self._edit = QPlainTextEdit(self)
        self._edit.setPlaceholderText(
            "例：\n"
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3173817085\n"
            "3219589892")
        root.addWidget(self._edit, 1)
        self._report_label = QLabel("", self)
        self._report_label.setWordWrap(True)
        self._report_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)  # 报告可选中复制
        root.addWidget(self._report_label, 1)
        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn_preview = QPushButton("解析预览", btn_row)
        self._btn_preview.setToolTip(
            "解析文本并核对收录情况；只读操作，不改任何数据")
        self._btn_preview.clicked.connect(self._on_preview)
        h.addWidget(self._btn_preview)
        self._btn_open = QPushButton("打开", btn_row)
        self._btn_open.setToolTip(
            "把解析出的页面逐个交给默认浏览器；开前有确认弹窗")
        self._btn_open.setEnabled(False)  # 预览出编号才亮
        self._btn_open.clicked.connect(self._on_open)
        h.addWidget(self._btn_open)
        h.addStretch(1)
        btn_close = QPushButton("关闭", btn_row)
        btn_close.clicked.connect(self.reject)
        h.addWidget(btn_close)
        root.addWidget(btn_row)

    # ---------- 槽 ----------
    def _on_preview(self) -> None:
        text = self._edit.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "解析预览", "先粘贴清单再解析。")
            return
        self._report = classify_open_targets(self._repo, text)
        self._report_label.setText(_render_report(self._report))
        self._btn_open.setEnabled(bool(self._report.parsed_ids))

    def _on_open(self) -> None:
        r = self._report
        if r is None or not r.parsed_ids:
            return
        ret = QMessageBox.question(
            self, "批量打开工坊页面",
            f"在浏览器打开 {len(r.parsed_ids)} 个工坊页面？\n"
            "每个编号一个标签页；数量多时浏览器会连开一片，"
            "可先关掉多余的页面再逐个订阅。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        for url in r.urls():
            QDesktopServices.openUrl(QUrl(url))
        if self._log is not None:
            self._log.ok(f"已请求浏览器打开 {len(r.parsed_ids)} 个工坊页面"
                         f"（其中未收录 {len(r.untracked_ids)} 个）")
        self.accept()


def _render_report(r: OpenPagesReport) -> str:
    """把解析结论翻译成人话。只摆事实 + 指路，不替用户做决定。"""
    if not r.parsed_ids:
        lines = ["没有解析出任何编号。"]
        if r.invalid_lines:
            lines.append("以下行无法识别（需要含 steamcommunity.com 与 id 参数的"
                         "工坊网址，或直接纯数字编号）：")
            lines += [f"· {line}" for line in r.invalid_lines]
        return "\n".join(lines)
    lines = [f"解析出 {len(r.parsed_ids)} 个编号："]
    lines.append(f"· 未收录 {len(r.untracked_ids)} 个"
                 + ("——正是要去点订阅的目标" if r.untracked_ids else ""))
    if r.tracked_ids:
        lines.append(f"· 已在账本 {len(r.tracked_ids)} 个"
                     "（已由本工具管理，照开不误，仅供核对）")
    if r.invalid_lines:
        lines.append(f"· 无法识别的行 {len(r.invalid_lines)} 条"
                     "（原样列出，不代纠正）：")
        lines += [f"  {line}" for line in r.invalid_lines]
    return "\n".join(lines)
