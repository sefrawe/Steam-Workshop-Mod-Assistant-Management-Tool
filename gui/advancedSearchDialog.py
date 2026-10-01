"""mod 库高级筛选对话框
"""
r"""gui/advancedSearchDialog.py · mod 库页的高级筛选（T12，决策 63）。

形态（拍板记录）：入口在菜单栏顶级项「高级筛选(S)」（决策 63 修订
后），非模态弹出——开着不影响主窗口继续操作（"边查边改"）。
实例由 mod 库页持有（决策 36⑦ 同款道理：局部变量 + show() 出
作用域会被回收，经典闪退），构造即创建不显示；关窗即清空条件（已过时）
（done() 统一漏斗，Esc / X / 关闭按钮三路全覆盖），重开为干净状态。

筛选语义（全部拍板为"并且"关系）：
- 本窗口内所有条件同时满足才显示（AND）；
- 与顶栏的搜索框、状态下拉、特别关注叠加生效——简单筛选是粗筛，
  高级条件在粗筛结果上再收紧，互不替代；
- 标签为精确匹配（整个标签相等），多选时要求条目同时带有所有
  勾选的标签；
- 「近 N 天」按本机时钟滚动计算，每次刷新都用当下时间重算。

交互：所有改动即时生效（300 毫秒防抖）；【清除全部】只清高级条件。

【v2.43 新增三件】
- 作者 ID（精确）：上传者 SteamID64，纯数字文本框（与编号框同一套
  红框校验）。SteamID64 唯一且 17 位，包含匹配全是误命中 → 精确。
  作者值随更新检测从工坊 API 取回；详情面板「作者」字段可选中
  复制后直接粘贴到这里；
- 本地版本时间：与"远端更新时间"同一套交互（快捷档 + 自定义区间），
  筛 acf 回填的本地版本时间；未下载 / 手动确认（版本未知）不属于
  任何区间，自然不命中；
- 标签清单过滤框：几十个标签里找目标用——只隐藏不删除，已勾选的
  标签（哪怕被隐藏）照常参与条件；显示变化不触发防抖。

【内存侧条件（重要，本版边界变化）】
作者 ID 与本地版本时间不进 SQL：repo.list_mods 契约不动（pytest
不碰），这两个条件由 mod 库页在取数后对行做 Python 过滤（几百条
量级成本可忽略——颜色筛选同一哲学）。本对话框只负责把条件收进
AdvancedFilter，在哪过滤是消费方的事（消费方见 modListPage._reload）。

边界（记事本架构约定）：本对话框纯 UI，不碰 repo、不发查询——
条件打包成 AdvancedFilter 交给 mod 库页，由页摊平（core 层零
GUI 依赖，反向 import 不允许）。
"""
from dataclasses import dataclass
import time
from core.appSettings import AppSettings
from PySide6.QtCore import QDateTime, QTime, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 编号/作者输入不合法时的红框样式（只提醒、不弹窗——输错不是错误，
# 是"还没输对"，改过来就恢复）
_BAD_INPUT = "QLineEdit { border: 1px solid #e5484d; }"

_DAY = 86400  # 一天的秒数（滚动时间窗用）


