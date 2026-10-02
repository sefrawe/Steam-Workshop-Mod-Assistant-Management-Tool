"""异常处理功能模块
"""
r"""六类异常的汇聚诊断台（gui/exceptionPage.py）。

【本页是干什么的】
一键把当前游戏档案的异常全查一遍，按"桶"分组展示——"桶"就是
一类异常的分组的通俗叫法（一桶 = 一类），每个桶给"发生了什么 +
该去哪修"的引导。与账实核验页的分工：核验页管"账↔盘逐条对账"，
本页管"全档案异常总览与处置引导"。

【六类异常（六个桶）与修法】
① 下载未完成（下载中断 / 空目录残留）→ 修法 = 重新下载
② 账实不符（账本记已下载、盘上目录没了）→ 修法 = 重新下载
③ 孤儿目录（盘上有、账本不认识）→ 修法 = 到【mod 库】页扫描入账
④ 远端失效（作者删除/下架，Steam 接口返回 result=9）→
   修法 = 三步主路径（见下），进阶 = 「关联替换」
⑤ 查询失败与疑似合集 → 看明细；疑似合集去【更新检测】页展开
⑥ 多前端环境冲突（无法自动检测，静态自查清单）

【两种检测】
- 【开始检测】纯离线毫秒级，同步执行（与扫描本地同理，不值得
  开线程）。覆盖桶①②③与桶④的账本半边。
- 【联网深度检测】后台线程重查库内全部未删除 mod 的远端实况，
  补桶④另一半（result=9 确认真失效）与桶⑤。
  纯读诊断：不写库、不改元数据、不拍快照。
  写库例外（v2.49）：【拉取依赖】更新依赖清单表 mod_dependencies；其余检测仍只读。
  写库仅桶④的处置动作（右键【软删除此记录】/「关联替换」），
  且逐条确认后才发生。

 【桶④处置 philosophy（v2.45 修订：result=9 多义性实测定口径）】
 result=9 = Steam 匿名接口看不见条目（作者删除/下架/转私有等）
 ——接口看不见 ≠ 一定下载不了：实测部分 result=9 条目
 steamcmd 仍能下载成功、部分报 File Not Found。处置改为
 "先验证再处置"：第 0 步 = 右键跳命令生成页试下载一次
 （成本极低，红黄行都开放），下不回来再走三步（工坊页找
 续作 → 网址批量导入入库 → 软删除旧记录）。「关联替换」
 仍为进阶项（仅限有失效归档的条目，无归档行菜单置灰+悬停
 说明）。软删除可逆（记录保留可恢复、盘上文件不动），
 result=9 条目每次深检仍会列回本表——属 Steam 接口判定
 边界，说明列注明，不算检测错误。


【本版（打包前 todo 3，第 2 刀）桶④升级为"失效 mod 管理台"】
- 桶④从逐行卡片升级成表格（备份管理页风格）：列 = ↗（打开
  工坊页）｜ mod 标题 ｜ mod 编号 ｜ 分类 ｜ 说明/原因。行内
  控件只有 ↗ 一个按钮，全部动作走右键菜单——行内控件随排序
  搬家的错位风险是删除页 v2.32 交过学费的坑，所以排序用下拉
  重灌式、刻意不做表头点击排序；
- "已收录"成为一等分类：账本记「已收录」（从未下载过）而远端
  已确认失效的条目在表格里单列一类，可筛选/分组/排序——它们
  没有失效归档（归档由【更新检测】建立），「关联替换」点了必
  报错，所以不给替换只给软删除，行文案说实话；
- 软删除成为主路径动作：走 repo.mark_deleted 正门（与核验页
  "标记为已移除"、mod 库页"软删除"同一条路，十字段删除前
  快照逐字段同款）——确认弹窗默认"否"（决策 69⒋），文案说清
  回程票（记录保留可恢复、盘上文件不动、想清文件去清理模块，
  failed 条目在那里正好只给"仅删文件"，分工咬合）；执行后
  自动复检；
- 筛选/视图/排序三个下拉（状态：全部/归档失效/已收录·未下载/
  已替换/已删除；视图：平铺/按分类分组；排序：编号/标题/分类）。
  选择状态记在页面级而非控件里：卡片是检测产物每次整体重建，
  下拉若跟着重置，"筛着修、修完复检"的节奏就断了；
- 软删除后的条目复检时归入"已删除"分类（灰显、只留查看类
  动作）——处置闭环看得见，而不是行凭空消失；
- 已替换的行右键可直达替代 mod 的工坊页面。

【其余桶（①②③⑤）维持上一版】
编号列表 + 右键菜单（打开工坊页面 / 复制编号 / 跳命令页）；
整页滚动、"先读我"说明卡、「只看有问题的桶」开关均不变。

【线程约定（照更新检测页同款，含两个关键防闪退细节）】
- 只有联网深检进后台线程；分批驱动查询、批间礼貌间隔、批边界
  生效的停止协议，全部照搬先例；
- 线程收尾必须在 finished 信号里先取引用置 None 再 wait()——
  在 succeeded/failed/stopped 的处理函数里直接销毁引用会踩中
  "销毁仍在运行的线程"（进程闪退 0xC0000409）；
- 深检进行中允许切档案：结果区不动（仍显示开始检测时那份），
  渲染一律按 rep.game_id 取数据，切了也不串。
"""
import time
from gui.modFolderOpener import open_mod_folder
from gui import netGate
import threading

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,QLineEdit,
    QAbstractItemView, QHeaderView,
)

from core import steamPaths
from core.appSettings import AppSettings
from core.models import Game
from core.steamApiClient import (SteamApiCancelled, SteamApiClient,
                                 SteamApiError, WorkshopItem)

from core.urlParser import WORKSHOP_URL_TEMPLATE
from gui.consolePanel import LogBus
from workflows import exceptionFlow

# 与批量下载步骤卡片一致的配色，按用途命名
_C_OK = "#46a758"    # 无异常
_C_FAIL = "#e5484d"  # 失效类（桶④）
_C_WARN = "#f5a623"  # 需要人管
_C_MUTED = "#8a8a8f" # 说明文字/未检测

# 桶④表格的分类（顺序 = 分组视图的组序，也是排序键的序）。
# "已清账" = 彻底清账后的历史归档（账本条目已物理删除，只剩
# failed_mods 证据行——按决策刻意存活）：默认视图不显示、
# 状态筛「已清账」可查（v2.48 决策 102）
_CAT_ORDER = ("归档失效", "已收录·未下载", "已替换", "已删除", "已清账")

# 分类 → 文字颜色（表格"分类"列着色用）
_CAT_COLORS = {
    "归档失效": _C_FAIL,
    "已收录·未下载": _C_WARN,
    "已替换": _C_MUTED,
    "已删除": "#6e6e73",  # 比说明文字更暗：已处理完的
    "已清账": "#55555a",  # 比已删除更暗：彻底终结的历史归档

}

_ID_LIMIT = 20  # 卡片里编号列表最多原样列出多少个，超出折成"…"

# 每批查询的条目数：Steam 官方接口单次上限就是 100（与更新检测页同款）
_BATCH = 100

def _item_to_dict(item: WorkshopItem) -> dict:
    """WorkshopItem → 引擎 QueryFn 契约的纯数据条目。
    只转分类要用的字段；引擎保持不 import 客户端类型（决策 28④ 的
    注入边界），转换这层"翻译"放在页面侧。
    title/banned（桶A 用）用 getattr 宽容提取：客户端封装暂时没带
    这个字段时返回 None，由引擎的 missing_banned_field 口径显式
    降级说明——绝不静默当 0。"""
    return {"publishedfileid": item.mod_id, "result": item.result,
            "file_size": item.file_size,
            "title": getattr(item, "title", None),
            "banned": getattr(item, "banned", None)}

