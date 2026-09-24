"""账实核验页
"""
"""
对当前档案做两份只读对账（同步执行：只做目录列表、存在性检查和 readlink，
毫秒级，不值得开线程——与"扫描本地"同理）：

1. 账本 ↔ 磁盘（core/modVerifier.verify）：库说已下载但盘上没有、
   盘上有目录但账本没记、来历不明的残留文件
2. 账本 ↔ 游戏侧链接（core/modVerifier.verify_junctions）：已下载的 mod
   在游戏侧 mods 目录里是否正确建了 junction——链接缺失/指错时
   游戏里根本看不到这个 mod，其他对账都测不出来

流程：先按 steamcmd 现位置核对下载目录（死路径重推导并写回，与扫描
本地复用同一套路），再依次跑两份对账。本页绝不删任何文件；写库只限
三处：死路径重推导写回、设置游戏侧目录、用户在"修复三选"里主动
选择的动作（确认入账 / 标记为已移除）。

修法提示口径（与 core 的分桶一一对应）：
- 盘上缺失/空目录 → 点【修复…】三选（双击行 = 快捷的普通重下）：
    ① 重新下载（增量）：生成普通下载命令——manifest 保证只补差异
    ② 校验重下（validate）：命令末尾加 validate，逐文件核对 manifest，
       被改动过的文件冲回原版；文件夹缺失/为空时等同完整重下
       （怀疑文件损坏时用它；日常更新命令【不加】validate）
    ③ 标记为已移除：账本记为「已删除」，本工具不再为它生成下载命令——
       破"已删 mod 复活"循环的本工具侧动作；其他前端（如 RimSort）
       清单里还有它的话仍可能被下回来，需在那些工具里一并移除
- 游戏侧缺链接 → 重建链接（重下无效！重下只恢复 content 侧）
- 指错目标/真实目录/多余条目 → 只报不动，人工确认（R4 精神）

勾选确认（"账未记"桶）：早期手动下载的 mod（库记「已收录」、盘上有
内容、acf 永无记录）在第一列勾选，点【确认勾选的已下载】批量入账——
勾哪几行确认哪几行；行数多时用旁边的【全选】。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core import modVerifier, steamPaths
from core.appSettings import AppSettings
from core.commandBuilder import build_validate_copy_text
from core.models import Game
from gui.consolePanel import LogBus
from gui.manualConfirm import confirm_batch

_COLUMNS = ["选", "编号", "账本状态", "盘上情况", "建议", "操作"]

# 账本状态 → 界面中文（None = 盘上有、库里没有的编号）
_STATUS_LABELS = {
    "tracked": "已收录",
    "downloaded": "已下载",
    "deleted": "已删除",
    "failed": "已失败",
}

class _Section(QWidget):
    """可折叠分区（T19㉑）：一行"箭头＋标题"的开关，下面挂明细区。
    摘要/说明不进分区（常驻可见），收起后页面只剩摘要数字行；
    默认展开，与旧版行为一致。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        self._toggle = QToolButton(self)
        self._toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)  # 默认展开
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setText(title)
        self._toggle.setFixedHeight(22)
        v.addWidget(self._toggle)

        self._body = QWidget(self)
        self._body_v = QVBoxLayout(self._body)
        self._body_v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._body)

        # clicked(bool) 对 checkable 按钮传的就是新状态，直接用
        self._toggle.clicked.connect(self._on_toggle)

    def _on_toggle(self, expanded: bool) -> None:
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)

    def set_content(self, widget: QWidget) -> None:
        """挂明细区（每分区只调一次）。"""
        self._body_v.addWidget(widget)


