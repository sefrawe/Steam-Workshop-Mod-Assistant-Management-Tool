"""快速命令查询页 """
r"""gui/quickCommandPage.py · 贴任意工坊网址/编号/命令行 → 生成归属
正确的下载命令。原 QuickCommandDialog 整编为独立页（导航：下载与备份
组，紧挨命令生成页）：判定边界、离线生成、互斥闸、审计记账全部照旧，
只换壳——QDialog→QWidget、关窗取消改 shutdown()（页面循环自动发现）。

【吞掉第三方下载站的核心用法】steamworkshopdownloader.io 一类网站
做的事：给一个工坊链接，还你一条 workshop_download_item 命令。本页
用官方接口 GetPublishedFileDetails 做同一件事，而且多还一样东西——
每条返回自带的 consumer_app_id（条目所属游戏）：
    workshop_download_item <条目自己的游戏AppID> <编号>
与账本无关、与当前界面显示的游戏无关：混着贴，各生成各的命令。

【判定边界（如实约束，绝不猜）】
- result≠1（接口看不见条目）：拿不到 consumer_app_id，无法生成
  命令——可能已失效也可能仍可下载，说明列如实注明；
- result=1 但 consumer_app_id 缺失（极少见）：同样不生成；
- 联网失败：本次给不出任何命令——绝不拿当前档案的 AppID 瞎猜；
- 【离线生成】= 把"猜归属"变成用户的显式选择：确认弹窗说清风险、
  默认停在取消。

【与终端的咬合】复制出的命令粘到 控制台 → steamcmd 终端 回车即可：
命令自带 AppID，批次按它归属；批次收尾自动盘点，盘上内容进该档案
的待认领区，到【入账中心】勾选确认转正。命令已按游戏分组排序。

【纪律】只读不写业务账（复制记 operations_log 审计）；查询走
QThread，等不到 park 保活（0xC0000409 老坑）；联网互斥闸 netGate。
"""
import threading

from core import constants
from core import netGate
from core import urlParser
from core.appSettings import AppSettings
from core.commandBuilder import build_plain_commands
from core.steamApiClient import SteamApiCancelled, SteamApiClient, SteamApiError
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from gui.logBus import LogBus
from gui.theme import font_px  # 字号单源（D25）


