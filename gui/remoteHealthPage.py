"""远端健康
"""
r"""gui/remoteHealthPage.py · 桶A 远端健康（D24 拆分页之一）。

标题不符 / 弃坑 / banned 三类远端信号，匿名接口一次查询判定，
无需 API key。与异常处置页【联网深度检测】同一种匿名批量查询——
共用 netGate 的「深度检测」闸（互斥正确：同一时刻只有一路匿名
批量查询在跑）与 RemoteQueryWorker。

检测结果是一次网络快照：切档案清空；页面切换不清（与异常处置页
深检结果同一口径）。查询顺带的失效/查询失败条目不在本页重复报
——归【异常处置】页（页内给计数与指路）。
"""
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QAction, QColor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget, QLineEdit, QTableWidget, QAbstractItemView, QHeaderView, QTableWidgetItem,
)

from core import constants, netGate  # ★ 待核：netGate 若在 gui 包改这行
from core.models import Game
from core.steamApiClient import SteamApiClient
from core.urlParser import workshop_url
from core.i18n import t
from gui.logBus import LogBus
from gui.remoteQueryWorker import RemoteQueryWorker, workshop_item_to_entry
from gui.theme import font_px  # 字号单源（D25）
from workflows import exceptionFlow
from gui.collapsibleSection import make_guide  # 先读我单源（共享件内）

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"
_C_MUTED = "#8a8a8f"


