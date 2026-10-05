"""入账中心（待确认区 + 登记区）
"""
"""
gui/accountCenterPage.py · 判决制的记账台。

本轮两区半：待确认区 + 最近判决史 + 登记区；待认领区随只读盘点
轮回补（盘点引擎的唯一产出就是认领候选，引擎与消费方同轮）。

三块内容的分工：
- 待确认区：判决日志的待确认队列（终端判决等背书）。勾选默认全
  不勾——确认 = 背书，背书须显式（刻意不做"默认全选"开关）。
  【确认入账】走 repo.confirm_items 唯一正门，与批次收尾的确认
  清单弹窗同源同门（那边的弹窗 = 本区的临时形态，【稍后处理】
  关掉的条目一直在这里）。
- 最近判决史：list_game_verdicts 只读时间线（成功/超时/失败全记，
  含已确认行）。已确认行右键「撤销确认」= 唯一回滚，走
  repo.revoke_confirmation：本地版本清空、下载状态不变，下轮
  检测自然重报；判决史保留备查。
- 登记区：网址/编号/命令粘贴 → urlParser 解析 → registerFlow
  五桶预览（页内表——粘贴常超 10 条需反复对照，不弹窗）→ 批查
  补元数据（匿名接口 100/批，netGate 入口）→ tracked 入账。
  登记只产意图（盘上还没有的东西）；入账（事实）在确认门，
  认领（盘面事实）在待认领区——三种来路不混。

线程纪律：批查在 QThread（联网慢）；登记落库毫秒级走主线程。
worker 零控件零 Qt 对象，结果走信号；shutdown 等 1.5 秒，等不到
park 保活（与批次控制器同口径）。

档案隔离：两区按当前档案过滤；顶部轻提示"其他档案还有 N 条待
确认"。set_game 广播重载两区；登记区预览属档案上下文，切档案
即清。刷新时机 = set_game + showEvent（每次进页自动重载，
不依赖导航钩子）。
"""
from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices


from core import constants, netGate
from core import registerFlow as rf
from core.formatters import fmt_size, relative_time
from core.steamApiClient import SteamApiClient, SteamApiError
from core.urlParser import WORKSHOP_URL_TEMPLATE, parse_lines
from types import SimpleNamespace

from core import inventoryFlow
from gui.modFolderOpener import open_mod_folder
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from gui.collapsibleSection import CollapsibleSection  # 固定高度+可折叠分区（单源共享件）
from gui.theme import font_px  # 字号单源（D25）

_C_MUTED = "#8a8a8f"   # 弱化：已在册 / 未验证 / 已确认（与确认清单同口径）
_C_WARN = "#c8a03c"    # 黄：黑名单警示
_C_BAD = "#c75450"     # 红：无法识别 / 错档拦截


class _RegisterQueryWorker(QThread):
    """登记批查：给待登记编号补元数据（标题/作者/大小/远端版本）。
    result：{mod_id: (time_updated, title, file_size, creator)}；
    查不到/失效条目不进字典（= 元数据留 NULL）；整批网络失败 →
    空字典 + error。netGate 互斥：拿不到名额 → 全部按查询失败处理
    （登记照常可进行，元数据留空等检测兜底）。creator 字段名未在
    客户端实文核过，防御取（取不到 = None，检测补全）。"""

    done = Signal(dict, str)

    def __init__(self, mod_ids: list[int], parent=None) -> None:
        super().__init__(parent)
        self._ids = list(dict.fromkeys(int(i) for i in mod_ids))


    def run(self) -> None:
        result: dict[int, tuple] = {}
        error = ""
        owner = netGate.try_acquire(constants.NET_GATE_REGISTER)
        if owner is not None:
            self.done.emit({}, f"联网查询名额被「{owner}」占用")
            return
        try:
            client = SteamApiClient()
            for it in client.query_details(self._ids):
                if it.mod_id is None or it.result != 1:
                    continue          # 查无此条/失效：元数据留空
                creator = getattr(it, "creator", None)
                result[it.mod_id] = (it.time_updated, it.title,
                                     it.file_size, creator)
        except SteamApiError as exc:
            error = str(exc)
        finally:
            netGate.release(constants.NET_GATE_REGISTER)
        self.done.emit(result, error)

class _ScanWorker(QThread):
    """盘点线程：目录遍历 + 大小实测可能很慢（机械盘几 GB 的 mod
    常见），放线程防界面卡死。repo 跨线程沿 BackupManager 先例
    （连接允许跨线程、即时提交）。零控件零 Qt 对象。"""
    done = Signal(object, str)        # (ScanReport | None, 错误说明)

    def __init__(self, repo, game, steamcmd_path: str, parent=None) -> None:
        super().__init__(parent)
        self._repo, self._game, self._cmd = repo, game, steamcmd_path

    def run(self) -> None:
        try:
            report = inventoryFlow.scan_game(
                self._repo, self._game, steamcmd_path=self._cmd)
        except (OSError, ValueError) as exc:
            self.done.emit(None, str(exc))
            return
        self.done.emit(report, "")