class _QueryWorker(QThread):
    """查询线程：客户端自动按 100/批分批限速；取消事件置位后
    等待与重试立即中断（steamApiClient 的 v2.45 协议）。"""
    succeeded = Signal(list)   # list[WorkshopItem]
    failed = Signal(str)

    def __init__(self, mod_ids: list[int], *, interval_ms: int,
                 max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._cancel = threading.Event()

    def stop(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        client = SteamApiClient(
            interval_ms=self._interval_ms, max_retries=self._max_retries,
            cancel_event=self._cancel)
        try:
            items = client.query_details(self._mod_ids)
        except SteamApiCancelled:
            return  # 关窗/退出取消：静默收场
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(items)


class QuickCommandPage(QWidget):
    """快速命令查询页：贴 → 解析 → 联网查归属 → 生成命令 → 复制。"""

    def __init__(self, repo, settings: AppSettings,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None, game=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._game = game  # 当前档案（离线生成的 AppID 来源）
        self._worker: _QueryWorker | None = None
        self._commands: list[str] = []   # 复制用（已按游戏分组排序）
        self._invalid: list[str] = []    # 认不出的原始片段
        self._net_held = False           # 联网闸持有标志（防双放）
        self._dup_counts: dict[int, int] = {}
        self._line_app_of: dict[int, int] = {}
        self._build_ui()

    def set_game(self, game) -> None:
        """档案广播（MainWindow 循环自动发现）：离线生成的 AppID
        来源随当前档案走。"""
        self._game = game

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        title = QLabel("快速命令查询", self)
        title.setStyleSheet(
            f"font-size: {font_px(18)}px; font-weight: 600;")
        v.addWidget(title)
        hint = QLabel(
            "每行贴一个，三种随便混：工坊网址 / 纯编号 / 整行下载命令"
            "（可多游戏混杂）。查询后按每个条目自己的游戏生成命令——"
            "与当前界面显示的游戏、与账本都无关；命令行里的旧 AppID "
            "以重查结果为准（填错的会被纠正）。\n"
            "复制后粘到 控制台 → steamcmd 终端 回车执行：批次按命令"
            "自带的 AppID 归属；批次收尾自动盘点，盘上内容进该档案的"
            "待认领区，到【入账中心】勾选确认。命令已按游戏分组，"
            "一个批次只服务一个游戏——逐游戏复制粘贴最稳。", self)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: gray; font-size: {font_px(12)}px;")
        v.addWidget(hint)

        self._input = QPlainTextEdit(self)
        self._input.setPlaceholderText(
            "例如：\n"
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3563882422\n"
            "3563882422\n"
            "workshop_download_item 1158310 3563882422")
        self._input.setMaximumHeight(90)
        v.addWidget(self._input)

        row = QHBoxLayout()
        self._btn_go = QPushButton("解析并查询", self)
        self._btn_go.setDefault(True)
        self._btn_go.clicked.connect(self._start)
        row.addWidget(self._btn_go)
        self._btn_offline = QPushButton("离线生成（当前档案游戏）", self)
        self._btn_offline.setToolTip(
            "不联网，直接按当前档案的游戏 AppID 给全部编号出命令"
            "——网络不畅时的兜底。贴进来的条目若属于别的游戏会"
            " No match，点下去前有确认弹窗说清风险")
        self._btn_offline.clicked.connect(self._on_offline)
        row.addWidget(self._btn_offline)
        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        row.addWidget(self._status, 1)
        v.addLayout(row)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(
            ["编号", "标题", "所属游戏 AppID", "说明"])
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        for col, w in ((0, 110), (2, 130), (3, 220)):
            self._table.setColumnWidth(col, w)
        v.addWidget(self._table, 1)

        self._output = QPlainTextEdit(self)
        self._output.setReadOnly(True)
        self._output.setPlaceholderText(
            "查询成功后，这里显示可粘贴进 steamcmd 的命令")
        self._output.setMaximumHeight(110)
        v.addWidget(self._output)

        bottom = QHBoxLayout()
        self._btn_copy = QPushButton("复制命令", self)
        self._btn_copy.setEnabled(False)
        self._btn_copy.clicked.connect(self._on_copy)
        bottom.addStretch(1)
        bottom.addWidget(self._btn_copy)
        v.addLayout(bottom)

    # ---------------- 查询 ----------------
    def _start(self) -> None:
        if self._worker is not None:
            return
        text = self._input.toPlainText()
        if not text.strip():
            self._set_status("先在上面贴点东西：网址 / 编号 / 命令行均可。", "")
            return
        report = urlParser.parse_lines(text.splitlines())
        if not report.mod_ids:
            msg = "没认出任何编号。"
            if report.invalid:
                msg += "认不出的片段：" + "、".join(report.invalid[:5])
            self._set_status(msg, "color: #e5484d;")
            return
        busy = netGate.try_acquire(constants.NET_GATE_QUICK_QUERY)
        if busy is not None:
            self._set_status(
                f"另有联网任务在进行（{busy}），等它结束再试。",
                "color: #f5a623;")
            return
        self._net_held = True
        self._invalid = list(report.invalid)
        self._dup_counts, self._line_app_of = self._analyze_lines(text)
        self._set_status(f"已解析 {len(report.mod_ids)} 个编号，正在查询…", "")
        self._btn_go.setEnabled(False)
        self._worker = _QueryWorker(
            report.mod_ids,
            interval_ms=self._settings.get_int("api_request_interval_ms", 200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._worker.succeeded.connect(self._on_ok)
        self._worker.failed.connect(self._on_fail)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @staticmethod
    def _analyze_lines(text: str) -> tuple[dict[int, int], dict[int, int]]:
        """输入侧体检：逐行复用同一解析器（单源）统计——① 每个编号
        出现在几行里 → 重复提醒；② 命令行自带 AppID 记到该行编号名下
        → 供重查比对，错配时明说并纠正。"""
        dup: dict[int, int] = {}
        line_app: dict[int, int] = {}
        for ln in text.splitlines():
            rep = urlParser.parse_lines([ln])
            for mid in rep.mod_ids:
                dup[mid] = dup.get(mid, 0) + 1
            apps = getattr(rep, "command_app_ids", None) or []
            if apps:
                for mid in rep.mod_ids:
                    line_app[mid] = apps[0]
        return dup, line_app

    def _on_ok(self, items: list) -> None:
        rows: list[dict] = []
        pairs: list[tuple[int, int]] = []
        n_invisible = n_noapp = n_mismatch = n_cross = 0
        n_dup = sum(1 for n in self._dup_counts.values() if n > 1)
        cur_app = self._game.app_id if self._game is not None else None
        for it in items:
            mid, title = it.mod_id, it.title or "（无标题）"
            notes: list[str] = []
            row_app = it.consumer_app_id if it.result == 1 else None
            if it.result == 1 and it.consumer_app_id:
                pairs.append((it.consumer_app_id, mid))
                notes.append("✓ 已生成命令")
                src = self._line_app_of.get(mid)
                if src is not None and src != it.consumer_app_id:
                    n_mismatch += 1
                    notes.insert(0, f"⚠ 原命令 AppID {src} 与条目实际"
                                    f"所属不符，已纠正为 {it.consumer_app_id}")
                if cur_app is not None and it.consumer_app_id != cur_app:
                    n_cross += 1
                    notes.append(f"属其他游戏（非当前档案"
                                 f"「{self._game.name}」）")
            elif it.result == 1:
                n_noapp += 1
                notes.append("接口没返回所属游戏，无法生成命令（极少见）")
            else:
                n_invisible += 1
                notes.append(
                    f"Steam 接口看不见（result={it.result}）——多半已"
                    "下架/删除或从未存在；少数情况是仍可下载的隐藏"
                    "条目。未生成命令，可到【异常处置】页核实")
            n_seen = self._dup_counts.get(mid, 0)
            if n_seen > 1:
                notes.append(f"输入中重复出现 {n_seen} 次（已合并）")
            rows.append({"mid": mid, "title": title, "app": row_app,
                         "note": "；".join(notes)})
        # 表格行序 = 命令行序（按游戏分组、组内按编号）；查不到的垫底
        rows.sort(key=lambda r: ((0, r["app"], r["mid"]) if r["app"]
                                 else (1, 0, r["mid"])))
        self._commands = []
        apps = sorted({a for a, _ in pairs})
        for app in apps:
            self._commands.extend(build_plain_commands(
                app, sorted(m for a, m in pairs if a == app)))
        self._fill_table(rows)
        self._output.setPlainText(
            "\n".join(self._commands) + "\n" if self._commands else "")
        self._btn_copy.setEnabled(bool(self._commands))
        extras = []
        if n_invisible:
            extras.append(f"接口看不见 {n_invisible}")
        if n_noapp:
            extras.append(f"缺归属 {n_noapp}")
        if self._invalid:
            extras.append(f"认不出 {len(self._invalid)}")
        if n_mismatch:
            extras.append(f"错配已纠正 {n_mismatch}")
        if n_cross:
            extras.append(f"跨档案 {n_cross}")
        if n_dup:
            extras.append(f"输入重复 {n_dup}")
        tail = ("；另有：" + "、".join(extras)) if extras else ""
        self._set_status(
            f"查询完成：生成 {len(self._commands)} 条命令，"
            f"涉及 {len(apps)} 个游戏{tail}", "")
        self._log.ok(f"快速命令查询：生成 {len(self._commands)} 条命令"
                     f"（涉及 {len(apps)} 个游戏）{tail}")
        if n_mismatch:
            self._log.warn(f"{n_mismatch} 条命令行里的 AppID 与条目实际"
                           "所属不符，已按重查结果纠正（明细见表格说明列）")
        if self._invalid:
            self._log.warn("认不出的片段（原样保留未处理）："
                           + "、".join(self._invalid[:8]))

    def _on_fail(self, message: str) -> None:
        self._set_status(
            f"联网查询失败：{message}\n"
            "（本次给不出命令——绝不拿当前档案的 AppID 猜归属，"
            "猜错的命令就是 No match。）", "color: #e5484d;")
        self._log.error(f"快速命令查询失败：{message}")

    def _on_offline(self) -> None:
        """【离线生成】：不联网，按当前档案的 AppID 出命令。确认弹窗
        说清风险、默认停在取消。纯本地路径，与联网查询互不干扰。"""
        if self._worker is not None:
            return
        if self._game is None:
            self._set_status(
                "离线生成需要知道游戏：请先在左上角选择档案；"
                "或者联网查询（那条路不需要档案）。", "color: #f5a623;")
            return
        text = self._input.toPlainText()
        if not text.strip():
            self._set_status("先在上面贴点东西：网址 / 编号 / 命令行均可。", "")
            return
        report = urlParser.parse_lines(text.splitlines())
        if not report.mod_ids:
            msg = "没认出任何编号。"
            if report.invalid:
                msg += "认不出的片段：" + "、".join(report.invalid[:5])
            self._set_status(msg, "color: #e5484d;")
            return
        app_id = self._game.app_id
        ret = QMessageBox.question(
            self, "离线生成（不联网）",
            f"不联网核实，直接按当前档案「{self._game.name}」"
            f"（AppID {app_id}）给全部 {len(report.mod_ids)} 个编号出"
            "命令？\n\n风险（离线 = 靠假定，不靠事实）：\n"
            "· 贴进来的条目若其实属于别的游戏 → steamcmd 报 No match；\n"
            "· 命令行里原带的游戏 AppID 会被当前档案的覆盖。\n\n"
            "网络通畅时更推荐【解析并查询】——按条目自己的游戏出命令。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._invalid = list(report.invalid)
        self._dup_counts, self._line_app_of = self._analyze_lines(text)
        ids = sorted(report.mod_ids)
        self._commands = build_plain_commands(app_id, ids)
        rows = []
        for mid in ids:
            note = f"离线生成——假定属于当前档案 {self._game.name}"
            n_seen = self._dup_counts.get(mid, 0)
            if n_seen > 1:
                note += f"；输入中重复出现 {n_seen} 次（已合并）"
            rows.append({"mid": mid, "title": "（离线·未查标题）",
                         "app": app_id, "note": note})
        self._fill_table(rows)
        self._output.setPlainText("\n".join(self._commands) + "\n")
        self._btn_copy.setEnabled(True)
        self._set_status(
            f"离线生成 {len(self._commands)} 条命令（AppID {app_id}，"
            "未联网核实）——贴进 steamcmd 后若见 No match，说明条目"
            "属于别的游戏，联网重查即可纠正。", "")
        self._log.warn(
            f"快速命令查询（离线）：按当前档案「{self._game.name}」生成 "
            f"{len(self._commands)} 条命令（未联网核实归属）")

    def _on_finished(self) -> None:
        # 线程收尾纪律：摘引用置 None 后再 wait（0xC0000409 老坑）
        w = self._worker
        self._worker = None
        self._btn_go.setEnabled(True)
        if w is not None:
            w.wait()
        self._release_net()

    # ---------------- 收尾（闸单源 + 退出守卫） ----------------
    def _release_net(self) -> None:
        if self._net_held:
            self._net_held = False
            netGate.release(constants.NET_GATE_QUICK_QUERY)

    def shutdown(self) -> None:
        """退出收尾（MainWindow.closeEvent 页面循环自动发现）：查询
        在跑就请求取消、等 2 秒；在途请求最长约 40 秒，等不到 park
        保活——绝不销毁活线程。闸释放走 _release_net 单源。"""
        w = self._worker
        if w is not None:
            w.stop()
            if not w.wait(2000):
                netGate.park(w)
            self._worker = None
        self._release_net()

    # ---------------- 展示与复制 ----------------
    def _fill_table(self, rows: list[dict]) -> None:
        t = self._table
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(str(r["mid"])))
            t.setItem(i, 1, QTableWidgetItem(r["title"]))
            t.setItem(i, 2, QTableWidgetItem(
                str(r["app"]) if r["app"] else "—"))
            ni = QTableWidgetItem(r["note"])
            ni.setToolTip(r["note"])
            t.setItem(i, 3, ni)

    def _set_status(self, text: str, color: str) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(color)

    def set_input_text(self, text: str) -> None:
        """外部预填输入框（向导类页面预留）：只填不跑。"""
        self._input.setPlainText(text)

    def _on_copy(self) -> None:
        if not self._commands:
            return
        text = "\n".join(self._commands) + "\n"
        QApplication.clipboard().setText(text)
        self._log.ok(f"已复制 {len(self._commands)} 条下载命令，"
                     "去 steamcmd 终端粘贴回车即可")
        # 记账与命令生成页同款：operations_log 存完整命令原文
        try:
            op_id = self._repo.add_operation(text)
            self._repo.finish_operation(op_id, result="generated")
        except Exception as exc:
            self._log.error(f"写 operations_log 失败（复制本身不受影响）：{exc}")
