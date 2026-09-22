"""mod 库页
"""
"""
后端调试主战场。

布局：顶部筛选条（含"扫描本地"）+ 水平分割（左表格 / 右详情）。
右键菜单：repo 已支持的直接可用；"获取下载命令"发信号给 MainWindow
跳到命令生成页并聚焦该 mod；"手动备份"已点亮——调 core.backupManager
把该 mod 的下载内容复制进备份区（单 mod 手动备份；批量备份、恢复、
钉住、删除等归备份管理页 M3）。

分工与边界（记事本架构约定）：
- 只通过 ModRepository 接口读写，GUI 层零 SQL；
- 查询条件（搜索词 / 状态筛选 / 排序）由本页持有，模型只管展示，
  模型不回头反问查询条件；
- "扫描本地"直接调 core.localScanner 且同步执行——本地只是读一个
  小文本文件，毫秒级完成，不值得为它开线程；
- "手动备份"同样同步执行（已知限制）：引擎内部要写库，repo 的连接
  有线程亲缘性不能跨线程共用，线程化需要给引擎配独立连接，
  归 M3 备份管理页一并做。GB 级 mod 备份期间界面会冻几十秒，
  备份前有日志预告。
- 扫描的定位钥匙是设置页的 steamcmd 程序路径（决策 21）：
  工坊内容与账本文件（acf）都在 steamcmd 自己管理的目录树里，
  按 core/steamPaths.py 的公式推导，不再让用户手填 Steam 库路径。
  扫描前顺带核对档案下载目录是否因 steamcmd 挪位失效
  （失效则重推导并写回，见 _on_scan_local 内注释）。
"""
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from core import localScanner, steamPaths
from core.appSettings import AppSettings
from core.backupManager import BackupManager, steamcmd_running
from core.models import Game
from gui.consolePanel import LogBus
from gui.formatters import fmt_size
from gui.modDetailPanel import ModDetailPanel
from gui.modListModel import _SORT_MAP, ModListModel

# 颜色标记：库里存 hex（将来徽标着色直接可用），右键菜单里显示中文名
_COLORS = {
    "红": "#e5484d",
    "橙": "#f76b15",
    "黄": "#f5d90a",
    "绿": "#46a758",
    "蓝": "#0091ff",
    "紫": "#8e4ec6",
}