class VerifyPage(QWidget):
    # 双击"缺失/空目录"行（或修复三选里选"重新下载"）时发出，
    # 参数 = mod id 列表；MainWindow 负责跳命令生成页并聚焦（既有契约不变）
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._pending_confirm_ids: list[int] = []  # 本轮核验里"可勾选确认"的编号
        # 勾选列的"程序正在灌表"开关：灌表时 setItem/setCheckState 会触发
        # itemChanged，不挡住的话计数函数会被空跑几十次
        self._filling: bool = False
        self._build_ui()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("账实核验", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        # 游戏侧 mods 目录配置行：junction 巡检的前提。
        # 按钮在"开始核验"同排（用户反馈：操作入口不该分居两处）
        self._jcfg_label = QLabel("", self)
        self._jcfg_label.setWordWrap(False)  # 踩坑 ④
        root.addWidget(self._jcfg_label)

        tip = QLabel(
            "检查数据库账本和磁盘实况是否对得上：库说已下载但盘上没有、"
            "盘上有目录但账本没记、来历不明的残留文件，都汇总在这里；"
            "配置游戏侧 mods 目录后，还会顺带检查每个已下载 mod 的"
            "游戏侧链接是否正确。\n"
            "核验只读不写——不会自动改状态、不会删任何文件，"
            "修不修、怎么修由你决定。缺失/空目录的行点【修复…】"
            "可选三种修法（含校验重下、标记为已移除）。")
        tip.setWordWrap(True)  # 踩坑 ⑨：可能变长的标签一律开换行
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)

        self._jcfg_btn = QPushButton("设置游戏侧目录…", btn_row)
        self._jcfg_btn.clicked.connect(self._set_game_mod_dir)
        h.addWidget(self._jcfg_btn)

        self._verify_btn = QPushButton("开始核验", btn_row)
        self._verify_btn.clicked.connect(self._start_verify)
        h.addWidget(self._verify_btn)

        # 全选开关：只影响"有勾选框"的行（已收录 + 盘上有内容）。
        # 初次迁移几十上百行时不用一个个点
        self._check_all_box = QCheckBox("全选", btn_row)
        self._check_all_box.setVisible(False)
        self._check_all_box.setToolTip(
            "勾上 = 选中本桶全部可确认的行；再点一下 = 全部取消。"
            "只对第一列有勾选框的行生效")
        self._check_all_box.toggled.connect(self._on_check_all_toggled)
        h.addWidget(self._check_all_box)

        # 桶级确认（勾选粒度版）：勾哪几行确认哪几行，数字实时跟随勾选
        self._confirm_all_btn = QPushButton("确认勾选的已下载…", btn_row)
        self._confirm_all_btn.setVisible(False)
        self._confirm_all_btn.setEnabled(False)
        self._confirm_all_btn.clicked.connect(self._confirm_checked)
        h.addWidget(self._confirm_all_btn)

        h.addStretch(1)
        root.addWidget(btn_row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)  # 踩坑 ④
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)  # 建议列拉伸
        for col, width in ((0, 36), (1, 110), (2, 90), (3, 150), (5, 120)):
            self._table.setColumnWidth(col, width)
        # 两个信号各管各的：双击跳命令页；勾选变化刷新按钮数字。
        # connect 行是高危区（踩坑 ㉒）——本次新增的 itemChanged 是独立一行，
        # 没有覆盖任何旧连接
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self._table.itemChanged.connect(self._on_item_changed)
        # 问题明细改可折叠（T19㉑）：表格与全部配置/信号不动，
        # 只是挂进分区；上方摘要行常驻
        self._sec_table = _Section("问题明细", self)
        self._sec_table.set_content(self._table)
        root.addWidget(self._sec_table, 1)

        self._nn_label = QLabel("", self)
        root.addWidget(self._nn_label)
        self._nn_list = QListWidget(self)
        self._sec_nn = _Section("非数字明细", self)
        self._sec_nn.set_content(self._nn_list)
        root.addWidget(self._sec_nn, 1)

        # junction 巡检结果区（配置了游戏侧目录才有内容）
        self._junc_label = QLabel("", self)
        self._junc_label.setWordWrap(True)
        root.addWidget(self._junc_label)
        self._junc_list = QListWidget(self)
        self._sec_junc = _Section("链接巡检明细", self)
        self._sec_junc.set_content(self._junc_list)
        root.addWidget(self._sec_junc, 1)

        self._verify_btn.setToolTip(
            "只读对账：账本↔磁盘实况 + 游戏侧链接；除\"修复三选\"里"
                                                            "用户主动选择的动作外，不写库、不删任何文件")
        self._jcfg_btn.setToolTip(
            "选择游戏读取 mod 的目录，启用链接布局巡检；单目录布局无需配置")
        self._confirm_all_btn.setToolTip(
            "把第一列【勾选的】「已收录」条目批量手动确认入账"
            "（早期手动下载，acf 永无记录，扫描无法确认）；"
            "确认后版本留空——备份将拒、更新检测列「版本未知」；"
            "重下并扫描可恢复")

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._table.setRowCount(0)
        self._nn_list.clear()
        self._nn_label.setText("")
        self._junc_list.clear()
        self._junc_label.setText("")
        self._summary.setText("")
        self._pending_confirm_ids = []
        self._refresh_confirm_ui()  # 连带收起勾选按钮/全选框
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
        # update_game 的约定是 None = 不修改（全项目口径），所以
        # "清除"用写空串表达——巡检函数对空串按未配置处理，与 None 同义
        self._repo.update_game(self._game.app_id, game_mod_dir=path)
        # 内存同步：与扫描本地死路径重推导同一套路
        self._game = self._repo.get_game(self._game.app_id)
        self._refresh_cfg_label()
        self._log.info(f"游戏侧 mods 目录已设置：{path}")

    # ---------- 核验流程 ----------
    def _start_verify(self) -> None:
        if self._game is None:
            return

        # 第一步：下载目录死路径重推导——与扫描本地完全同款的核对
        effective_dir, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id, download_dir=effective_dir)
            self._game = self._repo.get_game(self._game.app_id)  # 内存同步
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective_dir}")

        status_by_id = {m.mod_id: m.status
                        for m in self._repo.list_mods(self._game.app_id)}

        # 第二步：账本 ↔ 磁盘（纯读盘对账，不动文件）
        result = modVerifier.verify(self._game.download_dir, status_by_id)

        if result.dead_root:
            # 短路口径与 core 一致：逐条对账全是误报，不如说清原因
            self._table.setRowCount(0)
            self._nn_list.clear()
            self._nn_label.setText("")
            self._junc_list.clear()
            self._junc_label.setText("")
            # 上一轮可能把勾选按钮/全选点亮了：收起来 + 清空待确认清单
            # （防止按钮还在但旧数据已失效）
            self._pending_confirm_ids = []
            self._refresh_confirm_ui()
            self._summary.setStyleSheet("color: #e5484d;")
            self._summary.setText(
                "下载目录不存在，逐条对账没有意义（会满屏误报）。\n"
                "请检查：① 设置页的 steamcmd 程序路径是否填对；"
                "② 该游戏的 mod 是否用这台 steamcmd 下载过。")
            self._log.warn("账实核验：下载目录不存在，未做逐条对账")
            return

        # "已收录 + 盘上有内容"的行可勾选确认——先收齐编号给按钮计数用
        self._pending_confirm_ids = [
            mid for mid, st in result.untracked_content if st == "tracked"]

        self._fill_table(result)
        self._fill_non_numeric(result)

        n_bad = len(result.missing) + len(result.empty)
        self._summary.setStyleSheet(
            "color: #46a758;" if n_bad == 0 else "color: #f76b15;")
        self._summary.setText(
            f"账本 ↔ 磁盘：相符 {result.healthy}"
            f"｜盘上缺失 {len(result.missing)}｜空目录 {len(result.empty)}"
            f"｜盘上有而账未记 {len(result.untracked_content)}"
            f"｜非数字内容 {len(result.non_numeric)}")

        # 勾选按钮/全选框的显示与计数（内部读当前表格的勾选状态）
        self._refresh_confirm_ui()

        self._log.ok(
            f"账实核验完成（{self._game.name}）：相符 {result.healthy}，"
            f"缺失 {len(result.missing)}，空目录 {len(result.empty)}，"
            f"账未记 {len(result.untracked_content)}，"
            f"非数字 {len(result.non_numeric)}")

        # 第三步：账本 ↔ 游戏侧链接（未配置则显示提示）
        self._run_junction_check(status_by_id)

    # ---------- 表格填充 ----------
    def _fill_table(self, result: modVerifier.VerifyResult) -> None:
        """把三桶发现翻成人话行。

        每行六个位置：勾选框 / 编号 / 账本状态 / 盘上情况 / 建议 / 操作。
        - 第一列：只有"已收录 + 盘上有内容"的行放勾选框（勾选批量确认）
        - 操作列：缺失/空目录行放【修复…】按钮（三选修复）；
          双击 = 普通重下的快捷方式（与修复三选里的选项①同路）
        """
        # (mid, 状态文案, 盘上文案, 建议文案, 可双击重下, 可勾选确认)
        rows: list[tuple[int, str, str, str, bool, bool]] = []

        for mid in result.missing:
            rows.append((mid, "已下载", "目录不存在",
                         "双击本行重下；或点【修复…】选修复方式", True, False))
        for mid in result.empty:
            rows.append((mid, "已下载", "目录存在但为空",
                         "疑似中断残留：双击重下；或点【修复…】；"
                         "也可手动删除空目录", True, False))
        for mid, st in result.untracked_content:
            label = _STATUS_LABELS.get(st, "未入账")
            if st is None:
                rows.append((mid, label, "盘上有目录",
                             "本工具之外的内容：点【扫描本地】尝试入账",
                             False, False))
            elif st == "tracked":
                # 库里 tracked、盘上有内容，两种来路：
                # a) 刚用 steamcmd 下过还没扫描 → 扫描能自动确认（更好，
                #    版本三件套齐全）；b) 早期手动下载，acf 永远不会有
                #    记录 → 扫描永远无效，只能手动确认。
                # 文案两条路都摆出来，先推荐代价小的那条
                rows.append((mid, label, "盘上已有内容",
                             "先试【扫描本地】自动确认；不行（acf 无记录）"
                             "就在第一列勾选，点上方按钮批量确认",
                             False, True))
            else:
                # deleted / failed：软删除和失败记录本来就保留文件，正常
                rows.append((mid, label, "盘上仍有内容",
                             "保留文件属正常（软删除/失败记录）；"
                             "不需要可手动清理", False, False))

        self._filling = True  # 灌表期间屏蔽 itemChanged（防计数空跑）
        try:
            self._table.setRowCount(len(rows))
            for r, (mid, status, disk, advice, actionable, checkable) \
                    in enumerate(rows):
                # 第一列：勾选框（只有可确认的行）
                if checkable:
                    chk = QTableWidgetItem()
                    chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                                 | Qt.ItemFlag.ItemIsEnabled
                                 | Qt.ItemFlag.ItemIsSelectable)
                    chk.setCheckState(Qt.CheckState.Unchecked)
                    chk.setToolTip("勾选后点上方【确认勾选的已下载】批量入账")
                    self._table.setItem(r, 0, chk)
                else:
                    self._table.setItem(r, 0, QTableWidgetItem(""))

                # 第二列：编号。塞进 UserRole 供双击取用；
                # 可行动标记放 UserRole+1（供双击判断是否响应）
                mid_item = QTableWidgetItem(str(mid))
                mid_item.setData(Qt.ItemDataRole.UserRole, mid)
                mid_item.setData(Qt.ItemDataRole.UserRole + 1, actionable)
                self._table.setItem(r, 1, mid_item)

                self._table.setItem(r, 2, QTableWidgetItem(status))
                self._table.setItem(r, 3, QTableWidgetItem(disk))
                self._table.setItem(r, 4, QTableWidgetItem(advice))

                # 操作列：缺失/空目录给【修复…】三选按钮
                if actionable:
                    btn = QPushButton("修复…", self._table)
                    btn.setToolTip(
                        "三种修法任选：重新下载（增量）／校验重下 validate"
                        "（被改动过的文件冲回原版）／标记为已移除"
                        "（账本记为已删除，不再为它生成命令）")
                    btn.clicked.connect(
                        lambda _=False, mid=mid: self._repair_row(mid))
                    self._table.setCellWidget(r, 5, btn)
                else:
                    self._table.setItem(r, 5, QTableWidgetItem(""))
        finally:
            self._filling = False

    # ---------- 勾选确认（T20a：勾哪几行确认哪几行） ----------
    def _checked_ids(self) -> list[int]:
        """从表格第一列收出当前勾选的 mod id（按界面顺序）。"""
        ids: list[int] = []
        for r in range(self._table.rowCount()):
            chk = self._table.item(r, 0)
            if chk is None or not (chk.flags()
                                   & Qt.ItemFlag.ItemIsUserCheckable):
                continue  # 本行没有勾选框（不可确认的行）
            if chk.checkState() != Qt.CheckState.Checked:
                continue
            mid_item = self._table.item(r, 1)
            if mid_item is not None:
                ids.append(mid_item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _refresh_confirm_ui(self) -> None:
        """按钮/全选框的显隐与计数。数据源 = _pending_confirm_ids（有没有
        可确认的行）+ 当前表格勾选状态（勾了几个）。"""
        has_rows = bool(self._pending_confirm_ids)
        self._check_all_box.setVisible(has_rows)
        self._confirm_all_btn.setVisible(has_rows)
        if not has_rows:
            # 程序性改状态：挡住 toggled 信号，避免绕回本函数成环
            self._check_all_box.blockSignals(True)
            self._check_all_box.setChecked(False)
            self._check_all_box.blockSignals(False)
            return
        n = len(self._checked_ids())
        self._confirm_all_btn.setText(f"确认勾选的已下载（{n} 个）…")
        self._confirm_all_btn.setEnabled(n > 0)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """表格条目变化 → 只关心第一列勾选框 → 刷新按钮计数。
        灌表期间（_filling）不响应。"""
        if self._filling:
            return
        if item.column() == 0 and (item.flags()
                                   & Qt.ItemFlag.ItemIsUserCheckable):
            self._refresh_confirm_ui()

    def _on_check_all_toggled(self, checked: bool) -> None:
        """全选/全不选：只作用于有勾选框的行。"""
        self._filling = True  # 批量打勾不让 itemChanged 逐个刷计数
        try:
            for r in range(self._table.rowCount()):
                chk = self._table.item(r, 0)
                if chk is None or not (chk.flags()
                                       & Qt.ItemFlag.ItemIsUserCheckable):
                    continue
                chk.setCheckState(Qt.CheckState.Checked if checked
                                  else Qt.CheckState.Unchecked)
        finally:
            self._filling = False
        self._refresh_confirm_ui()

    def _confirm_checked(self) -> None:
        """把勾选的行批量确认入账；成功后重跑核验刷新全部数字
        （核验只读、毫秒级）。"""
        ids = self._checked_ids()
        if not ids:
            return  # 按钮无勾选时应为禁用态，这里是双保险
        if confirm_batch(self, self._repo, self._log, ids):
            self._start_verify()

    # ---------- 修复三选（T11a：重下 / 校验重下 / 标记为已移除） ----------
    def _repair_row(self, mod_id: int) -> None:
        """缺失/空目录行的修复入口：三个修法摆在一起，各写清后果。
        双击行 = 直接走选项①（普通重下），是这里的快捷方式。"""
        box = QMessageBox(self)
        box.setWindowTitle(f"修复 mod {mod_id}")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"账本记为「已下载」，但磁盘上对不上。三种修法：\n\n"
            "① 重新下载（增量）——生成普通下载命令，manifest 保证只补差异"
            "（日常首选）\n\n"
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
            "标记为已移除", QMessageBox.ButtonRole.DestructiveRole)  # 红色警示
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is b_redownload:
            self._log.info(
                f"跳转命令生成页并勾选 {mod_id}（普通重下修复）")
            self.command_gen_requested.emit([mod_id])
        elif clicked is b_validate:
            self._show_validate_dialog(mod_id)
        elif clicked is b_remove:
            self._mark_removed(mod_id)

    def _show_validate_dialog(self, mod_id: int) -> None:
        """校验重下：页内出命令文本 + 复制按钮（决策 27 同款形态——
        只出命令不代执行，粘贴回 steamcmd 由用户自己完成）。
        命令格式的单源在 core/commandBuilder，这里只负责展示。"""
        if self._game is None:  # 理论到不了：有行就有档案；防御一行
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
        b_copy = box.addButton(
            "复制命令", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is b_copy:
            QApplication.clipboard().setText(text)
            self._log.info(
                f"已复制 mod {mod_id} 的校验重下命令，请粘贴到 steamcmd 执行")
    def _mark_removed(self, mod_id: int) -> None:
        """标记为已移除（修复三选选项③）：软删除——与 mod 库页右键
        「软删除…」走同一条路（repo.mark_deleted），不是裸改状态：
        mark_deleted 会同时写 deleted_at（删除时刻）和 deleted_last_state
        （删除前元数据快照），"记录保留、可随时恢复"靠的就是这两列；
        update_status 只改 status，会把恢复凭据丢掉。
        本动作不动磁盘文件（能走到这里的行本来就盘上没有内容）。"""
        mod = self._repo.get_mod(mod_id)
        if mod is None:
            # 行是几秒前核验时读出来的，理论上不会消失；万一并发变动，
            # 按项目口径显式报告，不静默吞
            self._log.error(
                f"标记为已移除失败（mod {mod_id}）：账本中已找不到该条目")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mod_id} 不在账本中，无法标记为已移除。\n"
                "请重新点【开始核验】后再试。")
            return
        # 危险操作三重提示的第三重（决策 22⑥：删除管理须确认对话框；
        # 前两重 = 按钮 tooltip + 修复三选里的后果说明）
        ret = QMessageBox.question(
            self, "标记为已移除",
            f"确定将「{mod.title or mod.mod_id}」标记为已删除？\n"
            "本工具不再为它生成下载命令；记录保留（含删除前快照）。\n"
            "若 RimSort 等其他前端清单里还有它，仍可能被下回来，"
            "需在那些工具里一并移除。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # last_state 组装与 modListPage._soft_delete 逐字段同款
        # （repo 契约：调用方组装、repo 只管存取；将来抽 flow 层时
        # 两处一起搬走）
        last_state = {"title": mod.title, "url": mod.url,
                      "time_updated": mod.time_updated,
                      "local_timeupdated": mod.local_timeupdated,
                      "manifest": mod.manifest,
                      "local_size": mod.local_size,
                      "note": mod.note, "color_tag": mod.color_tag,
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
        self._start_verify()  # 刷新：该行应从问题清单里消失

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

    # ---------- junction 巡检 ----------
    def _run_junction_check(self, status_by_id: dict[int, str]) -> None:
        assert self._game is not None
        self._junc_list.clear()
        self._junc_label.setText("")

        result = modVerifier.verify_junctions(
            self._game.download_dir, self._game.game_mod_dir, status_by_id)
        if result is None:
            # None 有两种来路：未配置；或单目录布局（游戏侧=下载目录，
            # 本就没有链接可查）。文案把两种都说清，不吓唬人
            self._junc_label.setText(
                "junction 巡检：未启用——未配置游戏侧 mods 目录，"
                "或游戏侧目录与下载目录相同（单目录布局，没有链接可查）。"
                "需要检查链接布局时点上方【设置…】。")
            self._junc_label.setStyleSheet("color: gray;")
            return

        if result.dead_root:
            self._junc_label.setText(
                f"junction 巡检：目录不存在（{result.game_mod_dir}），"
                "请检查配置。")
            self._junc_label.setStyleSheet("color: #e5484d;")
            self._log.warn("junction 巡检：游戏侧目录不存在")
            return

        # 逐桶翻人话。注意修法差异：
        # - 缺链接 → 重建链接（重下无效！重下只恢复 content 侧，
        #   游戏侧链接不会自己长出来）
        # - 指错/真实目录/多余 → 只报不动（R4），人工确认
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

    # ---------- 跳转命令生成页（双击行的快捷重下） ----------
    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, 1)  # 编号列（第 0 列现在是勾选框）
        if item is None:
            return
        if not item.data(Qt.ItemDataRole.UserRole + 1):
            return  # 不可行动的行：双击没反应
        mid = item.data(Qt.ItemDataRole.UserRole)
        self._log.info(f"跳转命令生成页并勾选 {mid}（重下修复）")
        self.command_gen_requested.emit([mid])
