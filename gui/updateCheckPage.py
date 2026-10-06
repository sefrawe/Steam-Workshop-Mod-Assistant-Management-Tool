"""gui/updateCheckPage.py · 更新检测——账本页头承诺的「版本判定第二处」。"""
r"""对当前档案全部未删除 mod 批量查询 Steam 远端实况：补全标题等元
数据、按判决制公式判定哪些需要更新、给特别关注的 mod 记提醒、
result=9 时建立失效归档。

【判决制口径（本页的几条底线）】
- 判定 = D2 唯一公式：远端 time_updated > confirmed_version（仅
  downloaded）。confirmed_version 是你在批次收尾确认过的版本基准，
  检测只写远端侧（观测值/快照/提醒/查过时间），绝不去碰它（R17）。
- 因此「检测到但没确认下载」的条目下次检测必然再次报出，直到批次
  收尾确认入账对齐才消音——检测不吞更新，这是设计不是毛病。
- 版本未知（downloaded 但 confirmed_version 为空）照常查询补元数据，
  新旧无从判定，单独列「版本未知」，绝不混入需更新/已最新。
- 疑似合集（远端报成功但大小缺失/为 0）零写入——防把合集标题误填
  进普通 mod；确认展开走【展开合集…】。
- result=9（Steam 接口看不见条目）且条目已被用过（downloaded/
  failed）→ 自动建失效归档（异常页处置的依据）；tracked 刻意跳过
  （接口看不见 ≠ 下载不了，先验证再处置）。

【D3 触发值两来源 · 本页是其一】判定出的「需更新」清单连同各自的
远端 time_updated（触发值）一起经 updates_found 交给主窗口——询问
后开批次并把触发值随批携带，批次收尾按 D4 分支写确认基准（R18：
绝不写高于触发值的数）。手动批次/终端粘贴由控制器开工前批查补采，
与本页无关。

【线程与归属】联网查询在后台线程（只发网络请求，绝不碰数据库）；
写库全部留在主线程（SQLite 毫秒级）。停止 = 正常操作：点停后批边界
生效、已查到的数据一律丢弃不落库。结果归属 = 开始检测那一刻的档案
——检测进行中允许切换档案，旧结果仍归旧档案，不串家。
"""
import threading
import time

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import constants, netGate
from core.formatters import fmt_size, relative_time
from core.models import Game, Mod
from core.steamApiClient import (
    SteamApiCancelled, SteamApiClient, SteamApiError, WorkshopItem)
from core.urlParser import WORKSHOP_URL_TEMPLATE
from gui.consolePanel import LogBus
from gui.theme import font_px  # 字号单源（D25）

# 「发现更新后自动开始下载」的设置键（D16：唯一保留的就地开关——
# 界面勾选即时保存，不进设置页）
_KEY_AUTO_DOWNLOAD = "auto_download_after_check"

# Steam 官方接口单批上限 100（D13 沿用）
_BATCH = 100

_RESULT_COLUMNS = ["编号", "标题", "结果", "远端更新", "更新间隔", "大小", "操作"]

# 决策 46：网络类失败固定附带的提示（失败原文照展在前，提示附后——
# 连接超时是到 Steam 的链路波动，不是软件坏了，也不是 Steam 限流）
_NET_HINT = ("提示：连续失败常是到 Steam 服务器的网络波动（晚间更明显），"
             "属正常现象——稍等几分钟到几十分钟再试通常就能恢复。"
             "本次结果没有写入，数据无损；不必连续重试（失败要等重试"
             "跑完才报错，最长一两分钟），隔段时间来试即可。"
             "可以参考watt toolkit的连通性测试，若一直不通过，"
             "可以尝试关闭watt toolkit，然后尝试。下载mod同理。")


