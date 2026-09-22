"""更新检测页
"""
"""
对当前游戏档案下的全部 mod 批量查询 Steam 远端信息：
补全标题等字段、判定哪些 mod 需要更新、给特别关注的 mod 记提醒。

分工约定：联网查询放在后台线程（界面不卡、可中途停止）；
数据库写入全部留在主线程（SQLite 毫秒级，无需进线程）。
后台线程只发网络请求，绝不碰数据库。
 结果分五类展示：
 - 需更新 / 已最新 / 未下载：与 mod 库页同一套判定逻辑（直接复用）
 - 版本未知：手动确认入账的 mod（已下载但无 acf 版本，T18/决策 24）——
   照常查询并补全标题等字段，但新旧无从判定，结果里单独列为
   「版本未知」，绝不混入需更新/已最新

- 疑似合集/异常：Steam 返回"查询成功"但文件大小缺失或为 0——真实 mod 不可能是 0 字节，大概率是合集。这类条目不做任何写入（防止把合集的
  标题误填进 mod），等用户点"展开合集"确认后再处理
- 查询失败：接口对单个条目返回 result 非 1（被删除/设为私有/查无此条），
  原样展示给用户，不做任何写入
已删除的 mod 不查询（都不用了没必要查）；已失败的照查（就是它失效才要盯）。

停止与归属约定（代码行为与这里严格一致）：
- 停止是正常操作不是失败：点【停止】后灰字收场、不弹警告框；
  已查到的数据一律丢弃不落库，包括"最后一批查询途中"点的停
- 结果归属：本次检测的全部结果（含"展开合集"登记的条目）
  一律归属"开始检测那一刻"选中的档案——检测进行中允许切换档案，
  但旧结果仍归旧档案，防止把 mod 记错家
"""

import time

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import Game, Mod
from core.steamApiClient import SteamApiError, SteamApiClient, WorkshopItem
from gui.formatters import fmt_size, relative_time
from gui.modListModel import ModListModel
from gui.consolePanel import LogBus

# 与导入页保持一致（双方都用官方网页里的标准链接格式）
_URL_TEMPLATE = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"

# 每批查询的条目数：Steam 官方接口单次上限就是 100
_BATCH = 100

_RESULT_COLUMNS = ["编号", "标题", "结果", "远端更新", "更新间隔", "大小", "操作"]


class _CheckWorker(QThread):
    """后台检测线程：分批查询全部条目，每查完一批就报告一次进度。

    只发网络请求，绝不碰数据库（数据库操作全在主线程）。
    """

    batch_done = Signal(int)  # 已完成查询的条目数（驱动进度条）
    succeeded = Signal(list)  # 全部查完，携带 WorkshopItem 列表
    failed = Signal(str)      # 请求层面失败，携带给用户看的原因
    stopped = Signal()        # 用户点了"停止"：正常收场，不算失败

    def __init__(self, mod_ids: list[int], *, interval_ms: int, max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._stop_requested = False

    def stop(self) -> None:
        """请求停止：批边界生效，查询进行中无法打断，最多多等一批的时间。

        已查到的数据一律丢弃不落库（含最后一批途中点的停，见 run 末尾
        的兜底）——重新点一次检测很快，不值得为半截数据写复杂的续传逻辑。
        """
        self._stop_requested = True

    def run(self) -> None:
        client = SteamApiClient(interval_ms=self._interval_ms,
                                max_retries=self._max_retries)
        ids = list(dict.fromkeys(self._mod_ids))  # 去重且保持顺序
        done = 0
        items: list[WorkshopItem] = []
        try:
            for start in range(0, len(ids), _BATCH):
                if self._stop_requested:
                    # 停止是用户的正常操作，不是失败——走专用信号，
                    # 界面上以灰字收场，不弹"检测失败"的警告框
                    self.stopped.emit()
                    return
                items.extend(client.query_details(ids[start:start + _BATCH]))
                done += len(ids[start:start + _BATCH])
                self.batch_done.emit(done)
                if start + _BATCH < len(ids):
                    # 批间礼貌间隔，来源同客户端的请求间隔设置。
                    # 等待期间无法响应停止，最多多等一个间隔
                    time.sleep(self._interval_ms / 1000)
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        # 兜底：上面的停止检查只在"每批开始前"做——用户若在最后一批
        # 查询途中点停止，循环会正常跑完走到这里。按同样口径处理：
        # 丢弃结果、不落库，保证"停止 = 什么都没发生"这条承诺不破
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(items)


class _CollectionWorker(QThread):
    """后台查询合集成员：单次网络请求，只发不写。"""

    succeeded = Signal(int, list)  # (合集编号, 成员编号列表)
    failed = Signal(str)

    def __init__(self, collection_id: int, *, max_retries: int) -> None:
        super().__init__()
        self._collection_id = collection_id
        self._max_retries = max_retries

    def run(self) -> None:
        client = SteamApiClient(max_retries=self._max_retries)
        try:
            children = client.query_collection_children(self._collection_id)
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self._collection_id, children)


