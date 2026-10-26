"""标题检测
"""
r"""gui/titleCheckPage.py · 桶C 本地标题关键词提醒（D24 拆分页之一）。

从 V1 异常处理页拆出的离线专项：本地账本标题含提醒关键词的条目
黄字提醒。词表在设置页「本地标题提醒关键词」自行增删，留空 = 停用；
作者常把 Abandoned / Deprecated 写进标题表示不再维护——但地图名
也可能含这些词（如 "Abandoned Mines"），命中词在行内明示，只是
提醒不是判定，判断权在用户。

离线、毫秒级、零联网、不入闸（D24 明文：无联网资源可抢）。
set_game 与进页（refresh 钩子）都重算——设置页改完词表，切回来
就是新结果。只读：不写库、不动文件、不发网络请求。

远端侧的弃坑检测在【远端健康】页，两处词表相互独立（V1 口径照搬）。
"""
import html
import re

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QAction, QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget, QApplication,
    QAbstractItemView, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem
)

from core.models import Game
from core.urlParser import workshop_url
from gui.logBus import LogBus
from gui.theme import font_px  # 字号单源（D25）
from workflows import exceptionFlow
from gui.tableSort import SortItem  # 表头数值排序（单源共享件）
from gui.collapsibleSection import make_guide  # 先读我单源（共享件内）

_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_MUTED = "#8a8a8f"
_TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
# 标题纯文本住数据角色：显示层是富文本标签，查找过滤读这里


