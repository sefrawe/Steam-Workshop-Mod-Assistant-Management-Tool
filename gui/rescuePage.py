"""恢复旧版本功能模块
"""
"""
gui/rescuePage.py · 左导航「分步向导」组：三步恢复引导壳。

零新引擎：恢复的每一步都是既有功能的入口。备份区由备份引擎管理——
恢复前自动备份当前版本、恢复失败自动回退，这些保障住在引擎里，本页
一个字不复制、一行不重写。

V2 判决制下必须说清的一件事（本页存在的核心理由）：
恢复把旧版本文件放回下载目录，但账本里"本地版本"仍记着恢复前确认的
那个版本号——账与盘暂时不一致。对齐的两步住在第③步：
  ① 到【入账中心 · 最近判决】右键该 mod「撤销确认」——账本回到
     "未确认"态（下载状态不变，判决史保留备查），下次更新检测会
     重新报告它；
  ② 点第③步按钮去入账中心盘点——盘上的旧版本落成待认领候选，
     认领入账后账本如实回到旧版本。
不想对齐也能正常用：照常玩，想回新版重新下载即可。

三步：
① 确认档案——恢复发生在当前界面档案名下（set_game 广播，与其他
   模块页同款）；
② 打开【备份与恢复】页，勾选恰好一份要回到的备份，用
   「对选中 ▾ → 恢复所选」。选哪份看「版本时间」列；
③ 恢复完成后按上面两步对齐账与盘。

本页只有两个转调信号（去备份页 / 请求盘点），不发任何改数据的信号
——恢复按钮在备份页、盘点链在入账中心，本页只带路。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus
from gui.theme import font_px  # 字号单源（D25）

# 与批量下载步骤卡片、日常更新模块页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 一切正常
_C_FAIL = "#e5484d"   # 出错
_C_WARN = "#f5a623"   # 需要注意
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行


class RescuePage(QWidget):
    """「恢复旧版本」模块页：三步引导壳，全程转调既有入口。"""

    # ② 打开【备份与恢复】页（主窗口 _goto_page，树高亮同步）
    go_backup_requested = Signal()
    # ③ 转接入账中心盘点（主窗口跳页并代点【扫描游戏目录】）
    rescan_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo      # 保留统一构造签名（本页不直接读库）
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        self._build_ui()
        self._reset_flow()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 整页装进滚动区（与migrationPage同款）：窗口矮就出滚动条
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("恢复旧版本", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"）
        note = QFrame(self)
        note.setObjectName("rescue_note")
        note.setStyleSheet(
            "QFrame#rescue_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：① 确认档案 → ② 点【打开备份与恢复页】，勾选恰好"
            "一份要回到的备份，点「对选中 ▾ → 恢复所选」→ ③ 回本页"
            "对齐账与盘：入账中心右键「撤销确认」+ 盘点认领。",
            "为什么放心：恢复前引擎会先把当前版本复制进备份区（自动留"
            "退路），恢复出错会自动回退，不会把现有内容弄丢；钉住的"
            "备份豁免自动清理，适合长期留底。",
            "术语与关系：备份 = 某个时刻的完整内容副本；恢复 = 把那份"
            "副本复制回下载目录。恢复后账本里「本地版本」仍记着恢复前"
            "确认的版本号——账与盘暂时不一致，第③步的两步把它对齐"
            "（撤销确认 → 盘点认领，账本如实回到旧版本）。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet(f"border:none; color:#8a8a8f; font-size: {font_px(12)}px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # 三张固定步骤卡片（整页常驻，状态随流程更新）
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 档案卡：只显示归属与空态引导
        self._d1, _ = self._make_card("step1", "① 确认目标档案")

        # ② 备份卡：跳转按钮
        self._d2, box2 = self._make_card("step2", "② 到备份页恢复")
        row2 = QHBoxLayout()
        self._btn_backup = QPushButton("打开【备份与恢复】页", self)
        self._btn_backup.setToolTip(
            "跳到备份管理页：勾选恰好一份目标备份，点"
            "「对选中 ▾ → 恢复所选」；恢复前会自动备份当前版本")
        self._btn_backup.clicked.connect(self.go_backup_requested.emit)
        row2.addWidget(self._btn_backup)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ③ 对齐卡：盘点转接按钮（V2：撤销确认 + 盘点认领）
        self._d3, box3 = self._make_card("step3", "③ 恢复完成后：对齐账与盘")
        row3 = QHBoxLayout()
        self._btn_rescan = QPushButton("去入账中心盘点认领", self)
        self._btn_rescan.setToolTip(
            "跳到入账中心并自动开始扫描游戏目录：撤销确认后，盘上的"
            "旧版本会落成「待认领」候选，核对后认领入账")
        self._btn_rescan.clicked.connect(self.rescan_requested.emit)
        row3.addWidget(self._btn_rescan)
        row3.addStretch(1)
        box3.addLayout(row3)

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播（主窗口按 set_game 自动分发，零接线成本）。"""
        self._game = game
        self._reset_flow()

    # ---------- 内部：清场与卡片基建（样式与日常更新页同款） ----------
    def _reset_flow(self) -> None:
        """初始化与切档案共用：按钮与卡片回到待命状态。"""
        has = self._game is not None
        self._btn_backup.setEnabled(has)
        self._btn_rescan.setEnabled(has)

        if self._game is None:
            self._set_card(self._d1, "未选择档案——请先在左上角添加或选择游戏档案",
                           _C_WARN)
            self._set_card(self._d2, "等待选择档案。", _C_MUTED)
            self._set_card(self._d3, "恢复完成后回本页对齐账与盘。", _C_MUTED)
        else:
            self._set_card(
                self._d1,
                f"当前档案：{self._game.name}（{self._game.app_id}）——"
                "恢复动作发生在这个档案名下；换游戏请先在左上角切换",
                _C_OK)
            self._set_card(
                self._d2,
                "点下方按钮跳到备份页：勾选恰好一份目标备份"
                "（「版本时间」列帮你看回到哪个版本），点"
                "「对选中 ▾ → 恢复所选」。",
                _C_MUTED)
            self._set_card(
                self._d3,
                "恢复完成后做两件事对齐账与盘：\n"
                "① 到【入账中心 · 最近判决】右键该 mod「撤销确认」"
                "（账本回到未确认态，下次检测会重新报告）；\n"
                "② 点下方按钮去入账中心盘点，把盘上旧版本认领回账。\n"
                "不想对齐也能用：照常玩，想回新版重新下载即可。",
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
