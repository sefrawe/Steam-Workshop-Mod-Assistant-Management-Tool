"""清理与删除管理页
"""
r"""功能模块壳（决策 34 形态 · 决策 69 定案 · v2.32/v2.33 UI 对齐三页范本）。

本轮 UI 三位老师（照抄其纪律，不抄其业务）：
- 引导 = 日常更新页：顶部「流程/为什么这么做/术语与关系」三行说明块
  （决策 34③ 标准形态）+ 边框步骤卡 + 着色状态行；
- 筛选/排序 = mod 库页：状态下拉（显示中文、currentData 存库内原值）
  + 关键词框（即时行过滤，只隐藏不改数据）+ 排序下拉（重灌数据）；
- 行内件与右键 = 备份管理页：每行 ↗ 点击开工坊页面、右键菜单与
  行内控件同一组选项、操作对象按行内 id 定位不按行号。

刻意不做表头点击排序：清单行内挂着动作下拉/勾选框，表头排序会让
控件随行搬家，行号与数据错位在删除场景不可接受——排序用下拉重灌
（重排前收割已选动作、重灌后按 id 原样还原，选择不丢）。

过滤的两条纪律（mod 库页批量纪律移植）：
- 「全选推荐」只作用于当前显示（未被过滤隐藏）的行——不发生隐形
  选择；「全部重置」作用于所有行（重置是保守方向，可以放宽）；
- 已显式选好的动作不受过滤影响照常执行，隐藏行也照常参与收集——
  执行前的确认弹窗会全量列出，那是最后一道闸。

分工与边界（不变）：检测全在引擎 workflows/deleteManageFlow；本页
只管收集勾选、确认、展示；引擎只读盘点、逐条处置；工作线程逐条
调用、批间停止（决策 62：绝不删一半）；软删除条目的备份文件不在
本页删；游戏本体 mod 配置/存档只给指引不代劳（决策 69② 总纲）。

对外（MainWindow 接线，信号与方法名与上一版完全一致，零重新接线）：
- set_game(game) / shutdown()；
- goto_ledger_requested / goto_import_requested：孤儿认领跳转；
- ledger_changed：处置落账 → MainWindow 强制 mod 库页重读。
"""
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import steamPaths
from core.appSettings import AppSettings
from core.models import Game
from core.urlParser import WORKSHOP_URL_TEMPLATE  # 模板单源（决策 61④）
from gui.collapsibleSection import CollapsibleSection as _Section
from gui.consolePanel import LogBus
from gui.formatters import fmt_size, status_zh  # 状态显示走单源（踩坑㉔）
from workflows import deleteManageFlow as dmf
from workflows.exceptionFlow import MULTIFRONTEND_NOTE

# 状态色（与日常更新页/备份页同一套语义）
_C_OK = "#46a758"     # 完成 / 一切正常
_C_WARN = "#f5a623"   # 需要注意
_C_INFO = "#d4d4d4"   # 进行中 / 中性
_C_MUTED = "#8a8a8f"  # 说明文字 / 待命

# ---- 列布局（备份页纪律：列号显式起名，填充代码不写魔法数字）----
M_URL, M_ID, M_TITLE, M_BACKUPS, M_ACT = range(5)          # ② 账有盘无
C_URL, C_ID, C_TITLE, C_STATUS, C_SIZE, C_ACT = range(6)   # ③ 盘上可清
O_CHK, O_URL, O_ID, O_SIZE, O_ACF = range(5)               # ④ 孤儿目录

# 各表自己的 ↗ 列（点击开工坊页面按表查列，绝不做跨表集合判断——
# 编号列在②③表同为 1，跨表集合判断会把"点编号"误判成"点 ↗"）
_URL_COL = {"missing": M_URL, "clean": C_URL, "orphan": O_URL}

# ③ 排序下拉：文案 → 行排序键（对引擎行对象排序，重灌式）
_CLEAN_SORTS = (
    ("按编号（小→大）", lambda r: r.mod_id),
    ("按盘上大小（大→小）", lambda r: -(r.dir_size or 0)),
    ("按标题（A→Z）", lambda r: (r.title or "").casefold()),
)


class _ScanWorker(QThread):
    """盘点线程：引擎 inventory 全程只读。acf 结构损坏走 failed
    （要向用户解释的情况），预期外异常走 crashed（bug 性质）。"""
    done = Signal(object)
    failed = Signal(str)
    crashed = Signal(str)

    def __init__(self, repo, game, steamcmd_path, parent=None) -> None:
        super().__init__(parent)
        self._repo, self._game, self._path = repo, game, steamcmd_path

    def run(self) -> None:
        try:
            self.done.emit(dmf.inventory(self._repo, self._game, self._path))
        except ValueError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 —— 线程边界兜底
            self.crashed.emit(str(exc))


