"""收尾确认清单
"""
"""
gui/confirmListDialog.py · 批次收尾后的"入账关口"（计划书 D5①）：
下载成功的条目在这里列队，用户核对后点【确认入账】，版本号才经
确认门（repo.confirm_items）写进账本。默认全部不勾——入账是用户
的背书，绝不替用户做主。

与 V1 updateSelectDialog 的关系：不是改造，是新建（抛弃清单已记）。
V1 的"下载前选择"住在下载前，本清单住在下载后；V1 本体留给 M3
检测页做【备份并下载】的宿主。

非模态三件套由 MainWindow 负责（同刻至多一份 / 切档案自动关 /
不挡主窗操作），本对话框只管自己的一亩三分：列表、勾选、确认。

列说明（一行 = 一条待确认判决）：
- 勾选框：默认不勾
- 编号 / 标题：标题取判决行顺手存的值（D39），账上没有的条目也能显示
- 触发值：开批前见到的版本（检测页或开批前批查）
- 现场值：收尾批查的 time_updated（核对失败 = —）
- 将写入：确认后进 confirmed_version 的值（D4 分支结果）；
  显示"版本未知"= 确认后仍是版本未知态（备份守卫会拒绝它），
  但状态会变成"已下载"——这就是"下载了但版本说不清"的诚实账。
  灰色显示 = 未验证（批查没吻合/没成功），下轮检测会再报。
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from core.constants import CONFIRMED_SOURCE_ZH
from core.modRepository import ModRepository

_C_MUTED = "#8a8a8f"   # 未验证 / 版本未知弱化色（与库页口径一致）
_COL_CHECK = 0


class ConfirmListDialog(QDialog):
    def __init__(self, repo: ModRepository, game, verdicts: list,
                 parent: QWidget | None = None, log=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game = game
        self._log = log
        self.setWindowTitle("收尾确认清单")
        self.setModal(False)          # 非模态：开着也能去别页核对
        self.resize(720, 420)

        root = QVBoxLayout(self)
        # 档案名在闭包外要用：先落变量
        game_name = getattr(game, "name", "") if game is not None else ""
        head = QLabel(f"「{game_name}」下载批次收尾：核对无误的条目请勾选"
                      "后点【确认入账】。\n确认 = 把「将写入」的版本登记为"
                      "本地版本（可撤销）；先不确认也可以，之后在"
                      "「入账中心」处理。", self)
        head.setWordWrap(True)
        root.addWidget(head)

        self._table = QTableWidget(self)
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(
            ["勾选", "编号", "标题", "触发值", "现场值", "将写入"])
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._table, 1)

        btns = QHBoxLayout()
        self._btn_all = QPushButton("全选 / 全不选", self)
        self._btn_all.clicked.connect(self._toggle_all)
        btns.addWidget(self._btn_all)
        btns.addStretch(1)
        self._btn_later = QPushButton("稍后处理", self)
        self._btn_later.setToolTip("关闭本清单；待确认项保留，"
                                   "可在「入账中心」继续处理")
        self._btn_later.clicked.connect(self.reject)
        btns.addWidget(self._btn_later)
        self._btn_confirm = QPushButton("确认入账", self)
        self._btn_confirm.setToolTip("把勾选条目的「将写入」版本经确认门"
                                     "写进账本（唯一写入点）")
        self._btn_confirm.clicked.connect(self._on_confirm)
        btns.addWidget(self._btn_confirm)
        root.addLayout(btns)

        self._load(verdicts)

    # ---------------- 数据 ----------------
    def _load(self, verdicts: list) -> None:
        """按传入的待确认判决行重建表格（确认成功后重调 = 刷新）。"""
        rows = [v for v in verdicts
                if self._game is None or v.game_id == self._game.app_id]
        self._table.setRowCount(len(rows))
        for i, v in enumerate(rows):
            # 勾选框
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)   # 默认全不勾
            chk.setData(Qt.ItemDataRole.UserRole, v.mod_id)
            self._table.setItem(i, _COL_CHECK, chk)
            self._table.setItem(i, 1, QTableWidgetItem(str(v.mod_id)))
            self._table.setItem(i, 2, QTableWidgetItem(v.title or ""))
            self._table.setItem(i, 3, QTableWidgetItem(
                str(v.version_trigger) if v.version_trigger is not None else "—"))
            self._table.setItem(i, 4, QTableWidgetItem(
                str(v.version_query) if v.version_query is not None else "—"))
            # 将写入：NULL = 版本未知态（合法但备份守卫会拒）；灰 = 未验证
            if v.version_written is None:
                item = QTableWidgetItem("版本未知")
                item.setForeground(QColor(_C_MUTED))
            else:
                item = QTableWidgetItem(str(v.version_written))
                if v.source != "verified":
                    item.setForeground(QColor(_C_MUTED))
                    item.setText(item.text()
                                 + f"（{CONFIRMED_SOURCE_ZH.get(v.source, v.source)}）")
            self._table.setItem(i, 5, item)
        self._btn_confirm.setText(f"确认入账（{len(rows)} 条待处理）")

    # ---------------- 动作 ----------------
    def _toggle_all(self) -> None:
        rows = self._table.rowCount()
        if not rows:
            return
        any_off = any(
            self._table.item(r, _COL_CHECK).checkState()
            != Qt.CheckState.Checked for r in range(rows))
        state = (Qt.CheckState.Checked if any_off
                 else Qt.CheckState.Unchecked)
        for r in range(rows):
            self._table.item(r, _COL_CHECK).setCheckState(state)

    def _checked_ids(self) -> list[int]:
        ids = []
        for r in range(self._table.rowCount()):
            it = self._table.item(r, _COL_CHECK)
            if it.checkState() == Qt.CheckState.Checked:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        return ids

    def _on_confirm(self) -> None:
        ids = self._checked_ids()
        if not ids:
            QMessageBox.information(self, "确认入账", "先勾选要确认的条目。")
            return
        try:
            n = self._repo.confirm_items(ids)   # 确认门正门（R17）
        except ValueError as exc:
            # 黑名单 / 无 pending 行 / 已删除——实现的人话异常直接展示
            QMessageBox.warning(self, "确认入账失败", str(exc))
            return
        if self._log is not None:
            self._log.ok(f"已确认入账 {n} 条（当前档案）")
        # 刷新：队列里该档案还有剩余就重画，清空则自动关
        remaining = self._repo.pending_confirmations(
            self._game.app_id if self._game is not None else None)
        if remaining:
            self._load(remaining)
        else:
            self.accept()