class TitleCheckPage(QWidget):
    """桶C 本地标题关键词提醒：离线毫秒级，set_game / refresh 即算。"""

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()   # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._mods_by_id: dict[int, object] = {}
        self._build_ui()

    # ---------- UI ----------
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

        title = QLabel("标题检测", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)
        root.addWidget(make_guide(
            "先读我：词表在哪改 · 为什么只是提醒不是判定",
            ("本地账本标题里含提醒关键词的条目（词表在设置页"
             "「本地标题提醒关键词」自行增删，留空 = 停用）。",
             "作者常把 Abandoned / Deprecated 写进标题表示不再维护——"
             "但地图名也可能含这些词（如 \"Abandoned Mines\"），命中词"
             "在行内明示，只是提醒不是判定。离线毫秒级，不联网。"),
            self, 120))

        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("开始检测", row)
        btn.setToolTip("点击才扫描本地标题（设置页改完词表，点它重新生效）")
        btn.clicked.connect(self._recompute)
        h.addWidget(btn)
        h.addStretch(1)
        root.addWidget(row)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        # 页面级查找（备份页同款：防抖 300ms、只隐藏不清数据、清空恢复）
        srow = QWidget(self)
        sh = QHBoxLayout(srow)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(QLabel("查找：", srow))
        self._filter_edit = QLineEdit(srow)
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setPlaceholderText(
            "按标题 / 编号 / 命中词过滤（即时生效，只隐藏；清空恢复）…")
        self._filter_edit.textChanged.connect(
            lambda _t: self._filter_timer.start())
        sh.addWidget(self._filter_edit, 1)
        root.addWidget(srow)

        # 命中表（备份页同款表格化）：编号/标题/命中词分列——黏一行
        # 分不清的教训；标题列富文本标出命中词位置（"Abandoned Mines"
        # 式误报当场可辨）。双击行 = 开工坊页面
        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(
            ["编号", "标题（双击行 = 打开工坊页面）", "命中词"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._table.setWordWrap(False)
        # 不开交替行色（qdarktheme QSS 会盖掉富文本标签的底，⑩同款坑）
        _th = self._table.horizontalHeader()
        _th.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        _th.setSectionsClickable(True)  # 表头可点排序（原 False 关死）
        _th.sortIndicatorChanged.connect(
            lambda *_a: self._apply_filter())  # 排序搬条目、隐藏挂行号

        self._table.setColumnWidth(0, 110)
        self._table.setColumnWidth(2, 160)
        self._table.itemDoubleClicked.connect(self._on_dclicked)
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_menu)
        root.addWidget(self._table, 1)

        # 查找防抖（备份页同款 300ms）
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self._apply_filter)

        note = QLabel(
            "改词表：设置页 →「本地标题提醒关键词」。远端侧的弃坑检测"
            "在【远端健康】页（联网深检时判定），两处词表相互独立。",
            self)
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        root.addWidget(note)


    # ---------- 对外（MainWindow 自动调用）----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
        # 不自动检测：切档案只清场，结果等用户点【开始检测】
        # 不自动检测：切档案只清场，结果等用户点【开始检测】
        self._table.setRowCount(0)
        self._table.setSortingEnabled(False)  # 带排序灌数据=边灌边排、行错位

        self._filter_edit.clear()
        self._mods_by_id = {}

        if game is None:
            self._status.setText("选好档案后点【开始检测】。")
        else:
            self._status.setText("点【开始检测】扫描当前档案的本地标题。")
        self._status.setStyleSheet(f"color: {_C_MUTED};")

    def refresh(self) -> None:
        """进页钩子：不再自动重算（只在用户点击【开始检测】时跑）。
        改过词表后点【开始检测】重扫即可。"""

    # ---------- 计算（纯读）----------
    def _recompute(self) -> None:
        self._table.setRowCount(0)
        self._table.setSortingEnabled(False)  # 带排序灌数据=边灌边排、行错位
        if self._game is None:
            self._status.setText("请先在左上角选择档案，再点【开始检测】")
            self._status.setStyleSheet(f"color: {_C_MUTED};")
            return
        mods = self._repo.list_mods(self._game.app_id)
        self._mods_by_id = {m.mod_id: m for m in mods}
        titles = {m.mod_id: (m.title or "") for m in mods}
        # 词表来源：设置页；键恒有默认值（AppSettings 起底合并），
        # 用户清空 = 空串 = 停用（引擎口径）；settings 缺席时回退
        # 引擎默认词表（防御分支，正常接线到不了）
        kw_raw = (self._settings.get("local_title_warn_keywords")
                  if self._settings is not None else None)
        if kw_raw is None:
            kw_raw = exceptionFlow.DEFAULT_TITLE_KEYWORDS
        lt = exceptionFlow.classify_local_titles(titles, kw_raw)
        if not str(kw_raw or "").strip():
            self._status.setText("已停用（设置页关键词为空）")
            self._status.setStyleSheet(f"color: {_C_MUTED};")
            return
        if lt.total == 0:
            self._status.setText("无命中")
            self._status.setStyleSheet(f"color: {_C_OK};")
            return
        self._status.setText(
            f"{lt.total} 个标题含关键词——打开工坊页面对照一眼，"
            "内容真的停更了再决定去留；误报的把对应词从设置页词表删掉。")
        self._status.setStyleSheet(f"color: {_C_WARN};")
        hits = sorted(lt.hits.items())
        self._table.setRowCount(len(hits))
        for r, (mid, kws) in enumerate(hits):
            title = self._title_of(mid)
            c0 = SortItem(str(mid), mid)  # 排序键 = 编号数值
            c0.setData(Qt.ItemDataRole.UserRole, mid)
            # 显示全交富文本标签；条目只带排序键与数据角色——条目里
            # 再存一份显示文字 = 同一格画两遍（截图里的重影）
            c1 = SortItem("", title)
            c1.setData(_TITLE_ROLE, title)
            c1.setToolTip("双击行打开工坊页面；右键更多动作\n命中："
                          + "、".join(kws))
            c2 = SortItem("、".join(kws), "、".join(kws))
            c2.setForeground(QColor(_C_WARN))
            _f = c2.font()
            _f.setBold(True)
            c2.setFont(_f)
            for c, cell in enumerate((c0, c1, c2)):
                self._table.setItem(r, c, cell)
            # 标题列富文本：命中词加粗标色。鼠标穿透
            # （WA_TransparentForMouseEvents）——双击/右键/悬浮直达
            # 表格，不在标签上被吃掉；悬浮说明住在 c1 条目上
            lbl = QLabel(self._marked_title(title, kws), self._table)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setContentsMargins(6, 0, 2, 0)  # 与条目文字的左内边距看齐
            self._table.setCellWidget(r, 1, lbl)
        self._table.setSortingEnabled(True)  # 灌完再开；点过的排法自动重放
        self._apply_filter()
        self._log.warn(
            f"标题检测（{self._game.name}）：{lt.total} 个标题含关键词")

    @staticmethod
    def _marked_title(title: str, kws: list[str]) -> str:
        """标题富文本：命中词加粗标色（一眼看到词落在标题哪个位置）。
        大小写不敏感；多词重叠命中取并集；re.escape 防关键词里的
        正则特殊字符（如 C++）。"""
        spans: list[tuple[int, int]] = []
        for kw in kws:
            if not kw.strip():
                continue
            for m in re.finditer(re.escape(kw), title, re.IGNORECASE):
                spans.append((m.start(), m.end()))
        if not spans:
            return html.escape(title)
        spans.sort()
        merged: list[list[int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out: list[str] = []
        pos = 0
        for s, e in merged:
            out.append(html.escape(title[pos:s]))
            out.append('<b style="color:' + _C_WARN + '">'
                       + html.escape(title[s:e]) + "</b>")
            pos = e
        out.append(html.escape(title[pos:]))
        return "".join(out)

    def _on_dclicked(self, item) -> None:
        """双击任意格 = 打开该行 mod 的工坊页面（最快路径；右键
        菜单照旧——一个动作一个定义没变，只是多了一个入口）。"""
        it = self._table.item(item.row(), 0)
        mid = it.data(Qt.ItemDataRole.UserRole) \
            if it is not None else None
        if mid is not None:
            self._open_url(mid)
    def _apply_filter(self) -> None:
        """查找落地：不命中的行隐藏（只隐藏不清数据，清空即全恢复；
        幂等）。标题列的纯文本住在数据角色（显示层是富文本标签），
        匹配读数据不读显示。"""
        kw = self._filter_edit.text().strip().casefold()
        t = self._table
        for r in range(t.rowCount()):
            if not kw:
                t.setRowHidden(r, False)
                continue
            it1 = t.item(r, 1)
            hay = " ".join((
                t.item(r, 0).text() if t.item(r, 0) is not None else "",
                str(it1.data(_TITLE_ROLE) or "")
                if it1 is not None else "",
                t.item(r, 2).text() if t.item(r, 2) is not None else "",
            )).casefold()
            t.setRowHidden(r, kw not in hay)


    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or "（无标题）"

    # ---------- 行菜单（查看类，与 V1 桶C 行同款）----------
    def _on_row_menu(self, pos) -> None:
        it = self._table.item(self._table.rowAt(pos.y()), 0) \
            if self._table.rowAt(pos.y()) >= 0 else None
        mid = it.data(Qt.ItemDataRole.UserRole) \
            if it is not None else None
        if mid is None:
            return
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        act_url = QAction("打开工坊页面", menu)
        act_url.setToolTip("在浏览器打开该 mod 的创意工坊页面")
        act_url.triggered.connect(lambda: self._open_url(mid))
        menu.addAction(act_url)
        act_dir = QAction("打开 mod 文件夹", menu)
        act_dir.setToolTip("在文件管理器打开该 mod 的下载内容文件夹")
        act_dir.triggered.connect(lambda: self._open_folder(mid))
        menu.addAction(act_dir)
        act_copy = QAction("复制编号", menu)
        act_copy.triggered.connect(lambda: self._copy_id(mid))
        menu.addAction(act_copy)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _open_url(self, mid: int) -> None:
        # 网址走唯一入口 workshop_url（存了用存的、没存按编号现拼）。
        # 此前引用的 constants.WORKSHOP_URL_TEMPLATE 已删，再调用
        # 就是 AttributeError
        m = self._mods_by_id.get(mid)
        url = workshop_url(mid, m.url if m is not None else None)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打开页面", f"浏览器没有响应，请手动打开：\n{url}")

    def _open_folder(self, mid: int) -> None:
        from gui.modFolderOpener import open_mod_folder   # 局部 import：仅此菜单用到
        m = self._mods_by_id.get(mid)
        if m is None or self._game is None:
            return
        open_mod_folder(self, self._game, m, log=self._log)

    def _copy_id(self, mid: int) -> None:
        QApplication.clipboard().setText(str(mid))
        self._log.info(f"已复制编号 {mid}")
