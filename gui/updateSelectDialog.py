"""更新确认对话框
"""
"""
日常更新一条龙的"人工确认"关口（决策 26 第②步 / T20c / 决策 40 /
决策 36⑦⑧）：检测发现新版本后弹出，非模态工作台——

- 勾选条目（默认全选）→ 底部选动作 → 点【执行选中】请求开批；
- 非模态（决策 36⑦）：窗口开着时软件任何地方都能去——steamcmd 没
  启动就切到底部控制台启动并登录，回来再点执行（勾选不丢）；模态
  会把执行链锁死（踩坑㊱：模态窗口挡住它依赖的窗口外之物 = 死锁）；
- 受理回执（决策 36⑧）：点【执行选中】只发请求，本窗口不自行动
  勾选——主窗口确认批次真正受理后回叫 mark_executed（留痕 + 清勾
  选），没受理则回叫 notify_not_started（勾选原样保留、提示原因）。
  没开起来不装作在跑（决策 42④）；
- 已受理的条目在「上次执行」列留痕；重复执行无害：备份按保留策略
  滚动，steamcmd 对已最新的条目校验后秒过；
- 点【完成】或关闭窗口 = 收工。已经开出的批次不受影响（批次归批量
  下载控制器管，跑完照常回叫模块页），没执行的条目之后可到 mod 库
  页手动下载。

动作只有两种（不勾 = 不动）：
- 备份+更新：先把当前版本复制进备份区（新版不满意可到【备份管理】
  页恢复），再下载新版本。默认动作——更新前留一条退路；
- 仅更新：直接下载。

没有旧版本可备份的条目（还没下载过 / 手动确认入账而本地版本未知）
勾了「备份+更新」也会自动按「仅更新」执行——主窗口在执行前降级并
写日志（备份引擎拒绝无版本的备份，先降级免得等备份失败）。

每行一个【↗】按钮直达工坊页面查详情；本窗口非模态，切去别的页面
或控制台都不影响这里的状态。约定（T25）：以后所有"选 mod 做某事"
的窗口都要带这个按钮。

数据由主窗口组装后传入（Mod 对象整行 + 批次归属的档案 AppID），本
对话框零数据库访问、零网络请求。开批通过 execute_requested 信号请
主窗口执行——本对话框只管收集勾选与动作，不认识控制器。
"""
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from core.models import Mod
from gui.formatters import abs_time, fmt_size

_COLUMNS = ["选", "", "编号", "标题", "远端版本", "本地版本", "大小",
            "上次执行"]