class _CheckWorker(QThread):
    """后台检测线程：分批查询全部条目，每查完一批就报告一次进度。
    只发网络请求，绝不碰数据库（数据库操作全在主线程）。"""

    batch_done = Signal(int)      # 已完成查询的条目数（驱动进度条）
    succeeded = Signal(list)      # 全部查完，携带 WorkshopItem 列表
    failed = Signal(str)          # 请求层面失败，携带给用户看的原因
    stopped = Signal()            # 用户点了"停止"：正常收场，不算失败

    def __init__(self, mod_ids: list[int], *, interval_ms: int,
                 max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._stop_requested = False
        self._cancel = threading.Event()   # 与客户端共用的取消事件

    def stop(self) -> None:
        """请求停止：批边界生效（取消事件让客户端的等待与重试立即
        中断，最多多等一次在途请求的超时）。"""
        self._stop_requested = True
        self._cancel.set()

    def run(self) -> None:
        client = SteamApiClient(
            interval_ms=self._interval_ms,
            max_retries=self._max_retries,
            cancel_event=self._cancel)
        ids = list(dict.fromkeys(self._mod_ids))   # 去重且保序
        done = 0
        items: list[WorkshopItem] = []
        try:
            for start in range(0, len(ids), _BATCH):
                if self._stop_requested:
                    # 停止是用户的正常操作，不是失败——走专用信号，
                    # 界面上以灰字收场，不弹"检测失败"的警告框
                    self.stopped.emit()
                    return
                items.extend(
                    client.query_details(ids[start:start + _BATCH]))
                done += len(ids[start:start + _BATCH])
                self.batch_done.emit(done)
                if start + _BATCH < len(ids):
                    # 批间礼貌间隔，来源同客户端的请求间隔设置
                    time.sleep(self._interval_ms / 1000)
        except SteamApiCancelled:
            # 取消事件在请求/等待中途触发：与批边界停止同一收场——
            # 丢弃结果、走 stopped，不弹"检测失败"
            self.stopped.emit()
            return
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        # 兜底：停止检查只在"每批开始前"做——用户若在最后一批查询
        # 途中点停止，循环会正常跑完走到这里。按同样口径处理：
        # 丢弃结果、不落库，保证"停止 = 什么都没发生"这条承诺不破
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(items)


class _CollectionWorker(QThread):
    """后台查询合集成员：单次网络请求，只发不写。"""

    succeeded = Signal(object, list)  # (合集编号, 成员编号列表)
    # 合集编号也是工坊编号（publishedfileid 同一编号空间，>2³¹ 常态），
    # 与 mod_id 同坑（踩坑 79），必须 object

    failed = Signal(str)

    def __init__(self, collection_id: int, *, max_retries: int) -> None:
        super().__init__()
        self._collection_id = collection_id
        self._max_retries = max_retries

    def run(self) -> None:
        client = SteamApiClient(max_retries=self._max_retries)
        try:
            children = client.query_collection_children(
                self._collection_id)
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self._collection_id, children)