class AccountCenterPage(QWidget):
    """入账中心：repo/settings/log 注入，set_game 纳入主窗口广播
    循环（hasattr 自发现，零额外接线）；closeEvent 循环自动发现
    shutdown()。"""
    _SES_FOLD = "session/acct_fold_v1"  # 五个分区的折叠记忆（v1=大改版：固定高度+折叠，分裂器退役）

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings       # 留着：待认领区的认领默认勾选等键
        self._log = log
        self._game = None
        self._plan: rf.RegisterPlan | None = None
        self._pending_rows: list = []
        self._history_rows: list = []
        self._query_worker: _RegisterQueryWorker | None = None
        self._scan_worker: _ScanWorker | None = None
        self._claims_rows: list = []
        self._history_view: list = []   # 过滤后的显示行（右键菜单按它取数）

        # ---------- 页面骨架：标题 + 导读 + 先读我 + 查找 + 四个分区 ----------
        # （旧版 = 一段常驻大提示 + QSplitter 四分格：没有大标题、提示
        #   占半屏、四区高度互相拉扯、没有页内查找——本版全部重排）
        body = QWidget(self)
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 12, 16, 8)

        # 页面大标题：进页第一眼先知道这是哪一页
        title = QLabel("入账中心", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 一句话导读：只留最要紧的；详细说明全部搬进下面「先读我」
        tip = QLabel(
            "三条来路，一个门出：批次收尾的判决、盘点扫出的候选，都在"
            "这里核对后确认/认领入账；登记区收「想下载什么」。"
            "详细说明点开下方「先读我」。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        self._others = QLabel("", self)
        self._others.setWordWrap(True)
        root.addWidget(self._others)

        # 「先读我」：功能介绍 · 名词解释 · 各区步骤（可折叠，交互
        # 对齐异常处置页的同名卡）
        self._sec_guide = CollapsibleSection(
            "先读我：本页怎么用 · 名词解释 · 各区步骤")
        self._sec_guide.set_content(self._build_guide_body(), 300)
        root.addWidget(self._sec_guide)

        # 页面级查找：对①②③三张表做"只隐藏不清数据"的行过滤（清空
        # 即全部恢复）。原来只有判决史有搜索，待确认/待认领没有查找
        # 入口——242 条候选里找一行只能肉眼扫
        srow = QWidget(self)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel("查找：", srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            "按标题 / 编号过滤①②③三张表（即时生效，只隐藏；清空恢复）…")
        self._filter_edit.textChanged.connect(self._apply_item_filter)
        sh.addWidget(self._filter_edit, 1)
        root.addWidget(srow)

        # 四个分区：固定高度 + 点标题折叠。固定高度的用意：每区高度
        # 恒定、互不拉扯；收起只藏内容、标题行常在；内容多时表格内部
        # 出滚动条，分区高度不随内容涨（待认领 242 条也只占自己一格）
        self._sec_pending = CollapsibleSection("① 待确认——终端判决等背书")
        self._sec_pending.set_content(self._build_pending_zone(), 600)
        root.addWidget(self._sec_pending)

        self._sec_claims = CollapsibleSection("② 待认领——盘上有文件、账上没版本")
        self._sec_claims.set_content(self._build_claims_zone(), 600)
        root.addWidget(self._sec_claims)

        self._sec_history = CollapsibleSection("③ 最近判决（只读）")
        self._sec_history.set_content(self._build_history_zone(), 600)
        root.addWidget(self._sec_history)

        self._sec_register = CollapsibleSection("④ 登记（想下载什么）")
        self._sec_register.set_content(self._build_register_zone(), 600)
        root.addWidget(self._sec_register)

        # 收尾弹簧：内容不足一屏时多余高度全归它，上面内容贴顶紧凑
        # ——不给的话多余高度摊给各标签，页面重新出现大片空档
        # （与联接检测页同款问题、同款解法）
        root.addStretch(1)

        # 折叠记忆：先恢复、后接线（set_expanded 会发信号，顺序反了
        # 会把刚恢复的状态又写一遍）。首次使用没有记忆时默认：先读我
        # 和①②④展开，③最近判决收起（只读备查，少占屏）
        self._fold_state = [True, True, True, False, True]
        fold = QSettings().value(self._SES_FOLD)
        if fold is not None:
            flags = ["1" == f for f in str(fold).split(",")]
            self._fold_state = (flags + [True, True, True, False, True])[:5]
        sections = (self._sec_guide, self._sec_pending, self._sec_claims,
                    self._sec_history, self._sec_register)
        for sec, on in zip(sections, self._fold_state):
            sec.set_expanded(on)
        for i, sec in enumerate(sections):
            sec.expand_changed.connect(
                lambda on, i=i: self._on_fold_changed(i, on))

        # 全页可滚动（V1 站规）：所有分区装进滚动区，窗口不够高就出
        # 纵向滚动条，内容永不被裁
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    # ---------------- 三区构建 ----------------
    @staticmethod
    def _style_table(t: QTableWidget) -> None:
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)



    def _build_pending_zone(self) -> QWidget:
        zone = QWidget(self)
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(0, 0, 0, 0)

        self._pending_hint = QLabel("", self)
        self._pending_hint.setWordWrap(True)
        lay.addWidget(self._pending_hint)

        self._pending_table = QTableWidget(0, 8, zone)
        self._pending_table.setHorizontalHeaderLabels(
            ["勾选", "编号", "标题", "触发值", "查询值", "将写入", "大小", "来源"])
        self._style_table(self._pending_table)
        self._pending_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self._pending_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._pending_table.customContextMenuRequested.connect(
            self._on_pending_menu)
        lay.addWidget(self._pending_table, 1)

        btns = QHBoxLayout()
        self._btn_all = QPushButton("全选 / 全不选", zone)
        self._btn_all.setToolTip("这批全成了才用——勾选就是背书")
        self._btn_all.clicked.connect(self._toggle_all_pending)
        btns.addWidget(self._btn_all)
        btns.addStretch(1)
        self._btn_confirm = QPushButton("确认入账", zone)
        self._btn_confirm.setToolTip(
            "把勾选条目「将写入」的版本经确认门登记为本地版本"
            "（唯一写入点，可撤销）；黑名单条目会被拦下")
        self._btn_confirm.clicked.connect(self._on_confirm_checked)
        btns.addWidget(self._btn_confirm)
        lay.addLayout(btns)
        return zone
    def _build_claims_zone(self) -> QWidget:
        zone = QWidget(self)
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(0, 0, 0, 0)

        self._claims_hint = QLabel("", self)
        self._claims_hint.setWordWrap(True)
        lay.addWidget(self._claims_hint)

        self._claims_table = QTableWidget(0, 5, zone)
        self._claims_table.setHorizontalHeaderLabels(
            ["勾选", "编号", "盘面版本", "大小", "说明"])
        self._style_table(self._claims_table)
        self._claims_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch)
        self._claims_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._claims_table.customContextMenuRequested.connect(
            self._on_claims_menu)
        lay.addWidget(self._claims_table, 1)

        btns = QHBoxLayout()
        self._btn_scan = QPushButton("扫描游戏目录", zone)
        self._btn_scan.setToolTip(
            "盘点当前档案的 mod 目录：盘上有文件而账上没版本的编号落成"
            "候选（重复扫描不会重复落）；文件夹已消失的旧候选自动清除；"
            "顺手回填各 mod 的磁盘占用。\n纯本地磁盘操作，不联网")
        self._btn_scan.clicked.connect(self._on_scan)
        btns.addWidget(self._btn_scan)
        self._btn_claims_all = QPushButton("全选 / 全不选", zone)
        self._btn_claims_all.setToolTip("这批都要认才用——勾选就是入账")
        self._btn_claims_all.clicked.connect(self._toggle_all_claims)
        btns.addWidget(self._btn_claims_all)

        btns.addStretch(1)
        self._btn_claim = QPushButton("认领入账", zone)
        self._btn_claim.setToolTip(
            "勾选候选 → 按盘面版本认领入账（与批次确认走同一个门）；"
            "盘面版本读不出来的认成「版本未知」")
        self._btn_claim.clicked.connect(self._on_claim_checked)
        btns.addWidget(self._btn_claim)
        self._btn_ignore = QPushButton("忽略选中…", zone)
        self._btn_ignore.setToolTip(
            "把勾选候选记入「不收录」名单：今后扫描不再为它们落候选，"
            "登记与确认/认领也会被拦（与已清账黑名单同一闸）。\n"
            "反悔：到【已清账管理】页对相应编号【允许录入】")
        self._btn_ignore.clicked.connect(self._on_ignore_checked)
        btns.addWidget(self._btn_ignore)

        lay.addLayout(btns)
        return zone
    def _build_history_zone(self) -> QWidget:
        zone = QWidget(self)
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "含已确认行；已确认行右键可撤销确认。"
            "页面上方的【查找】框对本表同样生效", self)
        lay.addWidget(hint)
        self._history_table = QTableWidget(0, 8, zone)
        self._history_table.setHorizontalHeaderLabels(
            ["时间", "编号", "标题", "种类", "触发值", "写入值", "来源", "状态"])
        self._style_table(self._history_table)
        self._history_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        self._history_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_table.customContextMenuRequested.connect(
            self._on_history_menu)
        lay.addWidget(self._history_table, 1)
        return zone


    def _build_register_zone(self) -> QWidget:
        zone = QWidget(self)
        lay = QVBoxLayout(zone)
        lay.setContentsMargins(0, 0, 0, 0)

        self._paste_box = QTextEdit(zone)
        self._paste_box.setPlaceholderText(
            "粘贴工坊网址、纯数字编号或 steamcmd 下载命令"
            "（每行一条或空格分隔均可）……\n"
            "例：https://steamcommunity.com/sharedfiles/filedetails/"
            "?id=3403925213\n"
            "例：workshop_download_item 1158310 3403925213")
        lay.addWidget(self._paste_box, 1)

        btns = QHBoxLayout()
        self._btn_parse = QPushButton("解析预览", zone)
        self._btn_parse.setToolTip(
            "解析粘贴内容并与账本对表：新登记 / 已在册 / 黑名单警示 /"
            " 错档 / 无法识别，五类分色预览，不写库")
        self._btn_parse.clicked.connect(self._on_parse)
        btns.addWidget(self._btn_parse)
        self._btn_register = QPushButton("登记新编号", zone)
        self._btn_register.setEnabled(False)
        self._btn_register.setToolTip(
            "先联网核对新编号的标题/大小，然后登记为「待下载」；"
            "查不到的条目照常登记，元数据等更新检测补全")
        self._btn_register.clicked.connect(self._on_register)
        btns.addWidget(self._btn_register)
        btns.addStretch(1)
        lay.addLayout(btns)

        self._mismatch_label = QLabel("", zone)
        self._mismatch_label.setWordWrap(True)
        self._mismatch_label.setStyleSheet(f"color: {_C_BAD};")
        self._mismatch_label.hide()
        lay.addWidget(self._mismatch_label)

        self._preview_table = QTableWidget(0, 3, zone)
        self._preview_table.setHorizontalHeaderLabels(["类别", "编号", "说明"])
        self._style_table(self._preview_table)
        self._preview_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self._preview_table, 1)
        return zone
    # ---------------- 先读我 / 页面级查找 / 折叠记忆 ----------------

    def _build_guide_body(self) -> QWidget:
        """「先读我」的内容：本页管什么、名词解释、各区步骤、路标。
        外层套内部滚动区——说明较长且随窗口宽度换行，固定高度下
        万一放不下，内部自己出滚动条，绝不裁字。"""
        inner = QWidget(self)
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(8)
        paragraphs = (
            "【这页管什么】下载完成的 mod 不会自动算数——要在这里核对、"
            "确认之后才进账本（账本 = 软件记住每个 mod 状态的地方）。"
            "三条来路一个门：①批次收尾的终端判决、②盘点扫出的盘上候选、"
            "③自己登记的编号，都从「确认」这道门进账本；确认后仍可右键"
            "撤销。",

            "【名词解释】判决：软件对一次下载/检测的判定记录（成功、失败、"
            "超时都记）。待确认：判决说下载成功了，等你核对后背书才正式"
            "记账。待认领：盘上有以编号命名的文件夹、账本里却没有这个"
            "编号（手动拷进去的、别的工具下的）——盘点扫出来等你认。"
            "登记：把「我想下载这个编号」记进账本，状态「待下载」。"
            "版本未知：入账时没读到版本号（比如文件夹是空的）——重新下载"
            "并在收尾清单确认后补上。黑名单：彻底清账/忽略过的编号，登记"
            "与认领都会被拦；反悔去【已清账管理】允许录入。",

            "【① 待确认 怎么用】下载批次收尾后，成功条目在这里排队 →"
            " 逐条核对标题/大小/将写入的版本 → 勾选 →【确认入账】。"
            "默认全不勾是刻意的：确认 = 背书 = 写账本，背书必须逐条显式。"
            "标题/大小暂缺的条目照常入账，更新检测页会补全。",

            "【② 待认领 怎么用】点【扫描游戏目录】盘点一次 → 勾选要收编"
            "的 →【认领入账】（按盘面上读到的版本入账）；不想收的"
            "【忽略选中…】，今后扫描不再为它们落候选（反悔去已清账管理）。"
            "重复扫描不会重复落候选；文件夹删掉后下轮扫描自动清。",

            "【④ 登记 怎么用】粘贴工坊网址 / 纯数字编号 / steamcmd 下载"
            "命令（每行一条或空格分隔均可）→【解析预览】先对表（新登记 /"
            " 已在册 / 黑名单警示 / 错档拦截 / 无法识别，五类分色，不写库）"
            "→【登记新编号】（自动联网补标题/大小）→ 登记成「待下载」→"
            " 到【下载命令生成】页勾选下载。",

            "【路标】想看/管每个 mod 的状态 →【mod 库】页；批量下载 →"
            "【下载命令生成】页；删文件 →【清理与删除】页；账与盘逐条"
            "对账 →【账实核验】页；判决史只读备查，右键已确认行可撤销"
            "确认（唯一的后悔药：本地版本清空，下次更新检测重新报告）。")
        for text in paragraphs:
            lbl = QLabel(text, inner)
            lbl.setWordWrap(True)
            iv.addWidget(lbl)
        iv.addStretch(1)
        wrap = QScrollArea(self)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.Shape.NoFrame)
        wrap.setWidget(inner)
        return wrap

    def _apply_item_filter(self, *_a) -> None:
        """页面级查找：对①待确认、②待认领、③判决史三张表做行隐藏。
        只隐藏不清数据——清空关键词全部恢复；隐藏不影响已勾选的行
        （查找是视图帮手，不是数据闸门），不碰账本。"""
        kw = self._filter_edit.text().strip().casefold()

        def hide_table(t: QTableWidget, text_cols: list[int]) -> None:
            for r in range(t.rowCount()):
                if not kw:
                    t.setRowHidden(r, False)
                    continue
                hit = False
                for c in text_cols:
                    it = t.item(r, c)
                    if it is not None and kw in it.text().casefold():
                        hit = True
                        break
                t.setRowHidden(r, not hit)

        # 各表参与匹配的列：编号/标题优先，顺带说明类文字
        hide_table(self._pending_table, [1, 2, 5, 7])   # 编号/标题/将写入/来源
        hide_table(self._claims_table, [1, 2, 4])       # 编号/盘面版本/说明
        hide_table(self._history_table, [1, 2, 3, 5])   # 编号/标题/种类/写入值

    def _on_fold_changed(self, idx: int, on: bool) -> None:
        """任一分区收展一变：更新影子账并整体落盘——下次打开软件还是
        这个收展样子（哪个区常开、哪个区常收是自己的工作台习惯，
        值得记住；与账实核验页的折叠记忆同一套 session 口径）。"""
        self._fold_state[idx] = on
        QSettings().setValue(self._SES_FOLD, ",".join(
            "1" if on else "0" for on in self._fold_state))

    # ---------------- 刷新 ----------------
    def set_game(self, game) -> None:
        self._game = game
        self._plan = None
        self._paste_box.clear()
        self._preview_table.setRowCount(0)
        self._btn_register.setEnabled(False)
        self._btn_register.setText("登记新编号")
        self._mismatch_label.hide()
        self._reload_all()

    def showEvent(self, event) -> None:   # 进页自动刷新（导航钩子零接线）
        super().showEvent(event)
        self._reload_all()
    def start_inventory_scan(self) -> None:
        """跨页入口（换机迁移⑦、恢复旧版本③等模块页的「盘点确认」
        落点）：与页内【扫描游戏目录】完全同一个动作——无档案时
        _on_scan 自己会弹提示，扫描进行中也有互斥保护，不用另写。"""
        self._on_scan()

    def _reload_all(self) -> None:
        self._reload_pending()
        self._reload_claims()
        self._reload_history()
        self._update_others_notice()

    def _reload_pending(self) -> None:
        gid = self._game.app_id if self._game else None
        # claim 行归待认领区（本区只管"终端判决等背书"），这里滤掉
        rows = [v for v in (self._repo.pending_confirmations(gid)
                            if gid else []) if v.kind != "claim"]

        self._pending_rows = rows
        t = self._pending_table
        t.setRowCount(len(rows))
        for i, v in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)   # 默认全不勾
            chk.setData(Qt.ItemDataRole.UserRole, v.mod_id)
            t.setItem(i, 0, chk)
            t.setItem(i, 1, QTableWidgetItem(str(v.mod_id)))
            t.setItem(i, 2, QTableWidgetItem(v.title or ""))
            t.setItem(i, 3, QTableWidgetItem(
                str(v.version_trigger) if v.version_trigger is not None else "—"))
            t.setItem(i, 4, QTableWidgetItem(
                str(v.version_query) if v.version_query is not None else "—"))
            if v.version_written is None:
                item = QTableWidgetItem("版本未知")
                item.setForeground(QColor(_C_MUTED))
            else:
                word = (constants.CONFIRMED_SOURCE_ZH.get(v.source)
                        if v.source else None)
                item = QTableWidgetItem(str(v.version_written)
                                        + (f"（{word}）" if word else ""))
                if v.source != "verified":
                    item.setForeground(QColor(_C_MUTED))
            t.setItem(i, 5, item)
            t.setItem(i, 6, QTableWidgetItem(
                fmt_size(v.file_size) if v.file_size else "—"))
            src = (constants.CONFIRMED_SOURCE_ZH.get(v.source)
                   if v.source else None)
            t.setItem(i, 7, QTableWidgetItem(src or "—"))
        if rows:
            self._pending_hint.setText(
                f"{len(rows)} 条待确认。核对后勾选并【确认入账】；"
                "「版本未知」的条目确认后仍是未知态（重新下载并在收尾"
                "清单确认才是正路）。先不确认也可以，这里一直留着。")
        else:
            self._pending_hint.setText(
                "没有待确认的条目——下载批次收尾后，成功条目会在这里"
                "排队等你确认入账。")
        self._btn_confirm.setText(f"确认入账（{len(rows)} 条待处理）")
        # 分区标题带实时计数（收起也能看到有多少条在等）
        self._sec_pending.set_title(
            f"① 待确认——终端判决等背书（{len(rows)} 条）")

    def _reload_claims(self) -> None:
        gid = self._game.app_id if self._game else None
        rows = [v for v in (self._repo.pending_confirmations(gid)
                            if gid else []) if v.kind == "claim"]
        self._claims_rows = rows
        t = self._claims_table
        t.setRowCount(len(rows))
        for i, v in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, v.mod_id)
            t.setItem(i, 0, chk)
            t.setItem(i, 1, QTableWidgetItem(str(v.mod_id)))
            t.setItem(i, 2, QTableWidgetItem(
                str(v.version_written) if v.version_written is not None
                else "读不出（认成版本未知）"))
            t.setItem(i, 3, QTableWidgetItem(
                fmt_size(v.file_size) if v.file_size else "—"))
            t.setItem(i, 4, QTableWidgetItem(
                f"来自盘点， {relative_time(v.occurred_at)}"
                if v.occurred_at else "来自盘点"))
        if rows:
            self._claims_hint.setText(
                f"{len(rows)} 条候选。核对后勾选【认领入账】；不想认的先"
                "留着（不影响任何功能），文件夹删掉后下轮扫描自动清。")
        else:
            self._claims_hint.setText(
                "没有待认领的候选——点【扫描游戏目录】盘点一次；盘上有"
                "文件、账上没版本的 mod 会出现在这里。")
        # 分区标题带实时计数
        self._sec_claims.set_title(
            f"② 待认领——盘上有文件、账上没版本（{len(rows)} 条）")

    def _reload_history(self) -> None:
        """重灌判决史表格：全部行一次进表。旧版页内自带的小搜索框和
        "按关键字重建表格"已退役——查找统一交给页面级【查找】框
        （只隐藏不清行，见 _apply_item_filter）；本方法只管灌数据。"""
        gid = self._game.app_id if self._game else None
        self._history_rows = (list(self._repo.list_game_verdicts(gid))
                              if gid else [])
        self._history_view = self._history_rows
        t = self._history_table
        t.setRowCount(len(self._history_rows))
        for i, v in enumerate(self._history_rows):
            t.setItem(i, 0, QTableWidgetItem(
                relative_time(v.occurred_at) if v.occurred_at else "—"))
            t.setItem(i, 1, QTableWidgetItem(str(v.mod_id)))
            t.setItem(i, 2, QTableWidgetItem(v.title or ""))
            kind = constants.VERDICT_KIND_ZH.get(v.kind, v.kind)
            t.setItem(i, 3, QTableWidgetItem(kind))
            t.setItem(i, 4, QTableWidgetItem(
                str(v.version_trigger)
                if v.version_trigger is not None else "—"))
            t.setItem(i, 5, QTableWidgetItem(
                str(v.version_written)
                if v.version_written is not None else "—"))
            src = (constants.CONFIRMED_SOURCE_ZH.get(v.source)
                   if v.source else None)
            t.setItem(i, 6, QTableWidgetItem(src or "—"))
            t.setItem(i, 7, QTableWidgetItem(
                "待确认" if v.confirmed_at is None else "已确认"))
        # 分区标题带实时计数
        self._sec_history.set_title(
            f"③ 最近判决（只读 · {len(self._history_rows)} 条）")

    def _update_others_notice(self) -> None:
        if self._game is None:
            self._others.setText(
                "当前没有档案：先在左上角添加或选择游戏档案。")
            return
        non_claim = [v for v in self._repo.pending_confirmations(None)
                     if v.kind != "claim"]
        mine = sum(1 for v in non_claim
                   if v.game_id == self._game.app_id)
        total = len(non_claim)

        others = total - mine
        self._others.setText(
            f"当前档案：{self._game.name}（{self._game.app_id}）。"
            + (f"其他档案还有 {others} 条待确认——切到对应档案即可处理。"
               if others > 0 else ""))

    # ---------------- 待确认区动作 ----------------
    def _checked_pending_ids(self) -> list[int]:
        t = self._pending_table
        ids = []
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        return ids

    def _toggle_all_pending(self) -> None:
        t = self._pending_table
        if not t.rowCount():
            return
        any_off = any(t.item(r, 0).checkState() != Qt.CheckState.Checked
                      for r in range(t.rowCount()))
        state = (Qt.CheckState.Checked if any_off
                 else Qt.CheckState.Unchecked)
        for r in range(t.rowCount()):
            t.item(r, 0).setCheckState(state)

    def _on_confirm_checked(self) -> None:
        ids = self._checked_pending_ids()
        if not ids:
            QMessageBox.information(self, "确认入账", "先勾选要确认的条目。")
            return
        try:
            n = self._repo.confirm_items(ids)   # 确认门正门（唯一写入点）
        except ValueError as exc:
            QMessageBox.warning(self, "确认入账失败", str(exc))
            return
        if self._log is not None:
            self._log.ok(f"已确认入账 {n} 条")
        self._reload_all()

    def _on_pending_menu(self, pos) -> None:
        row = self._pending_table.rowAt(pos.y())
        if row < 0 or row >= len(self._pending_rows):
            return
        v = self._pending_rows[row]
        menu = QMenu(self)
        act_open = menu.addAction("↗ 打开工坊页面")
        act_copy = menu.addAction("复制编号")
        chosen = menu.exec(self._pending_table.viewport().mapToGlobal(pos))
        if chosen == act_copy:
            QApplication.clipboard().setText(str(v.mod_id))
            if self._log is not None:
                self._log.info(f"已复制编号 {v.mod_id}")
        elif chosen == act_open:
            QDesktopServices.openUrl(
                QUrl(WORKSHOP_URL_TEMPLATE.format(v.mod_id)))

    # ---------------- 判决史动作 ----------------
    def _on_history_menu(self, pos) -> None:
        row = self._history_table.rowAt(pos.y())
        if row < 0 or row >= len(self._history_view):
            return
        v = self._history_view[row]

        menu = QMenu(self)
        act_open = menu.addAction("↗ 打开工坊页面")
        act_copy = menu.addAction("复制编号")
        act_revoke = None
        if v.confirmed_at is not None:
            menu.addSeparator()
            act_revoke = menu.addAction("撤销确认（该 mod 的本地版本）")
        chosen = menu.exec(self._history_table.viewport().mapToGlobal(pos))
        if chosen == act_copy:
            QApplication.clipboard().setText(str(v.mod_id))
            if self._log is not None:
                self._log.info(f"已复制编号 {v.mod_id}")
        elif chosen == act_open:
            QDesktopServices.openUrl(
                QUrl(WORKSHOP_URL_TEMPLATE.format(v.mod_id)))
        elif act_revoke is not None and chosen == act_revoke:
            self._revoke(v.mod_id)

    def _revoke(self, mod_id: int) -> None:
        ret = QMessageBox.question(
            self, "撤销确认",
            f"撤销 mod {mod_id} 的本地版本确认？\n\n"
            "本地版本与确认来源将被清空（下载状态不变），下次更新"
            "检测会重新报告该 mod；判决史保留备查。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.revoke_confirmation(mod_id)
        except ValueError as exc:
            QMessageBox.warning(self, "撤销确认失败", str(exc))
            return
        if self._log is not None:
            self._log.info(f"mod {mod_id} 已标记为未确认——下次更新检测"
                           "会重新报告")
        self._reload_all()

    # ---------------- 登记区动作 ----------------
    # ---------------- 待认领区动作 ----------------
    def _checked_claim_ids(self) -> list[int]:
        t = self._claims_table
        return [t.item(r, 0).data(Qt.ItemDataRole.UserRole)
                for r in range(t.rowCount())
                if t.item(r, 0).checkState() == Qt.CheckState.Checked]
    def _toggle_all_claims(self) -> None:
        t = self._claims_table
        if not t.rowCount():
            return
        any_off = any(t.item(r, 0).checkState() != Qt.CheckState.Checked
                      for r in range(t.rowCount()))
        state = (Qt.CheckState.Checked if any_off
                 else Qt.CheckState.Unchecked)
        for r in range(t.rowCount()):
            t.item(r, 0).setCheckState(state)

    def _on_claim_checked(self) -> None:
        ids = self._checked_claim_ids()
        if not ids:
            QMessageBox.information(self, "认领入账", "先勾选要认领的候选。")
            return
        try:
            n = self._repo.confirm_items(ids)   # 与批次确认同一扇门
        except ValueError as exc:
            QMessageBox.warning(self, "认领入账失败", str(exc))
            return
        if self._log is not None:
            self._log.ok(f"已认领入账 {n} 条")
        self._reload_all()
    def _on_ignore_checked(self) -> None:
        """⑭ 忽略候选：记录进「已清账/不收录」侧表（与黑名单同表
        同权——拦扫描候选、拦登记、拦确认/认领，D11 全套现成闸零
        新增），同时清掉未确认的 claim 提案（提案随忽略作废）。"""
        ids = self._checked_claim_ids()
        if not ids:
            QMessageBox.information(self, "忽略候选", "先勾选要忽略的候选。")
            return
        ret = QMessageBox.question(
            self, "忽略候选",
            f"把勾选的 {len(ids)} 个候选记入「不收录」名单？\n\n"
            "· 今后扫描不再为它们落候选（文件夹还在也不报）；\n"
            "· 登记与确认/认领也会被拦（与已清账黑名单同一闸）；\n"
            "· 账本与磁盘都不动——只是不再打扰；\n"
            "· 反悔：到【已清账管理】页对相应编号【允许录入】。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        gid = self._game.app_id if self._game is not None else None
        if gid is None:
            return
        for mid in ids:  # 逐条独立（repo 单语句方法自带原子性）
            self._repo.add_purged(mid, gid, note="盘点忽略")
        self._repo.drop_stale_claims(gid, ids)  # 候选提案随忽略作废

        if self._log is not None:
            self._log.ok(
                f"已忽略 {len(ids)} 条候选（今后不再落候选、登记入账"
                "被拦；到已清账管理页可恢复收录）")
        self._reload_all()

    def _on_scan(self) -> None:
        if self._game is None:
            QMessageBox.information(self, "盘点", "先在左上角选择游戏档案。")
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            QMessageBox.information(self, "盘点", "上一轮扫描还在跑，等它结束。")
            return
        self._btn_scan.setEnabled(False)
        self._btn_scan.setText("正在盘点…")
        cmd = (str(self._settings.get("steamcmd_path") or "")
               if self._settings is not None else "")
        self._scan_worker = _ScanWorker(self._repo, self._game, cmd)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, report, error: str) -> None:
        self._scan_worker = None
        self._btn_scan.setEnabled(True)
        self._btn_scan.setText("扫描游戏目录")
        if self._log is not None:
            if error:
                self._log.error(f"盘点失败：{error}")
            elif report is not None:
                self._log.info(
                    f"盘点完成（{report.mod_dir}）：盘上 {report.folders_found}"
                    f" 个 mod 文件夹，新候选 {len(report.candidates_new)} 条，"
                    f"清失效 {len(report.stale_removed)} 条，大小回填 "
                    f"{report.sizes_backfilled} 条；{report.acf_note}")
        self._reload_all()

    def _on_claims_menu(self, pos) -> None:
        row = self._claims_table.rowAt(pos.y())
        if row < 0 or row >= len(self._claims_rows):
            return
        v = self._claims_rows[row]
        menu = QMenu(self)
        act_folder = (menu.addAction("↗ 打开所在文件夹")
                      if self._game is not None else None)
        act_open = menu.addAction("↗ 打开工坊页面")
        act_copy = menu.addAction("复制编号")
        chosen = menu.exec(self._claims_table.viewport().mapToGlobal(pos))
        if chosen == act_copy:
            QApplication.clipboard().setText(str(v.mod_id))
            if self._log is not None:
                self._log.info(f"已复制编号 {v.mod_id}")
        elif chosen == act_open:
            QDesktopServices.openUrl(
                QUrl(WORKSHOP_URL_TEMPLATE.format(v.mod_id)))
        elif act_folder is not None and chosen == act_folder:
            # 候选可能不在账本（D39 同款）：喂只带 mod_id 的兜底对象
            open_mod_folder(self.window(), self._game,
                            SimpleNamespace(mod_id=v.mod_id),
                            log=self._log)

    def _on_parse(self) -> None:
        if self._game is None:
            QMessageBox.information(
                self, "登记", "先在左上角选择游戏档案——登记的编号"
                "归属于当前档案。")
            return
        text = self._paste_box.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "登记", "先粘贴工坊网址、编号"
                                    "或下载命令。")
            return
        parsed = parse_lines(text.splitlines())
        existing = {m.mod_id: m
                    for m in self._repo.list_mods(self._game.app_id,
                                                  order_by="mod_id")}
        purged = (self._repo.filter_purged(parsed.mod_ids)
                  if parsed.mod_ids else set())
        self._plan = rf.classify_register(parsed, existing, purged,
                                          app_id=self._game.app_id)
        self._fill_preview()

    def _fill_preview(self) -> None:
        p = self._plan
        rows: list[tuple[str, str, str, QColor | None]] = []
        if p.blocked_by_mismatch:
            apps = "、".join(str(a) for a in p.wrong_game_apps)
            for mid in p.mismatch_ids:
                rows.append(("错档拦截", str(mid),
                             f"命令行里的游戏 AppID（{apps}）与当前档案"
                             f"（{p.app_id}）不一致——本批全部拦下", _C_BAD))
        for mid in p.to_register:
            if mid in p.blacklisted:
                rows.append(("黑名单警示", str(mid),
                             "在已清账黑名单：可登记，但确认入账/认领会"
                             "被拦——到「已清账管理」允许录入后才能入账",
                             _C_WARN))
            else:
                rows.append(("新登记", str(mid), "将登记为「待下载」", None))
        for mid, status in p.already_in_ledger:
            rows.append(("已在册", str(mid),
                         "跳过——当前状态："
                         + constants.STATUS_ZH.get(status, status),
                         _C_MUTED))
        for raw in p.invalid:
            shown = raw if len(raw) <= 60 else raw[:60] + "…"
            rows.append(("无法识别", "—", f"原样保留：{shown}", _C_BAD))
        if not rows:
            rows.append(("—", "—", "没有识别到任何内容——检查粘贴内容",
                         None))

        t = self._preview_table
        t.setRowCount(len(rows))
        for i, (cat, mid, note, color) in enumerate(rows):
            c1 = QTableWidgetItem(cat)
            c3 = QTableWidgetItem(note)
            if color is not None:
                c1.setForeground(color)
                c3.setForeground(color)
            t.setItem(i, 0, c1)
            t.setItem(i, 1, QTableWidgetItem(mid))
            t.setItem(i, 2, c3)

        if p.blocked_by_mismatch:
            apps = "、".join(str(a) for a in p.wrong_game_apps)
            self._mismatch_label.setText(
                f"检测到命令行里的游戏 AppID（{apps}）与当前档案不一致，"
                f"{len(p.mismatch_ids)} 个编号全部未登记——请分开粘贴："
                "其他游戏的命令先切换到对应档案再登记。")
            self._mismatch_label.show()
        else:
            self._mismatch_label.hide()
        self._btn_register.setEnabled(bool(p.to_register))
        self._btn_register.setText(f"登记新编号（{len(p.to_register)} 条）")

    def _on_register(self) -> None:
        p = self._plan
        if p is None or not p.to_register or p.blocked_by_mismatch:
            return
        self._btn_register.setEnabled(False)
        self._btn_register.setText("正在批查补元数据…")
        if self._log is not None:
            self._log.info(f"正在核对 {len(p.to_register)} 条的远端"
                           "元数据…")
        self._query_worker = _RegisterQueryWorker(p.to_register)
        self._query_worker.done.connect(self._on_query_done)
        self._query_worker.start()

    def _on_query_done(self, result: dict, error: str) -> None:
        self._query_worker = None
        if self._plan is None:
            return          # 切档案清场后迟到的回执：丢弃
        p = self._plan
        if error:
            if self._log is not None:
                self._log.warn(f"元数据批查失败（{error}）：登记照常，"
                               "标题/大小留空，等更新检测补全")
        metas = {mid: rf.RegisterMeta(
            mod_id=mid, title=t, creator=c, file_size=s, time_updated=tu)
            for mid, (tu, t, s, c) in result.items()}
        report = rf.apply_register(self._repo, p, metas)
        if self._log is not None:
            self._log.ok(f"已登记 {report.registered} 条为「待下载」"
                         f"（{report.with_metadata} 条带标题/大小，"
                         f"{report.without_metadata} 条待更新检测补全）"
                         "——可到 mod 库页按「待下载」筛选查看")
            if p.blacklisted:
                self._log.warn(
                    f"其中 {len(p.blacklisted)} 条在已清账黑名单：确认"
                    "入账/认领会被拦，到「已清账管理」允许录入后才能"
                    "入账")
        self._plan = None
        self._paste_box.clear()
        self._preview_table.setRowCount(0)
        self._btn_register.setEnabled(False)
        self._btn_register.setText("登记新编号")
        self._mismatch_label.hide()

    # ---------------- 收尾 ----------------

    def shutdown(self) -> None:
        """程序退出收尾（主窗口 hasattr 循环自动发现）：两个后台线程
        各等 1.5 秒，等不到 park 保活。批查线程只发网络请求；盘点线程
        只读磁盘，写库走 repo 事务（要么提交要么回滚，强杀不留半截）。"""
        for worker in (self._query_worker, self._scan_worker):
            if worker is not None and worker.isRunning():
                worker.wait(1500)
                netGate.park(worker)
