"""恢复旧版本功能模块
"""
r"""gui/rescuePage.py —— 功能模块「恢复旧版本」（决策 34 第二档）。

引导壳，零新引擎：恢复的每一步都是既有功能的入口。备份区由备份
引擎管理——恢复前自动备份当前版本、恢复失败自动回退（备份页的
说明口径），这些保障住在引擎里，本页一个字不复制、一行不重写。

三步：
① 确认档案——恢复发生在当前界面档案名下（set_game 广播，与其他
   模块页同款）；
② 打开【备份与恢复】页，勾选恰好一份要回到的备份，用
   「对选中 ▾ → 恢复所选」。选哪份看「版本时间」列；
③ 恢复完成后回本页点【扫描确认】：转调 mod 库页的 quiet 扫描链
   重读本地状态；要核对远端版本再去【更新检测】。

本页只有两个转调信号（去备份页 / 请求扫描），不发任何改数据的
信号——恢复按钮在备份页、扫描链在 mod 库页，本页只带路。
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


class RescuePage(QWidget):
    """「恢复旧版本」模块页：三步引导壳，全程转调既有入口。"""

    # ② 打开【备份与恢复】页（主窗口 _goto_page(2)，树高亮同步）
    go_backup_requested = Signal()
    # ③ 扫描确认：主窗口转调 mod 库页 scan_local_quiet（当前档案）
    rescan_requested = Signal()

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

        title = QLabel("恢复旧版本", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"）
        note = QFrame(self)
        note.setObjectName("rescue_note")
        note.setStyleSheet("QFrame#rescue_note { border: 1px solid #3a3a3a;"
                           " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：① 确认档案 → ② 点【打开备份与恢复页】，勾选恰好一份"
            "要回到的备份，点「对选中 ▾ → 恢复所选」→ ③ 回本页点"
            "【扫描确认】重读本地状态。",
            "为什么放心：恢复前引擎会先把当前版本复制进备份区（自动留"
            "退路），恢复出错会自动回退，不会把现有内容弄丢；钉住的"
            "备份豁免自动清理，适合长期留底。",
            "术语与关系：备份 = 某个时刻的完整内容副本，每次备份独立"
            "成份；恢复 = 把那份副本复制回下载目录。回到哪个版本看"
            "「版本时间」列；恢复后想核对远端，去【更新检测】查。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
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

        # ③ 扫描卡：确认按钮
        self._d3, box3 = self._make_card("step3", "③ 恢复完成后：扫描确认")
        row3 = QHBoxLayout()
        self._btn_rescan = QPushButton("扫描确认", self)
        self._btn_rescan.setToolTip(
            "重新读取本地状态（与 mod 库页的扫描是同一条链，只写日志"
            "不弹窗）；要核对远端版本，去【更新检测】再查")
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

    def on_rescan_done(self, game_name: str) -> None:
        """扫描确认完成（主窗口跑完 quiet 扫描后回叫）。"""
        self._set_card(self._d3,
                       f"扫描确认完成（{game_name}）：本地状态已重读，"
                       "mod 库页点【刷新】即可看到各 mod 当前状态。",
                       _C_OK)

    # ---------- 内部：清场与卡片基建（样式与日常更新页同款） ----------

    def _reset_flow(self) -> None:
        """初始化与切档案共用：按钮与卡片回到待命状态。"""
        has = self._game is not None
        self._btn_backup.setEnabled(has)
        self._btn_rescan.setEnabled(has)
        if self._game is None:
            self._set_card(self._d1,
                           "未选择档案——请先在左上角添加或选择游戏档案",
                           _C_WARN)
            self._set_card(self._d2, "等待选择档案。", _C_MUTED)
            self._set_card(self._d3, "恢复完成后回本页点【扫描确认】。",
                           _C_MUTED)
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
                "恢复动作完成后点【扫描确认】重读本地状态；"
                "需要核对远端版本再去【更新检测】。",
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
