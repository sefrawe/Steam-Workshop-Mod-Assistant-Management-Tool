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
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QAction
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from core.urlParser import workshop_url

from core import constants, netGate   # ★ 待核：netGate 若在 gui 包改这行
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.logBus import LogBus
from gui.remoteQueryWorker import RemoteQueryWorker, workshop_item_to_entry
from workflows import exceptionFlow
from gui.theme import font_px  # 字号单源（D25）

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

        title = QLabel("远端健康", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        tip = QLabel(
            "与账本记录对不上的三类远端信号（匿名接口可判，无需 API "
            "key）：标题不符 = 作者改名或上传内容被顶替的信号（黄）；"
            "标题含弃坑关键词 = 作者明示不再维护（红）；banned=1 = 被 "
            "Steam 封禁（红）。都是提醒不是损坏——动作是打开工坊页面"
            "看一眼，再决定去留。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn = QPushButton("开始检测", row)
        self._btn.setToolTip(
            "匿名接口重查库内全部未删除 mod 的远端实况，判定三类健康"
            "信号；每 100 个一批、礼貌限速，耗时取决于 mod 数量")
        self._btn.clicked.connect(self._start_check)
        h.addWidget(self._btn)
        self._stop_btn = QPushButton("停止", row)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_check)
        h.addWidget(self._stop_btn)
        self._progress = QProgressBar(row)
        self._progress.setVisible(False)
        h.addWidget(self._progress, 1)
        root.addWidget(row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)
        self._sec_mismatch = self._make_section(
            "标题不符（本地 → 远端）",
            "作者改名或上传内容被顶替的信号：打开工坊页面对照一眼，"
            "内容确实换了再决定去留。版本号差异已自动忽略。")
        self._sec_abandoned = self._make_section(
            "标题含弃坑关键词",
            "作者明示不再维护：还能用就继续用，出问题再找替代。")
        self._sec_banned = self._make_section(
            "banned=1（被 Steam 封禁）",
            "通常不可再下载，处置参考【异常处置】页桶④三步。")
        self._note = QLabel("", self)
        self._note.setWordWrap(True)
        root.addWidget(self._note)
        root.addStretch(1)
    def _make_section(self, heading: str, note: str) -> tuple:
        """一个结果区：说明行 + 列表，装进滚动内容区（root_layout）。
        返回（说明标签, 列表）。"""
        lbl = QLabel(heading + "——" + note, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray;")
        self.root_layout.addWidget(lbl)
        lw = QListWidget(self)
        lw.setWordWrap(False)
        lw.setAlternatingRowColors(True)
        lw.setFixedHeight(180)
        lw.setVisible(False)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lw.customContextMenuRequested.connect(
            lambda pos, w=lw: self._on_row_menu(w, pos))
        self.root_layout.addWidget(lw)
        return lbl, lw


    # ---------- 对外 ----------
    def set_game(self, game: Game | None) -> None:
        if self._worker is not None:
            # 检测进行中：只更新标签，不动结果区（归属开始时的档案）
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
            self._log.warn("当前档案没有可检测的 mod（已删除的除外）")
            return
        busy = netGate.try_acquire(constants.NET_GATE_DEEP_CHECK)
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再检测。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(f"远端健康检测未开始：{busy} 正在使用联网查询")
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
        self._log.info(f"远端健康检测开始：{len(ids)} 个 mod（匿名接口，"
                       "只读不写库）…")

    def _stop_check(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._stop_btn.setEnabled(False)
            self._summary.setText(
                "已请求停止：等待与重试已中断，最多再等一次在途请求收场。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止远端健康检测")

    # ---------- 回调 ----------
    def _on_succeeded(self, entries: list) -> None:
        self._set_running(False)
        health = exceptionFlow.classify_health(entries, self._titles)
        skipped = exceptionFlow.classify_entries(entries)   # 失效/查询失败计数（纯函数复用）
        cross = len(skipped.invalid) + len(skipped.query_failed)
        shown = self._fill_list(self._sec_mismatch[1], [
            (mid, self._title_of(mid), f"远端标题：{rt}")
            for mid, local, rt in health.title_mismatch])
        self._fill_list(self._sec_abandoned[1], [
            (mid, self._title_of(mid), "标题含弃坑关键词")
            for mid in health.abandoned])
        self._fill_list(self._sec_banned[1], [
            (mid, self._title_of(mid), "banned=1（被 Steam 封禁）")
            for mid in health.banned])
        n = health.total
        if n == 0:
            self._summary.setText("无异常（远端标题与账本一致，无弃坑/"
                                  "banned 信号）。")
            self._summary.setStyleSheet(f"color: {_C_OK};")
        else:
            parts = (f"标题不符 {len(health.title_mismatch)}"
                     f" · 弃坑 {len(health.abandoned)}"
                     f" · banned {len(health.banned)}")
            self._summary.setText(parts)
            self._summary.setStyleSheet(f"color: {_C_WARN};")
        extra = []
        if cross:
            extra.append(f"另有 {cross} 条远端失效/查询失败，"
                         "归【异常处置】页管")
        if health.missing_banned_field:
            extra.append("本次响应未携带 banned 字段：banned 检测不可用"
                         "（Steam 原始响应里有它）——把日志发给开发者")
        self._note.setText("。\n".join(extra) + ("。" if extra else ""))
        self._log.ok(f"远端健康检测完成：{n} 处信号"
                     + (f"；另有 {cross} 条失效/查询失败（见异常处置页）"
                        if cross else ""))

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self._summary.setText(
            f"检测失败：{message}\n（本地数据未动，稍后可重试。可以参考 "
            "watt toolkit 的连通性测试，若一直不通过，可以尝试关闭 "
            "watt toolkit 再试。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"远端健康检测失败：{message}")

    def _on_stopped(self) -> None:
        self._set_running(False)
        self._summary.setText("已停止，本次结果未使用。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("远端健康检测已手动停止")

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
        for sec in (self._sec_mismatch, self._sec_abandoned,
                    self._sec_banned):
            sec[1].clear()
            sec[1].setVisible(False)
        self._summary.setText("")
        self._note.setText("")

    def _fill_list(self, lw: QListWidget, rows) -> int:
        lw.clear()
        for mid, title, note in rows:
            it = QListWidgetItem(f"{mid} {title} · {note}")
            it.setData(Qt.ItemDataRole.UserRole, mid)
            lw.addItem(it)
        lw.setVisible(bool(rows))
        return len(rows)

    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or "（无标题）"

    def _on_row_menu(self, lw: QListWidget, pos) -> None:
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
        act_dir = QAction("打开 mod 文件夹", menu)
        act_dir.triggered.connect(lambda: self._open_folder(mid))
        menu.addAction(act_dir)
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

    def _open_folder(self, mid: int) -> None:
        from gui.modFolderOpener import open_mod_folder
        m = self._mods_by_id.get(mid)
        if m is None or self._owner_game is None:
            return
        open_mod_folder(self, self._owner_game, m, log=self._log)