class ModListPage(QWidget):
    # 右键"获取下载命令"时发出，参数 = 要聚焦的 mod id 列表；MainWindow 负责跳转
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo          # ModRepository：读 mod 清单，写备注/标记/状态
        self._settings = settings  # AppSettings：读 steamcmd 程序路径（扫描本地用）
        # 日志总线：MainWindow 注入；单独调试本页时兜底建一个
        # （消息没人显示但不崩，"发进空气 = 无操作"），所以后面可以放心直呼
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None
        self._search = ""                      # 当前生效的搜索词（防抖后更新）
        self._order_by = "time_updated DESC"   # 默认按远端版本新 → 旧
        self._sort_col = 3                     # 表头箭头停在"远端版本"列
        self._selected_mod_id: int | None = None  # 重新载入后恢复选中用
        self._build_ui()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---- 顶部筛选条 ----
        bar = QWidget(self)
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 6)

        row = QWidget(bar)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)

        self._search_edit = QLineEdit(row)
        self._search_edit.setPlaceholderText("搜索标题 / 备注…")
        self._search_edit.setClearButtonEnabled(True)
        h.addWidget(self._search_edit, 1)

        # 状态下拉：界面显示中文，currentData 里存库内原始值，
        # 查询时直接把 currentData 递给 repo，不用做"中文 → 状态值"翻译
        self._status_combo = QComboBox(row)
        for label, val in (
                ("全部", None), ("已收录", "tracked"), ("已下载", "downloaded"),
                ("已删除", "deleted"), ("已失败", "failed"),
        ):
            self._status_combo.addItem(label, val)
        h.addWidget(self._status_combo)

        self._special_check = QCheckBox("特别关注", row)
        h.addWidget(self._special_check)

        btn = QPushButton("刷新", row)
        btn.clicked.connect(self._reload)
        h.addWidget(btn)

        scan_btn = QPushButton("扫描本地")
        scan_btn.setToolTip("解析当前游戏的 appworkshop acf，回填本地版本三件套")
        scan_btn.clicked.connect(self._on_scan_local)
        h.addWidget(scan_btn)

        self._count_label = QLabel("", row)
        h.addWidget(self._count_label)

        bar_layout.addWidget(row)
        root.addWidget(bar)

        # ---- 左表格 / 右详情 ----
        split = QSplitter(Qt.Orientation.Horizontal, self)
        self._table = QTableView(split)
        self._model = ModListModel(self._table)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # 标题列占满剩余宽度
        for col, width in ((0, 90), (2, 96), (3, 92), (4, 92),
                           (5, 72), (6, 88), (7, 170), (8, 32), (9, 160)):
            self._table.setColumnWidth(col, width)
        header.setSortIndicator(self._sort_col, Qt.SortOrder.DescendingOrder)
        header.setSortIndicatorShown(True)

        self._detail = ModDetailPanel(split)
        split.addWidget(self._table)
        split.addWidget(self._detail)
        split.setSizes([820, 380])
        root.addWidget(split, 1)

        # ---- 信号 ----
        # 搜索防抖：每敲一个字都查库太浪费，停手 300ms 再查
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_edit.textChanged.connect(self._search_timer.start)

        self._status_combo.currentIndexChanged.connect(self._reload)
        self._special_check.toggled.connect(self._reload)
        header.sectionClicked.connect(self._on_header_clicked)
        self._table.selectionModel().currentRowChanged.connect(self._on_current_row)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        # 切档案必须同时清掉旧选中：不清的话，下面的恢复选中逻辑
        # 会把上一个档案的 mod 详情带进新档案的界面，看着像串档
        self._game = game
        self._selected_mod_id = None
        self._reload()

    # ---------- 查询 ----------

    def _apply_search(self) -> None:
        self._search = self._search_edit.text().strip()
        self._reload()

    def _reload(self) -> None:
        if self._game is None:
            self._model.set_rows([])
            self._count_label.setText("请先在左上角选择游戏档案")
            return
        rows = self._repo.list_mods(
            self._game.app_id,
            status=self._status_combo.currentData(),
            special_only=self._special_check.isChecked(),
            search=self._search or None,
            order_by=self._order_by,
        )
        self._model.set_rows(rows)
        self._count_label.setText(f"共 {len(rows)} 个 mod")
        # 恢复选中：右键操作后的 reload 都会走到这里，
        # 之前选中的 mod 还在清单里就重新选上，详情面板不闪空
        if self._selected_mod_id is not None:
            for i, m in enumerate(rows):
                if m.mod_id == self._selected_mod_id:
                    self._table.selectRow(i)
                    break

    # ---------- 槽 ----------

    def _on_header_clicked(self, col: int) -> None:
        # 表头点击 → 排序串：映射表在 modListModel._SORT_MAP（白名单防注入，
        # 本页只做"列号 → 用哪个排序串"的翻译，不自己拼 SQL 片段）。
        # 白名单每一项是 (默认方向, 备选方向)，备选可为 None = 只许单向。
        pair = _SORT_MAP.get(col)
        if pair is None:
            return
        first, second = pair
        # 当前已在默认方向且有备选 → 换备选；否则回到默认方向
        new_order_by = second if (self._order_by == first and second) else first
        self._order_by = new_order_by
        self._sort_col = col
        # 箭头方向必须跟排序串的真实方向走：白名单里有的列默认就是
        # "从新到旧 / 从大到小"（如 time_updated DESC、local_size DESC），
        # 若按"默认=升序箭头、备选=降序箭头"想当然写，箭头就和数据方向相反
        order = (Qt.SortOrder.DescendingOrder
                 if new_order_by.upper().endswith(" DESC")
                 else Qt.SortOrder.AscendingOrder)
        self._table.horizontalHeader().setSortIndicator(col, order)
        self._reload()

    def _on_current_row(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if current.isValid():
            m = self._model.mod_at(current.row())
            self._selected_mod_id = m.mod_id
            self._detail.set_mod(m)
        else:
            self._detail.set_mod(None)

    def _on_context_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        m = self._model.mod_at(index.row())
        menu = QMenu(self)
        mid = m.mod_id

        # 打开工坊页面：链接理论上必有（导入时按模板生成），保险起见判一下
        act_open = QAction("打开工坊页面", menu)
        act_open.setEnabled(bool(m.url))
        act_open.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(m.url)))
        menu.addAction(act_open)

        menu.addSeparator()
        for text, cb in (
                ("编辑备注…", lambda: self._edit_note(m)),
                ("颜色标记…", lambda: self._pick_color(m)),
                ("切换特别关注", lambda: self._toggle_special(m)),
        ):
            act = QAction(text, menu)
            act.triggered.connect(cb)
            menu.addAction(act)

        menu.addSeparator()
        # 跳到命令生成页，让它只勾选这个 mod（不带页面参数，交给 MainWindow 接线）
        act_cmdgen = QAction("获取下载命令…", menu)
        act_cmdgen.triggered.connect(
            lambda: self.command_gen_requested.emit([mid]))
        menu.addAction(act_cmdgen)

        act_backup = QAction("手动备份…", menu)
        act_backup.setToolTip(
            "把该 mod 的下载内容复制进备份区（robocopy）；"
            "保留份数与总配额按设置页执行")
        act_backup.triggered.connect(lambda: self._start_backup(m))
        menu.addAction(act_backup)

        menu.addSeparator()
        act_del = QAction("软删除…", menu)
        act_del.setEnabled(m.status != "deleted")  # 已删除的不重复删
        act_del.triggered.connect(lambda: self._soft_delete(m))
        menu.addAction(act_del)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    # ---------- 动作 ----------

    def _edit_note(self, m) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "编辑备注", f"mod {m.mod_id} 的备注：", m.note or "")
        if ok:
            # 空串 → None：备注清空等于"没有备注"，库里不留空串
            self._repo.set_note(m.mod_id, text.strip() or None)
            self._reload()

    def _pick_color(self, m) -> None:
        names = [*_COLORS.keys(), "（清除标记）"]
        name, ok = QInputDialog.getItem(
            self, "颜色标记", f"mod {m.mod_id}：", names,
            current=0, editable=False)
        if ok:
            self._repo.set_color_tag(
                m.mod_id, None if name.startswith("（") else _COLORS[name])
            self._reload()

    def _toggle_special(self, m) -> None:
        self._repo.set_special(m.mod_id, not m.is_special)
        self._reload()

    def _soft_delete(self, m) -> None:
        ret = QMessageBox.question(
            self, "软删除",
            f"确定将「{m.title or m.mod_id}」标记为已删除？\n"
            "记录会保留在库中（含删除前快照），可随时恢复。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # last_state 由调用方组装、repo 只管存取（repo 契约如此）；
        # 将来抽 flow 层时这段组装会一起搬走，GUI 目前代行
        last_state = {"title": m.title, "url": m.url,
                      "time_updated": m.time_updated,
                      "local_timeupdated": m.local_timeupdated,
                      "manifest": m.manifest, "local_size": m.local_size,
                      "note": m.note, "color_tag": m.color_tag,
                      "is_special": m.is_special, "local_path": m.local_path}
        self._repo.mark_deleted(m.mod_id, last_state)
        self._reload()

    def _steamcmd_root(self) -> Path | None:
        """设置页的 steamcmd 程序路径 → steamcmd 根目录（core/steamPaths 推导）。

        没配 steamcmd 程序路径 → None，调用方提示去设置页。
        """
        return steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))

    def _make_backup_engine(self) -> BackupManager:
        """按当前设置构造备份引擎（参数注入模式，每次现读设置）。"""
        return BackupManager(
            self._repo,
            keep_per_mod=self._settings.get_int("backup_keep_per_mod", 1),
            quota_bytes=(self._settings.get_int("backup_total_quota_gb", 100)
                         * 2 ** 30),
            steamcmd_path=self._settings.get("steamcmd_path"),
        )

    def _start_backup(self, m) -> None:
        """右键"手动备份"：把该 mod 的下载内容复制进备份区。

        R7：steamcmd 在跑时备份可能拿到写了一半的文件——记事本定性为
        "建议等待"，弹不弹窗是 GUI 的事，这里问一句再动手。
        备份本身同步执行（已知限制见文件头），先预告再动手。
        """
        if steamcmd_running():
            ret = QMessageBox.question(
                self, "steamcmd 正在运行",
                "检测到 steamcmd 正在运行：此刻备份可能拿到不完整的副本"
                "（下载仍在写入）。\n\n建议等下载结束后再备份。仍要现在备份吗？")
            if ret != QMessageBox.StandardButton.Yes:
                self._log.info("已取消备份：等 steamcmd 结束后再来")
                return
        self._log.info(
            f"开始备份 mod {m.mod_id}（robocopy 同步执行，"
            "大目录期间界面会冻结，请耐心等待）…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rep = self._make_backup_engine().backup_mod(m.mod_id)
            err = None
        except Exception as exc:
            # 引擎约定"操作层面不成立走报告"，能抛到这里的都是意外
            rep, err = None, str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if err is not None:
            self._log.error(f"备份未能完成：{err}")
            QMessageBox.critical(self, "备份失败", f"发生意外错误：{err}")
            return
        self._report_backup(rep, m)

    def _report_backup(self, rep, m) -> None:
        """把引擎报告翻译成控制台日志和必要的弹窗。
        备份记录本身已入 backups 表（数据库记结构化事实），
        这里只说人话结论（决策 18 两层分离）。"""
        for w in rep.warnings:
            self._log.warn(w)
        if rep.ok:
            rec = rep.backup
            self._log.ok(
                f"备份完成：mod {m.mod_id} → {rec.backup_path}"
                f"（{fmt_size(rec.size_bytes)}，版本 {rec.version_timeupdated}）")
            for name in rep.cleaned:
                self._log.info(f"保留策略清腾旧备份：{name}")
        else:
            self._log.error(f"备份失败：{rep.error}")
            QMessageBox.warning(self, "备份失败", rep.error)

    def _on_scan_local(self) -> None:
        """扫描当前游戏的 acf，回填本地三件套（同步执行，本地 IO 毫秒级）。

        流程：核对下载目录（死路径重推导）→ 定位 acf → 解析条目
        → 与库内状态做差 → 按计划写库 → 刷新界面。
        中间任何一步不成立都以弹窗说明原因，不让用户猜。
        """
        if self._game is None:
            QMessageBox.information(self, "扫描本地", "请先选择游戏档案。")
            return
        root = self._steamcmd_root()
        if root is None:
            QMessageBox.warning(
                self, "扫描本地",
                "尚未设置 steamcmd 程序路径，无法定位工坊账本文件。\n"
                "请先到设置页填写 steamcmd 程序（steamcmd.exe）的完整路径。")
            return
        # 顺手核对档案的下载目录（决策 21：它永远可由 steamcmd 位置推导）。
        # 档案建好后 steamcmd 若挪了位置，旧目录就成了死路径——
        # 现值在盘上不存在时按当前位置重新推导，推出不同值就写回档案，
        # 不让死路径长期潜伏（核验页/备份引擎落地后同款核对在那里复用）。
        effective_dir, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id,
                                   download_dir=effective_dir)
            self._game = self._repo.get_game(self._game.app_id)  # 内存同步
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective_dir}")
        acf = localScanner.locate_acf(root, self._game.app_id)
        if acf is None:
            QMessageBox.warning(
                self, "扫描本地",
                f"在 steamcmd 的工坊目录（{root}\\steamapps\\workshop）下，"
                f"没有找到 appworkshop_{self._game.app_id}.acf。\n"
                "请检查：① 设置页的 steamcmd 程序路径是否填对；"
                "② 该游戏的 mod 是否用这台 steamcmd 下载过"
                "（Steam 客户端订阅下载的内容不在这个目录树里）。")
            return
        try:
            result = localScanner.scan_acf(acf)
        except ValueError as exc:
            # acf 内容结构性损坏（解析器约定用 ValueError 表达）
            self._log.error(f"扫描本地失败：{exc}")
            QMessageBox.critical(self, "扫描本地", str(exc))
            return
        if not result.items:
            note = (f"（跳过 {len(result.skipped)} 条不完整条目）"
                    if result.skipped else "")
            self._log.warn(f"扫描本地：acf 中没有任何工坊条目{note}")
            return
        # 先取库内状态做差，再统一写库——扫描器自己不碰数据库
        status_map = {m.mod_id: m.status
                      for m in self._repo.list_mods(self._game.app_id)}
        plan = localScanner.diff_plan(
            result.items, status_map, game_id=self._game.app_id)
        rep = localScanner.apply(self._repo, plan)
        self._reload()

        skipped = (f"，跳过 {len(result.skipped)} 条不完整条目"
                   if result.skipped else "")
        self._log.ok(
            f"扫描本地完成（{self._game.name}）：共 {len(result.items)} 条，"
            f"回填 {rep.updated}（首次确认下载 {rep.transitioned}），"
            f"新入库 {rep.inserted}{skipped}")
        # 决策 23 质量谓词的产出：疑似下载中断的条目没有入账，单独提醒
        # （与上面"不完整条目"分开说——格式类要排查工具/文件，
        # 中断类的修复动作就是重新下载）
        n_interrupted = len(result.interrupted)
        if n_interrupted:
            ids_text = "、".join(result.interrupted[:10]) + \
                       ("…" if n_interrupted > 10 else "")
            self._log.warn(
                f"发现 {n_interrupted} 条疑似下载中断的条目，未入账："
                f"{ids_text}；如需修复请重新下载（命令生成页勾选对应编号）")
        # size 类异常不拦入账，扫描器聚合好的警告原样转述
        for w in result.warnings:
            self._log.warn(f"扫描本地：{w}")
        if rep.inserted:
            self._log.info(
                "新入库条目缺标题等远端信息，建议跑一次更新检测补齐")