class RemoteHealthPage(QWidget):
    """桶A：标题不符（黄）/ 弃坑（红）/ banned（红）。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()
        self._game: Game | None = None
        self._worker = None
        self._mods_by_id: dict[int, object] = {}
        self._owner_game: Game | None = None   # 结果归属档案（行菜单用）
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
        self.root_layout = root

        title = QLabel(t("远端健康"), self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)
        root.addWidget(make_guide(
            t("先读我：三类远端信号 · 都是提醒不是损坏"),
            (t("与账本记录对不上的三类远端信号（匿名接口可判，无需 API "
             "key）：标题不符 = 作者改名或上传内容被顶替的信号（黄）；"
             "标题含弃坑关键词 = 作者明示不再维护（红）；banned=1 = 被 "
             "Steam 封禁（红）。"),
             t("都是提醒不是损坏——动作是打开工坊页面看一眼，再决定去留。")),
            self, 120))


        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn = QPushButton(t("开始检测"), row)
        self._btn.setToolTip(
            t("匿名接口重查库内全部未删除 mod 的远端实况，判定三类健康"
            "信号；每 100 个一批、礼貌限速，耗时取决于 mod 数量"))
        self._btn.clicked.connect(self._start_check)
        h.addWidget(self._btn)
        self._stop_btn = QPushButton(t("停止"), row)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_check)
        h.addWidget(self._stop_btn)
        self._progress = QProgressBar(row)
        self._progress.setVisible(False)
        h.addWidget(self._progress, 1)
        root.addWidget(row)
        # 页面级查找（依赖检测/备份页同款）：三张结果表联动过滤——
        # 只隐藏不清数据，清空恢复；勾选不受影响
        srow = QWidget(body)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel(t("查找："), srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            t("按标题 / 编号 / 说明过滤三张结果表（即时，只隐藏；清空"
            "恢复）…"))
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
        self._sec_mismatch = self._make_section(
            t('标题不符（本地 → 远端）'),
            t('作者改名或上传内容被顶替的信号：打开工坊页面对照一眼，内容确实换了再决定去留。版本号差异已自动忽略。'),
            _C_WARN)
        self._sec_abandoned = self._make_section(
            t('标题含弃坑关键词'),
            t('作者明示不再维护：还能用就继续用，出问题再找替代。'),
            _C_FAIL)
        self._sec_banned = self._make_section(
            t('banned=1（被 Steam 封禁）'),
            t('通常不可再下载，处置参考【异常处置】页桶④三步。'),
            _C_FAIL)

        self._note = QLabel("", self)
        self._note.setWordWrap(True)
        root.addWidget(self._note)
        root.addStretch(1)

    def _make_section(self, heading: str, note: str,
                      color: str = "#8a8a8f") -> tuple:
        """一个结果区：说明行 + 结果表（备份页同款表格化：编号/标题
        /说明分列——黏一行分不清的教训；勾选列 = 复制勾选编号的
        对象来源）。返回（说明标签, 表格）。"""
        lbl = QLabel(heading + "——" + note, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray;")
        self.root_layout.addWidget(lbl)
        tbl = QTableWidget(0, 4, self)
        tbl.setHorizontalHeaderLabels([t('选'), t('编号'), t('标题'), t('说明')])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        tbl.setWordWrap(False)
        th = tbl.horizontalHeader()
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        th.setSectionsClickable(True)  # 表头可点排序（SortItem）
        for col, width in ((0, 34), (1, 110), (2, 240)):
            tbl.setColumnWidth(col, width)
        tbl.setMinimumHeight(150)
        tbl.setProperty("note_color", color)  # 说明列上色（_fill_list 读）
        tbl.itemDoubleClicked.connect(self._on_row_dclicked)
        tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tbl.customContextMenuRequested.connect(
            lambda pos, w=t: self._on_row_menu(w, pos))
        th.sortIndicatorChanged.connect(lambda *_a: self._apply_filter())
        tbl.setVisible(False)
        self.root_layout.addWidget(tbl)
        return lbl, tbl

    # ---------- 对外 ----------
    def set_game(self, game: Game | None) -> None:
        if self._worker is not None:
            # 检测进行中：只更新标签，不动结果区（归属开始时的档案）。
            # self._game 必须同步跟上：检测收尾后，下一次检测才不会
            # 拿旧档案当新档案跑（修24，与依赖检测/异常处置页同族）。
            self._game = game
            self._game_label.setText(
                (t('当前游戏：') + f"{game.name}" + '（' + f"{game.app_id}" + '）')
                if game is not None else t('当前游戏：（未选择）'))
            return
        self._game = game
        self._clear_results()
        if game is None:
            self._game_label.setText(
                t("当前游戏：（未选择）—— 请先在左上角添加或选择档案"))
            self._btn.setEnabled(False)
        else:
            self._game_label.setText(
                t('当前游戏：') + f"{game.name}" + f'（' + f"{game.app_id}" + f'）')
            self._btn.setEnabled(True)

    def shutdown(self) -> None:
        """退出收尾：等 1.5 秒，等不到交 netGate.park 保活（线程只读
        网络不写库，零数据损失）——v2.45 口径。"""
        if self._worker is not None:
            self._worker.stop()
            if not self._worker.wait(1500):
                netGate.park(self._worker)
            self._worker = None

    # ---------- 检测 ----------
    def _start_check(self) -> None:
        if self._game is None or self._worker is not None:
            return
        mods = self._repo.list_mods(self._game.app_id)
        self._mods_by_id = {m.mod_id: m for m in mods}
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn(t("当前档案没有可检测的 mod（已删除的除外）"))
            return
        busy = netGate.try_acquire(constants.NET_GATE_DEEP_CHECK)
        if busy is not None:
            self._summary.setText(
                t('另有联网任务在进行（') + f"{busy}" + t('）：等它结束后再检测。'))
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(t('远端健康检测未开始：') + f"{busy}" + t(' 正在使用联网查询'))
            return
        self._owner_game = self._game
        self._titles = {m.mod_id: (m.title or "") for m in mods}
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
            return lambda batch: client.query_details(batch)

        self._worker = RemoteQueryWorker(
            ids, make_query=make_query, to_entry=workshop_item_to_entry,
            interval_ms=interval, max_retries=retries, parent=self)
        self._worker.batch_done.connect(self._progress.setValue)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()
        self._log.info(t('远端健康检测开始：') + f"{len(ids)}" + t(' 个 mod（匿名接口，只读不写库）…'))

    def _stop_check(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._stop_btn.setEnabled(False)
            self._summary.setText(
                t("已请求停止：等待与重试已中断，最多再等一次在途请求收场。"))
            self._summary.setStyleSheet("color: gray;")
            self._log.info(t('已请求停止远端健康检测'))

    # ---------- 回调 ----------
    def _on_succeeded(self, entries: list) -> None:
        self._set_running(False)
        health = exceptionFlow.classify_health(
            entries, self._titles,
            keywords_raw=(self._settings.get("remote_title_warn_keywords")
                          if self._settings is not None else None))
        skipped = exceptionFlow.classify_entries(entries)   # 失效/查询失败计数（纯函数复用）
        cross = len(skipped.invalid) + len(skipped.query_failed)
        shown = self._fill_list(self._sec_mismatch[1], [
            (mid, self._title_of(mid), (t('远端标题：') + f"{rt}"))
            for mid, local, rt in health.title_mismatch])
        self._fill_list(self._sec_abandoned[1], [
            (mid, self._title_of(mid), t('标题含弃坑关键词'))
            for mid in health.abandoned])
        self._fill_list(self._sec_banned[1], [
            (mid, self._title_of(mid), t('banned=1（被 Steam 封禁）'))
            for mid in health.banned])
        n = health.total
        if n == 0:
            self._summary.setText(t("无异常（远端标题与账本一致，无弃坑/"
                                  "banned 信号）。"))
            self._summary.setStyleSheet(f"color: {_C_OK};")
        else:
            parts = ((t('标题不符 ') + f"{len(health.title_mismatch)}" + t(' · 弃坑 ') + f"{len(health.abandoned)}" + ' · banned ' + f"{len(health.banned)}"))
            self._summary.setText(parts)
            self._summary.setStyleSheet(f"color: {_C_WARN};")
        extra = []
        if cross:
            extra.append(t('另有 ') + f"{cross}" + t(' 条远端失效/查询失败，归【异常处置】页管'))
        if health.missing_banned_field:
            extra.append(t('本次响应未携带 banned 字段：banned 检测不可用（Steam 原始响应里有它）——把日志发给开发者'))
        self._note.setText("。\n".join(extra) + ("。" if extra else ""))
        self._log.ok((t('远端健康检测完成：') + f"{n}" + t(' 处信号'))
                     + ((t('；另有 ') + f"{cross}" + t(' 条失效/查询失败（见异常处置页）'))
                        if cross else ""))

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self._summary.setText(
            t('检测失败：') + f"{message}" + t('\n（本地数据未动，稍后可重试。可以参考 watt toolkit 的连通性测试，若一直不通过，可以尝试关闭 watt toolkit 再试。）'))
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(t('远端健康检测失败：') + f"{message}")

    def _on_stopped(self) -> None:
        self._set_running(False)
        self._summary.setText(t("已停止，本次结果未使用。"))
        self._summary.setStyleSheet("color: gray;")
        self._log.info(t('远端健康检测已手动停止'))

    def _on_finished(self) -> None:
        """线程收尾：先取引用置 None 再 wait（0xC0000409 坑纪律）；
        闸在 finished 里归还——无论成功/失败/停止都必发。"""
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()
        netGate.release(constants.NET_GATE_DEEP_CHECK)

    # ---------- 小件 ----------
    def _set_running(self, running: bool) -> None:
        self._btn.setEnabled(not running and self._game is not None)
        self._stop_btn.setEnabled(running)
        self._progress.setVisible(running)
    def _clear_results(self) -> None:
        self._filter_edit.clear()
        for sec in (self._sec_mismatch, self._sec_abandoned,
                    self._sec_banned):
            sec[1].setRowCount(0)
            sec[1].setVisible(False)
        self._summary.setText("")
        self._note.setText("")
    def _fill_list(self, tbl, rows) -> int:
        """结果表灌装（灌表期关排序、灌完再开）。行 = (编号, 标题, 说明)；
        勾选列默认全不勾；说明列按 section 级别上色。"""
        tbl.setSortingEnabled(False)  # 带排序灌数据=边灌边排、行错位
        tbl.setRowCount(len(rows))
        color = tbl.property("note_color")
        for r, (mid, title, note) in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, mid)
            from gui.tableSort import SortItem
            c1 = SortItem(str(mid), mid)
            c1.setData(Qt.ItemDataRole.UserRole, mid)
            c3 = SortItem(note, note)
            if color:
                c3.setForeground(QColor(color))
            for c, cell in enumerate((chk, c1, SortItem(title, title), c3)):
                tbl.setItem(r, c, cell)
        tbl.setSortingEnabled(True)  # 灌完再开；点过的排法自动重放
        tbl.setVisible(bool(rows))
        return len(rows)


    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or t('（无标题）')
    def _on_row_menu(self, tbl, pos) -> None:
        """结果表右键：勾选总控 + 单行动作（光标下有行才有）+ 勾选
        批动作。三张表共用（一个动作一个定义）。"""
        row = tbl.rowAt(pos.y())
        mid = None
        if row >= 0:
            it = tbl.item(row, 1)
            mid = it.data(Qt.ItemDataRole.UserRole) \
                if it is not None else None
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        a_all = QAction(t("全选（可见行）"), menu)
        a_all.setToolTip(t("只勾选当前看得见的行——查找过滤在场时不盲勾"))
        a_all.triggered.connect(lambda _=False, w=tbl:
                                self._check_all_visible(w))
        menu.addAction(a_all)
        a_none = QAction(t("清空勾选"), menu)
        a_none.triggered.connect(lambda _=False, w=tbl:
                                 self._uncheck_all(w))
        menu.addAction(a_none)
        if mid is not None:
            menu.addSeparator()
            act_url = QAction(t("打开工坊页面"), menu)
            act_url.setToolTip(t("在浏览器打开该 mod 的创意工坊页面"))
            act_url.triggered.connect(lambda _=False, m=mid:
                                      self._open_url(m))
            menu.addAction(act_url)
            act_dir = QAction(t("打开 mod 文件夹"), menu)
            act_dir.triggered.connect(lambda _=False, m=mid:
                                      self._open_folder(m))
            menu.addAction(act_dir)
            act_copy = QAction(t("复制编号"), menu)
            act_copy.triggered.connect(
                lambda _=False, m=mid: (
                    QApplication.clipboard().setText(str(m)),
                    self._log.info(t('已复制编号 ') + f"{m}")))
            menu.addAction(act_copy)
        menu.addSeparator()
        a3 = QAction(t("复制勾选行的编号"), menu)
        a3.setToolTip(t("先勾选若干行再点（每行一个）"))
        a3.triggered.connect(lambda _=False, w=tbl: self._copy_checked(w))
        menu.addAction(a3)
        menu.exec(tbl.viewport().mapToGlobal(pos))

    def _on_row_dclicked(self, item) -> None:
        """双击行 = 打开该 mod 的工坊页面。"""
        tbl = item.tableWidget()
        it = tbl.item(item.row(), 1)
        mid = it.data(Qt.ItemDataRole.UserRole) \
            if it is not None else None
        if mid is not None:
            self._open_url(mid)
    def _check_all_visible(self, tbl) -> None:
        """全选只勾可见行（备份页同款）；清空仍作用所有行。"""
        for r in range(tbl.rowCount()):
            if tbl.isRowHidden(r):
                continue
            it = tbl.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Checked)

    def _uncheck_all(self, tbl) -> None:
        for r in range(tbl.rowCount()):
            it = tbl.item(r, 0)
            if it is not None:
                it.setCheckState(Qt.CheckState.Unchecked)


    def _copy_checked(self, tbl) -> None:
        ids: list[int] = []
        for r in range(tbl.rowCount()):
            it = tbl.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                mid = it.data(Qt.ItemDataRole.UserRole)
                if mid is not None and int(mid) not in ids:
                    ids.append(int(mid))
        if not ids:
            QMessageBox.information(self, t("复制勾选行"),
                                    t("先在列表里勾选若干行。"))
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in sorted(ids)) + "\n")
        self._log.ok(t('已复制勾选的 ') + f"{len(ids)}" + t(' 个编号（每行一个）'))
    def _apply_filter(self) -> None:
        """页面级查找落地：三张结果表联动（编号/标题/说明匹配）。
        只隐藏不清数据；清空全恢复；勾选不受影响。幂等。"""
        kw = self._filter_edit.text().strip().casefold()
        for sec in (self._sec_mismatch, self._sec_abandoned, self._sec_banned):
            tbl = sec[1]
            for r in range(tbl.rowCount()):
                if not kw:
                    tbl.setRowHidden(r, False)
                    continue
                hay = " ".join(
                    tbl.item(r, c).text() for c in (1, 2, 3)
                    if tbl.item(r, c) is not None).casefold()
                tbl.setRowHidden(r, kw not in hay)


    def _open_url(self, mid: int) -> None:
        m = self._mods_by_id.get(mid)
        # 网址现拼走唯一入口 workshop_url。此前直接 .format(mid) 填进
        # constants 里那份 {mod_id} 命名占位模板——右键打开页面当场
        # KeyError，即本次修复的根因
        url = workshop_url(mid, m.url if m is not None else None)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, t("打开页面"), t('浏览器没有响应，请手动打开：\n') + f"{url}")

    def _open_folder(self, mid: int) -> None:
        from gui.modFolderOpener import open_mod_folder
        m = self._mods_by_id.get(mid)
        if m is None or self._owner_game is None:
            return
        open_mod_folder(self, self._owner_game, m, log=self._log)
