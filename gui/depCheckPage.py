"""依赖检测
"""
from core.urlParser import workshop_url

r"""gui/depCheckPage.py · 桶B 依赖检测（D24 拆分页之一）。

必需物品清单由带 key 的官方接口拉取（key 现场粘贴、用完即弃——
本页不保存）：清单里编号不在账本 = 缺依赖（红）；与上次拉取有差 =
依赖变化（黄，以最新为准）。拉取成功后依赖边入账（replace_
dependencies，本页唯一的写库动作；children 入账与判定同口径：num_children=0 的无依赖条目照写空清单（基线可比）；说有依赖却拿不到清单的不写——不冒充"无依赖"
——
没拿到的东西不冒充"无依赖"）。
netGate「依赖拉取」闸（constants.NET_GATE_DEPENDENCIES）；
RemoteQueryWorker + keyed 查询闭包。
"""
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QInputDialog, QProgressBar, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem,
    QLineEdit,
)

from core import constants, netGate   # ★ 待核：netGate 若在 gui 包改这行
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.logBus import LogBus
from gui.remoteQueryWorker import RemoteQueryWorker, keyed_item_to_entry
from workflows import exceptionFlow
from gui.theme import font_px  # 字号单源（D25）
from PySide6.QtGui import QAction, QColor, QDesktopServices
from core.formatters import abs_time, fmt_size, relative_time
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from gui.tableSort import SortItem  # 表头数值排序（单源共享件）
from gui.collapsibleSection import make_guide  # 先读我单源（共享件内）

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"
_C_MUTED = "#8a8a8f"


