"""更新确认清单（发现更新后的人工挑选关口）
"""
"""
gui/updateSelectDialog.py · 检测到新版本后弹出的工作台窗口：
勾选条目 → 底部选动作 → 点【执行选中】请求开批。

只与主窗口交接，本对话框零数据库访问、零网络请求：
- 数据（mod 整行 + 批次归属的档案编号）由主窗口组装后传入；
- 点【执行选中】只发请求（execute_requested 信号），本窗口不自行
  动勾选、不自行开批——批次是否真正受理，由主窗口回话：
  受理 → mark_executed（留痕 + 清勾选）；没受理 → notify_not_started
  （勾选原样保留、提示原因）。修好原因后直接再点一次即可。

非模态窗口（不挡别的操作）：
- 窗口开着时软件任何地方都能去——steamcmd 没启动就切到底部控制台
  启动并登录，回来再点执行，勾选不丢。模态窗口会挡住它依赖的
  窗口外之物（去启动 steamcmd），等于自己锁死自己；
- 主窗口在切换游戏档案时自动关闭本窗口（批次归属 = 打开那一刻
  的档案，档案能切但归属不能跟着变，干脆关掉重开）。

两种动作（不勾 = 不动）：
- 备份+更新：先把当前版本复制进备份区（新版不满意可到【备份与
  恢复】页恢复），再下载新版本。默认动作——更新前留一条退路；
- 仅更新：直接下载。

对"没有旧版本可备份"的条目，诚实告知两种不同下场（v2 判决制）：
- 尚未下载过（下载目录里没有这个 mod 的文件夹）：没有东西可保护，
  选「备份+更新」也会按「仅更新」执行，下载照常；
- 本地版本未确认（账本里没有确认版本）：备份引擎按守卫纪律拒绝
  备份（账本只存有版本锚的备份），这种条目会被整条剔出批次——
  请先到 mod 库页右键【设定本地版本…】再下载。

每行一个【↗】按钮直达工坊页面查详情（项目约定：凡"选 mod 做某事"
的窗口都带这个按钮）。本窗口非模态，切去别的页面或控制台都不影响
这里的状态。
"""
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from core.formatters import abs_time, fmt_size
from core.models import Mod

_COLUMNS = ["选", "", "编号", "标题", "远端版本", "本地版本", "大小", "上次执行"]


