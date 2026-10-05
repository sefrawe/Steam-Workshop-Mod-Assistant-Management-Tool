"""账实核验页
"""
r"""gui/verifyPage.py · 两份只读对账（V2 版，V2.41 收录机制退役）。

对当前档案做两份同步对账（只做目录列表、存在性检查和 readlink，
毫秒级，不值得开线程）：
1. 账本 ↔ 磁盘（core/modVerifier.verify）：库说已下载但盘上没有、
   盘上有目录但账本没记、来历不明的残留文件
2. 账本 ↔ 游戏侧链接（core/modVerifier.verify_junctions）：已下载
   的 mod 在游戏侧 mods 目录里是否正确建了 junction

【V2 与 V1 的差别（一句话版）：本页变成纯只读】
- 收录功能整个退役：账本外条目的入账统一走【入账中心 → 扫描游戏
  目录】——盘点会把账外目录摆成待认领，确认后入账。写库正门只有
  入账中心一处，本页不再二次实现收录。
- "确认勾选的已下载"一并退役：V2 的确认必须用盘点产生的待认领行
  （确认时从盘点取真实版本号），本页不读盘点，给不出版本号，走不
  通确认门。
- 勾选机制随之退役：第一列勾选框、全选框、两个批量按钮都是为收录/
  确认服务的，服务对象没了就一起删。表格从 7 列变 6 列。
- "账新盘旧（疑似）"桶退役：V2 明令不带（V1 决策 115 的抛弃清单
  执行）。引擎如果还返回 stale_top 数据，本页刻意不消费。

【保留的全部能力】
- 右键菜单：复制编号 / 打开工坊页面 / 打开 mod 文件夹 / 修复…
- 修复三选：重新下载（跳命令生成页）／校验重下 validate（页内出
  命令+复制）／标记为已移除（走 repo.mark_deleted 正门）
- 设置游戏侧目录；死路径重推导写回
- junction 巡检节：反向拓扑预检 + verify_junctions 逐条
- 桶筛选（5 桶）+ 关键词过滤 + 重灌式排序
- 三个分区的折叠记忆（QSettings，重启保持）

本页写库只限两处：死路径重推导写回、设置游戏侧目录，外加修复三选
里用户主动选择的"标记为已移除"。除此之外绝不删任何文件、不自动
改状态。MainWindow 零改动：核验毫秒级无后台线程，不需要 shutdown 钩子。
"""
import os
from types import SimpleNamespace

from PySide6.QtCore import Qt, QSettings, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QScrollArea,
)

from core import modVerifier, steamPaths
from core.appSettings import AppSettings
from core.commandBuilder import build_validate_copy_text
from core.models import Game
from core.constants import WORKSHOP_URL_TEMPLATE
from core.formatters import status_zh          # V2：formatters 住 core
from gui.logBus import LogBus                  # V2：LogBus 独立成文件
from gui.modFolderOpener import open_mod_folder
from gui.collapsibleSection import CollapsibleSection as _Section
from gui.theme import font_px  # 字号单源（D25）

# 表格 6 列：勾选列（"选"）已随收录/确认退役
_COLUMNS = ["编号", "标题", "账本状态", "盘上情况", "建议", "操作"]

# 行类别（kind）：筛选下拉/右键菜单/双击都按它分流。
# missing/empty = 修复类；claim = 账本外（本页只指路）；confirm =
# 已收录+盘上有内容（本页只指路）；keep = 软删除/失效保留文件（只提示）
_BUCKETS = [
    ("全部行", None),
    ("盘上缺失（已下载）", "missing"),
    ("空目录（疑似中断）", "empty"),
    ("账本外（盘上有目录）", "claim"),
    ("已收录·盘上有内容", "confirm"),
    ("软删除/失效·保留文件", "keep"),
]

_GUIDE_PARAS = (
    "两把尺子先分清：本页核验看的是【磁盘】（含反向联接后的游戏真实"
    "目录）；【扫描本地】读的是 steamcmd 自己的账本文件（acf）。所以"
    "核验能看见的条目，扫描本地未必认——两者对不上不是故障。",

    "各行含义：账说「已下载」但盘上缺失/为空 → 点【修复…】三选一；"
    "「账本外」（盘上有目录、账本没登记）→ 到入账中心收编；「已收录」"
    "且盘上有内容 → 到入账中心确认补版本；软删除/失效记录盘上保留"
    "文件 → 正常，不动。",

    "账本外、已收录待确认两类，本页只看不办：入账（含补版本号）统一"
    "走【入账中心 → 扫描游戏目录】——盘点会把它们摆成待认领，确认时"
    "一并取真实版本。入账正门只有一处，账本不会被两头改。",

    "修复三选：重新下载（增量，日常首选）／校验重下 validate（怀疑"
    "文件被改坏时用，会把改动冲回原版）／标记为已移除（本工具不再为"
    "它生成命令；其他前端清单里还有它仍可能被下回来）。双击缺失/"
    "空目录行 = 快捷普通重下。",

    "本页不删任何文件；写库只发生在你主动的动作（修复三选里的"
    "「标记为已移除」／设置游戏侧目录）与死路径自动重推导。链接巡检"
    "只报不修；修不修、怎么修由你决定。",
)

