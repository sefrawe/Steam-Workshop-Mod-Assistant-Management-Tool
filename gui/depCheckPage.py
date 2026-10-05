"""依赖检测
"""
r"""gui/depCheckPage.py · 桶B 依赖检测（D24 拆分页之一）。

必需物品清单由带 key 的官方接口拉取（key 现场粘贴、用完即弃——
本页不保存）：清单里编号不在账本 = 缺依赖（红）；与上次拉取有差 =
依赖变化（黄，以最新为准）。拉取成功后依赖边入账（replace_
dependencies，本页唯一的写库动作；children 键缺席的条目绝不写——
没拿到的东西不冒充"无依赖"）。
netGate「依赖拉取」闸（constants.NET_GATE_DEPENDENCIES）；
RemoteQueryWorker + keyed 查询闭包。
"""
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QInputDialog, QProgressBar, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core import constants, netGate   # ★ 待核：netGate 若在 gui 包改这行
from core.formatters import abs_time
from core.models import Game
from core.steamApiClient import SteamApiClient
from gui.logBus import LogBus
from gui.remoteQueryWorker import RemoteQueryWorker, keyed_item_to_entry
from workflows import exceptionFlow
from gui.theme import font_px  # 字号单源（D25）

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"
_C_MUTED = "#8a8a8f"


class DepCheckPage(QWidget):
    """桶B：缺依赖 / 依赖变化。拉取成功才写库，停止 = 什么都没发生。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()
        self._game: Game | None = None
        self._worker = None
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

        tip = QLabel(
            "mod 的「必需物品」清单由带 key 的官方接口拉取（key 现场粘贴、"
            "用完即弃，注册方法见【Steam API 密钥】页）：清单里编号不在"
            "账本 = 缺依赖（红，一键复制编号去【网址批量导入】添加）；"
            "与上次拉取有差 = 依赖变化（黄，作者增删了依赖，以最新为准）。",
            self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)
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
        self._progress = QProgressBar(row)
        self._progress.setVisible(False)
        h.addWidget(self._progress, 1)
        root.addWidget(row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._sec_missing = self._make_section(
            "缺依赖（必需物品编号不在账本——下载了也跑不起来）",
            "点按钮复制全部缺失编号，到【网址批量导入】添加后再下载；"
            "右键单条可打开工坊页面看是什么。", _C_FAIL)
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
        if self._worker is not None:
            self._worker.stop()
            if not self._worker.wait(1500):
                netGate.park(self._worker)
            self._worker = None

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
        # ② 依赖边入账（本页唯一的写库动作）：children 键缺席的条目
        # 不写——没拿到的东西绝不冒充"无依赖"。逐条原子，中途失败
        # 下次拉取全量覆盖
        written = 0
        for e in entries:
            if e.get("children") is None:
                continue
            try:
                self._repo.replace_dependencies(e["mod_id"], e["children"])
                written += 1
            except ValueError as exc:
                self._log.error(
                    f"依赖入账失败（mod {e.get('mod_id')}）：{exc}")
        # ③ 渲染
        self._fill_list(self._sec_missing[1], [
            (mid, self._title_of(mid),
             f"缺 {len(lack)} 个必需物品：{self._ids_text(lack)}")
            for mid, lack in sorted(deps.missing.items())])
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
            self._summary.setText(
                f"无异常（解析 {deps.fetched} 条：无依赖 "
                f"{len(deps.childless)} 条，其余依赖全部在账本）。")
            self._summary.setStyleSheet(f"color: {_C_OK};")
        else:
            stat = (f"缺依赖 {len(deps.missing)} · "
                    f"依赖变化 {len(deps.changed)}")
            if deps.undetermined:
                stat += f" · 未判定 {len(deps.undetermined)}"
            self._summary.setText(stat)
            self._summary.setStyleSheet(f"color: {_C_WARN};")
        # 补充说明行
        last_fetch = self._repo.latest_dependency_fetch()
        notes = []
        if deps.missing:
            all_missing = sorted(
                {i for lack in deps.missing.values() for i in lack})
            btn = QPushButton(
                f"复制全部缺失编号（{len(all_missing)} 个）…", self)
            btn.setToolTip("复制到剪贴板（每行一个）：到【网址批量导入】"
                           "粘贴即可全部入库，之后正常下载")
            btn.clicked.connect(
                lambda _=False, ids=all_missing: self._copy_missing(ids))
            self.root_layout.insertWidget(
                self.root_layout.indexOf(self._sec_changed[0]), btn)
            self._copy_btn = btn   # 清场时摘除
        if deps.undetermined:
            notes.append(
                f"未判定 {len(deps.undetermined)} 条（响应里没有 children "
                "键，接口行为异常）——这些条目的依赖本次没拿到，也绝不"
                "冒充「无依赖」入账。重拉一次；持续出现把日志发给开发者。"
                + self._ids_text(deps.undetermined))
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
        self._sec_missing[1].clear()
        self._sec_missing[1].setVisible(False)
        self._sec_changed[1].clear()
        self._sec_changed[1].setVisible(False)
        self._summary.setText("")
        self._note.setText("")
        btn = getattr(self, "_copy_btn", None)
        if btn is not None:
            self.root_layout.removeWidget(btn)
            btn.deleteLater()
            self._copy_btn = None

    def _fill_list(self, lw: QListWidget, rows) -> None:
        lw.clear()
        for mid, title, note in rows:
            it = QListWidgetItem(f"{mid} {title} · {note}")
            it.setData(Qt.ItemDataRole.UserRole, mid)
            lw.addItem(it)
        lw.setVisible(bool(rows))

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
                     "（去【网址批量导入】粘贴添加）")

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
        act_copy = QAction("复制编号", menu)
        act_copy.triggered.connect(
            lambda: (QApplication.clipboard().setText(str(mid)),
                     self._log.info(f"已复制编号 {mid}")))
        menu.addAction(act_copy)
        menu.exec(lw.mapToGlobal(pos))

    def _open_url(self, mid: int) -> None:
        m = self._mods_by_id.get(mid)
        url = ((m.url if m is not None else "") or "").strip() \
            or constants.WORKSHOP_URL_TEMPLATE.format(mid)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打开页面",
                                f"浏览器没有响应，请手动打开：\n{url}")