@dataclass(frozen=True)
class AdvancedFilter:
    """一组高级筛选条件。全字段默认"不限"（None/空元组）；
    is_active() 全空时为 False，页面据此跳过传参。
    core 层不 import 本类——页面负责摊平（部分进 SQL、部分内存过滤）。
    """

    title_contains: str | None = None
    note_contains: str | None = None
    mod_id: int | None = None        # 精确编号
    creator_id: str | None = None    # 作者 SteamID64（精确；内存侧过滤）
    size_min: int | None = None      # 字节
    size_max: int | None = None      # 字节
    updated_from: int | None = None  # 远端更新 epoch 秒（含当天 00:00:00）
    updated_to: int | None = None    # 远端更新 epoch 秒（含当天 23:59:59）
    local_from: int | None = None    # 本地版本时间下限（内存侧过滤）
    local_to: int | None = None      # 本地版本时间上限（内存侧过滤）
    tags_all: tuple[str, ...] = ()   # 须同时带有的标签（精确名）

    def is_active(self) -> bool:
        return any((
            self.title_contains, self.note_contains,
            self.mod_id is not None, self.creator_id,
            self.size_min is not None, self.size_max is not None,
            self.updated_from is not None, self.updated_to is not None,
            self.local_from is not None, self.local_to is not None,
            self.tags_all))

    def describe(self) -> str:
        """给人看的一句话摘要（筛选指示按钮的悬浮说明用）。"""
        parts: list[str] = []
        if self.title_contains:
            parts.append(f"标题含“{self.title_contains}”")
        if self.note_contains:
            parts.append(f"备注含“{self.note_contains}”")
        if self.mod_id is not None:
            parts.append(f"编号={self.mod_id}")
        if self.creator_id:
            parts.append(f"作者={self.creator_id}")
        if self.size_min is not None:
            parts.append(f"大小≥{self.size_min / 1024 ** 2:.1f} MiB")
        if self.size_max is not None:
            parts.append(f"大小≤{self.size_max / 1024 ** 2:.1f} MiB")
        if self.updated_from is not None and self.updated_to is not None:
            f = QDateTime.fromSecsSinceEpoch(self.updated_from).date()
            t = QDateTime.fromSecsSinceEpoch(self.updated_to).date()
            parts.append(f"远端更新 {f.toString('yyyy-MM-dd')}"
                         f"～{t.toString('yyyy-MM-dd')}")
        elif self.updated_from is not None:
            days = max(round((time.time() - self.updated_from) / _DAY), 1)
            parts.append(f"远端更新：近 {days} 天")
        if self.local_from is not None and self.local_to is not None:
            f = QDateTime.fromSecsSinceEpoch(self.local_from).date()
            t = QDateTime.fromSecsSinceEpoch(self.local_to).date()
            parts.append(f"本地版本 {f.toString('yyyy-MM-dd')}"
                         f"～{t.toString('yyyy-MM-dd')}")
        elif self.local_from is not None:
            days = max(round((time.time() - self.local_from) / _DAY), 1)
            parts.append(f"本地版本：近 {days} 天")
        if self.tags_all:
            parts.append("标签同时含 " + "、".join(self.tags_all))
        return " · ".join(parts) if parts else "（无生效条件）"