# 折叠记忆的三个键：QSettings 的 session/ 命名空间，值 "1"/"0"
_SES_SEC_TABLE = "session/verify_sec_table"    # 问题明细
_SES_SEC_NN = "session/verify_sec_nn"          # 非数字明细
_SES_SEC_JUNC = "session/verify_sec_junc"      # 链接巡检明细


class VerifyPage(QWidget):
    # 双击"缺失/空目录"行（或修复三选里选"重新下载"）时发出，
    # 参数 = mod id 列表；MainWindow 负责跳命令生成页并聚焦
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings,
                 parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()   # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._rows_spec: list[dict] = []   # 本轮核验的行清单（筛选/排序数据源）
        self._titles: dict[int, str] = {}  # 账内条目标题（核验时一次查询取齐）
        self._build_ui()
        self._restore_fold_memory()

    # ---------- UI 构建 ----------
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

        title = QLabel("账实核验", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        self._jcfg_label = QLabel("", self)
        self._jcfg_label.setWordWrap(True)   # 长路径换行，别撑宽窗口
        root.addWidget(self._jcfg_label)

        tip = QLabel(
            "核验本身只读——写库只发生在你主动的动作（修复三选里的"
            "「标记为已移除」／设置游戏侧目录），不会删任何文件、不自动"
            "改状态。两把尺子的区别与各桶含义，点开下方【先读我】。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        root.addWidget(self._make_guide())

        # ---- 工具行：桶筛选 / 关键词 / 排序 ----
        frow = QWidget(self)
        fh = QHBoxLayout(frow)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.addWidget(QLabel("查看：", frow))
        self._bucket_combo = QComboBox(frow)
        for label, data in _BUCKETS:
            self._bucket_combo.addItem(label, data)
        self._bucket_combo.setToolTip("只看某一类问题行；筛选只隐藏行")
        self._bucket_combo.currentIndexChanged.connect(self._apply_row_filter)
        fh.addWidget(self._bucket_combo)

        self._kw_edit = QLineEdit(frow)
        self._kw_edit.setClearButtonEnabled(True)
        self._kw_edit.setPlaceholderText(
            "按编号 / 标题 / 建议过滤…（即时生效，只隐藏行）")
        self._kw_edit.textChanged.connect(self._apply_row_filter)
        fh.addWidget(self._kw_edit, 1)

        self._sort_combo = QComboBox(frow)
        for label, data in (("按桶分组", "bucket"),
                            ("编号 从小到大", "id_asc"),
                            ("编号 从大到小", "id_desc")):
            self._sort_combo.addItem(label, data)
        self._sort_combo.setToolTip("重灌式排序：数据按编号重排，内容不变")
        self._sort_combo.currentIndexChanged.connect(
            lambda _i: self._rebuild_table())
        fh.addWidget(self._sort_combo)
        root.addWidget(frow)

        # ---- 按钮行：只剩两颗（勾选批量按钮已随收录/确认退役）----
        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._jcfg_btn = QPushButton("设置游戏侧目录…", btn_row)
        self._jcfg_btn.clicked.connect(self._set_game_mod_dir)
        h.addWidget(self._jcfg_btn)
        self._verify_btn = QPushButton("开始核验", btn_row)
        self._verify_btn.clicked.connect(self._start_verify)
        h.addWidget(self._verify_btn)
        h.addStretch(1)
        root.addWidget(btn_row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)  # 建议列
        for col, width in ((0, 110), (1, 170), (2, 90), (3, 150), (5, 120)):
            self._table.setColumnWidth(col, width)

        # 两个信号各管各的：双击跳命令页；右键菜单。
        # connect 行是高危区——各自独立一行，互不覆盖
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_menu)

        self._sec_table = _Section("问题明细", self)
        self._sec_table.set_content(self._table, 500)
        root.addWidget(self._sec_table)

        self._nn_label = QLabel("", self)
        root.addWidget(self._nn_label)
        self._nn_list = QListWidget(self)
        self._sec_nn = _Section("非数字明细", self)
        self._sec_nn.set_content(self._nn_list, 300)
        root.addWidget(self._sec_nn)

        self._junc_label = QLabel("", self)
        self._junc_label.setWordWrap(True)
        root.addWidget(self._junc_label)
        self._junc_list = QListWidget(self)
        self._sec_junc = _Section("链接巡检明细", self)
        self._sec_junc.set_content(self._junc_list, 300)
        root.addWidget(self._sec_junc)

        # 折叠记忆：三节收展一变即落盘（含程序性收展）
        self._sec_table.expand_changed.connect(
            lambda ex: self._save_fold(_SES_SEC_TABLE, ex))
        self._sec_nn.expand_changed.connect(
            lambda ex: self._save_fold(_SES_SEC_NN, ex))
        self._sec_junc.expand_changed.connect(
            lambda ex: self._save_fold(_SES_SEC_JUNC, ex))

        self._verify_btn.setToolTip(
            "只读对账：账本↔磁盘实况 + 游戏侧链接；除你主动选择的动作"
            "（修复三选）外，不写库、不删任何文件")
        self._jcfg_btn.setToolTip(
            "选择游戏读取 mod 的目录，启用链接布局巡检；单目录布局无需配置")

        root.addStretch(1)   # 剩余空间留页尾：分区保持自然高度

    def _make_guide(self) -> _Section:
        """先读我折叠卡（内容包滚动区卡内滚动，默认收起）。"""
        body = QWidget(self)
        bv = QVBoxLayout(body)
        bv.setContentsMargins(4, 4, 4, 4)
        bv.setSpacing(6)
        for text in _GUIDE_PARAS:
            lbl = QLabel(text, body)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: #8a8a8f; font-size: {font_px(12)}px;")
            bv.addWidget(lbl)
        bv.addStretch(1)
        wrap = QScrollArea(self)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        wrap.setWidget(body)
        sec = _Section("先读我：两把尺子 · 桶含义 · 修复三选 · 只读边界", self)
        sec.set_content(wrap, 250)
        sec.set_expanded(False)
        return sec

    # ---------- 折叠记忆 ----------
    def _save_fold(self, key: str, expanded: bool) -> None:
        """一节收展一变即落盘（用户点击与程序性收展都走这）。"""
        QSettings().setValue(key, "1" if expanded else "0")

    def _restore_fold_memory(self) -> None:
        """启动时恢复三节上次的收展；无记录 = 维持默认展开。"""
        q = QSettings()
        for key, sec in ((_SES_SEC_TABLE, self._sec_table),
                         (_SES_SEC_NN, self._sec_nn),
                         (_SES_SEC_JUNC, self._sec_junc)):
            raw = q.value(key)
            if raw is not None:
                sec.set_expanded(str(raw) != "0")

    # ---------- 对外（MainWindow 调用）----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._table.setRowCount(0)
        self._rows_spec = []
        self._titles = {}
        self._nn_list.clear()
        self._nn_label.setText("")
        self._junc_list.clear()
        self._junc_label.setText("")
        # 切档案后旧明细已清空：两节收回折叠态，不留空框
        self._sec_nn.set_expanded(False)
        self._sec_junc.set_expanded(False)
        self._sec_table.set_title("问题明细")
        self._summary.setText("")
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._jcfg_label.setText("")
            self._jcfg_btn.setEnabled(False)
            self._verify_btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._refresh_cfg_label()
            self._jcfg_btn.setEnabled(True)
            self._verify_btn.setEnabled(True)

    # ---------- 游戏侧目录配置 ----------
    def _refresh_cfg_label(self) -> None:
        assert self._game is not None
        gd = (self._game.game_mod_dir or "").strip()
        if gd:
            self._jcfg_label.setText(f"游戏侧 mods 目录：{gd}")
        else:
            self._jcfg_label.setText(
                "游戏侧 mods 目录：（未配置——不检查游戏侧链接）")

    def _set_game_mod_dir(self) -> None:
        if self._game is None:
            return
        start = self._game.game_mod_dir or ""
        path = QFileDialog.getExistingDirectory(
            self, "选择游戏侧 mods 目录（如 CK3 的 mod 文件夹）", start)
        if not path:
            return
        # update_game 约定 None=不修改；"清除"用写空串表达
        self._repo.update_game(self._game.app_id, game_mod_dir=path)
        self._game = self._repo.get_game(self._game.app_id)   # 内存同步
        self._refresh_cfg_label()
        self._log.info(f"游戏侧 mods 目录已设置：{path}")

    # ---------- 核验流程 ----------
    def _start_verify(self) -> None:
        if self._game is None:
            return
        # 第一步：下载目录死路径重推导——与入账中心扫描同款
        effective_dir, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id,
                                   download_dir=effective_dir)
            self._game = self._repo.get_game(self._game.app_id)
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective_dir}")

        rows = self._repo.list_mods(self._game.app_id)
        status_by_id = {m.mod_id: m.status for m in rows}
        self._titles = {m.mod_id: (m.title or "") for m in rows}

        # 第二步：账本 ↔ 磁盘（纯读盘对账，不动文件）。
        # 刻意不传 local_times：V2 已不带"账新盘旧"检查，传了也白传
        result = modVerifier.verify(self._game.download_dir, status_by_id)

        if result.dead_root:
            # 短路口径与 core 一致：逐条对账全是误报，不如说清原因
            self._table.setRowCount(0)
            self._rows_spec = []
            self._nn_list.clear()
            self._nn_label.setText("")
            self._junc_list.clear()
            self._junc_label.setText("")
            self._sec_table.set_expanded(False)
            self._sec_nn.set_expanded(False)
            self._sec_junc.set_expanded(False)
            self._summary.setStyleSheet("color: #e5484d;")
            self._summary.setText(
                "下载目录不存在，逐条对账没有意义（会满屏误报）。\n"
                "请检查：① 设置页的 steamcmd 程序路径是否填对；"
                "② 该游戏的 mod 是否用这台 steamcmd 下载过。")
            self._log.warn("账实核验：下载目录不存在，未做逐条对账")
            return

        self._fill_table(result)
        self._fill_non_numeric(result)
        # untracked_content 是 (编号, 状态) 列表：状态 None=账本没登记，
        # "tracked"=登记了已收录还没确认，其余=软删/失效保留文件
        n_claim = sum(1 for _m, st in result.untracked_content if st is None)
        n_confirm = sum(1 for _m, st in result.untracked_content
                        if st == "tracked")
        n_keep = len(result.untracked_content) - n_claim - n_confirm
        n_bad = len(result.missing) + len(result.empty)
        self._summary.setStyleSheet(
            "color: #46a758;" if n_bad == 0 else "color: #f76b15;")
        parts = [f"账本 ↔ 磁盘：相符 {result.healthy}",
                 f"｜盘上缺失 {len(result.missing)}",
                 f"｜空目录 {len(result.empty)}"]
        if n_claim:
            parts.append(f"｜账本外 {n_claim}")
        if n_confirm:
            parts.append(f"｜已收录待确认 {n_confirm}")
        if n_keep:
            parts.append(f"｜软删/失效保留 {n_keep}")
        parts.append(f"｜非数字内容 {len(result.non_numeric)}")
        self._summary.setText("".join(parts))
        self._log.ok(
            f"账实核验完成（{self._game.name}）：相符 {result.healthy}，"
            f"缺失 {len(result.missing)}，空目录 {len(result.empty)}，"
            f"账本外 {n_claim}，已收录待确认 {n_confirm}"
            + (f"，软删/失效保留 {n_keep}" if n_keep else "")
            + f"，非数字 {len(result.non_numeric)}")

        # 第三步：账本 ↔ 游戏侧链接（未配置则显示提示）
        self._run_junction_check(status_by_id)

    # ---------- 表格填充（行清单与重灌分离：筛选/排序不重查盘）----------
    def _fill_table(self, result: modVerifier.VerifyResult) -> None:
        """把各桶发现翻成人话行，存进 _rows_spec，再交给 _rebuild_table。
        行类别决定一切分流：修复按钮（missing/empty）、右键菜单项、
        筛选桶、双击是否可行动。"""
        specs: list[dict] = []
        for mid in result.missing:
            specs.append(dict(
                mid=mid, kind="missing", status="已下载",
                disk="目录不存在",
                advice="双击本行重下；或点【修复…】选修复方式"))
        for mid in result.empty:
            specs.append(dict(
                mid=mid, kind="empty", status="已下载",
                disk="目录存在但为空",
                advice="疑似中断残留：双击重下；或点【修复…】；"
                       "也可手动删除空目录"))
        for mid, st in result.untracked_content:
            if st is None:
                specs.append(dict(
                    mid=mid, kind="claim", status="未入账",
                    disk="盘上有目录",
                    advice="本页只看不办：到【入账中心】点【扫描游戏目录】，"
                           "盘点会把它摆成待认领，确认后入账"))
            elif st == "tracked":
                specs.append(dict(
                    mid=mid, kind="confirm", status=status_zh(st),
                    disk="盘上已有内容",
                    advice="到【入账中心】点【扫描游戏目录】：盘点同样把它"
                           "摆成待认领，确认时一并补上真实版本号"))
            else:
                # deleted / failed：软删除和失败记录本来就保留文件
                specs.append(dict(
                    mid=mid, kind="keep", status=status_zh(st),
                    disk="盘上仍有内容",
                    advice="保留文件属正常（软删除/失效记录）；"
                           "不需要可到「清理与删除」页处置"))
        self._rows_spec = specs
        # 核验"按结果重设收展"：有问题摊开、全清收起
        self._sec_table.set_expanded(bool(specs))
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        """按当前排序重灌表格（数据源 _rows_spec，不重查盘）。
        刻意不做表头点击排序：行内控件随行搬家的表用重灌式
        （清理页同一取舍）。"""
        specs = list(self._rows_spec)
        order = self._sort_combo.currentData()
        if order == "id_asc":
            specs.sort(key=lambda s: s["mid"])
        elif order == "id_desc":
            specs.sort(key=lambda s: s["mid"], reverse=True)
        # "bucket"（默认）= 构造顺序：缺失 → 空目录 → 账本外 → 待确认 → 保留
        self._table.setRowCount(len(specs))
        for r, s in enumerate(specs):
            self._fill_row(r, s)
        self._apply_row_filter()

    def _fill_row(self, r: int, s: dict) -> None:
        mid = s["mid"]
        # 编号列：UserRole=编号本体；UserRole+1=可双击重下；
        # UserRole+2=行类别（筛选/右键用）
        mid_item = QTableWidgetItem(str(mid))
        mid_item.setData(Qt.ItemDataRole.UserRole, mid)
        mid_item.setData(Qt.ItemDataRole.UserRole + 1,
                         s["kind"] in ("missing", "empty"))
        mid_item.setData(Qt.ItemDataRole.UserRole + 2, s["kind"])
        self._table.setItem(r, 0, mid_item)

        if s["kind"] == "claim":
            title = "（账本外）"
            tip = ("账本里还没有这条编号；在入账中心确认收录并跑"
                   "【更新检测】后，这里会显示标题")
        else:
            title = self._titles.get(mid) or "（无标题）"
            tip = title
        t_item = QTableWidgetItem(title)
        t_item.setToolTip(tip)
        self._table.setItem(r, 1, t_item)

        self._table.setItem(r, 2, QTableWidgetItem(s["status"]))
        self._table.setItem(r, 3, QTableWidgetItem(s["disk"]))
        self._table.setItem(r, 4, QTableWidgetItem(s["advice"]))

        if s["kind"] in ("missing", "empty"):
            btn = QPushButton("修复…", self._table)
            btn.setToolTip(
                "三种修法任选：重新下载（增量）／校验重下 validate"
                "（被改动过的文件冲回原版）／标记为已移除"
                "（账本记为已删除，不再为它生成命令）")
            btn.clicked.connect(
                lambda _=False, mid=mid: self._repair_row(mid))
            self._table.setCellWidget(r, 5, btn)
        else:
            # 排序重灌会复用行：原来带按钮的行现在可能不带——
            # 不摘除的话旧按钮会残留在新内容上
            self._table.removeCellWidget(r, 5)
            self._table.setItem(r, 5, QTableWidgetItem(""))

    # ---------- 筛选 / 排序 ----------
    def _apply_row_filter(self, *_a) -> None:
        """桶筛选 + 关键词：只隐藏行。isRowHidden 是行自身的显式标志
        ——分区收起（控件不可见）不影响它，绝不读 isVisible。"""
        bucket = self._bucket_combo.currentData()
        kw = self._kw_edit.text().strip().casefold()
        total = self._table.rowCount()
        visible = 0
        for r in range(total):
            it = self._table.item(r, 0)
            kind = it.data(Qt.ItemDataRole.UserRole + 2) if it else None
            hay = " ".join(
                (self._table.item(r, c).text()
                 if self._table.item(r, c) is not None else "")
                for c in (0, 1, 2, 3, 4)).casefold()
            ok = (bucket is None or kind == bucket) and (not kw or kw in hay)
            self._table.setRowHidden(r, not ok)
            if ok:
                visible += 1
        if total:
            suffix = (f"（显示 {visible} / 共 {total}）"
                      if (bucket is not None or kw) else f"（共 {total}）")
            self._sec_table.set_title(f"问题明细{suffix}")

    # ---------- 右键菜单（与行内按钮同一组口径）----------
    def _on_row_menu(self, pos) -> None:
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        it = self._table.item(row, 0)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        kind = it.data(Qt.ItemDataRole.UserRole + 2)

        menu = QMenu(self)
        menu.setMinimumWidth(220)          # 中文菜单宽度兜底
        menu.setToolTipsVisible(True)      # 菜单 tooltip 默认不显示，显式开

        act_copy = QAction("复制编号", menu)
        act_copy.setToolTip(f"复制编号 {mid} 进剪贴板")
        act_url = QAction("打开工坊页面", menu)
        act_url.setToolTip(
            "在浏览器打开该编号的创意工坊页面（链接按模板现拼，"
            "未收录也能开）")
        menu.addAction(act_copy)
        menu.addAction(act_url)

        act_dir = None
        if kind != "missing":
            # missing 行目录本就不在；其余几类盘上都有内容
            act_dir = QAction("打开 mod 文件夹", menu)
            act_dir.setToolTip(
                "在文件管理器打开该编号的下载内容文件夹"
                "（空目录可借此查看后手动清理）")
            menu.addAction(act_dir)

        act_fix = None
        if kind in ("missing", "empty"):
            act_fix = QAction("修复…", menu)
            act_fix.setToolTip("重新下载（增量）／校验重下 validate／标记为已移除")
            menu.addAction(act_fix)

        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            QApplication.clipboard().setText(str(mid))
            self._log.ok(f"已复制编号 {mid}")
        elif chosen is act_url:
            QDesktopServices.openUrl(QUrl(WORKSHOP_URL_TEMPLATE.format(mid)))
        elif act_dir is not None and chosen is act_dir:
            # 账本外行没有 Mod 对象：打开器只摸 mod_id/local_path 两字段，
            # SimpleNamespace 喂最小事实（local_path=None → 自动走
            # download_dir/编号 候选）——不赌 models.Mod 构造默认值
            open_mod_folder(self, self._game,
                            SimpleNamespace(mod_id=mid, local_path=None),
                            log=self._log)
        elif act_fix is not None and chosen is act_fix:
            self._repair_row(mid)

    # ---------- 修复三选（重下 / 校验重下 / 标记为已移除）----------
    def _repair_row(self, mod_id: int) -> None:
        """缺失/空目录行的修复入口：三个修法摆在一起，各写清后果。
        双击行 = 直接走选项①（普通重下），是这里的快捷方式。"""
        box = QMessageBox(self)
        box.setWindowTitle(f"修复 mod {mod_id}")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"账本记为「已下载」，但磁盘上对不上。三种修法：\n\n"
            "① 重新下载（增量）——生成普通下载命令，manifest 保证只补"
            "差异（日常首选）\n\n"
            "② 校验重下（validate）——命令加 validate 参数，逐文件核对 "
            "manifest，被改动过的文件会冲回原版；文件夹缺失或为空时"
            "等同完整重下（怀疑文件损坏时用）\n\n"
            "③ 标记为已移除——账本记为「已删除」，本工具不再为它生成"
            "下载命令；若其他工具（如 RimSort）的清单里还有它，"
            "仍可能被下回来，需在那些工具里一并移除")
        b_redownload = box.addButton(
            "重新下载（增量）", QMessageBox.ButtonRole.ActionRole)
        b_validate = box.addButton(
            "校验重下（validate）", QMessageBox.ButtonRole.ActionRole)
        b_remove = box.addButton(
            "标记为已移除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_redownload:
            self._log.info(f"跳转命令生成页并勾选 {mod_id}（普通重下修复）")
            self.command_gen_requested.emit([mod_id])
        elif clicked is b_validate:
            self._show_validate_dialog(mod_id)
        elif clicked is b_remove:
            self._mark_removed(mod_id)

    def _show_validate_dialog(self, mod_id: int) -> None:
        """校验重下：页内出命令文本 + 复制按钮。命令格式的单源在
        core/commandBuilder，这里只负责展示。"""
        if self._game is None:
            return
        text = build_validate_copy_text(self._game.app_id, [mod_id])
        box = QMessageBox(self)
        box.setWindowTitle(f"校验重下 mod {mod_id}")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "复制下面的命令，粘贴到 steamcmd 终端执行"
            "（终端面板可从控制台菜单打开）：\n\n"
            f"{text.strip()}\n\n"
            "注意：validate 会把被改动过的文件冲回原版；"
            "文件夹缺失或为空时等同完整重下。"
            "执行完回到本页点【开始核验】即可看到结果。")
        b_copy = box.addButton("复制命令", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is b_copy:
            QApplication.clipboard().setText(text)
            self._log.info(
                f"已复制 mod {mod_id} 的校验重下命令，请粘贴到 steamcmd 执行")

    def _mark_removed(self, mod_id: int) -> None:
        """标记为已移除（修复三选选项③）：软删除——走 repo.mark_deleted
        正门（同时写删除时间与删除前快照），不是裸改状态。
        本动作不动磁盘文件（能走到这里的行本来就盘上没有内容）。"""
        mod = self._repo.get_mod(mod_id)
        if mod is None:
            self._log.error(
                f"标记为已移除失败（mod {mod_id}）：账本中已找不到该条目")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mod_id} 不在账本中，无法标记为已移除。\n"
                "请重新点【开始核验】后再试。")
            return
        ret = QMessageBox.question(
            self, "标记为已移除",
            f"确定将「{mod.title or mod.mod_id}」标记为已删除？\n"
            "本工具不再为它生成下载命令；记录保留（含删除前快照）。\n"
            "若 RimSort 等其他前端清单里还有它，仍可能被下回来，"
            "需在那些工具里一并移除。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # ★ 待核点：快照字段清单请对 models.Mod 现稿核对。
        # V2 口径：版本锚是 confirmed_version；V1 的 local_timeupdated /
        # manifest 两个字段已按 V2 决策剔除，不再入快照
        last_state = {"title": mod.title, "url": mod.url,
                      "time_updated": mod.time_updated,
                      "confirmed_version": mod.confirmed_version,
                      "local_size": mod.local_size, "note": mod.note,
                      "color_tag": mod.color_tag,
                      "is_special": mod.is_special,
                      "local_path": mod.local_path}
        try:
            self._repo.mark_deleted(mod_id, last_state)
        except ValueError as exc:
            self._log.error(f"标记为已移除失败（mod {mod_id}）：{exc}")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mod_id} 标记为已移除失败：{exc}\n"
                "请重新点【开始核验】后再试。")
            return
        self._log.warn(
            f"mod {mod_id} 已标记为已移除（账本记为已删除）："
            "本工具不再为它生成下载命令；"
            "其他前端清单里还有它的话记得一并移除")
        self._start_verify()   # 刷新：该行应从问题清单里消失

    # ---------- 非数字内容桶 ----------
    def _fill_non_numeric(self, result: modVerifier.VerifyResult) -> None:
        self._nn_list.clear()
        n = len(result.non_numeric)
        self._nn_label.setText(
            f"非数字内容桶（{n} 项）——来历不明的目录/文件，"
            "本工具不代删，请人工确认后自行处理：")
        if result.non_numeric:
            for name in result.non_numeric:
                self._nn_list.addItem(name)
        else:
            self._nn_list.addItem("（无——正常）")
        self._sec_nn.set_expanded(n > 0)   # 空桶自动收起

    # ---------- junction 巡检 ----------
    def _run_junction_check(self, status_by_id: dict[int, str]) -> None:
        """账本 ↔ 游戏侧链接巡检。动手逐条查之前，先用判定单源
        （core/steamPaths.junction_state）认一次拓扑：
        - 反向拓扑健康 → 一行绿字收工（逐条查会把每个已下载 mod 都
          报成"实为目录"，满屏同一句误报；各 mod 在不在盘上，上方
          账实对账透过联接已经查过）；
        - 反向联接指错目标 / 悬空 → 一行红字 + 指路联接检测页
          （联接一坏，steamcmd 写入就静默落空）；
        - 其余情况维持既有逐条巡检。"""
        assert self._game is not None
        self._junc_list.clear()
        self._junc_label.setText("")
        game_dir = str(self._game.game_mod_dir or "").strip().strip('"').strip()
        if game_dir:
            reverse = steamPaths.junction_state(self._game.download_dir,
                                                game_dir)
            if reverse.state == "linked":
                if os.path.isdir(game_dir):
                    self._junc_label.setText(
                        "junction 巡检：✓ 反向拓扑正常——下载目录是联接，"
                        f"指向游戏侧目录：{game_dir}\n"
                        "（反向布局下游戏直接读真实目录，无需逐条链接；"
                        "各 mod 是否在盘上，已由上方账实对账覆盖）")
                    self._junc_label.setStyleSheet("color: #46a758;")
                    self._log.ok(f"junction 巡检：反向拓扑正常（{game_dir}）")
                    # 反向健康 = 一行绿字收工，必须 return：
                    # 不 return 会落进逐条巡检，modVerifier 对反向布局
                    # 返回 None，绿字会被"未启用"覆盖
                    self._sec_junc.set_expanded(False)
                    return
                # 悬空：不赌状态名，用存在性兜底——绝不能当"正常"放过去：
                # steamcmd 下次下载会写进一个不存在的位置，静默失败
                self._junc_label.setText(
                    "junction 巡检：✗ 反向联接指对了位置，"
                    f"但游戏侧目录当前不存在：{game_dir}\n"
                    "steamcmd 的下载此刻写不进去。"
                    "到「游戏 → 连接指引」重新检测，按步骤修复"
                    "（通常把该目录建回来即可接上）。")
                self._junc_label.setStyleSheet("color: #e5484d;")
                self._log.warn(
                    "junction 巡检：反向联接悬空，"
                    f"游戏目录不存在：{game_dir}")
                self._sec_junc.set_expanded(False)
                return
            if reverse.state == "wrong_target":
                self._junc_label.setText(
                    "junction 巡检：✗ 反向联接指错了目标——下载目录是联接，"
                    f"但没有指向游戏侧目录（解析目标：{reverse.detail}）。\n"
                    "steamcmd 的内容正写进别处，游戏侧收不到。"
                    "到「游戏 → 连接指引」重新检测，按步骤原地重建。")
                self._junc_label.setStyleSheet("color: #e5484d;")
                self._log.warn(
                    "junction 巡检：反向联接指错目标，"
                    f"解析目标：{reverse.detail}")
                self._sec_junc.set_expanded(False)
                return
            # 其余状态（下载目录是真实目录等）→ 落到逐条巡检

        result = modVerifier.verify_junctions(
            self._game.download_dir, self._game.game_mod_dir, status_by_id)
        if result is None:
            self._junc_label.setText(
                "junction 巡检：未启用——未配置游戏侧 mods 目录，"
                "或游戏侧目录与下载目录相同（单目录布局，没有链接可查）。"
                "需要检查链接布局时点上方【设置游戏侧目录…】。")
            self._junc_label.setStyleSheet("color: gray;")
            self._sec_junc.set_expanded(False)
            return
        if result.dead_root:
            self._junc_label.setText(
                f"junction 巡检：目录不存在（{result.game_mod_dir}），"
                "请检查配置。")
            self._junc_label.setStyleSheet("color: #e5484d;")
            self._log.warn("junction 巡检：游戏侧目录不存在")
            self._sec_junc.set_expanded(False)
            return

        # 修法差异：缺链接 → 重建链接（重下无效！重下只恢复 content 侧）；
        # 指错/真实目录/多余 → 只报不动，人工确认
        lines: list[str] = []
        for mid in result.link_missing:
            lines.append(f"{mid}：游戏侧缺少链接（游戏里看不到此 mod）"
                         "——重建链接即可，无需重新下载")
        for mid, target in result.link_wrong_target:
            lines.append(f"{mid}：链接指向 {target}，不是工坊内容目录"
                         "——确认无用后删除重建")
        for mid in result.link_real_dir:
            lines.append(f"{mid}：游戏侧是真实目录不是链接"
                         "——本工具不代删，请人工确认")
        for name in result.extra:
            lines.append(f"{name}：游戏侧多余条目——人工确认后自行处理")
        for line in lines:
            self._junc_list.addItem(line)

        n_bad = len(lines)
        self._junc_label.setText(
            f"junction 巡检：正常 {result.ok}"
            f"｜缺链接 {len(result.link_missing)}"
            f"｜指错目标 {len(result.link_wrong_target)}"
            f"｜实为目录 {len(result.link_real_dir)}"
            f"｜多余条目 {len(result.extra)}")
        self._junc_label.setStyleSheet(
            "color: #46a758;" if n_bad == 0 else "color: #f76b15;")
        self._log.ok(
            f"junction 巡检完成：正常 {result.ok}，缺链接 "
            f"{len(result.link_missing)}，指错 "
            f"{len(result.link_wrong_target)}，实为目录 "
            f"{len(result.link_real_dir)}，多余 {len(result.extra)}")
        self._sec_junc.set_expanded(bool(lines))

    # ---------- 跳转命令生成页（双击行的快捷重下）----------
    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)   # 编号列
        if item is None:
            return
        if not item.data(Qt.ItemDataRole.UserRole + 1):
            return   # 不可行动的行：双击没反应
        mid = item.data(Qt.ItemDataRole.UserRole)
        self._log.info(f"跳转命令生成页并勾选 {mid}（重下修复）")
        self.command_gen_requested.emit([mid])
