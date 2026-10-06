"""备份管理页
"""
"""
gui/backupPage.py · mod 内容备份的交互层。

分工：引擎 core/backupManager 拥有全部文件操作与账目逻辑（复制/
登记/保留策略清理/恢复/删除，内置"先核对再动手"的路径保险丝）；
本页只做四件事——勾选、确认、线程、把引擎的报告翻成人话。
本页不直接碰任何 mod 文件，只与 BackupManager / ModRepository
接口交互，零 SQL。

交互模型：
- 操作对象一律来自「选」列的勾选框。勾选记备份 id、不记行号——
  表格排序/重建后行号会漂，只有 id 是稳定的；
- 动作收进三个下拉菜单（QToolButton ▾），表格右键菜单与
  「对选中 ▾」共用同一组 QAction（一个动作一个定义、两个入口）：
  备份 ▾（备份 mod… ／ 备份数据库——另一种备份，互不相干）；
  对选中 ▾（全选/清空｜恢复所选（恰好勾 1 份——批量恢复要求每份
  恢复前都先备份当前版本，交互又重又危险，不做）｜钉住/取消钉住
  （纯账目、可逆、不打扰）｜删登记…（只删账，盘上文件原样保留）｜
  删除备份文件…（先盘后账，后台线程逐份执行，某份失败不影响
  其余）｜打开所在文件夹（恰好勾 1 份））；
  目录 ▾（打开备份目录／重定位…／搬家…）；
- 停止/刷新/视图切换保持独立按钮：停止只在任务进行时可用，
  刷新高频，视图切换要一眼看到当前在哪一态；
- 表格排序：显示给人看的可读文本，比较用库内原始值（自定义
  排序键），"9.9 MiB" 不会排到 "13.7 MiB" 后面；排序选择记住；
- 两种视图：平铺（默认按备份时间新→旧）↔ 按 mod 分组（组头
  一行跨整表宽度：标题+编号+份数+总大小）。分组态不排序；
- 信息件：steamcmd 运行横幅（在跑时备份只警告、恢复被引擎直接
  拒绝——两边口径不同是引擎故意的）、备份目录状态、盘上三态
  （✓/失联/—）、统计行。

备份区目录结构（备份轮定稿）：两层 <备份根>/<mod编号>/<备份
文件夹>，旧的单段记录混存兼容——路径拼接对两种段数天然兼容，
悬浮提示里的"备份目录名"会看到带斜杠的两段式。

线程规矩：备份/批量删除放后台线程逐份顺序执行（robocopy 自带
多线程，外层再并行只会互抢磁盘）；worker 引用只在 finished 收尾
函数里释放；同一时刻只跑一个任务（单飞互斥）；停止 = 批间停止
（正在处理的那份做完，绝不把一份备份删一半）；恢复没有停止点，
等它跑完；shutdown() 供主窗口退出统一调用。

危险操作的确认口径（数据先可见再动手）：确认框把"将动多少份、
合计多大、都涉及哪些"摆全，默认按钮是"否"，回车不会误触发。
"""


from pathlib import Path

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal

from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, )
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout, QLineEdit,
    QWidget, )

from core.backupManager import BackupManager, steamcmd_running
from core.models import Backup, Game
from core.urlParser import WORKSHOP_URL_TEMPLATE
from gui.backupMoveDialog import BackupMoveDialog
from gui.backupRelocateDialog import BackupRelocateDialog
from gui.logBus import LogBus
from gui.theme import system_prefers_dark, font_px
from core.formatters import abs_time, fmt_size, status_zh
from core import steamPaths  # 档案未设备份目录时按 steamcmd 位置推导兜底

# ---- 与设置页核对过的真键名（settingsPage.py）。注意配额在设置页
# 以 GB 计（人好填），引擎以字节计（好比较），换算只在 _make_manager 做 ----
_KEY_KEEP_PER_MOD = "backup_keep_per_mod"
_KEY_QUOTA_GB = "backup_total_quota_gb"

# ---- 状态色（与重定位对话框同源，全局只有这一份口径）----
_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_FAIL = "#e5484d"

# 组头配色（按 mod 分组视图）：底色、字色都按主题各锁一个定值。
# 只锁底色不锁字色是不行的——字色会被主题 QSS 或旧版遗留的浅色字
# 抢走，深浅一叠加就糊。亮色=浅灰底黑字；深色=深灰底白字。
_GROUP_BG_LIGHT = "#e0e0e0"
_GROUP_FG_LIGHT = "#1a1a1a"
_GROUP_BG_DARK = "#3a3f47"
_GROUP_FG_DARK = "#f0f0f0"

# ---- 列布局：列号显式起名，填充代码一律用列号，不写魔法数字 ----
COL_CHECK = 0         # 勾选框：操作对象的唯一来源
COL_URL = 1           # ↗ 打开该 mod 的工坊页面
COL_TITLE = 2         # mod 标题（伸展列）
COL_MOD_ID = 3
COL_BACKUP_TIME = 4   # 备份时间 = 记录创建时间（created_at）
COL_VERSION_TIME = 5  # 版本时间 = 备份捕获的游戏版本（version_timeupdated）
COL_SIZE = 6
COL_DISK = 7          # 盘上三态：✓ / 失联 / —
COL_PIN = 8
COL_NOTE = 9
_COL_HEADERS = ["选", "↗", "mod 标题", "mod 编号", "备份时间",
                "版本时间", "大小", "盘上", "钉", "备注"]
# 盘上列的排序次序：正常的在前、失联居中、没档案垫底
_DISK_RANK = {"✓": 0, "失联": 1, "—": 2}


class _SortItem(QTableWidgetItem):
    """带数值排序键的单元格：显示文本给人看，比较大小用库内原值。
    QTableWidget 点表头排序默认按显示文本比——"9.9 MiB" 会排到
    "13.7 MiB" 后面；挂上原始数值后排序才符合直觉。
    """

    def __init__(self, text: str, key) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem):
            return self._key < other._key
        return super().__lt__(other)


class _BackupWorker(QThread):
    """后台备份线程：逐个 mod 顺序调引擎。每完成一个报一次进度。
    停止协议：批间生效（一个 mod 备完才停），进行中的复制不打断。
    """

    one_done = Signal(int, int, bool, str)  # 已完成数, 总数, 成功?, 一行摘要
    all_done = Signal(bool, list)           # (是否手动停止, [(编号, 成功?, 摘要)])
    crashed = Signal(str)                   # 预期外异常（bug 性质）

    def __init__(self, manager: BackupManager, mod_ids: list[int]) -> None:
        super().__init__()
        self._manager = manager
        self._ids = list(mod_ids)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        results: list[tuple[int, bool, str]] = []
        total = len(self._ids)
        for i, mid in enumerate(self._ids, 1):
            if self._stop:
                self.all_done.emit(True, results)
                return
            try:
                rep = self._manager.backup_mod(mid)
            except Exception as exc:  # 引擎约定操作层失败走报告；到这里=bug
                self.crashed.emit(f"备份 mod {mid} 时出现预期外错误：{exc}")
                return
            line = f"mod {mid} 备份成功" if rep.ok else f"mod {mid} 失败：{rep.error}"
            results.append((mid, rep.ok, line))
            self.one_done.emit(i, total, rep.ok, line)
        self.all_done.emit(False, results)


