"""mod 详情面板
"""
"""
gui/modDetailPanel.py · mod 库页右侧的详情面板（只读展示）。
选中列表行 → show_mod()；清空选择 → show_mod(None) 显示占位提示。

口径（与 modListModel 同源，改口径两处一起改）：
- 本地版本 = 你确认过的版本；来源词取 constants.CONFIRMED_SOURCE_ZH，
  未验证/旧账两档附一行灰色说明（右键「设定本地版本…」可重新认定）；
- 「最近判决」= repo.list_verdicts 最近 5 条（D22）。判决 = 本地版本
  每一次落账的凭据：批次收尾确认、认领、手动设定各记一条——账上
  为什么是这个版本，判决史说了算，有争议先看这里；
- 面板零写操作：唯一的动作按钮「打开 mod 文件夹」只发请求信号，
  动作实现唯一住在 gui/modFolderOpener（打开行为全软件一份）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.constants import CONFIRMED_SOURCE_ZH, VERDICT_KIND_ZH
from core.formatters import abs_time, fmt_size, relative_time, status_zh

# 未验证/旧账两档来源的灰色说明（一次性文案，留本模块——D37 准入）
_SOURCE_NOTES = {
    "unverified": "这一版没有终端「下载成功」判决背书（批查失败但照常"
                  "确认）——要重认一遍：右键「设定本地版本…」，或重新"
                  "下载后确认。",
    "inherited_acf": "旧账：一次性迁移时继承的记录——右键「设定本地"
                     "版本…」可重新认定。",
}
_DIM = "#8a8a8a"   # 弱化信息统一灰（与列表模型同拍）
_VERDICT_SHOW = 5  # 最近判决展示条数


class ModDetailPanel(QWidget):
    """详情面板。open_folder_requested(编号)：打开文件夹按钮发出，
    mod 库页接住后调 gui/modFolderOpener 的唯一实现（面板不认识
    Game 对象，不直接开文件夹）。"""

    open_folder_requested = Signal(int)

    def __init__(self, repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._mod = None
        self._vals: dict[str, QLabel] = {}  # 字段名 → 值标签（惰性建）
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        root.addWidget(self._stack)

        # 页 0：未选中占位
        ph = QWidget(self)
        pl = QVBoxLayout(ph)
        pl.addStretch(1)
        self._placeholder = QLabel("在左侧列表选中一个 mod\n查看详情", ph)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(f"color: {_DIM};")
        pl.addWidget(self._placeholder)
        pl.addStretch(2)
        self._stack.addWidget(ph)

        # 页 1：详情正文
        body = QWidget(self)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(6)

        self._title = QLabel(body)
        self._title.setWordWrap(True)
        self._title.setStyleSheet("font-size: 15px; font-weight: 600;")
        bl.addWidget(self._title)

        # 字段区：表单两列（字段名 | 值）。值标签一律可选中复制——
        # 编号、作者 ID 要粘到别处用（高级筛选、登记框）
        self._form = QFormLayout()
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        bl.addLayout(self._form)

        # 来源灰色说明行：只在未验证/旧账时出现
        self._src_note = QLabel(body)
        self._src_note.setWordWrap(True)
        self._src_note.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        self._src_note.hide()
        bl.addWidget(self._src_note)

        bl.addWidget(self._hline())

        cap = QLabel("最近判决（本地版本每一次落账的凭据，新 → 旧）", body)
        cap.setStyleSheet("font-weight: 600;")
        bl.addWidget(cap)
        self._verdicts = QLabel(body)
        self._verdicts.setWordWrap(True)
        self._verdicts.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self._verdicts)

        bl.addStretch(1)
        btn_row = QHBoxLayout()
        self._btn_folder = QPushButton("打开 mod 文件夹", body)
        self._btn_folder.setToolTip(
            "在文件管理器打开这个 mod 的下载内容文件夹"
            "（下载目录\\编号）")
        self._btn_folder.clicked.connect(self._on_open_folder)
        btn_row.addWidget(self._btn_folder)
        btn_row.addStretch(1)
        bl.addLayout(btn_row)

        self._stack.addWidget(body)
        self._stack.setCurrentIndex(0)
        self.setMinimumWidth(240)

    @staticmethod
    def _hline() -> QFrame:
        """主题安全水细分隔线（welcomePage 同款）。"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ---------- 对外 ----------

    def show_mod(self, mod) -> None:
        """显示一个 mod；传 None = 回到未选中占位。"""
        self._mod = mod
        if mod is None:
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)

        title = mod.title or "（无标题）"
        self._title.setText(title)
        self._title.setToolTip(title)

        self._set("编号", str(mod.mod_id))
        self._set("状态", status_zh(mod.status))
        creator = str(getattr(mod, "creator", "")
                      or getattr(mod, "creator_id", "") or "")

        self._set("作者", creator or "—")
        self._set("大小", fmt_size(getattr(mod, "local_size", None)))
        rt = getattr(mod, "time_updated", None)
        self._set("远端版本",
                  f"{relative_time(rt)}（{abs_time(rt)}）" if rt
                  else "从未检测")
        self._set("特别关注",
                  "★ 是" if getattr(mod, "special", False) else "—")
        note = getattr(mod, "note", "") or ""
        self._set("备注", note or "—")

        cv = getattr(mod, "confirmed_version", None)
        source = getattr(mod, "confirmed_source", "") or ""
        if cv:
            word = CONFIRMED_SOURCE_ZH.get(source, source) or "—"
            self._set("本地版本",
                      f"{relative_time(cv)}（{abs_time(cv)}）· {word}")
            tip = _SOURCE_NOTES.get(source)
            self._src_note.setText(tip or "")
            self._src_note.setVisible(bool(tip))
        else:
            self._set("本地版本", "未知")
            self._src_note.setText(
                "还没有确认过本地版本：批次收尾清单里勾选确认，"
                "或右键「设定本地版本…」手工认定。")
            self._src_note.setVisible(True)

        self._fill_verdicts(mod.mod_id)

    # ---------- 内部 ----------

    def _set(self, key: str, value: str) -> None:
        """字段赋值（惰性建行：第一次出现的字段名固定进表单，
        之后只改文字——表单行序稳定不跳动）。"""
        val = self._vals.get(key)
        if val is None:
            val = QLabel(self)
            val.setWordWrap(True)
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self._form.addRow(QLabel(key, self), val)
            self._vals[key] = val
        val.setText(value)

    def _fill_verdicts(self, mod_id: int) -> None:
        """最近判决（D22）。repo.list_verdicts 约定返回新 → 旧；
        判决史是展示性信息，取不到不拦详情（异常兜底显示暂无）。"""
        try:
            rows = self._repo.list_verdicts(mod_id)
        except Exception:
            rows = []
        if not rows:
            self._verdicts.setText(
                "（暂无——批次收尾确认、认领、手动设定时各记一条）")
            return
        lines: list[str] = []
        for v in rows[:_VERDICT_SHOW]:
            kind_raw = getattr(v, "kind", "")
            kind = VERDICT_KIND_ZH.get(str(kind_raw), str(kind_raw) or "?")
            when = getattr(v, "occurred_at", None)
            vw = getattr(v, "version_written", None)
            note = str(getattr(v, "note", "") or "")
            line = f"{abs_time(when)}　{kind}"
            if vw:
                line += f"　版本 {abs_time(vw)}"
            if note:
                line += f"　{note}"
            lines.append(line)

        self._verdicts.setText("\n".join(lines))

    def _on_open_folder(self) -> None:
        if self._mod is not None:
            self.open_folder_requested.emit(self._mod.mod_id)