class UpdateSelectDialog(QDialog):
    """更新确认清单（非模态工作台）：执行不关窗、可分批反复操作；
    与底部控制台并用（steamcmd 没启动时切过去启动登录，回来再执行）。"""

    # 【执行选中】：参数 = (批次归属档案编号, 动作, mod_id 清单)。
    # 主窗口接——上一批还在跑则提示稍候，否则开批并把受理结果
    # 回叫给本窗口（mark_executed / notify_not_started）
    execute_requested = Signal(int, str, list)

    # 动作常量（单源住在本类；主窗口按 UpdateSelectDialog.ACT_BACKUP
    # 比对分组）。★值必须与信号里发的完全一致：下拉框 currentData
    # 取自 _ACTION_LABELS，而 _ACTION_LABELS 引用的就是这两个常量。
    # 历史教训：曾有版本把显示文案（"备份+更新"）误当常量挂在类上，
    # 与信号发的数据值永远不相等——"备份+更新"被静默降级成直接
    # 下载、不备份。值是值、文案是文案，别合并。
    ACT_BACKUP = "backup_and_update"  # 先备份当前版本，再下载新版
    ACT_UPDATE = "update_only"        # 直接下载新版

    def __init__(self, mods: list[Mod], app_id: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发现更新——选择要执行的条目")
        self.resize(880, 540)
        self._mods = list(mods)
        # 批次归属 = 检测那一刻的档案，随执行信号原样带回。非模态下
        # 档案能切——主窗口在切档案时自动关掉本窗口，本窗口存活
        # 期间归属不变
        self._app_id = app_id
        self._building = True  # 建表期间 itemChanged 是程序设值

        root = QVBoxLayout(self)
        tip = QLabel(
            f"检测到 {len(self._mods)} 个 mod 有新版本。勾选条目、"
            "在底部选动作、点【执行选中】：\n"
            "· 备份+更新 = 先把当前版本复制进备份区（可回滚），再下载"
            "新版；\n"
            "· 仅更新 = 直接下载；不勾选 = 不动。\n"
            "尚未下载过的条目没有旧版本可保护，选「备份+更新」会按"
            "「仅更新」执行；本地版本未确认的条目会被整条剔出批次"
            "（先到 mod 库页右键【设定本地版本…】）。\n"
            "本窗口不挡别的操作：steamcmd 没启动的话，切到底部控制台"
            "启动并登录，回来再点【执行选中】——勾选不会丢。\n"
            "批次真正受理后勾选才清掉、留痕；可以改勾选、换动作、继续"
            "执行下一组（上一批没跑完时会提示稍候）。点【完成】收工"
            "——已开的批次照常跑完，不受影响。",
            self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        self._table = QTableWidget(len(self._mods), len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 40), (1, 44), (2, 96), (4, 140),
                           (5, 140), (6, 90), (7, 110)):
            self._table.setColumnWidth(col, width)
        self._fill_rows()
        root.addWidget(self._table, 1)

        bottom = QHBoxLayout()
        btn_all = QPushButton("全选", self)
        btn_all.setToolTip("勾选全部条目（打开时默认已全选）")
        btn_all.clicked.connect(lambda: self._set_all_checked(True))
        btn_none = QPushButton("全不选", self)
        btn_none.setToolTip("取消全部勾选——不勾选的条目不会被处理")
        btn_none.clicked.connect(lambda: self._set_all_checked(False))
        bottom.addWidget(btn_all)
        bottom.addWidget(btn_none)

        self._combo_action = QComboBox(self)
        for value, label in _ACTION_LABELS:
            self._combo_action.addItem(label, value)
        self._combo_action.setToolTip(
            "对本次勾选的条目统一执行这个动作：\n"
            "备份+更新 = 先备份当前版本再下载；仅更新 = 直接下载")
        bottom.addWidget(self._combo_action)

        self._btn_execute = QPushButton("执行选中", self)
        self._btn_execute.setDefault(True)
        self._btn_execute.setToolTip(
            "把勾选的条目按所选动作请求开批（顺序 = 表格行序）；\n"
            "steamcmd 未启动时批次不会开始、勾选保留——切到底部控制台"
            "启动并登录后回来再点一次即可")
        self._btn_execute.clicked.connect(self._on_execute_clicked)
        bottom.addWidget(self._btn_execute)
        bottom.addStretch(1)

        self._hint = QLabel("", self)
        self._hint.setStyleSheet("color: #f5a623;")
        bottom.addWidget(self._hint)

        btn_done = QPushButton("完成", self)
        btn_done.setToolTip(
            "关闭窗口。已开出的批次照常跑完；没执行的条目之后可到"
            " mod 库页手动下载")
        btn_done.clicked.connect(self.accept)
        bottom.addWidget(btn_done)
        root.addLayout(bottom)

        # 建完表再连信号：填充期间的程序设值不当作用户点击
        self._building = False
        self._table.itemChanged.connect(lambda _it: self._update_count())
        self._update_count()

    # ---------- 内部 ----------

    def _fill_rows(self) -> None:
        """一行一个 mod。版本一律显示绝对日期——选择时人要对比
        "远端哪天更的 / 本地是哪天的"，相对时间（3 天前）不方便比。
        本地版本列显示的是确认版本（你点头入过账的），不是扫描值——
        判决制下账本里没有别的"本地版本"。"""
        for r, m in enumerate(self._mods):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            if m.status != "downloaded":
                chk.setToolTip(
                    "这个条目尚未下载过，没有旧版本可备份——"
                    "选「备份+更新」也会自动按「仅更新」执行")
            elif m.version_unknown:
                chk.setToolTip(
                    "这个条目的本地版本未确认：会被整条剔出批次"
                    "（备份引擎拒绝没有版本锚的备份）。\n"
                    "先到 mod 库页右键【设定本地版本…】再下载")
            self._table.setItem(r, 0, chk)

            # ↗ = 打开工坊页面查详情（约定：选 mod 的窗口都带）
            btn = QToolButton()
            btn.setText("↗")
            btn.setToolTip(f"打开工坊页面查看详情：\n{m.url or '（无链接）'}")
            btn.setEnabled(bool(m.url))
            btn.clicked.connect(
                lambda _=False, u=m.url: QDesktopServices.openUrl(QUrl(u)))
            self._table.setCellWidget(r, 1, btn)

            self._table.setItem(r, 2, QTableWidgetItem(str(m.mod_id)))
            title = ("★ " if m.is_special else "") + (m.title or "（无标题）")
            item_title = QTableWidgetItem(title)
            item_title.setToolTip(
                f"{m.title or '（无标题）'}\n编号 {m.mod_id}"
                + ("\n★ 特别关注：出新版本时检测会记提醒"
                   if m.is_special else ""))
            self._table.setItem(r, 3, item_title)
            self._table.setItem(r, 4,
                                QTableWidgetItem(abs_time(m.time_updated)))
            if m.status != "downloaded":
                local = "未下载"
            elif m.version_unknown:
                local = "版本未知"
            else:
                local = abs_time(m.confirmed_version)
            item_local = QTableWidgetItem(local)
            item_local.setToolTip("本地版本 = 你确认过的版本"
                                  "（确认入账/手工设定后才有值）")
            self._table.setItem(r, 5, item_local)
            self._table.setItem(
                r, 6, QTableWidgetItem(fmt_size(m.local_size or m.file_size)))
            self._table.setItem(r, 7, QTableWidgetItem(""))  # 上次执行

    def _set_all_checked(self, checked: bool) -> None:
        state = (Qt.CheckState.Checked if checked
                 else Qt.CheckState.Unchecked)
        self._building = True  # 批量设值不当作用户点击，设完手动刷计数
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item is not None:
                item.setCheckState(state)
        self._building = False
        self._update_count()

    def _checked_rows(self) -> list[int]:
        """当前勾选的行号（升序 = 表格行序 = 批次执行顺序）。"""
        return [r for r in range(self._table.rowCount())
                if self._table.item(r, 0).checkState()
                == Qt.CheckState.Checked]

    def _update_count(self) -> None:
        """刷新底部提示行的勾选计数。_building=True 期间（建表填充、
        批量设值）itemChanged 也会触发本方法——此时直接返回，由
        调用方设完后手动刷一次，避免逐项重算。"""
        if self._building:
            return
        self._hint.setText(
            f"已勾选 {len(self._checked_rows())} / "
            f"{self._table.rowCount()} 个条目。")

    def _on_execute_clicked(self) -> None:
        """【执行选中】：只发请求，不动勾选、不留痕。受理与否由
        主窗口回话：受理 → mark_executed（留痕 + 清勾选），没受理 →
        notify_not_started（勾选保留）。信号槽同步执行，回叫发生在
        emit 返回之前，本方法无需任何后续处理。"""
        rows = self._checked_rows()
        if not rows:
            self._hint.setText("没有勾选任何条目——先在表格第一列勾选要"
                               "处理的 mod。")
            return
        self._hint.setText("")
        action = self._combo_action.currentData()
        ids = [self._mods[r].mod_id for r in rows]
        self.execute_requested.emit(self._app_id, action, ids)

    # ---------- 受理回执（主窗口调用） ----------

    def mark_executed(self, action: str, mod_ids: list[int]) -> None:
        """受理回执：批次真正开起来了。留痕与清勾选只覆盖本次执行的
        编号，其余行不受影响。留痕记的是所选动作；其中被剔除的条目
        （版本未知等）不逐条区分——真相以日志与批次汇总为准。"""
        id_set = set(mod_ids)
        for r, m in enumerate(self._mods):
            if m.mod_id not in id_set:
                continue
            self._table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
            self._table.item(r, 7).setText(_ACTION_NAME.get(action, action))
        self._hint.setStyleSheet("color: #46a758;")
        self._hint.setText(
            f"已受理 {len(mod_ids)} 个"
            f"（{_ACTION_NAME.get(action, action)}）——进度见底部控制台"
            "·「下载批次」；批次结束后会弹收尾确认清单，核对勾选后"
            "【确认入账】版本才落账。")

    def notify_not_started(self, reason: str) -> None:
        """拒绝回执：批次没开起来。勾选原样保留——修好原因（启动
        steamcmd / 等上一批跑完）后直接再点【执行选中】即可。"""
        self._hint.setStyleSheet("color: #f5a623;")
        self._hint.setText(
            f"批次没有开始：{reason}"
            "（清单可能被主窗口挡住：任务栏或 Alt+Tab 切回来）")


# 动作 → 显示文案（下拉框条目与留痕列共用）：值引用类常量单源，
# 别在别处再写一份字面量
_ACTION_LABELS = (
    (UpdateSelectDialog.ACT_BACKUP, "备份+更新"),
    (UpdateSelectDialog.ACT_UPDATE, "仅更新"),
)
_ACTION_NAME = dict(_ACTION_LABELS)  # 动作值 → 中文（留痕列用）