class UpdateCheckPage(QWidget):
    """checks_finished(int)：一次检测完成落库后发射，参数为本次检测到
    有新版本的 mod 数（合集展开登记后发射 0），主窗口借此刷新 mod 库
    页（V2：mod 库页进页自动刷新，此信号可不接）。
    updates_found(int, list, dict)：确有新版本的 (档案 app_id, 编号
    清单, 触发值映射 mod_id→远端 time_updated)。主窗口接它做日常
    更新一条龙（D26 + D3①）：弹窗确认或按勾选自动开批次，触发值
    随批携带给批次收尾的 D4 分支。
    progress_changed(int, int) / check_interrupted(str)：给「日常更新」
    模块页的第二表盘与收尾信号（D42）——模块页未搬迁前发进空气，
    无害。"""

    checks_finished = Signal(int)
    updates_found = Signal(int, list, object)
    # 尾参 object 而非 dict（踩坑 87 漏网）：dict 信号走 QVariantMap
    # 要求字符串键，本映射是 {mod_id: 触发值} 的 int 键 → Shiboken
    # 静默换空字典（实证：检测到 73 条，开批后触发值全丢）。list 参数
    # 不用动：列表元素走 64 位转换，大编号能活。首参 app_id 小值，int 合法

    progress_changed = Signal(int, int)
    check_interrupted = Signal(str)

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()     # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._worker: _CheckWorker | None = None
        self._col_worker: _CollectionWorker | None = None
        self._mods_at_start: list = []  # 开始检测时的库内顺序，决定展示顺序
        # 开始检测那一刻的档案 = 本轮结果的真正归属。检测进行中允许
        # 切换档案（set_game 只更新标签不动结果区），但旧结果、旧结果
        # 行上的"展开合集"都仍归这个档案——防止把 mod 登记到新档案名下
        self._check_game: Game | None = None
        self._build_ui()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        title = QLabel("更新检测", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)
        self._game_label = QLabel(self)
        root.addWidget(self._game_label)
        tip = QLabel(
            "点【开始检测】查询当前档案全部 mod 的远端信息：自动补全标题"
            "等字段、判定需不需要更新；特别关注的 mod 出新版本时会记一条"
            "提醒。\n"
            "判定基准 = 你在批次收尾确认过的本地版本：检测只记远端观测、"
            "从不改它——所以检测到但还没确认下载的条目，下次检测会再次"
            "报出，确认入账对齐后才消音。\n"
            "文件大小缺失的条目列为「疑似合集」，确认后可展开入库；"
            "查询失败的条目会列出原因——其中 result=9 = Steam 已看不见"
            "该条目（作者删除/下架/转私有），已下载过的会自动建立失效"
            "归档，处置见【异常处置】页。\n"
            "已删除的 mod 不查询；版本未知的 mod 照常查询，但无法判定"
            "新旧，结果里单独列出。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._start_btn = QPushButton("开始检测", btn_row)
        self._start_btn.clicked.connect(self._start_check)
        self._start_btn.setToolTip(
            "批量查询当前档案全部 mod 的远端信息：补全标题、判定需不需要"
            "更新、给特别关注的 mod 记提醒。可中途【停止】（等待与重试"
            "立即中断）；已删除的 mod 不查询。\n"
            "查询失败的条目原样列出：result=9 = 条目已失效——已下载过的"
            "自动建立失效归档，之后到【异常处置】页处置（先试重下）")
        self._stop_btn = QPushButton("停止", btn_row)
        self._stop_btn.setToolTip(
            "请求停止本次检测：等待与重试立即中断，最多再等一次在途请求"
            "收场（网络不畅时约 10~40 秒）；已查到的数据全部丢弃不写入"
            "——重新点【开始检测】即可重来")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_check)
        # 日常更新一条龙的自动开关（D26）：勾了就不询问直接下载。
        # 勾选即时保存，不经过设置页的【保存】按钮
        self._auto_check = QCheckBox("发现更新后自动开始下载", btn_row)
        self._auto_check.setToolTip(
            "勾选：检测到新版本后不再询问，直接交给「下载批次」按列表"
            "顺序逐条下载（仍需要 steamcmd 已在终端里启动并登录）。\n"
            "不勾选：每次检测到更新先弹窗询问。勾选即时保存。")
        self._auto_check.setChecked(
            self._settings.get_int(_KEY_AUTO_DOWNLOAD, 0) != 0)
        self._auto_check.toggled.connect(self._on_auto_download_toggled)
        self._progress = QProgressBar(btn_row)
        self._progress.setVisible(False)
        h.addWidget(self._start_btn)
        h.addWidget(self._stop_btn)
        h.addWidget(self._auto_check)
        h.addWidget(self._progress, 1)
        root.addWidget(btn_row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = QTableWidget(0, len(_RESULT_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_RESULT_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 110), (2, 110), (3, 130), (4, 90),
                           (5, 110), (6, 110)):
            self._table.setColumnWidth(col, width)
        root.addWidget(self._table, 1)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        if self._worker is not None:
            # 检测进行中：只更新标签，不动结果区——结果仍归属开始检测
            # 时的档案（self._check_game）。写入按编号进行，与界面当前
            # 选的游戏无关，所以数据永远写不错
            return
        self._mods_at_start = []
        self._table.setRowCount(0)
        self._summary.setText("")
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._start_btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._start_btn.setEnabled(True)

    def shutdown(self) -> None:
        """程序退出前的收尾：请求检测线程停止并等 1.5 秒（原来无限期
        wait()，线程卡在网络超时/重试里时主线程跟着冻住、Windows 判
        "未响应"——等不到就交 netGate.park 保活，退场守卫随进程带走；
        线程只发网络请求不写库，零数据损失）。合集查询没有停止协议但
        只有一次请求，同样 1.5 秒封顶。"""
        if self._worker is not None:
            self._worker.stop()
            if not self._worker.wait(1500):
                netGate.park(self._worker)
            self._worker = None
        if self._col_worker is not None:
            if not self._col_worker.wait(1500):
                netGate.park(self._col_worker)
            self._col_worker = None

    def start_check(self) -> str:
        """对外入口：「日常更新」模块页（未搬迁）经主窗口调到这里
        自动开测。与点【开始检测】同一条路、同一套守卫；返回 "" = 
        已受理，非空 = 拒绝原因。"""
        return self._start_check()

    # ---------- 检测流程 ----------
    def _start_check(self) -> str:
        """开一次检测。返回 "" = 已受理开跑；非空 = 拒绝原因（中文，
        给主窗口写日志用）。"""
        if self._game is None:
            return "没有选择档案"
        if self._worker is not None:
            return "已有检测在进行"
        mods = self._repo.list_mods(self._game.app_id,
                                    order_by="time_updated DESC")
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._summary.setText(
                "当前档案没有任何可检测的 mod（已删除的除外）。")
            self._summary.setStyleSheet("color: gray;")
            return "当前档案没有可检测的 mod"
        self._mods_at_start = mods
        # 记下"开始检测那一刻"的档案：本轮所有结果（含展开合集登记的
        # 条目）都写进它，即便检测过程中用户切到了别的档案
        self._check_game = self._game
        self._check_total = len(ids)     # 进度中继要用
        # 联网互斥闸：同一时刻只允许一个联网入口在查
        busy = netGate.try_acquire(constants.NET_GATE_UPDATE_CHECK)
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再检测。")
            self._summary.setStyleSheet("color: #f5a623;")
            return f"另有联网任务在进行（{busy}）"
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_running(True)
        self._worker = _CheckWorker(
            ids,
            interval_ms=self._settings.get_int("api_request_interval_ms",
                                               200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._worker.batch_done.connect(self._progress.setValue)
        self._worker.batch_done.connect(self._on_batch_progress)
        self._worker.succeeded.connect(self._on_check_succeeded)
        self._worker.failed.connect(self._on_check_failed)
        self._worker.stopped.connect(self._on_check_stopped)
        # 线程跑完（无论哪种结局）统一由 finished 收尾销毁。
        # 注意：不能在 stopped/succeeded/failed 的处理函数里把
        # self._worker 置 None——那时线程可能还没完全退出，
        # Python 侧提前销毁会让 Qt 直接终止进程（闪退 0xC0000409）
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self._log.info(f"开始检测 {len(ids)} 个 mod…")
        return ""

    def _on_batch_progress(self, done: int) -> None:
        """进度中继（D42）：worker 的 batch_done 在驱动本页进度条之外，
        原样转发为页面级 progress_changed——日常更新模块页的进度条是
        同一份数据的第二块表盘。中继只发信号，不碰控件。"""
        self.progress_changed.emit(done, self._check_total)

    def _stop_check(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._stop_btn.setEnabled(False)
            self._summary.setText(
                "已请求停止：等待与重试已中断，最多再等一次在途请求收场"
                "（网络不畅时约 10~40 秒）；已查到的数据照旧不写入，"
                "退出软件不必等它。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止更新检测：等待与重试已中断，"
                           "等在途请求收场")

    def _set_running(self, running: bool) -> None:
        self._start_btn.setEnabled(not running and self._game is not None)
        self._stop_btn.setEnabled(running)
        self._progress.setVisible(running)

    def _on_worker_finished(self) -> None:
        """检测线程跑完的统一收尾：确认线程彻底退出后，才释放引用。
        finished 发出时线程本体可能还在做最后的退出动作，直接把引用
        丢给 Python 销毁会踩中"销毁仍在运行的线程"，进程当场终止
        （0xC0000409）。联网互斥闸也在这里归还：finished 无论成功/
        失败/停止都必发。"""
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_UPDATE_CHECK)

    # ---------- 结果处理（主线程） ----------
    @staticmethod
    def _classify(items: list[WorkshopItem]):
        """按结果分桶：正常条目 / 疑似合集（大小缺失或为 0）/ 查询失败。"""
        ok_items: list[WorkshopItem] = []
        suspected_ids: set[int] = set()
        failed: dict[int, int] = {}     # 编号 → 接口返回的 result 原值
        for item in items:
            if item.mod_id is None:
                continue                # 响应异常条目，无处可写，只能丢弃
            if item.result != 1:
                failed[item.mod_id] = item.result
            elif not item.file_size:
                suspected_ids.add(item.mod_id)
            else:
                ok_items.append(item)
        return ok_items, suspected_ids, failed

    @staticmethod
    def _kind_of(m: Mod) -> str:
        """D2 唯一公式的检测侧分桶（与 mod 库页"更新"列同源同尺）：
        downloaded 且确认基准为空 = 版本未知；downloaded 且远端更新 >
        确认基准 = 需更新；其余（已最新 / 未下载 / 远端未知）不进表。"""
        if m.status != "downloaded":
            return "未下载"
        if m.confirmed_version is None:
            return "版本未知"
        if (m.time_updated is not None
                and m.time_updated > m.confirmed_version):
            return "需更新"
        return "已最新"

    def _apply_results(self, ok_items: list[WorkshopItem],
                       suspected_ids: set[int],
                       failed: dict[int, int]) -> tuple[int, dict, int]:
        """把查询结果写入数据库，返回 (需更新个数, 触发值映射, 本次新建
        失效归档条数)。

        判定 = D2 唯一公式：仅 downloaded 且 confirmed_version 非空且
        远端 time_updated > confirmed_version 计入需更新，并记下触发值
        （mod_id → 远端 time_updated）随 updates_found 交主窗口随批携带。
        检测只写远端侧三路（元数据 / 版本快照 / 特别关注提醒）+ 查过
        时间 + result=9 失效归档；confirmed_version / confirmed_source
        一个不碰——那是确认门的地盘（R17）。

        写入三路与判定无关，各管各的账：first_fill = 第一次拿到远端
        信息（last_time_updated 记为当前值 = 没有上一版）；changed =
        远端真变了（旧远端值挪进 last_time_updated 作间隔基准 + 拍
        版本快照 + 特别关注记提醒，同一远端版本只提醒一次）；其余只
        刷新标题等元数据。"复遇版本相同按已最新"（分钟级缓存容忍）
        由 changed 的不等式天然覆盖。

        失效归档：result=9 且条目已被用过（downloaded / failed）且尚无
        归档时建档；状态逐条现查（防检测进行中条目被处置后误翻状态）；
        tracked 刻意跳过（接口看不见≠下载不了，先验证再处置）；已有
        归档跳过。全部包在一个事务里，失败整体回滚。
        """
        now = int(time.time())
        updates_found = 0
        triggers: dict[int, int] = {}
        # 已有归档的编号（防重复建档）：归档量小，一次读回毫秒级
        archived: set[int] = set()
        if self._check_game is not None:
            archived = {r.mod_id for r in
                        self._repo.list_failed(self._check_game.app_id)}
        # 先整体记"检测过"（包括失败和疑似合集的）——"查过了"这个事实
        # 本身就值得记，否则"上次检测时间"的展示会骗人
        all_ids = ([i.mod_id for i in ok_items]
                   + sorted(suspected_ids) + sorted(failed))
        n_archived = 0
        with self._repo.transaction():
            self._repo.touch_checked(all_ids, checked_at=now)
            # 失效归档：只认 result=9；状态逐条现查
            for mid, code in sorted(failed.items()):
                if code != 9 or mid in archived:
                    continue
                row = self._repo.get_mod(mid)
                if row is None or row.status not in ("downloaded",
                                                     "failed"):
                    continue
                self._repo.mark_failed(
                    mid, "接口返回 result=9（删除/下架/转私有等）")
                n_archived += 1
            for item in ok_items:
                row = self._repo.get_mod(item.mod_id)
                if row is None:
                    continue  # 编号取自库中，理论到不了；防御一行
                # ——判定（D2 公式，与 mod 库页同源同尺）——
                if (row.status == "downloaded"
                        and row.confirmed_version is not None
                        and item.time_updated is not None
                        and item.time_updated > row.confirmed_version):
                    updates_found += 1
                    triggers[item.mod_id] = item.time_updated
                # ——写入（只管远端侧的账，确认基准一个不碰）——
                changed = (item.time_updated is not None
                           and item.time_updated != row.time_updated)
                first_fill = (item.time_updated is not None
                              and row.time_updated is None)
                meta = {
                    "title": item.title,
                    "creator": item.creator,
                    "time_created": item.time_created,
                    "file_size": item.file_size,
                    "subscriptions": item.subscriptions,
                    "favorited": item.favorited,
                    "views": item.views,
                    "tags": item.tags,
                    "preview_url": item.preview_url,
                }
                if first_fill:
                    self._repo.update_api_metadata(
                        item.mod_id,
                        time_updated=item.time_updated,
                        last_time_updated=item.time_updated,
                        **meta)
                elif changed:
                    self._repo.update_api_metadata(
                        item.mod_id,
                        time_updated=item.time_updated,
                        last_time_updated=row.time_updated,
                        **meta)
                    # 拍版本快照（远端观测史，滚动保留）
                    # 拍版本快照（远端观测史，滚动保留）。
                    # v2 签名在 * 之后只收带名参数；这里沿用旧版的位置
                    # 传参会报 "takes 2 positional arguments but 3 were
                    # given"，触发检测结果整体回滚——改成带名即愈
                    self._repo.add_snapshot(item.mod_id,
                                            time_updated=item.time_updated)

                    if row.is_special:
                        # 特别关注：同一远端版本只提醒一次
                        last = self._repo.get_last_alert(item.mod_id)
                        if last is None or \
                                last.remote_time_updated != item.time_updated:
                            diff = ((item.time_updated
                                     - row.last_time_updated)
                                    if row.last_time_updated else None)
                            self._repo.add_alert(
                                item.mod_id, item.time_updated,
                                diff_seconds=diff,
                                was_downloaded=(row.status
                                                == "downloaded"))
                else:
                    # 远端没变：只刷新其余元数据，时间字段一个不动
                    self._repo.update_api_metadata(item.mod_id, **meta)
        return updates_found, triggers, n_archived

    def _show_results(self, suspected_ids: set[int],
                      failed: dict[int, int],
                      n_archived: int = 0) -> None:
        """结果表只显示有信息量的行：需更新/版本未知/疑似合集/查询失败
        进表；已最新/未下载/远端未知不占行——数量进汇总行兜底，完整
        消失会让人怀疑没查到。顺序仍按开始检测时的库内顺序；正常条目
        重新从库里读一遍（展示写入后的最新状态）。"""
        threshold = self._settings.get_int("slow_update_days", 30)
        visible = [m for m in self._mods_at_start if m.status != "deleted"]
        # 第一遍：分拣 + 顺带取 fresh（第二遍不重查库）
        rows = []       # (m, fresh, title, kind) 有信息量的
        n_normal = 0
        for m in visible:
            fresh = self._repo.get_mod(m.mod_id)
            title = (fresh.title if fresh is not None else None) \
                or m.title or "（无标题）"
            if m.mod_id in failed:
                kind = "查询失败"
            elif m.mod_id in suspected_ids:
                kind = "疑似合集/异常"
            else:
                kind = self._kind_of(fresh)
            if kind in ("需更新", "版本未知", "查询失败", "疑似合集/异常"):
                rows.append((m, fresh, title, kind))
            else:
                n_normal += 1
        counts = {"需更新": 0, "版本未知": 0,
                  "疑似": len(suspected_ids), "失败": len(failed)}
        self._table.setRowCount(len(rows))
        red = QColor("#e5484d")
        for r, (m, fresh, title, kind) in enumerate(rows):
            self._set_cell(r, 0, str(m.mod_id))
            self._set_cell(r, 1, title)
            counts["需更新"] += kind == "需更新"
            counts["版本未知"] += kind == "版本未知"
            self._set_cell(r, 2, kind)
            if m.mod_id in failed:
                code = failed[m.mod_id]
                if code == 9:
                    it = self._set_cell(r, 3, "result=9：条目已失效")
                    it.setToolTip(
                        "Steam 接口已看不见该条目（作者删除/下架/"
                        "转私有等）。接口看不见 ≠ 一定下载不了——"
                        "可先到【下载命令生成】页试下载一次；处置"
                        "去【异常处置】页。已下载过的条目本次检测"
                        "已自动建立失效归档")
                else:
                    it = self._set_cell(r, 3, f"接口返回 result={code}")
                    it.setToolTip(
                        "接口返回了异常结果码（可能被设为私有/地区"
                        "限制/远端波动）——稍后重测即可")
            elif m.mod_id in suspected_ids:
                self._set_cell(r, 3,
                               "远端缺少文件大小——可能是合集，"
                               "点右侧按钮确认")
            else:
                self._set_cell(r, 3, relative_time(fresh.time_updated))
            # 更新间隔 = 本次版本距上一版过了多少天（作者的更新节奏）。
            # last_time_updated 与当前值相等 = "没有上一版"，显示"—"
            baseline = fresh.last_time_updated
            days = None
            if (fresh.time_updated is not None
                    and baseline is not None
                    and baseline != fresh.time_updated):
                days = (fresh.time_updated - baseline) / 86400
            if days is not None and days >= 0:
                text = f"{days:.0f} 天" if days >= 1 else "不足 1 天"
                item = self._set_cell(r, 4, text)
                if days > threshold:
                    item.setForeground(red)
            else:
                self._set_cell(r, 4, "—")
            self._set_cell(r, 5,
                           fmt_size(fresh.local_size or fresh.file_size))
            self._set_cell(r, 6, "")
            if m.mod_id in suspected_ids:
                btn = QPushButton("展开合集…", self._table)
                btn.clicked.connect(
                    lambda _=False, mid=m.mod_id:
                    self._expand_collection(mid))
                self._table.setCellWidget(r, 6, btn)
        arch_tail = f"（result=9 建档 {n_archived}）" if n_archived else ""
        self._summary.setText(
            f"检测完成：需更新 {counts['需更新']}｜版本未知 "
            f"{counts['版本未知']}｜疑似合集 {counts['疑似']}"
            f"｜查询失败 {counts['失败']}{arch_tail}"
            f"｜已最新/未下载/远端未知 {n_normal} 个（不占表）")
        self._summary.setStyleSheet("color: #46a758;")

    def _set_cell(self, row: int, col: int,
                  text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        self._table.setItem(row, col, item)
        return item

    def _on_check_succeeded(self, items: list) -> None:
        self._set_running(False)
        ok_items, suspected_ids, failed = self._classify(items)
        try:
            updates_found, triggers, n_archived = self._apply_results(
                ok_items, suspected_ids, failed)
        except Exception as exc:
            # 写库失败：明确告诉用户，不让程序无声崩溃
            self._log.error(f"检测结果写入失败，已整体回滚：{exc}")
            QMessageBox.critical(
                self, "写入失败",
                f"查询成功，但写入数据库时出错：{exc}\n"
                "数据已整体回滚，请重试一次；若反复出现请反馈。")
            return
        self._show_results(suspected_ids, failed, n_archived)
        self.checks_finished.emit(updates_found)
        if triggers and self._check_game is not None:
            # 交给主窗口的"日常更新一条龙"（D26 + D3①）：归属开始
            # 检测那一刻的档案，触发值随批携带（批次收尾 D4 分支用）
            self.updates_found.emit(self._check_game.app_id,
                                    list(triggers.keys()), triggers)
        tail = (f"，result=9 已建失效归档 {n_archived} 条"
                if n_archived else "")
        self._log.ok(f"检测完成：需更新 {updates_found}，"
                     f"疑似合集 {len(suspected_ids)}，"
                     f"查询失败 {len(failed)}{tail}")

    def _on_check_failed(self, message: str) -> None:
        self._set_running(False)
        # D42：非成功收尾通知模块页收起进度条（成功走 checks_finished）
        self.check_interrupted.emit(message)
        # D46：失败细节原文保留（诊断有用），固定提示附在后面
        self._summary.setText(message + "\n" + _NET_HINT)
        self._summary.setStyleSheet("color: #e5484d;")
        self._log.error(message)
        self._log.warn(_NET_HINT)
        QMessageBox.warning(self, "检测失败", message + "\n\n" + _NET_HINT)

    def _on_check_stopped(self) -> None:
        """用户手动停止：正常收场。不弹窗、不标红——停止是用户主动
        做的正常操作，和"接口报错"是两回事，界面上用灰字说明即可。
        数据没写库，无需任何清理。"""
        self._set_running(False)
        self.check_interrupted.emit("已手动停止，本次结果未写入")
        self._summary.setText(
            "已手动停止，本次结果未写入。重新点【开始检测】即可。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("更新检测已手动停止，本次结果未写入")

    def _on_auto_download_toggled(self, checked: bool) -> None:
        """「发现更新后自动开始下载」勾选即保存（D26）。不经过设置页
        的【保存】按钮。"""
        self._settings.set(_KEY_AUTO_DOWNLOAD, "1" if checked else "0")
        self._settings.save()

    # ---------- 展开合集 ----------
    def _expand_collection(self, collection_id: int) -> None:
        if self._col_worker is not None:
            return      # 一次只查一个合集，防止连点
        # 联网互斥闸：与更新检测等共用一个名额
        busy = netGate.try_acquire(constants.NET_GATE_COLLECTION)
        if busy is not None:
            self._log.warn(
                f"展开合集未开始：{busy} 正在使用联网查询")
            QMessageBox.information(
                self, "展开合集",
                f"另有联网任务在进行（{busy}），稍后再试。")
            return
        self._col_worker = _CollectionWorker(
            collection_id,
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._col_worker.succeeded.connect(self._on_collection_children)
        self._col_worker.failed.connect(self._on_collection_failed)
        self._col_worker.finished.connect(self._on_col_worker_finished)
        self._col_worker.start()

    def _on_collection_failed(self, message: str) -> None:
        QMessageBox.warning(self, "查询失败",
                            f"无法获取合集成员：{message}")

    def _on_col_worker_finished(self) -> None:
        """合集查询线程跑完的统一收尾（理由同 _on_worker_finished）。"""
        w = self._col_worker
        self._col_worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_COLLECTION)

    def _on_collection_children(self, collection_id: int,
                                children: list[int]) -> None:
        if not children:
            self._log.warn(
                f"编号 {collection_id} 不是合集（查不到成员）")
            QMessageBox.information(
                self, "不是合集",
                f"编号 {collection_id} 查不到任何成员——它不是合集，"
                "或者远端数据异常。\n如果你确认它应该是普通 mod，"
                "重新检测一次即可补全信息。")
            return
        # 归属"开始检测时的档案"，不是当前档案——检测中切过档案的话，
        # 当前的 self._game 已经是别的游戏了，用它会把这些 mod 记错家
        game = self._check_game
        if game is None:
            return
        existing = {m.mod_id
                    for m in self._repo.list_mods(game.app_id,
                                                  order_by="mod_id")}
        new_ids = [i for i in children if i not in existing]
        preview = "、".join(str(i) for i in children[:20]) \
            + ("…" if len(children) > 20 else "")
        ret = QMessageBox.question(
            self, "展开合集",
            f"该合集包含 {len(children)} 个条目：\n{preview}\n\n"
            f"其中 {len(new_ids)} 个尚未入库。确认把它们登记到当前档案"
            f"「{game.name}」名下吗？（状态：待下载）")
        if ret != QMessageBox.StandardButton.Yes:
            return
        now = int(time.time())
        with self._repo.transaction():
            for mid in new_ids:
                self._repo.add_mod(Mod(
                    mod_id=mid,
                    game_id=game.app_id,
                    status="tracked",
                    url=WORKSHOP_URL_TEMPLATE.format(mid),
                    title=None,
                    creator=None,
                    file_size=None,
                    time_updated=None,
                    first_tracked_at=now))
        self._log.ok(f"合集 {collection_id} 展开：新登记 "
                     f"{len(new_ids)} 个条目（待下载）")
        QMessageBox.information(
            self, "已登记",
            f"已登记 {len(new_ids)} 个新条目（状态：待下载）。\n"
            "点【开始检测】即可为它们补全标题等信息。")
        # 参数 0 = 没有新版本，只是让主窗口刷新一下 mod 库页
        self.checks_finished.emit(0)
