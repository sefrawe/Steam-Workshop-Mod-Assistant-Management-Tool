"""已清账管理页
"""
r"""gui/purgedPage.py · 已清账黑名单的跨档案管理台（V2 消费版）。

「已清账」= 从账本物理删除并登记进黑名单的编号（彻底清账的终结态）。
黑名单的唯一用途：**拦截复活**——
· 别的前端（RimSort 等）清单里还留着它，指挥 steamcmd 时下回来；
· 一类"毒 mod"（下载永远超时，实测 3563882422）被尝试下载时，
  steamcmd 会对本地工坊状态做整体校验装配，把缓存里的旧块重新
  装配成 content 并写回 steamcmd 的账本文件。
只要编号在本名单里，入账中心【扫描游戏目录】就会拦下它（拦截回执
见运行日志）。想重新收录 = 本页「允许录入」后再扫描。

登记进本名单的两条路：
· mod 库页右键「彻底清账」/ 清理页彻底清账（自动登记）；
· 本页【手动拉黑…】（贴编号 + 选档案）——对付"软件外手动删了
  文件夹"这类从没走过清账流程的编号。

与备份总览页同款交互纪律：跨档案、无 set_game、进页 refresh()；
操作对象来自「选」列勾选框，勾选记编号不记行号；搜索即时过滤
只隐藏不重建。全程只动黑名单表（purged_mods），不碰 mod 账本、
不碰磁盘文件。

【V2 相对 V1 的改动】
- localScanner 模块已取消（D10）：locate_acf / remove_items_from_acf
  改从 core/steamPaths 调用，其余逻辑零改动；
- 用户可见文案不再出现内部缩写：steamcmd 的账本文件统一叫
  「steamcmd 账本」；重新收录入口统一指入账中心【扫描游戏目录】
  （mod 库页扫描按钮在 V2 已摘除）。
"""
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import constants, steamPaths
from core.backupManager import steamcmd_running
from core.formatters import abs_time            # V2：formatters 住 core
from gui.logBus import LogBus                   # V2：LogBus 独立成文件
from gui.theme import font_px  # 字号单源（D25）
from core.urlParser import workshop_url

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_MUTED = "#8a8a8f"

COL_CHECK = 0
COL_GAME = 1
COL_MOD_ID = 2
COL_TITLE = 3
COL_URL = 4
COL_PURGED = 5
COL_NOTE = 6

_COL_HEADERS = ["选", "游戏", "Mod 编号", "标题快照", "↗", "清账时间", "备注"]