class _BatchWorker(QThread):
    """后台批量删除线程：逐份调引擎 delete_backup（先删盘上目录、
    成功才删记录）。每份独立——某份失败不影响其余；批间可停止，
    停止时未执行的份原样保留（记录和文件都在）。
    """

    one_done = Signal(int, int, bool, str)
    all_done = Signal(bool, list)  # (是否手动停止, [(备份id, 成功?, 说明)])
    crashed = Signal(str)

    def __init__(self, manager: BackupManager, recs: list[Backup]) -> None:
        super().__init__()
        self._manager = manager
        self._recs = list(recs)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        results: list[tuple[int, bool, str]] = []
        total = len(self._recs)
        for i, rec in enumerate(self._recs, 1):
            if self._stop:
                self.all_done.emit(True, results)
                return
            try:
                ok, msg = self._manager.delete_backup(rec.id)
            except Exception as exc:  # 到这里=bug，不是操作层失败
                self.crashed.emit(
                    f"删除备份 {rec.id}（mod {rec.mod_id}）时出现预期外错误：{exc}")
                return
            line = (f"备份 {rec.id}（mod {rec.mod_id}）："
                    + (msg if msg else ("已删除" if ok else "失败")))
            results.append((rec.id, ok, line))
            self.one_done.emit(i, total, ok, line)
        self.all_done.emit(False, results)


class _CallWorker(QThread):
    """单任务线程（恢复用）：跑一个引擎调用，结果/异常发回主线程。"""

    done = Signal(object)
    crashed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.crashed.emit(str(exc))


