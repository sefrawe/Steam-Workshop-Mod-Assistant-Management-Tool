"""分享清单功能模块
"""
r"""gui/shareListPage.py —— 功能模块「分享与接收清单」（决策 34 第二档）。

引导壳 + 两个转调入口：分享包导出/导入的引擎在 core/dataExporter
（决策 33），文件菜单里一直有同款两项。本页是第二入口（两条路
一本账，与「首次使用」把建档/连接做成转调入口同一哲学），并补上
文件菜单给不了的"之后该干什么"的引导。

语义与引擎口径逐字一致（文案不许添油加醋）：
- 导出 = 当前档案的收录清单 + 整理成果（备注、标签、特别关注），
  不含下载状态等本机信息——朋友收到的是"该收哪些"而不是"我下到哪"；
- 导入 = 增量并入：已有的 mod 自动跳过，现有数据一字不改。收清单
  因此无风险、不需要确认弹窗（与文件菜单同款行为）；
- 收到之后：分享包自带归属档案，导入后到左上角切换到那个游戏；
  新登记的 mod 没有标题等远端信息，跑一遍【更新检测】自动补全。

本页零新引擎：三个按钮各发一个信号，主窗口转调既有方法——确认
弹窗、防呆、日志、状态栏回执全住在原处，本页一个字不复制。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus

# 与批量下载步骤卡片、日常更新模块页一致的配色，按用途命名
_C_OK = "#46a758"    # 完成 / 一切正常
_C_FAIL = "#e5484d"  # 出错
_C_WARN = "#f5a623"  # 需要注意
_C_INFO = "#d4d4d4"  # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行


class ShareListPage(QWidget):
    """「分享清单」模块页：分享 / 收取 / 收尾三步引导。"""

    # ① 导出分享包（当前档案）：主窗口转调 _export_sharepack（文件菜单同款）
    share_out_requested = Signal()
    # ② 导入分享包：主窗口转调 _import_sharepack（增量并入，无需确认）
    share_in_requested = Signal()
    # ③ 收尾：去【更新检测】补全新登记 mod 的远端信息
    open_update_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo            # 保留统一构造签名（本页不直接读库）
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        self._build_ui()
        self._reset_flow()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("分享清单", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        note = QFrame(self)
        note.setObjectName("share_note")
        note.setStyleSheet("QFrame#share_note { border: 1px solid #3a3a3a;"
                           " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：① 整理好当前档案的备注、标签、特别关注 → 点【导出"
            "分享包】生成一个 JSON 文件发给朋友 → ② 朋友在本软件点"
            "【导入分享包】选那个文件（增量并入，已有的自动跳过）→ "
            "③ 切到对应档案、跑一遍【更新检测】补全标题。",
            "为什么安全：导入只新增、绝不改动现有数据——朋友清单里你"
            "已经收过的 mod 原样保留，你的备注标签一字不动；所以导入"
            "不需要确认弹窗，结果看状态栏报数。",
            "术语与关系：分享包 = 收录清单 + 整理成果（备注/标签/特别"
            "关注），不含下载状态等本机信息；文件菜单里也有同款两项，"
            "两边是同一个功能的两扇门。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 分享卡：导出按钮（按当前档案，无档案置灰）
        self._d1, box1 = self._make_card("step1", "① 把整理成果分享给朋友")
        row1 = QHBoxLayout()
        self._btn_out = QPushButton("导出分享包（当前档案）…", self)
        self._btn_out.setToolTip(
            "把当前游戏的收录清单与整理成果（备注、标签、特别关注）"
            "导出为 JSON 文件；不含下载状态等本机信息")
        self._btn_out.clicked.connect(self.share_out_requested.emit)
        row1.addWidget(self._btn_out)
        row1.addStretch(1)
        box1.addLayout(row1)

        # ② 收取卡：导入按钮（与档案无关，恒可点）
        self._d2, box2 = self._make_card("step2", "② 收朋友的清单")
        row2 = QHBoxLayout()
        self._btn_in = QPushButton("导入分享包…", self)
        self._btn_in.setToolTip(
            "增量并入：已有的 mod 自动跳过，现有数据一字不改；"
            "完成后状态栏报新增/跳过条数")
        self._btn_in.clicked.connect(self.share_in_requested.emit)
        row2.addWidget(self._btn_in)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ③ 收尾卡：跳更新检测
        self._d3, box3 = self._make_card("step3", "③ 导入之后")
        row3 = QHBoxLayout()
        self._btn_update = QPushButton("去更新检测（补全标题）", self)
        self._btn_update.setToolTip(
            "新登记的 mod 没有标题、作者、大小等远端信息，"
            "跑一遍更新检测自动补全")
        self._btn_update.clicked.connect(self.open_update_requested.emit)
        row3.addWidget(self._btn_update)
        row3.addStretch(1)
        box3.addLayout(row3)

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        """档案切换广播：只影响①卡（导出按当前档案）。"""
        self._game = game
        self._reset_flow()

    # ---------- 内部：清场与卡片基建（样式与日常更新页同款） ----------

    def _reset_flow(self) -> None:
        """初始化与切档案共用：①随档案启停；②③与档案无关。"""
        has = self._game is not None
        self._btn_out.setEnabled(has)
        if self._game is None:
            self._set_card(self._d1,
                           "未选择档案——分享按当前档案导出，请先在左上角"
                           "添加或选择游戏档案", _C_WARN)
        else:
            self._set_card(
                self._d1,
                f"将导出档案「{self._game.name}」"
                f"（{self._game.app_id}）的收录清单与整理成果；"
                "导出前建议先在 mod 库页把备注、标签、特别关注整理好。",
                _C_OK)
        self._set_card(
            self._d2,
            "点下方按钮选朋友发来的 JSON 文件：已有的 mod 自动跳过，"
            "现有数据一字不改；完成后状态栏报新增/跳过条数。",
            _C_MUTED)
        self._set_card(
            self._d3,
            "到左上角切换到分享包对应的游戏；新登记的 mod 跑一遍"
            "【更新检测】补全标题、作者、大小，之后照常下载/备份/更新。",
            _C_MUTED)

    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张固定步骤卡，返回（状态行, 内容布局）。样式选择器限定
        到本框（QFrame#card_xxx）：QLabel 也是 QFrame 子类，不限定会把
        边框画到卡里每行字上——既有页面的同款处理。"""
        frame = QFrame(self)
        frame.setObjectName(f"card_{name}")
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        detail = QLabel("", frame)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"border:none; color:{_C_MUTED};")
        box.addWidget(title_lbl)
        box.addWidget(detail)
        self._cards_box.addWidget(frame)
        return detail, box

    def _set_card(self, detail: QLabel, text: str, color: str) -> None:
        detail.setText(text)
        detail.setStyleSheet(f"border:none; color:{color};")
