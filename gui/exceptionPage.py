"""异常处置
"""
r"""gui/exceptionPage.py · 桶①②③④⑤⑥ 的汇聚诊断台（V2 消费版，D24 拆分后）。

【本页是干什么的】
一键把当前游戏档案的异常全查一遍，按"桶"分组展示，每个桶给
"发生了什么 + 该去哪修"的引导。与账实核验页的分工：核验页管
"账↔盘逐条对账"，本页管"全档案异常总览与处置引导"。

【六类异常（六个桶）与修法】
① 下载未完成（盘上空目录残留）→ 修法 = 重新下载
② 账实不符（账本记已下载、盘上目录没了）→ 修法 = 重新下载
③ 孤儿目录（盘上有、账本不认识）→ 修法 = 到【入账中心】扫描认领
④ 远端失效（result=9）→ 先试重下，再三步主路径；进阶 = 关联替换
⑤ 查询失败与疑似合集 → 看明细；疑似合集去【更新检测】页展开
⑥ 多前端环境冲突（静态自查清单）

【D24 拆分说明】桶A 远端健康 / 桶B 依赖检测 / 桶C 本地标题提醒
各有专项页（【远端健康】【依赖检测】【标题检测】），本页不再重复。
联网深检仍是本页动作之一（桶④⑤的远端半边），共用 RemoteQueryWorker。

【V2 相对 V1 的适配（文案以本文件为准）】
- 桶① 的"下载中断"半边随 acf 质量谓词退役（三分类在抛弃清单）：
  只剩空目录；本地快检不再依赖 steamcmd 配置（引擎签名收窄），
  steamcmd_missing / acf_missing 两旗标与 acf 损坏弹窗退役；
- 桶③修法改指【入账中心】【扫描游戏目录】——V2 盘点最欢迎手动
  拷入/无 acf 记录的候选（V1"扫描只认完好条目"的旧说明作废）；
- 新增"排队中"提示卡：待下载且盘上已有内容（tracked_on_disk），
  非异常、不报警，指路入账中心确认补版本；
- "已收录"按 D30 改称"待下载"（桶④分类名随之：待下载·未下载过）；
- netGate 闸名 / 网址模板 / LogBus 三处 import 单源化（D37）；
- 软删除快照字段换血：confirmed_version / confirmed_source 顶替
  local_timeupdated / manifest / local_path（与核验页同一刀）。

【线程约定（照 V1）】深检进后台线程（RemoteQueryWorker，批边界
生效的停止协议）；线程收尾必须在 finished 里先取引用置 None 再
wait()（0xC0000409 纪律）；闸在 finished 里归还。深检进行中允许
切档案：结果区不动，渲染一律按 rep.game_id 取数。
"""
import time

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget, QInputDialog,
)
from gui.theme import font_px
from core.urlParser import workshop_url

from core import constants, netGate, steamPaths   # ★ 待核：netGate 若在 gui 包改这行
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.logBus import LogBus
from gui.modFolderOpener import open_mod_folder
from gui.remoteQueryWorker import RemoteQueryWorker, workshop_item_to_entry
from workflows import exceptionFlow
from gui.theme import font_px  # 字号单源（D25）

# 与批量下载步骤卡片一致的配色，按用途命名
_C_OK = "#46a758"      # 无异常
_C_FAIL = "#e5484d"    # 失效类（桶④）
_C_WARN = "#f5a623"    # 需要人管
_C_MUTED = "#8a8a8f"   # 说明文字/未检测

# 桶④表格的分类（顺序 = 分组视图的组序，也是排序键的序）。
# "待下载·未下载过" = 账本记「待下载」（从未下载过）而远端已确认失效
# （V1 分类名"已收录·未下载"按 D30 改词）；
# "已清账" = 彻底清账后的历史归档：默认视图不显示、状态筛可查
_CAT_ORDER = ("归档失效", "待下载·未下载过", "已替换", "已删除", "已清账")
_CAT_COLORS = {
    "归档失效": _C_FAIL,
    "待下载·未下载过": _C_WARN,
    "已替换": _C_MUTED,
    "已删除": "#6e6e73",
    "已清账": "#55555a",
}
_ID_LIMIT = 20   # 卡片里编号列表最多原样列出多少个，超出折成"…"


class _BucketCard(QFrame):
    """一张桶卡片（可折叠）。外观 = 带边框的圆角卡；头部一行
    （展开箭头 + 桶名 + 状态文字常显）+ 内容区。点头部收展；程序性
    set_expanded 走同一条路。不发收展信号、不做跨会话记忆：卡片是
    检测的产物，每次检测整体重建并按结果重设收展。"""

    def __init__(self, name: str, title: str, tip: str = "") -> None:
        super().__init__()
        # 样式选择器限定到本框：QLabel 也是 QFrame 子类，不限定会把
        # 边框画到卡里每行字上（批量下载步骤卡片踩过的坑）
        self.setObjectName(f"card_{name}")
        self.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        self.has_attention = False   # 「只看有问题的桶」据此藏卡
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)
        head = QWidget(self)
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(6)
        self._toggle = QToolButton(head)
        self._toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setText(title)
        self._toggle.setStyleSheet("border:none; font-weight:600;")
        if tip:
            self._toggle.setToolTip(tip)
        hh.addWidget(self._toggle)
        self._status = QLabel("", head)
        self._status.setWordWrap(True)   # 踩坑⑨：可能变长的标签开换行
        self._status.setStyleSheet("border:none;")
        hh.addWidget(self._status, 1)
        v.addWidget(head)
        self._body = QWidget(self)
        self._box = QVBoxLayout(self._body)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(4)
        v.addWidget(self._body)
        self._toggle.clicked.connect(self._on_toggle)

    def _on_toggle(self, expanded: bool) -> None:
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)

    def set_expanded(self, expanded: bool) -> None:
        # setChecked 只发 toggled 不发 clicked，外观同步要手动调一次
        self._toggle.setChecked(expanded)
        self._on_toggle(expanded)

    def set_status(self, text: str, color: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"border:none; color:{color};")

    def add_text(self, text: str, color: str = _C_MUTED) -> QLabel:
        lbl = QLabel(text, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"border:none; color:{color}; font-size:{font_px(12)}px;")
        self._box.addWidget(lbl)
        return lbl

    def add_button(self, text: str, tooltip: str, on_click) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setToolTip(tooltip)
        btn.clicked.connect(on_click)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        self._box.addLayout(row)
        return btn

    def add_widget(self, w: QWidget) -> None:
        self._box.addWidget(w)


