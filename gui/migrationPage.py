"""换机迁移功能模块
"""
"""
gui/migrationPage.py · 左导航「分步向导」组：七步换机迁移引导壳。

零新引擎：账本导出/导入、steamcmd 路径推导（编辑档案的推导按钮）、
联接检测、备份重定位、本地盘点，每一步都是既有功能的入口，本页只
排顺序、给按钮；数据搬运本身（工坊 content 与备份区 mod_backups 的
目录复制）在软件外完成，本软件不碰那些文件。

与 V1 的两处不同（V2 判决制适配，记事本挂账项落地）：
- 第⑥步「连接指引」→ 跳【联接检测】页（原连接指引对话框已退役，
  页面版能力一致：检测通过会自动记录游戏读取目录）；
- 第⑦步「扫描确认」→ 转接【入账中心】的【扫描游戏目录】：账本里的
  版本快照随完整账本一起迁来，但"账上记的"和"盘上装的"要对过才算
  数——盘上有文件而账上没版本的条目落成待认领候选，由用户核对后
  认领入账（确认门纪律：机器不替人写版本，与全项目同一口径）。

两个必须说死的语义：
- 完整账本导入 = 整体替换（清库重灌），与分享包的"增量并入"不同。
  新机首次导入无损失；日后重复导入会覆盖当前数据——确认弹窗
  （默认"否"）与"批次进行中拒绝"都住在主窗口原处，本页不复制；
- 标题、版本快照、备份登记都随完整账本一起过来，所以迁移后不需要
  重查远端——盘点只负责把"账与盘"对齐。想刷新远端状态另跑
  【更新检测】。

七个转调信号，主窗口逐一转调既有方法；本页不发任何改数据的信号。
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


class MigrationPage(QWidget):
    """「换机迁移」模块页：七步引导，旧机一步、新机六步。"""

    # ① 导出完整账本（主窗口转调 _export_ledger：纯读，保存框在原处）
    export_requested = Signal()
    # ② 导入完整账本（主窗口转调 _import_ledger：整体替换，确认框在原处）
    import_requested = Signal()
    # ③ 打开设置页（填 steamcmd 路径）
    open_settings_requested = Signal()
    # ④ 编辑当前档案（对话框里有【改回推导值】）
    open_edit_requested = Signal()
    # ⑥a 打开联接检测页（重建联接；V1 的连接指引对话框已退役，跳页同一能力）
    open_link_requested = Signal()
    # ⑥b 重定位备份目录（指认备份文件夹新位置）
    open_relocate_requested = Signal()
    # ⑦ 转接入账中心盘点（主窗口跳页并代点【扫描游戏目录】）
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

        # 整页装进滚动区：窗口不够高就出纵向滚动条，内容永不被裁
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("换机迁移", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"）
        note = QFrame(self)
        note.setObjectName("migration_note")
        note.setStyleSheet(
            "QFrame#migration_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
                "流程：① 在旧机点【导出完整账本】带走 JSON → 新机装好本软件"
                "后：② 导入账本 → ③ 设置页填 steamcmd 路径 → ④ 逐档案"
                "【改回推导值】修正下载目录 → ⑤ 在软件外把工坊内容与备份区"
                "复制到新机 → ⑥ 按需重建联接（联接检测页）、指认备份位置"
                " → ⑦ 逐档案【盘点确认】（转接入账中心）。第①步只在旧机"
                "做一次，其余在新机按档案推进。",
                "为什么这么做：账本导出/导入、目录推导、联接检测、备份"
                "重定位、本地盘点用的全是既有功能，本页只排顺序、给入口；"
                "数据搬运本身在软件外完成（content 与 mod_backups 目录"
                "复制），软件不碰那些文件。",
                "术语与关系：完整账本 = 全部档案 + 全部 mod 记录（标题、"
                "快照、备份登记都在内），导入是整体替换——与分享包的"
                "「增量并入」不同；下载目录永远等于按 steamcmd 位置推导的"
                "标准路径；备份登记与实际文件对不上时用「重定位备份目录」"
                "修复；迁移后的「账与盘对齐」由入账中心的盘点认领完成。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet(f"border:none; color:#8a8a8f; font-size: {font_px(12)}px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # 七张固定步骤卡片（整页常驻，状态随档案切换更新）
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 离开旧机：导出完整账本（与档案无关，恒可点）
        self._d1, box1 = self._make_card("step1", "① 离开旧机：导出完整账本")
        self._btn_export = self._add_button(
            box1, "导出完整账本…",
            "把全部档案与全部 mod 记录（含快照、备份登记、判决史）"
            "导出为一个 JSON 文件；纯读操作，不影响旧机数据。"
            "保存框默认定位到本软件的 data 目录，建议文件名已备好")
        self._btn_export.clicked.connect(self.export_requested.emit)

        # ② 到新机：导入完整账本
        self._d2, box2 = self._make_card("step2", "② 到新机：导入完整账本")
        self._btn_import = self._add_button(
            box2, "导入完整账本…",
            "从导出的 JSON 恢复账本。注意：导入会整体替换当前账本"
            "（清库重灌），动手前会再次确认；下载批次进行中会被拒绝")
        self._btn_import.clicked.connect(self.import_requested.emit)

        # ③ 配置 steamcmd
        self._d3, box3 = self._make_card("step3", "③ 配置 steamcmd")
        self._btn_settings = self._add_button(
            box3, "打开设置页",
            "新机的 steamcmd 装好后填写程序路径；此后下载目录、备份根"
            "目录的推导都以它为钥匙")
        self._btn_settings.clicked.connect(self.open_settings_requested.emit)

        # ④ 逐档案修正下载目录（按当前档案）
        self._d4, box4 = self._make_card("step4", "④ 逐档案修正下载目录")
        self._btn_edit = self._add_button(
            box4, "编辑当前档案…",
            "编辑档案对话框里有【改回推导值】：按当前 steamcmd 位置"
            "重新推导下载目录并保存")
        self._btn_edit.clicked.connect(self.open_edit_requested.emit)

        # ⑤ 搬运内容与备份区：软件外完成，纯说明卡、无按钮
        self._d5, _ = self._make_card(
            "step5", "⑤ 搬运工坊内容与备份区（软件外完成）")

        # ⑥ 重建联接与指认备份（两个转调入口）
        self._d6, box6 = self._make_card("step6", "⑥ 重建联接与指认备份")
        self._btn_link = self._add_button(
            box6, "打开联接检测页…",
            "游戏 mod 目录与下载目录的联接在新机上需要重建：跳到"
            "【联接检测】页判定现状、按步骤接通；检测通过会自动记录"
            "游戏读取目录")
        self._btn_link.clicked.connect(self.open_link_requested.emit)
        self._btn_relocate = self._add_button(
            box6, "重定位备份目录…",
            "备份文件复制到了新位置时用：指认备份文件夹现在的位置，"
            "先预演能对回多少条，确认后只改档案字段、不动文件")
        self._btn_relocate.clicked.connect(self.open_relocate_requested.emit)

        # ⑦ 盘点确认收尾（按当前档案；V2 = 转接入账中心）
        self._d7, box7 = self._make_card("step7", "⑦ 盘点确认收尾")
        self._btn_rescan = self._add_button(
            box7, "去入账中心盘点确认",
            "跳到入账中心并自动开始扫描当前档案的游戏目录：盘上有"
            "文件、账上没版本的 mod 落成「待认领」候选，核对后"
            "【认领入账】——账与盘就算对齐了")
        self._btn_rescan.clicked.connect(self.rescan_requested.emit)

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播：④⑦两张卡跟随当前档案；其余卡与档案无关。"""
        self._game = game
        self._reset_flow()

    # ---------- 内部：清场与卡片基建（样式与日常更新页同款） ----------
    def _reset_flow(self) -> None:
        """初始化与切档案共用：④⑦随档案启停，其余卡静态文案。"""
        has = self._game is not None
        self._btn_edit.setEnabled(has)
        self._btn_rescan.setEnabled(has)

        # ①②③⑥ 静态
        self._set_card(
            self._d1,
            "在旧电脑点这里，把账本装进一个 JSON 文件带走（U 盘 / 网盘"
            "均可）。纯读操作，旧机数据原样不动。",
            _C_MUTED)
        self._set_card(
            self._d2,
            "在新电脑点这里选那个 JSON：全部档案、mod 记录（含标题与"
            "快照）一次性恢复，左上角下拉即刻可选全部档案。新机首次"
            "导入无损失；日后重复导入会覆盖当前数据——确认框会拦一道。",
            _C_MUTED)
        self._set_card(
            self._d3,
            "新机 steamcmd 安装好（或解压好）后，到设置页填 steamcmd "
            "程序路径——第④步的目录推导靠它。",
            _C_MUTED)
        self._set_card(
            self._d6,
            "只从自己目录读 mod 的游戏需要重建联接（到联接检测页按"
            "步骤接通）；备份文件换了位置就用重定位指认——备份总览"
            "「盘上」列失联的记录靠它对回来。",
            _C_MUTED)

        # ④⑦ 随档案
        if has:
            self._set_card(
                self._d4,
                f"当前档案「{self._game.name}」（{self._game.app_id}）："
                "打开编辑档案 → 点【改回推导值】，下载目录即对准新机 "
                "steamcmd 的位置；左上角切换档案，每个档案做一次。",
                _C_MUTED)
            self._set_card(
                self._d7,
                "content 复制完成后点这里：跳到入账中心自动盘点当前"
                "档案——盘上有文件、账上没版本的 mod 落成「待认领」"
                "候选，核对后【认领入账】。标题与版本快照已随账本带来，"
                "不必重查；想刷新远端状态再跑【更新检测】。换档案逐个"
                "处理，全部做完即迁移完成。",
                _C_MUTED)
        else:
            self._set_card(
                self._d4,
                "导入账本后（第②步）档案就有了；切到要处理的档案再操作。",
                _C_WARN)
            self._set_card(
                self._d7,
                "同第④步：先有档案，再逐个盘点确认。",
                _C_WARN)

        # ⑤ 静态说明卡
        self._set_card(
            self._d5,
            "用资源管理器或 robocopy 把旧机 "
            "steamapps\\workshop\\content\\<游戏AppID>\\ 整目录复制到"
            "新机 steamcmd 的同一位置；旧机的备份区（mod_backups）整"
            "目录一并带走。本步骤不涉及本软件——复制完进⑥⑦。",
            _C_MUTED)

    def _add_button(self, box: QVBoxLayout, text: str, tooltip: str) -> QPushButton:
        """往卡片内容布局里加一颗带 tooltip 的按钮，返回它。"""
        btn = QPushButton(text, self)
        btn.setToolTip(tooltip)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        box.addLayout(row)
        return btn

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