class _DeepWorker(QThread):
    """联网深检后台线程：分批查询全部条目，每批报告一次进度。

    只发网络请求；分类不在这里做（交回主线程调引擎的
    classify_entries），更不碰数据库。停止协议照 _CheckWorker：
    批边界生效，已查到的数据一律丢弃。"""

    batch_done = Signal(int)   # 已完成查询的条目数（驱动进度条）
    succeeded = Signal(list)   # 全部查完，携带纯数据条目字典列表
    failed = Signal(str)       # 请求层面失败，携带给用户看的原因
    stopped = Signal()         # 用户点了"停止"：正常收场，不算失败

    def __init__(self, mod_ids: list[int], *,
                 interval_ms: int, max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._stop_requested = False
        self._cancel = threading.Event()  # 与客户端共用的取消事件

    def stop(self) -> None:
        """请求停止：批边界生效（v2.45 后取消事件让客户端的等待与
        重试立即中断，最多多等一次在途请求的超时）。"""
        self._stop_requested = True
        self._cancel.set()

    def run(self) -> None:
        client = SteamApiClient(
            interval_ms=self._interval_ms,
            max_retries=self._max_retries,
            cancel_event=self._cancel)
        ids = list(dict.fromkeys(self._mod_ids))  # 去重且保序
        done = 0
        entries: list[dict] = []
        try:
            for start in range(0, len(ids), _BATCH):
                if self._stop_requested:
                    self.stopped.emit()
                    return
                chunk = ids[start:start + _BATCH]
                items = client.query_details(chunk)
                entries.extend(_item_to_dict(i) for i in items)
                done += len(chunk)
                self.batch_done.emit(done)
                if start + _BATCH < len(ids):
                    # 批间礼貌间隔；等待期间无法响应停止，
                    # 最多多等一个间隔（同更新检测页口径）
                    time.sleep(self._interval_ms / 1000)
        except SteamApiCancelled:
            # 取消事件在请求/等待中途触发（v2.45）：与批边界停止
            # 同一收场——丢弃结果、走 stopped
            self.stopped.emit()
            return
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        # 兜底：最后一批查询途中点的停，同样按"什么都没发生"处理
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(entries)

class _DepWorker(QThread):
    """依赖拉取后台线程：keyed GetDetails 分批查必需物品清单。
    只发网络请求 + 解析成纯数据字典；判定与写库都在主线程回调
    （repo 不过线程）。停止协议照 _DeepWorker：批边界生效，
    已查数据一律丢弃。"""
    batch_done = Signal(int)
    succeeded = Signal(list)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, mod_ids: list[int], *, key: str,
                 interval_ms: int, max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._key = key
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._stop_requested = False
        self._cancel = threading.Event()

    def stop(self) -> None:
        self._stop_requested = True
        self._cancel.set()

    @staticmethod
    def _to_entry(item) -> dict:
        """KeyedItem → 纯数据条目（引擎不 import 客户端类型的边界，
        _item_to_dict 同款）。children 三态原样保留。"""
        return {"mod_id": item.mod_id, "result": item.result,
                "title": item.title, "children": item.children}

    def run(self) -> None:
        client = SteamApiClient(interval_ms=self._interval_ms,
                                max_retries=self._max_retries,
                                cancel_event=self._cancel)
        ids = list(dict.fromkeys(self._mod_ids))
        done = 0
        entries: list[dict] = []
        try:
            for start in range(0, len(ids), _BATCH):
                if self._stop_requested:
                    self.stopped.emit()
                    return
                chunk = ids[start:start + _BATCH]
                items = client.query_details_keyed(chunk, self._key)
                entries.extend(self._to_entry(i) for i in items)
                done += len(chunk)
                self.batch_done.emit(done)
                if start + _BATCH < len(ids):
                    time.sleep(self._interval_ms / 1000)
        except SteamApiCancelled:
            self.stopped.emit()
            return
        except SteamApiError as exc:
            if "403" in str(exc):
                self.failed.emit(
                    "服务器拒绝（403）：key 无效或已被重置——到"
                    "【Steam API 密钥】页核对或重置后再试")
            else:
                self.failed.emit(str(exc))
            return
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(entries)

class _BucketCard(QFrame):
    """一张桶卡片（可折叠）。

    外观 = 带边框的圆角卡（与批量下载步骤卡片同款做法）；
    结构 = 头部一行（展开箭头 + 桶名 + 状态文字）+ 内容区。

    - 状态文字常显：卡片收起时也能看到"这桶有没有问题"，
      不必为看一眼状态把每张卡都摊开；
    - 点头部收展；程序性 set_expanded 走同一条路（与共享件
      collapsibleSection 同款实现，只是外观是卡片）；
    - 不发收展信号、不做跨会话记忆：卡片是检测的产物，每次检测
      整体重建并按结果重设收展（有异常摊开、无异常收起），记忆
      没有落点——与核验页常驻分区的差别，见文件头说明。
    """

    def __init__(self, name: str, title: str, tip: str = "") -> None:
        super().__init__()
        # 样式选择器限定到本框（QFrame#card_xxx）：QLabel 也是
        # QFrame 子类，不限定的边框会画到卡里每行字上——批量下载
        # 步骤卡片踩过的同款坑，照抄其处理。
        self.setObjectName(f"card_{name}")
        self.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        # 这桶是否"值得看"（有异常 / 未能检查）：
        # 「只看有问题的桶」开关靠它决定藏不藏这张卡。
        self.has_attention = False

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)

        # ---- 头部：箭头开关 + 状态文字 ----
        head = QWidget(self)
        hh = QHBoxLayout(head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(6)
        self._toggle = QToolButton(head)
        self._toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(True)  # 默认展开
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setText(title)
        self._toggle.setStyleSheet("border:none; font-weight:600;")
        if tip:
            self._toggle.setToolTip(tip)
        hh.addWidget(self._toggle)
        self._status = QLabel("", head)
        self._status.setWordWrap(True)  # 踩坑⑨：可能变长的标签开换行
        self._status.setStyleSheet("border:none;")
        hh.addWidget(self._status, 1)   # 状态文字占头部剩余宽度
        v.addWidget(head)

        # ---- 内容区（收展的就是它）----
        self._body = QWidget(self)
        self._box = QVBoxLayout(self._body)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(4)
        v.addWidget(self._body)

        # checkable 按钮的 clicked(bool) 传的就是新状态，直接用
        self._toggle.clicked.connect(self._on_toggle)

    # ---- 收展 ----

    def _on_toggle(self, expanded: bool) -> None:
        """收展唯一出口：用户点箭头与程序性 set_expanded 都到这。"""
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self._body.setVisible(expanded)

    def set_expanded(self, expanded: bool) -> None:
        """程序性收展（检测后按结果重设用）。
        setChecked 只发 toggled 不发 clicked，而我们连的是
        clicked——外观同步要手动调一次 _on_toggle。"""
        self._toggle.setChecked(expanded)
        self._on_toggle(expanded)

    # ---- 状态行与内容 ----

    def set_status(self, text: str, color: str) -> None:
        """头部状态文字（常显——收起也看得到）。"""
        self._status.setText(text)
        self._status.setStyleSheet(f"border:none; color:{color};")

    def add_text(self, text: str, color: str = _C_MUTED) -> QLabel:
        """内容区加一行说明文字（自动换行，踩坑⑨）。"""
        lbl = QLabel(text, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"border:none; color:{color}; font-size:12px;")
        self._box.addWidget(lbl)
        return lbl

    def add_button(self, text: str, tooltip: str,
                   on_click) -> QPushButton:
        """内容区加一个按钮（保持自然宽度，不横向撑满）。"""
        btn = QPushButton(text, self)
        btn.setToolTip(tooltip)
        btn.clicked.connect(on_click)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        self._box.addLayout(row)
        return btn

    def add_widget(self, w: QWidget) -> None:
        """内容区加一个自备布局的控件（表格、编号列表等）。"""
        self._box.addWidget(w)


class ExceptionPage(QWidget):
    # 一键跳命令生成页：参数 = 要勾选的 mod id 列表。
    # 与 mod 库页 / 核验页同一份跳转契约，MainWindow 接线到
    # _on_command_gen_requested（切页 + 重新载入 + 只勾这些）
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._cards_box: QVBoxLayout | None = None
        # 本次检测的卡片表（名字 → 卡片）：「只看有问题的桶」
        # 筛选靠遍历它逐卡显隐
        self._cards: dict[str, _BucketCard] = {}
        # 渲染时一次取齐的 编号 → 账本记录 对照表：列表行显示标题、
        # 行菜单取工坊网址都从这里查（一次取齐，行循环里只查字典）
        self._mods_by_id: dict[int, object] = {}
        # 最近一次本地快检报告：深检结果回来时与它同屏重画
        self._last_local: exceptionFlow.LocalReport | None = None
        self._deep_worker: _DeepWorker | None = None
        # 最近一次深检结果与渲染档案（v2.45）：桶④处置后的自动复检
        # 只重跑本地部分，深检结果刻意保留——重画时带上，桶⑤不回
        # "待检测"、桶④联网确认半边不丢（用户 todo：处置一次就被
        # 打回重跑深检，节奏断了）。set_game 换档案时一并清
        self._last_remote: exceptionFlow.RemoteFindings | None = None
        self._last_health: exceptionFlow.HealthFindings | None = None
        self._last_deps: exceptionFlow.DepFindings | None = None
        self._dep_game_id: int | None = None   # 依赖结果归属档案
        self._dep_worker: _DepWorker | None = None
        self._dep_baseline: dict[int, list[int]] = {}
        self._dep_lib_ids: set[int] = set()
        self._dep_first_fetch = True

        self._owner_game: Game | None = None
        self._b4_checked: set[int] = set()   # 桶④勾选（按 mid，跨重建存活）
        self._id_lists: list = []            # 各桶编号列表登记（查找框过滤用）

        # ---- 桶④管理台的状态（页面级：卡片每次检测重建，
        # 下拉选择若跟着控件被销毁重置，"筛着修、修完复检"的
        # 节奏就断了——状态放这里，重建时恢复）----
        self._b4_rows: list[dict] = []     # 表格数据源（分类后的行）
        self._b4_filter = "全部"           # 状态筛选
        self._b4_sort = "编号"             # 排序键
        self._b4_group = False             # 视图：False=平铺 True=分组
        self._b4_table: QTableWidget | None = None
        self._b4_combo_sort: QComboBox | None = None
        self._build_ui()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        # 整页滚动（核验页/设置页/统计页同款做法）：桶卡片多、
        # 失效条目多时页面总高度可能超过窗口，装进 QScrollArea
        # 兜底——内容永远看得到全，窗口小了就滚。
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

        title = QLabel("异常处理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        # 页首一句话导览（详细说明在下面的"先读我"卡里，不重复占屏）
        tip = QLabel(
            "一键检查当前游戏档案的六类异常，按桶（一类异常一组）给出"
            "修法引导。\n【开始检测】纯离线、毫秒级、只读不动文件；"
            "【联网深度检测】后台重查远端实况（只读不写库），补全"
            "桶④⑤。写库有两处：桶④的处置动作（右键软删除 / 关联替换，逐条确认）；【拉取依赖】更新依赖清单表（其余检测仍只读）。\n"
            "「已收录」（已入库还没下载）是正常排队，不算异常、不在"
            "本页——批量下载去【下载命令生成】页。")

        tip.setWordWrap(True)  # 踩坑⑨
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # ---- "先读我"说明卡（常驻、可折叠、不随检测重建）----
        # 为什么常驻：桶卡片每次检测整体重建（收展按结果重设），
        # 说明卡若跟着重建，用户刚展开读完就被下一次检测收走——
        # 常驻 + 不参与重建，折叠状态在同一次运行里自然保得住。
        self._guide_card = _BucketCard(
            "guide", "先读我：本页怎么用 · 名词解释 · 处置方式",
            tip="展开看详细说明：六个桶各是什么、两种检测的分工、"
                "失效 mod 的三步处置法与「关联替换」进阶用法。"
                "不想读可以一直收着。")
        self._fill_guide_card()
        root.addWidget(self._guide_card)

        # ---- 按钮行 ----
        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._detect_btn = QPushButton("开始检测", btn_row)
        self._detect_btn.setToolTip(
            "检查当前档案的六类异常（本地部分）。离线毫秒级；"
            "未配置 steamcmd 路径时只查账本半边，并在结果里说明")
        self._detect_btn.clicked.connect(self._start_detect)
        self._deep_btn = QPushButton("联网深度检测", btn_row)
        self._deep_btn.setToolTip(
            "重查库内全部未删除 mod 的远端实况（只读，不写库）：\n"
            "确认哪些真失效（result=9）、列出查询失败与疑似合集。\n"
            "需要网络；每 100 个一批、礼貌限速，耗时取决于 mod 数量。\n"
            "本地快检没跑过时会先自动跑一遍本地部分")
        self._deep_btn.clicked.connect(self._start_deep)
        self._deep_stop_btn = QPushButton("停止", btn_row)
        self._deep_stop_btn.setEnabled(False)
        self._deep_stop_btn.clicked.connect(self._stop_deep)
        self._dep_btn = QPushButton("拉取依赖（联网）", btn_row)
        self._dep_btn.setToolTip(
            "带 key 批量拉取全部未删除 mod 的必需物品清单：\n"
            "缺什么（账本里没有的依赖）、变了什么（与上次拉取的"
            "差异）一次说清。\n需要先在【Steam API 密钥】页保存 "
            "key；与深度检测共用联网互斥闸")
        self._dep_btn.clicked.connect(self._start_dep)
        self._dep_stop_btn = QPushButton("停止", btn_row)
        self._dep_stop_btn.setEnabled(False)
        self._dep_stop_btn.clicked.connect(self._stop_dep)

        self._progress = QProgressBar(btn_row)
        self._progress.setVisible(False)
        # 筛选开关：勾上后无异常/待检测的桶整卡隐藏
        self._only_issue = QCheckBox("只看有问题的桶", btn_row)
        self._only_issue.setToolTip(
            "勾上 = 隐藏「无异常」与「待检测」的桶卡片，只留有异常和"
            "未能检查的部分；桶⑥多前端自查是说明性内容，也会被隐藏。")
        self._only_issue.toggled.connect(self._apply_card_filter)
        h.addWidget(self._detect_btn)
        h.addWidget(self._deep_btn)
        h.addWidget(self._deep_stop_btn)
        h.addWidget(self._dep_btn)
        h.addWidget(self._dep_stop_btn)

        h.addWidget(self._only_issue)
        h.addWidget(self._progress, 1)
        root.addWidget(btn_row)
        # 查找框（v2.45 用户 todo）：按标题/编号过滤各桶条目——只隐藏
        # 不销毁，清空即全回。列表逐项 setHidden；桶④表格随重灌过滤
        srow = QWidget(self)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel("查找：", srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            "按标题 / 编号过滤各桶条目（即时生效，只隐藏）…")
        self._filter_edit.setToolTip(
            "输入关键词，六个桶里匹配的条目留下、其余暂时隐藏；\n"
            "清空恢复全部。不影响检测，只帮你找行")
        self._filter_edit.textChanged.connect(
            lambda *_: self._apply_item_filter())
        sh.addWidget(self._filter_edit, 1)
        root.addWidget(srow)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)  # 踩坑⑨
        root.addWidget(self._summary)

        # ---- 桶卡片容器（每次检测整体重建，说明卡不在这里）----
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # 检测前的空态引导（决策 22④：不留白板）
        self._empty_hint = QLabel(
            "还没有检测结果——选好档案后点上方【开始检测】。", self)
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setStyleSheet("color: gray;")
        root.addWidget(self._empty_hint)

        root.addStretch(1)

    def _fill_guide_card(self) -> None:
        """"先读我"卡的内容：名词、机制、分工。纯说明零交互。
        文案原则：只说用户看得见的东西，不写内部术语；"桶"这个词
        出现处都带白话解释。"""
        c = self._guide_card
        c.add_text(
            "六个桶分别是什么：\n"
            "· 桶① 下载未完成——steamcmd 的下载中断了（账本文件里有"
            "半截记录），或盘上留下空目录。修法 = 重新下载。\n"
            "· 桶② 账实不符——账本记着「已下载」，盘上目录却没了"
            "（可能被手动删掉或杀毒软件清理）。修法 = 重新下载。\n"
            "· 桶③ 孤儿目录——盘上有以编号命名的文件夹，账本里却没"
            "这个编号。修法 = 到【mod 库】页点【扫描本地】尝试入账；"
            "确认无用再手动删除。\n"
            "· 桶④ 远端失效——Steam 接口看不见该条目（result=9："
            "作者删除/下架/转私有等）。注意：接口看不见 ≠ 一定下载"
            "不了，建议先试下载再处置（见下）。\n"
            "· 桶⑤ 查询失败与疑似合集——查询失败可能是条目被设为私有"
            "或远端波动，稍后重测；疑似合集是「看着像一个 mod，实际"
            "可能是一整个合集」，需要到【更新检测】页展开确认。\n"
            "· 桶⑥ 多前端环境冲突——无法自动检测，对照桶⑥卡里的"
            "清单自查。")
        c.add_text(
            "· 桶A 远端健康（v2.49）——随【联网深度检测】一并判定，"
            "匿名接口就能查、不需要 API key：标题不符（作者改名/内容"
            "顶替信号，黄）、标题含弃坑关键词（红）、banned=1（被 "
            "Steam 封禁，红）。都是提醒不是损坏——动作是打开工坊"
            "页面看一眼，再决定去留。")
        c.add_text(
            "· 桶B 依赖检测（v2.49）——mod 的「必需物品」清单由带 "
            "key 的官方接口拉取（点【拉取依赖（联网）】，需先在"
            "【Steam API 密钥】页保存免费 key）：清单里编号不在账本"
            " = 缺依赖（红，一键复制编号去【网址批量导入】入库）；"
            "与上次拉取有差 = 依赖变化（黄，作者增删了依赖，以最新"
            "为准）。依赖数据只在拉取时更新，本页其余检测仍只读。")


        c.add_text(
            "两种检测的分工：\n"
            "· 【开始检测】纯离线、毫秒级，只查本地（账本文件与磁盘），"
            "不动任何文件；\n"
            "· 【联网深度检测】逐条重查 Steam 远端实况（后台线程、"
            "礼貌限速），只读不写库——想诊断又不想动数据时用它。")
        c.add_text(
            "什么不是异常：\n"
            "· 「已收录」（已入库、还没下载）是正常排队：本地检测不报"
            "它们——批量下载到【下载命令生成】页勾选执行；逐个管理到"
            "【mod 库】页状态筛选「已收录」。\n"
            "· 已收录里远端已失效的（result=9）不会被漏掉：联网"
            "深度检测后会出现在桶④表格里，分类「已收录·未下载」。")
        c.add_text(
            "桶④失效 mod 怎么办（先试重下，再走三步）：\n"
            "⓪ 右键该行【跳到命令生成页】试着下载一次——result=9 "
            "的条目有时仍能下载成功（接口看不见≠内容服务器没有），"
            "下载成功后到【mod 库】页【扫描本地】入账即可；\n"
            "① 下载失败的话，右键行【打开工坊页面】，看作者有没有留"
            "续作/重传的链接，拿到新编号；\n"
            "② 到【网址批量导入】把新 mod 添加进库；\n"
            "③ 回本页右键旧行【软删除此记录】——记录保留（可到"
            "【mod 库】页右键「恢复」）、盘上文件不动（想清文件去"
            "【清理删除】模块）。\n"
            "表格支持按分类筛选/分组/排序：「已收录·未下载」是"
            "从没下载过就确认失效的条目——但它们恰恰最值得先试"
            "下载（本来就没下过，万一能下回来就赚了）。")
        c.add_text(
            "进阶：「关联替换」（右键菜单里，仅限有失效归档的条目）"
            "可把旧记录的备注、颜色标记、特别关注一键带给一个已在库"
            "的替代条目，归档里记下「旧 → 新」证据链——整理成果多"
            "时省事。菜单里该项是活的 = 这行有失效归档；置灰 = "
            "没有（悬停看原因）。不想用替换、直接走三步也完全没问题。")
        c.add_text(
            "关于 result=9 的一个诚实提醒：只要条目在 Steam 匿名"
            "接口看不见，每次联网深度检测都会把它列回本表——即使"
            "你已确认它实际可用、甚至已成功下载到本地。这是 Steam "
            "接口的判定边界，不是检测错了；本地已有文件的行会在"
            "说明列注明。")
        c.add_text(
            "修完怎么确认：再点一次【开始检测】，对应桶的数字应归零"
            "（软删除的行会变「已删除」分类）；远端数字要重跑"
            "【联网深度检测】才会刷新。")

    # ---------- 对外（MainWindow 调用）----------

    def set_game(self, game: Game | None) -> None:
        self._game = game
        if self._deep_worker is not None:
            # 深检进行中：只更新标签，不动结果区——结果仍归属开始
            # 深检时的档案（渲染按 rep.game_id 取数，不会串）。
            # 与更新检测页同款约定
            if game is None:
                self._game_label.setText(
                    "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            else:
                self._game_label.setText(
                    f"当前游戏：{game.name}（{game.app_id}）")
            return
        self._last_local = None
        self._last_remote = None
        self._last_health = None
        self._last_deps = None
        self._dep_game_id = None

        self._owner_game = None

        self._clear_cards()
        self._summary.setText("")
        # 结果清了：回到空态引导
        self._empty_hint.setVisible(True)
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._detect_btn.setEnabled(False)
            self._deep_btn.setEnabled(False)
            self._dep_btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._detect_btn.setEnabled(True)
            self._deep_btn.setEnabled(True)
            self._dep_btn.setEnabled(True)

    def shutdown(self) -> None:
        """程序退出前的收尾：请求深检线程停止并等 1.5 秒。
        v2.45 修正：不再无限期 wait()——线程可能卡在一次 HTTP 超时/
        重试里数分钟（实测点停止后批边界迟迟不来），同步等待会冻住
        主线程、Windows 判"未响应"。等不到就交 netGate.park 保活 +
        退场守卫随进程带走（线程只读网络不写库，零数据损失）。"""
        if self._deep_worker is not None:
            self._deep_worker.stop()
            if not self._deep_worker.wait(1500):
                netGate.park(self._deep_worker)
                self._deep_worker = None
        if self._dep_worker is not None:
            self._dep_worker.stop()
            if not self._dep_worker.wait(1500):
                netGate.park(self._dep_worker)
            self._dep_worker = None


    # ---------- 本地快检 ----------

    def _start_detect(self) -> None:
        if self._game is None or self._deep_worker is not None:
            return
        game = self._latest_game()  # 先核对下载目录（引擎只读）
        if game is None:
            return
        self._run_local(game)

    def _run_local(self, game: Game) -> exceptionFlow.LocalReport | None:
        """跑本地快检并渲染；失败（acf 损坏）弹窗说明并返回 None。
        本地入口与深检入口共用：深检前没跑过本地时也走这里。"""
        root = self._steamcmd_root()
        if root is None:
            self._log.warn(
                "未设置 steamcmd 程序路径：本次只查账本半边"
                "（盘面部分无法检查，结果页会说明）")
        try:
            report = exceptionFlow.detect_local(self._repo, root, game)
        except ValueError as exc:
            # acf 结构性损坏：引擎口径 = 原样上抛（显式爆炸），这里
            # 接住说人话。修复 = 到设置页核对 steamcmd 程序路径
            self._log.error(f"检测中断：工坊账本文件（acf）损坏：{exc}")
            QMessageBox.critical(
                self, "检测中断",
                f"工坊账本文件（acf）损坏，无法完成检测：\n{exc}\n\n"
                "请到设置页核对 steamcmd 程序路径是否指向正确的"
                " steamcmd 目录。")
            return None
        self._last_local = report
        self._render(report, remote=self._last_remote)
        return report

    def _latest_game(self) -> Game | None:
        """检测前核对下载目录（决策 21：它永远可由 steamcmd 位置推导）。
        steamcmd 挪过位置的话旧目录成死路径——按当前位置重新推导并写回。
        与扫描本地/核验页同款三步：推导 → 写库 → 内存同步。"""
        assert self._game is not None
        effective, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id,
                                   download_dir=effective)
            self._game = self._repo.get_game(self._game.app_id)
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective}")
        return self._game

    def _steamcmd_root(self):
        """设置页的 steamcmd 程序路径 → steamcmd 根目录（可能为 None，
        引擎会立旗标而不是报错——"没查成"要说明原因）。"""
        return steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))

    # ---------- 联网深度检测 ----------

    def _start_deep(self) -> None:
        if self._game is None or self._deep_worker is not None:
            return
        game = self._latest_game()
        if game is None:
            return
        # 本地快检没跑过就先跑一遍（同步毫秒级）：深检结果要与本地
        # 结果同屏重画，且桶④的"账本半边"来自它
        if self._run_local(game) is None:
            return
        # 深检范围：未删除的全部（failed 照查——就是它失效才要盯，
        # 与更新检测页同一口径）
        mods = self._repo.list_mods(game.app_id)
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn("当前档案没有可深检的 mod（已删除的除外）")
            return
        # 联网互斥闸（v2.45）：同一时刻只允许一个联网入口在查——
        # 两个客户端一起卡进超时重试时，用户看到的是两个都"还在检测"
        busy = netGate.try_acquire("联网深度检测")
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再跑深度检测。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(f"深度检测未开始：{busy} 正在使用联网查询")
            return

        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_deep_running(True)
        self._deep_worker = _DeepWorker(
            ids,
            interval_ms=self._settings.get_int(
                "api_request_interval_ms", 200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._deep_worker.batch_done.connect(self._progress.setValue)
        self._deep_worker.succeeded.connect(self._on_deep_succeeded)
        self._deep_worker.failed.connect(self._on_deep_failed)
        self._deep_worker.stopped.connect(self._on_deep_stopped)
        # 线程跑完（无论哪种结局）统一由 finished 收尾销毁。
        # 不能在 succeeded/failed/stopped 的处理函数里把
        # self._deep_worker 置 None——那时线程可能还没完全退出，
        # Python 侧提前销毁会让 Qt 直接终止进程（闪退 0xC0000409）
        self._deep_worker.finished.connect(self._on_deep_finished)
        self._deep_worker.start()
        self._log.info(
            f"联网深度检测开始：{len(ids)} 个 mod（只读不写库）…")

    def _stop_deep(self) -> None:
        if self._deep_worker is not None:
            self._deep_worker.stop()
            self._deep_stop_btn.setEnabled(False)
            # 立刻给反馈：点停止 = 取消事件已置位（等待/重试立即中断），
            # 只剩在途的那一个请求要等——没有反馈时"在等待"和"卡死"
            # 长得一模一样（v2.45 实测教训）
            self._summary.setText(
                "已请求停止：等待与重试已中断，最多再等一次在途请求收场"
                "（网络不畅时约 10~40 秒）——退出软件不必等它，可直接关。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止联网深度检测：等待与重试已中断，"
                           "等在途请求收场")

    def _set_deep_running(self, running: bool) -> None:
        self._detect_btn.setEnabled(not running and self._game is not None)
        self._deep_btn.setEnabled(not running and self._game is not None)
        self._dep_btn.setEnabled(not running and self._game is not None)

        self._deep_stop_btn.setEnabled(running)
        self._progress.setVisible(running)
    def _on_deep_succeeded(self, entries: list) -> None:
        self._set_deep_running(False)
        if self._last_local is None:
            return  # 理论到不了：深检前必先本地快检；防御一行
        remote = exceptionFlow.classify_entries(entries)
        self._last_remote = remote
        # 桶A（v2.49）：同一批查询顺带做健康判定——标题不符/弃坑/
        # banned，数据全在匿名接口返回里，不增加任何请求数
        titles = {m.mod_id: (m.title or "")
                  for m in self._repo.list_mods(self._last_local.game_id)}
        health = exceptionFlow.classify_health(entries, titles)
        self._last_health = health
        self._render(self._last_local, remote=remote)
        self._log.ok(
            f"联网深度检测完成：确认失效 {len(remote.invalid)}，"
            f"查询失败 {len(remote.query_failed)}，"
            f"疑似合集 {len(remote.suspected_collection)}"
            + (f"，健康异常 {health.total}" if health.total else ""))
        if health.missing_banned_field:
            self._log.warn(
                "当前 Steam 客户端封装未携带 banned 字段：banned 红字"
                "检测本次不可用（Steam 原始响应里有它）——把 "
                "core/steamApiClient.py 发给开发者补一行即可")


    def _on_deep_failed(self, message: str) -> None:
        self._set_deep_running(False)
        # 不弹窗（quiet 口径）：网络失败常见，红字汇总 + 日志足够，
        # 本地快检的卡片结果仍然有效、不受影响
        self._summary.setText(
            f"联网深度检测失败：{message}\n（本地快检结果仍有效，"
            "见下方卡片；稍后可重试。可以参考watt toolkit的连通性测试，若一直不通过，可以尝试关闭watt toolkit，然后尝试。下载mod同理。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"联网深度检测失败：{message}")

    def _on_deep_stopped(self) -> None:
        """用户手动停止：正常收场，不弹窗不标红（与更新检测页同款）。"""
        self._set_deep_running(False)
        self._summary.setText(
            "已停止联网深度检测，本次结果未使用（本地快检结果仍有效）。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("联网深度检测已手动停止")

    def _on_deep_finished(self) -> None:
        """深检线程跑完的统一收尾（理由见 _start_deep 里的注释，
        照更新检测页 _on_worker_finished 同款）。"""
        w = self._deep_worker
        self._deep_worker = None
        if w is not None:
            w.wait()
        # 联网互斥闸在此归还：finished 无论成功/失败/停止都必发，
        # 释放放这里一次就够，漏不掉
        netGate.release("联网深度检测")
    # ---------- 依赖拉取（桶B，v2.49）----------
    def _start_dep(self) -> None:
        """【拉取依赖（联网）】：keyed 接口批量拉必需物品清单 →
        引擎判定（缺依赖/依赖变化）→ 依赖边入账。与深检同一批
        纪律：线程只联网、库读写全在 GUI 线程、netGate 互斥、
        停止 = 什么都没发生（入账只发生在成功回调里）。"""
        if self._game is None or self._dep_worker is not None:
            return
        game = self._latest_game()
        if game is None:
            return
        key = (self._settings.get("steam_api_key") or "").strip()
        if not key:
            QMessageBox.information(
                self, "拉取依赖",
                "依赖检测需要 Steam Web API key（免费）：\n到【基础"
                "功能 → Steam API 密钥】页按四步注册并保存，再回来"
                "拉取。")
            return
        # 与深检同款：先跑本地快检（毫秒级），渲染才有本地半边
        if self._run_local(game) is None:
            return
        mods = self._repo.list_mods(game.app_id)
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn("当前档案没有可拉取依赖的 mod（已删除的除外）")
            return
        # 拉取前基线（GUI 线程读库）：旧边 + 是否首拉 + 账本全集。
        # 判定用的三样数据在这里取齐，线程回来直接算
        self._dep_lib_ids = {m.mod_id for m in mods}
        self._dep_baseline = {m.mod_id: self._repo.list_dependencies(m.mod_id)
                              for m in mods}
        self._dep_first_fetch = self._repo.latest_dependency_fetch() is None
        self._dep_game_id = game.app_id
        busy = netGate.try_acquire("依赖拉取")
        if busy is not None:
            self._summary.setText(
                f"另有联网任务在进行（{busy}）：等它结束后再拉取。")
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._log.warn(f"依赖拉取未开始：{busy} 正在使用联网查询")
            return
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_dep_running(True)
        self._dep_worker = _DepWorker(
            ids, key=key,
            interval_ms=self._settings.get_int("api_request_interval_ms", 200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._dep_worker.batch_done.connect(self._progress.setValue)
        self._dep_worker.succeeded.connect(self._on_dep_succeeded)
        self._dep_worker.failed.connect(self._on_dep_failed)
        self._dep_worker.stopped.connect(self._on_dep_stopped)
        # 线程收尾必须 finished 里先取引用置 None 再 wait()——
        # 深检踩过的 0xC0000409 闪退坑，同一份纪律
        self._dep_worker.finished.connect(self._on_dep_finished)
        self._dep_worker.start()
        self._log.info(f"依赖拉取开始：{len(ids)} 个 mod（keyed 接口，"
                       "含必需物品清单）…")

    def _stop_dep(self) -> None:
        if self._dep_worker is not None:
            self._dep_worker.stop()
            self._dep_stop_btn.setEnabled(False)
            self._summary.setText(
                "已请求停止依赖拉取：等待与重试已中断，最多再等一次"
                "在途请求收场——本次结果未使用、依赖表未动。")
            self._summary.setStyleSheet("color: gray;")
            self._log.info("已请求停止依赖拉取")

    def _set_dep_running(self, running: bool) -> None:
        self._detect_btn.setEnabled(not running and self._game is not None)
        self._deep_btn.setEnabled(not running and self._game is not None)
        self._dep_btn.setEnabled(not running and self._game is not None)
        self._dep_stop_btn.setEnabled(running)
        self._progress.setVisible(running)

    def _on_dep_succeeded(self, entries: list) -> None:
        self._set_dep_running(False)
        if self._last_local is None:
            return
        # ① 判定（纯函数；此刻库还没动过——基线就是拉取前那份）
        deps = exceptionFlow.classify_dependencies(
            entries, self._dep_lib_ids, self._dep_baseline,
            first_fetch=self._dep_first_fetch)
        # ② 依赖边入账（本页唯一的检测性写库动作）：children 键缺席
        #    的条目不写——没拿到的东西绝不冒充"无依赖"。逐条原子
        #    （不嵌套事务），中途失败下次拉取全量覆盖
        written = 0
        for e in entries:
            if e.get("children") is None:
                continue
            try:
                self._repo.replace_dependencies(e["mod_id"], e["children"])
                written += 1
            except ValueError as exc:
                self._log.error(
                    f"依赖入账失败（mod {e.get('mod_id')}）：{exc}")
        self._last_deps = deps
        self._render(self._last_local, remote=self._last_remote)
        parts = [f"{written} 个 mod 清单入账"]
        if deps.missing:
            n_edges = sum(len(v) for v in deps.missing.values())
            parts.append(f"缺依赖 {len(deps.missing)} 个 mod"
                         f"（共 {n_edges} 条）")
        if deps.changed:
            parts.append(f"依赖变化 {len(deps.changed)}")
        if deps.undetermined:
            parts.append(f"未判定 {len(deps.undetermined)}")
        self._log.ok("依赖拉取完成：" + "，".join(parts))

    def _on_dep_failed(self, message: str) -> None:
        self._set_dep_running(False)
        self._summary.setText(
            f"依赖拉取失败：{message}\n（依赖表未动，稍后可重试。）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"依赖拉取失败：{message}")

    def _on_dep_stopped(self) -> None:
        self._set_dep_running(False)
        self._summary.setText("已停止依赖拉取，本次结果未使用（依赖表未动）。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("依赖拉取已手动停止")

    def _on_dep_finished(self) -> None:
        """依赖拉取线程统一收尾（与 _on_deep_finished 同一份纪律）。"""
        w = self._dep_worker
        self._dep_worker = None
        if w is not None:
            w.wait()
        netGate.release("依赖拉取")

    def _copy_missing(self, ids: list[int]) -> None:
        QApplication.clipboard().setText("\n".join(str(i) for i in ids))
        self._log.ok(f"已复制 {len(ids)} 个缺失依赖编号（去【网址批量"
                     "导入】粘贴添加）")

    def _refresh_after_dispose(self) -> None:
        """桶④处置（软删除/替换）后的复检刷新。
        深检进行中不自动复检（_start_detect 会静默拒绝）——
        深检完成后 _render 会重建表格，行会按最新账本状态显示；
        不在深检中就立即跑本地快检（毫秒级）。"""
        if self._deep_worker is not None:
            self._log.info(
                "联网深度检测进行中：暂不自动复检，"
                "深检完成后表格会按最新账本状态刷新")
            return
        self._start_detect()

    # ---------- 渲染 ----------

    def _render(self, rep: exceptionFlow.LocalReport,
                remote: exceptionFlow.RemoteFindings | None = None
                ) -> None:
        """把检测报告画成桶卡片。

        remote=None：只渲染本地快检结果（桶④仅账本半边、桶⑤待深检）。
        remote 给定：桶④合并本地归档与联网确认（含"恢复正常"提示），
        桶⑤实装，汇总行加深检数字。

        收展纪律：每桶建完按结果重设——有异常自动摊开、无异常/
        待检测自动收起（核验页"结果优先"同一口径）；用户手工收展
        在两次检测之间有效。flags 横幅与说明性内容默认收起/按需。

        渲染一律按 rep.game_id 取数（不是当前档案）——深检进行中
        切过档案也不会串。
        """
        self._clear_cards()
        self._empty_hint.setVisible(False)

        owner = self._repo.get_game(rep.game_id)
        self._owner_game = owner

        owner_name = owner.name if owner is not None \
            else f"档案 {rep.game_id}"

        # 库内条目一览：编号 → 记录。列表行显示标题、行菜单取工坊
        # 网址都从这里查（一次取齐，行循环里只查字典不查库）
        self._mods_by_id = {
            m.mod_id: m for m in self._repo.list_mods(rep.game_id)}
        lib_ids = set(self._mods_by_id)

        # 各桶问题清单先算好（汇总行要用总数）
        b1_ids = sorted(set(rep.interrupted) | set(rep.empty_dirs))
        # 桶①逐条情况说明：中断与空目录的来历不同，列表里分开说
        b1_notes = {mid: "acf 记下载中断" for mid in rep.interrupted}
        for mid in rep.empty_dirs:
            b1_notes[mid] = "盘上空目录（断点残留）"
        disk_ok = not (rep.steamcmd_missing or rep.dead_root)

        # ---- 旗标横幅：本次没能检查的部分 ----
        flags: list[str] = []
        if rep.steamcmd_missing:
            flags.append("未配置 steamcmd 程序路径——盘面三桶（①②③）"
                         "无法检测；到设置页填写后重测")
        if rep.dead_root:
            flags.append("下载目录不存在——盘面三桶（①②③）无法检测；"
                         "请检查设置页 steamcmd 路径与该档案是否匹配")
        if rep.acf_missing:
            flags.append("找不到工坊账本文件（acf）——桶①的「下载中断」"
                         "半边无法检测（该游戏可能从未用本机 steamcmd "
                         "下载过）")
        if flags:
            card = self._card(
                "flags", "本次未能检查的部分",
                tip="有些前提没满足，一部分检查没跑成——原因与"
                    "解决办法看下面几行。",
                attention=True)
            card.set_status(f"{len(flags)} 项前提不满足", _C_WARN)
            for f in flags:
                card.add_text("· " + f, _C_WARN)
            card.set_expanded(True)

        # ---- 桶① 下载未完成 ----
        card = self._card(
            "b1", "桶① 下载未完成",
            tip="steamcmd 下载中断的条目与盘上空目录。中断条目没有"
                "入账（不影响账本），空目录是断点残留；修法都是"
                "重新下载。",
            attention=bool(b1_ids) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not b1_ids:
            card.set_status("无异常", _C_OK)
            if rep.acf_missing:
                card.add_text("注：下载中断半边未检查（无 acf，见横幅）")
            card.set_expanded(False)
        else:
            card.set_status(
                f"{len(b1_ids)} 处（下载中断 {len(rep.interrupted)}"
                f" · 空目录 {len(rep.empty_dirs)}）", _C_WARN)
            card.add_text(
                "修法 = 重新下载：中断条目未入账（不影响账本），空目录"
                "是断点残留；下载完成后到【mod 库】页点【扫描本地】入账。")
            # 已入库的逐条列出来（带标题）；右键可单条跳命令页
            b1_in = [i for i in b1_ids if i in lib_ids]
            b1_out = [i for i in b1_ids if i not in lib_ids]
            if b1_in:
                self._add_id_list(
                    card,
                    [(mid, self._title_of(mid), b1_notes.get(mid, ""))
                     for mid in b1_in],
                    allow_cmd=True)
            if b1_out:
                card.add_text(
                    f"另有 {len(b1_out)} 个编号不在库中（新下载中途被打断"
                    "的）：想下载它们先到【网址批量导入】添加，再到命令"
                    "生成页勾选。", _C_WARN)
            if b1_in:
                card.add_button(
                    f"生成重下命令（{len(b1_in)} 条，可在命令页增减）…",
                    "跳到【下载命令生成】页并勾选这些编号；"
                    "不想下的取消勾选即可，复制前还能再核对预览",
                    lambda _=False, ids=list(b1_in):
                    self._emit_command_gen(ids))
            card.set_expanded(True)

        # ---- 桶② 账实不符 ----
        card = self._card(
            "b2", "桶② 账实不符（账本记已下载、盘上没有）",
            tip="账本里记着已下载，下载目录里却找不到——可能被手动"
                "删除或杀毒软件清理。快捷修法 = 重新下载（steamcmd "
                "会自动补齐）。",
            attention=bool(rep.missing) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not rep.missing:
            card.set_status("无异常", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(f"{len(rep.missing)} 个", _C_WARN)
            card.add_text(
                "目录整个没了（可能被手动删除/杀毒清理）。快捷修法 = "
                "重新下载；「校验重下 validate」「标记为已移除」等完整"
                "三选在【账实核验】页对应行里。")
            self._add_id_list(
                card,
                [(mid, self._title_of(mid), "目录不存在")
                 for mid in rep.missing],
                allow_cmd=True)
            card.add_button(
                f"生成重下命令（{len(rep.missing)} 条）…",
                "跳到【下载命令生成】页并勾选这些编号",
                lambda _=False, ids=list(rep.missing):
                self._emit_command_gen(ids))
            card.set_expanded(True)

        # ---- 桶③ 孤儿目录 ----
        card = self._card(
            "b3", "桶③ 孤儿目录（盘上有、账本不认识）",
            tip="下载目录里有以编号命名的文件夹，账本里却没有这个"
                "编号——可能是手动放进去的，或别的工具下的。修法 = "
                "到【mod 库】页点【扫描本地】尝试入账。",
            attention=bool(rep.orphans) and disk_ok)
        if not disk_ok:
            card.set_status("未能检查（原因见横幅）", _C_MUTED)
            card.set_expanded(False)
        elif not rep.orphans:
            card.set_status("无异常", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(f"{len(rep.orphans)} 个", _C_WARN)
            card.add_text(
                "到【mod 库】页点【扫描本地】尝试入账。注意：扫描只认"
                "账本文件里的完好条目——损坏/中断的下载和纯手动放进去"
                "的文件扫不进来，确认无用再手动删除。")
            # 孤儿不在账本里：没有标题可查，跳命令页也没用（命令页
            # 按账本勾选）——行菜单只给"打开工坊页面 / 复制编号"
            self._add_id_list(
                card,
                [(mid, "（未入账）", "账本没有这个编号")
                 for mid in rep.orphans],
                allow_cmd=False)
            card.set_expanded(True)

        # ---- 桶④ 远端失效：失效 mod 管理台（表格）----
        # 本地账本半边 ∪ 联网确认的失效；联网查过才谈"恢复正常"
        remote_invalid = list(remote.invalid) if remote else []
        all_invalid = sorted(set(rep.failed_ids) | set(remote_invalid))
        newly = [i for i in remote_invalid if i not in set(rep.failed_ids)]
        recovered = sorted(
            set(rep.failed_ids) & (set(remote.ok) if remote else set()))
        card = self._card(
            "b4",
            "桶④ 远端失效（result=9：删除/下架/转私有等）",
            tip="Steam 匿名接口看不见这个条目（result=9）。注意："
                "接口看不见 ≠ 一定下载不了——实测有的条目 steamcmd "
                "仍能下载成功。建议先右键行【跳到命令生成页】试下"
                "一次，下不回来再走三步（见引导文字）。进阶："
                "「关联替换」（右键菜单，仅限有失效归档的行）。",
            attention=bool(all_invalid))
        if not all_invalid:
            if remote is None:
                card.set_status("无（按账本记录）", _C_OK)
                card.add_text(
                    "远端实况重查（区分真失效/暂时查不到）点上方"
                    "【联网深度检测】。")
            else:
                card.set_status(
                    f"无异常（联网重查 {len(remote.ok) + len(remote.invalid) + len(remote.query_failed)} 个条目）",
                    _C_OK)
            card.set_expanded(False)
        else:
            # -- 分类：每条失效 mod 归入哪一类、允许哪些动作 --
            # 判定顺序：已删除（软删除过的闭环）→ 已替换（归档里有
            # 替代记录）→ 已收录·未下载（tracked）→ 归档失效（兜底，
            # 含"已下载过、本次联网刚确认、还没有归档"的行）。
            # 无归档的行「关联替换」置灰（_fill_row_menu）；
            # 跳命令页试下载对红黄都开放——result=9 有时仍能下载
            # 成功（v2.45 实测口径，见文件头 philosophy）
            rec_map = {r.mod_id: r for r in rep.failed_records}
            rows: list[dict] = []
            for mid in all_invalid:
                rec = rec_map.get(mid)
                m = self._mods_by_id.get(mid)
                st = m.status if m is not None else None
                replaced = rec.replaced_by if rec else None
                if st == "deleted":
                    cat, can_repl, can_del = "已删除", False, False
                elif replaced:
                    cat, can_repl, can_del = "已替换", False, False
                elif st == "tracked":
                    cat, can_repl, can_del = "已收录·未下载", False, True
                elif m is None:
                    # 账本条目已不存在（彻底清账后 mods 行物理删除，
                    # failed_mods 证据行按决策刻意存活）——终结历史，
                    # 不再给任何处置入口（软删除/替换/试下载全关）
                    cat, can_repl, can_del = "已清账", False, False
                else:
                    cat = "归档失效"
                    can_repl, can_del = rec is not None, True

                # -- 说明列：每类的"说实话"文案 --
                if st == "deleted":
                    note = "已软删除——恢复到【mod 库】页右键「恢复」"
                elif replaced:
                    note = f"已替换 → mod {replaced}"

                elif m is None:
                    note = ("已彻底清账（在【清理与删除】执行）：账本条目"
                            "与盘上文件均已清除；本行为失效归档的历史证据，"
                            "默认不显示，状态筛「已清账」可查")


                elif rec and rec.reason:
                    note = rec.reason
                    # 建档后 status 变 failed，下面"本地已有文件"分支不再
                    # 命中——决策 85 的边界提示续在这里，先读我承诺不落空
                    if m is not None and m.local_size:
                        note += "（本地文件仍在，照常用）"
                elif st == "tracked":
                    note = ("result=9 确认（未下载过；偶尔仍能下载，"
                            "建议先试）"
                            if mid in remote_invalid
                            else "远端已失效（从未下载过）")
                elif st == "downloaded" and mid in remote_invalid:
                    note = ("本地已有文件——result=9 有时仍能下载成功，"
                            "已在本地就照常用（每次深检仍会列在本表，"
                            "属 Steam 接口判定边界）")
                elif mid in remote_invalid:
                    note = "本次联网确认 result=9"
                else:
                    note = "（无归档：仅有 failed 状态）"
                rows.append({"mid": mid, "title": self._title_of(mid),
                             "cat": cat, "note": note,
                             "can_replace": can_repl,
                             "can_delete": can_del,
                             "replaced_by": replaced})
            self._b4_rows = rows
            # 已清账历史不算"值得看"：只剩历史时桶④不亮红、不强制
            # 展开（「只看有问题的桶」也不藏它——历史不是问题）
            card.has_attention = any(r["cat"] != "已清账" for r in rows)

            # 状态行：活账数 + 分类计数。已清账是终结态历史归档，
            # 不算异常、不进红色计数（v2.48 决策 104）——红字只属于
            # 还有待处置活账的情形；只剩历史时整行灰字收场
            n_cat = {c: 0 for c in _CAT_ORDER}
            for r in rows:
                n_cat[r["cat"]] += 1
            n_arch = n_cat["已清账"]
            n_live = len(all_invalid) - n_arch
            live = [c for c in _CAT_ORDER if c != "已清账"]
            stat = " · ".join(f"{c} {n_cat[c]}" for c in live if n_cat[c])
            extra = f"（含联网新确认 {len(newly)} 个）" if newly else ""
            if n_live:
                if n_arch:
                    stat += (f" · 已清账 {n_arch}（历史归档，默认不显示，"
                             "状态筛「已清账」可查）")
                card.set_status(f"{n_live} 个{extra}：{stat}", _C_FAIL)
            else:
                card.set_status(
                    f"无待处置异常 · 已清账 {n_arch}"
                    "（彻底清账的历史归档，默认不显示，状态筛「已清账」可查）",
                    _C_MUTED)

            # -- 引导文字：先试重下（⓪）→ 三步主路径 → 进阶一句话 --
            card.add_text(
                "result=9 = Steam 接口看不见条目（作者删除/下架/"
                "转私有等）——接口看不见 ≠ 一定下载不了：实测有的"
                "result=9 条目 steamcmd 仍能下载成功，有的报 File "
                "Not Found（真没了）。建议先右键行【跳到命令生成页】"
                "试下载一次（成本极低）。下不回来再走三步：① 右键行"
                "【打开工坊页面】找作者的续作/重传 → ② 到【网址批量"
                "导入】添加新编号 → ③ 回本行右键【软删除此记录】"
                "（记录保留可恢复，盘上文件不动）。「关联替换」（进阶）"
                "只对有失效归档的行可用——菜单里该项是活的即有归档、"
                "置灰即没有（悬停看原因）。表格可按分类筛选/分组/排序。\n"
                "处置闭环：右键软删除的行变「已删除」（记录保留，可到"
                "【mod 库】页恢复）；在【清理与删除】彻底清账的行变"
                "「已清账」——账本条目与盘上文件都已清除，不会再出现在"
                "任何待办里，本表默认也不再显示（状态筛「已清账」可查"
                "历史归档）。")

            # -- 归档缺口提示（v2.45 用户三次在此困惑的收口）：红行没归档
            # = 关联替换置灰。建档唯一出口在【更新检测】页（决策 86），
            # 本页深检是纯只读承诺、永不建档——把解锁路径送到眼前
            n_no_arch = sum(1 for r in rows
                            if r["cat"] == "归档失效" and not r["can_replace"])
            if n_no_arch:
                card.add_text(
                    f"其中 {n_no_arch} 行（红）还没有失效归档，「关联替换」"
                    "暂为置灰：归档只由【更新检测】建立（本页深度检测纯只读、"
                    "不建档）。到【更新检测】页点【开始检测】，汇总行出现"
                    "「result=9 建档 N」即建档完成，回本页复检后解锁。"
                    "黄行（已收录·未下载）的置灰是设计如此：从未下载过、"
                    "没有可转移的整理内容，刻意不建档。",
                    _C_WARN)

            # -- 管理台本体：工具行 + 表格 --
            self._build_b4_card(card)
            if recovered:
                card.add_text(
                    "本地记为失效、本次远端查询却正常（未列入上表）："
                    + _ids_text(recovered)
                    + "（可能是作者恢复了条目——要不要继续用自行判断；"
                      "更新检测会照常盯它们）",
                    _C_WARN)
            card.add_text(
                "处置后点【开始检测】复检（软删除的行会变「已删除」）；"
                "远端数字要重跑【联网深度检测】才会刷新。已清账的历史行"
                "不受复检影响——账本条目已删，本地与远端都不会再查到"
                "它们，属预期不是刷新失灵。")
            card.set_expanded(card.has_attention)

        # ---- 桶⑤ 查询失败 / 疑似合集 ----
        card = self._card(
            "b5", "桶⑤ 查询失败 / 疑似合集",
            tip="查询失败 = Steam 接口返回了异常结果码（可能私有/"
                "地区限制/远端波动），稍后重测；疑似合集 = 看着像"
                "一个 mod、实际可能是一整个合集，要展开确认后才能"
                "入库。",
            attention=bool(remote is not None
                           and (remote.query_failed
                                or remote.suspected_collection)))
        if remote is None:
            card.set_status("待联网深度检测", _C_MUTED)
            card.add_text(
                "本桶需要重新查询远端才能判定，点上方【联网深度检测】。")
            card.set_expanded(False)
        else:
            n5 = len(remote.query_failed) + len(remote.suspected_collection)
            if n5 == 0 and not remote.malformed:
                card.set_status("无异常（本次联网重查全部正常）", _C_OK)
                card.set_expanded(False)
            else:
                card.set_status(
                    f"查询失败 {len(remote.query_failed)}"
                    f" · 疑似合集 {len(remote.suspected_collection)}",
                    _C_WARN)
                if remote.query_failed:
                    card.add_text(
                        "查询失败（接口返回异常结果码）——可能是条目被"
                        "设为私有/地区限制/远端波动——稍后重测；确认"
                        "真失效的会归入桶④。")
                    self._add_id_list(
                        card,
                        [(mid, self._title_of(mid), "查询失败") for mid in remote.query_failed],
                        allow_cmd=False, allow_dispose=True)

                if remote.suspected_collection:
                    card.add_text(
                        "疑似合集（远端缺少文件大小）——确认与展开入库"
                        "到【更新检测】页进行（对应行有【展开合集…】"
                        "按钮）——本页是纯读诊断，不做登记。")
                    self._add_id_list(
                        card,
                        [(mid, self._title_of(mid), "疑似合集")
                         for mid in remote.suspected_collection],
                        allow_cmd=False)
                if remote.malformed:
                    card.add_text(
                        f"另有 {len(remote.malformed)} 条响应数据读不动，"
                        "已忽略（极少见，多为接口异常波动）。", _C_WARN)
                card.set_expanded(True)
        # ---- 桶A 远端健康（v2.49）：标题不符 / 弃坑 / banned ----
        # 数据源 = 匿名接口（无需 API key），随深检顺带判定；
        # health=None = 本次会话还没跑过联网深检
        health = self._last_health
        n_health = health.total if health is not None else 0
        card = self._card(
            "ba", "桶A 远端健康：标题不符 / 弃坑 / banned",
            tip="与账本记录对不上的三类远端信号（匿名接口可判，无需 "
                "API key）：标题不符 = 作者改名或上传内容被顶替的信号"
                "（黄）；标题含弃坑关键词 = 作者明示不再维护（红）；"
                "banned=1 = 被 Steam 封禁（红）。都是提醒不是损坏。",
            attention=bool(health is not None and n_health > 0))
        if health is None:
            card.set_status("待联网深度检测", _C_MUTED)
            card.add_text("本桶随【联网深度检测】一并判定（同一批查询"
                          "顺带完成，不增加请求数）。")
            card.set_expanded(False)
        elif n_health == 0:
            card.set_status("无异常（远端标题与账本一致，无弃坑/banned "
                            "信号）", _C_OK)
            card.set_expanded(False)
        else:
            card.set_status(
                f"标题不符 {len(health.title_mismatch)}"
                f" · 弃坑 {len(health.abandoned)}"
                f" · banned {len(health.banned)}", _C_WARN)
            if health.title_mismatch:
                card.add_text(
                    "标题不符（本地标题 → 远端标题）——作者改名或上传"
                    "内容被顶替的信号：打开工坊页面对照一眼，内容确实"
                    "换了再决定去留。版本号差异已自动忽略。")
                self._add_id_list(
                    card,
                    [(mid, local_t, f"远端标题：{remote_t}")
                     for mid, local_t, remote_t in health.title_mismatch],
                    allow_cmd=False)
            if health.abandoned:
                card.add_text("标题含弃坑关键词——作者明示不再维护："
                              "还能用就继续用，出问题再找替代。")
                self._add_id_list(
                    card,
                    [(mid, self._title_of(mid), "标题含弃坑关键词")
                     for mid in health.abandoned],
                    allow_cmd=False)
            if health.banned:
                card.add_text("banned=1——被 Steam 封禁的条目：通常"
                              "不可再下载，处置参考桶④三步。")
                self._add_id_list(
                    card,
                    [(mid, self._title_of(mid), "banned=1")
                     for mid in health.banned],
                    allow_cmd=False)
            if health.missing_banned_field:
                card.add_text("注：客户端封装未携带 banned 字段，本次 "
                              "banned 检测不可用（见运行日志）。",
                              _C_WARN)
            card.set_expanded(True)
        # ---- 桶B 依赖检测（v2.49）：缺依赖 / 依赖变化 ----
        # 数据源 = keyed GetDetails（需要 API key）；deps=None =
        # 本次会话没拉取过（或结果归属别的档案）
        deps = (self._last_deps
                if self._last_deps is not None
                   and rep.game_id == self._dep_game_id else None)
        deps_bad = (0 if deps is None else len(deps.missing)
                                           + len(deps.changed) + len(deps.undetermined))
        last_fetch = self._repo.latest_dependency_fetch()
        card = self._card(
            "bb", "桶B 依赖检测：缺依赖 / 依赖变化",
            tip="必需物品清单由带 key 的官方接口拉取（【拉取依赖】"
                "按钮）：清单里编号不在账本 = 缺依赖（红，一键复制"
                "编号去【网址批量导入】添加）；与上次拉取有差 = 依赖"
                "变化（黄，作者增删了依赖，以最新为准）。需要 "
                "Steam API 密钥。",
            attention=bool(deps is not None and deps_bad > 0))
        if deps is None:
            card.set_status(
                "待拉取" + (f"（上次拉取：{_fmt_ts(last_fetch)}）"
                            if last_fetch else "（从未拉取）"), _C_MUTED)
            card.add_text(
                "点上方【拉取依赖（联网）】：带 key 批量查询全部未"
                "删除 mod 的必需物品清单，缺什么、变了什么一次说清。"
                "需要先在【Steam API 密钥】页保存 key。")
            card.set_expanded(False)
        elif deps_bad == 0:
            card.set_status(
                f"无异常（解析 {deps.fetched} 条：无依赖 "
                f"{len(deps.childless)} 条，其余依赖全部在账本）", _C_OK)
            card.set_expanded(False)
        else:
            stat = (f"缺依赖 {len(deps.missing)} · "
                    f"依赖变化 {len(deps.changed)}")
            if deps.undetermined:
                stat += f" · 未判定 {len(deps.undetermined)}"
            card.set_status(stat, _C_WARN)
            if deps.missing:
                card.add_text(
                    "缺依赖（必需物品编号不在账本——下载了也跑不起"
                    "来）：点按钮复制全部缺失编号，到【网址批量导入】"
                    "添加后再下载；右键单条可打开工坊页面看是什么。",
                    _C_FAIL)
                miss_rows = [
                    (mid, self._title_of(mid),
                     f"缺 {len(lack)} 个必需物品：{_ids_text(lack)}")
                    for mid, lack in sorted(deps.missing.items())]
                self._add_id_list(card, miss_rows, allow_cmd=False)
                all_missing = sorted({i for lack in deps.missing.values()
                                      for i in lack})
                card.add_button(
                    f"复制全部缺失编号（{len(all_missing)} 个）…",
                    "复制到剪贴板（每行一个）：到【网址批量导入】"
                    "粘贴即可全部入库，之后正常下载",
                    lambda _=False, ids=all_missing: self._copy_missing(ids))
            if deps.changed:
                card.add_text(
                    "依赖变化（与上次拉取相比，作者增删了必需物品）"
                    "——清单已按本次为准入账，对照看即可：", _C_WARN)
                chg_rows = []
                for mid, old, new in deps.changed:
                    added = sorted(set(new) - set(old))
                    removed = sorted(set(old) - set(new))
                    note = (f"{len(old)} → {len(new)}"
                            + (f"，新增 {_ids_text(added)}" if added else "")
                            + (f"，移除 {_ids_text(removed)}" if removed else ""))
                    chg_rows.append((mid, self._title_of(mid), note))
                self._add_id_list(card, chg_rows, allow_cmd=False)
            if deps.undetermined:
                card.add_text(
                    f"未判定 {len(deps.undetermined)} 条（响应里没有 "
                    "children 键，接口行为异常）——这些条目的依赖本次"
                    "没拿到，也绝不冒充「无依赖」入账。重拉一次；持续"
                    "出现把日志发给开发者。"
                    + _ids_text(deps.undetermined), _C_WARN)
            if deps.skipped:
                card.add_text(
                    f"另有 {deps.skipped} 条远端失效/查询失败，无从判"
                    "依赖（桶④⑤已列）。")
            card.add_text(
                "依赖清单以最近一次拉取为准（重拉全量刷新）；上次"
                "拉取：" + (_fmt_ts(last_fetch) if last_fetch else
                           "（本次是第一次）")
                + "。依赖指向的条目虽在账本但已失效/删除的，去桶④"
                  "处置——本桶不重复报。")
            card.set_expanded(True)

        # ---- 附加发现：非数字内容 ----
        if rep.non_numeric:
            card = self._card(
                "nn", "附加发现：非数字内容（编号之外的目录/文件）",
                tip="下载目录树里只应存在以编号命名的文件夹；其他"
                    "来路的目录/文件列在这里。本工具不代删：人工确认"
                    "后自行处理。",
                attention=True)
            card.set_status(f"{len(rep.non_numeric)} 项", _C_WARN)
            for name in rep.non_numeric[:_ID_LIMIT]:
                card.add_text("· " + name)
            if len(rep.non_numeric) > _ID_LIMIT:
                card.add_text(
                    f"…以及另外 {len(rep.non_numeric) - _ID_LIMIT} 项"
                    "（控制台日志不重复列，见上方汇总）")
            card.set_expanded(True)

        # ---- 桶⑥ 多前端环境冲突（静态自查清单，文案单源在引擎）----
        card = self._card(
            "b6", "桶⑥ 多前端环境冲突（无法自动检测，自查）",
            tip="同一个 steamcmd 目录可能被多个工具指挥（如 RimSort），"
                "删掉的 mod 可能被别的工具下回来。无法程序化检测，"
                "对照卡里的清单自查。",
            attention=False)
        card.set_status("自查清单", _C_MUTED)
        card.add_text(exceptionFlow.MULTIFRONTEND_NOTE)
        card.set_expanded(False)

        # ---- 汇总行 ----
        # 已清账的归档行（账本条目已删 = 不在 lib_ids）不算"本地发现"——
        # 它们是历史证据，不是异常（v2.48 决策 104）
        n_purged_arch = sum(1 for mid in rep.failed_ids if mid not in lib_ids)
        local_total = (len(b1_ids) + len(rep.missing) + len(rep.orphans)
                       + len(rep.failed_ids) - n_purged_arch
                       + len(rep.non_numeric))

        if remote is not None:
            remote_total = (len(remote.invalid) + len(remote.query_failed)
                            + len(remote.suspected_collection))
        else:
            remote_total = None
        # v2.10.1 修正：仅本地检测时 remote_total 是 None，原写法
        # `remote_total == 0` 恒不成立 → 全净绿分支永不触发，跌进
        # "部分未能检查"分支、凭空声称有横幅（实测干净库误报）。
        # `not (remote_total or 0)` 把"深检没跑(None)"和"跑了零发现"
        # 一视同仁
        if local_total == 0 and not flags and not (remote_total or 0) \
                and n_health == 0 and deps_bad == 0:
            tail = "（含联网深度检测）" if remote is not None else ""
            self._summary.setStyleSheet(f"color: {_C_OK};")
            self._summary.setText(f"六桶检查完毕{tail}：未发现异常。")
            self._log.ok(f"异常检测（{owner_name}）：未发现异常")
            self._apply_card_filter()
            return
        # 本身零发现、只有未能检查的旗标：专用的友好汇总
        if not local_total and not (remote_total or 0) and flags \
                and n_health == 0:
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._summary.setText(
                "可判范围内未发现异常；有未能检查的部分，见上方横幅。")
            self._log.warn(
                f"异常检测（{owner_name}）：部分范围未能检查")
            self._apply_card_filter()
            return
        parts: list[str] = []
        if local_total:
            parts.append(f"本地发现 {local_total} 处")
        if remote_total:
            parts.append(f"联网深检确认 {remote_total} 处"
                         "（失效/查询失败/疑似合集）")
        if n_health:
            parts.append(f"远端健康异常 {n_health} 处（标题不符/弃坑/"
                         "banned）")
        if deps_bad:
            parts.append(f"依赖异常 {deps_bad} 处（缺依赖/依赖变化）")
        self._summary.setStyleSheet(f"color: {_C_WARN};")

        self._summary.setText(
            f"「{owner_name}」：" + "；".join(parts)
            + "。各桶卡片内有明细与修法引导，修完复检。")
        self._log.warn(f"异常检测（{owner_name}）：" + "；".join(parts))
        # 检测重建完卡片，套用「只看有问题的桶」筛选
        self._apply_card_filter()

    # ---------- 卡片基建 ----------

    def _card(self, name: str, title: str, tip: str,
              attention: bool) -> _BucketCard:
        """造一张桶卡片：登记进卡片表（筛选遍历用）+ 挂进容器。
        attention = 这桶是否"值得看"（有异常/未能检查）——
        「只看有问题的桶」开关据此藏卡。"""
        card = _BucketCard(name, title, tip)
        card.has_attention = attention
        self._cards[name] = card
        self._cards_box.addWidget(card)
        return card

    def _clear_cards(self) -> None:
        """清空上次检测的卡片（整体重建的"清"半边）。
        桶④的表格/下拉/批量按钮引用一并置 None：控件马上 deleteLater，
        悬空引用在销毁完成窗口期被访问会崩。"""
        self._cards = {}
        self._b4_table = None
        self._b4_combo_sort = None
        self._b4_batch_btn = None
        self._id_lists = []  # 查找框的列表登记一并清（v2.45）
        if self._cards_box is None:
            return
        while self._cards_box.count():
            item = self._cards_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _apply_card_filter(self) -> None:
        """「只看有问题的桶」开关：勾上后无异常/待检测/纯说明的桶
        整卡隐藏（不销毁，取消勾选立刻回来）。深检完成后桶⑤从
        "待检测"变成有内容，_render 末尾会重新套用本筛选。"""
        only_issues = self._only_issue.isChecked()
        for card in self._cards.values():
            card.setVisible((not only_issues) or card.has_attention)

    def _title_of(self, mid: int) -> str:
        """编号 → 标题（取自 _render 时一次取齐的对照表）。
        查不到或没标题（孤儿等）按「无标题」兜底。"""
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) \
            or "（无标题）"

    # ---------- 桶④管理台：工具行 + 表格 ----------

    def _build_b4_card(self, card: _BucketCard) -> None:
        """桶④卡片内容：工具行（状态筛选/视图/排序下拉 + 对勾选项 ▾）
        + 表格（✓ 勾选列，v2.45 六列）。下拉的选中值先按页面级状态
        恢复、再连信号、最后手动灌一次表——顺序保证不会在 connect 时
        触发多余的重建。"""
        # ---- 工具行 ----
        bar = QWidget(card)
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(4)
        lbl_f = QLabel("状态", bar)
        lbl_f.setStyleSheet("color: gray;")
        bh.addWidget(lbl_f)
        combo_filter = QComboBox(bar)
        combo_filter.addItems(["全部", *_CAT_ORDER])
        combo_filter.setCurrentText(self._b4_filter)
        combo_filter.setToolTip(
            "只显示所选分类的失效条目。「已收录·未下载」= 从未下载过"
            "就失效的条目（无可转移的整理内容，直接软删除即可）；"
            "「已删除」= 本页软删除过的（可到 mod 库页恢复）；"
            "「已清账」= 彻底清账后的历史归档（只读）。")

        combo_filter.currentTextChanged.connect(self._on_b4_filter)
        bh.addWidget(combo_filter)
        lbl_v = QLabel("视图", bar)
        lbl_v.setStyleSheet("color: gray;")
        bh.addWidget(lbl_v)
        combo_view = QComboBox(bar)
        combo_view.addItems(["平铺", "按分类分组"])
        combo_view.setCurrentIndex(1 if self._b4_group else 0)
        combo_view.setToolTip(
            "平铺 = 全部行一张表；按分类分组 = 表内插分类标题行"
            "（备份管理页的分组视图同款思路）。")
        combo_view.currentIndexChanged.connect(self._on_b4_view)
        bh.addWidget(combo_view)
        lbl_s = QLabel("排序", bar)
        lbl_s.setStyleSheet("color: gray;")
        bh.addWidget(lbl_s)
        self._b4_combo_sort = QComboBox(bar)
        self._b4_combo_sort.addItems(["编号", "标题", "分类"])
        self._b4_combo_sort.setCurrentText(self._b4_sort)
        # 分组视图下排序禁用（组内固定按编号，组间按固定类序）——
        # 与备份页"分组态不排序"同一口径；tooltip 说明去哪了
        self._b4_combo_sort.setEnabled(not self._b4_group)
        self._b4_combo_sort.setToolTip(
            "平铺视图下生效；分组视图下组内固定按编号排列。")
        self._b4_combo_sort.currentTextChanged.connect(self._on_b4_sort)
        bh.addWidget(self._b4_combo_sort)
        # 对勾选项 ▾（v2.45 用户 todo：勾选框→批处理）：只作用勾选行
        self._b4_batch_btn = QToolButton(bar)
        self._b4_batch_btn.setText("对勾选项 ▾")
        self._b4_batch_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._b4_batch_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        bm = QMenu(self._b4_batch_btn)
        bm.setToolTipsVisible(True)
        a_pages = QAction("批量打开工坊页面", bm)
        a_pages.setToolTip("勾选 mod 的工坊页面逐个在浏览器打开"
                           "（每个一页标签，开前确认）")
        a_pages.triggered.connect(self._b4_batch_open_pages)
        a_dirs = QAction("批量打开文件夹", bm)
        a_dirs.setToolTip("勾选 mod 的下载内容文件夹逐个在文件管理器打开")
        a_dirs.triggered.connect(self._b4_batch_open_dirs)
        a_del = QAction("批量软删除…", bm)
        a_del.setToolTip("把勾选中「可处置」的失效条目整批软删除"
                         "（列清单确认、默认否；已替换/已删除的行自动跳过）")
        a_del.triggered.connect(self._b4_batch_soft_delete)
        for a in (a_pages, a_dirs, a_del):
            bm.addAction(a)
        self._b4_batch_btn.setMenu(bm)
        bh.addWidget(self._b4_batch_btn)
        bh.addStretch(1)
        card.add_widget(bar)
        # ---- 表格 ----
        # 列（v2.45 六列）：✓ 勾选 ｜ ↗ ｜ mod 标题 ｜ mod 编号 ｜ 分类
        # ｜ 说明/原因。行内控件 = 勾选框 + ↗，其余动作走右键菜单——
        # 行内控件随排序搬家错位是删除页 v2.32 的坑，排序保持下拉重灌式
        t = QTableWidget(0, 6, card)
        t.setHorizontalHeaderLabels(
            ["✓", "↗", "mod 标题", "mod 编号", "分类", "说明 / 原因"])
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setWordWrap(False)  # 踩坑④：表格关换行，全文进 tooltip
        t.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = t.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 32), (1, 36), (3, 110), (4, 120), (5, 260)):
            t.setColumnWidth(col, width)
        # 右键 = 行菜单；双击 = 打开工坊页面（核验页"双击=快捷动作"
        # 同款手感）
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(self._on_b4_menu)
        t.cellDoubleClicked.connect(self._on_b4_double)
        card.add_widget(t)
        self._b4_table = t
        self._rebuild_b4_table()  # 初次灌表

    def _on_b4_filter(self, text: str) -> None:
        self._b4_filter = text
        self._rebuild_b4_table()

    def _on_b4_view(self, index: int) -> None:
        self._b4_group = (index == 1)
        # 分组态禁用排序下拉（口径见 _build_b4_card 里的 tooltip）
        if self._b4_combo_sort is not None:
            self._b4_combo_sort.setEnabled(not self._b4_group)
        self._rebuild_b4_table()

    def _on_b4_sort(self, text: str) -> None:
        self._b4_sort = text
        self._rebuild_b4_table()

    def _rebuild_b4_table(self) -> None:
        """桶④表格的唯一灌表出口：勾选剪除 → 状态筛选 → 查找过滤 →
        排序 →（可选）分组 → 灌行。数据源 = self._b4_rows（_render
        分类好的行字典列表）；这里绝不查库——表格只是视图开关。"""
        t = self._b4_table
        if t is None:
            return
        rows = list(self._b4_rows)
        # 勾选剪除（v2.45）：检测结果刷新后不在最新清单里的勾选清掉，
        # 集合不积灰（与 mod 库页模型 set_rows 的剪除同一思路）；
        # 已清账行一并剪除——它们没有任何可批量动作
        self._b4_checked &= {r["mid"] for r in rows
                             if r["cat"] != "已清账"}
        # 1. 状态筛选。「已清账」（彻底清账的历史归档，v2.48）不在
        #    默认视图显示——全是终结态、永远不会再有动作，留在
        #    「全部」里只会越积越多；入口 = 状态筛「已清账」
        #    （决策 78 同款：隐藏必须留计数，计数在卡片状态行）
        if self._b4_filter != "全部":
            rows = [r for r in rows if r["cat"] == self._b4_filter]
        else:
            rows = [r for r in rows if r["cat"] != "已清账"]

        # 2. 查找框过滤（v2.45）：标题/编号/说明包含即留
        kw = self._filter_edit.text().strip().casefold()
        if kw:
            rows = [r for r in rows if
                    kw in r["title"].casefold()
                    or kw in str(r["mid"])
                    or kw in (r["note"] or "").casefold()]
        # 3. 排序（分类序按 _CAT_ORDER 固定类序，组内按编号）
        if self._b4_sort == "标题":
            rows.sort(key=lambda r: (r["title"], r["mid"]))
        elif self._b4_sort == "分类":
            order = {c: i for i, c in enumerate(_CAT_ORDER)}
            rows.sort(key=lambda r: (order.get(r["cat"], 9), r["mid"]))
        else:
            rows.sort(key=lambda r: r["mid"])
        # 4. 灌表（先清残留：行与分组跨列合并都要清干净）
        t.clearSpans()
        t.setRowCount(0)
        if self._b4_group:
            for cat in _CAT_ORDER:
                subset = [r for r in rows if r["cat"] == cat]
                if not subset:
                    continue
                self._append_b4_group_header(
                    t, f"{cat}（{len(subset)} 条）")
                for r in subset:
                    self._append_b4_row(t, r)
        else:
            for r in rows:
                self._append_b4_row(t, r)
        # 5. 高度：行多时封顶内部滚动，行少时不留大片空白
        n_vis = t.rowCount()
        t.setFixedHeight(max(120, min(380, 28 * n_vis + 32)))
        if not rows:
            self._log.info("桶④：当前筛选下没有条目")

    def _append_b4_group_header(self, t: QTableWidget, text: str) -> None:
        """分组视图的分类标题行：跨全部 6 列合并、灰底、不可选中。"""
        row = t.rowCount()
        t.insertRow(row)
        it = QTableWidgetItem(text)
        # 只留 ItemIsEnabled：去掉可选/可编辑——标题行不是数据行，
        # 右键菜单与双击都按"编号列为空"跳过它
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
        it.setBackground(QBrush(QColor("#262626")))
        t.setItem(row, 0, it)
        t.setSpan(row, 0, 1, 6)  # v2.45：勾选列加入后 5 → 6

    def _append_b4_row(self, t: QTableWidget, r: dict) -> None:
        """桶④表格灌一行。勾选框状态按页面级 _b4_checked（mid）恢复
        ——重灌/排序不丢；编号塞进第 3 列 UserRole（右键/双击按数据
        取，不按行号反查——行号会漂，id 才稳，备份页同款纪律）。"""
        row = t.rowCount()
        t.insertRow(row)
        # 第 0 列：✓ 勾选框（v2.45，批量动作的入口）
        cb = QCheckBox(t)
        cb.setToolTip("勾选后可用「对勾选项 ▾」批量操作")
        cb.setChecked(r["mid"] in self._b4_checked)
        cb.toggled.connect(
            lambda on, mid=r["mid"]: self._on_b4_check(mid, on))
        t.setCellWidget(row, 0, cb)
        # 第 1 列：↗ 打开工坊页面
        btn = QToolButton(t)
        btn.setText("↗")
        btn.setToolTip("打开工坊页面（右键本行有更多动作）")
        btn.clicked.connect(
            lambda _=False, m=r["mid"]: self._open_workshop(m))
        t.setCellWidget(row, 1, btn)
        # 第 2 列：标题（全文进 tooltip，表格关了换行）
        ti = QTableWidgetItem(r["title"])
        ti.setToolTip(f"{r['mid']} {r['title']}")
        t.setItem(row, 2, ti)
        # 第 3 列：编号（UserRole = id，右键菜单按它找行数据）
        mi = QTableWidgetItem(str(r["mid"]))
        mi.setData(Qt.ItemDataRole.UserRole, r["mid"])
        t.setItem(row, 3, mi)
        # 第 4 列：分类（着色，一眼分清处置状态）
        ci = QTableWidgetItem(r["cat"])
        ci.setForeground(QBrush(QColor(
            _CAT_COLORS.get(r["cat"], _C_MUTED))))
        t.setItem(row, 4, ci)
        # 第 5 列：说明/原因（全文进 tooltip）
        ni = QTableWidgetItem(r["note"])
        ni.setToolTip(r["note"])
        t.setItem(row, 5, ni)

    def _b4_row_at(self, row: int) -> dict | None:
        """表格行号 → 行数据（按编号列的 UserRole 找）。分组标题行
        或取不到编号的行返回 None——调用方据此跳过。"""
        t = self._b4_table
        if t is None:
            return None
        mi = t.item(row, 3)  # v2.45：编号列 2 → 3（前面加了勾选列）
        if mi is None:
            return None
        mid = mi.data(Qt.ItemDataRole.UserRole)
        if mid is None:
            return None
        return next((r for r in self._b4_rows if r["mid"] == mid), None)

    def _on_b4_menu(self, pos) -> None:
        """桶④表格右键：按鼠标位置找行，按该行的分类装菜单
        （_fill_row_menu 单一出口，参数裁剪可见动作）。
        allow_cmd 跟 can_delete 走（v2.45）：红黄行（未删除、未替换）
        都给「跳到命令生成页」——result=9 有时仍能下载成功，
        先验证再处置（实测定口径）。"""
        t = self._b4_table
        if t is None:
            return
        item = t.itemAt(pos)
        if item is None:
            return
        r = self._b4_row_at(item.row())
        if r is None:
            return  # 分组标题行等：不弹菜单
        menu = QMenu(self)
        menu.setToolTipsVisible(True)  # QMenu 默认不显示 tooltip
        self._fill_row_menu(
            menu, r["mid"],
            allow_cmd=r["can_delete"],
            allow_replace=r["can_replace"],
            allow_delete=r["can_delete"],
            replaced_by=r["replaced_by"])
        menu.exec(t.viewport().mapToGlobal(pos))

    def _on_b4_double(self, row: int, _col: int) -> None:
        """双击行 = 打开工坊页面（核验页"双击=快捷动作"同款手感）。
        分组标题行没有编号数据，自然跳过。"""
        r = self._b4_row_at(row)
        if r is not None:
            self._open_workshop(r["mid"])
    def _on_b4_check(self, mid: int, on: bool) -> None:
        """桶④勾选状态单一出口：记集合 + 批量按钮带数。"""
        (self._b4_checked.add if on else self._b4_checked.discard)(mid)
        n = len(self._b4_checked)
        self._b4_batch_btn.setText(f"对勾选项({n}) ▾" if n else "对勾选项 ▾")

    def _b4_checked_rows(self) -> list[dict]:
        """勾选的行数据（按当前 _b4_rows 对表；顺序按编号）。"""
        mids = set(self._b4_checked)
        return sorted((r for r in self._b4_rows if r["mid"] in mids),
                      key=lambda r: r["mid"])

    def _b4_batch_open_pages(self) -> None:
        rows = self._b4_checked_rows()
        if not rows:
            QMessageBox.information(self, "批量打开工坊页面",
                                    "先勾选表格里的行。")
            return
        ret = QMessageBox.question(
            self, "批量打开工坊页面",
            f"在浏览器打开 {len(rows)} 个工坊页面？每个一页标签。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            self._open_workshop(r["mid"])

    def _b4_batch_open_dirs(self) -> None:
        rows = self._b4_checked_rows()
        if not rows:
            QMessageBox.information(self, "批量打开文件夹",
                                    "先勾选表格里的行。")
            return
        for r in rows:
            self._open_folder(r["mid"])

    def _b4_batch_soft_delete(self) -> None:
        """批量软删除：只对可处置（can_delete）的行生效；列清单确认、
        默认否（决策 69⒋）；逐条走 mark_deleted 正门（快照十字段与
        单条同源）。都是确认失效的条目，整批一次确认成立。"""
        rows = [r for r in self._b4_checked_rows() if r["can_delete"]]
        skipped = len(self._b4_checked) - len(rows)
        if not rows:
            QMessageBox.information(
                self, "批量软删除",
                "勾选里没有可处置的条目（已替换/已删除的行不能再删）。")
            return
        preview = "\n".join(f"· {r['mid']} {r['title']}" for r in rows[:20]) \
                  + ("\n…" if len(rows) > 20 else "")
        tail = (f"\n\n另有 {skipped} 个勾选行不可处置（已替换/已删除），"
                "将自动跳过。") if skipped else ""
        ret = QMessageBox.question(
            self, "批量软删除",
            f"把以下 {len(rows)} 条失效记录整批软删除？\n{preview}{tail}\n\n"
            "· 记录保留（含删除前快照），可到【mod 库】页右键「恢复」；\n"
            "· 盘上文件不动。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        done = 0
        for r in rows:
            mod = self._repo.get_mod(r["mid"])
            if mod is None:
                continue
            try:
                self._repo.mark_deleted(mod.mod_id,
                                        self._deleted_last_state(mod))
                done += 1
            except ValueError as exc:
                self._log.error(f"软删除失败（mod {mod.mod_id}）：{exc}")
        self._log.warn(f"批量软删除完成：{done}/{len(rows)} 条"
                       "（记录保留可恢复，盘上文件未动）")
        self._refresh_after_dispose()


    # ---------- 行列表与行菜单（桶①②③⑤ / 桶④共用）----------

    def _add_id_list(self, card: _BucketCard, entries: list[tuple[int, str, str]], *,
                     allow_cmd: bool, allow_dispose: bool = False) -> None:
        """编号清单 → 列表（每行 = 编号 + 标题 + 情况说明）。
        行数少时列表自然高，行数多时封顶内部滚动——卡片高度不失控，
        整页滚动也不被撑爆。行右键 = 行菜单（_fill_row_menu）。
        allow_dispose（v2.45）：给行菜单开放处置动作（软删除/关联替换）
        ——桶⑤查询失败的行用；疑似合集不开放（展开归检测页）。"""
        lw = QListWidget(card)
        lw.setWordWrap(False)
        lw.setAlternatingRowColors(True)
        # 高度：每行约 24px，下限 100 上限 240；行少时不留大片空白
        lw.setFixedHeight(min(240, max(100, 24 * len(entries) + 12)))
        for mid, title, note in entries:
            text = f"{mid} {title}"
            if note:
                text += f" · {note}"
            it = QListWidgetItem(text)
            # 编号塞进 UserRole：右键菜单从条目数据取，不按行号反查
            it.setData(Qt.ItemDataRole.UserRole, mid)
            lw.addItem(it)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lw.customContextMenuRequested.connect(
            lambda pos, w=lw: self._on_id_list_menu(w, pos, allow_cmd,
                                                    allow_dispose))
        self._id_lists.append(lw)  # 查找框过滤要遍历（v2.45）
        card.add_widget(lw)

    def _on_id_list_menu(self, lw: QListWidget, pos, allow_cmd: bool,
                         allow_dispose: bool = False) -> None:
        """编号列表的右键菜单：按鼠标位置找行，按行的编号装菜单。"""
        it = lw.itemAt(pos)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)  # QMenu 默认不显示 tooltip，必须显式开
        # allow_dispose（v2.45）：桶⑤查询失败的行也开放处置——查询失败
        # ≠确认失效（可能私有/波动），但确认没救的条目允许就地软删除/
        # 关联替换，不用绕道 mod 库页；确认弹窗与 tooltip 里另有说明
        self._fill_row_menu(menu, mid, allow_cmd=allow_cmd,
                            allow_replace=allow_dispose,
                            allow_delete=allow_dispose)
        menu.exec(lw.mapToGlobal(pos))

    def _fill_row_menu(self, menu: QMenu, mid: int, *,
                       allow_cmd: bool = False,
                       allow_replace: bool = False,
                       allow_delete: bool = False,
                       replaced_by: int | None = None) -> None:
        """给行菜单装动作的单一出口（所有行菜单共用，口径不漂移）。
        按参数裁剪可见项：
        - 跳命令页：桶①②（修复重下）+ 桶④红黄行（result=9 有时
          仍能下载成功，先试一次——v2.45 实测定口径）；
        - 关联替换：有失效归档的行（桶⑤查询失败行归档有无未知，
          点了无归档会弹窗说明）；确定无归档 = 置灰项 + 悬停说明；
        - 软删除：桶④可处置的行与桶⑤查询失败行（已替换/已删除不给）；
        - 打开替代 mod 页面：仅已替换的行；
        - 打开工坊页面 / 打开 mod 文件夹 / 复制编号：人人都有。
        查看类在上、处置类在下，中间加分隔线。"""
        # ---- 查看类 ----
        act = QAction("打开工坊页面", menu)
        act.setToolTip(
            "在浏览器打开该 mod 的创意工坊页面"
            "（失效条目也能打开，可看作者是否留了续作/说明；"
            "网页能打开的大概率还能下载）")
        act.triggered.connect(lambda: self._open_workshop(mid))
        menu.addAction(act)
        if replaced_by:
            act = QAction(f"打开替代 mod（{replaced_by}）页面", menu)
            act.setToolTip("在浏览器打开替换目标 mod 的创意工坊页面")
            act.triggered.connect(
                lambda: self._open_workshop(replaced_by))
            menu.addAction(act)
        act = QAction("打开 mod 文件夹", menu)
        act.setToolTip(
            "在文件管理器打开该 mod 的下载内容文件夹"
            "（steamcmd 工坊内容目录下以编号命名的文件夹）")
        act.triggered.connect(lambda: self._open_folder(mid))
        menu.addAction(act)
        act = QAction("复制编号", menu)
        act.setToolTip("把这个 mod 的工坊编号复制到剪贴板")
        act.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act)
        # ---- 处置类 ----
        if allow_cmd or allow_replace or allow_delete:
            menu.addSeparator()
        if allow_cmd:
            act = QAction("跳到命令生成页（只勾这条）…", menu)
            act.setToolTip(
                "跳到【下载命令生成】页并只勾选这个编号，生成下载"
                "命令。result=9 的条目有时仍能下载成功（接口看不见"
                "≠内容服务器没有）——值得一试；下载成功后到"
                "【mod 库】页点【扫描本地】入账")
            act.triggered.connect(
                lambda: self._emit_command_gen([mid]))
            menu.addAction(act)
        if allow_delete:
            act = QAction("软删除此记录…", menu)
            act.setToolTip(
                "账本记为已删除（记录保留可恢复，盘上文件不动）。"
                "三步主路径的最后一步：找续作 → 加入新 mod → "
                "删旧记录。建议先试一次重下（result=9 偶尔仍能"
                "下载成功）再决定")
            act.triggered.connect(lambda: self._soft_delete_failed(mid))
            menu.addAction(act)
        if allow_replace:
            act = QAction("关联替换…（进阶）", menu)
            act.setToolTip(
                "把这条记录的备注/颜色标记/特别关注一键带给一个"
                "已在库的替代条目，归档记「旧 → 新」证据链。\n"
                "需要该条目有失效归档（由【更新检测】对 result=9 建立）；"
                "没有归档时软件会弹窗说明原因")
            act.triggered.connect(lambda: self._replace_failed(mid))
            menu.addAction(act)
        elif allow_delete:
            # 确定无归档（分类时 rec is None 已判定）：置灰 + 悬停说明
            # ——红黄都可能无归档（刚联网确认还没建档的 / 从未下载过的）
            act = QAction("关联替换…（进阶）", menu)
            act.setEnabled(False)
            act.setToolTip(
                "这一行没有失效归档，暂不能关联替换——失效归档由"
                "【更新检测】在确认条目失效（result=9）时建立，"
                "本行目前还没有。\n"
                "直接走三步主路径即可：找续作 → 加入新 mod → "
                "软删除旧记录")
            menu.addAction(act)

    def _open_workshop(self, mid: int) -> None:
        """在浏览器打开该 mod 的工坊页面。
        网址优先取账本 url 字段，缺省按模板拼（与备份页同一份单源）。"""
        m = self._mods_by_id.get(mid)
        url = ((m.url if m is not None else "") or "").strip() \
              or WORKSHOP_URL_TEMPLATE.format(mid)
        # openUrl 失败是静默的（v2.18 教训）：手动兜底提示
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(
                self, "打开页面",
                f"浏览器没有响应，请手动打开：\n{url}")
            return
        self._log.info(f"已请求浏览器打开 mod {mid} 的工坊页面")

    def _copy_id(self, mid: int) -> None:
        """复制编号到剪贴板 + 日志回执（决策 22③：用户动作要有反馈）。"""
        QApplication.clipboard().setText(str(mid))
        self._log.info(f"已复制编号 {mid}")

    def _open_folder(self, mid: int) -> None:
        """行菜单「打开 mod 文件夹」：实现单源 gui/modFolderOpener
        （与 mod 库/更新对照页共用）。档案取渲染时锁定的 owner——
        深检进行中切了档案也不会把文件夹开到别的游戏名下。"""
        if self._owner_game is None:
            QMessageBox.information(self, "打开 mod 文件夹",
                                    "请先点【开始检测】再使用此动作。")
            return
        m = self._mods_by_id.get(mid)
        if m is None:
            QMessageBox.information(self, "打开 mod 文件夹",
                                    f"账本里没有 mod {mid} 的记录。")
            return
        open_mod_folder(self, self._owner_game, m, log=self._log)

    def _apply_item_filter(self) -> None:
        """查找框出口：各编号列表逐项隐藏 + 桶④表格按关键词重灌。
        只隐藏不销毁，清空即全回。"""
        kw = self._filter_edit.text().strip().casefold()
        for lw in self._id_lists:
            for i in range(lw.count()):
                it = lw.item(i)
                it.setHidden(bool(kw) and kw not in it.text().casefold())
        self._rebuild_b4_table()

    @staticmethod
    def _deleted_last_state(mod) -> dict:
        """软删除前快照的十字段组装（repo 契约：调用方组装）——
        单条与批量软删除共用，不再两处各抄一份。"""
        return {"title": mod.title, "url": mod.url,
                "time_updated": mod.time_updated,
                "local_timeupdated": mod.local_timeupdated,
                "manifest": mod.manifest, "local_size": mod.local_size,
                "note": mod.note, "color_tag": mod.color_tag,
                "is_special": mod.is_special, "local_path": mod.local_path}

    # ---------- 桶④处置一：软删除（主路径）----------

    def _soft_delete_failed(self, mid: int) -> None:
        """桶④处置主路径：软删除失效记录。
        走 repo.mark_deleted 正门（与核验页"标记为已移除"、mod 库页
        "软删除"同一条路）——mark_deleted 会同时写 deleted_at（删除
        时刻）和 deleted_last_state（删除前元数据快照），"记录保留、
        可随时恢复"靠的就是这两列；update_status 只改 status，会把
        恢复凭据丢掉。本动作不动磁盘文件。
        确认弹窗默认"否"（决策 69⒋）；last_state 十字段组装走
        _deleted_last_state 单源（与批量软删除同源）。
        result=9 偶尔仍能下载成功（v2.45 实测）——弹窗提醒先试重下，
        防止把能救的条目顺手删了（软删除可逆，但能不删就不删）。"""
        mod = self._repo.get_mod(mid)
        if mod is None:
            # 行是检测时读出来的，理论上不会消失；万一并发变动，
            # 按项目口径显式报告，不静默吞
            self._log.error(
                f"软删除失败（mod {mid}）：账本中已找不到该条目")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mid} 不在账本中，无法软删除。\n"
                "请重新点【开始检测】后再试。")
            return
        ret = QMessageBox.question(
            self, "软删除失效记录",
            f"确定将失效的「{mod.title or mid}」（mod {mid}）的"
            "账本记录软删除？\n\n"
            "· 记录保留（含删除前快照），可到【mod 库】页右键「恢复」；\n"
            "· 盘上文件不动——想清文件去【清理删除】模块"
            "（失效条目在那里正好只给「仅删文件」）；\n"
            "· 本工具不再为它生成下载命令。\n\n"
            "提示：result=9 的条目偶尔仍能下载成功——若还没试过"
            "重下，可先到【下载命令生成】页试一次再决定。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)  # 默认否（决策 69⒋）
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.mark_deleted(mid, self._deleted_last_state(mod))
        except ValueError as exc:
            self._log.error(f"软删除失败（mod {mid}）：{exc}")
            QMessageBox.critical(
                self, "操作失败",
                f"mod {mid} 软删除失败：{exc}\n"
                "请重新点【开始检测】后再试。")
            return
        self._log.warn(
            f"mod {mid}（失效）已软删除：记录保留可恢复，"
            "盘上文件未动；复检后该行会变为「已删除」分类")
        self._refresh_after_dispose()

    # ---------- 桶④处置二：关联替换（进阶）----------

    def _replace_failed(self, old_id: int) -> None:
        """桶④进阶处置：关联替换——把备注/颜色标记/特别关注一键
        带给已在库的替代条目（主路径 = 三步引导 + 软删除，见文件头）。
        交互四步：输入新编号 → 校验 → 确认 → 执行并复检。
        全部异常显式报告，不静默吞。
        注意编号输入不用 QInputDialog.getInt——它封顶 2³¹-1，
        而工坊编号可到约 43 亿，超上限；用 getText 手工解析。"""
        text, ok = QInputDialog.getText(
            self, "关联替换",
            f"mod {old_id} 已失效。\n输入替代 mod 的工坊编号（纯数字）：")
        if not ok:
            return
        try:
            new_id = int(text.strip())
        except ValueError:
            QMessageBox.warning(self, "关联替换", "编号必须是纯数字。")
            return
        if new_id <= 0 or new_id == old_id:
            QMessageBox.warning(
                self, "关联替换",
                "编号无效（需为正整数，且不能与失效编号相同）。")
            return
        mod = self._repo.get_mod(new_id)
        if mod is None:
            QMessageBox.warning(
                self, "关联替换",
                f"编号 {new_id} 不在账本中。\n请先【网址批量导入】或"
                "扫描入账，再回来替换。")
            return
        ret = QMessageBox.question(
            self, "关联替换",
            f"把 mod {old_id} 的失效记录替换为 mod {new_id}"
            f"「{mod.title or ''}」？\n\n"
            "将迁移整理成果：备注、颜色标记、特别关注；\n"
            "归档中记下「旧 → 新」证据链；\n"
            "旧 mod 保留失效状态，作为历史记录。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.replace_failed_mod(old_id, new_id)
        except ValueError as exc:
            # 常见情形：旧条目只有 failed 状态没有归档（归档由更新检测
            # 建立），或替代条目状态异常——原文报给用户，不猜
            self._log.error(
                f"关联替换失败（mod {old_id} → {new_id}）：{exc}")
            QMessageBox.critical(self, "关联替换失败", str(exc))
            return
        self._log.ok(
            f"已关联替换：mod {old_id} → mod {new_id}"
            "（旧记录保留为失效归档；复检后该行会变为「已替换」分类）")
        self._refresh_after_dispose()

    # ---------- 跳转命令生成页 ----------

    def _emit_command_gen(self, ids: list[int]) -> None:
        self._log.info(
            f"跳转命令生成页并勾选 {len(ids)} 个编号（异常修复重下）")
        self.command_gen_requested.emit(list(ids))


    def _ids_text(ids: list[int], limit: int = _ID_LIMIT) -> str:
        """编号清单 → 展示文本：最多列 limit 个，超出折成"…"。"""
        head = "、".join(str(i) for i in ids[:limit])
        if len(ids) > limit:
            head += f" …（共 {len(ids)} 个）"
        return head

    def _fmt_ts(ts: int | None) -> str:
        if not ts:
            return "从未"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