class UpdateSelectDialog(QDialog):
    """更新确认清单（非模态工作台）：执行不关窗、可分批反复操作；
    与底部控制台并用（steamcmd 没启动时切过去启动登录，回来再执行）。"""

    # 【执行选中】：参数 = (批次归属档案 AppID, 动作, mod_id 清单)。
    # 主窗口接——上一批还在跑则提示稍候，否则开批并把受理结果回叫
    # 给本窗口（mark_executed / notify_not_started，决策 36⑧）
    execute_requested = Signal(int, str, list)

    # 动作常量（单源住在本类；主窗口按 UpdateSelectDialog.ACT_BACKUP
    # 比对分组）。★值必须与信号里发的完全一致：combo 的 currentData
    # 取自 _ACTION_LABELS 的值，而 _ACTION_LABELS 引用的就是这两个
    # 常量。v2.17 实证（踩坑㊲）：类上曾误挂显示文案（"备份+更新"）
    # 当常量，与信号发的数据值永远不相等——"备份+更新"被静默降级成
    # 直接下载、不备份
    ACT_BACKUP = "backup_and_update"  # 先备份当前版本，再下载新版
    ACT_UPDATE = "update_only"        # 直接下载新版

    def __init__(self, mods: list[Mod], app_id: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发现更新——选择要执行的条目")
        self.resize(880, 540)
        self._mods = list(mods)
        # 批次归属 = 检测那一刻的档案，随执行信号原样带回。非模态下
        # 档案能切——主窗口在切档案时自动关掉本窗口（决策 36⑦），
        # 本窗口存活期间归属不变
        self._app_id = app_id
        self._building = True  # 建表期间 itemChanged 是程序设值

        root = QVBoxLayout(self)
        tip = QLabel(
            f"检测到 {len(self._mods)} 个 mod 有新版本。勾选条目、"
            "在底部选动作、点【执行选中】：\n"
            "· 备份+更新 = 先把当前版本复制进备份区（可回滚），再下载"
            "新版；\n"
            "· 仅更新 = 直接下载；不勾选 = 不动。\n"
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
        for col, width in ((0, 40), (1, 44), (2, 96), (4, 140), (5, 140),
                           (6, 90), (7, 110)):
            self._table.setColumnWidth(col, width)
        self._fill_rows()
        root.addWidget(self._table, 1)

        bottom = QHBoxLayout()
        btn_all = QPushButton("全选", self)
        btn_all.clicked.connect(lambda: self._set_all_checked(True))
        btn_none = QPushButton("全不选", self)
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
        "远端哪天更的 / 本地是哪天的"，相对时间（3 天前）不方便比。"""
        for r, m in enumerate(self._mods):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked)
            if m.status != "downloaded" or m.version_unknown:
                chk.setToolTip(
                    "这个条目没有旧版本可备份（尚未下载过，或本地版本"
                    "未知）——选「备份+更新」也会自动按「仅更新」执行")
            self._table.setItem(r, 0, chk)
            # ↗ = 打开工坊页面查详情（T25 契约：选 mod 的窗口都带）
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
            local = (abs_time(m.local_timeupdated) if m.local_timeupdated
                     else "未下载")
            self._table.setItem(r, 5, QTableWidgetItem(local))
            self._table.setItem(
                r, 6, QTableWidgetItem(fmt_size(m.local_size
                                                or m.file_size)))
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

    def _on_execute_clicked(self) -> None:
        """【执行选中】：只发请求，不动勾选、不留痕（决策 36⑧）。
        受理与否由主窗口回话：受理 → mark_executed（留痕 + 清勾选），
        没受理 → notify_not_started（勾选保留）。信号槽同步执行，
        回叫发生在 emit 返回之前，本方法无需任何后续处理。"""
        rows = self._checked_rows()
        if not rows:
            self._hint.setText("没有勾选任何条目——先在表格第一列勾选要"
                               "处理的 mod。")
            return
        self._hint.setText("")
        action = self._combo_action.currentData()
        ids = [self._mods[r].mod_id for r in rows]
        self.execute_requested.emit(self._app_id, action, ids)

    # ---------- 受理回执（主窗口调用，决策 36⑧） ----------

    def mark_executed(self, action: str, mod_ids: list[int]) -> None:
        """受理回执：批次真正开起来了。留痕与清勾选只覆盖本次执行的
        编号，其余行不受影响。留痕记的是所选动作；其中被主窗口降级成
        「仅更新」的条目（无旧版本可备份）不逐条区分——真相以日志与
        批次汇总为准。"""
        id_set = set(mod_ids)
        for r, m in enumerate(self._mods):
            if m.mod_id not in id_set:
                continue
            self._table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
            self._table.item(r, 7).setText(_ACTION_NAME.get(action, action))
        self._hint.setStyleSheet("color: #46a758;")
        self._hint.setText(
            f"已受理 {len(mod_ids)} 个（{_ACTION_NAME.get(action, action)}"
            "）——进度见底部控制台 ·「下载批次」；批次结束后自动复扫"
            "入账。")

    def notify_not_started(self, reason: str) -> None:
        """拒绝回执：批次没开起来。勾选原样保留——修好原因（启动
        steamcmd / 等上一批跑完）后直接再点【执行选中】即可。"""
        self._hint.setStyleSheet("color: #f5a623;")
        self._hint.setText(
            f"批次没有开始：{reason}"
            "（清单可能被主窗口挡住：任务栏或 Alt+Tab 切回来）")


# 动作 → 显示文案（combo 条目与留痕列共用）：值引用类常量单源，
# 别在别处再写一份字面量
_ACTION_LABELS = (
    (UpdateSelectDialog.ACT_BACKUP, "备份+更新"),
    (UpdateSelectDialog.ACT_UPDATE, "仅更新"),
)
_ACTION_NAME = dict(_ACTION_LABELS)  # 动作值 → 中文（留痕列用）