class DepCheckPage(QWidget):
    """桶B：缺依赖 / 依赖变化。拉取成功才写库，停止 = 什么都没发生。"""
    # 「送登记」→ 主窗口转接入账中心登记区（预填+解析预览，不写库）。
    # 跨件传编号清单用 list（踩坑 79：Signal(int) 会 Overflow）
    send_to_register_requested = Signal(list)

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()
        self._game: Game | None = None
        self._worker = None
        self._meta_worker = None   # 缺失依赖远端信息查询轮（匿名接口）
        self._missing_now: dict[int, list[int]] = {}
        self._dep_meta: dict[int, dict] = {}  # 补查回执（内存，不入账）

        self._mods_by_id: dict[int, object] = {}
        self._owner_game: Game | None = None
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

        title = QLabel("依赖检测", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        root.addWidget(make_guide(
            "先读我：key 现场粘贴用完即弃 · 缺依赖 / 依赖变化的含义",
            ("mod 的「必需物品」清单由带 key 的官方接口拉取（key 现场"
             "粘贴、用完即弃，注册方法见【Steam API 密钥】页）。",
             "清单里编号不在账本 = 缺依赖（红，一键复制编号到"
             "【入账中心 · 登记】补进账本）；与上次拉取有差 = 依赖变化"
             "（黄，作者增删了依赖，以最新为准）。"),
            self, 130))

        # key 管理页直链：已申请过的用户点开就能查看/复制自己的 key，
        # 不用再去【Steam API 密钥】页翻注册步骤
        key_link = QLabel(
            '<a href="https://steamcommunity.com/dev/apikey">'
            "已申请过 key？点这里打开 Steam 官方 key 页面"
            "（登录后可见并可复制）</a>", self)
        key_link.setOpenExternalLinks(True)  # 没有这行 <a> 只是字、点不动
        root.addWidget(key_link)

        self.root_layout = root   # _make_section 与复制按钮的挂载点

        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn = QPushButton("拉取依赖（联网）", row)
        self._btn.setToolTip(
            "带 key 批量拉取全部未删除 mod 的必需物品清单：缺什么、"
            "变了什么一次说清。拉取时会请你粘贴 key（本软件不保存）；"
            "与其它联网功能共用互斥闸")
        self._btn.clicked.connect(self._start_fetch)
        h.addWidget(self._btn)
        self._stop_btn = QPushButton("停止", row)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_fetch)
        h.addWidget(self._stop_btn)
        self._btn_offline = QPushButton("离线重算总览", row)
        self._btn_offline.setToolTip(
            "不联网：按账本里入过账的依赖清单 × 账本编号集，现算谁缺"
            "依赖（毫秒级、与拉取判定同一把尺子）。上次拉取之后账本"
            "有增删，点它刷新总览")
        self._btn_offline.clicked.connect(self._recompute_missing_offline)
        h.addWidget(self._btn_offline)

        self._progress = QProgressBar(row)
        self._progress.setVisible(False)
        h.addWidget(self._progress, 1)
        root.addWidget(row)
        # 页面级查找（备份页同款）：缺依赖表 / 明细表 / 依赖变化列表
        # 三处联动——只隐藏不清数据，清空恢复；勾选不受影响（查找是
        # 视图帮手，不是数据闸门）
        srow = QWidget(body)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel("查找：", srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            "按标题 / 编号过滤缺依赖、明细、依赖变化（即时，只隐藏；"
            "清空恢复）…")
        self._filter_edit.textChanged.connect(
            lambda _t: self._filter_timer.start())
        sh.addWidget(self._filter_edit, 1)
        root.addWidget(srow)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self._apply_filter)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)
        # —— 缺依赖表（备份页同款表格化 + 勾选列）：黏一行分不清的
        # 教训——编号/标题/缺数/缺失依赖各占一列；勾选列 = 批量动作
        # 的唯一对象来源（备份页 COL_CHECK 同款：勾选记编号不记行号）。
        # 双击行 = 打开该 mod 的工坊页面
        self._miss_head = QLabel(
            "缺依赖（必需物品编号不在账本）——"
            "点按钮复制/送登记；右键更多动作；双击行开工坊页面", self)
        self._miss_head.setWordWrap(True)
        self._miss_head.setStyleSheet(f"color: {_C_FAIL};")
        self._miss_head.setVisible(False)
        self.root_layout.addWidget(self._miss_head)
        self._miss_table = QTableWidget(0, 5, self)
        self._miss_table.setHorizontalHeaderLabels(
            ["选", "编号", "标题", "缺数", "缺失依赖（必需物品）"])
        self._miss_table.verticalHeader().setVisible(False)
        self._miss_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._miss_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._miss_table.setWordWrap(False)
        _mh2 = self._miss_table.horizontalHeader()
        _mh2.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        _mh2.setSectionsClickable(True)  # 表头可点排序（原 False 关死）

        for col, width in ((0, 34), (1, 110), (3, 50), (4, 280)):
            self._miss_table.setColumnWidth(col, width)
        self._miss_table.itemDoubleClicked.connect(self._on_miss_dclicked)
        self._miss_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._miss_table.customContextMenuRequested.connect(
            self._on_miss_menu)
        self._miss_table.setVisible(False)
        self.root_layout.addWidget(self._miss_table)

        # —— 缺失依赖明细（⑫）：一行一个缺失的依赖编号 ——
        # 缺失清单是"上家视角"（谁缺），本表是"依赖视角"（缺的啥）。
        # 账外编号远端状态本地无从得知：点【查远端信息】补查后
        # 蓝=在架 / 灰=查无；查无此条是 Steam 的正常回答，不是错误。
        # 与拉取判定同尺：missing 全是账外编号（账内已删除/失效由
        # 异常处置页桶③④管，本页不重复报）
        self._dep_detail_head = QLabel(
            "缺失依赖明细（蓝=远端在架｜灰=账外未查/远端查无；"
            "双击行开工坊网页）", self)
        self._dep_detail_head.setWordWrap(True)
        self._dep_detail_head.setVisible(False)
        self.root_layout.addWidget(self._dep_detail_head)
        self._dep_detail = QTableWidget(0, 7, self)
        self._dep_detail.setHorizontalHeaderLabels(
            ["选", "编号", "标题", "大小", "远端更新", "状态", "被谁依赖"])
        self._dep_detail.verticalHeader().setVisible(False)
        self._dep_detail.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._dep_detail.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._dep_detail.setWordWrap(False)
        _mh = self._dep_detail.horizontalHeader()
        _mh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        _mh.setSectionsClickable(True)  # 表头可点排序
        for col, width in ((0, 34), (1, 100), (3, 80), (4, 100),
                           (5, 130), (6, 220)):
            self._dep_detail.setColumnWidth(col, width)
        self._dep_detail.itemDoubleClicked.connect(
            self._on_detail_dclicked)
        self._dep_detail.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._dep_detail.customContextMenuRequested.connect(
            self._on_dep_menu)
        self._dep_detail.setVisible(False)
        self.root_layout.addWidget(self._dep_detail)

        # 表头点击排序（SortItem 机构）：排序搬条目、行隐藏挂行号
        # ——排完重滤一次对齐（两表同一落点）
        self._miss_table.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_a: self._apply_filter())
        self._dep_detail.horizontalHeader().sortIndicatorChanged.connect(
            lambda *_a: self._apply_filter())

        self._sec_changed = self._make_section(
            "依赖变化（与上次拉取相比，作者增删了必需物品）",
            "清单已按本次为准入账，对照看即可。", _C_WARN)

        self._note = QLabel("", self)
        self._note.setWordWrap(True)
        root.addWidget(self._note)
        root.addStretch(1)

    def _make_section(self, heading: str, note: str,
                      color: str) -> tuple[QLabel, QListWidget]:
        lbl = QLabel(heading + "——" + note, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {color};")
        self.root_layout.addWidget(lbl)
        lw = QListWidget(self)
        lw.setWordWrap(False)
        lw.setAlternatingRowColors(True)
        lw.setFixedHeight(200)
        lw.setVisible(False)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lw.customContextMenuRequested.connect(
            lambda pos, w=lw: self._on_row_menu(w, pos))
        self.root_layout.addWidget(lw)
        return lbl, lw

    # ---------- 对外 ----------
    def set_game(self, game: Game | None) -> None:
        if self._worker is not None:
            # 拉取进行中：只更新标签，不动结果区——结果仍归属开始
            # 拉取时的档案（基线/清单开工时已捕获，不串）。但
            # self._game 必须同步跟上：否则本次拉取收尾后，下一次
            # 动作会拿旧档案当新档案跑（修24）。
            self._game = game
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）"
                if game is not None else "当前游戏：（未选择）")
            return
        self._game = game
        self._clear_results()
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._btn.setEnabled(True)
            self._recompute_missing_offline()
    def shutdown(self) -> None:
        """关窗收尾：拉取轮与远端信息轮两路 worker 一并请停（等 1.5
        秒，等不到 park 保活——MainWindow 的页面循环自动发现并调用）。"""
        for name in ("_worker", "_meta_worker"):
            w = getattr(self, name, None)
            if w is not None:
                w.stop()
                if not w.wait(1500):
                    netGate.park(w)
                setattr(self, name, None)


    # ---------- 拉取 ----------
    def _start_fetch(self) -> None:
        if self._game is None or self._worker is not None:
            return
        # key 用完即弃：不落盘、不进日志、不进会话缓存——每次拉取
        # 现场粘贴；取消或空 = 什么都不发生
        key, ok = QInputDialog.getText(
            self, "拉取依赖",
            "粘贴 Steam Web API key（本软件不保存，用完即弃）：\n"
            "还没有 key？【Steam API 密钥】页有四步注册指引。")
        if not ok:
            return
        key = key.strip()
        if not key:
            QMessageBox.information(self, "拉取依赖",
                                    "key 为空：本次没有做任何事。")
            return
        mods = self._repo.list_mods(self._game.app_id)
        self._mods_by_id = {m.mod_id: m for m in mods}
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn("当前档案没有可拉取依赖的 mod（已删除的除外）")
            return
        # 拉取前基线（GUI 线程读库）：旧边 + 是否首拉 + 账本全集。
        # 判定用的三样数据在这里取齐，线程回来直接算
        self._lib_ids = {m.mod_id for m in mods}
        self._baseline = {m.mod_id: self._repo.list_dependencies(m.mod_id)
                          for m in mods}
        self._first_fetch = self._repo.latest_dependency_fetch() is None
        busy = netGate.try_acquire(constants.NET_GATE_DEPENDENCIES)
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再拉取。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(f"依赖拉取未开始：{busy} 正在使用联网查询")
            return
        self._owner_game = self._game
        self._clear_results()
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        interval = self._settings.get_int("api_request_interval_ms", 200)
        retries = self._settings.get_int("api_max_retries", 3)

        def make_query(cancel_event):
            client = SteamApiClient(interval_ms=interval,
                                    max_retries=retries,
                                    cancel_event=cancel_event)
            return lambda batch: client.query_details_keyed(batch, key)

        self._worker = RemoteQueryWorker(
            ids, make_query=make_query, to_entry=keyed_item_to_entry,
            interval_ms=interval, max_retries=retries, parent=self)
        self._worker.batch_done.connect(self._progress.setValue)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()
        self._log.info(f"依赖拉取开始：{len(ids)} 个 mod（keyed 接口，"
                       "含必需物品清单）…")

    def _stop_fetch(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._stop_btn.setEnabled(False)
            self._summary.setText(
                "已请求停止依赖拉取：等待与重试已中断，最多再等一次在途"
                "请求收场——本次结果未使用、依赖表未动。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止依赖拉取")

    # ---------- 回调 ----------
    def _on_succeeded(self, entries: list) -> None:
        self._set_running(False)
        # ① 判定（纯函数；此刻库还没动过——基线就是拉取前那份）
        deps = exceptionFlow.classify_dependencies(
            entries, self._lib_ids, self._baseline,
            first_fetch=self._first_fetch)
        # ② 依赖边入账（本页唯一的写库动作）。入账与判定用同一把尺子
        # （num_children 仲裁，2026-10 探针实测：Steam 对零依赖条目
        # 省略空清单不回键，但 num_children 照发）：
        # children 是清单（可为空）→ 照写，空清单也写——记录
        # "已核实无依赖"，下次拉取才有基线可比，不会永远停在未判定；
        # children 键缺席且 num_children=0 → Steam 官方盖章的零依赖，
        # 同样写空清单；
        # 说有依赖却拿不到清单（num_children>0）或数据不全 → 继续绝不
        # 写——没拿到的东西不冒充"无依赖"，保守桶不污染账本。
        # 逐条原子，中途失败下次拉取全量覆盖
        written = 0
        for e in entries:
            children = e.get("children")
            if children is None:
                if e.get("num_children") == 0:
                    children = []
                else:
                    continue

            try:
                self._repo.replace_dependencies(e["mod_id"], children)
                written += 1
            except ValueError as exc:
                self._log.error(
                    f"依赖入账失败（mod {e.get('mod_id')}）：{exc}")
        # ②′ 描述原文顺手落库（刀1，2026-10）：keyed 回执里的
        # description（BBCode 全文）随依赖拉取进 mods 表——零额外请求，
        # 与更新检测同为零成本正门。时间戳用账内 mod 的 time_updated
        # （keyed 接口不回 time_updated；账内条目大多检测过有值，
        # 没检测过的留 NULL——显示侧 None 比对不误报）。拉取期间
        # 条目被处置 → update_api_metadata 报不存在，跳过不拦流程。
        # 缺失依赖的匿名补查（_on_meta_succeeded）刻意不入库：
        # 账外编号维持"回执只进显示"的既有豁免。
        n_desc = 0
        for e in entries:
            desc = (e.get("description") or "").strip() or None
            if not desc or e.get("mod_id") is None:
                continue
            mod = self._mods_by_id.get(int(e["mod_id"]))
            try:
                self._repo.update_api_metadata(
                    int(e["mod_id"]), description=desc,
                    description_ts=(mod.time_updated
                                    if mod is not None else None))
                n_desc += 1
            except ValueError:
                pass  # 条目已被处置：描述下次检测再补
        # ③ 渲染
        self._render_missing(deps.missing, offline=False)
        chg_rows = []
        for mid, old, new in deps.changed:
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            note = (f"{len(old)} → {len(new)}"
                    + (f"，新增 {self._ids_text(added)}" if added else "")
                    + (f"，移除 {self._ids_text(removed)}" if removed else ""))
            chg_rows.append((mid, self._title_of(mid), note))
        self._fill_list(self._sec_changed[1], chg_rows)
        n_bad = len(deps.missing) + len(deps.changed)
        if n_bad == 0:
            if self._first_fetch:
                # 首次拉取 = 建基线，不是"对比过没变化"——话说清
                self._summary.setText(
                    f"首次拉取完成：依赖基线已建立并入账"
                    f"（{written} 条清单，其中无依赖 "
                    f"{len(deps.childless)} 条，其余依赖全部在"
                    "账本）。之后作者增删依赖，这里才会报"
                    "「依赖变化」。")
                self._summary.setStyleSheet(f"color: {_C_MUTED};")
            else:
                self._summary.setText(
                    f"无异常（解析 {deps.fetched} 条：无依赖 "
                    f"{len(deps.childless)} 条，其余依赖全部在账本）。")
                self._summary.setStyleSheet(f"color: {_C_OK};")
        else:
            head = ("首次拉取（本次结果已作为基线入账，缺依赖照常要补）：\n"
                    if self._first_fetch else "")
            stat = (f"缺依赖 {len(deps.missing)} · "
                    f"依赖变化 {len(deps.changed)}")
            if deps.undetermined:
                stat += f" · 未判定 {len(deps.undetermined)}"
            self._summary.setText(head + stat)
            self._summary.setStyleSheet(f"color: {_C_WARN};")
        # 补充说明行
        last_fetch = self._repo.latest_dependency_fetch()
        notes = []
        if deps.undetermined:
            notes.append(
                f"未判定 {len(deps.undetermined)} 条（Steam 说这些条目"
                "有依赖，但清单没随响应给出，或数据不全）——保守起见"
                "本次不入账、不冒充「无依赖」。重拉一次；持续出现把"
                "日志发给开发者。" + self._ids_text(deps.undetermined))
        if deps.skipped:
            notes.append(f"另有 {deps.skipped} 条远端失效/查询失败，"
                         "无从判依赖（见【异常处置】页桶④⑤）。")
        notes.append(
            "依赖清单以最近一次拉取为准（重拉全量刷新）；上次拉取："
            + (abs_time(last_fetch) if last_fetch else "（本次是第一次）")
            + "。依赖指向的条目虽在账本但已失效/删除的，去【异常处置】页"
              "处置——本页不重复报。")
        self._note.setText("\n".join(notes))
        self._log.ok(
            "依赖拉取完成：" + f"{written} 个 mod 清单入账"
            + (f"，描述落库 {n_desc} 条" if n_desc else "")
            + (f"，缺依赖 {len(deps.missing)} 个 mod" if deps.missing else "")
            + (f"，依赖变化 {len(deps.changed)}" if deps.changed else "")
            + (f"，未判定 {len(deps.undetermined)}"
               if deps.undetermined else ""))

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        text = message
        if "403" in message:
            text = ("服务器拒绝（403）：key 无效或已被重置——重新拉取并"
                    "粘贴正确的 key（注册方法见【Steam API 密钥】页）")
        self._summary.setText(
            f"依赖拉取失败：{text}\n（依赖表未动，稍后可重试。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"依赖拉取失败：{message}")

    def _on_stopped(self) -> None:
        self._set_running(False)
        self._summary.setText("已停止依赖拉取，本次结果未使用（依赖表未动）。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("依赖拉取已手动停止")

    def _on_finished(self) -> None:
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_DEPENDENCIES)

    # ---------- 小件 ----------
    def _set_running(self, running: bool) -> None:
        self._btn.setEnabled(not running and self._game is not None)
        self._stop_btn.setEnabled(running)
        self._progress.setVisible(running)
    def _clear_results(self) -> None:
        self._miss_head.setVisible(False)
        self._miss_table.setRowCount(0)
        self._miss_table.setVisible(False)
        self._filter_edit.clear()

        self._sec_changed[1].clear()
        self._sec_changed[1].setVisible(False)
        self._summary.setText("")
        self._note.setText("")
        self._remove_copy_row()
        self._missing_now = {}
        self._dep_meta = {}
        head = getattr(self, "_dep_detail_head", None)
        if head is not None:
            head.setVisible(False)
        t = getattr(self, "_dep_detail", None)
        if t is not None:
            t.setRowCount(0)
            t.setVisible(False)
    def _fill_list(self, lw: QListWidget, rows, *,
                   checkable: bool = False) -> None:
        lw.clear()
        for mid, title, note in rows:
            it = QListWidgetItem(f"{mid} {title} · {note}")
            it.setData(Qt.ItemDataRole.UserRole, mid)
            if checkable:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked)
            lw.addItem(it)
        lw.setVisible(bool(rows))

    # ---------- 缺依赖总览 / 明细 / 远端补查（⑫） ----------

    def _recompute_missing_offline(self) -> None:
        """离线重算缺依赖总览（零联网、毫秒级）：账本里入过账的依赖
        清单 × 账本编号集，谁家清单指向账外就报谁。与拉取判定
        （classify_dependencies）同一把尺子：missing 只含账外编号——
        依赖指向的编号虽在账本但已删除/失效的不报（异常处置页桶③④
        管，不重复报）。从未拉取的 mod 清单为空，天然不产生缺失。
        _mods_by_id 顺手自填：离线路径没有拉取轮的铺垫，_title_of
        照常可用。"""
        if self._game is None:
            return
        if self._worker is not None:
            self._log.info("依赖拉取进行中：离线重算等它结束后再来")
            return
        mods = self._repo.list_mods(self._game.app_id)
        self._mods_by_id = {m.mod_id: m for m in mods}
        lib_ids = {m.mod_id for m in mods}
        missing: dict[int, list[int]] = {}
        for m in mods:
            if m.status == "deleted":
                continue
            deps = self._repo.list_dependencies(m.mod_id)
            lack = sorted(d for d in deps
                          if d not in lib_ids and d != m.mod_id)
            if lack:
                missing[m.mod_id] = lack
        self._render_missing(missing, offline=True)
        self._summary.setText(
            f"离线总览（按上次拉取入账的清单现算，未联网）："
            f"缺依赖 {len(missing)} 个 mod。"
            + ("点【拉取依赖（联网）】刷新清单与判定。"
               if missing else
               "没有缺失——或依赖清单还没拉取过（先联网拉一次）。"))
        self._summary.setStyleSheet(f"color: {_C_MUTED};")
    def _render_missing(self, missing: dict[int, list[int]], *,
                        offline: bool = False) -> None:
        """缺依赖清单渲染（拉取完成 / 离线重算两路同走，口径一份）：
        行 = 一个缺依赖的 mod，勾选列 = 批量动作对象（勾选记编号不记
        行号——备份页铁律）；按钮组与明细表随行挂/灌，末尾重放查找。"""
        self._missing_now = {mid: list(lack)
                             for mid, lack in missing.items()}
        self._miss_head.setText(
            "缺依赖（必需物品编号不在账本）"
            + ("——按上次拉取入账的清单现算；联网重拉会刷新"
               if offline else ""))
        self._miss_head.setToolTip(
            "点按钮复制全部缺失编号/网址或送【入账中心 · 登记】；"
            "右键单条可打开工坊页面、复制缺失清单；先勾选若干行再"
            "右键可只复制/送登记勾选行的")
        rows = sorted(missing.items())
        t = self._miss_table
        t.setSortingEnabled(False)  # 带排序灌数据=边灌边排、行错位

        t.setRowCount(len(rows))
        for r, (mid, lack) in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)  # 默认全不勾
            chk.setData(Qt.ItemDataRole.UserRole, mid)
            c1 = SortItem(str(mid), mid)  # 排序键 = 编号数值
            c1.setData(Qt.ItemDataRole.UserRole, mid)
            title = self._title_of(mid)
            c4 = SortItem(self._ids_text(lack), tuple(lack))
            # 排序键 = 缺失编号元组（按首编号排，文本截断不影响）
            c4.setToolTip("全部缺失依赖编号：\n"
                          + "\n".join(str(i) for i in lack))
            for c, cell in enumerate((chk, c1,
                                      SortItem(title, title),
                                      SortItem(str(len(lack)), len(lack)),
                                      c4)):
                t.setItem(r, c, cell)
        t.setSortingEnabled(True)  # 灌完再开；点过的排法自动重放

        self._miss_head.setVisible(bool(rows))
        t.setVisible(bool(rows))
        self._ensure_copy_buttons(self._collected_missing_ids())
        self._fill_dep_detail(self._missing_now)
        self._apply_filter()

    def _collected_missing_ids(self) -> list[int]:
        """当前总览的全部缺失依赖编号（去重升序）——离线/实时两态
        同源（都走 _missing_now）。"""
        ids: set[int] = set()
        for lack in getattr(self, "_missing_now", {}).values():
            ids.update(lack)
        return sorted(ids)

    def _fill_dep_detail(self, missing: dict[int, list[int]]) -> None:
        """缺失依赖明细表：一行 = 一个缺失的依赖编号（并集去重——
        被多个 mod 依赖也只占一行，"被谁依赖"列合并展示、悬浮给
        全量）。状态判定：_dep_meta 有回执 → result=1 蓝「远端在架」、
        否则灰「远端查无」；没查过 → 灰「账外·未查」。列值用 SortItem：
        大小按字节、远端更新按时间戳、状态按 在架<查无<未查 的处置
        优先序。排序纪律：灌表期关排序、灌完再开、排后重滤。"""
        ids = self._collected_missing_ids()
        req: dict[int, list[int]] = {}
        for mid, lack in missing.items():
            for rid in lack:
                req.setdefault(rid, []).append(mid)
        meta = getattr(self, "_dep_meta", {})
        t = self._dep_detail
        t.setSortingEnabled(False)  # 带排序灌数据=边灌边排、行错位
        t.setRowCount(len(ids))
        for r, rid in enumerate(ids):
            it = meta.get(rid)
            if it is not None and it.get("result") == 1:
                title = it.get("title") or "—"
                size = fmt_size(it.get("file_size"))
                size_key = it.get("file_size") or 0
                upd = (relative_time(it["time_updated"])
                       if it.get("time_updated") else "—")
                upd_key = it.get("time_updated") or 0
                state, color, state_rank = "远端在架", QColor("#5aa9ff"), 0
            elif it is not None:
                title, size, upd = "—", "—", "—"
                size_key = upd_key = 0
                state, color, state_rank = ("远端查无（可能下架）",
                                            QColor("#8a8a8f"), 1)
            else:
                title, size, upd = "—", "—", "—"
                size_key = upd_key = 0
                state, color, state_rank = "账外·未查", QColor("#8a8a8f"), 2
            who = req.get(rid) or []
            names = [f"{self._title_of(mid)}（{mid}）" for mid in who]
            who_text = "、".join(names[:2]) + (f" 等 {len(who)} 个"
                                              if len(who) > 2 else "")
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)  # 默认全不勾
            chk.setData(Qt.ItemDataRole.UserRole, rid)
            c1 = SortItem(str(rid), rid)
            c1.setData(Qt.ItemDataRole.UserRole, rid)
            c6 = SortItem(who_text, who_text)
            if names:
                c6.setToolTip("被这些 mod 依赖：\n" + "\n".join(names))
            cells = (chk, c1, SortItem(title, title),
                     SortItem(size, size_key), SortItem(upd, upd_key),
                     SortItem(state, state_rank), c6)
            for c, cell in enumerate(cells):
                if c == 5:
                    cell.setForeground(color)
                t.setItem(r, c, cell)
        t.setSortingEnabled(True)  # 灌完再开；点过的排法自动重放
        self._dep_detail_head.setVisible(bool(ids))
        t.setVisible(bool(ids))
        self._apply_filter()  # 排序搬条目、隐藏挂行号——重灌后重滤对齐

    def _on_detail_dclicked(self, item) -> None:
        """明细行双击 = 开工坊网页（账内账外统一——网页总有用）。"""
        from PySide6.QtCore import QUrl

        it = self._dep_detail.item(item.row(), 1)
        rid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if rid:
            QDesktopServices.openUrl(QUrl(workshop_url(rid)))

    @staticmethod
    def _item_to_entry(it) -> dict:
        """匿名 WorkshopItem → 条目字典（RemoteQueryWorker 的
        to_entry 注入点；只留明细表消费的字段）。"""
        return {"mod_id": it.mod_id, "result": it.result,
                "title": it.title, "time_updated": it.time_updated,
                "file_size": it.file_size}

    def _on_fetch_meta(self) -> None:
        """【查远端信息】：对缺失依赖编号起一轮匿名批查
        （GetPublishedFileDetails，100/批无需 key；缺失的往往就几个，
        一轮负担得起）。复用 RemoteQueryWorker（分批/限流/取消全包）
        与 NET_GATE_DEPENDENCIES 一把闸（与拉取互斥）；结果只进内存
        _dep_meta，不入账——账外编号的归宿是【入账中心 · 登记】。"""
        ids = self._collected_missing_ids()
        if not ids:
            return
        if self._meta_worker is not None:
            QMessageBox.information(self, "查远端信息",
                                    "上一轮查询还在进行——等它结束。")
            return
        interval = self._settings.get_int("api_request_interval_ms", 200)
        retries = self._settings.get_int("api_max_retries", 3)
        busy = netGate.try_acquire(constants.NET_GATE_DEPENDENCIES)
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再查。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            return
        self._btn_meta.setEnabled(False)
        self._btn_meta.setText(f"正在查询（{len(ids)} 个）…")

        def make_query(cancel_event):
            client = SteamApiClient(interval_ms=interval,
                                    max_retries=retries,
                                    cancel_event=cancel_event)
            return lambda batch: client.query_details(batch)

        self._meta_worker = RemoteQueryWorker(
            ids, make_query=make_query, to_entry=self._item_to_entry,
            interval_ms=interval, max_retries=retries, parent=self)
        self._meta_worker.succeeded.connect(self._on_meta_succeeded)
        self._meta_worker.failed.connect(self._on_meta_failed)
        self._meta_worker.finished.connect(self._on_meta_finished)
        self._meta_worker.start()
        self._log.info(f"缺失依赖远端信息查询开始：{len(ids)} 个"
                       "（匿名接口，不写账本）…")

    def _on_meta_succeeded(self, entries: list) -> None:
        got = {e["mod_id"]: e for e in entries
               if e.get("mod_id") is not None}
        self._dep_meta.update(got)
        n_live = sum(1 for e in got.values() if e.get("result") == 1)
        self._log.ok(f"远端信息已补：{len(got)} 条（在架 {n_live}、"
                     f"查无 {len(got) - n_live}）——明细表已刷新")
        self._fill_dep_detail(getattr(self, "_missing_now", {}))

    def _on_meta_failed(self, message: str) -> None:
        self._summary.setText(
            f"远端信息查询失败：{message}\n（缺失清单不受影响，"
            "可稍后再点。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"缺失依赖远端信息查询失败：{message}")

    def _on_meta_finished(self) -> None:
        w = self._meta_worker
        self._meta_worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_DEPENDENCIES)
        btn = getattr(self, "_btn_meta", None)
        if btn is not None:  # 按钮可能已随清单重建/清场——守卫
            btn.setEnabled(True)
            btn.setText("查远端信息…")

    def _ensure_copy_buttons(self, all_missing: list[int]) -> None:
        """缺依赖非空时挂按钮组（复制编号/网址 + 查远端信息），
        离线重算与拉取完成两路共用；位置沿用原单按钮的插点
        （依赖变化区标题之前）。"""
        self._remove_copy_row()
        if not all_missing:
            return
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        b1 = QPushButton(f"复制全部缺失编号（{len(all_missing)} 个）…",
                         row)
        b1.setToolTip("复制到剪贴板（每行一个）：【入账中心 · 登记】"
                      "粘贴登记即可全部入库，之后正常下载")
        b1.clicked.connect(lambda _=False, ids=all_missing:
                           self._copy_missing(ids))
        b2 = QPushButton(f"复制缺失依赖工坊网址（{len(all_missing)} 条）…",
                         row)
        b2.setToolTip("每行一条工坊链接（按编号现拼，账外条目照样能出）"
                      "——可整段贴进【入账中心 · 登记】，或浏览器批量打开")
        b2.clicked.connect(lambda _=False, ids=all_missing:
                           self._copy_missing_urls(ids))
        self._btn_meta = QPushButton("查远端信息…", row)
        b3 = QPushButton("送登记…", row)
        b3.setToolTip(
            "把这批缺失依赖编号直接送【入账中心 · 登记】：预填粘贴框、"
            "自动解析预览（不写库）——核对五类分色结果后点"
            "【登记新编号】入账，闭环「缺失→待下载」")
        b3.clicked.connect(lambda _=False, ids=all_missing:
                           self.send_to_register_requested.emit(list(ids)))

        self._btn_meta.setToolTip(
            "对缺失编号做一轮匿名批量查询（无需 key、不写账本）："
            "明细表显示「远端在架」（蓝）或「远端查无」（灰）。\n"
            "在架的复制编号去【入账中心 · 登记】收录即闭环")
        self._btn_meta.clicked.connect(self._on_fetch_meta)
        h.addWidget(b1)
        h.addWidget(b2)
        h.addWidget(b3)
        h.addWidget(self._btn_meta)
        h.addStretch(1)
        self.root_layout.insertWidget(
            self.root_layout.indexOf(self._sec_changed[0]), row)
        self._copy_row = row

    def _remove_copy_row(self) -> None:
        row = getattr(self, "_copy_row", None)
        if row is not None:
            self.root_layout.removeWidget(row)
            row.deleteLater()
            self._copy_row = None

    def _copy_missing_urls(self, ids: list[int]) -> None:
        QApplication.clipboard().setText(
            "\n".join(workshop_url(i) for i in ids) + "\n")
        self._log.ok(f"已复制 {len(ids)} 条缺失依赖的工坊网址"
                     "（每行一条，可贴进【入账中心 · 登记】）")

    def _checked_missing_ids(self) -> list[int]:
        """勾选行的缺失依赖编号并集（去重升序）——复制与送登记两路
        共用一份采集，口径不漂。勾选记编号不记行号：行内容按编号从
        _missing_now 现取，重建表格后勾选不掉。"""
        t = self._miss_table
        ids: set[int] = set()
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                ids.update((getattr(self, "_missing_now", {}) or {})
                           .get(it.data(Qt.ItemDataRole.UserRole), []))
        return sorted(ids)

    def _copy_checked_missing(self) -> None:
        """勾选行的缺失依赖编号并集复制（每行一个）。没勾提示先勾。"""
        ordered = self._checked_missing_ids()
        if not ordered:
            QMessageBox.information(self, "复制勾选行",
                                    "先在缺依赖列表里勾选若干行。")
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in ordered) + "\n")
        self._log.ok(f"已复制勾选行的 {len(ordered)} 个缺失依赖编号"
                     "（每行一个，到【入账中心 · 登记】粘贴登记）")

    def _send_checked_to_register(self) -> None:
        """勾选行的缺失依赖直接送【入账中心 · 登记】（预填+解析预览，
        不写库；落点在主窗口）。没勾提示先勾。"""
        ordered = self._checked_missing_ids()
        if not ordered:
            QMessageBox.information(self, "送登记",
                                    "先在缺依赖列表里勾选若干行。")
            return
        self.send_to_register_requested.emit(ordered)

    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or "（无标题）"

    @staticmethod
    def _ids_text(ids: list[int], limit: int = 20) -> str:
        head = "、".join(str(i) for i in ids[:limit])
        return head + (f" …（共 {len(ids)} 个）" if len(ids) > limit else "")

    def _copy_missing(self, ids: list[int]) -> None:
        QApplication.clipboard().setText("\n".join(str(i) for i in ids))
        self._log.ok(f"已复制 {len(ids)} 个缺失依赖编号"
                     "（到【入账中心 · 登记】粘贴登记）")

    def _on_row_menu(self, lw: QListWidget, pos) -> None:
        """依赖变化列表右键（查看类）。缺依赖表有专属菜单
        （_on_miss_menu），不走这里。"""
        it = lw.itemAt(pos)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        act_url = QAction("打开工坊页面", menu)
        act_url.setToolTip("在浏览器打开该 mod 的创意工坊页面")
        act_url.triggered.connect(lambda: self._open_url(mid))
        menu.addAction(act_url)
        act_copy = QAction("复制编号", menu)
        act_copy.triggered.connect(
            lambda: (QApplication.clipboard().setText(str(mid)),
                     self._log.info(f"已复制编号 {mid}")))
        menu.addAction(act_copy)
        menu.exec(lw.mapToGlobal(pos))

    def _open_url(self, mid: int) -> None:
        m = self._mods_by_id.get(mid)
        # 网址现拼走唯一入口 workshop_url。此前直接 .format(mid) 填进
        # constants 里那份 {mod_id} 命名占位模板——右键打开页面当场
        # KeyError，即本次修复的根因
        url = workshop_url(mid, m.url if m is not None else None)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打开页面", f"浏览器没有响应，请手动打开：\n{url}")
    def _on_miss_dclicked(self, item) -> None:
        """缺依赖行双击 = 打开该 mod 的工坊页面（缺的上家；缺的啥
        到明细表看——两表分工）。"""
        it = self._miss_table.item(item.row(), 1)
        mid = it.data(Qt.ItemDataRole.UserRole) \
            if it is not None else None
        if mid is not None:
            self._open_url(mid)

    def _on_miss_menu(self, pos) -> None:
        """缺依赖表右键：顶部两枚勾选总控（全选只勾可见行——查找
        过滤在场时勾进看不见的行 = 盲勾隐患，备份页同款纪律），
        单行动作（光标下有行才有），底部勾选批动作。"""
        t = self._miss_table
        row = t.rowAt(pos.y())
        mid = None
        if row >= 0:
            it = t.item(row, 1)
            mid = it.data(Qt.ItemDataRole.UserRole) \
                if it is not None else None
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        a_all = QAction("全选（可见行）", menu)
        a_all.setToolTip("只勾选当前看得见的行——查找过滤在场时不盲勾")
        a_all.triggered.connect(self._check_all_visible)
        menu.addAction(a_all)
        a_none = QAction("清空勾选", menu)
        a_none.triggered.connect(self._uncheck_all_missing)
        menu.addAction(a_none)
        if mid is not None:
            menu.addSeparator()
            act_url = QAction("打开工坊页面", menu)
            act_url.setToolTip("在浏览器打开该 mod 的创意工坊页面")
            act_url.triggered.connect(lambda: self._open_url(mid))
            menu.addAction(act_url)
            act_copy = QAction("复制编号", menu)
            act_copy.triggered.connect(
                lambda: (QApplication.clipboard().setText(str(mid)),
                         self._log.info(f"已复制编号 {mid}")))
            menu.addAction(act_copy)
            lack = (getattr(self, "_missing_now", {}) or {}).get(mid) or []
            if lack:
                menu.addSeparator()
                a1 = QAction(
                    f"复制此 mod 缺失的依赖编号（{len(lack)} 个）", menu)
                a1.triggered.connect(
                    lambda: QApplication.clipboard().setText(
                        "\n".join(str(i) for i in lack) + "\n"))
                menu.addAction(a1)
                a2 = QAction("复制缺失依赖的工坊网址（每行一条）", menu)
                a2.triggered.connect(
                    lambda: self._copy_missing_urls(lack))
                menu.addAction(a2)
        menu.addSeparator()
        a3 = QAction("复制勾选行的缺失依赖编号", menu)
        a3.setToolTip("先勾选若干行再点——把选中 mod 的缺失依赖一次带走")
        a3.triggered.connect(self._copy_checked_missing)
        menu.addAction(a3)
        a4 = QAction("送登记勾选行的缺失依赖…", menu)
        a4.setToolTip("把勾选行缺失的依赖编号直接送【入账中心 · 登记】"
                      "预填并解析预览（不写库）")
        a4.triggered.connect(self._send_checked_to_register)
        menu.addAction(a4)
        menu.exec(t.viewport().mapToGlobal(pos))

    def _check_all_visible(self) -> None:
        """全选只勾可见行（备份页同款）；清空仍作用所有行——只往
        安全方向走。"""
        t = self._miss_table
        for r in range(t.rowCount()):
            if t.isRowHidden(r):
                continue
            it = t.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Checked)

    def _uncheck_all_missing(self) -> None:
        t = self._miss_table
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Unchecked)

    def _apply_filter(self) -> None:
        """页面级查找落地：缺依赖表（编号/标题/缺失列）/ 明细表
        （编号/标题/状态/被谁依赖）/ 依赖变化列表三处联动，不命中的
        行隐藏。只隐藏不清数据；清空全恢复；勾选不受影响。幂等。"""
        kw = self._filter_edit.text().strip().casefold()
        t = self._miss_table
        for r in range(t.rowCount()):
            if not kw:
                t.setRowHidden(r, False)
                continue
            hay = " ".join(
                t.item(r, c).text()
                for c in (1, 2, 4)
                if t.item(r, c) is not None).casefold()
            t.setRowHidden(r, kw not in hay)
        d = self._dep_detail
        for r in range(d.rowCount()):
            if not kw:
                d.setRowHidden(r, False)
                continue
            hay = " ".join(
                d.item(r, c).text()
                for c in (1, 2, 5, 6)
                if d.item(r, c) is not None).casefold()
            d.setRowHidden(r, kw not in hay)
        lw = self._sec_changed[1]
        for i in range(lw.count()):
            it = lw.item(i)
            if it is not None:
                it.setHidden(bool(kw)
                             and kw not in it.text().casefold())
    def _checked_dep_ids(self) -> list[int]:
        """明细表勾选行的缺失编号（去重升序）——复制与送登记共用。"""
        t = self._dep_detail
        ids: set[int] = set()
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                rid = it.data(Qt.ItemDataRole.UserRole)
                if rid is not None:
                    ids.add(int(rid))
        return sorted(ids)

    def _check_all_deps_visible(self) -> None:
        t = self._dep_detail
        for r in range(t.rowCount()):
            if t.isRowHidden(r):
                continue
            it = t.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Checked)

    def _uncheck_all_deps(self) -> None:
        t = self._dep_detail
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Unchecked)

    def _copy_checked_deps(self) -> None:
        ordered = self._checked_dep_ids()
        if not ordered:
            QMessageBox.information(self, "复制勾选行",
                                    "先在明细表里勾选若干行。")
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in ordered) + "\n")
        self._log.ok(f"已复制勾选的 {len(ordered)} 个缺失依赖编号"
                     "（每行一个，到【入账中心 · 登记】粘贴登记）")

    def _send_checked_deps_to_register(self) -> None:
        ordered = self._checked_dep_ids()
        if not ordered:
            QMessageBox.information(self, "送登记",
                                    "先在明细表里勾选若干行。")
            return
        self.send_to_register_requested.emit(ordered)

    def _on_dep_menu(self, pos) -> None:
        """明细表右键：勾选总控 + 单行动作 + 勾选批动作。明细行本身
        就是缺失编号——送登记送的是编号本身。"""
        t = self._dep_detail
        row = t.rowAt(pos.y())
        rid = None
        if row >= 0:
            it = t.item(row, 1)
            rid = it.data(Qt.ItemDataRole.UserRole) \
                if it is not None else None
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        a_all = QAction("全选（可见行）", menu)
        a_all.setToolTip("只勾选当前看得见的行——查找过滤在场时不盲勾")
        a_all.triggered.connect(self._check_all_deps_visible)
        menu.addAction(a_all)
        a_none = QAction("清空勾选", menu)
        a_none.triggered.connect(self._uncheck_all_deps)
        menu.addAction(a_none)
        if rid is not None:
            menu.addSeparator()
            act_url = QAction("打开工坊页面", menu)
            act_url.triggered.connect(lambda: self._open_url(rid))
            menu.addAction(act_url)
            act_copy = QAction("复制编号", menu)
            act_copy.triggered.connect(
                lambda: (QApplication.clipboard().setText(str(rid)),
                         self._log.info(f"已复制编号 {rid}")))
            menu.addAction(act_copy)
        menu.addSeparator()
        a3 = QAction("复制勾选行的编号", menu)
        a3.setToolTip("先勾选若干行再点（每行一个）")
        a3.triggered.connect(self._copy_checked_deps)
        menu.addAction(a3)
        a4 = QAction("送登记勾选行…", menu)
        a4.setToolTip("把勾选的缺失依赖编号直接送【入账中心 · 登记】"
                      "预填并解析预览（不写库）")
        a4.triggered.connect(self._send_checked_deps_to_register)
        menu.addAction(a4)
        menu.exec(t.viewport().mapToGlobal(pos))