class ExceptionPage(QWidget):
    # 跳命令生成页（桶①②修复 / 桶④先试重下）：MainWindow 转调
    # 与核验页同一个落点（切页 + 只勾这些）
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()
        self._game: Game | None = None
        self._cards: dict[str, _BucketCard] = {}
        self._cards_box: QVBoxLayout | None = None
        self._mods_by_id: dict[int, object] = {}
        self._last_local: exceptionFlow.LocalReport | None = None
        self._last_remote: exceptionFlow.RemoteFindings | None = None
        self._deep_worker: RemoteQueryWorker | None = None
        self._owner_game: Game | None = None
        self._b4_checked: set[int] = set()   # 桶④勾选（按 mid，跨重建存活）
        self._id_lists: list = []
        # 桶④管理台状态（页面级：卡片重建不重置，"筛着修、修完复检"
        # 的节奏不断——V1 口径）
        self._b4_rows: list[dict] = []
        self._b4_filter = "全部"
        self._b4_sort = "编号"
        self._b4_group = False
        self._b4_table: QTableWidget | None = None
        self._b4_combo_sort: QComboBox | None = None
        self._b4_batch_btn: QToolButton | None = None
        self._build_ui()

    # ---------- UI ----------
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

        title = QLabel("异常处置", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        tip = QLabel(
            "一键检查当前游戏档案的六类异常，按桶（一类异常一组）给出"
            "修法引导。\n【开始检测】纯离线、毫秒级、只读不动文件；"
            "【联网深度检测】后台重查远端实况（只读不写库），补全"
            "桶④⑤。写库一处：桶④的处置动作（右键软删除 / 关联替换，"
            "逐条确认）。\n"
            "「待下载」（已入库还没下载）是正常排队，不算异常、不在"
            "本页——批量下载去【下载命令生成】页。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # "先读我"说明卡（常驻、可折叠、不随检测重建——重建会收走
        # 用户刚展开的内容）
        self._guide_card = _BucketCard(
            "guide", "先读我：本页怎么用 · 名词解释 · 处置方式",
            tip="展开看详细说明：六个桶各是什么、两种检测的分工、"
                "失效 mod 的三步处置法与「关联替换」进阶用法。"
                "不想读可以一直收着。")
        self._fill_guide_card()
        root.addWidget(self._guide_card)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._detect_btn = QPushButton("开始检测", btn_row)
        self._detect_btn.setToolTip(
            "检查当前档案的六类异常（本地部分）。离线毫秒级、只读")
        self._detect_btn.clicked.connect(self._start_detect)
        h.addWidget(self._detect_btn)
        self._deep_btn = QPushButton("联网深度检测", btn_row)
        self._deep_btn.setToolTip(
            "重查库内全部未删除 mod 的远端实况（只读，不写库）：\n"
            "确认哪些真失效（result=9）、列出查询失败与疑似合集。\n"
            "需要网络；每 100 个一批、礼貌限速。\n"
            "本地快检没跑过时会先自动跑一遍本地部分")
        self._deep_btn.clicked.connect(self._start_deep)
        h.addWidget(self._deep_btn)
        self._deep_stop_btn = QPushButton("停止", btn_row)
        self._deep_stop_btn.setEnabled(False)
        self._deep_stop_btn.clicked.connect(self._stop_deep)
        h.addWidget(self._deep_stop_btn)
        self._only_issue = QCheckBox("只看有问题的桶", btn_row)
        self._only_issue.setToolTip(
            "勾上 = 隐藏「无异常」「待检测」与说明性的桶卡片，只留有"
            "异常和未能检查的部分。")
        self._only_issue.toggled.connect(self._apply_card_filter)
        h.addWidget(self._only_issue)
        self._progress = QProgressBar(btn_row)
        self._progress.setVisible(False)
        h.addWidget(self._progress, 1)
        root.addWidget(btn_row)

        # 查找框：按标题/编号过滤各桶条目——只隐藏不销毁，清空即全回
        srow = QWidget(self)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel("查找：", srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            "按标题 / 编号过滤各桶条目（即时生效，只隐藏）…")
        self._filter_edit.setToolTip(
            "输入关键词，各桶里匹配的条目留下、其余暂时隐藏；\n"
            "清空恢复全部。不影响检测，只帮你找行")
        self._filter_edit.textChanged.connect(
            lambda *_: self._apply_item_filter())
        sh.addWidget(self._filter_edit, 1)
        root.addWidget(srow)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        self._empty_hint = QLabel(
            "还没有检测结果——选好档案后点上方【开始检测】。", self)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: gray;")
        root.addWidget(self._empty_hint)
        root.addStretch(1)

    def _fill_guide_card(self) -> None:
        c = self._guide_card
        c.add_text(
            "六个桶分别是什么：\n"
            "· 桶① 下载未完成——盘上留下空目录（下载中断的断点残留）。\n"
            "  修法 = 重新下载。\n"
            "· 桶② 账实不符——账本记着「已下载」，盘上目录却没了"
            "（可能被手动删掉或杀毒软件清理）。修法 = 重新下载。\n"
            "· 桶③ 孤儿目录——盘上有以编号命名的文件夹，账本里却没"
            "这个编号。修法 = 到【入账中心】点【扫描游戏目录】：盘点"
            "会把它们摆成待认领，确认后入账（手动放进去的、没有下载"
            "记录的都认）；确认无用再手动删除。\n"
            "· 桶④ 远端失效——Steam 接口看不见该条目（result=9："
            "作者删除/下架/转私有等）。注意：接口看不见 ≠ 一定下载"
            "不了，建议先试下载再处置（见下）。\n"
            "· 桶⑤ 查询失败与疑似合集——查询失败可能是条目被设为私有"
            "或远端波动，稍后重测；疑似合集是「看着像一个 mod，实际"
            "可能是一整个合集」，需要到【更新检测】页展开确认。\n"
            "· 桶⑥ 多前端环境冲突——无法自动检测，对照桶⑥卡里的"
            "清单自查。")
        c.add_text(
            "三个专项页（本页之外，各管一摊）：\n"
            "· 【标题检测】——本地账本标题含提醒关键词（离线毫秒级；"
            "词表在设置页）。\n"
            "· 【远端健康】——标题不符 / 弃坑 / banned，匿名接口一次"
            "查询判定（无需 API key）。\n"
            "· 【依赖检测】——必需物品清单（缺依赖 / 依赖变化），"
            "带 key 官方接口拉取、现场粘贴 key。")
        c.add_text(
            "两种检测的分工：\n"
            "· 【开始检测】纯离线、毫秒级，只查本地（账本与磁盘），"
            "不动任何文件；\n"
            "· 【联网深度检测】逐条重查 Steam 远端实况（后台线程、"
            "礼貌限速），只读不写库——想诊断又不想动数据时用它。")
        c.add_text(
            "什么不是异常：\n"
            "· 「待下载」（已入库、还没下载）是正常排队：本地检测不报"
            "它们——批量下载到【下载命令生成】页勾选执行；逐个管理到"
            "【mod 库】页状态筛选「待下载」。\n"
            "· 待下载里盘上已有文件的：到【入账中心】点【扫描游戏"
            "目录】确认补版本（本页以「排队中」提示卡列出数量，"
            "不报警）。\n"
            "· 待下载里远端已失效的（result=9）不会被漏掉：联网"
            "深度检测后会出现在桶④表格里，分类「待下载·未下载过」。")
        c.add_text(
            "桶④失效 mod 怎么办（先试重下，再走三步）：\n"
            "⓪ 右键该行【跳到命令生成页】试着下载一次——result=9 "
            "的条目有时仍能下载成功（接口看不见≠内容服务器没有），"
            "下载成功后到【入账中心】点【扫描游戏目录】确认入账；\n"
            "① 下载失败的话，右键行【打开工坊页面】，看作者有没有留"
            "续作/重传的链接，拿到新编号；\n"
            "② 到【入账中心 · 登记】把新 mod 添加进账本；\n"
            "③ 回本页右键旧行【软删除此记录】——记录保留（可到"
            "【mod 库】页右键「恢复」）、盘上文件不动（想清文件去"
            "【清理与删除】页）。\n"
            "表格支持按分类筛选/分组/排序：「待下载·未下载过」是"
            "从没下载过就确认失效的条目——但它们恰恰最值得先试"
            "下载（本来就没下过，万一能下回来就赚了）。")
        c.add_text(
            "进阶：「关联替换」（右键菜单里，仅限有失效归档的条目）"
            "可把旧记录的备注、颜色标记、特别关注一键带给一个已在库"
            "的替代条目，归档里记下「旧 → 新」证据链——整理成果多"
            "时省事。菜单里该项是活的 = 这行有失效归档；置灰 = "
            "没有（悬停看原因）。不想用替换、直接走三步也完全没问题。")
        c.add_text(
            "关于 result=9 的一个诚实提醒：只要条目在 Steam 匿名"
            "接口看不见，每次联网深度检测都会把它列回本表——即使"
            "你已确认它实际可用、甚至已成功下载到本地。这是 Steam "
            "接口的判定边界，不是检测错了；本地已有文件的行会在"
            "说明列注明。")
        c.add_text(
            "修完怎么确认：再点一次【开始检测】，对应桶的数字应归零"
            "（软删除的行会变「已删除」分类）；远端数字要重跑"
            "【联网深度检测】才会刷新。")

    # ---------- 对外（MainWindow 调用）----------
    def set_game(self, game: Game | None) -> None:
        # self._game 必须无条件先赋值——它是本页一切检测的出发点。
        # 修24 曾把这条赋值挪进"深检进行中"分支（本意：深检中切档案
        # 变量也要跟上），普通路径于是丢了赋值：深检没在跑→不进分支
        # →self._game 永远停在 None；而它是 None 又永远开不了深检→
        # 永远进不了分支。死锁闭环，重启无效。页面"当前游戏"标签在
        # 普通路径照常更新，页面看着有档案、变量里永远没有。现恢复
        # 无条件赋值；修24 的本意保留——深检中切档案，变量照样跟上，
        # 只是结果区不动。
        self._game = game
        if self._deep_worker is not None:
            # 深检进行中：只更新标签，不动结果区——结果仍归属开始
            # 深检时的档案（渲染按 rep.game_id 取数，不会串）
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）"
                if game is not None else "当前游戏：（未选择）")
            return
        self._last_local = None
        self._last_remote = None
        self._owner_game = None
        self._clear_cards()
        self._summary.setText("")
        self._empty_hint.setVisible(True)
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._detect_btn.setEnabled(False)
            self._deep_btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._detect_btn.setEnabled(True)
            self._deep_btn.setEnabled(True)

    def shutdown(self) -> None:
        """退出收尾：深检线程等 1.5 秒，等不到交 netGate.park 保活
        （线程只读网络不写库，零数据损失）——v2.45 口径。"""
        if self._deep_worker is not None:
            self._deep_worker.stop()
            if not self._deep_worker.wait(1500):
                netGate.park(self._deep_worker)
            self._deep_worker = None

    # ---------- 本地快检 ----------
    def _reap_dead_worker(self) -> None:
        """僵尸引用收割：深检线程引用还在、线程本体其实已结束
        （finished 信号万一没送达的场景）——摘引用、归还闸、恢复
        按钮，别让一次没收尾的深检把两个检测按钮永远锁死。"""
        w = self._deep_worker
        if w is not None and w.isFinished():
            self._deep_worker = None
            try:
                netGate.release(constants.NET_GATE_DEEP_CHECK)
            except Exception as exc:  # 归还异常不拦检测
                self._log.warn(f"深检闸归还异常（已忽略）：{exc}")
            self._set_deep_running(False)

    def _start_detect(self) -> None:
        self._reap_dead_worker()
        if self._game is None:
            self._log.warn("本地快检未开始：还没有选择游戏档案")
            return
        if self._deep_worker is not None:
            self._log.warn("本地快检未开始：联网深度检测进行中")
            return
        game = self._latest_game()
        if game is None:
            return
        self._run_local(game)

    def _run_local(self, game: Game) -> exceptionFlow.LocalReport | None:
        """跑本地快检并渲染。V2 引擎不再依赖 steamcmd 配置、不再抛 acf 损坏（解析退役）——失败路径只剩理论上的账本异常。"""
        try:
            report = exceptionFlow.detect_local(self._repo, game)
        except Exception as exc:  # noqa: BLE001 —— 显式报告，不静默吞
            self._log.error(f"检测中断：{exc}")
            QMessageBox.critical(self, "检测中断", f"检测没有完成：\n{exc}")
            return None
        self._last_local = report
        self._render(report, remote=self._last_remote)
        return report

    def _latest_game(self) -> Game | None:
        """检测前核对下载目录（决策 21：永远可由 steamcmd 位置推导）。
        steamcmd 挪过位置的话旧目录成死路径——按当前位置重新推导并
        写回。与扫描/核验页同款三步：推导 → 写库 → 内存同步。"""
        assert self._game is not None
        effective, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id, download_dir=effective)
            self._game = self._repo.get_game(self._game.app_id)
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective}")
        return self._game

    def _start_deep(self) -> None:
        self._reap_dead_worker()
        if self._game is None:
            self._log.warn("深度检测未开始：还没有选择游戏档案")
            return
        if self._deep_worker is not None:
            self._log.warn("深度检测未开始：上一场深检引用未收尾")
            return
        game = self._latest_game()
        if game is None:
            return
        # 本地快检没跑过就先跑一遍（同步毫秒级）：深检结果要与本地
        # 结果同屏重画，且桶④的"账本半边"来自它
        if self._run_local(game) is None:
            return
        mods = self._repo.list_mods(game.app_id)
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn("当前档案没有可深检的 mod（已删除的除外）")
            return
        busy = netGate.try_acquire(constants.NET_GATE_DEEP_CHECK)
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再跑深度检测。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(f"深度检测未开始：{busy} 正在使用联网查询")
            return
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_deep_running(True)
        interval = self._settings.get_int("api_request_interval_ms", 200)
        retries = self._settings.get_int("api_max_retries", 3)

        def make_query(cancel_event):
            client = SteamApiClient(interval_ms=interval,
                                    max_retries=retries,
                                    cancel_event=cancel_event)
            return lambda batch: client.query_details(batch)

        self._deep_worker = RemoteQueryWorker(
            ids, make_query=make_query, to_entry=workshop_item_to_entry,
            interval_ms=interval, max_retries=retries, parent=self)
        self._deep_worker.batch_done.connect(self._progress.setValue)
        self._deep_worker.succeeded.connect(self._on_deep_succeeded)
        self._deep_worker.failed.connect(self._on_deep_failed)
        self._deep_worker.stopped.connect(self._on_deep_stopped)
        # 线程跑完统一由 finished 收尾销毁（不能在 succeeded/failed/
        # stopped 里置 None——销毁仍在运行的线程 = 闪退 0xC0000409）
        self._deep_worker.finished.connect(self._on_deep_finished)
        self._deep_worker.start()
        self._log.info(f"联网深度检测开始：{len(ids)} 个 mod（只读不写库）…")

    def _stop_deep(self) -> None:
        if self._deep_worker is not None:
            self._deep_worker.stop()
            self._deep_stop_btn.setEnabled(False)
            self._summary.setText(
                "已请求停止：等待与重试已中断，最多再等一次在途请求收场"
                "（网络不畅时约 10~40 秒）——退出软件不必等它，可直接关。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止联网深度检测")

    def _set_deep_running(self, running: bool) -> None:
        self._detect_btn.setEnabled(not running and self._game is not None)
        self._deep_btn.setEnabled(not running and self._game is not None)
        self._deep_stop_btn.setEnabled(running)
        self._progress.setVisible(running)

    def _on_deep_succeeded(self, entries: list) -> None:
        self._set_deep_running(False)
        if self._last_local is None:
            return   # 理论到不了：深检前必先本地快检；防御一行
        remote = exceptionFlow.classify_entries(entries)
        self._last_remote = remote
        self._render(self._last_local, remote=remote)
        self._log.ok(
            f"联网深度检测完成：确认失效 {len(remote.invalid)}，"
            f"查询失败 {len(remote.query_failed)}，"
            f"疑似合集 {len(remote.suspected_collection)}")

    def _on_deep_failed(self, message: str) -> None:
        self._set_deep_running(False)
        # 不弹窗（quiet 口径）：网络失败常见，红字汇总 + 日志足够
        self._summary.setText(
            f"联网深度检测失败：{message}\n（本地快检结果仍有效，"
            "见下方卡片；稍后可重试。可以参考 watt toolkit 的连通性"
            "测试，若一直不通过，可以尝试关闭 watt toolkit 再试。"
            "下载 mod 同理。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"联网深度检测失败：{message}")

    def _on_deep_stopped(self) -> None:
        self._set_deep_running(False)
        self._summary.setText(
            "已停止联网深度检测，本次结果未使用（本地快检结果仍有效）。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("联网深度检测已手动停止")

    def _on_deep_finished(self) -> None:
        w = self._deep_worker
        self._deep_worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_DEEP_CHECK)

    def _refresh_after_dispose(self) -> None:
        """桶④处置（软删除/替换/清账）后的复检刷新。深检进行中不
        自动复检——深检完成后 _render 会重建表格；否则立即跑本地
        快检（毫秒级）。"""
        if self._deep_worker is not None:
            self._log.info("联网深度检测进行中：暂不自动复检，"
                           "深检完成后表格会按最新账本状态刷新")
            return
        self._start_detect()

    # ---------- 渲染 ----------
    def _render(self, rep: exceptionFlow.LocalReport,
                remote: exceptionFlow.RemoteFindings | None = None) -> None:
        """把检测报告画成桶卡片。remote=None：只渲染本地快检结果
        （桶④仅账本半边、桶⑤待深检）。渲染一律按 rep.game_id 取数
        （不是当前档案）——深检进行中切过档案也不会串。"""
        self._clear_cards()
        self._empty_hint.setVisible(False)
        owner = self._repo.get_game(rep.game_id)
        self._owner_game = owner
        owner_name = owner.name if owner is not None else f"档案 {rep.game_id}"
        self._mods_by_id = {
            m.mod_id: m for m in self._repo.list_mods(rep.game_id)}
        lib_ids = set(self._mods_by_id)
        disk_ok = not rep.dead_root

        # ---- 旗标横幅：本次没能检查的部分（V2 只剩 dead_root）----
        flags: list[str] = []
        if rep.dead_root:
            flags.append(
                "下载目录不存在——盘面三桶（①②③）无法检测；已按当前 "
                "steamcmd 位置重推导仍无效，请检查设置页 steamcmd 路径"
                "与该档案是否匹配")
        if flags:
            card = self._card(
                "flags", "本次未能检查的部分",
                tip="有些前提没满足，一部分检查没跑成——原因与"
                    "解决办法看下面几行。", attention=True)
            card.set_status(f"{len(flags)} 项前提不满足", _C_WARN)
            for f in flags:
                card.add_text("· " + f, _C_WARN)
            card.set_expanded(True)

        # ---- 排队中（非异常）：待下载且盘上已有内容 ----
        if rep.tracked_on_disk:
            card = self._card(
                "tq", "排队中：待下载且盘上已有内容（非异常）",
                tip="账本记「待下载」、盘上已有文件的条目——正常排队，"
                    "不是异常。确认补版本在入账中心。",
                attention=False)
            card.set_status(f"{len(rep.tracked_on_disk)} 个", _C_MUTED)
            card.add_text(
                "到【入账中心】点【扫描游戏目录】：盘点会把它们摆成"
                "待认领，确认后补上盘面版本、转「已下载」。", _C_MUTED)
            self._add_id_list(
                card,
                [(mid, self._title_of(mid), "盘上已有内容")
                 for mid in rep.tracked_on_disk],
                allow_cmd=False)
            card.set_expanded(False)

        # ---- 桶① 下载未完成（V2：只剩空目录半边）----
        b1_ids = list(rep.empty_dirs)
        card = self._card(
            "b1", "桶① 下载未完成（空目录残留）",
            tip="盘上留下的空目录（下载中断的断点残留）；修法 = "
                "重新下载。",
            attention=bool(b1_ids) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not b1_ids:
            card.set_status("无异常", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(f"{len(b1_ids)} 个空目录", _C_WARN)
            card.add_text(
                "修法 = 重新下载：空目录是断点残留；下载完成后到"
                "【入账中心】点【扫描游戏目录】入账。")
            self._add_id_list(
                card,
                [(mid, self._title_of(mid), "盘上空目录（断点残留）")
                 for mid in b1_ids],
                allow_cmd=True)
            card.add_button(
                f"生成重下命令（{len(b1_ids)} 条，可在命令页增减）…",
                "跳到【下载命令生成】页并勾选这些编号；不想下的取消"
                "勾选即可，复制前还能再核对预览",
                lambda _=False, ids=list(b1_ids):
                self._emit_command_gen(ids))
            card.set_expanded(True)

        # ---- 桶② 账实不符 ----
        card = self._card(
            "b2", "桶② 账实不符（账本记已下载、盘上没有）",
            tip="账本里记着已下载，下载目录里却找不到——可能被手动"
                "删除或杀毒软件清理。快捷修法 = 重新下载。",
            attention=bool(rep.missing) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not rep.missing:
            card.set_status("无异常", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(f"{len(rep.missing)} 个", _C_WARN)
            card.add_text(
                "目录整个没了（可能被手动删除/杀毒清理）。快捷修法 = "
                "重新下载；「校验重下 validate」「标记为已移除」等完整"
                "三选在【账实核验】页对应行里。")
            self._add_id_list(
                card,
                [(mid, self._title_of(mid), "目录不存在")
                 for mid in rep.missing],
                allow_cmd=True)
            card.add_button(
                f"生成重下命令（{len(rep.missing)} 条）…",
                "跳到【下载命令生成】页并勾选这些编号",
                lambda _=False, ids=list(rep.missing):
                self._emit_command_gen(ids))
            card.set_expanded(True)

        # ---- 桶③ 孤儿目录 ----
        card = self._card(
            "b3", "桶③ 孤儿目录（盘上有、账本不认识）",
            tip="下载目录里有以编号命名的文件夹，账本里却没有这个"
                "编号——可能是手动放进去的，或别的工具下的。",
            attention=bool(rep.orphans) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not rep.orphans:
            card.set_status("无异常", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(f"{len(rep.orphans)} 个", _C_WARN)
            card.add_text(
                "到【入账中心】点【扫描游戏目录】：盘点把账外目录摆成"
                "待认领，确认后入账（手动放进去的、没有下载记录的都"
                "认）。确认无用再手动删除。")
            # 孤儿不在账本里：没有标题可查，跳命令页也没用（命令页
            # 按账本勾选）——行菜单只给查看类
            self._add_id_list(
                card,
                [(mid, "（未入账）", "账本没有这个编号")
                 for mid in rep.orphans],
                allow_cmd=False)
            card.set_expanded(True)

        # ---- 桶④ 远端失效：失效 mod 管理台（表格）----
        remote_invalid = list(remote.invalid) if remote else []
        all_invalid = sorted(set(rep.failed_ids) | set(remote_invalid))
        newly = [i for i in remote_invalid
                 if i not in set(rep.failed_ids)]
        recovered = sorted(
            set(rep.failed_ids) & (set(remote.ok) if remote else set()))
        card = self._card(
            "b4", "桶④ 远端失效（result=9：删除/下架/转私有等）",
            tip="Steam 匿名接口看不见这个条目（result=9）。注意："
                "接口看不见 ≠ 一定下载不了——实测有的条目 steamcmd "
                "仍能下载成功。建议先右键行【跳到命令生成页】试下"
                "一次，下不回来再走三步（见引导文字）。进阶："
                "「关联替换」（右键菜单，仅限有失效归档的行）。",
            attention=bool(all_invalid))
        if not all_invalid:
            if remote is None:
                card.set_status("无（按账本记录）", _C_OK)
                card.add_text(
                    "远端实况重查（区分真失效/暂时查不到）点上方"
                    "【联网深度检测】。")
            else:
                card.set_status(
                    f"无异常（联网重查 {len(remote.ok) + len(remote.invalid) + len(remote.query_failed)} 个条目）",
                    _C_OK)
            card.set_expanded(False)
        else:
            rec_map = {r.mod_id: r for r in rep.failed_records}
            rows: list[dict] = []
            for mid in all_invalid:
                rec = rec_map.get(mid)
                m = self._mods_by_id.get(mid)
                st = m.status if m is not None else None
                replaced = rec.replaced_by if rec else None
                if st == "deleted":
                    cat, can_repl, can_del = "已删除", False, False
                elif replaced:
                    cat, can_repl, can_del = "已替换", False, False
                elif st == "tracked":
                    cat, can_repl, can_del = "待下载·未下载过", False, True
                elif m is None:
                    # 账本条目已不存在（彻底清账后 mods 行物理删除，
                    # failed_mods 证据行按决策刻意存活）——终结历史，
                    # 不再给任何处置入口
                    cat, can_repl, can_del = "已清账", False, False
                else:
                    cat = "归档失效"
                    can_repl, can_del = rec is not None, True
                if st == "deleted":
                    note = "已软删除——恢复到【mod 库】页右键「恢复」"
                elif replaced:
                    note = f"已替换 → mod {replaced}"
                elif m is None:
                    note = ("已彻底清账（在【清理与删除】执行）：账本条目"
                            "与盘上文件均已清除；本行为失效归档的历史证据，"
                            "默认不显示，状态筛「已清账」可查")
                elif rec and rec.reason:
                    note = rec.reason
                    if m is not None and m.local_size:
                        note += "（本地文件仍在，照常用）"
                elif st == "tracked":
                    note = ("result=9 确认（未下载过；偶尔仍能下载，"
                            "建议先试）" if mid in remote_invalid
                            else "远端已失效（从未下载过）")
                elif st == "downloaded" and mid in remote_invalid:
                    note = ("本地已有文件——result=9 有时仍能下载成功，"
                            "已在本地就照常用（每次深检仍会列在本表，"
                            "属 Steam 接口判定边界）")
                elif mid in remote_invalid:
                    note = "本次联网确认 result=9"
                else:
                    note = "（无归档：仅有 failed 状态）"
                rows.append({"mid": mid, "title": self._title_of(mid),
                             "cat": cat, "note": note,
                             "can_replace": can_repl,
                             "can_delete": can_del,
                             "replaced_by": replaced})
            self._b4_rows = rows
            card.has_attention = any(r["cat"] != "已清账" for r in rows)
            n_cat = {c: 0 for c in _CAT_ORDER}
            for r in rows:
                n_cat[r["cat"]] += 1
            n_arch = n_cat["已清账"]
            n_live = len(all_invalid) - n_arch
            live = [c for c in _CAT_ORDER if c != "已清账"]
            stat = " · ".join(f"{c} {n_cat[c]}" for c in live if n_cat[c])
            extra = f"（含联网新确认 {len(newly)} 个）" if newly else ""
            if n_live:
                if n_arch:
                    stat += (f" · 已清账 {n_arch}（历史归档，默认不显示，"
                             "状态筛「已清账」可查）")
                card.set_status(f"{n_live} 个{extra}：{stat}", _C_FAIL)
            else:
                card.set_status(
                    f"无待处置异常 · 已清账 {n_arch}"
                    "（彻底清账的历史归档，默认不显示，状态筛「已清账」可查）",
                    _C_MUTED)
            card.add_text(
                "result=9 = Steam 接口看不见条目（作者删除/下架/"
                "转私有等）——接口看不见 ≠ 一定下载不了：实测有的"
                "result=9 条目 steamcmd 仍能下载成功，有的报 File "
                "Not Found（真没了）。建议先右键行【跳到命令生成页】"
                "试下载一次（成本极低）。下不回来再走三步：① 右键行"
                "【打开工坊页面】找作者的续作/重传 → ② 到【入账中心" + "· 登记】添加新编号 → ③ 回本行右键【软删除此记录】"
                "（记录保留可恢复，盘上文件不动）。「关联替换」（进阶）"
                "只对有失效归档的行可用——菜单里该项是活的即有归档、"
                "置灰即没有（悬停看原因）。表格可按分类筛选/分组/排序。\n"
                "处置闭环：右键软删除的行变「已删除」（记录保留，可到"
                "【mod 库】页恢复）；在【清理与删除】彻底清账的行变"
                "「已清账」——账本条目与盘上文件都已清除，不会再出现在"
                "任何待办里，本表默认也不再显示（状态筛「已清账」可查"
                "历史归档）。")
            n_no_arch = sum(1 for r in rows
                            if r["cat"] == "归档失效"
                            and not r["can_replace"])
            if n_no_arch:
                card.add_text(
                    f"其中 {n_no_arch} 行（红）还没有失效归档，「关联替换」"
                    "暂为置灰：归档只由【更新检测】建立（本页深度检测纯只读、"
                    "不建档）。到【更新检测】页点【开始检测】，汇总行出现"
                    "「result=9 建档 N」即建档完成，回本页复检后解锁。"
                    "黄行（待下载·未下载过）的置灰是设计如此：从未下载过、"
                    "没有可转移的整理内容，刻意不建档。", _C_WARN)
            self._build_b4_card(card)
            if recovered:
                card.add_text(
                    "本地记为失效、本次远端查询却正常（未列入上表）："
                    + _ids_text(recovered)
                    + "（可能是作者恢复了条目——要不要继续用自行判断；"
                      "更新检测会照常盯它们）", _C_WARN)
            card.add_text(
                "处置后点【开始检测】复检（软删除的行会变「已删除」）；"
                "远端数字要重跑【联网深度检测】才会刷新。已清账的历史行"
                "不受复检影响——账本条目已删，本地与远端都不会再查到"
                "它们，属预期不是刷新失灵。")
            card.set_expanded(card.has_attention)

        # ---- 桶⑤ 查询失败 / 疑似合集 ----
        card = self._card(
            "b5", "桶⑤ 查询失败 / 疑似合集",
            tip="查询失败 = Steam 接口返回了异常结果码（可能私有/"
                "地区限制/远端波动），稍后重测；疑似合集 = 看着像"
                "一个 mod、实际可能是一整个合集，要展开确认后才能"
                "入库。",
            attention=bool(remote is not None and (
                    remote.query_failed or remote.suspected_collection)))
        if remote is None:
            card.set_status("待联网深度检测", _C_MUTED)
            card.add_text(
                "本桶需要重新查询远端才能判定，点上方【联网深度检测】。")
            card.set_expanded(False)
        else:
            n5 = (len(remote.query_failed)
                  + len(remote.suspected_collection))
            if n5 == 0 and not remote.malformed:
                card.set_status("无异常（本次联网重查全部正常）", _C_OK)
                card.set_expanded(False)
            else:
                card.set_status(
                    f"查询失败 {len(remote.query_failed)}"
                    f" · 疑似合集 {len(remote.suspected_collection)}",
                    _C_WARN)
                if remote.query_failed:
                    card.add_text(
                        "查询失败（接口返回异常结果码）——可能是条目被"
                        "设为私有/地区限制/远端波动——稍后重测；确认"
                        "真失效的会归入桶④。")
                    self._add_id_list(
                        card,
                        [(mid, self._title_of(mid), "查询失败")
                         for mid in remote.query_failed],
                        allow_cmd=False, allow_dispose=True)
                if remote.suspected_collection:
                    card.add_text(
                        "疑似合集（远端缺少文件大小）——确认与展开入库"
                        "到【更新检测】页进行（对应行有【展开合集…】"
                        "按钮）——本页是纯读诊断，不做登记。")
                    self._add_id_list(
                        card,
                        [(mid, self._title_of(mid), "疑似合集")
                         for mid in remote.suspected_collection],
                        allow_cmd=False)
                if remote.malformed:
                    card.add_text(
                        f"另有 {len(remote.malformed)} 条响应数据读不动，"
                        "已忽略（极少见，多为接口异常波动）。", _C_WARN)
                card.set_expanded(True)

        # ---- 附加发现：非数字内容 ----
        if rep.non_numeric:
            card = self._card(
                "nn", "附加发现：非数字内容（编号之外的目录/文件）",
                tip="下载目录树里只应存在以编号命名的文件夹；其他"
                    "来路的目录/文件列在这里。本工具不代删：人工确认"
                    "后自行处理。", attention=True)
            card.set_status(f"{len(rep.non_numeric)} 项", _C_WARN)
            for name in rep.non_numeric[:_ID_LIMIT]:
                card.add_text("· " + name)
            if len(rep.non_numeric) > _ID_LIMIT:
                card.add_text(
                    f"…以及另外 {len(rep.non_numeric) - _ID_LIMIT} 项")
            card.set_expanded(True)

        # ---- 桶⑥ 多前端环境冲突（静态自查清单）----
        card = self._card(
            "b6", "桶⑥ 多前端环境冲突（无法自动检测，自查）",
            tip="同一个 steamcmd 目录可能被多个工具指挥（如 RimSort），"
                "删掉的 mod 可能被别的工具下回来。无法程序化检测，"
                "对照卡里的清单自查。", attention=False)
        card.set_status("自查清单", _C_MUTED)
        card.add_text(exceptionFlow.MULTIFRONTEND_NOTE)
        card.set_expanded(False)

        # ---- 汇总行（已清账历史归档不算"本地发现"，决策 104）----
        n_purged_arch = sum(1 for mid in rep.failed_ids
                            if mid not in lib_ids)
        local_total = (len(b1_ids) + len(rep.missing) + len(rep.orphans)
                       + len(rep.failed_ids) - n_purged_arch
                       + len(rep.non_numeric))
        if remote is not None:
            remote_total = (len(remote.invalid)
                            + len(remote.query_failed)
                            + len(remote.suspected_collection))
        else:
            remote_total = None
        if local_total == 0 and not flags and not (remote_total or 0):
            tail = "（含联网深度检测）" if remote is not None else ""
            self._summary.setStyleSheet(f"color: {_C_OK};")
            self._summary.setText(f"六桶检查完毕{tail}：未发现异常。")
            self._log.ok(f"异常检测（{owner_name}）：未发现异常")
            self._apply_card_filter()
            return
        if (not local_total and not (remote_total or 0) and flags):
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._summary.setText(
                "可判范围内未发现异常；有未能检查的部分，见上方横幅。")
            self._log.warn(f"异常检测（{owner_name}）：部分范围未能检查")
            self._apply_card_filter()
            return
        parts: list[str] = []
        if local_total:
            parts.append(f"本地发现 {local_total} 处")
        if remote_total:
            parts.append(f"联网深检确认 {remote_total} 处"
                         "（失效/查询失败/疑似合集）")
        if rep.tracked_on_disk:
            parts.append(f"待下载·盘上已有内容 {len(rep.tracked_on_disk)}"
                         "（排队中，非异常）")
        self._summary.setStyleSheet(f"color: {_C_WARN};")
        self._summary.setText(
            f"「{owner_name}」：" + "；".join(parts)
            + "。各桶卡片内有明细与修法引导，修完复检。")
        self._log.warn(f"异常检测（{owner_name}）：" + "；".join(parts))
        self._apply_card_filter()

    # ---------- 卡片基建 ----------
    def _card(self, name: str, title: str, tip: str,
              attention: bool) -> _BucketCard:
        card = _BucketCard(name, title, tip)
        card.has_attention = attention
        self._cards[name] = card
        self._cards_box.addWidget(card)
        return card

    def _clear_cards(self) -> None:
        self._cards = {}
        self._b4_table = None
        self._b4_combo_sort = None
        self._b4_batch_btn = None
        self._id_lists = []
        if self._cards_box is None:
            return
        while self._cards_box.count():
            item = self._cards_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _apply_card_filter(self) -> None:
        only_issues = self._only_issue.isChecked()
        for card in self._cards.values():
            card.setVisible((not only_issues) or card.has_attention)

    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or "（无标题）"

    # ---------- 桶④管理台（V1 全量保留；分类名随 D30）----------
    def _build_b4_card(self, card: _BucketCard) -> None:
        bar = QWidget(card)
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(4)
        lbl_f = QLabel("状态", bar)
        lbl_f.setStyleSheet("color: gray;")
        bh.addWidget(lbl_f)
        combo_filter = QComboBox(bar)
        combo_filter.addItems(["全部", *_CAT_ORDER])
        combo_filter.setCurrentText(self._b4_filter)
        combo_filter.setToolTip(
            "只显示所选分类的失效条目。「待下载·未下载过」= 从未下载过"
            "就失效的条目（无可转移的整理内容，直接软删除即可）；"
            "「已删除」= 本页软删除过的（可到 mod 库页恢复）；"
            "「已清账」= 彻底清账后的历史归档（只读）。")
        combo_filter.currentTextChanged.connect(self._on_b4_filter)
        bh.addWidget(combo_filter)
        lbl_v = QLabel("视图", bar)
        lbl_v.setStyleSheet("color: gray;")
        bh.addWidget(lbl_v)
        combo_view = QComboBox(bar)
        combo_view.addItems(["平铺", "按分类分组"])
        combo_view.setCurrentIndex(1 if self._b4_group else 0)
        combo_view.setToolTip(
            "平铺 = 全部行一张表；按分类分组 = 表内插分类标题行。")
        combo_view.currentIndexChanged.connect(self._on_b4_view)
        bh.addWidget(combo_view)
        lbl_s = QLabel("排序", bar)
        lbl_s.setStyleSheet("color: gray;")
        bh.addWidget(lbl_s)
        self._b4_combo_sort = QComboBox(bar)
        self._b4_combo_sort.addItems(["编号", "标题", "分类"])
        self._b4_combo_sort.setCurrentText(self._b4_sort)
        self._b4_combo_sort.setEnabled(not self._b4_group)
        self._b4_combo_sort.setToolTip(
            "平铺视图下生效；分组视图下组内固定按编号排列。")
        self._b4_combo_sort.currentTextChanged.connect(self._on_b4_sort)
        bh.addWidget(self._b4_combo_sort)
        self._b4_batch_btn = QToolButton(bar)
        self._b4_batch_btn.setText("对勾选项 ▾")
        self._b4_batch_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._b4_batch_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        bm = QMenu(self._b4_batch_btn)
        bm.setToolTipsVisible(True)
        a_pages = QAction("批量打开工坊页面", bm)
        a_pages.setToolTip("勾选 mod 的工坊页面逐个在浏览器打开"
                           "（每个一页标签，开前确认）")
        a_pages.triggered.connect(self._b4_batch_open_pages)
        a_dirs = QAction("批量打开文件夹", bm)
        a_dirs.setToolTip("勾选 mod 的下载内容文件夹逐个在文件管理器打开")
        a_dirs.triggered.connect(self._b4_batch_open_dirs)
        a_del = QAction("批量软删除…", bm)
        a_del.setToolTip("把勾选中「可处置」的失效条目整批软删除"
                         "（列清单确认、默认否；已替换/已删除的行自动跳过）")
        a_del.triggered.connect(self._b4_batch_soft_delete)
        a_purge = QAction("批量彻底清账…", bm)
        a_purge.setToolTip(
            "把勾选中「已删除」分类的行整批物理删除账本记录（不可恢复，"
            "自动登记黑名单）；其他分类的行自动跳过。\n"
            "磁盘一个字节不动；名下有备份登记的条目默认被拒绝，"
            "确认框里勾选才连带删登记")
        a_purge.triggered.connect(self._b4_batch_purge)
        bm.addAction(a_purge)
        for a in (a_pages, a_dirs, a_del):
            bm.addAction(a)
        self._b4_batch_btn.setMenu(bm)
        bh.addWidget(self._b4_batch_btn)
        bh.addStretch(1)
        card.add_widget(bar)

        t = QTableWidget(0, 6, card)
        t.setHorizontalHeaderLabels(
            ["✓", "↗", "mod 标题", "mod 编号", "分类", "说明 / 原因"])
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setWordWrap(False)   # 踩坑④：表格关换行，全文进 tooltip
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = t.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 32), (1, 36), (3, 110), (4, 120), (5, 260)):
            t.setColumnWidth(col, width)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self._on_b4_menu)
        t.cellDoubleClicked.connect(self._on_b4_double)
        card.add_widget(t)
        self._b4_table = t
        self._rebuild_b4_table()

    def _on_b4_filter(self, text: str) -> None:
        self._b4_filter = text
        self._rebuild_b4_table()

    def _on_b4_view(self, index: int) -> None:
        self._b4_group = (index == 1)
        if self._b4_combo_sort is not None:
            self._b4_combo_sort.setEnabled(not self._b4_group)
        self._rebuild_b4_table()

    def _on_b4_sort(self, text: str) -> None:
        self._b4_sort = text
        self._rebuild_b4_table()

    def _rebuild_b4_table(self) -> None:
        """桶④表格的唯一灌表出口：勾选剪除 → 状态筛选 → 查找过滤 →
        排序 →（可选）分组 → 灌行。绝不查库——表格只是视图开关。"""
        t = self._b4_table
        if t is None:
            return
        rows = list(self._b4_rows)
        self._b4_checked &= {r["mid"] for r in rows
                             if r["cat"] != "已清账"}
        if self._b4_filter != "全部":
            rows = [r for r in rows if r["cat"] == self._b4_filter]
        else:
            rows = [r for r in rows if r["cat"] != "已清账"]
        kw = self._filter_edit.text().strip().casefold()
        if kw:
            rows = [r for r in rows
                    if kw in r["title"].casefold()
                    or kw in str(r["mid"])
                    or kw in (r["note"] or "").casefold()]
        if self._b4_sort == "标题":
            rows.sort(key=lambda r: (r["title"], r["mid"]))
        elif self._b4_sort == "分类":
            order = {c: i for i, c in enumerate(_CAT_ORDER)}
            rows.sort(key=lambda r: (order.get(r["cat"], 9), r["mid"]))
        else:
            rows.sort(key=lambda r: r["mid"])
        t.clearSpans()
        t.setRowCount(0)
        if self._b4_group:
            for cat in _CAT_ORDER:
                subset = [r for r in rows if r["cat"] == cat]
                if not subset:
                    continue
                self._append_b4_group_header(t, f"{cat}（{len(subset)} 条）")
                for r in subset:
                    self._append_b4_row(t, r)
        else:
            for r in rows:
                self._append_b4_row(t, r)
        n_vis = t.rowCount()
        t.setFixedHeight(max(120, min(380, 28 * n_vis + 32)))
        if not rows:
            self._log.info("桶④：当前筛选下没有条目")

    def _append_b4_group_header(self, t: QTableWidget, text: str) -> None:
        row = t.rowCount()
        t.insertRow(row)
        it = QTableWidgetItem(text)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setBackground(QBrush(QColor("#262626")))
        t.setItem(row, 0, it)
        t.setSpan(row, 0, 1, 6)

    def _append_b4_row(self, t: QTableWidget, r: dict) -> None:
        row = t.rowCount()
        t.insertRow(row)
        cb = QCheckBox(t)
        cb.setToolTip("勾选后可用「对勾选项 ▾」批量操作")
        cb.setChecked(r["mid"] in self._b4_checked)
        cb.toggled.connect(
            lambda on, mid=r["mid"]: self._on_b4_check(mid, on))
        t.setCellWidget(row, 0, cb)
        btn = QToolButton(t)
        btn.setText("↗")
        btn.setToolTip("打开工坊页面（右键本行有更多动作）")
        btn.clicked.connect(
            lambda _=False, m=r["mid"]: self._open_workshop(m))
        t.setCellWidget(row, 1, btn)
        ti = QTableWidgetItem(r["title"])
        ti.setToolTip(f"{r['mid']} {r['title']}")
        t.setItem(row, 2, ti)
        mi = QTableWidgetItem(str(r["mid"]))
        mi.setData(Qt.ItemDataRole.UserRole, r["mid"])
        t.setItem(row, 3, mi)
        ci = QTableWidgetItem(r["cat"])
        ci.setForeground(QBrush(QColor(
            _CAT_COLORS.get(r["cat"], _C_MUTED))))
        t.setItem(row, 4, ci)
        ni = QTableWidgetItem(r["note"])
        ni.setToolTip(r["note"])
        t.setItem(row, 5, ni)

    def _b4_row_at(self, row: int) -> dict | None:
        t = self._b4_table
        if t is None:
            return None
        mi = t.item(row, 3)
        if mi is None:
            return None
        mid = mi.data(Qt.ItemDataRole.UserRole)
        if mid is None:
            return None
        return next((r for r in self._b4_rows if r["mid"] == mid), None)

    def _on_b4_menu(self, pos) -> None:
        t = self._b4_table
        if t is None:
            return
        item = t.itemAt(pos)
        if item is None:
            return
        r = self._b4_row_at(item.row())
        if r is None:
            return   # 分组标题行等：不弹菜单
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        self._fill_row_menu(
            menu, r["mid"],
            allow_cmd=r["can_delete"],
            allow_replace=r["can_replace"],
            allow_delete=r["can_delete"],
            replaced_by=r["replaced_by"])
        menu.exec(t.viewport().mapToGlobal(pos))

    def _on_b4_double(self, row: int, _col: int) -> None:
        r = self._b4_row_at(row)
        if r is not None:
            self._open_workshop(r["mid"])

    def _on_b4_check(self, mid: int, on: bool) -> None:
        (self._b4_checked.add if on else self._b4_checked.discard)(mid)
        n = len(self._b4_checked)
        if self._b4_batch_btn is not None:
            self._b4_batch_btn.setText(
                f"对勾选项({n}) ▾" if n else "对勾选项 ▾")

    def _b4_checked_rows(self) -> list[dict]:
        mids = set(self._b4_checked)
        return sorted((r for r in self._b4_rows if r["mid"] in mids),
                      key=lambda r: r["mid"])

    def _b4_batch_open_pages(self) -> None:
        rows = self._b4_checked_rows()
        if not rows:
            QMessageBox.information(self, "批量打开工坊页面",
                                    "先勾选表格里的行。")
            return
        ret = QMessageBox.question(
            self, "批量打开工坊页面",
            f"在浏览器打开 {len(rows)} 个工坊页面？每个一页标签。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            self._open_workshop(r["mid"])

    def _b4_batch_open_dirs(self) -> None:
        rows = self._b4_checked_rows()
        if not rows:
            QMessageBox.information(self, "批量打开文件夹",
                                    "先勾选表格里的行。")
            return
        for r in rows:
            self._open_folder(r["mid"])

    def _b4_batch_purge(self) -> None:
        rows = [r for r in self._b4_checked_rows() if r["cat"] == "已删除"]
        skipped = len(self._b4_checked) - len(rows)
        if not rows:
            QMessageBox.information(
                self, "批量彻底清账",
                "勾选里没有可清账的条目（只有「已删除」分类的行支持——"
                "失效/已替换/已清账的行不能在这里清）。")
            return
        preview = "\n".join(f"· {r['mid']} {r['title']}"
                            for r in rows[:20]) \
                  + ("\n…" if len(rows) > 20 else "")
        tail = (f"\n\n另有 {skipped} 个勾选行不可清账，将自动跳过。"
                if skipped else "")
        box = QMessageBox(self)
        box.setWindowTitle("批量彻底清账")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"把以下 {len(rows)} 条记录从账本里彻底清除？\n{preview}{tail}\n\n"
            "· 记录、版本快照、特别关注提醒：物理删除，不可恢复；\n"
            "· 自动登记「已清账」黑名单：扫描不再录入（含 steamcmd 复活）；\n"
            "· 磁盘一个字节不动；\n"
            "· 名下有备份登记的条目会被跳过（勾选下方复选框才连带删登记，"
            "磁盘备份文件夹仍保留）。")
        cb = QCheckBox("同时删除备份登记（磁盘上的备份文件夹仍保留）", box)
        box.setCheckBox(cb)
        b_no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        b_yes = box.addButton("彻底清账",
                              QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(b_no)   # 默认永远保守
        box.exec()
        if box.clickedButton() is not b_yes:
            self._log.info("已取消批量彻底清账：没有做任何改动")
            return
        done = 0
        blocked = 0
        for r in rows:
            try:
                self._repo.purge_mod(r["mid"], purge_backups=cb.isChecked())
                done += 1
            except ValueError:
                blocked += 1
        self._log.warn(
            f"批量彻底清账完成：{done} 条记录物理删除、已登记黑名单"
            + (f"；{blocked} 条被跳过（名下有备份登记未一并删除——到"
               "【备份总览】页处置登记后再来）" if blocked else ""))
        self._refresh_after_dispose()

    def _b4_batch_soft_delete(self) -> None:
        rows = [r for r in self._b4_checked_rows() if r["can_delete"]]
        skipped = len(self._b4_checked) - len(rows)
        if not rows:
            QMessageBox.information(
                self, "批量软删除",
                "勾选里没有可处置的条目（已替换/已删除的行不能再删）。")
            return
        preview = "\n".join(f"· {r['mid']} {r['title']}"
                            for r in rows[:20]) \
                  + ("\n…" if len(rows) > 20 else "")
        tail = (f"\n\n另有 {skipped} 个勾选行不可处置（已替换/已删除），"
                "将自动跳过。") if skipped else ""
        ret = QMessageBox.question(
            self, "批量软删除",
            f"把以下 {len(rows)} 条失效记录整批软删除？\n{preview}{tail}\n\n"
            "· 记录保留（含删除前快照），可到【mod 库】页右键「恢复」；\n"
            "· 盘上文件不动。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        done = 0
        for r in rows:
            mod = self._repo.get_mod(r["mid"])
            if mod is None:
                continue
            try:
                self._repo.mark_deleted(mod.mod_id,
                                        self._deleted_last_state(mod))
                done += 1
            except ValueError as exc:
                self._log.error(f"软删除失败（mod {mod.mod_id}）：{exc}")
        self._log.warn(f"批量软删除完成：{done}/{len(rows)} 条"
                       "（记录保留可恢复，盘上文件未动）")
        self._refresh_after_dispose()

    # ---------- 编号清单与行菜单（各桶共用）----------
    def _add_id_list(self, card: _BucketCard,
                     entries: list[tuple[int, str, str]], *,
                     allow_cmd: bool,
                     allow_dispose: bool = False) -> None:
        lw = QListWidget(card)
        lw.setWordWrap(False)
        lw.setAlternatingRowColors(True)
        lw.setFixedHeight(min(240, max(100, 24 * len(entries) + 12)))
        for mid, title, note in entries:
            text = f"{mid} {title}"
            if note:
                text += f" · {note}"
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, mid)
            lw.addItem(it)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lw.customContextMenuRequested.connect(
            lambda pos, w=lw: self._on_id_list_menu(w, pos, allow_cmd,
                                                    allow_dispose))
        self._id_lists.append(lw)
        card.add_widget(lw)

    def _on_id_list_menu(self, lw: QListWidget, pos, allow_cmd: bool,
                         allow_dispose: bool = False) -> None:
        it = lw.itemAt(pos)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        self._fill_row_menu(menu, mid, allow_cmd=allow_cmd,
                            allow_replace=allow_dispose,
                            allow_delete=allow_dispose)
        menu.exec(lw.mapToGlobal(pos))

    def _fill_row_menu(self, menu: QMenu, mid: int, *,
                       allow_cmd: bool = False,
                       allow_replace: bool = False,
                       allow_delete: bool = False,
                       replaced_by: int | None = None) -> None:
        """行菜单装动作的单一出口（口径不漂移）。查看类在上、处置类
        在下，中间加分隔线。"""
        act = QAction("打开工坊页面", menu)
        act.setToolTip(
            "在浏览器打开该 mod 的创意工坊页面"
            "（失效条目也能打开，可看作者是否留了续作/说明；"
            "网页能打开的大概率还能下载）")
        act.triggered.connect(lambda: self._open_workshop(mid))
        menu.addAction(act)
        if replaced_by:
            act = QAction(f"打开替代 mod（{replaced_by}）页面", menu)
            act.setToolTip("在浏览器打开替换目标 mod 的创意工坊页面")
            act.triggered.connect(lambda: self._open_workshop(replaced_by))
            menu.addAction(act)
        act = QAction("打开 mod 文件夹", menu)
        act.setToolTip("在文件管理器打开该 mod 的下载内容文件夹")
        act.triggered.connect(lambda: self._open_folder(mid))
        menu.addAction(act)
        act = QAction("复制编号", menu)
        act.setToolTip("把这个 mod 的工坊编号复制到剪贴板")
        act.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act)
        if allow_cmd or allow_replace or allow_delete:
            menu.addSeparator()
        if allow_cmd:
            act = QAction("跳到命令生成页（只勾这条）…", menu)
            act.setToolTip(
                "跳到【下载命令生成】页并只勾选这个编号，生成下载"
                "命令。result=9 的条目有时仍能下载成功（接口看不见"
                "≠内容服务器没有）——值得一试；下载成功后到"
                "【入账中心】点【扫描游戏目录】确认入账")
            act.triggered.connect(lambda: self._emit_command_gen([mid]))
            menu.addAction(act)
        if allow_delete:
            act = QAction("软删除此记录…", menu)
            act.setToolTip(
                "账本记为已删除（记录保留可恢复，盘上文件不动）。"
                "三步主路径的最后一步：找续作 → 入账中心登记 →"
                "删旧记录。建议先试一次重下（result=9 偶尔仍能"
                "下载成功）再决定")
            act.triggered.connect(lambda: self._soft_delete_failed(mid))
            menu.addAction(act)
        if allow_replace:
            act = QAction("关联替换…（进阶）", menu)
            act.setToolTip(
                "把这条记录的备注/颜色标记/特别关注一键带给一个"
                "已在库的替代条目，归档记「旧 → 新」证据链。\n"
                "需要该条目有失效归档（由【更新检测】对 result=9 建立）；"
                "没有归档时软件会弹窗说明原因")
            act.triggered.connect(lambda: self._replace_failed(mid))
            menu.addAction(act)
        elif allow_delete:
            # 确定无归档：置灰 + 悬停说明
            act = QAction("关联替换…（进阶）", menu)
            act.setEnabled(False)
            act.setToolTip(
                "这一行没有失效归档，暂不能关联替换——失效归档由"
                "【更新检测】在确认条目失效（result=9）时建立，"
                "本行目前还没有。\n"
                "直接走三步主路径即可：找续作 → 入账中心登记 →"
                "软删除旧记录")
            menu.addAction(act)

    def _open_workshop(self, mid: int) -> None:
        # 网址走唯一入口 workshop_url：存了用存的、没存按编号现拼
        # （失效条目也能开——作者可能留了续作说明）。此前引用的
        # constants.WORKSHOP_URL_TEMPLATE 已删，再调用就是 AttributeError。
        # 顺手修：原函数在 if 块后面有一条无条件 return，把"已请求
        # 打开"的日志行变成永远到不了的死代码——现在改为只在
        # 打开失败时提前返回，成功照常记日志
        m = self._mods_by_id.get(mid)
        url = workshop_url(mid, m.url if m is not None else None)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(
                self, "打开页面", f"浏览器没有响应，请手动打开：\n{url}")
            return
        self._log.info(f"已请求浏览器打开 mod {mid} 的工坊页面")

    def _copy_id(self, mid: int) -> None:
        QApplication.clipboard().setText(str(mid))
        self._log.info(f"已复制编号 {mid}")

    def _open_folder(self, mid: int) -> None:
        if self._owner_game is None:
            QMessageBox.information(self, "打开 mod 文件夹",
                                    "请先点【开始检测】再使用此动作。")
            return
        m = self._mods_by_id.get(mid)
        if m is None:
            QMessageBox.information(self, "打开 mod 文件夹",
                                    f"账本里没有 mod {mid} 的记录。")
            return
        open_mod_folder(self, self._owner_game, m, log=self._log)

    def _apply_item_filter(self) -> None:
        kw = self._filter_edit.text().strip().casefold()
        for lw in self._id_lists:
            for i in range(lw.count()):
                it = lw.item(i)
                it.setHidden(bool(kw)
                             and kw not in it.text().casefold())
        self._rebuild_b4_table()

    @staticmethod
    def _deleted_last_state(mod) -> dict:
        """软删除前快照组装（repo 契约：调用方组装）。V2 字段：确认
        版本与来源顶替旧版的 acf 本地版本/manifest/local_path——
        与核验页、清理页同一份口径。"""
        return {"title": mod.title, "url": mod.url,
                "time_updated": mod.time_updated,
                "confirmed_version": mod.confirmed_version,
                "confirmed_source": mod.confirmed_source,
                "local_size": mod.local_size, "note": mod.note,
                "color_tag": mod.color_tag, "is_special": mod.is_special}

    # ---------- 桶④处置一：软删除（主路径）----------
    def _soft_delete_failed(self, mid: int) -> None:
        mod = self._repo.get_mod(mid)
        if mod is None:
            self._log.error(
                f"软删除失败（mod {mid}）：账本中已找不到该条目")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mid} 不在账本中，无法软删除。\n"
                "请重新点【开始检测】后再试。")
            return
        ret = QMessageBox.question(
            self, "软删除失效记录",
            f"确定将失效的「{mod.title or mid}」（mod {mid}）的"
            "账本记录软删除？\n\n"
            "· 记录保留（含删除前快照），可到【mod 库】页右键「恢复」；\n"
            "· 盘上文件不动——想清文件去【清理与删除】页"
            "（失效条目在那里正好只给「仅删文件」）；\n"
            "· 本工具不再为它生成下载命令。\n\n"
            "提示：result=9 的条目偶尔仍能下载成功——若还没试过"
            "重下，可先到【下载命令生成】页试一次再决定。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)   # 默认否
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.mark_deleted(mid, self._deleted_last_state(mod))
        except ValueError as exc:
            self._log.error(f"软删除失败（mod {mid}）：{exc}")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mid} 软删除失败：{exc}\n请重新点【开始检测】后再试。")
            return
        self._log.warn(
            f"mod {mid}（失效）已软删除：记录保留可恢复，"
            "盘上文件未动；复检后该行会变为「已删除」分类")
        self._refresh_after_dispose()

    # ---------- 桶④处置二：关联替换（进阶）----------
    def _replace_failed(self, old_id: int) -> None:
        # 编号输入不用 getInt——封顶 2³¹-1，工坊编号可到约 43 亿
        text, ok = QInputDialog.getText(
            self, "关联替换",
            f"mod {old_id} 已失效。\n输入替代 mod 的工坊编号（纯数字）：")
        if not ok:
            return
        try:
            new_id = int(text.strip())
        except ValueError:
            QMessageBox.warning(self, "关联替换", "编号必须是纯数字。")
            return
        if new_id <= 0 or new_id == old_id:
            QMessageBox.warning(
                self, "关联替换",
                "编号无效（需为正整数，且不能与失效编号相同）。")
            return
        mod = self._repo.get_mod(new_id)
        if mod is None:
            QMessageBox.warning(
                self, "关联替换",
                f"编号 {new_id} 不在账本中。\n请先到【入账中心 · 登记】"
                "或扫描入账，再回来替换。")
            return
        ret = QMessageBox.question(
            self, "关联替换",
            f"把 mod {old_id} 的失效记录替换为 mod {new_id}"
            f"「{mod.title or ''}」？\n\n"
            "将迁移整理成果：备注、颜色标记、特别关注；\n"
            "归档中记下「旧 → 新」证据链；\n"
            "旧 mod 保留失效状态，作为历史记录。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.replace_failed_mod(old_id, new_id)
        except ValueError as exc:
            self._log.error(
                f"关联替换失败（mod {old_id} → {new_id}）：{exc}")
            QMessageBox.critical(self, "关联替换失败", str(exc))
            return
        self._log.ok(
            f"已关联替换：mod {old_id} → mod {new_id}"
            "（旧记录保留为失效归档；复检后该行会变为「已替换」分类）")
        self._refresh_after_dispose()

    # ---------- 跳转命令生成页 ----------
    def _emit_command_gen(self, ids: list[int]) -> None:
        self._log.info(
            f"跳转命令生成页并勾选 {len(ids)} 个编号（异常修复重下）")
        self.command_gen_requested.emit(list(ids))


def _ids_text(ids: list[int], limit: int = _ID_LIMIT) -> str:
    head = "、".join(str(i) for i in ids[:limit])
    if len(ids) > limit:
        head += f" …（共 {len(ids)} 个）"
    return head