class _PickModsDialog(QDialog):
    """勾选要备份的 mod（每行带 ↗ 直达工坊页面——"选 mod 做某事"的
    窗口必须能顺手查详情）。"""

    def __init__(self, mods, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择要备份的 mod")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"共 {len(mods)} 个已下载 mod，勾选要备份的：", self))
        self._list = QListWidget(self)
        self._cbs: list[tuple[int, QCheckBox]] = []  # (mod_id, 勾选框)
        for m in mods:
            item = QListWidgetItem(self._list)
            row = QWidget(self._list)
            rh = QHBoxLayout(row)
            rh.setContentsMargins(8, 2, 4, 2)
            cb = QCheckBox(f"{m.mod_id} {m.title or '（无标题）'}", row)
            cb.setChecked(True)
            rh.addWidget(cb, 1)
            btn_open = QPushButton("↗", row)
            btn_open.setFixedWidth(28)
            btn_open.setToolTip("在浏览器打开该 mod 的创意工坊页面")
            btn_open.setEnabled(bool(m.url))
            if m.url:
                btn_open.clicked.connect(
                    lambda _=False, u=m.url: QDesktopServices.openUrl(QUrl(u)))
            rh.addWidget(btn_open)
            self._list.setItemWidget(item, row)
            item.setData(Qt.ItemDataRole.UserRole, m.mod_id)
            self._cbs.append((m.mod_id, cb))
        v.addWidget(self._list, 1)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(460, 480)

    def selected_ids(self) -> list[int]:
        return [mid for mid, cb in self._cbs if cb.isChecked()]


class BackupPage(QWidget):
    """备份管理页。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        # 分组头条目登记簿：切主题后回本页要重新上色（见 showEvent）。
        self._group_heads: list[QTableWidgetItem] = []

        self._repo = repo
        self._settings = settings
        self._log = log if log is not None else LogBus()
        self._game: Game | None = None

        self._rows: list[Backup] = []          # 当前档案的备份记录（新→旧）
        self._row_by_id: dict[int, Backup] = {}  # 勾选记 id 不记行号（关键）
        self._mods_by_id: dict[int, object] = {}  # 标题对照表，_reload 时取齐
        self._view_mode = "flat"               # flat=平铺 / grouped=按 mod 分组
        self._sort_state = (COL_BACKUP_TIME, Qt.SortOrder.DescendingOrder)
        self._building = False                 # 重建表格期间抑制 itemChanged
        # 搜索防抖（300ms，与更新对照页同一手感）
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply_filter)

        self._busy = False

        self._backup_worker: _BackupWorker | None = None
        self._batch_worker: _BatchWorker | None = None
        self._job_worker: _CallWorker | None = None

        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("备份管理", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # steamcmd 运行横幅：默认藏起，_reload 时检测后决定显示
        self._steam_banner = QLabel(self)
        self._steam_banner.setWordWrap(True)   # 可能变长的标签一律开换行
        self._steam_banner.setVisible(False)
        root.addWidget(self._steam_banner)

        tip = QLabel(
            "mod 内容备份：恢复前会自动备份当前版本，恢复失败自动回退，不会丢数据；"
            "删除备份文件是先删盘上目录、成功才删记录；钉住的备份豁免自动清理，"
            "但不挡手动删除。\n"
            "操作对象一律来自「选」列的勾选框（表格上点右键是同一组动作）；"
            "点表头可排序，也可切换按 mod 分组查看。", self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # ---- 工具行：三个下拉收齐全部动作，一行不再铺满按钮 ----
        bar = QWidget(self)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)

        # 「备份 ▾」：发起备份。备份数据库与 mod 内容备份是两回事，
        # 用分隔线隔开、文字里写明，避免误当成一回事
        self._btn_backup_menu = QToolButton(bar)
        self._btn_backup_menu.setText("备份 ▾")
        self._btn_backup_menu.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_backup_menu.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        m_backup = QMenu(self._btn_backup_menu)
        m_backup.setToolTipsVisible(True)  # QMenu 默认不显示悬浮说明，必须显式开
        self._act_backup_mod = QAction("备份 mod…", m_backup)
        self._act_backup_mod.setToolTip(
            "把勾选的 mod 内容复制进备份区；保留份数与配额按设置页执行")
        self._act_backup_mod.triggered.connect(self._start_backup)
        m_backup.addAction(self._act_backup_mod)
        m_backup.addSeparator()
        self._act_backup_db = QAction("备份数据库（另一种备份）…", m_backup)
        self._act_backup_db.setToolTip(
            "生成数据库快照文件（与 mod 内容备份互不相干），滚动保留 3 份")
        self._act_backup_db.triggered.connect(self._backup_database)
        m_backup.addAction(self._act_backup_db)
        self._btn_backup_menu.setMenu(m_backup)
        h.addWidget(self._btn_backup_menu)

        # 「对选中 ▾」：跟着勾选走的全部动作。
        # 这组 QAction 同时挂进表格右键菜单——一个动作一个定义、
        # 两个入口，永不出现两套口径
        self._btn_sel_menu = QToolButton(bar)
        self._btn_sel_menu.setText("对选中")
        self._btn_sel_menu.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_sel_menu.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        m_sel = QMenu(self._btn_sel_menu)
        m_sel.setToolTipsVisible(True)
        self._act_check_all = QAction("全选", m_sel)
        self._act_check_all.triggered.connect(
            lambda: self._set_all_checks(True))
        m_sel.addAction(self._act_check_all)
        self._act_check_none = QAction("清空选择", m_sel)
        self._act_check_none.triggered.connect(
            lambda: self._set_all_checks(False))
        m_sel.addAction(self._act_check_none)
        m_sel.addSeparator()
        self._act_restore = QAction("恢复所选（恰好勾 1 份）", m_sel)
        self._act_restore.setToolTip(
            "把勾选的那一份备份复制回下载目录；恢复前自动备份当前版本，"
            "失败自动回退")
        self._act_restore.triggered.connect(self._start_restore)
        m_sel.addAction(self._act_restore)
        self._act_pin = QAction("钉住", m_sel)
        self._act_pin.setToolTip("勾选的备份全部钉住（豁免自动清理，可随时取消）")
        self._act_pin.triggered.connect(lambda: self._pin_checked(True))
        m_sel.addAction(self._act_pin)
        self._act_unpin = QAction("取消钉住", m_sel)
        self._act_unpin.setToolTip("勾选的备份全部取消钉住")
        self._act_unpin.triggered.connect(lambda: self._pin_checked(False))
        m_sel.addAction(self._act_unpin)
        m_sel.addSeparator()
        self._act_del_rec = QAction("删登记…（只删账）", m_sel)
        self._act_del_rec.setToolTip(
            "只删除数据库登记，盘上文件原样保留（之后不再被本工具跟踪）")
        self._act_del_rec.triggered.connect(self._delete_records)
        m_sel.addAction(self._act_del_rec)
        self._act_del_file = QAction("删除备份文件…（连盘上一起删）", m_sel)
        self._act_del_file.setToolTip(
            "删除磁盘目录和数据库记录，不可撤销；先删盘、成功才删账；"
            "逐份独立执行，某份失败不影响其余")
        self._act_del_file.triggered.connect(self._delete_files)
        m_sel.addAction(self._act_del_file)
        m_sel.addSeparator()
        self._act_open_folder = QAction("打开所在文件夹（恰好勾 1 份）", m_sel)
        self._act_open_folder.setToolTip(
            "在资源管理器中打开勾选的那一份备份的文件夹")
        self._act_open_folder.triggered.connect(self._open_selected_folder)
        m_sel.addAction(self._act_open_folder)
        self._btn_sel_menu.setMenu(m_sel)
        h.addWidget(self._btn_sel_menu)

        # 「目录 ▾」：备份位置相关的整页级动作
        self._btn_dir_menu = QToolButton(bar)
        self._btn_dir_menu.setText("目录 ▾")
        self._btn_dir_menu.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn_dir_menu.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        m_dir = QMenu(self._btn_dir_menu)
        m_dir.setToolTipsVisible(True)
        self._act_open_dir = QAction("打开备份目录", m_dir)
        self._act_open_dir.setToolTip("在资源管理器中打开当前游戏的备份目录")
        self._act_open_dir.triggered.connect(self._open_backup_dir)
        m_dir.addAction(self._act_open_dir)
        self._act_relocate = QAction("重定位备份目录…", m_dir)
        self._act_relocate.setToolTip(
            "「盘上」列有失联记录时用：指认备份文件夹现在的位置，"
            "先预演能对回多少条，确认后只改档案的备份目录字段，不动文件")
        self._act_relocate.triggered.connect(self._relocate)
        m_dir.addAction(self._act_relocate)
        self._act_move = QAction("备份搬家…", m_dir)
        self._act_move.setToolTip(
            "备份所在的盘快满时用：生成把备份整体搬到新位置并原地建联接的命令，"
            "记录一个不用改；与重定位的分工见窗口内说明")
        self._act_move.triggered.connect(self._move)
        m_dir.addAction(self._act_move)
        self._btn_dir_menu.setMenu(m_dir)
        h.addWidget(self._btn_dir_menu)

        # 停止 / 刷新 / 视图：保持独立——停止带状态（只在任务进行时可用），
        # 刷新高频，视图切换要一眼看到当前在哪一态
        self._stop_running_btn = QPushButton("停止", bar)
        self._stop_running_btn.setEnabled(False)
        self._stop_running_btn.clicked.connect(self._stop_running)
        self._stop_running_btn.setToolTip(
            "请求停止：当前这一份做完后不再继续（绝不会把一份备份删一半）")
        h.addWidget(self._stop_running_btn)

        self._refresh_btn = QPushButton("刷新", bar)
        self._refresh_btn.clicked.connect(self._reload)
        self._refresh_btn.setToolTip("重新读取备份记录列表（会清空当前勾选）")
        h.addWidget(self._refresh_btn)

        h.addWidget(QLabel("视图：", bar))
        self._view_combo = QComboBox(bar)
        self._view_combo.addItem("平铺")
        self._view_combo.addItem("按 mod 分组")
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        self._view_combo.setToolTip(
            "平铺=全部备份按时间排；按 mod 分组=每个 mod 一组，"
            "组头显示份数和总大小")
        h.addWidget(self._view_combo)
        self._search = QLineEdit(bar)
        self._search.setPlaceholderText("搜索：标题 / 编号 / 备注…")
        self._search.setClearButtonEnabled(True)
        self._search.setToolTip(
            "即时过滤当前列表：不命中的行隐藏，已勾选的原样保留；\n"
            "匹配 mod 标题、编号、备注、备份目录名。清空搜索框恢复全部")
        self._search.textChanged.connect(self._on_search_changed)
        h.addWidget(self._search, 1)
        h.addStretch(1)

        root.addWidget(bar)

        self._game_label = QLabel(self)
        self._game_label.setWordWrap(True)
        root.addWidget(self._game_label)

        self._progress = QProgressBar(self)
        self._progress.setVisible(False)
        root.addWidget(self._progress)

        self._table = QTableWidget(0, len(_COL_HEADERS), self)
        self._table.setHorizontalHeaderLabels(_COL_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_CHECK, 36), (COL_URL, 36), (COL_MOD_ID, 110),
                           (COL_BACKUP_TIME, 150), (COL_VERSION_TIME, 150),
                           (COL_SIZE, 90), (COL_DISK, 60), (COL_PIN, 40),
                           (COL_NOTE, 180)):
            self._table.setColumnWidth(col, width)
        # 重建表格的时序纪律：建表期关排序，_reload 末尾灌完数据再开
        self._table.setSortingEnabled(False)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        header.sortIndicatorChanged.connect(self._on_sort_changed)
        # 右键菜单：与「对选中 ▾」共用同一组动作
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self._table, 1)

        self._count_label = QLabel("", self)
        self._count_label.setWordWrap(True)
        root.addWidget(self._count_label)
        self._btn_backup_menu.setToolTip("发起备份：mod 内容备份或数据库快照")
        self._btn_sel_menu.setToolTip("对勾选的备份执行动作；菜单项与表格右键是同一组动作")
        self._btn_dir_menu.setToolTip("备份位置相关动作：打开目录 / 重定位 / 搬家")

        self._refresh_op_buttons()


    # ---------- 对外（MainWindow 调用）----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._reload()

    def shutdown(self) -> None:
        """主窗口退出前调用（closeEvent 统一调，无需改 MainWindow）。
        备份/批量删除线程批间停止（一份做完才停，绝不删一半）；
        恢复线程没有停止点，等它跑完（单个复制秒级~分钟级）。
        """
        if self._backup_worker is not None:
            self._backup_worker.stop()
            self._backup_worker.wait()
        if self._batch_worker is not None:
            self._batch_worker.stop()
            self._batch_worker.wait()
        if self._job_worker is not None:
            self._job_worker.wait()

    def backup_ids_for(self, app_id: int, mod_ids: list[int]) -> None:
        """mod 库页【备份选中项】的落点：切到对应档案，过滤出可备份
        （已下载）的 mod 后进批次。档案切换在这里做——用户可能在
        A 游戏的库页勾选，而备份页正停在 B 游戏；不切会备错档案。
        """
        game = self._repo.get_game(app_id)
        if game is None:
            self._log.warn(f"备份取消：找不到游戏档案 {app_id}")
            return
        self.set_game(game)
        downloadable = {m.mod_id for m in self._repo.list_mods(app_id)
                        if m.status == "downloaded"}
        ids = [i for i in mod_ids if i in downloadable]
        if not ids:
            QMessageBox.information(
                self, "备份选中项", "勾选的 mod 里没有已下载的，无内容可备份。")
            return
        skipped = [i for i in mod_ids if i not in downloadable]
        if skipped:
            # 被跳过的按实际状态报（status_zh 原话）——库里无内容的状态
            # 不止一种，手写"未下载"会和列表里看到的词对不上号
            by_status: dict[str, int] = {}
            for i in skipped:
                m = self._repo.get_mod(i)
                name = status_zh(m.status) if m is not None else "记录缺失"
                by_status[name] = by_status.get(name, 0) + 1
            detail = "、".join(f"{k} {v} 个" for k, v in by_status.items())
            self._log.info(
                f"备份批次：跳过 {len(skipped)} 个无本地内容的 mod（{detail}）")
        self._run_backup(ids)

    # ---------- 内部：公共小件 ----------

    def _make_manager(self) -> BackupManager:
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 3)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 10)
        return BackupManager(
            self._repo,
            keep_per_mod=keep,
            # 0 = 不限；GB → 字节只在边界换算一次
            quota_bytes=(quota_gb * 1024 ** 3) if quota_gb > 0 else None,
            steamcmd_path=self._settings.get("steamcmd_path"))

    def _checked(self) -> list[Backup]:
        """勾选的备份记录（按表格当前显示顺序）。
        通过条目上的"可勾选"标记识别数据行——分组头行没有这个标记，
        天然跳过；记录本体用 id 从 _row_by_id 取，不按行号反查：
        排序/重建后行号会漂，id 不会。
        """
        out: list[Backup] = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, COL_CHECK)
            if it is None:
                continue
            if not (it.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            if it.checkState() == Qt.CheckState.Checked:
                rec = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
                if rec is not None:
                    out.append(rec)
        return out

    def _title_of_id(self, mod_id: int) -> str:
        mod = self._mods_by_id.get(mod_id)
        return (mod.title if mod is not None else None) or "（无标题或记录缺失）"

    def _title_of(self, b: Backup) -> str:
        return self._title_of_id(b.mod_id)

    def _confirm_batch(self, recs: list[Backup], title: str, what: str) -> bool:
        """危险批量动作的统一确认框：把"将动多少份、合计多大、都涉及
        哪些 mod"摆出来再让用户拍板（数据先可见再动手）。
        默认按钮是"否"——回车不会误触发。
        """
        total = sum(b.size_bytes or 0 for b in recs)
        pin_n = sum(1 for b in recs if b.pinned)
        lines = [f"对 {len(recs)} 份备份执行：{title}（合计 {fmt_size(total)}）"]
        if pin_n:
            lines.append(f"其中 {pin_n} 份已钉住——钉住只豁免自动清理，不挡手动删除。")
        lines.append(what)
        lines.append("")
        lines.append("涉及：")
        for b in recs[:8]:
            lines.append(f" · {b.mod_id} {self._title_of(b)}｜"
                         f"{abs_time(b.version_timeupdated)}｜{fmt_size(b.size_bytes)}")
        if len(recs) > 8:
            lines.append(f" ……以及另外 {len(recs) - 8} 份")
        ret = QMessageBox.question(
            self, title, "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ret == QMessageBox.StandardButton.Yes

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        # 三个菜单按钮 + 刷新 + 视图：任务进行中一律禁用，
        # 防止"边删边换视图"这类中途变阵
        for b in (self._btn_backup_menu, self._btn_sel_menu,
                  self._btn_dir_menu, self._refresh_btn):
            b.setEnabled(not busy)
        self._view_combo.setEnabled(not busy)
        # 停止按钮只在启动任务的代码里点亮（worker.start 之后），
        # 收尾时在这里统一熄灭
        self._stop_running_btn.setEnabled(False)
        self._refresh_op_buttons()

    def _refresh_op_buttons(self) -> None:
        """按当前勾选数刷新"对选中"组动作的可用状态与按钮文字。
        菜单项与右键菜单共用同一组 QAction，改这里两处同时生效。"""
        n = 0 if self._building else len(self._checked())
        has = (n > 0) and not self._busy
        single = (n == 1) and not self._busy
        self._act_restore.setEnabled(single)
        self._act_pin.setEnabled(has)
        self._act_unpin.setEnabled(has)
        self._act_del_rec.setEnabled(has)
        self._act_del_file.setEnabled(has)
        self._act_open_folder.setEnabled(single)
        # 按钮文字带上勾选数（与 mod 库页"已勾选 N"同一思路），
        # 不打开菜单也知道动作会落到几份上
        self._btn_sel_menu.setText(f"对选中({n})" if n else "对选中")

    def _on_context_menu(self, pos) -> None:
        """表格右键菜单：与「对选中 ▾」完全同一组 QAction——
        菜单只是同一组动作的另一个入口，不会出现两套口径。"""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addAction(self._act_check_all)
        menu.addAction(self._act_check_none)
        menu.addSeparator()
        menu.addAction(self._act_restore)
        menu.addAction(self._act_pin)
        menu.addAction(self._act_unpin)
        menu.addSeparator()
        menu.addAction(self._act_del_rec)
        menu.addAction(self._act_del_file)
        menu.addSeparator()
        menu.addAction(self._act_open_folder)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        # 重建表格期间 setItem 会触发大量 itemChanged，全部忽略；
        # 只有关用户点勾选框时才需要刷新按钮状态
        if self._building:
            return
        if item.column() == COL_CHECK:
            self._refresh_op_buttons()

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """↗ 列：点单元格打开工坊页面。
        用文本单元格而不是每行挂按钮部件——几百行挂部件又重又卡，
        点击时从条目数据里取网址（数据跟着条目走，排序后也不会错位）。
        """
        if col != COL_URL:
            return
        item = self._table.item(row, col)
        if item is None:
            return
        url = item.data(Qt.ItemDataRole.UserRole)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_sort_changed(self, col: int, order: Qt.SortOrder) -> None:
        # 记住用户的排序选择，刷新/重载后照旧。
        # 排序搬的是条目、行隐藏挂的是行号——排序后重滤一次对齐
        self._sort_state = (col, order)
        self._apply_filter()

    def _on_search_changed(self, _text: str) -> None:
        """搜索框防抖：停手 300ms 才真正过滤。"""
        self._search_timer.start()

    def _apply_filter(self) -> None:
        """把搜索框关键词落到表格：不命中的行隐藏（只隐藏不重建，
        勾选原样保留）。平铺视图逐行匹配；分组视图整组判定——
        组内任一成员命中，组头与命中成员显示，未命中成员隐藏。
        空关键词 = 全部显示。重复执行无害（幂等）。"""
        kw = self._search.text().strip().casefold()
        rows = self._table.rowCount()
        if not kw:
            for r in range(rows):
                self._table.setRowHidden(r, False)
            return
        hit = [False] * rows
        for r in range(rows):
            it = self._table.item(r, COL_CHECK)
            if it is None or not (it.flags()
                                  & Qt.ItemFlag.ItemIsUserCheckable):
                continue  # 组头行在下面整组判定
            b = self._row_by_id.get(it.data(Qt.ItemDataRole.UserRole))
            if b is None:
                continue
            hay = " ".join((
                self._title_of(b), str(b.mod_id),
                b.note or "", b.backup_path or "",
            )).casefold()
            hit[r] = kw in hay
        if self._view_mode == "grouped":
            i = 0
            while i < rows:
                head_it = self._table.item(i, 0)
                if head_it is None or (head_it.flags()
                                       & Qt.ItemFlag.ItemIsUserCheckable):
                    i += 1
                    continue
                j = i + 1
                group_hit = False
                while j < rows:
                    m_it = self._table.item(j, COL_CHECK)
                    if m_it is None or not (m_it.flags()
                                            & Qt.ItemFlag.ItemIsUserCheckable):
                        break  # 下一组的组头
                    if hit[j]:
                        group_hit = True
                    j += 1
                hit[i] = group_hit
                i = j
        for r in range(rows):
            self._table.setRowHidden(r, not hit[r])

    def _on_view_changed(self, idx: int) -> None:
        self._view_mode = "grouped" if idx == 1 else "flat"
        self._reload()

    def _set_all_checks(self, on: bool) -> None:
        self._building = True
        # 批量改勾选状态，别每行都刷一遍按钮。
        # 全选只勾可见行：搜索过滤在场时，勾进看不见的行=盲删隐患；
        # 清空选择仍作用所有行（只往安全方向走）
        for r in range(self._table.rowCount()):
            if on and self._table.isRowHidden(r):
                continue
            it = self._table.item(r, COL_CHECK)
            if it is not None and (it.flags()
                                   & Qt.ItemFlag.ItemIsUserCheckable):
                it.setCheckState(
                    Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self._building = False
        self._refresh_op_buttons()

    def _refresh_banner(self) -> None:
        """steamcmd 运行横幅（引擎 R7 的界面表达）：备份只警告不拦，
        恢复被引擎直接拒绝——横幅把两边口径都说清，避免用户以为
        "横幅只是吓唬人"。"""
        if steamcmd_running():
            self._steam_banner.setText(
                "⚠ 检测到 steamcmd 正在运行：此时备份可能拿到不完整副本"
                "（下载仍在写入），建议等它结束再备份。恢复操作会被直接拒绝——"
                "恢复要把整份内容写回下载目录，和正在下载的 steamcmd 抢同一批文件，"
                "两边都会写坏；拒绝是保护，等下载结束再恢复。"
            )
            self._steam_banner.setStyleSheet(f"color: {_C_WARN};")
            self._steam_banner.setVisible(True)
        else:
            self._steam_banner.setVisible(False)

    # ---------- 列表重建 ----------

    def _reload(self) -> None:
        """重建表格（唯一入口：切档案、任何动作完成后都回到这里）。
        重建期间的时序纪律：先关排序 → 清表灌数据 → 再开排序。
        带着排序灌数据，Qt 会边灌边排，行和数据会错位。
        """
        self._building = True
        self._table.setSortingEnabled(False)
        self._table.clearSpans()
        self._table.setRowCount(0)
        self._row_by_id = {}
        self._mods_by_id = {}

        self._refresh_banner()

        if self._game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._count_label.setText("")
            self._building = False
            self._refresh_op_buttons()
            return

        bdir_text = (self._game.backup_dir or "").strip()
        bdir = Path(bdir_text) if bdir_text else None

        # 标题对照表一次取齐（行循环里只查字典，不查库）
        self._mods_by_id = {m.mod_id: m
                            for m in self._repo.list_mods(self._game.app_id)}
        ids = set(self._mods_by_id)
        rows = [b for b in self._repo.list_backups(oldest_first=True)
                if b.mod_id in ids]
        self._rows = list(reversed(rows))  # 新 → 旧（平铺默认序）
        self._row_by_id = {b.id: b for b in self._rows}

        # 盘上三态逐份核对（repo 只管账；盘上的事在本页做）
        disk_map: dict[int, str] = {}
        lost = 0
        for b in self._rows:
            if bdir is None:
                disk_map[b.id] = "—"
            elif (bdir / b.backup_path).is_dir():
                disk_map[b.id] = "✓"
            else:
                disk_map[b.id] = "失联"
                lost += 1

        if self._view_mode == "flat":
            self._table.setRowCount(len(self._rows))
            for r, b in enumerate(self._rows):
                self._fill_row(r, b, disk_map[b.id])
            # 数据灌完再开排序，并按记住的列/方向排一次
            self._table.horizontalHeader().setSortIndicatorShown(True)
            self._table.setSortingEnabled(True)
            col, order = self._sort_state
            if col >= len(_COL_HEADERS):
                col, order = COL_BACKUP_TIME, Qt.SortOrder.DescendingOrder
            self._table.sortItems(col, order)
        else:
            self._fill_grouped(disk_map)
            self._table.horizontalHeader().setSortIndicatorShown(False)

        # 顶部信息行：档案 + 备份目录状态
        if bdir_text:
            state = ("盘上存在" if bdir.is_dir()
                     else "盘上不存在（位置指认有误？可用【重定位备份目录】修正）")
        else:
            # 未设置时把推导值亮出来：一眼看到"备份会去哪 / 文件该在哪"，
            # 也方便把老档案补上（编辑档案 →【改为推导值】）
            derived = steamPaths.backup_root_default(
                self._settings.get("steamcmd_path"), self._game.app_id)
            state = ("未设置（首次备份时按 steamcmd 位置推导并写入档案；"
                     + (f"推导值 = {derived}" if derived
                        else "steamcmd 未配置，暂推导不出") + "）")

        self._game_label.setText(
            f"当前游戏：{self._game.name}（{self._game.app_id}）｜"
            f"备份目录：{bdir_text or '—'}（{state}）")

        # 统计行 + 保留策略
        total = sum(b.size_bytes or 0 for b in self._rows)
        pinned_n = sum(1 for b in self._rows if b.pinned)
        if self._rows:
            lost_note = (f"，失联 {lost} 份（盘上已找不到——"
                         f"可用【重定位备份目录】指认新位置）" if lost else "")
            head = (f"共 {len(self._rows)} 份备份，合计 {fmt_size(total)}，"
                    f"钉住 {pinned_n} 份{lost_note}")
        else:
            head = "当前档案还没有备份记录。第一次备份：点【备份 mod…】勾选要备份的 mod。"
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 3)
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 10)
        policy = (f"每个 mod 保留最新 {keep} 份"
                  + (f"，总量上限 {quota_gb} GB" if quota_gb else "，总量不限")
                  + "（设置页修改）")
        self._count_label.setText(f"{head}\n保留策略：{policy}")

        self._building = False
        self._refresh_op_buttons()
        self._apply_filter()

    def _fill_row(self, r: int, b: Backup, on_disk: str) -> None:
        """填一行数据。所有排序用不到的辅助信息放悬浮提示，不占列。"""
        # 勾选列：UserRole 存备份 id——排序/重建后靠它找回记录
        it = QTableWidgetItem()
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable)
        it.setCheckState(Qt.CheckState.Unchecked)
        it.setData(Qt.ItemDataRole.UserRole, b.id)
        self._table.setItem(r, COL_CHECK, it)

        # ↗ 列：文本单元格，点击打开工坊页面（网址存条目数据里）
        mod = self._mods_by_id.get(b.mod_id)
        url = ((mod.url if mod is not None else "") or "").strip()
        if not url:
            # 决策 61④：工坊页地址模板单源 workflows.intakeFlow
            url = WORKSHOP_URL_TEMPLATE.format(b.mod_id)
        url_item = QTableWidgetItem("↗")
        url_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        url_item.setForeground(QBrush(QColor("#5aa9ff")))  # 蓝字提示可点
        url_item.setData(Qt.ItemDataRole.UserRole, url)
        url_item.setToolTip("点击在浏览器打开该 mod 的创意工坊页面")
        self._table.setItem(r, COL_URL, url_item)

        title_item = QTableWidgetItem(self._title_of(b))
        title_item.setToolTip(f"备份目录名：{b.backup_path}"
                              + (f"\n备注：{b.note}" if b.note else ""))
        self._table.setItem(r, COL_TITLE, title_item)

        # 数值列全部用 _SortItem：显示可读文本、排序按原始值
        self._table.setItem(r, COL_MOD_ID, _SortItem(str(b.mod_id), b.mod_id))
        self._table.setItem(
            r, COL_BACKUP_TIME,
            _SortItem(abs_time(b.created_at), b.created_at or 0))
        self._table.setItem(
            r, COL_VERSION_TIME,
            _SortItem(abs_time(b.version_timeupdated),
                      b.version_timeupdated or 0))
        self._table.setItem(
            r, COL_SIZE, _SortItem(fmt_size(b.size_bytes), b.size_bytes or 0))

        disk_item = _SortItem(on_disk, _DISK_RANK.get(on_disk, 2))
        if on_disk == "失联":
            disk_item.setForeground(QBrush(QColor(_C_FAIL)))
        elif on_disk == "✓":
            disk_item.setForeground(QBrush(QColor(_C_OK)))
        self._table.setItem(r, COL_DISK, disk_item)

        self._table.setItem(
            r, COL_PIN, _SortItem("是" if b.pinned else "", 1 if b.pinned else 0))
        self._table.setItem(r, COL_NOTE, QTableWidgetItem(b.note or ""))

    def _group_colors(self) -> tuple[QColor, QColor]:
        """组头（底色, 字色）：按当前主题返回成对定值（常量见文件头
        _GROUP_BG_* / _GROUP_FG_*）。主题来源 = 设置键 theme_mode
        （与设置页下拉同一份事实）：dark / light 直接用；auto（跟随
        系统）→ 问 Windows 注册表（见 _system_prefers_dark 的说明，
        别问 Qt）。字色和底色成对返回、成对上色——对比度自己锁死，
        不赌 QSS。"""
        mode = str(self._settings.get("theme_mode") or "auto").strip()
        if mode not in ("dark", "light"):
            mode = "dark" if system_prefers_dark() else "light"

        if mode == "dark":
            return QColor(_GROUP_BG_DARK), QColor(_GROUP_FG_DARK)
        return QColor(_GROUP_BG_LIGHT), QColor(_GROUP_FG_LIGHT)

    def showEvent(self, event) -> None:
        """切到本页时把组头颜色刷一遍。为什么要有它：组头颜色是填表
        那一刻定下的，而换主题发生在设置页——用户换完主题走回本页，
        showEvent 正好接住，颜色当场换好，不用再按【刷新】。
        try/except 防的是旧登记：整表重建后旧条目对象已被回收，
        摸一下会报 RuntimeError，跳过并清出登记簿即可。"""
        super().showEvent(event)
        bg, fg = self._group_colors()
        alive: list[QTableWidgetItem] = []
        for head in self._group_heads:
            try:
                head.setBackground(QBrush(bg))
                head.setForeground(QBrush(fg))
                alive.append(head)
            except RuntimeError:
                continue  # 登记簿里的死条目（整表重建时已回收）
        self._group_heads = alive

    def _fill_grouped(self, disk_map: dict[int, str]) -> None:
        """按 mod 分组视图：组头一行跨整表宽度（标题 + 编号 + 份数 +
        总大小），组内版本时间新→旧，组间按最近备份时间新→旧。
        分组态不排序（排序只在平铺态；组头的跨行格和排序混用会错位）。
        """
        by_mod: dict[int, list[Backup]] = {}
        for b in self._rows:
            by_mod.setdefault(b.mod_id, []).append(b)
        groups = sorted(by_mod.values(),
                        key=lambda rs: max(b.created_at or 0 for b in rs),
                        reverse=True)
        self._table.setRowCount(len(self._rows) + len(groups))
        r = 0
        for rs in groups:
            rs_sorted = sorted(
                rs,
                key=lambda b: (b.version_timeupdated or 0, b.created_at or 0),
                reverse=True)
            mid = rs_sorted[0].mod_id
            gsize = sum(b.size_bytes or 0 for b in rs_sorted)
            head = QTableWidgetItem(
                f"{self._title_of_id(mid)}（{mid}）— "
                f"{len(rs_sorted)} 份 · {fmt_size(gsize)}")
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)  # 组头只展示，不可选/不可勾
            font = head.font()
            font.setBold(True)
            head.setFont(font)
            bg, fg = self._group_colors()
            head.setBackground(QBrush(bg))
            head.setForeground(QBrush(fg))
            self._group_heads.append(head)

            self._table.setItem(r, 0, head)
            self._table.setSpan(r, 0, 1, len(_COL_HEADERS))
            r += 1
            for b in rs_sorted:
                self._fill_row(r, b, disk_map[b.id])
                r += 1

    # ---------- 备份 ----------

    def _start_backup(self) -> None:
        if self._busy or self._game is None:
            return
        mods = [m for m in self._repo.list_mods(self._game.app_id)
                if m.status == "downloaded"]
        if not mods:
            QMessageBox.information(self, "备份 mod", "当前档案没有已下载的 mod。")
            return
        dlg = _PickModsDialog(mods, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._run_backup(dlg.selected_ids())

    def _run_backup(self, ids: list[int]) -> None:
        """备份批次执行核心：勾选对话框与 mod 库页【备份选中项】共用。
        单飞互斥在这里统一把关——不管入口来自哪个按钮，同一时刻
        只允许一个备份/恢复/删除任务。
        """
        if self._busy:
            self._log.warn("已有备份/恢复/删除任务在进行：请等它结束再试")
            return
        if self._game is None or not ids:
            return
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._set_busy(True)
        self._backup_worker = _BackupWorker(self._make_manager(), ids)
        self._backup_worker.one_done.connect(self._on_one_backed)
        self._backup_worker.all_done.connect(self._on_backup_all_done)
        self._backup_worker.crashed.connect(self._on_worker_crashed)
        self._backup_worker.finished.connect(self._on_backup_thread_finished)
        self._backup_worker.start()
        self._stop_running_btn.setEnabled(True)
        self._log.info(f"开始备份 {len(ids)} 个 mod…")


    def _on_one_backed(self, done: int, total: int, ok: bool, line: str) -> None:
        self._progress.setValue(done)
        (self._log.ok if ok else self._log.warn)(f"备份进度 {done}/{total}：{line}")

    def _on_backup_all_done(self, stopped: bool, results: list) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        oks = [r for r in results if r[1]]
        bads = [r for r in results if not r[1]]
        tail = "（手动停止，剩余未执行）" if stopped else ""
        self._log.ok(f"备份结束：成功 {len(oks)}，失败 {len(bads)}{tail}")
        if bads or stopped:
            detail = "\n".join(r[2] for r in bads[:10])
            QMessageBox.warning(self, "备份结束",
                                f"成功 {len(oks)}，失败 {len(bads)}{tail}\n{detail}")
        self._reload()

    def _on_worker_crashed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        self._log.error(message)
        QMessageBox.critical(self, "备份管理", message)

    def _on_backup_thread_finished(self) -> None:
        w = self._backup_worker
        self._backup_worker = None
        if w is not None:
            w.wait()

    # ---------- 恢复（单份；批量恢复首版不做，理由见文件头）----------

    def _start_restore(self) -> None:
        recs = self._checked()
        if self._busy or len(recs) != 1:
            return
        rec = recs[0]
        title = self._title_of(rec)
        ret = QMessageBox.question(
            self, "恢复备份",
            f"把这份备份恢复到下载目录？\n\n"
            f"mod：{rec.mod_id} {title}\n"
            f"备份版本：{abs_time(rec.version_timeupdated)}｜"
            f"大小：{fmt_size(rec.size_bytes)}\n\n"
            "当前内容会被替换。恢复前会自动备份当前版本，"
            "过程失败会自动回退。\n恢复期间请不要操作 steamcmd 或改动下载目录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        manager = self._make_manager()
        self._set_busy(True)
        self._job_worker = _CallWorker(lambda: manager.restore_backup(rec.id))
        self._job_worker.done.connect(self._on_restore_done)
        self._job_worker.crashed.connect(self._on_worker_crashed)
        self._job_worker.finished.connect(self._on_job_thread_finished)
        self._job_worker.start()
        self._log.info(f"开始恢复备份 {rec.id}（mod {rec.mod_id}）…")

    def _on_restore_done(self, report) -> None:
        self._set_busy(False)
        if report.ok:
            # 三种前置情形分别说清（备份轮两分）：版本已知 → 恢复前
            # 自动备份（入账）；版本未知 → 不入账的保命副本（位置在
            # 报告里，确认无误后手动删）；盘上没内容 → 没有可保的。
            # 引擎的 warnings（含保命副本位置）照单发日志
            if report.pre_backup is not None:
                pre = "，恢复前已自动备份当前版本"
            elif report.pre_copy_dir is not None:
                pre = ("，恢复前的保命副本已存到：\n"
                       f"{report.pre_copy_dir}\n"
                       "（不入备份账，确认无误后可手动删除）")
            else:
                pre = "（此前无本地内容，未做恢复前备份）"
            self._log.ok(f"恢复完成{pre}")
            for w in report.warnings:
                self._log.warn(w)
            QMessageBox.information(self, "恢复备份", f"恢复完成{pre}。")


        else:
            self._log.error(f"恢复失败：{report.error}")
            QMessageBox.critical(self, "恢复备份", report.error or "未知错误")
        self._reload()

    def _on_job_thread_finished(self) -> None:
        w = self._job_worker
        self._job_worker = None
        if w is not None:
            w.wait()

    # ---------- 批量：钉住 / 删登记 / 删文件 ----------

    def _pin_checked(self, pinned: bool) -> None:
        """批量钉住/取消钉住：纯账目、可逆，不打扰确认。
        整批包进一个事务——要么全改要么全不动，中途出错账不会留半截。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        try:
            with self._repo.transaction():
                for b in recs:
                    self._repo.set_pinned(b.id, pinned)
        except Exception as exc:
            QMessageBox.critical(self, "钉住", f"失败（账未改动）：{exc}")
            return
        word = "已钉住" if pinned else "已取消钉住"
        self._log.ok(f"{word} {len(recs)} 份备份")
        self._reload()

    def _delete_records(self) -> None:
        """批量删登记：只删数据库记录，盘上文件原样保留。
        注意语义：删完后这些文件成了"账外之物"，本工具不再跟踪——
        确认框里必须把这话说清，用户才知道清理要自己去资源管理器。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        if not self._confirm_batch(
                recs, "删登记",
                "只删除备份登记（数据库记录），磁盘上的备份目录原样保留；\n"
                "之后本工具不再跟踪这些文件，清理请自行到资源管理器操作。"):
            return
        try:
            with self._repo.transaction():
                for b in recs:
                    self._repo.delete_backup_record(b.id)
        except Exception as exc:
            QMessageBox.critical(self, "删登记", f"失败（账未改动）：{exc}")
            return
        self._log.ok(f"已删除 {len(recs)} 条备份登记（盘上文件保留）")
        self._reload()

    def _delete_files(self) -> None:
        """批量删文件（磁盘目录 + 记录，先盘后账）：后台线程逐份执行，
        每份独立——某份失败（路径保险丝拦下/权限问题）不影响其余。
        磁盘删除没有撤回，确认框把清单摆全再动手。
        """
        recs = self._checked()
        if self._busy or not recs:
            return
        if not self._confirm_batch(
                recs, "删除备份文件",
                "将逐份删除磁盘目录和数据库记录，不可撤销；\n"
                "逐份独立执行，某份失败不影响其余；批间可点【停止】。"):
            return
        self._progress.setRange(0, len(recs))
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._set_busy(True)
        self._batch_worker = _BatchWorker(self._make_manager(), recs)
        self._batch_worker.one_done.connect(self._on_batch_one_done)
        self._batch_worker.all_done.connect(self._on_batch_all_done)
        self._batch_worker.crashed.connect(self._on_worker_crashed)
        self._batch_worker.finished.connect(self._on_batch_thread_finished)
        self._batch_worker.start()
        self._stop_running_btn.setEnabled(True)
        self._log.info(f"开始删除 {len(recs)} 份备份（磁盘目录+记录）…")

    def _on_batch_one_done(self, done: int, total: int, ok: bool, line: str) -> None:
        self._progress.setValue(done)
        (self._log.ok if ok else self._log.warn)(f"删除进度 {done}/{total}：{line}")

    def _on_batch_all_done(self, stopped: bool, results: list) -> None:
        self._progress.setVisible(False)
        self._set_busy(False)
        oks = [r for r in results if r[1]]
        bads = [r for r in results if not r[1]]
        tail = "（手动停止，剩余未执行——未执行的份原样保留）" if stopped else ""
        self._log.ok(f"删除结束：成功 {len(oks)}，失败 {len(bads)}{tail}")
        if bads or stopped:
            detail = "\n".join(r[2] for r in bads[:10])
            QMessageBox.warning(self, "删除备份文件",
                                f"成功 {len(oks)}，失败 {len(bads)}{tail}\n{detail}")
        self._reload()

    def _on_batch_thread_finished(self) -> None:
        w = self._batch_worker
        self._batch_worker = None
        if w is not None:
            w.wait()

    # ---------- 停止 ----------

    def _stop_running(self) -> None:
        """停止按钮：对谁生效看当前在跑什么。备份/批量删除是批间停止
        （正在处理的那一份会做完，绝不删一半）；恢复没有停止点。
        """
        if self._backup_worker is not None:
            self._backup_worker.stop()
        if self._batch_worker is not None:
            self._batch_worker.stop()
        self._stop_running_btn.setEnabled(False)

    # ---------- 打开所在文件夹（单份）----------
    def _open_selected_folder(self) -> None:
        recs = self._checked()
        if self._busy or len(recs) != 1 or self._game is None:
            return
        rec = recs[0]
        bdir_text = (self._game.backup_dir or "").strip()
        if not bdir_text:
            # 档案没存备份目录（老档案/迁移来的档案常见）：不再直接说
            # "无法定位"——先按 steamcmd 当前位置推导一次，推导得出就
            # 用它打开。注意：只用于本次打开，绝不写档案——写档案是
            # 账面改动，只走【编辑档案】的「改为推导值」显式通道
            # （延续"备份目录指针不静默自愈"的边界：填空≠改指针，
            # 但读路径允许推导兜底）。
            derived = steamPaths.backup_root_default(
                self._settings.get("steamcmd_path"), self._game.app_id)
            if derived:
                bdir_text = derived
                self._log.info(
                    f"档案未设置备份目录，已按 steamcmd 位置推导打开：{derived}"
                    "（想固定下来：编辑档案 → 备份目录 →【改为推导值】→ 保存）")
            else:
                QMessageBox.information(
                    self, "打开所在文件夹",
                    "当前档案还没有备份目录，且设置页未配置 steamcmd 程序，"
                    "推导不出备份位置。\n"
                    "请把备份文件夹的实际位置填进档案：编辑档案 → 备份目录。")
                return
        target = Path(bdir_text) / rec.backup_path
        if not target.is_dir():
            QMessageBox.warning(
                self, "打开所在文件夹",
                f"盘上找不到这份备份：\n{target}\n\n"
                "可用【重定位备份目录】指认新位置；若文件确实在盘上，"
                "把实际位置填进档案（编辑档案 → 备份目录）即可全部对上。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


    # ---------- 数据库备份 / 打开目录 ----------

    def _backup_database(self) -> None:
        start = (self._game.backup_dir or "") if self._game else ""
        d = QFileDialog.getExistingDirectory(
            self, "选择数据库备份存放目录", start)
        if not d:
            return
        try:
            out = self._repo.backup_to(Path(d))
        except Exception as exc:
            QMessageBox.critical(self, "备份数据库", f"失败：{exc}")
            return
        self._log.ok(f"数据库已备份：{out}")
        QMessageBox.information(self, "备份数据库", f"已生成：{out}")

    def _open_backup_dir(self) -> None:
        if self._game is None or not (self._game.backup_dir or "").strip():
            QMessageBox.information(
                self, "打开备份目录",
                "当前档案还没有备份目录。\n"
                "第一次备份时会按 steamcmd 位置自动推导并写入档案。")
            return
        # 建目录可能失败（盘满/无权限/路径被占用），拦住给人话提示
        try:
            Path(self._game.backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "打开备份目录", f"备份目录无法创建：{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._game.backup_dir))

    # ---------- 备份目录重定位 ----------

    def _relocate(self) -> None:
        """打开重定位对话框：引擎只读预演，确认后只写档案的备份目录
        一个字段。成功后本页立即重载，「盘上」列按新位置重新核对。"""
        if self._game is None:
            QMessageBox.information(self, "重定位备份目录", "当前未选择游戏档案。")
            return
        dlg = BackupRelocateDialog(self._repo, self._game, self._settings,
                                   self, log=self._log)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    # ---------- 备份搬家指引（命令生成，全程只读）----------

    def _move(self) -> None:
        """打开备份搬家指引：与重定位互补——备份都在、只是想换位置时
        用它（物理搬运 + 旧位置建联接，档案记录一个不动）。
        对话框关闭后无条件重载一次：联接建好后旧路径恢复可达，
        「盘上」列按现状重新核对。"""
        if self._game is None:
            QMessageBox.information(self, "备份搬家", "当前未选择游戏档案。")
            return
        # T15 批 3：补传 log=self._log——对话框里点「复制」后，
        # 运行日志多一行回执（与上面 _relocate 的重定位对话框同一套约定）
        dlg = BackupMoveDialog(self._game, self._settings, self,
                               log=self._log)
        dlg.exec()
        self._reload()