class AdvancedSearchDialog(QDialog):
    """非模态高级筛选窗。mod 库页持有实例、连 conditions_changed，
    每次打开前调 set_available_tags() 喂入当前列表出现过的标签。"""

    conditions_changed = Signal()  # 防抖后的"条件变了"，页面收到就 _reload
    go_to_results = Signal()       # 「到 mod 库查看结果」：MainWindow 负责跳页

    def __init__(self, parent: QWidget | None = None,
                 settings: AppSettings | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("mod 库高级筛选")
        # 关窗清空开关（决策 97）：现读现判不缓存——设置页改完保存，
        # 下一次关窗即按新值执行。None（未注入）按开处理 = 原行为
        self._settings = settings

        # 防抖定时器：任何改动先等 300ms，停手才广播——
        # 连续打字不会一连串触发重查（与顶栏搜索框同一手感）
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.conditions_changed.emit)
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        tip = QLabel(
            "与顶栏筛选叠加：下面所有条件同时满足才会显示。\n"
            "改动即时生效（约 0.3 秒防抖）；窗口开着期间可以"
            "边看结果边改备注、勾选下载。\n"
            "【清除全部】随时清空这里的高级条件；设置页「关闭高级筛选时"
            "自动清空条件」开着（默认）时，关闭本窗口也会自动清空。\n"
            "顶栏的搜索框 / 状态 / 特别关注不受影响。",
            self)

        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        root.addLayout(form)

        # 标题 / 备注：分开指定（顶栏搜索框是两处合查，这里分开设）
        self._title_edit = QLineEdit(self)
        self._title_edit.setClearButtonEnabled(True)
        self._title_edit.setPlaceholderText("包含这几个字就算命中")
        self._title_edit.textChanged.connect(self._poke)
        form.addRow("标题包含", self._title_edit)

        self._note_edit = QLineEdit(self)
        self._note_edit.setClearButtonEnabled(True)
        self._note_edit.setPlaceholderText("包含这几个字就算命中")
        self._note_edit.textChanged.connect(self._poke)
        form.addRow("备注包含", self._note_edit)

        # 编号：精确匹配。工坊编号可超过 21 亿（int32 上限），
        # 所以不做数字旋钮，用文本框 + 数字校验（非法输红框）
        self._id_edit = QLineEdit(self)
        self._id_edit.setClearButtonEnabled(True)
        self._id_edit.setPlaceholderText("完整编号，如 3736343635")
        self._id_edit.textChanged.connect(self._poke)
        form.addRow("mod 编号（精确）", self._id_edit)

        # 作者 ID（v2.43）：上传者 SteamID64。与编号同一套"文本框 +
        # 数字校验"；精确匹配（17 位数字做包含匹配全是误命中）
        self._author_edit = QLineEdit(self)
        self._author_edit.setClearButtonEnabled(True)
        self._author_edit.setPlaceholderText("完整 SteamID64，如 76561198000000000")
        self._author_edit.setToolTip(
            "作者 = 上传者的 SteamID64（纯数字），随更新检测从工坊 API "
            "取回。\n在 mod 库页右侧详情的「作者」字段可选中复制后"
            "粘贴到这里")
        self._author_edit.textChanged.connect(self._poke)
        form.addRow("作者 ID（精确）", self._author_edit)

        # 大小区间：QDoubleSpinBox 的"最小值 = 不限"技巧——
        # 值停在 0 时显示 specialValueText，天然表达"没设下限"
        self._size_min = QDoubleSpinBox(self)
        self._size_min.setRange(0.0, 10_000_000.0)  # 上限 10 TB，到不了
        self._size_min.setSuffix(" MiB")
        self._size_min.setSpecialValueText("不限")
        self._size_min.setDecimals(1)
        self._size_min.valueChanged.connect(self._poke)
        form.addRow("大小下限", self._size_min)

        self._size_max = QDoubleSpinBox(self)
        self._size_max.setRange(0.0, 10_000_000.0)
        self._size_max.setSuffix(" MiB")
        self._size_max.setSpecialValueText("不限")
        self._size_max.setDecimals(1)
        self._size_max.valueChanged.connect(self._poke)
        form.addRow("大小上限", self._size_max)

        # 远端更新时间：快捷档 + 自定义区间两用
        self._quick_combo = QComboBox(self)
        for label, data in (("不限", None), ("近 7 天", 7), ("近 30 天", 30),
                            ("近 90 天", 90), ("自定义区间", "custom")):
            self._quick_combo.addItem(label, data)
        self._quick_combo.currentIndexChanged.connect(self._on_quick_changed)
        form.addRow("远端更新时间", self._quick_combo)
        dates = QWidget(self)
        dh = QHBoxLayout(dates)
        dh.setContentsMargins(0, 0, 0, 0)
        self._from_date = QDateEdit(dates)
        self._from_date.setCalendarPopup(True)
        self._from_date.setDisplayFormat("yyyy-MM-dd")
        self._from_date.setDate(
            QDateTime.currentDateTime().date().addDays(-7))
        self._from_date.dateChanged.connect(self._poke)
        self._to_date = QDateEdit(dates)
        self._to_date.setCalendarPopup(True)
        self._to_date.setDisplayFormat("yyyy-MM-dd")
        self._to_date.setDate(QDateTime.currentDateTime().date())
        self._to_date.dateChanged.connect(self._poke)
        dh.addWidget(QLabel("从", dates))
        dh.addWidget(self._from_date)
        dh.addWidget(QLabel("到", dates))
        dh.addWidget(self._to_date)
        dh.addStretch(1)
        form.addRow("", dates)
        self._on_quick_changed()  # 按默认档（不限）把日期框置灰

        # 本地版本时间（v2.43）：与"远端更新时间"同一套交互。
        # 筛的是 acf 回填的本地版本——未下载 / 手动确认（版本未知）
        # 不属于任何区间，自然不命中（语义见文件头）
        self._local_combo = QComboBox(self)
        for label, data in (("不限", None), ("近 7 天", 7), ("近 30 天", 30),
                            ("近 90 天", 90), ("自定义区间", "custom")):
            self._local_combo.addItem(label, data)
        self._local_combo.currentIndexChanged.connect(self._on_local_changed)
        form.addRow("本地版本时间", self._local_combo)
        ldates = QWidget(self)
        lh = QHBoxLayout(ldates)
        lh.setContentsMargins(0, 0, 0, 0)
        self._local_from = QDateEdit(ldates)
        self._local_from.setCalendarPopup(True)
        self._local_from.setDisplayFormat("yyyy-MM-dd")
        self._local_from.setDate(
            QDateTime.currentDateTime().date().addDays(-30))
        self._local_from.dateChanged.connect(self._poke)
        self._local_to = QDateEdit(ldates)
        self._local_to.setCalendarPopup(True)
        self._local_to.setDisplayFormat("yyyy-MM-dd")
        self._local_to.setDate(QDateTime.currentDateTime().date())
        self._local_to.dateChanged.connect(self._poke)
        lh.addWidget(QLabel("从", ldates))
        lh.addWidget(self._local_from)
        lh.addWidget(QLabel("到", ldates))
        lh.addWidget(self._local_to)
        lh.addStretch(1)
        form.addRow("", ldates)
        self._on_local_changed()  # 按默认档（不限）把日期框置灰

        # 标签：勾选式多选（精确匹配）。清单来自当前列表出现过的标签；
        # 已勾选的标签在清单重建时保留（哪怕它暂时没出现在列表里）
        self._tag_list = QListWidget(self)
        self._tag_list.setFixedHeight(120)
        self._tag_list.itemChanged.connect(self._poke)
        # 标签清单过滤框（v2.43）：只管显示，不改条件语义——
        # "帮找"不"帮删"，被隐藏的已勾选标签照常参与条件
        tag_wrap = QWidget(self)
        tv = QVBoxLayout(tag_wrap)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        self._tag_filter = QLineEdit(tag_wrap)
        self._tag_filter.setClearButtonEnabled(True)
        self._tag_filter.setPlaceholderText(
            "过滤下面的标签清单…（只管显示，已勾选不受影响）")
        self._tag_filter.textChanged.connect(self._apply_tag_filter)
        tv.addWidget(self._tag_filter)
        tv.addWidget(self._tag_list)
        form.addRow("标签（须全有）", tag_wrap)

        tag_hint = QLabel(
            "清单来自当前列表里出现过的标签；重新打开本窗口时自动更新。",
            self)
        tag_hint.setWordWrap(True)
        tag_hint.setStyleSheet("color: gray; font-size: 11px;")
        root.addWidget(tag_hint)

        # 结果反馈行：命中数由 mod 库页每次刷新后回填（本窗口不查库）
        self._result_label = QLabel(
            "未生效——填写任一条件后，mod 库列表即时收窄", self)
        self._result_label.setWordWrap(True)
        root.addWidget(self._result_label)
        root.addStretch(1)

        # 底部按钮：清空 + 关闭（没有"应用"——改动本来就是即时的）
        btns = QHBoxLayout()
        self._goto_btn = QPushButton("到 mod 库查看结果 →", self)
        self._goto_btn.setToolTip(
            "跳到 mod 库页——结果列表就在那里，可直接改备注、勾选下载")
        self._goto_btn.clicked.connect(self.go_to_results.emit)
        btns.addWidget(self._goto_btn)
        btns.addStretch(1)
        clear_btn = QPushButton("清除全部", self)
        clear_btn.setToolTip("清空本窗口全部高级条件并立即刷新列表")
        clear_btn.clicked.connect(self.clear_all)
        btns.addWidget(clear_btn)
        close_btn = QPushButton("关闭", self)
        close_btn.setToolTip(
            "关闭窗口；条件是否随关窗自动清空由设置页"
            "「关闭高级筛选时自动清空条件」决定")

        close_btn.clicked.connect(self.close)
        btns.addWidget(close_btn)
        root.addLayout(btns)

        self.resize(480, 640)  # v2.43 两行新条件，窗口加高一档

    # ---------- 内部小件 ----------

    def _poke(self, *_args) -> None:
        """任何条件变化：刷新编号/作者红框 → 重置防抖计时。"""
        self._parse_mod_id()
        self._parse_author()
        self._debounce.start()

    def _on_quick_changed(self) -> None:
        """远端更新时间：只有自定义才允许改日期。"""
        custom = self._quick_combo.currentData() == "custom"
        self._from_date.setEnabled(custom)
        self._to_date.setEnabled(custom)
        self._poke()

    def _on_local_changed(self) -> None:
        """本地版本时间（v2.43）：与远端同款——只有自定义才允许改日期。"""
        custom = self._local_combo.currentData() == "custom"
        self._local_from.setEnabled(custom)
        self._local_to.setEnabled(custom)
        self._poke()

    def _apply_tag_filter(self, *_args) -> None:
        """标签清单的显示过滤（v2.43）：只隐藏不删除。
        _checked_tags 读的是全部条目（不看隐藏与否），被隐藏的已勾选
        标签照常参与条件。本方法刻意不触发防抖：显示变化不是条件变化，
        不该引起列表重查。"""
        q = self._tag_filter.text().strip().casefold()
        for i in range(self._tag_list.count()):
            it = self._tag_list.item(i)
            it.setHidden(bool(q) and q not in it.text().casefold())

    @staticmethod
    def _size_to_bytes(spin: QDoubleSpinBox) -> int | None:
        if spin.value() <= 0:  # 停在最小值 = 显示"不限" = 不参与筛选
            return None
        return int(round(spin.value() * 1024 * 1024))

    def _parse_mod_id(self) -> int | None:
        """编号框：空 = 不限；纯数字 = 精确编号；其他 = 红框提醒，
        暂不参与筛选（改对即恢复）。"""
        text = self._id_edit.text().strip()
        if not text:
            self._id_edit.setStyleSheet("")
            return None
        if text.isdigit():
            self._id_edit.setStyleSheet("")
            return int(text)
        self._id_edit.setStyleSheet(_BAD_INPUT)
        return None

    def _parse_author(self) -> str | None:
        """作者框（v2.43）：空 = 不限；纯数字 = 精确 SteamID64；
        其他 = 红框提醒。存字符串不转 int：比较按文本逐字相等即可，
        转整数没有收益。"""
        text = self._author_edit.text().strip()
        if not text:
            self._author_edit.setStyleSheet("")
            return None
        if text.isdigit():
            self._author_edit.setStyleSheet("")
            return text
        self._author_edit.setStyleSheet(_BAD_INPUT)
        return None

    def _updated_range(self) -> tuple[int | None, int | None]:
        """远端更新时间的 (下限, 上限)，epoch 秒。
        快捷档：只设下限（按当下时钟滚动算）。自定义：起止日期 →
        当天 00:00:00 ～ 23:59:59（本地时区）。"""
        data = self._quick_combo.currentData()
        if data is None:
            return None, None
        if data == "custom":
            day_start = QDateTime(self._from_date.date(), QTime(0, 0, 0))
            day_end = QDateTime(self._to_date.date(), QTime(23, 59, 59))
            return day_start.toSecsSinceEpoch(), day_end.toSecsSinceEpoch()
        return int(time.time()) - int(data) * _DAY, None

    def _local_range(self) -> tuple[int | None, int | None]:
        """本地版本时间的 (下限, 上限)（v2.43）：与 _updated_range
        完全同构，筛的字段换成 acf 回填的本地版本时间。"""
        data = self._local_combo.currentData()
        if data is None:
            return None, None
        if data == "custom":
            day_start = QDateTime(self._local_from.date(), QTime(0, 0, 0))
            day_end = QDateTime(self._local_to.date(), QTime(23, 59, 59))
            return day_start.toSecsSinceEpoch(), day_end.toSecsSinceEpoch()
        return int(time.time()) - int(data) * _DAY, None

    def _checked_tags(self) -> tuple[str, ...]:
        """勾选中的标签（勾选框列表是唯一来源，顺序按字母序稳定）。
        读全部条目、不看隐藏与否——标签过滤框只管显示。"""
        out = []
        for i in range(self._tag_list.count()):
            it = self._tag_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.text())
        return tuple(sorted(out))

    # ---------- 对外（mod 库页调用）----------

    def set_available_tags(self, tags: list[str]) -> None:
        """重建标签清单。已勾选的标签优先保留——哪怕它暂时没出现在
        列表里（比如列表正被别的条件筛得很窄），条件不该因此悄悄丢掉。"""
        checked = set(self._checked_tags())
        merged = sorted(set(tags) | checked)
        self._tag_list.blockSignals(True)  # 重建过程别触发防抖/重查
        try:
            self._tag_list.clear()
            for t in merged:
                item = QListWidgetItem(t, self._tag_list)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if t in checked
                    else Qt.CheckState.Unchecked)
        finally:
            self._tag_list.blockSignals(False)
        self._apply_tag_filter()  # 重建后套用当前过滤词（v2.43）

    def conditions(self) -> AdvancedFilter | None:
        """收集当前条件。全空时返回 None——页面据此不传高级参数，
        查询路径与没有高级筛选时完全一致。"""
        updated_from, updated_to = self._updated_range()
        local_from, local_to = self._local_range()
        f = AdvancedFilter(
            title_contains=self._title_edit.text().strip() or None,
            note_contains=self._note_edit.text().strip() or None,
            mod_id=self._parse_mod_id(),
            creator_id=self._parse_author(),
            size_min=self._size_to_bytes(self._size_min),
            size_max=self._size_to_bytes(self._size_max),
            updated_from=updated_from,
            updated_to=updated_to,
            local_from=local_from,
            local_to=local_to,
            tags_all=self._checked_tags(),
        )
        return f if f.is_active() else None

    def set_hit_count(self, n: int | None) -> None:
        """mod 库页刷新后回填命中数；None = 条件未生效（空闲文案）。
        对话框自己不查库——数字永远是页面最近一次查询的结果。"""
        if n is None:
            self._result_label.setText(
                "未生效——填写任一条件后，mod 库列表即时收窄")
            self._goto_btn.setEnabled(False)
        else:
            self._result_label.setText(
                f"当前命中 {n} 个 mod（结果在 mod 库列表中显示）")
            self._goto_btn.setEnabled(True)

    def done(self, result: int) -> None:
        """所有关闭路径的单一漏斗（Esc / 窗口 X / 【关闭】按钮三路全覆盖，
        也只走一次）。

        关窗清空开关（决策 97）：设置页同名开关开着（默认）时照旧清空
        ——重开为干净状态；关着（"0"）时跳过清空，条件与 mod 库页的
        筛选指示原样保留，重开接着用。【清除全部】按钮不经过这里，
        两种设置下都随时可用。

        ★ 踩坑52 机制照旧成立：主窗口退出时本对话框被联动关闭，走与
        Esc/× 相同的 closeEvent → reject → done() 路。开着清空时，退出
        路上 modListPage.shutdown() 已先摘除 conditions_changed →
        _reload 的连接（断链），广播落空不炸；关着清空时 done() 本就
        不广播，两条路都碰不到已关闭的数据库。
        """
        if self._settings is None or \
                self._settings.get("advsearch_autoclear") != "0":
            self.clear_all()
        super().done(result)

    def clear_all(self) -> None:
        """清空全部高级条件并立即广播（不等防抖）。只动本窗口，
        顶栏筛选不碰。"""
        self._debounce.stop()
        self._title_edit.clear()
        self._note_edit.clear()
        self._id_edit.clear()
        self._id_edit.setStyleSheet("")
        self._author_edit.clear()           # v2.43
        self._author_edit.setStyleSheet("")
        self._size_min.setValue(0)
        self._size_max.setValue(0)
        self._quick_combo.setCurrentIndex(0)
        self._from_date.setDate(
            QDateTime.currentDateTime().date().addDays(-7))
        self._to_date.setDate(QDateTime.currentDateTime().date())
        self._local_combo.setCurrentIndex(0)   # v2.43
        self._local_from.setDate(
            QDateTime.currentDateTime().date().addDays(-30))
        self._local_to.setDate(QDateTime.currentDateTime().date())
        self._tag_filter.clear()               # v2.43（清过滤 → 全部显示）
        for i in range(self._tag_list.count()):
            self._tag_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.conditions_changed.emit()