class _ExecuteWorker(QThread):
    """处置执行线程：逐条调用引擎 execute_action，条与条之间检查
    停止请求（决策 62：批间停止，绝不删一半——单条动作内部不打断）。"""
    progress = Signal(int, int, str)     # 第i条/共n条, 说明
    finished_run = Signal(object)        # {"results": [...], "stopped": bool}
    crashed = Signal(str)

    def __init__(self, repo, actions, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._actions = actions
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        results: list = []
        total = len(self._actions)
        stopped = False
        try:
            for i, act in enumerate(self._actions, 1):
                if self._stop:
                    stopped = True
                    break
                res = dmf.execute_action(self._repo, act)
                results.append(res)
                self.progress.emit(i, total,
                                   f"{act.kind} mod {act.mod_id}: {res.detail}")
        except Exception as exc:  # noqa: BLE001
            self.crashed.emit(str(exc))
            return
        self.finished_run.emit({"results": results, "stopped": stopped})


class DeletePage(QWidget):
    goto_ledger_requested = Signal()   # 去 mod 库页（扫描本地认领 / 右键手动确认）
    goto_import_requested = Signal()   # 去导入页（贴网址认领）
    ledger_changed = Signal()          # 处置落账 → MainWindow 强制 mod 库页重读

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._report: dmf.InventoryReport | None = None
        self._scan_worker: _ScanWorker | None = None
        self._exec_worker: _ExecuteWorker | None = None
        self._mods_by_id: dict[int, object] = {}  # 标题/网址对照（↗ 与标题列用）
        self._clean_sort = 0                       # ③ 排序下拉当前档
        self._init_ui()

    # ---------- 骨架 ----------
    def _init_ui(self) -> None:
        # 整页滚动：明细区固定高度后总高度随内容变化，装进 QScrollArea
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self._body = body  # 卡片工厂要用（局部变量出不了这个方法）
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("清理与删除", body)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # —— 说明块（日常更新页同款三行，决策 34③）——
        note = QFrame(body)
        note.setObjectName("delete_note")
        note.setStyleSheet(
            "QFrame#delete_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
                "流程：① 确认档案 → ② 点【开始盘点】对照账本与 content 目录"
                "（只读，不动任何东西）→ ③ 在三节清单里逐行选动作（默认全部"
                "「不动」，可先筛选、排序缩小范围）→ ④ 点【执行所选处置】，"
                "确认弹窗列出将删什么，默认「否」→ 执行完自动重新盘点。",
                "为什么这么做：删除最怕删错——先盘点、后动手；每行默认"
                "「不动」、确认默认「否」；执行逐条进行、可中途停止（停在条"
                "与条之间）；steamcmd 正在下载时只警告不拦（误删的条目重跑"
                "下载命令就能回来）。",
                "术语与关系：「账有盘无」= 账本记着已下载、盘上文件夹没了；"
                "「盘上可清」= 账内 mod 还占着 content 目录；「孤儿目录」= "
                "盘上有、账本完全不认识。已删除记录的恢复在【mod 库】页右键；"
                "备份的逐份增删在【备份管理】页；失效条目的处置在【异常处理】页。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # steamcmd 运行横幅（备份页同款：在跑时只警告不拦截）
        self._banner = QLabel(body)
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(f"color: {_C_WARN};")
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        self._lbl_game = QLabel("", body)
        self._lbl_game.setWordWrap(True)
        root.addWidget(self._lbl_game)

        # ---- 卡① 盘点（常驻） ----
        _, box1 = self._make_card(root, "scan", "① 盘点：把账与盘的差距摆到明面上")
        lbl = QLabel(
            "做什么：对照账本和 steamcmd 的 content 目录，把三类东西列成"
            "清单——账有盘无 / 盘上可清 / 孤儿目录。\n"
            "为什么：删除类操作最怕删错，先盘点就是把「将删什么」全部摆在"
            "动手之前。\n"
            "你的选择：盘点本身不动任何东西；看完结果再逐条决定。", body)
        lbl.setWordWrap(True)
        box1.addWidget(lbl)
        row = QHBoxLayout()
        self._btn_scan = QPushButton("开始盘点", body)
        self._btn_scan.setToolTip(
            "只读扫描：对照账本与盘上目录并统计大小。大目录统计可能要几秒"
            "到几十秒，期间不打扰你操作别的页面")
        self._btn_scan.clicked.connect(self._start_scan)
        row.addWidget(self._btn_scan)
        self._lbl_scan = QLabel("", body)
        self._lbl_scan.setWordWrap(True)
        row.addWidget(self._lbl_scan, 1)
        box1.addLayout(row)

        # ---- 卡② 账有盘无 ----
        self._box_missing, box2 = self._make_card(
            root, "missing", "② 账有盘无：账本说已下载、盘上没了")
        lbl = QLabel(
            "为什么会出现：文件夹被手动删过、换过盘、或被别的工具删掉。"
            "处置只动账本、不碰磁盘（盘上本来就没有）。\n"
            "「软删除」到底动了什么：只动账本——状态改成「已删除」，"
            "记下删除时间，并把删除前的关键信息（标题、版本、大小、"
            "备注等）存成「删除前快照」（mod 库页右侧详情面板的"
            "「软删除」节可查）。磁盘上一个文件都不动：下载的内容"
            "文件夹、备份文件、备份登记全部原样保留。\n"
            "软删除之后：更新检测不再查询它、下载命令生成页不再列出"
            "它、扫描也不再收录它；但【mod 库】页状态下拉选「已删除」"
            "仍能找到它，右键「恢复」随时回。\n"
            "文件往后怎么办（软删除不替你决定，都不急）：\n"
            "① 一直留着——哪天想恢复，文件还在就是完整的「已下载」；\n"
            "② 想腾空间——它会出现在本页③「盘上可清」清单里，单独选"
            "「删文件」即可（删的是文件不是账，重新下载还能回来；"
            "恢复后文件缺了的话，盘点/核验会以「账有盘无」如实报出）；\n"
            "③ 备份——软删除不碰备份文件与备份登记，逐份增删走"
            "【备份管理】页。\n"
            "彻底清账 = 记录与快照物理删除、不可恢复，备份登记随之"
            "清除（磁盘备份文件默认保留成普通文件夹，想一并带走"
            "用下面的勾）。", body)
        lbl.setWordWrap(True)
        box2.addWidget(lbl)


        # 工具条：过滤（mod 库页同款交互）
        self._bar_missing = QWidget(body)
        h1 = QHBoxLayout(self._bar_missing)
        h1.setContentsMargins(0, 0, 0, 0)
        h1.addWidget(QLabel("过滤", self._bar_missing))
        self._filter_missing = QLineEdit(self._bar_missing)
        self._filter_missing.setPlaceholderText(
            "输入标题或编号，隐藏不匹配的行（清空恢复全部）")
        self._filter_missing.setClearButtonEnabled(True)
        self._filter_missing.setToolTip(
            "只隐藏显示，不改任何数据；隐藏的行不参与【全选推荐】，"
            "但已显式选好的动作照常执行——执行前确认弹窗会全量列出")
        self._filter_missing.textChanged.connect(self._apply_filters)
        h1.addWidget(self._filter_missing, 1)
        box2.addWidget(self._bar_missing)
        self._tbl_missing = self._make_table(5, M_TITLE)
        self._tbl_missing.setHorizontalHeaderLabels(
            ["↗", "编号", "标题", "备份登记", "动作"])
        for col, w in ((M_URL, 36), (M_ID, 110), (M_BACKUPS, 190), (M_ACT, 260)):
            self._tbl_missing.setColumnWidth(col, w)
        self._sec_missing = _Section("问题明细（0 行）", body)
        self._sec_missing.set_content(self._tbl_missing, 240)
        box2.addWidget(self._sec_missing)
        self._chk_backup_files = QCheckBox(
            "彻底清账的条目：同时删除磁盘备份文件（默认保留）", body)
        self._chk_backup_files.setToolTip(
            "不勾：备份文件留在原地，成为未登记的普通文件夹，可日后手动删；\n"
            "勾：随执行一并删除（不可恢复）。个别备份的增删平时走备份总览页")
        box2.addWidget(self._chk_backup_files)
        btn_row = QHBoxLayout()
        self._btn_rec_missing = QPushButton("全选推荐（软删除）", body)
        self._btn_rec_missing.setToolTip(
            "把当前显示（未被过滤隐藏）的行全部设为软删除——被过滤藏起来"
            "的行不参与，不发生隐形选择")
        self._btn_rec_missing.clicked.connect(
            lambda: self._recommend(self._tbl_missing, M_ACT, "soft"))
        btn_row.addWidget(self._btn_rec_missing)
        self._btn_reset_missing = QPushButton("全部重置", body)
        self._btn_reset_missing.setToolTip(
            "把这一节所有行（含被过滤隐藏的行）的动作全部改回「不动」")
        self._btn_reset_missing.clicked.connect(
            lambda: self._reset_actions(self._tbl_missing, M_ACT))
        btn_row.addWidget(self._btn_reset_missing)
        btn_row.addStretch(1)
        box2.addLayout(btn_row)

        # ---- 卡③ 盘上可清 ----
        self._box_clean, box3 = self._make_card(
            root, "clean", "③ 盘上可清：账内 mod 占着的 content 目录")
        lbl = QLabel(
            "做什么：删除这些 mod 在 steamcmd content 目录里的文件夹"
            "（空间大头）。\n"
            "为什么分开「是否连账面一起处置」：只删文件的话，账本还记着"
            "「已下载」，下次盘点会以「账有盘无」出现在【异常处理】页——"
            "所以推荐连账面一起处置，账实保持一致。想留着以后重下的，选"
            "「仅删文件」即可。\n"
            "回程票：删掉的文件重新下载就能回来（命令生成页勾选该编号）。"
            "软删除的记录随时可在【mod 库】页右键恢复。执行键在第5步。", body)
        lbl.setWordWrap(True)
        box3.addWidget(lbl)
        # 工具条：状态 + 过滤 + 排序（mod 库页筛选条同款三件套）
        self._bar_clean = QWidget(body)
        h2 = QHBoxLayout(self._bar_clean)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.addWidget(QLabel("状态", self._bar_clean))
        self._status_filter = QComboBox(self._bar_clean)
        for label, val in (("全部", None), ("已下载", "downloaded"),
                           ("已删除", "deleted"), ("已失败", "failed")):
            self._status_filter.addItem(label, val)
        self._status_filter.setToolTip(
            "按账本状态过滤显示（与 mod 库页状态下拉同款：显示中文、"
            "内部存原值）")
        self._status_filter.currentIndexChanged.connect(self._apply_filters)
        h2.addWidget(self._status_filter)
        h2.addWidget(QLabel("过滤", self._bar_clean))
        self._filter_clean = QLineEdit(self._bar_clean)
        self._filter_clean.setPlaceholderText(
            "输入标题或编号，隐藏不匹配的行（清空恢复全部）")
        self._filter_clean.setClearButtonEnabled(True)
        self._filter_clean.setToolTip(
            "只隐藏显示，不改任何数据；隐藏的行不参与【全选推荐】，"
            "但已显式选好的动作照常执行——执行前确认弹窗会全量列出")
        self._filter_clean.textChanged.connect(self._apply_filters)
        h2.addWidget(self._filter_clean, 1)
        h2.addWidget(QLabel("排序", self._bar_clean))
        self._sort_combo = QComboBox(self._bar_clean)
        for label, _key in _CLEAN_SORTS:
            self._sort_combo.addItem(label)
        self._sort_combo.setToolTip(
            "重排清单（数据重灌，不用表头排序——行内挂着动作下拉，"
            "表头排序会让控件搬家错位）；重排不丢已选动作")
        self._sort_combo.currentIndexChanged.connect(self._on_clean_sort)
        h2.addWidget(self._sort_combo)
        box3.addWidget(self._bar_clean)
        self._tbl_clean = self._make_table(6, C_TITLE)
        self._tbl_clean.setHorizontalHeaderLabels(
            ["↗", "编号", "标题", "状态", "盘上大小", "动作"])
        for col, w in ((C_URL, 36), (C_ID, 110), (C_STATUS, 90),
                       (C_SIZE, 110), (C_ACT, 280)):
            self._tbl_clean.setColumnWidth(col, w)
        self._sec_clean = _Section("清理清单（0 行）", body)
        self._sec_clean.set_content(self._tbl_clean, 600)
        box3.addWidget(self._sec_clean)
        btn_row = QHBoxLayout()
        self._btn_rec_clean = QPushButton("全选推荐", body)
        self._btn_rec_clean.setToolTip(
            "按每行状态给推荐动作（只作用于当前显示的行）：已下载→删文件"
            "+软删除；已删除→删文件；已失败→仅删文件。选完逐行过一眼再执行")
        self._btn_rec_clean.clicked.connect(self._recommend_clean)
        btn_row.addWidget(self._btn_rec_clean)
        self._btn_reset_clean = QPushButton("全部重置", body)
        self._btn_reset_clean.setToolTip(
            "把这一节所有行（含被过滤隐藏的行）的动作全部改回「不动」")
        self._btn_reset_clean.clicked.connect(
            lambda: self._reset_actions(self._tbl_clean, C_ACT))
        btn_row.addWidget(self._btn_reset_clean)
        btn_row.addStretch(1)
        box3.addLayout(btn_row)

        # ---- 卡④ 孤儿目录 ----
        self._box_orphan, box4 = self._make_card(
            root, "orphan", "④ 孤儿目录：盘上有、账本完全不认识")
        olbl = QLabel(
            "来源：acf 被换过、别的工具留下的残骸、或彻底清账后的文件。"
            "默认全部不选——来路不明的目录要逐个确认。\n"
            "认领（推荐先想这条路）：acf 认识的编号，到【mod 库】页点"
            "【扫描本地】即可自动入库认领；acf 不认识的，到导入页贴网址"
            "登记 + 右键「确认已下载（手动）」。删除不可恢复，且多前端"
            "环境里别的工具（如 RimSort）清单里留着的 mod 会被它指挥"
            "下回来——悬浮此处可看多前端自查要点。", body)
        olbl.setWordWrap(True)
        olbl.setToolTip(MULTIFRONTEND_NOTE)
        box4.addWidget(olbl)
        self._bar_orphan = QWidget(body)
        h3 = QHBoxLayout(self._bar_orphan)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.addWidget(QLabel("过滤", self._bar_orphan))
        self._filter_orphan = QLineEdit(self._bar_orphan)
        self._filter_orphan.setPlaceholderText(
            "输入编号等关键词，隐藏不匹配的行（清空恢复全部）")
        self._filter_orphan.setClearButtonEnabled(True)
        self._filter_orphan.setToolTip(
            "只隐藏显示，不改任何数据；勾选状态不受过滤影响")
        self._filter_orphan.textChanged.connect(self._apply_filters)
        h3.addWidget(self._filter_orphan, 1)
        box4.addWidget(self._bar_orphan)
        self._tbl_orphan = self._make_table(5, O_ACF)
        self._tbl_orphan.setHorizontalHeaderLabels(
            ["选择", "↗", "编号", "大小", "acf 认识?"])
        for col, w in ((O_CHK, 40), (O_URL, 36), (O_ID, 120), (O_SIZE, 100)):
            self._tbl_orphan.setColumnWidth(col, w)
        self._sec_orphan = _Section("孤儿清单（0 个）", body)
        self._sec_orphan.set_content(self._tbl_orphan, 240)
        box4.addWidget(self._sec_orphan)
        btn_row = QHBoxLayout()
        self._btn_claim_scan = QPushButton("去 mod 库页认领（扫描本地）", body)
        self._btn_claim_scan.clicked.connect(self.goto_ledger_requested.emit)
        btn_row.addWidget(self._btn_claim_scan)
        self._btn_claim_import = QPushButton("去导入页登记网址", body)
        self._btn_claim_import.clicked.connect(self.goto_import_requested.emit)
        btn_row.addWidget(self._btn_claim_import)
        btn_row.addStretch(1)
        box4.addLayout(btn_row)
        self._btn_claim_scan.setToolTip(
            "跳到【mod 库】页点【扫描本地】：acf 认识的编号会自动入库认领")
        self._btn_claim_import.setToolTip(
            "跳到【网址批量导入】页把编号登记进账本，再到 mod 库页"
            "右键「确认已下载（手动）」完成认领")


        # ---- 卡⑤ 执行（常驻） ----
        _, box5 = self._make_card(root, "exec", "⑤ 执行所选处置")
        self._bar = QProgressBar(body)
        self._bar.setVisible(False)
        box5.addWidget(self._bar)
        self._lbl_progress = QLabel("", body)
        self._lbl_progress.setWordWrap(True)
        box5.addWidget(self._lbl_progress)
        row = QHBoxLayout()
        self._btn_exec = QPushButton("执行所选处置…", body)
        self._btn_exec.setToolTip(
            "先弹确认清单（将删什么、多大、哪些可逆），确认后才动手；"
            "执行中可点停止——停在条与条之间，绝不删一半")
        self._btn_exec.clicked.connect(self._on_execute)
        self._btn_stop = QPushButton("停止", body)
        self._btn_stop.setEnabled(False)
        self._btn_stop.setToolTip(
            "请求停止：当前这一条做完后不再继续（绝不把一个目录删一半）")
        self._btn_stop.clicked.connect(self._on_stop)
        row.addWidget(self._btn_exec)
        row.addWidget(self._btn_stop)
        row.addStretch(1)
        box5.addLayout(row)
        root.addStretch(1)

        # ---- 行内交互接线（上一版漏掉的整段）：↗ 点击 + 右键菜单。
        # 三张表同一组纪律；闭包用关键字默认值把表与类型当场定死
        # （迟到绑定防呆，v2.32 实证写法）----
        for tbl, kind in ((self._tbl_missing, "missing"),
                          (self._tbl_clean, "clean"),
                          (self._tbl_orphan, "orphan")):
            tbl.cellClicked.connect(
                lambda r, c, t=tbl, k=kind: self._on_cell_clicked(t, k, r, c))
            tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tbl.customContextMenuRequested.connect(
                lambda pos, t=tbl, k=kind: self._on_row_menu(t, k, pos))

        self._set_results_visible(False)

    def _make_card(self, root: QVBoxLayout, name: str,
                   title: str) -> tuple[QFrame, QVBoxLayout]:
        """造一张步骤卡（日常更新页同款样式）。返回 (卡片框架, 内容布局)：
        框架留给显隐控制，布局留给调用方装内容。"""
        frame = QFrame(self._body)
        frame.setObjectName(f"card_{name}")
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        box.addWidget(title_lbl)
        root.addWidget(frame)
        return frame, box

    def _make_table(self, cols: int, stretch_col: int) -> QTableWidget:
        """清单表通用底子：只读、无行头、斑马纹（核验页同款观感）、
        指定列拉伸占满剩余宽度。"""
        t = QTableWidget(0, cols)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setWordWrap(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.horizontalHeader().setSectionResizeMode(
            stretch_col, QHeaderView.ResizeMode.Stretch)
        return t

    # ---------- 对外 ----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._report = None
        self._mods_by_id = {}
        if game is None:
            self._lbl_game.setText(
                "（还没有选择游戏档案）——请先在左上角添加或选择档案")
        else:
            self._lbl_game.setText(
                f"当前档案：{game.name}（AppID {game.app_id}）——"
                "盘点与处置只针对这一个档案")
        # 过滤条件清干净：上个档案的关键词/状态不该管到这个档案的清单
        for e in (self._filter_missing, self._filter_clean,
                  self._filter_orphan):
            e.blockSignals(True)
            e.clear()
            e.blockSignals(False)
        self._status_filter.blockSignals(True)
        self._status_filter.setCurrentIndex(0)
        self._status_filter.blockSignals(False)
        self._set_results_visible(False)
        self._set_colored(self._lbl_scan, "", _C_MUTED)
        self._banner.setVisible(False)

    def shutdown(self) -> None:
        """关窗收尾（MainWindow.closeEvent 的页面循环自动发现并调用）：
        处置线程先请求条间停止、再等它收尾；盘点线程只读、直接等跑完。
        两条都是「等」——绝不销毁活线程；处置的单条动作内部没有安全
        停止点（决策 62），wait() 等到当前条做完为止是唯一正确语义。"""
        w = self._exec_worker
        if w is not None:
            w.request_stop()
            w.wait()
            self._exec_worker = None
        s = self._scan_worker
        if s is not None:
            s.wait()
            self._scan_worker = None

    # ---------- 盘点 ----------
    def _start_scan(self) -> None:
        if self._game is None:
            QMessageBox.information(
                self, "请先选择档案",
                "盘点按当前游戏档案执行——请先在左上角添加或选择游戏档案。")
            return
        if self._steamcmd_root() is None:
            QMessageBox.warning(
                self, "盘点",
                "尚未设置 steamcmd 程序路径，无法定位 content 目录。\n"
                "请先到设置页填写 steamcmd 程序（steamcmd.exe）的完整路径。")
            return
        # 下载目录死路径核对（与扫描本地同款：steamcmd 挪位后重推导并写回）
        effective, changed = steamPaths.refresh_download_dir(
            self._game.download_dir, self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id,
                                   download_dir=effective)
            self._game = self._repo.get_game(self._game.app_id)
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective}")

        self._btn_scan.setEnabled(False)
        self._set_colored(self._lbl_scan,
                          "正在盘点并统计大小（大目录可能要一会儿）…", _C_INFO)
        self._scan_worker = _ScanWorker(self._repo, self._game,
                                        self._settings.get("steamcmd_path"))
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.crashed.connect(self._on_crashed)
        self._scan_worker.start()

    def _on_scan_failed(self, msg: str) -> None:
        self._btn_scan.setEnabled(True)
        self._set_colored(self._lbl_scan, "", _C_MUTED)
        QMessageBox.critical(self, "盘点失败", msg)

    def _on_scan_done(self, rep) -> None:
        self._btn_scan.setEnabled(True)
        # 过期守卫：盘点期间切了档案 → 按报告里的 game_id 比对丢弃，
        # 绝不把 A 档案的清单灌进 B 档案的界面
        if self._game is None or rep.game_id != self._game.app_id:
            return
        self._report = rep
        if rep.steamcmd_missing:
            self._show_banner("未配置 steamcmd 程序路径：盘上两节不可判。"
                              "请到设置页填写后再盘点。")
            return
        if rep.dead_root:
            self._show_banner(
                f"下载目录不存在：{rep.download_dir}\n"
                "已尝试按当前 steamcmd 位置重推导仍无效——请检查 steamcmd "
                "是否还在设置页填的位置。")
            return
        if rep.steamcmd_running:
            self._show_banner(
                "steamcmd 正在运行：此刻执行删除可能与下载争用。"
                "被误删的条目重跑下载命令即可回来（steamcmd 会自建条目）——"
                "警告不拦截，是否继续由你决定。")
        else:
            self._banner.setVisible(False)
        # 标题/网址对照表一次取齐（↗ 点击时只查字典，不查库；备份页同款）
        self._mods_by_id = {m.mod_id: m
                            for m in self._repo.list_mods(self._game.app_id)}
        self._fill_tables(rep)
        self._set_results_visible(True)
        n_missing = len(rep.missing_rows)
        n_clean = len(rep.cleanable_rows)
        n_orphan = len(rep.orphans)
        # 分区标题带计数 + 空桶自动收起 + 空桶配套控件隐藏（核验页同款）
        self._refresh_buckets(n_missing, n_clean, n_orphan)
        tracked_on_disk = sum(
            1 for r in rep.ledger_rows
            if r.status == "tracked" and r.dir_exists)
        if n_missing == n_clean == n_orphan == 0:
            self._set_colored(
                self._lbl_scan,
                "盘点完成：账实相符，盘上也没有孤儿目录——没有可清理项。",
                _C_OK)
            if tracked_on_disk:
                self._log.info(
                    f"另有 {tracked_on_disk} 个「已收录」条目盘上有目录："
                    "那是手动下载待确认，请到 mod 库页右键「确认已下载"
                    "（手动）」，不属于清理范围")
            return
        self._set_colored(
            self._lbl_scan,
            f"盘点完成：账有盘无 {n_missing} 个；盘上可清 {n_clean} 个"
            f"（共 {fmt_size(sum(r.dir_size or 0 for r in rep.cleanable_rows))}）；"
            f"孤儿 {n_orphan} 个。逐行选动作，或用【全选推荐】。",
            _C_INFO)

    def _show_banner(self, text: str) -> None:
        self._banner.setText(text)
        self._banner.setVisible(True)
        self._set_colored(self._lbl_scan, "", _C_MUTED)
        self._set_results_visible(False)

    # ---------- 分区与过滤 ----------
    def _refresh_buckets(self, n_missing: int, n_clean: int,
                         n_orphan: int) -> None:
        """分区标题带计数、空桶自动收起、空桶的配套控件（工具条/按钮）
        一并隐藏——0 行的桶不留空框，也不留没有对象的控件。"""
        self._sec_missing.set_title(f"问题明细（{n_missing} 行）")
        self._sec_missing.set_expanded(n_missing > 0)
        self._bar_missing.setVisible(n_missing > 0)
        self._chk_backup_files.setVisible(n_missing > 0)
        self._btn_rec_missing.setVisible(n_missing > 0)
        self._btn_reset_missing.setVisible(n_missing > 0)
        self._sec_clean.set_title(f"清理清单（{n_clean} 行）")
        self._sec_clean.set_expanded(n_clean > 0)
        self._bar_clean.setVisible(n_clean > 0)
        self._btn_rec_clean.setVisible(n_clean > 0)
        self._btn_reset_clean.setVisible(n_clean > 0)
        self._sec_orphan.set_title(f"孤儿清单（{n_orphan} 个）")
        self._sec_orphan.set_expanded(n_orphan > 0)
        self._bar_orphan.setVisible(n_orphan > 0)
        self._btn_claim_scan.setVisible(n_orphan > 0)
        self._btn_claim_import.setVisible(n_orphan > 0)

    def _apply_filters(self) -> None:
        """三节清单的行过滤（mod 库页同款交互：即时、只隐藏显示）。"""
        self._filter_rows(self._tbl_missing, self._filter_missing.text(),
                          None)
        self._filter_rows(self._tbl_clean, self._filter_clean.text(),
                          self._status_filter.currentData())
        self._filter_rows(self._tbl_orphan, self._filter_orphan.text(),
                          None)

    @staticmethod
    def _filter_rows(table: QTableWidget, text: str,
                     status_want: str | None) -> None:
        key = text.strip().casefold()
        for r in range(table.rowCount()):
            ok = True
            if key:
                ok = any(
                    key in (table.item(r, c).text().casefold()
                            if table.item(r, c) is not None else "")
                    for c in range(table.columnCount()))
            if ok and status_want is not None:
                it = table.item(r, C_STATUS)
                ok = (it is not None
                      and it.data(Qt.ItemDataRole.UserRole) == status_want)
            table.setRowHidden(r, not ok)

    # ---------- 填表 ----------
    def _fill_tables(self, rep) -> None:
        # —— ② 账有盘无：动作下拉（默认「不动」；tooltip 挂在条目入列之后）——
        rows = rep.missing_rows
        t = self._tbl_missing
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t.setItem(i, M_URL, self._make_url_item(r.mod_id))
            it = QTableWidgetItem(str(r.mod_id))
            # 行内 id：操作对象按 id 定位不按行号（备份页纪律）
            it.setData(Qt.ItemDataRole.UserRole, r.mod_id)
            t.setItem(i, M_ID, it)
            t.setItem(i, M_TITLE, QTableWidgetItem(r.title or "（无标题）"))
            t.setItem(i, M_BACKUPS, QTableWidgetItem(
                f"{r.backup_count} 份 / {fmt_size(r.backup_bytes)}"))
            combo = QComboBox()
            combo.addItem("不动", None)
            combo.addItem("软删除（推荐）", "soft")
            combo.setItemData(1, "记录保留，可随时恢复（推荐）",
                              Qt.ItemDataRole.ToolTipRole)
            combo.addItem("彻底清账（不可逆）", "purge")
            combo.setItemData(2, "记录物理删除，不可恢复；备份登记随账清除",
                              Qt.ItemDataRole.ToolTipRole)
            t.setCellWidget(i, M_ACT, combo)

        # —— ③ 盘上可清：动作按状态分派；排序在下拉里选（重灌式）——
        rows = list(rep.cleanable_rows)
        rows.sort(key=_CLEAN_SORTS[self._clean_sort][1])
        t = self._tbl_clean
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t.setItem(i, C_URL, self._make_url_item(r.mod_id))
            it = QTableWidgetItem(str(r.mod_id))
            it.setData(Qt.ItemDataRole.UserRole, r.mod_id)
            t.setItem(i, C_ID, it)
            t.setItem(i, C_TITLE, QTableWidgetItem(r.title or "（无标题）"))
            # 状态列：显示走 status_zh 单源（决策 11/踩坑㉔），英文原值进
            # UserRole 供推荐/过滤逻辑使用——显示与数据分离
            st = QTableWidgetItem(status_zh(r.status))
            st.setData(Qt.ItemDataRole.UserRole, r.status)
            t.setItem(i, C_STATUS, st)
            t.setItem(i, C_SIZE, QTableWidgetItem(fmt_size(r.dir_size or 0)))
            combo = QComboBox()
            combo.addItem("不动", None)
            if r.status == "downloaded":
                combo.addItem("删文件+软删除（推荐）", "wipe_soft")
                combo.setItemData(1, "账实一起收尾：文件删、记录保留可恢复"
                                     "（推荐）", Qt.ItemDataRole.ToolTipRole)
                combo.addItem("仅删文件", "wipe")
                combo.setItemData(2, "账本仍记着「已下载」——之后它会在"
                                     "【异常处理】页以「账有盘无」出现",
                                  Qt.ItemDataRole.ToolTipRole)
                combo.addItem("删文件+彻底清账（不可逆）", "wipe_purge")
                combo.setItemData(3, "文件与记录都不要了：记录物理删除，"
                                     "不可恢复", Qt.ItemDataRole.ToolTipRole)
            elif r.status == "deleted":
                combo.addItem("删文件（推荐）", "wipe")
                combo.setItemData(1, "账本已经是删除状态，只需清掉占地的文件",
                                  Qt.ItemDataRole.ToolTipRole)
                combo.addItem("删文件+彻底清账（不可逆）", "wipe_purge")
                combo.setItemData(2, "连删除记录一起物理删除，不可恢复",
                                  Qt.ItemDataRole.ToolTipRole)
            else:  # failed：处置链归异常页，本页只清文件
                combo.addItem("仅删文件（推荐）", "wipe")
                combo.setItemData(1, "失效原因的处置在【异常处理】页，"
                                     "这里只清占地的文件",
                                  Qt.ItemDataRole.ToolTipRole)
            t.setCellWidget(i, C_ACT, combo)

        # —— ④ 孤儿：勾选框，默认全不选（刻意不给批量全选——逐个确认）——
        rows = rep.orphans
        t = self._tbl_orphan
        t.setRowCount(len(rows))
        for i, o in enumerate(rows):
            chk = QCheckBox()
            chk.setToolTip("勾选 = 执行时删除该目录（不可恢复）")
            t.setCellWidget(i, O_CHK, chk)
            t.setItem(i, O_URL, self._make_url_item(o.mod_id))
            it = QTableWidgetItem(str(o.mod_id))
            it.setData(Qt.ItemDataRole.UserRole, o.mod_id)
            t.setItem(i, O_ID, it)
            t.setItem(i, O_SIZE, QTableWidgetItem(fmt_size(o.dir_size)))
            t.setItem(i, O_ACF, QTableWidgetItem(
                "认识——去扫描本地即可自动入库认领"
                if o.in_acf else
                "不认识——走导入网址+手动确认认领，或勾选删除"))
        self._apply_filters()

    def _make_url_item(self, mod_id: int) -> QTableWidgetItem:
        """↗ 单元格（备份页同款：文本 + 点击取数据，不逐行挂按钮部件——
        几百行挂部件又重又卡）。"""
        it = QTableWidgetItem("↗")
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it.setForeground(QBrush(QColor("#5aa9ff")))  # 蓝字提示可点
        it.setData(Qt.ItemDataRole.UserRole, mod_id)
        it.setToolTip("点击在浏览器打开该 mod 的创意工坊页面"
                      "（动手之前顺手看一眼它是什么）")
        return it

    def _on_clean_sort(self, idx: int) -> None:
        """重排③清单：数据重灌而非表头排序（理由见文件头）。重排前
        收割已选动作、重灌后按 id 原样还原——换排序不丢选择。"""
        self._clean_sort = idx
        if self._report is None:
            return
        prev = self._collect_request()
        self._fill_tables(self._report)
        self._restore_actions(prev)

    def _restore_actions(self, prev: dmf.PlanRequest) -> None:
        t = self._tbl_missing
        for r in range(t.rowCount()):
            mid = int(t.item(r, M_ID).data(Qt.ItemDataRole.UserRole))
            combo = t.cellWidget(r, M_ACT)
            combo.setCurrentIndex(max(combo.findData(
                prev.ledger_actions.get(mid)), 0))
        t = self._tbl_clean
        for r in range(t.rowCount()):
            mid = int(t.item(r, C_ID).data(Qt.ItemDataRole.UserRole))
            combo = t.cellWidget(r, C_ACT)
            combo.setCurrentIndex(max(combo.findData(
                prev.clean_actions.get(mid)), 0))
        t = self._tbl_orphan
        for r in range(t.rowCount()):
            mid = int(t.item(r, O_ID).data(Qt.ItemDataRole.UserRole))
            t.cellWidget(r, O_CHK).setChecked(mid in prev.orphan_ids)

    # ---------- 行内动作：↗ / 右键 / 推荐 / 重置 ----------
    def _on_cell_clicked(self, table: QTableWidget, kind: str,
                         row: int, col: int) -> None:
        """单元格点击：只认本表自己的 ↗ 列（按 kind 查表，绝不做跨表
        列号集合判断——编号列在②③表同为 1，会误触发）。"""
        if col != _URL_COL[kind]:
            return
        it = table.item(row, col)
        if it is None:
            return
        self._open_row_url(int(it.data(Qt.ItemDataRole.UserRole)))

    def _open_row_url(self, mod_id: int) -> None:
        """开工坊页面：网址取账本字段，缺省按模板现拼（决策 61④ 单源）。
        孤儿不在账本里，正好落到模板兜底——这正是模板单源存在的意义。"""
        m = self._mods_by_id.get(mod_id)
        url = (getattr(m, "url", None) or "").strip() if m is not None else ""
        if not url:
            url = WORKSHOP_URL_TEMPLATE.format(mod_id)
        QDesktopServices.openUrl(QUrl(url))

    def _open_content_folder(self, mod_id: int) -> None:
        """打开该 mod 的 content 文件夹（删之前看看里面实际有什么）。"""
        if self._game is None or not (self._game.download_dir or "").strip():
            QMessageBox.information(self, "打开 content 文件夹",
                                    "当前档案没有记录下载目录。")
            return
        target = Path(self._game.download_dir) / str(mod_id)
        if not target.is_dir():
            QMessageBox.warning(self, "打开 content 文件夹",
                                f"盘上找不到：\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _on_row_menu(self, table: QTableWidget, kind: str, pos) -> None:
        """右键菜单（备份页同款哲学：与行内控件同一组选项，右键只是
        另一个入口，不出现两套口径）。"""
        r = table.rowAt(pos.y())
        if r < 0:
            return
        id_col = {"missing": M_ID, "clean": C_ID, "orphan": O_ID}[kind]
        id_item = table.item(r, id_col)
        if id_item is None:
            return
        mid = int(id_item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        menu.setMinimumWidth(240)  # 中文菜单项宽度兜底（T19①）
        menu.setToolTipsVisible(True)  # QMenu 默认不显示悬浮说明，显式开
        act_open = QAction("打开工坊页面", menu)
        act_open.setToolTip("在浏览器打开该编号的创意工坊页面")
        act_open.triggered.connect(lambda: self._open_row_url(mid))
        menu.addAction(act_open)
        if kind in ("clean", "orphan"):
            act_dir = QAction("打开 content 文件夹", menu)
            act_dir.setToolTip("在资源管理器里看看这个目录里实际有什么，"
                               "再决定怎么处置")
            act_dir.triggered.connect(lambda: self._open_content_folder(mid))
            menu.addAction(act_dir)
        act_copy = QAction("复制编号", menu)
        act_copy.triggered.connect(
            lambda: QApplication.clipboard().setText(str(mid)))
        menu.addAction(act_copy)
        # 动作快捷设置：与行内下拉同一组选项（孤儿没有动作下拉，跳过）。
        # 闭包迟到绑定防呆：cc/ii 用关键字默认值当场定死
        combo = None
        if kind == "missing":
            combo = table.cellWidget(r, M_ACT)
        elif kind == "clean":
            combo = table.cellWidget(r, C_ACT)
        if combo is not None:
            menu.addSeparator()
            for i in range(combo.count()):
                text = ("✓ " if combo.currentIndex() == i else "") \
                       + combo.itemText(i)
                act = QAction(text, menu)
                act.triggered.connect(
                    lambda _=False, cc=combo, ii=i: cc.setCurrentIndex(ii))
                menu.addAction(act)
        menu.exec(table.viewport().mapToGlobal(pos))

    def _recommend(self, table: QTableWidget, act_col: int,
                   value: str) -> None:
        """全选推荐：只作用于当前显示（未被过滤隐藏）的行——与 mod 库页
        批量动作同一纪律：点按钮的人看得见要动的是什么。确认弹窗仍会
        全量列出将执行什么。"""
        for r in range(table.rowCount()):
            if table.isRowHidden(r):
                continue
            combo = table.cellWidget(r, act_col)
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _recommend_clean(self) -> None:
        """③ 全选推荐：按行状态给推荐动作。状态一律读 UserRole 里的
        英文原值——显示列是中文（status_zh），拿显示文本去查英文键
        的字典永远 miss（v2.33 修正）。"""
        t = self._tbl_clean
        for r in range(t.rowCount()):
            if t.isRowHidden(r):
                continue
            status = t.item(r, C_STATUS).data(Qt.ItemDataRole.UserRole)
            combo = t.cellWidget(r, C_ACT)
            want = {"downloaded": "wipe_soft", "deleted": "wipe",
                    "failed": "wipe"}.get(status)
            idx = combo.findData(want) if want else -1
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _reset_actions(self, table: QTableWidget, act_col: int) -> None:
        """全部重置：作用于所有行（含被过滤隐藏的行）——重置是保守方向，
        可以放宽；激进方向（推荐/执行）才限定在看得见的行。"""
        for r in range(table.rowCount()):
            table.cellWidget(r, act_col).setCurrentIndex(0)

    # ---------- 收集与执行 ----------
    def _collect_request(self) -> dmf.PlanRequest:
        """收集动作（备份页纪律移植：按行内 id 定位，不按行号）。隐藏行
        已显式选好的动作照常收集——执行前确认弹窗全量列出。"""
        req = dmf.PlanRequest()
        t = self._tbl_missing
        for r in range(t.rowCount()):
            act = t.cellWidget(r, M_ACT).currentData()
            if act:
                req.ledger_actions[int(t.item(r, M_ID).data(
                    Qt.ItemDataRole.UserRole))] = act
        t = self._tbl_clean
        for r in range(t.rowCount()):
            act = t.cellWidget(r, C_ACT).currentData()
            if act:
                req.clean_actions[int(t.item(r, C_ID).data(
                    Qt.ItemDataRole.UserRole))] = act
        req.purge_backup_files = self._chk_backup_files.isChecked()
        t = self._tbl_orphan
        for r in range(t.rowCount()):
            if t.cellWidget(r, O_CHK).isChecked():
                req.orphan_ids.append(int(t.item(r, O_ID).data(
                    Qt.ItemDataRole.UserRole)))
        return req

    def _on_execute(self) -> None:
        if self._report is None or self._exec_worker is not None:
            return
        req = self._collect_request()
        try:
            plan = dmf.build_plan(self._repo, self._report, req)
        except ValueError as exc:
            QMessageBox.warning(self, "没法执行", str(exc))
            return
        if not plan.actions:
            QMessageBox.information(
                self, "执行所选处置",
                "还没有选择任何处置动作——在各节的「动作」下拉里选（或右键"
                "行选动作），或用【全选推荐】。")
            return
        ret = QMessageBox.question(
            self, "确认执行（最后一道确认）",
            "\n".join(plan.notes)
            + "\n\n确认按上述执行？默认按钮是「否」，回车不会动手。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消：没有做任何改动")
            return
        self._bar.setRange(0, len(plan.actions))
        self._bar.setValue(0)
        self._bar.setVisible(True)
        self._btn_exec.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._set_colored(self._lbl_progress, "开始执行…", _C_INFO)
        self._exec_worker = _ExecuteWorker(self._repo, plan.actions)
        self._exec_worker.progress.connect(self._on_exec_progress)
        self._exec_worker.finished_run.connect(self._on_exec_done)
        self._exec_worker.crashed.connect(self._on_crashed)
        self._exec_worker.start()

    def _on_stop(self) -> None:
        if self._exec_worker is not None:
            self._exec_worker.request_stop()
            self._btn_stop.setEnabled(False)
            self._set_colored(self._lbl_progress, "将在当前条完成后停止…",
                              _C_WARN)

    def _on_exec_progress(self, i: int, n: int, text: str) -> None:
        self._bar.setValue(i)
        self._set_colored(self._lbl_progress, f"（{i}/{n}）{text}", _C_INFO)

    def _on_exec_done(self, payload: dict) -> None:
        self._exec_worker = None
        self._bar.setVisible(False)
        self._btn_exec.setEnabled(True)
        self._btn_stop.setEnabled(False)
        results = payload["results"]
        stopped = payload["stopped"]
        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        tail = "（已按要求停止，剩余动作未执行）" if stopped else ""
        summary = (f"执行结束：完成 {len(ok)} 条，失败 {len(bad)} 条{tail}"
                   + ("\n" + "\n".join(f"· {r.kind} {r.mod_id}：{r.detail}"
                                       for r in bad) if bad else ""))
        self._set_colored(self._lbl_progress, summary,
                          _C_OK if (not bad and not stopped) else _C_WARN)
        if stopped:
            self._log.warn("处置已停止：停在条与条之间，完成的条目已生效")
        for r in ok:
            self._log.ok(f"清理·{r.kind} mod {r.mod_id}：{r.detail}")
        for r in bad:
            self._log.error(f"清理·{r.kind} mod {r.mod_id} 失败：{r.detail}")
        # 处置落账 → mod 库页强制重读（软删除/清账结果不用用户自己点刷新）
        self.ledger_changed.emit()
        # 处置完自动重新盘点：残留与新生孤儿立刻显现，不用自己想起来
        self._start_scan()

    def _on_crashed(self, msg: str) -> None:
        self._exec_worker = None
        self._scan_worker = None
        self._bar.setVisible(False)
        self._btn_exec.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_scan.setEnabled(True)
        self._set_colored(self._lbl_scan, "", _C_MUTED)
        self._log.error(f"清理页发生预期外错误：{msg}")
        QMessageBox.critical(self, "清理与删除", f"发生预期外错误：{msg}")

    # ---------- 小件 ----------
    @staticmethod
    def _set_colored(lbl: QLabel, text: str, color: str) -> None:
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {color};")

    def _steamcmd_root(self):
        return steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))

    def _set_results_visible(self, visible: bool) -> None:
        """盘点结果三张卡的显隐（盘点前/切换档案时全藏，只留盘点卡）。"""
        for box in (self._box_missing, self._box_clean, self._box_orphan):
            box.setVisible(visible)