class UpdateCheckPage(QWidget):
    """checks_finished(int)：一次检测完成落库后发射，参数为本次检测到
    有新版本的 mod 数（合集展开登记后发射 0），主窗口借此刷新 mod 库页。
    """

    checks_finished = Signal(int)

    def __init__(self, repo, settings, parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._worker: _CheckWorker | None = None
        self._col_worker: _CollectionWorker | None = None
        self._mods_at_start: list[Mod] = []  # 开始检测时的库内顺序，决定展示顺序
        # 开始检测那一刻的档案 = 本轮结果的真正归属。检测进行中允许切换
        # 档案（set_game 只更新标签不动结果区），但旧结果、旧结果行上的
        # "展开合集"都仍归这个档案——防止把 mod 登记到新档案名下
        self._check_game: Game | None = None
        self._build_ui()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("更新检测", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        tip = QLabel(
            "点【开始检测】查询当前档案全部 mod 的远端信息：自动补全标题等字段、"
            "判定需不需要更新；特别关注的 mod 出新版本时会记一条提醒。\n"
            "文件大小缺失的条目会列为「疑似合集」，确认后可展开入库；"
            "已删除的 mod 不会查询；手动确认入账（版本未知）的 mod "
            "照常查询，但无法判定新旧，结果里单独列为「版本未知」。")

        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._start_btn = QPushButton("开始检测", btn_row)
        self._start_btn.clicked.connect(self._start_check)
        self._stop_btn = QPushButton("停止", btn_row)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_check)
        self._progress = QProgressBar(btn_row)
        self._progress.setVisible(False)
        h.addWidget(self._start_btn)
        h.addWidget(self._stop_btn)
        h.addWidget(self._progress, 1)
        root.addWidget(btn_row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = QTableWidget(0, len(_RESULT_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_RESULT_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 110), (2, 110), (3, 130), (4, 90), (5, 110), (6, 110)):
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
        """程序退出前的收尾：停掉可能还在跑的线程并等它们退出。

        检测线程有停止协议：点一下停止标记，它做完当前这批查询就会退，
        所以 wait() 最多等几秒（网络超时上限内）。合集查询没有停止
        协议但本身就一次请求，直接等它跑完即可。
        """
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait()
        if self._col_worker is not None:
            self._col_worker.wait()


    # ---------- 检测流程 ----------

    def _start_check(self) -> None:
        if self._game is None or self._worker is not None:
            return
        mods = self._repo.list_mods(self._game.app_id, order_by="time_updated DESC")
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._summary.setText("当前档案没有任何可检测的 mod（已删除的除外）。")
            self._summary.setStyleSheet("color: gray;")
            return
        self._mods_at_start = mods
        # 记下"开始检测那一刻"的档案：本轮所有结果（含展开合集登记的
        # 条目）都写进它，即便检测过程中用户切到了别的档案
        self._check_game = self._game
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_running(True)
        self._worker = _CheckWorker(
            ids,
            interval_ms=self._settings.get_int("api_request_interval_ms", 200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._worker.batch_done.connect(self._progress.setValue)
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

    def _stop_check(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._stop_btn.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self._start_btn.setEnabled(not running and self._game is not None)
        self._stop_btn.setEnabled(running)
        self._progress.setVisible(running)

    def _on_worker_finished(self) -> None:
        """检测线程跑完的统一收尾：确认线程彻底退出后，才释放引用。

        finished 发出时线程本体可能还在做最后的退出动作，直接把
        引用丢给 Python 销毁会踩中"销毁仍在运行的线程"，进程当场
        终止（0xC0000409）。wait() 在这里只等几毫秒（线程已在收尾），
        换一个绝不闪退，值得。
        """
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()


    # ---------- 结果处理（主线程） ----------

    @staticmethod
    def _classify(items: list[WorkshopItem]):
        """按结果分桶：正常条目 / 疑似合集（大小缺失或为 0）/ 查询失败。"""
        ok_items: list[WorkshopItem] = []
        suspected_ids: set[int] = set()
        failed: dict[int, int] = {}  # 编号 → 接口返回的 result 原值
        for item in items:
            if item.mod_id is None:
                continue  # 响应异常条目，无处可写，只能丢弃（数量极少）
            if item.result != 1:
                failed[item.mod_id] = item.result
            elif not item.file_size:
                suspected_ids.add(item.mod_id)
            else:
                ok_items.append(item)
        return ok_items, suspected_ids, failed

    def _apply_results(self, ok_items: list[WorkshopItem],
                       suspected_ids: set[int],
                       failed: dict[int, int]) -> int:
        """把查询结果写入数据库，返回"检测到有新版本"的个数。

        全部写入包在一个事务里：中途任何一步失败，整体回滚，
        不会出现"一半 mod 更新了一半没更新"的中间状态。
        """
        now = int(time.time())
        updates_found = 0
        # 先整体记"检测过"（包括失败和疑似合集的）——"查过了"这个事实本身
        # 就值得记，否则"上次检测时间"的展示会骗人
        all_ids = ([i.mod_id for i in ok_items]
                   + sorted(suspected_ids) + sorted(failed))
        with self._repo.transaction():
            self._repo.touch_checked(all_ids, checked_at=now)
            for item in ok_items:
                row = self._repo.get_mod(item.mod_id)
                if row is None:
                    continue  # 编号本来就取自库中，理论到不了这里；防御一行
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
                    # 第一次拿到远端信息：上一版无从得知，
                    # last_time_updated 先记成与当前值相同
                    self._repo.update_api_metadata(
                        item.mod_id, time_updated=item.time_updated,
                        last_time_updated=item.time_updated, **meta)
                elif changed:
                    # 检测到新版本：旧的远端时间挪进 last_time_updated，
                    # 作为"作者更新间隔"的计算基准
                    self._repo.update_api_metadata(
                        item.mod_id, time_updated=item.time_updated,
                        last_time_updated=row.time_updated, **meta)
                    # 拍版本快照：记录新版本 + 当时的本地状态（manifest / 本地版本）
                    self._repo.add_snapshot(
                        item.mod_id, time_updated=item.time_updated,
                        manifest=row.manifest,
                        local_timeupdated=row.local_timeupdated)
                    updates_found += 1
                    if row.is_special:
                        # 特别关注的 mod：同一远端版本只提醒一次
                        # （拿最近一条提醒记录的远端时间比对去重）
                        last = self._repo.get_last_alert(item.mod_id)
                        if last is None or last.remote_time_updated != item.time_updated:
                            diff = ((item.time_updated - row.last_time_updated)
                                    if row.last_time_updated else None)
                            self._repo.add_alert(
                                item.mod_id, item.time_updated, diff_seconds=diff,
                                was_downloaded=(row.status == "downloaded"))
                else:
                    # 版本没变：只刷新其余元数据，时间字段一个都不动
                    self._repo.update_api_metadata(item.mod_id, **meta)
        return updates_found

    def _show_results(self, suspected_ids: set[int], failed: dict[int, int]) -> None:
        """按开始检测时的库内顺序展示结果。正常条目重新从库里读一遍——
        展示的是写入完成后的最新状态，和 mod 库页看到的一致。
        """
        threshold = self._settings.get_int("slow_update_days", 30)
        visible = [m for m in self._mods_at_start if m.status != "deleted"]
        self._table.setRowCount(len(visible))
        red = QColor("#e5484d")
        counts = {"需更新": 0, "版本未知": 0,
                  "疑似": len(suspected_ids), "失败": len(failed)}
        for r, m in enumerate(visible):
            fresh = self._repo.get_mod(m.mod_id)
            title = (fresh.title if fresh is not None else None) or m.title \
                    or "（无标题）"
            self._set_cell(r, 0, str(m.mod_id))
            self._set_cell(r, 1, title)
            if m.mod_id in failed:
                self._set_cell(r, 2, "查询失败")
                self._set_cell(r, 3, f"接口返回 result={failed[m.mod_id]}")
            elif m.mod_id in suspected_ids:
                self._set_cell(r, 2, "疑似合集/异常")
                self._set_cell(
                    r, 3, "远端缺少文件大小——可能是合集，点右侧按钮确认")
            else:
                kind = ModListModel._update_state(fresh)
                counts["需更新"] += kind == "需更新"
                counts["版本未知"] += kind == "版本未知"
                self._set_cell(r, 2, kind)
                self._set_cell(r, 3, relative_time(fresh.time_updated))
                # 更新间隔 = 本次版本距上一版过了多少天（作者的更新节奏）。
                # 只有 last_time_updated 与当前值不同才算真基线——第一次
                # 拿到远端信息时两者相等，那是"没有上一版"，显示"—"，
                # 不显示误导性的"不足 1 天"
                baseline = fresh.last_time_updated
                days = None
                if (fresh.time_updated is not None and baseline is not None
                        and baseline != fresh.time_updated):
                    days = (fresh.time_updated - baseline) / 86400
                if days is not None and days >= 0:
                    text = f"{days:.0f} 天" if days >= 1 else "不足 1 天"
                    item = self._set_cell(r, 4, text)
                    if days > threshold:
                        item.setForeground(red)
                else:
                    self._set_cell(r, 4, "—")
            self._set_cell(
                r, 5, fmt_size(fresh.local_size or fresh.file_size))
            self._set_cell(r, 6, "")
            if m.mod_id in suspected_ids:
                btn = QPushButton("展开合集…", self._table)
                btn.clicked.connect(
                    lambda _=False, mid=m.mod_id: self._expand_collection(mid))
                self._table.setCellWidget(r, 6, btn)
        self._summary.setText(
            f"检测完成：共 {len(visible)} 个｜需更新 {counts['需更新']}"
            f"｜版本未知 {counts['版本未知']}"
            f"｜疑似合集 {counts['疑似']}｜查询失败 {counts['失败']}"
            f"（其余为已最新 / 未下载 / 远端未知）")

        self._summary.setStyleSheet("color: #46a758;")

    def _set_cell(self, row: int, col: int, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        self._table.setItem(row, col, item)
        return item

    def _on_check_succeeded(self, items: list) -> None:

        self._set_running(False)
        ok_items, suspected_ids, failed = self._classify(items)
        try:
            updates_found = self._apply_results(ok_items, suspected_ids, failed)
        except Exception as exc:
            # 写库失败：明确告诉用户，不让程序无声崩溃
            self._log.error(f"检测结果写入失败，已整体回滚：{exc}")
            QMessageBox.critical(
                self, "写入失败",
                f"查询成功，但写入数据库时出错：{exc}\n"
                "数据已整体回滚，请重试一次；若反复出现请反馈。")
            return
        self._show_results(suspected_ids, failed)
        self.checks_finished.emit(updates_found)
        self._log.ok(f"检测完成：需更新 {updates_found}，"
                     f"疑似合集 {len(suspected_ids)}，查询失败 {len(failed)}")

    def _on_check_failed(self, message: str) -> None:

        self._set_running(False)
        self._summary.setText(message)
        self._summary.setStyleSheet("color: #e5484d;")
        self._log.error(message)
        QMessageBox.warning(self, "检测失败", message)

    def _on_check_stopped(self) -> None:
        """用户手动停止：正常收场。

        不弹窗、不标红——停止是用户主动做的正常操作，和"接口报错"
        是两回事，界面上用灰字说明即可。数据没写库，无需任何清理。
        """

        self._set_running(False)
        self._summary.setText("已手动停止，本次结果未写入。重新点【开始检测】即可。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("更新检测已手动停止，本次结果未写入")

    # ---------- 展开合集 ----------

    def _expand_collection(self, collection_id: int) -> None:
        if self._col_worker is not None:
            return  # 一次只查一个合集，防止连点
        self._col_worker = _CollectionWorker(
            collection_id,
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._col_worker.succeeded.connect(self._on_collection_children)
        self._col_worker.failed.connect(self._on_collection_failed)
        self._col_worker.finished.connect(self._on_col_worker_finished)

        self._col_worker.start()

    def _on_collection_failed(self, message: str) -> None:
        QMessageBox.warning(self, "查询失败", f"无法获取合集成员：{message}")

    def _on_col_worker_finished(self) -> None:
        """合集查询线程跑完的统一收尾（理由同 _on_worker_finished）。"""
        w = self._col_worker
        self._col_worker = None
        if w is not None:
            w.wait()


    def _on_collection_children(self, collection_id: int, children: list[int]) -> None:
        if not children:
            self._log.warn(f"编号 {collection_id} 不是合集（查不到成员）")
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
            return  # 理论到不了：能点"展开合集"就说明检测过，检测过就有档案
        existing = self._repo.filter_existing_ids(children)
        new_ids = [i for i in children if i not in existing]
        preview = "、".join(str(i) for i in children[:20]) \
                  + ("…" if len(children) > 20 else "")
        ret = QMessageBox.question(
            self, "展开合集",
            f"该合集包含 {len(children)} 个条目：\n{preview}\n\n"
            f"其中 {len(new_ids)} 个尚未入库。确认把它们登记到当前档案"
            f"「{game.name}」名下吗？（状态：已收录）")
        if ret != QMessageBox.StandardButton.Yes:
            return
        now = int(time.time())
        with self._repo.transaction():
            for mid in new_ids:
                self._repo.add_mod(Mod(
                    mod_id=mid, game_id=game.app_id,
                    url=_URL_TEMPLATE.format(mid),
                    status="tracked", first_tracked_at=now))
        self._log.ok(f"合集 {collection_id} 展开：新登记 {len(new_ids)} 个条目")
        QMessageBox.information(
            self, "已登记",
            f"已登记 {len(new_ids)} 个新条目（状态：已收录）。\n"
            "点【开始检测】即可为它们补全标题等信息。")
        # 参数 0 = 没有新版本，只是让主窗口刷新一下 mod 库页
        self.checks_finished.emit(0)
