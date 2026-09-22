"""账实核验页
"""
"""
对当前档案做两份只读对账（同步执行：只做目录列表、存在性检查和
readlink，毫秒级，不值得开线程——与"扫描本地"同理）：
1. 账本 ↔ 磁盘（core/modVerifier.verify）：库说已下载但盘上没有、
   盘上有目录但账本没记、来历不明的残留文件
2. 账本 ↔ 游戏侧链接（core/modVerifier.verify_junctions）：已下载的
   mod 在游戏侧 mods 目录里是否正确建了 junction——链接缺失/指错时
   游戏里根本看不到这个 mod，其他对账都测不出来

流程：先按 steamcmd 现位置核对下载目录（死路径重推导并写回，与扫描
本地复用同一套路），再依次跑两份对账。本页绝不写库（重推导写回与
设置游戏侧目录除外）、绝不删任何文件。

修法提示口径（与 core 的分桶一一对应）：
- 盘上缺失/空目录 → 重新下载（双击行跳命令生成页）
- 游戏侧缺链接 → 重建链接（重下无效！重下只恢复 content 侧）
- 指错目标/真实目录/多余条目 → 只报不动，人工确认（R4 精神）
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import modVerifier, steamPaths
from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus
from gui.manualConfirm import confirm_batch, confirm_one

_COLUMNS = ["编号", "账本状态", "盘上情况", "建议", "操作"]


# 账本状态 → 界面中文（None = 盘上有、库里没有的编号）
_STATUS_LABELS = {
    "tracked": "已收录",
    "downloaded": "已下载",
    "deleted": "已删除",
    "failed": "已失败",
}


class VerifyPage(QWidget):
    # 双击"缺失/空目录"行时发出，参数 = mod id 列表；
    # MainWindow 负责跳命令生成页并聚焦（与 modListPage 同一份契约）
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings,
                 parent: QWidget | None = None,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._pending_confirm_ids: list[int] = []  # 本次核验发现的待确认编号

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
        # 按钮挪到"开始核验"同排（用户反馈：操作入口不该分居两处）
        self._jcfg_label = QLabel("", self)
        self._jcfg_label.setWordWrap(False)  # 踩坑 ④
        root.addWidget(self._jcfg_label)

        tip = QLabel(
            "检查数据库账本和磁盘实况是否对得上：库说已下载但盘上没有、"
            "盘上有目录但账本没记、来历不明的残留文件，都汇总在这里；"
            "配置游戏侧 mods 目录后，还会顺带检查每个已下载 mod 的"
            "游戏侧链接是否正确。\n"
            "核验只读不写——不会自动改状态、不会删任何文件，"
            "修不修、怎么修由你决定。")
        tip.setWordWrap(True)
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
        # 桶级批量确认（T18）：核验发现"tracked + 盘上有内容"的行后
        # 才出现，一次把整桶确认掉，不用一行行点
        self._confirm_all_btn = QPushButton("全部确认已下载", btn_row)
        self._confirm_all_btn.setVisible(False)
        self._confirm_all_btn.clicked.connect(self._confirm_all_tracked)
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
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 110), (1, 90), (2, 150), (4, 120)):
            self._table.setColumnWidth(col, width)
        self._table.cellDoubleClicked.connect(self._on_row_double_clicked)
        root.addWidget(self._table, 1)

        self._nn_label = QLabel("", self)
        root.addWidget(self._nn_label)
        self._nn_list = QListWidget(self)
        root.addWidget(self._nn_list, 1)

        # junction 巡检结果区（配置了游戏侧目录才有内容）
        self._junc_label = QLabel("", self)
        self._junc_label.setWordWrap(True)
        root.addWidget(self._junc_label)
        self._junc_list = QListWidget(self)
        root.addWidget(self._junc_list, 1)
        self._verify_btn.setToolTip(
            "只读对账：账本↔磁盘实况 + 游戏侧链接；不写库、不删任何文件")
        self._jcfg_btn.setToolTip(
            "选择游戏读取 mod 的目录，启用链接布局巡检；单目录布局无需配置")


    # ---------- 对外（MainWindow 调用） ----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        self._table.setRowCount(0)
        self._nn_list.clear()
        self._nn_label.setText("")
        self._junc_list.clear()
        self._junc_label.setText("")
        self._summary.setText("")
        self._confirm_all_btn.setVisible(False)
        self._pending_confirm_ids = []

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

        # 第一步：下载目录死路径重推导（决策 21）——与扫描本地完全同款
        # 的核对，modListPage 里预告的"核验页复用点"就在这里
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

        status_by_id = {m.mod_id: m.status
                        for m in self._repo.list_mods(self._game.app_id)}

        # 第二步：账本 ↔ 磁盘（纯读盘对账，不写库、不动文件）
        result = modVerifier.verify(self._game.download_dir, status_by_id)

        if result.dead_root:
            # 短路口径与 core 一致：逐条对账全是误报，不如说清原因
            self._table.setRowCount(0)
            self._nn_list.clear()
            self._nn_label.setText("")
            self._junc_list.clear()
            self._junc_label.setText("")
            # T18：上一轮核验可能把批量确认按钮点亮了，死根时收起来，
            # 待确认清单一并清空（防止按钮藏了但旧数据还挂着）
            self._confirm_all_btn.setVisible(False)
            self._pending_confirm_ids = []
            self._summary.setStyleSheet("color: #e5484d;")
            self._summary.setText(
                "下载目录不存在，逐条对账没有意义（会满屏误报）。\n"
                "请检查：① 设置页的 steamcmd 程序路径是否填对；"
                "② 该游戏的 mod 是否用这台 steamcmd 下载过。")
            self._log.warn("账实核验：下载目录不存在，未做逐条对账")
            return

        # T18：账未记桶里 tracked + 盘上有内容的行可手动确认，
        # 先收齐编号给桶级按钮用
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

        # T18：有可确认条目才点亮批量按钮（文案带数量，tooltip 讲清
        # 后果——决策 22：按钮必须自解释）；没有就收起来
        if self._pending_confirm_ids:
            self._confirm_all_btn.setText(
                f"全部确认已下载（{len(self._pending_confirm_ids)} 个）…")
            self._confirm_all_btn.setToolTip(
                "把本桶全部「已收录」条目一次性手动确认入账"
                "（早期手动下载，acf 永无记录，扫描无法确认）；"
                "确认后版本留空——备份将拒、更新检测列「版本未知」")
            self._confirm_all_btn.setVisible(True)
        else:
            self._confirm_all_btn.setVisible(False)

        self._log.ok(
            f"账实核验完成（{self._game.name}）：相符 {result.healthy}，"
            f"缺失 {len(result.missing)}，空目录 {len(result.empty)}，"
            f"账未记 {len(result.untracked_content)}，"
            f"非数字 {len(result.non_numeric)}")

        # 第三步：账本 ↔ 游戏侧链接（未配置则显示提示）
        self._run_junction_check(status_by_id)

    def _fill_table(self, result: modVerifier.VerifyResult) -> None:
        """把三桶发现翻成人话行。双击动作只给"缺失/空目录"开——
        它们的修法是重下，其他桶的修法都不在命令生成页。
        T18：tracked 且盘上有内容的行，在「操作」列给确认按钮。
        """
        rows: list[tuple[int, str, str, str, bool, bool]] = []
        for mid in result.missing:
            rows.append((mid, "已下载", "目录不存在",
                         "重新下载（双击本行生成命令）", True, False))
        for mid in result.empty:
            rows.append((mid, "已下载", "目录存在但为空",
                         "疑似中断残留：重下（双击）或手动删除空目录",
                         True, False))
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
                             "再点右侧【确认已下载】", False, True))
            else:
                # deleted / failed：软删除和失败记录本来就保留文件，正常
                rows.append((mid, label, "盘上仍有内容",
                             "保留文件属正常（软删除/失败记录）；"
                             "不需要可手动清理", False, False))
        self._table.setRowCount(len(rows))
        for r, (mid, status, disk, advice, actionable,
                need_confirm) in enumerate(rows):
            mid_item = QTableWidgetItem(str(mid))
            # 编号塞进 UserRole 供双击取用；可行动标记放 UserRole+1
            mid_item.setData(Qt.ItemDataRole.UserRole, mid)
            mid_item.setData(Qt.ItemDataRole.UserRole + 1, actionable)
            self._table.setItem(r, 0, mid_item)
            self._table.setItem(r, 1, QTableWidgetItem(status))
            self._table.setItem(r, 2, QTableWidgetItem(disk))
            self._table.setItem(r, 3, QTableWidgetItem(advice))
            if need_confirm:
                btn = QPushButton("确认已下载…", self._table)
                btn.setToolTip(
                    "手动确认入账（早期手动下载，acf 永无记录，扫描无法"
                    "确认）：确认后记为「已下载」，版本留空——备份将拒、"
                    "更新检测列「版本未知」；重下并扫描可恢复")
                btn.clicked.connect(
                    lambda _=False, mid=mid: self._confirm_row(mid))
                self._table.setCellWidget(r, 4, btn)
            else:
                self._table.setItem(r, 4, QTableWidgetItem(""))

    def _confirm_row(self, mod_id: int) -> None:
        """单行确认（T18）：弹窗与写库在共享 helper；
        确认成功后重跑一次核验刷新全部数字（核验是只读的，毫秒级）。"""
        if confirm_one(self, self._repo, self._log, mod_id):
            self._start_verify()

    def _confirm_all_tracked(self) -> None:
        """桶级批量确认（T18）：把本桶 tracked 条目一次确认完。"""
        if not self._pending_confirm_ids:
            return
        if confirm_batch(self, self._repo, self._log,
                         list(self._pending_confirm_ids)):
            self._start_verify()

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

    # ---------- 跳转命令生成页 ----------

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        if not item.data(Qt.ItemDataRole.UserRole + 1):
            return  # 不可行动的行：双击没反应
        mid = item.data(Qt.ItemDataRole.UserRole)
        self._log.info(f"跳转命令生成页并勾选 {mid}（重下修复）")
        self.command_gen_requested.emit([mid])