class PurgedPage(QWidget):
    """已清账黑名单管理台。refresh() 供主窗口切到本页时调用
    （MainWindow 的进页刷新钩子自动发现，零接线）。"""

    def __init__(self, repo, parent: QWidget | None = None, *,
                 log: LogBus | None = None, settings=None) -> None:
        super().__init__(parent)
        self._repo = repo
        # AppSettings：读 steamcmd 程序路径（「从 steamcmd 账本移除」
        # 定位 steamcmd 账本文件用）
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._rows: list = []                     # list[PurgedMod]（全量）
        self._row_by_id: dict[int, object] = {}
        self._games: dict[int, str] = {}          # app_id → 游戏名
        self._games_in_order: list = []
        self._filter_game: int | None = None
        self._building = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)
        self._build_ui()
        self._reload()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)   # V2：局部 import 上提到文件头
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("已清账管理", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        tip = QLabel(
            "这里是「彻底清账」登记的黑名单：名单里的编号，入账中心"
            "【扫描游戏目录】时一律拦截复活入库（steamcmd 把删掉的 mod "
            "装配回来也不入账——包括别的下载器清单残留和\"毒 mod\"下载"
            "尝试触发的装配）。黑名单拦的是「进本工具的账」：别的前端"
            "（如 RimSort）清单里还留着这些编号、主动指挥 steamcmd 重新"
            "下载时，文件照样回到硬盘——要彻底断根，先把那边清单清掉"
            "（详见欢迎页「一个游戏一个管家」）。\n"

            "名词·毒 mod：一类下载永远超时的条目。steamcmd 尝试下载它"
            "时，会对本游戏的工坊状态做整体校验装配，把缓存里的旧数据块"
            "重新装配成内容文件夹并写回 steamcmd 的账本文件——这是"
            "\"已删 mod 复活\"的机制之一；黑名单能拦住它入账，但拦不住"
            "它反复占磁盘。\n"
            "「从 steamcmd 账本移除」：把勾选条目从 steamcmd 自己的工坊"
            "账本文件里删掉——黑名单只拦入账，steamcmd 账本里的记录"
            "还在，steamcmd 仍会反复校验装配它们；移除后 steamcmd 彻底"
            "忘了这些条目。steamcmd 运行中不可执行（它退出时会整个覆盖"
            "写回）；操作前自动备份原账本文件，改坏可还原。\n"
            "想重新收录某个编号：勾选它 →「允许录入」，再到入账中心"
            "【扫描游戏目录】。\n"
            "软件外手动删过文件夹、或从没入过账的\"毒 mod\"：用"
            "【手动拉黑…】登记。本页只动黑名单登记与 steamcmd 账本"
            "条目，不碰 mod 内容文件夹（要删内容文件夹去【清理与删除】页）。",
            self)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # ---- 筛选行 ----
        frow = QWidget(self)
        fh = QHBoxLayout(frow)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.addWidget(QLabel("档案：", frow))
        self._combo = QComboBox(frow)
        self._combo.setToolTip("按游戏档案过滤黑名单；「全部档案」看所有")
        self._combo.currentIndexChanged.connect(self._fill_table)
        fh.addWidget(self._combo)
        self._search = QLineEdit(frow)
        self._search.setPlaceholderText("搜索：编号 / 标题 / 备注…")
        self._search.setClearButtonEnabled(True)
        self._search.setToolTip(
            "即时过滤当前列表：不命中的行隐藏，已勾选的原样保留；"
            "清空恢复全部")
        self._search.textChanged.connect(lambda *_: self._search_timer.start())
        fh.addWidget(self._search, 1)
        fh.addStretch(1)
        btn_refresh = QPushButton("刷新", frow)
        btn_refresh.setToolTip("重新读取黑名单（会清空当前勾选）")
        btn_refresh.clicked.connect(self._reload)
        fh.addWidget(btn_refresh)
        root.addWidget(frow)

        # ---- 工具行 ----
        bar = QWidget(self)
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(0, 0, 0, 0)
        self._btn_sel = QPushButton("对选中 ▾", bar)
        self._btn_sel.setToolTip("对勾选的黑名单条目执行动作")
        menu = QMenu(self._btn_sel)
        menu.setToolTipsVisible(True)
        act_all = QAction("全选当前显示", menu)
        act_all.triggered.connect(lambda: self._set_all_checks(True))
        menu.addAction(act_all)
        act_none = QAction("清除勾选", menu)
        act_none.triggered.connect(lambda: self._set_all_checks(False))
        menu.addAction(act_none)
        menu.addSeparator()
        self._act_allow = QAction("允许录入…（移出黑名单）", menu)
        self._act_allow.setToolTip(
            "把勾选的编号移出黑名单：之后入账中心【扫描游戏目录】不再"
            "拦截它们——盘上已有文件的会正常落成待认领")
        self._act_allow.triggered.connect(self._allow_checked)
        menu.addAction(self._act_allow)
        # V2：动作名去内部缩写（acf），指向不变
        self._act_acf = QAction("从 steamcmd 账本移除条目…", menu)
        self._act_acf.setToolTip(
            "把勾选条目从 steamcmd 的工坊账本文件里删除——"
            "steamcmd 从此彻底忘了它们，不再校验装配、不再占磁盘。\n"
            "steamcmd 运行中会拒绝执行；操作前自动备份原账本文件")
        self._act_acf.triggered.connect(self._remove_from_acf_checked)
        menu.addAction(self._act_acf)
        self._btn_sel.setMenu(menu)
        bh.addWidget(self._btn_sel)

        btn_black = QPushButton("手动拉黑…", bar)
        btn_black.setToolTip(
            "把编号清单登记进黑名单（选所属档案）：对付软件外手动"
            "删过文件夹、或从未入过账的\"毒 mod\"")
        btn_black.clicked.connect(self._manual_blacklist)
        bh.addWidget(btn_black)
        bh.addStretch(1)
        root.addWidget(bar)

        # ---- 表格 ----
        self._table = QTableWidget(0, len(_COL_HEADERS), self)
        self._table.setHorizontalHeaderLabels(_COL_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_CHECK, 36), (COL_GAME, 130), (COL_MOD_ID, 110),
                           (COL_URL, 36), (COL_PURGED, 150), (COL_NOTE, 200)):
            self._table.setColumnWidth(col, width)
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        root.addWidget(self._count_label)

    # ---------- 数据 ----------
    def _reload(self) -> None:
        self._games = {g.app_id: g.name for g in self._repo.list_games()}
        self._games_in_order = list(self._repo.list_games())
        self._combo.blockSignals(True)
        try:
            self._combo.clear()
            self._combo.addItem("全部档案", None)
            found = False
            for g in self._games_in_order:
                self._combo.addItem(f"{g.name}（{g.app_id}）", g.app_id)
                if g.app_id == self._filter_game:
                    found = True
            if not found:
                self._filter_game = None
            idx = self._combo.findData(self._filter_game)
            self._combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._combo.blockSignals(False)
        self._fill_table()

    def _fill_table(self) -> None:
        self._building = True
        self._table.setRowCount(0)
        rows = self._repo.list_purged()  # 清账时间新→旧
        self._filter_game = self._combo.currentData()
        if self._filter_game is not None:
            rows = [r for r in rows if r.game_id == self._filter_game]
        self._rows = rows
        self._row_by_id = {r.mod_id: r for r in rows}
        self._table.setRowCount(len(rows))
        for row, r in enumerate(rows):
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                        | Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, r.mod_id)
            self._table.setItem(row, COL_CHECK, it)
            self._table.setItem(
                row, COL_GAME,
                QTableWidgetItem(self._games.get(r.game_id, str(r.game_id))))
            self._table.setItem(row, COL_MOD_ID,
                                QTableWidgetItem(str(r.mod_id)))
            ti = QTableWidgetItem(r.title or "（无标题快照）")
            ti.setToolTip(r.title or "")
            self._table.setItem(row, COL_TITLE, ti)
            ui = QTableWidgetItem("↗")
            ui.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ui.setForeground(QBrush(QColor("#5aa9ff")))
            ui.setData(Qt.ItemDataRole.UserRole, r.mod_id)
            ui.setToolTip("点击在浏览器打开该 mod 的创意工坊页面")
            self._table.setItem(row, COL_URL, ui)
            self._table.setItem(row, COL_PURGED,
                                QTableWidgetItem(abs_time(r.purged_at)))
            self._table.setItem(row, COL_NOTE, QTableWidgetItem(r.note or ""))
        n = len(rows)
        self._count_label.setText(
            f"黑名单共 {n} 个编号"
            + ("（空——还没有编号被彻底清账或拉黑）" if n == 0 else ""))
        self._refresh_buttons()
        self._apply_filter()
        self._building = False

    # ---------- 勾选 ----------
    def _checked(self) -> list:
        out = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, COL_CHECK)
            if it is not None and (it.flags() & Qt.ItemFlag.ItemIsUserCheckable) \
                    and it.checkState() == Qt.CheckState.Checked:
                rec = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
                if rec is not None:
                    out.append(rec)
        return out

    def _set_all_checks(self, on: bool) -> None:
        self._building = True
        for r in range(self._table.rowCount()):
            if on and self._table.isRowHidden(r):
                continue  # 全选只勾可见行（与备份总览同款防盲选）
            it = self._table.item(r, COL_CHECK)
            if it is not None and (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                it.setCheckState(
                    Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._building = False
        self._refresh_buttons()

    def _on_item_changed(self, item) -> None:
        if not self._building and item.column() == COL_CHECK:
            self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        n = 0 if self._building else len(self._checked())
        has = n > 0
        self._act_allow.setEnabled(has)
        self._act_acf.setEnabled(has)
        self._btn_sel.setText(f"对选中({n})" if n else "对选中")

    def _apply_filter(self) -> None:
        kw = self._search.text().strip().casefold()
        for r in range(self._table.rowCount()):
            it = self._table.item(r, COL_CHECK)
            if it is None or not (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                self._table.setRowHidden(r, False)
                continue
            rec = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
            if rec is None:
                self._table.setRowHidden(r, bool(kw))
                continue
            hay = " ".join((self._games.get(rec.game_id, ""),
                            rec.title or "", str(rec.mod_id),
                            rec.note or "")).casefold()
            self._table.setRowHidden(r, bool(kw) and kw not in hay)

    # ---------- 动作 ----------
    def _allow_checked(self) -> None:
        recs = self._checked()
        if not recs:
            QMessageBox.information(self, "允许录入",
                                    "先勾选要移出黑名单的编号。")
            return
        ret = QMessageBox.question(
            self, "允许录入",
            f"把 {len(recs)} 个编号移出黑名单？\n\n"
            "· 移出后入账中心【扫描游戏目录】不再拦截它们：盘上已有其"
            "内容文件夹的，下次扫描会正常落成待认领；盘上没有的照旧"
            "不入账（没文件不算下载成功）；\n"
            "· steamcmd 侧的复活源头（RimSort 清单 / 毒 mod）没解决的话，"
            "它们仍可能再次被装回——到时可再拉黑。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消允许录入：黑名单未动")
            return
        done = 0
        try:
            with self._repo.transaction():
                for r in recs:
                    self._repo.remove_purged(r.mod_id)
                    done += 1
        except Exception as exc:
            QMessageBox.critical(self, "允许录入", f"失败（名单未改动）：{exc}")
            return
        self._log.ok(f"已允许录入 {done} 个编号（已移出黑名单；"
                     "要入库到入账中心【扫描游戏目录】）")
        self._reload()

    def _manual_blacklist(self) -> None:
        if not self._games_in_order:
            QMessageBox.information(
                self, "手动拉黑", "还没有任何游戏档案：先在左上角添加档案。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("手动拉黑（登记进黑名单）")
        v = QVBoxLayout(dlg)
        tip = QLabel(
            "把编号登记进黑名单：之后入账中心【扫描游戏目录】会拦截这些"
            "编号的复活入库。适用于软件外手动删过文件夹、或从未入过账的"
            "\"毒 mod\"。\n已在名单里的编号 = 覆盖刷新标题与备注。",
            dlg)
        tip.setWordWrap(True)
        v.addWidget(tip)
        edit = QPlainTextEdit(dlg)   # V2：局部 import 已上提文件头
        edit.setFixedHeight(96)
        edit.setPlaceholderText("每行一个工坊编号（纯数字）…")
        v.addWidget(edit)
        row = QHBoxLayout()
        row.addWidget(QLabel("所属游戏档案：", dlg))
        combo = QComboBox(dlg)
        for g in self._games_in_order:
            combo.addItem(f"{g.name}（{g.app_id}）", g.app_id)
        row.addWidget(combo, 1)
        v.addLayout(row)
        note_edit = QLineEdit(dlg)
        note_edit.setPlaceholderText("备注（可选）：为什么拉黑，方便日后回想")
        v.addWidget(note_edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel, dlg)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("登记")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        ids: list[int] = []
        bad: list[str] = []
        for ln in edit.toPlainText().splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                n = int(s)
                if n > 0:
                    ids.append(n)
                    continue
            except ValueError:
                pass
            bad.append(s)
        ids = sorted(set(ids))
        if not ids:
            QMessageBox.information(self, "手动拉黑", "没有可登记的编号。")
            return
        if bad:
            self._log.warn(f"手动拉黑：{len(bad)} 行不是有效编号已忽略："
                           f"{bad[:5]}{'…' if len(bad) > 5 else ''}")
        game_id = combo.currentData()
        note = note_edit.text().strip() or None
        gname = self._games.get(game_id, str(game_id))
        ret = QMessageBox.question(
            self, "手动拉黑",
            f"把 {len(ids)} 个编号登记进「{gname}」的黑名单？\n"
            "登记后【扫描游戏目录】将拦截它们的复活入库；想重新收录时到"
            "本页「允许录入」。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消手动拉黑：黑名单未动")
            return
        try:
            with self._repo.transaction():
                for mid in ids:
                    self._repo.add_purged(mid, game_id, note=note)
        except Exception as exc:
            QMessageBox.critical(self, "手动拉黑", f"失败（名单未改动）：{exc}")
            return
        self._log.warn(f"已把 {len(ids)} 个编号登记进「{gname}」黑名单"
                       "（扫描游戏目录将拦截其复活入库）")
        self._reload()

    def _remove_from_acf_checked(self) -> None:
        """把勾选条目从 steamcmd 的工坊账本文件里移除（方法名保留 acf
        是内部命名，界面文案一律叫「steamcmd 账本」）。
        黑名单拦截的是"入账"；steamcmd 账本里条目还在的话，steamcmd
        每次下载其他 mod 时仍会对它们做整体校验装配（毒 mod 复活机制）
        ——把条目从 steamcmd 账本里删掉才是断根。安全边界：
        - steamcmd 在跑 → 拒绝执行（与恢复备份的引擎级拒绝同级）；
        - 写前自动备份（同目录 .bak_时间戳），解析失败绝不落笔；
        - 只删指定条目的登记子块，steamcmd 账本其余内容一字不动；
        - 黑名单登记不受影响（条目继续被拦截入账）。
        """
        recs = self._checked()
        if not recs:
            QMessageBox.information(self, "从 steamcmd 账本移除",
                                    "先勾选要移除的编号。")
            return
        if steamcmd_running():
            QMessageBox.warning(
                self, "steamcmd 正在运行",
                "steamcmd 正在运行，现在改它的账本文件会在它退出时被"
                "整个覆盖回去（等于白改）。\n\n请先停止 steamcmd"
                "（控制台 → steamcmd 终端 → 停止），再回来执行。")
            return
        root = steamPaths.steamcmd_root(
            self._settings.get("steamcmd_path") if self._settings else "")
        if root is None:
            QMessageBox.information(
                self, "从 steamcmd 账本移除",
                "尚未设置 steamcmd 程序路径，无法定位工坊账本文件。\n"
                "请先到设置页填写 steamcmd 程序路径。")
            return
        # 黑名单跨档案：条目可能属于多个游戏，按档案分组定位各自的账本文件
        by_game: dict[int, list[int]] = {}
        for r in recs:
            by_game.setdefault(r.game_id, []).append(r.mod_id)
        ret = QMessageBox.question(
            self, "从 steamcmd 账本移除",
            f"把 {len(recs)} 个编号从 steamcmd 的工坊账本文件里移除？\n\n"
            "· steamcmd 从此彻底忘了这些条目：不再校验装配、不再占磁盘；\n"
            "· 已下载的内容文件夹不受影响，要删去【清理与删除】页；\n"
            "· 操作前自动备份原账本文件（同目录 .bak_时间戳），"
            "改坏可还原；\n"
            "· 黑名单登记保留：条目继续被拦截入账（想重新收录走"
            "「允许录入」）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消：steamcmd 账本未动")
            return
        total_removed: list[int] = []
        missing_acf: list[str] = []
        for game_id, mids in sorted(by_game.items()):
            gname = self._games.get(game_id, str(game_id))
            # V2（D10）：localScanner 取消，定位/移除都住 core/steamPaths
            acf = steamPaths.locate_acf(root, game_id)
            if acf is None:
                missing_acf.append(gname)
                continue
            try:
                removed, absent = steamPaths.remove_items_from_acf(acf, mids)
            except ValueError as exc:
                self._log.error(f"「{gname}」的 steamcmd 账本移除失败"
                                f"（条目未动）：{exc}")
                continue
            total_removed.extend(removed)
            line = f"「{gname}」：已移除 {len(removed)} 个条目"
            if absent:
                line += f"；{len(absent)} 个编号账本里本来就没有（无需处理）"
            self._log.ok(line)
        if total_removed:
            self._log.warn(
                f"共从 steamcmd 账本移除 {len(total_removed)} 个条目"
                "（原文件已自动备份在同目录；黑名单登记保留）")
        for gname in missing_acf:
            self._log.info(f"「{gname}」没有 steamcmd 的工坊账本文件——"
                           "该游戏可能从没用本机 steamcmd 下载过，无需清理")
        self._reload()

    # ---------- 表格交互 ----------
    def _on_cell_clicked(self, row: int, col: int) -> None:
        if col != COL_URL:
            return
        it = self._table.item(row, col)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        url = workshop_url(mid)
        if QDesktopServices.openUrl(QUrl(url)):
            self._log.info(f"已在浏览器打开 mod {mid} 的创意工坊页面")
        else:
            QMessageBox.warning(self, "打开工坊页面",
                                f"浏览器没有响应，请手动打开：\n{url}")

    def _on_context_menu(self, pos) -> None:
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        chk = self._table.item(row, COL_CHECK)
        if chk is None or not (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        mid = chk.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        act_open = QAction("打开工坊页面", menu)
        act_open.triggered.connect(lambda: self._on_cell_clicked(row, COL_URL))
        menu.addAction(act_open)
        act_copy = QAction("复制编号", menu)
        act_copy.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act_copy)
        menu.addSeparator()
        act_allow = QAction("允许录入…（移出黑名单）", menu)
        act_allow.setToolTip("把这一个编号移出黑名单（同批量动作，单条版）")
        act_allow.triggered.connect(
            lambda: (self._table.item(row, COL_CHECK).setCheckState(
                Qt.CheckState.Checked), self._allow_checked()))
        menu.addAction(act_allow)
        act_acf = QAction("从 steamcmd 账本移除条目…", menu)
        act_acf.setToolTip("把这一个编号从 steamcmd 账本里删除"
                           "（同批量动作，单条版）")
        act_acf.triggered.connect(
            lambda: (self._table.item(row, COL_CHECK).setCheckState(
                Qt.CheckState.Checked), self._remove_from_acf_checked()))
        menu.addAction(act_acf)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _copy_id(self, mid: int) -> None:
        QApplication.clipboard().setText(str(mid))   # V2：import 已上提
        self._log.info(f"已复制编号 {mid}")

    # ---------- 对外 ----------
    def refresh(self) -> None:
        """主窗口切到本页时调用：黑名单可能刚被别处（右键彻底清账 /
        清理页）改动，进页重读。"""
        self._reload()
