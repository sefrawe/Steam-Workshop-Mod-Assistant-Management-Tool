"""mod 详情面板（全字段版）

gui/modDetailPanel.py · mod 库页右侧的详情面板（只读展示）。
选中列表行 → show_mod()；清空选择 → show_mod(None) 显示占位提示。

V2 初版只搬了 10 个字段（判决制新芯），V1 全字段面板的其余字段没跟上
——本轮按 V1 的分区骨架补全：基本档案 / 版本对照 / Steam 数据 /
标记与备注 / 软删除 / 最近判决。字段全部来自 V2 的 Mod 模型；
manifest / local_timeupdated / local_path 三个 acf 时代字段已随判决制
从模型删除，不复活（显示层无米下锅是设计，不是遗漏）。

口径（与 modListModel 同源，改口径两处一起改）：
- 本地版本 = 你确认过的版本；来源词取 constants.CONFIRMED_SOURCE_ZH，
  未验证/旧账两档附一行灰色说明（右键「设定本地版本…」可重新认定）；
- 「最近判决」= repo.list_verdicts 最近 5 条（D22）。判决 = 本地版本
  每一次落账的凭据：批次收尾确认、认领、手动设定各记一条——账上
  为什么是这个版本，判决史说了算，有争议先看这里；
- 全部值可选中复制（D18）；字段名进名称格的悬浮提示（调试对照库表）；
- 面板零写操作：唯一的动作按钮「打开 mod 文件夹」只发请求信号，
  动作实现唯一住在 gui/modFolderOpener（打开行为全软件一份）。
"""

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QAction
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget, QAbstractItemView, QApplication, QHeaderView, QTableWidget, QTableWidgetItem, QHBoxLayout, QPushButton,
    QMessageBox, QTabBar, QMenu,
    QTextBrowser,

)

from core import netGate
from core.bbcodeParser import blocks_to_html, parse_description, text_segments
from core.constants import (CONFIRMED_SOURCE_ZH, NET_GATE_TRANSLATE,
                            STATUS_DELETED, STATUS_DOWNLOADED, VERDICT_KIND_ZH)
from core.formatters import abs_time, fmt_size, relative_time, status_zh
from core.urlParser import workshop_url
from gui.theme import font_px  # 字号单源（D25）
from gui.translateWorker import TranslateWorker

# 未验证/旧账两档来源的灰色说明（一次性文案，留本模块——D37 准入）
_SOURCE_NOTES = {
    "unverified": "这一版没有终端「下载成功」判决背书（批查失败但照常"
                  "确认）——要重认一遍：右键「设定本地版本…」，或重新"
                  "下载后确认。",
    "inherited_acf": "旧账：一次性迁移时继承的记录——右键「设定本地"
                     "版本…」可重新认定。",
}

_DIM = "#8a8a8a"      # 弱化信息统一灰（与列表模型同拍）
_VERDICT_SHOW = 5     # 最近判决展示条数（D22）

# 查远端信息 / 送登记 按钮共用说明（页 1 卡片与依赖子页两处按钮同文案）
_FETCH_TIP = ("对依赖清单做一轮匿名批量查询（无需 key、不写账本）："
              "账外与未下载的依赖补出「远端在架」（蓝）或「远端查无」"
              "（灰），大小与远端更新时间一并补上。\n"
              "回执只进显示、不入账本——账外编号想收录，送"
              "【入账中心 · 登记】。与其它联网功能共用互斥闸")
_SEND_TIP = ("把清单里「账外」的依赖编号直接送【入账中心 · 登记】："
             "预填粘贴框、自动解析预览（不写库）——核对后点"
             "【登记新编号】入账")

class ModDetailPanel(QWidget):
    """详情面板。open_folder_requested(编号)：打开文件夹按钮发出，
    mod 库页接住后调 gui/modFolderOpener 的唯一实现（面板不认识
    Game 对象，不直接开文件夹）。"""

    # 参数 = mod 编号。必须用 object 不用 int：工坊编号超 32 位
    # （实测 2846867517），Signal(int) 是 C++ int32 → emit 时 Overflow、
    # 信号派发失败、槽被报"not found"——本地位置链接与打开文件夹
    # 按钮两条路一起哑火。跨件传 mod_id 一律 object/list，见踩坑 79
    open_folder_requested = Signal(object)
    # 依赖子页行双击 → 请求库页定位选中（跳转动作住库页，面板只发
    # 信号——与 open_folder_requested 同一分工）。跨件传编号一律
    # object（工坊编号超 32 位，Signal(int) 会 Overflow——踩坑 79）
    mod_link_requested = Signal(object)
    # 依赖子页「送登记」→ 主窗口转接入账中心（只送账外编号——账内
    # 的已在册，登记解析只会回"已在册"，送了也白送）
    send_to_register_requested = Signal(list)
    # 描述翻译完成 → 译文分段数组（与 bbcodeParser.text_segments 的
    # text 块同序，None=该段失败）交接收方落库（写口 A：面板零写
    # 操作，repo 写入住 mod 库页/主窗）。mid 用 object（超 32 位）
    translation_ready = Signal(object, object)  # 双 object：list 过
    # QVariant 转换时 None 段有被整形的风险，object 直传 Python 原对象
    # 零转换（踩坑 79 同精神：跨件传复杂东西一律 object）

    # 页签 ↔ 栈页映射（栈页：0=无选中占位 1=基本信息 2=依赖 3=描述；
    # 页签：0基本信息 1描述 2依赖。翻译/描述合一页签，页内三态切换）
    _TAB_TO_PAGE = {0: 1, 1: 3, 2: 2}
    _PAGE_TO_TAB = {1: 0, 3: 1, 2: 2}

    # 分区与字段清单（单源：构建与赋值都按它走；键 = _vals 索引）
    _SECTIONS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
        ("基本档案", (
            ("title", "标题", "title"),
            ("mod_id", "Mod 编号", "mod_id"),
            ("status", "状态", "status"),
            ("game_id", "所属游戏", "game_id"),
            ("creator", "作者", "creator"),
            ("url", "工坊链接", "url"),
            ("time_created", "创建时间", "time_created"),
            ("first_tracked_at", "收录时间", "first_tracked_at"),
            ("local_path", "本地位置", "（推导：下载目录\\编号，不入库）"),

        )),
        ("版本对照（本地 ↔ 远端）", (
            ("confirmed", "本地版本", "confirmed_version"),
            ("confirmed_at", "确认时间", "confirmed_at"),
            ("local_size", "本地大小", "local_size"),
            ("remote", "远端版本", "time_updated"),
            ("last_time_updated", "上次远端更新", "last_time_updated"),
            ("file_size", "远端大小", "file_size"),
            ("last_checked_at", "上次检测时间", "last_checked_at"),
        )),
        # 依赖（⑪）：明细住依赖子页（栈页 2），本区只摆入口行——
        # 值 = 三态摘要（未获取/无依赖/分布计数+链接）；条目留这份
        # 清单只为显示顺序单源
        ("依赖（必需物品）", (
            ("deps", "依赖", "（依赖检测页拉取后入账）"),
        )),

        ("Steam 数据", (
            ("subscriptions", "订阅数", "subscriptions"),
            ("favorited", "收藏数", "favorited"),
            ("views", "浏览数", "views"),
            ("tags", "标签", "tags"),
            ("preview_url", "预览图链接", "preview_url"),
        )),
        ("标记与备注", (
            ("is_special", "特别关注", "is_special"),
            ("color_tag", "颜色标记", "color_tag"),
            ("note", "备注", "note"),
        )),
        ("软删除", (
            ("deleted_at", "删除时间", "deleted_at"),
            ("deleted_last_state", "删除前快照", "deleted_last_state"),
        )),
    )

    def __init__(self, repo, settings=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        # settings 只为「查远端信息」读请求间隔/重试次数；None = 用默认值
        # （面板单独调试时不炸）。面板本体依旧零写操作，查询不碰库。
        self._settings = settings
        self._mod = None
        self._download_dir: str | None = None
        # 依赖远端信息回执（匿名批查，内存不入账——账外编号的归宿
        # 是【入账中心 · 登记】，账内条目的账本数据照旧）
        self._dep_meta: dict[int, dict] = {}
        self._fetch_worker = None  # 查远端信息的批查询线程
        self._meta_note = ""  # 查询回执信息（finished 里统一落 hint 行）
        self._dep_graph = None  # 反向依赖图缓存：(game_id, 拉取戳) → 图
        self._trans_worker = None   # 翻译线程（刀3）
        self._desc_blocks: list = []   # 当前 mod 描述块序列（解析缓存）
        self._desc_segs = None      # 译文分段（list[str|None] | None）
        self._desc_mode = "original"
        self._trans_mid = None      # 进行中翻译的归属 mod（防切 mod 串档）
        self._trans_done = 0
        self._trans_total = 0

        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        # 页签导航（基本信息/描述/翻译/依赖）：点页签切下方内容区；
        # 描述/翻译 = 占位页（模块冻结中），依赖 = 原子页
        self._tabs = QTabBar(self)
        # 页签导航（基本信息/描述/依赖）：点页签切下方内容区；
        # 描述 = 三态渲染+翻译正身页，依赖 = 原子页
        for _n in ("基本信息", "描述", "依赖"):
            self._tabs.addTab(_n)

        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tabs.setEnabled(False)  # show_mod 按有无选中开/关
        _tabs_wrap = QWidget(self)
        _tv = QVBoxLayout(_tabs_wrap)
        _tv.setContentsMargins(0, 0, 0, 0)
        _tv.setSpacing(4)
        _tv.addWidget(self._tabs)
        _tv.addWidget(self._stack, 1)

        root.addWidget(_tabs_wrap)

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

        # 页 1：详情正文——全字段 20+ 行，必须装滚动区，固定骨架不裁字
        page = QWidget(self)
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 0, 0, 0)
        body = QWidget(page)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(6)

        self._title = QLabel(body)
        self._title.setWordWrap(True)
        self._title.setStyleSheet(
            f"font-size: {font_px(15)}px; font-weight: 600;")
        bl.addWidget(self._title)

        self._form = QFormLayout()
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        bl.addLayout(self._form)

        # 来源灰色说明行：只在未验证/旧账/版本未知时出现
        self._src_note = QLabel(body)
        self._src_note.setWordWrap(True)
        self._src_note.setStyleSheet(
            f"color: {_DIM}; font-size: {font_px(11)}px;")
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



        # 字段行一次建齐（固定顺序 = 固定行序）：show_mod 只改字，
        # 不再加删行——面板不跳动，惰性重建的老坑不存在
        self._vals: dict[str, QLabel] = {}
        for sec, rows in self._SECTIONS:
            head = QLabel(sec, body)
            head.setStyleSheet("font-weight: 600;")
            self._form.addRow(head)
            for key, zh, field in rows:
                name = QLabel(zh, body)
                # 推导类字段（field 以"（"开头）不是库字段，提示照实说
                name.setToolTip(field if field.startswith("（")
                                else f"数据库字段：{field}")
                val = QLabel("—", body)
                val.setWordWrap(True)
                val.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse)
                if key in ("url", "preview_url"):
                    # 链接可点 = 交互标志必须含 LinksAccessibleByMouse
                    # ——只给 TextSelectableByMouse 会把链接点性一起
                    # 关掉（预览图链接"点击没反应"的根因）
                    val.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                        | Qt.TextInteractionFlag.LinksAccessibleByMouse)
                    val.setOpenExternalLinks(True)  # 点文字即开浏览器
                elif key == "local_path":
                    # 本地位置做成链接：点 = 发打开文件夹请求。
                    # 不开 openExternalLinks（那是开浏览器的），走
                    # linkActivated 转既有信号——动作实现仍单源在
                    # gui/modFolderOpener，面板零实现
                    val.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                        | Qt.TextInteractionFlag.LinksAccessibleByMouse)
                    val.linkActivated.connect(
                        lambda _u: self._emit_open_folder())
                elif key == "deps":
                    # 依赖入口行（明细住子页）：链接走 linkActivated
                    # （href = 字面量 "deps"），绝不开 openExternalLinks
                    val.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextSelectableByMouse
                        | Qt.TextInteractionFlag.LinksAccessibleByMouse)
                    val.linkActivated.connect(
                        lambda _u: self._open_deps_tab())

                self._form.addRow(name, val)
                self._vals[key] = val

        wrap = QScrollArea(page)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.Shape.NoFrame)
        wrap.setWidget(body)
        pv.addWidget(wrap)
        self._stack.addWidget(page)
        # —— 栈页 2：依赖页（与基本信息并列；明细唯一形态）——
        # 2026-10 改判：页 1 折叠卡片撤出，全部能力迁到本页——
        # ① 表格 6 列（勾选记编号不记行号）；② 状态五色；③ 右键
        # 三段式菜单；④「被谁依赖」区块（反向路标，点 = 跳转）；
        # ⑤ 速览折叠面板（单击行展开、再点同一行或「收起 ▲」收起）。
        # 渲染走 _render_dep_table 单源。
        deps_page = QWidget(self)
        dv = QVBoxLayout(deps_page)
        dv.setContentsMargins(10, 8, 10, 8)
        dv.setSpacing(6)
        dep_head = QHBoxLayout()
        self._deps_title = QLabel("", deps_page)
        self._deps_title.setWordWrap(True)
        self._deps_title.setStyleSheet("font-weight: 600;")
        dep_head.addWidget(self._deps_title, 1)
        dv.addLayout(dep_head)
        self._deps_hint = QLabel("", deps_page)
        self._deps_hint.setWordWrap(True)
        self._deps_hint.setStyleSheet(
            f"color: {_DIM}; font-size: {font_px(11)}px;")
        dv.addWidget(self._deps_hint)
        # 速览折叠面板（默认收起；单击表格行展开）
        dv.addWidget(self._build_dep_mini(deps_page))
        self._deps_table = QTableWidget(0, 6, deps_page)
        self._deps_table.setHorizontalHeaderLabels(
            ["勾", "编号", "名称", "状态", "大小", "远端更新"])
        self._deps_table.verticalHeader().setVisible(False)
        self._deps_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._deps_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._deps_table.setWordWrap(False)
        _dh = self._deps_table.horizontalHeader()
        _dh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        _dh.setSectionsClickable(False)
        for col, width in ((0, 34), (1, 100), (3, 90), (4, 80), (5, 90)):
            self._deps_table.setColumnWidth(col, width)
        self._deps_table.itemClicked.connect(self._on_dep_row_clicked)
        self._deps_table.itemDoubleClicked.connect(self._on_dep_dclicked)
        self._deps_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._deps_table.customContextMenuRequested.connect(
            self._on_deps_menu)
        dv.addWidget(self._deps_table, 1)
        rev_head = QLabel("被以下 mod 依赖（账本内；点 = 跳过去）：",
                          deps_page)
        rev_head.setStyleSheet("font-weight: 600;")
        dv.addWidget(rev_head)
        # 被依赖区：一行一个依赖者（每行 = 可点链接 + 状态词着色），
        # 装进滚动区并限高——Harmony 这类基础库会被上百个 mod 依赖，
        # 不限高整页被撑爆、上方依赖表被挤没（2026-10 用户点破）
        self._dependents_scroll = QScrollArea(deps_page)
        self._dependents_scroll.setWidgetResizable(True)
        self._dependents_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._dependents_box = QWidget(self._dependents_scroll)
        self._dependents_lay = QVBoxLayout(self._dependents_box)
        self._dependents_lay.setContentsMargins(12, 0, 0, 0)
        self._dependents_lay.setSpacing(2)
        self._dependents_lay.addStretch(1)  # 行少时内容靠上，不被拉伸
        self._dependents_scroll.setWidget(self._dependents_box)
        dv.addWidget(self._dependents_scroll)

        dep_acts = QHBoxLayout()
        self._btn_deps_fetch = QPushButton("查远端信息…", deps_page)
        self._btn_deps_fetch.setToolTip(_FETCH_TIP)
        self._btn_deps_fetch.clicked.connect(self._on_fetch_dep_meta)
        dep_acts.addWidget(self._btn_deps_fetch)
        self._btn_deps_copy = QPushButton("复制全部编号", deps_page)
        self._btn_deps_copy.setToolTip(
            "依赖清单的工坊编号逐行复制（每行一个）——可贴进"
            "【入账中心 · 登记】收录，或【依赖检测】页对表")
        self._btn_deps_copy.clicked.connect(self._copy_dep_ids)
        dep_acts.addWidget(self._btn_deps_copy)
        self._btn_deps_send = QPushButton("送登记账外编号…", deps_page)
        self._btn_deps_send.setToolTip(_SEND_TIP)
        self._btn_deps_send.clicked.connect(self._send_outdeps_register)
        dep_acts.addWidget(self._btn_deps_send)
        dep_acts.addStretch(1)
        dv.addLayout(dep_acts)
        self._pg_deps = self._stack.addWidget(deps_page)  # 页号（=2）

        # 描述页（栈页 3）：刀2/刀3 正身——原文/译文/对照三态 + 翻译
        # 按钮（翻译/描述合一页签，页内切换；原「翻译」页签撤销）
        self._pg_desc = self._stack.addWidget(self._build_desc_page())

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
    def set_download_dir(self, base: str | None) -> None:
        """档案下载目录（推导"本地位置"用），切档案时由 mod 库页喂入。
        路径规则与 gui/modFolderOpener 同源：下载目录\\编号。"""
        self._download_dir = (base or "").strip() or None

    def show_mod(self, mod) -> None:
        """显示一个 mod；传 None = 回到未选中占位。"""
        self._mod = mod
        if mod is None:
            self._stack.setCurrentIndex(0)
            self._tabs.setEnabled(False)
            return
        # 切 mod 时若正停在子页（依赖/描述/翻译）→ 原位重灌：依赖行
        # 双击 → 库页定位选中 → show_mod 回到这里，形成"沿依赖链
        # 连逛"；页签随时切换
        staying = self._stack.currentIndex() in (self._pg_deps, self._pg_desc)

        if not staying:
            self._stack.setCurrentIndex(1)
        self._tabs.setEnabled(True)

        title = mod.title or "（无标题）"
        self._title.setText(title)
        self._title.setToolTip(title)
        v = self._vals

        # —— 基本档案 ——
        v["title"].setText(title)
        v["mod_id"].setText(str(mod.mod_id))
        v["status"].setText(status_zh(mod.status))
        v["game_id"].setText(str(mod.game_id))
        v["creator"].setText(getattr(mod, "creator", "") or "—")
        v["url"].setText(self._link(mod.url, mod.mod_id))

        v["time_created"].setText(self._ts(mod.time_created))
        v["first_tracked_at"].setText(self._ts(mod.first_tracked_at))
        v["local_path"].setText(self._local_path_text(mod.mod_id))


        # —— 版本对照 ——
        cv = getattr(mod, "confirmed_version", None)
        source = getattr(mod, "confirmed_source", "") or ""
        if cv:
            word = CONFIRMED_SOURCE_ZH.get(source, source) or "—"
            v["confirmed"].setText(
                f"{relative_time(cv)}（{abs_time(cv)}）· {word}")
        else:
            v["confirmed"].setText("未知")
        if cv is None:
            tip = ("还没有确认过本地版本：批次收尾清单里勾选确认，"
                   "或右键「设定本地版本…」手工认定。")
        else:
            tip = _SOURCE_NOTES.get(source, "")
        self._src_note.setText(tip)
        self._src_note.setVisible(bool(tip))
        v["confirmed_at"].setText(self._ts(mod.confirmed_at))
        v["local_size"].setText(self._size(mod.local_size))
        rt = getattr(mod, "time_updated", None)
        v["remote"].setText(
            f"{relative_time(rt)}（{abs_time(rt)}）" if rt else "从未检测")
        v["last_time_updated"].setText(self._ts(mod.last_time_updated))
        v["file_size"].setText(self._size(mod.file_size))
        v["last_checked_at"].setText(self._ts(mod.last_checked_at))

        # —— Steam 数据 ——
        for key in ("subscriptions", "favorited", "views"):
            num = getattr(mod, key, None)
            v[key].setText("—" if num is None else f"{num:,}")
        tags = getattr(mod, "tags", None) or []
        v["tags"].setText(" ".join(tags) if tags else "—")
        v["preview_url"].setText(self._link(mod.preview_url))

        # —— 标记与备注 ——
        v["is_special"].setText("★ 是" if mod.is_special else "否")
        v["color_tag"].setText(getattr(mod, "color_tag", "") or "—")
        v["note"].setText(getattr(mod, "note", "") or "—")

        # —— 软删除 ——
        v["deleted_at"].setText(self._ts(mod.deleted_at))
        state = getattr(mod, "deleted_last_state", None)
        v["deleted_last_state"].setText(
            json.dumps(state, ensure_ascii=False) if state else "—")

        self._fill_verdicts(mod.mod_id)
        # —— 依赖（必需物品）——入口行摘要；明细卡片与子页随时重灌
        # （卡片可能正展开着：切 mod 原位刷新，数据不串档案）
        v["deps"].setText(self._deps_entry(mod))
        self._fill_deps_page(mod)
        self._fill_description(mod)



        self._sync_tabs()

    # ---------- 内部 ----------
    @staticmethod
    def _link(url: str | None, mod_id: int | None = None) -> str:
        """URL 字段 → 可点击链接（文字仍是全文，照旧可选中复制）。
        工坊链接允许空：mod_id 给了就按 urlParser 模板现拼
        （决策 61④ 模板单源——编号→网址是确定规则，认领/手动设定
        入账的条目没存 url 也能给出链接）；preview_url 没有现拼规则，
        空就还是 —（缩略图显示在候选池①）。"""
        u = (url or "").strip()
        if not u and mod_id is not None:
            u = workshop_url(mod_id)
        return f'<a href="{u}">{u}</a>' if u else "—"
    def _local_path_text(self, mod_id: int) -> str:
        """本地位置 = 下载目录\\编号（显示侧推导，不入库）。做成链接：
        点 = 打开文件夹；不在盘上照样显示，点了一样发请求、
        opener 会就地说明。"""
        if not self._download_dir:
            return "—（档案还没设下载目录）"
        p = Path(self._download_dir) / str(mod_id)
        text = f"{p}（{'在盘上' if p.is_dir() else '不在盘上'}）"
        return f'<a href="open">{text}</a>'

    def _emit_open_folder(self) -> None:
        """本地位置链接点击：转既有 open_folder_requested 信号。"""
        if self._mod is not None:
            self.open_folder_requested.emit(self._mod.mod_id)


    @staticmethod
    def _ts(value) -> str:
        """时间戳 → 可读值（原始值并列）；空 → —。"""
        return f"{abs_time(value)}（{value}）" if value else "—"

    @staticmethod
    def _size(value) -> str:
        return f"{fmt_size(value)}（{value}）" if value else "—"

    # ---------- 依赖（必需物品）：入口行 + 折叠卡片 + 子页 ----------

    def _deps_state(self, mod) -> tuple[str, list[int]]:
        """依赖三态判定（口径单源：入口行、卡片表与子页表共用）。
        契约口径 list_dependencies 对"从未拉取"与"确无依赖"同返 []
        （契约原文），单靠它分不出两态——用全局拉取时间兜底（零契约
        改动）：- "none" 未获取：全账本从没成功拉取过
        （latest_dependency_fetch 为 None）——此时任何 mod 都必然没拉
        过；查询异常同按未获取；- "empty" 无依赖：全局拉取过而本条
        清单为空；- "list"：有清单。多档案下全局口径分不清哪个档案拉
        的：B 档案拉过后，没拉过的 A 档案条目会误报"无依赖"——子页
        空态里已披露；根治（每 mod 拉取戳）在改善项池。"""
        try:
            deps = self._repo.list_dependencies(mod.mod_id)
        except Exception:
            return "none", []
        try:
            ever = self._repo.latest_dependency_fetch() is not None
        except Exception:
            ever = False
        if not deps and not ever:
            return "none", []
        if not deps:
            return "empty", []
        return "list", deps

    def _build_dep_rows(self, deps: list[int]) -> list[tuple]:
        """编号清单 → 行数据（渲染单源：入口行计数、卡片表、子页表
        三处都从这份行数据取数，口径绝不两份）。
        行 = (编号, 名称, 状态词, 状态色, 大小, 远端更新, 账内?, 悬浮)。
        状态五色：绿=已下载｜红=账上没下载｜黄=已删除（可恢复）｜
        蓝=远端在架（查过回执）｜灰=账外未查/远端查无。
        「查远端信息」回执（_dep_meta，内存不入账）补显示缺口：
        账外条目以回执定蓝/灰并带上大小与远端更新；账内"未下载"
        条目账本字段缺失（从没检测过）时用回执补大小/远端更新，
        状态词仍按账本——回执是显示借的米，绝不冒充账本数据。"""
        rows: list[tuple] = []
        meta = self._dep_meta
        for d in sorted(deps):
            m = self._repo.get_mod(d)
            if m is None:
                it = meta.get(d)
                if it is not None and it.get("result") == 1:
                    rows.append((
                        d, it.get("title") or "—", "远端在架",
                        QColor("#5aa9ff"), fmt_size(it.get("file_size")),
                        relative_time(it["time_updated"])
                        if it.get("time_updated") else "—",
                        False,
                        "账外条目：远端在架（匿名批查回执，未入账）——"
                        "要收录请送【入账中心 · 登记】"))
                elif it is not None:
                    rows.append((
                        d, "—", "远端查无", QColor("#8a8a8f"),
                        "—", "—", False,
                        "账外条目：Steam 远端查无此条（可能已下架）——"
                        "这是接口的正常回答，不是错误"))
                else:
                    rows.append((
                        d, "—", "账外·未查", QColor("#8a8a8f"),
                        "—", "—", False,
                        "账外条目：本地无从得知远端状态——点"
                        "【查远端信息】补查"))
                continue
            if m.status == STATUS_DOWNLOADED:
                rows.append((
                    d, m.title or "—", "已下载", QColor("#46a758"),
                    fmt_size(m.local_size or m.file_size),
                    relative_time(m.time_updated)
                    if m.time_updated else "—",
                    True, "已下载：双击跳到该 mod 的详情"))
            elif m.status == STATUS_DELETED:
                rows.append((
                    d, m.title or "—", "已删除", QColor("#c8a03c"),
                    fmt_size(m.local_size or m.file_size),
                    relative_time(m.time_updated)
                    if m.time_updated else "—",
                    True, "已删除（可恢复）：双击跳到该 mod 的详情"))
            else:
                # tracked / failed：账上在管、本地缺失。账本没记的
                # 远端信息用回执补显示（tooltip 标明来源）
                it = meta.get(d)
                live = it is not None and it.get("result") == 1
                size = (fmt_size(m.file_size) if m.file_size
                        else fmt_size(it.get("file_size")) if live else "—")
                if m.time_updated:
                    upd = relative_time(m.time_updated)
                elif live and it.get("time_updated"):
                    upd = relative_time(it["time_updated"])
                else:
                    upd = "—"
                tip = "账上在管、本地缺失：双击跳到该 mod 的详情"
                if m.time_updated is None and live:
                    tip += "（远端更新时间来自匿名批查回执——账本还没检测过）"
                rows.append((d, m.title or "—", "未下载",
                             QColor("#e5484d"), size, upd, True, tip))
        return rows

    def _deps_entry(self, mod) -> str:
        """页 1 依赖入口行（明细、被谁依赖与速览都住「依赖」页签，
        本行只做摘要+入口）。未获取/无依赖 = 灰色短词（详细说明在
        依赖页空态）。被依赖数独立于正向清单——本条没拉过依赖 ≠
        没人依赖它。查过远端后账外条目在摘要里拆出「在架/查无/未查」
        三数。"""
        state, deps = self._deps_state(mod)
        n_rev = len(self._dependents_map().get(mod.mod_id, []))
        rev = f" · 被依赖 {n_rev}"
        if state == "none":
            return ('<span style="color:#8a8a8f">未获取——到'
                    f"【依赖检测】页拉取后入账{rev}</span>")
        if state == "empty":
            return f'<span style="color:#8a8a8f">无依赖{rev}</span>'
        counts: dict[str, int] = {}
        for x in self._build_dep_rows(deps):
            counts[x[2]] = counts.get(x[2], 0) + 1
        parts = [f"本地 {counts.get('已下载', 0)}",
                 f"缺 {counts.get('未下载', 0)}",
                 f"已删 {counts.get('已删除', 0)}"]
        n_live = counts.get('远端在架', 0)
        n_dead = counts.get('远端查无', 0)
        n_unk = counts.get('账外·未查', 0)
        if n_live or n_dead or n_unk:
            parts.append(f"账外 在架{n_live}/查无{n_dead}/未查{n_unk}")
        return (f"{len(deps)} 个（{' · '.join(parts)}）{rev} "
                '<a href="deps">打开依赖页 →</a>')

    def _open_deps_tab(self) -> None:
        """依赖入口行链接落点：切到与基本信息并列的「依赖」页签
        （栈页 2）——明细、被谁依赖与速览折叠面板都住在那边；
        页 1 只留摘要（2026-10 改判：卡片撤出页 1）。"""
        self._stack.setCurrentIndex(self._pg_deps)
        self._sync_tabs()
    def _build_dep_mini(self, parent) -> QFrame:
        """依赖页内置速览折叠面板（2026-10 从页 1 卡片迁来）：单击
        依赖行展开 = 就地看该依赖的关键信息（含自身依赖与被谁依赖，
        不跳页）；「收起 ▲」或再点同一行收起——折叠在整页高度下
        终于有了正经用法。账内取账本字段，账外取【查远端信息】回执。"""
        f = QFrame(parent)
        self._dep_mini_frame = f  # 点击收展三处都引用它——上轮漏存，单击即炸

        f.setFrameShape(QFrame.Shape.StyledPanel)
        mv = QVBoxLayout(f)
        mv.setContentsMargins(6, 4, 6, 6)
        mv.setSpacing(4)
        top = QHBoxLayout()
        self._mini_title = QLabel("", f)
        self._mini_title.setWordWrap(True)
        self._mini_title.setStyleSheet("font-weight: 600;")
        top.addWidget(self._mini_title, 1)
        self._btn_mini_fold = QPushButton("收起 ▲", f)
        self._btn_mini_fold.setToolTip("收起速览（依赖清单原位不动，"
                                       "再点同一行可再展开）")
        self._btn_mini_fold.clicked.connect(self._hide_dep_mini)
        top.addWidget(self._btn_mini_fold)
        mv.addLayout(top)
        form_w = QWidget(f)
        mf = QFormLayout(form_w)
        mf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        mf.setContentsMargins(2, 0, 2, 0)
        self._mini_vals: dict[str, QLabel] = {}
        for key, zh in (("mid", "编号"), ("status", "状态"),
                        ("local", "本地版本"), ("remote", "远端版本"),
                        ("size", "大小"), ("checked", "上次检测"),
                        ("tags", "标签"), ("subs", "订阅 · 收藏"),
                        ("subdeps", "自身依赖"), ("revdeps", "被谁依赖"),
                        ("note", "备注")):
            name = QLabel(zh, form_w)
            val = QLabel("—", form_w)
            val.setWordWrap(True)
            if key in ("subdeps", "revdeps"):
                # 依赖链接：点了转跳（href = 编号，绝不开浏览器）
                val.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                    | Qt.TextInteractionFlag.LinksAccessibleByMouse)
                val.linkActivated.connect(self._on_dependent_link)
            else:
                val.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse)
            mf.addRow(name, val)
            self._mini_vals[key] = val
        mv.addWidget(form_w)
        acts2 = QHBoxLayout()
        self._btn_mini_open = QPushButton("在库页打开 →", f)
        self._btn_mini_open.setToolTip(
            "真正跳到该 mod 的库页详情（左表定位选中）；"
            "想沿依赖链连逛时用")
        self._btn_mini_open.clicked.connect(self._on_mini_open)
        acts2.addWidget(self._btn_mini_open)
        self._btn_mini_web = QPushButton("打开工坊网页", f)
        self._btn_mini_web.setToolTip("在浏览器打开该编号的工坊页面")
        self._btn_mini_web.clicked.connect(self._on_mini_web)
        acts2.addWidget(self._btn_mini_web)
        self._btn_mini_send = QPushButton("送登记…", f)
        self._btn_mini_send.setToolTip(_SEND_TIP)
        self._btn_mini_send.clicked.connect(self._on_mini_send)
        acts2.addWidget(self._btn_mini_send)
        acts2.addStretch(1)
        mv.addLayout(acts2)
        f.setVisible(False)  # 默认收起：单击依赖行展开
        return f


    # ---- 被谁依赖（反向依赖；懒构建 + 缓存）----

    def _dependents_map(self) -> dict[int, list[int]]:
        """反向依赖图（缓存）：依赖编号 → [账本内依赖它的 mod 编号]。
        repo 契约没有反向查询接口——GUI 层遍历当前档案全部条目的
        依赖清单来建（零契约改动）；缓存键 = (档案,
        latest_dependency_fetch)，依赖数据一变（任何一次拉取落库）
        或切档案即重建，不会用旧图。成本：首次 ~每条一次轻查询
        （几百条量级 <0.2s、一次性），之后 O(1)。根治（repo 一条
        SQL 原生反向查询）在改善项池，档案量级上来再动账本。"""
        mod = self._mod
        if mod is None:
            return {}
        app_id = getattr(mod, "game_id", None)
        if app_id is None:
            return {}
        try:
            ts = self._repo.latest_dependency_fetch()
        except Exception:
            ts = None
        key = (app_id, ts)
        if self._dep_graph is not None and self._dep_graph[0] == key:
            return self._dep_graph[1]
        graph: dict[int, list[int]] = {}
        try:
            for m in self._repo.list_mods(app_id):
                for d in self._repo.list_dependencies(m.mod_id):
                    graph.setdefault(int(d), []).append(m.mod_id)
        except Exception:
            graph = {}  # 查不动就给空图：显示层缺米好过整页查死
        self._dep_graph = (key, graph)
        return graph

    def _dependents_html(self, mid: int) -> str:
        """被依赖清单 → 富文本（速览面板「被谁依赖」行用）：一行一个，
        截前 6 行 + 总数——全表就在同一页下方「被以下 mod 依赖」
        区块（Harmony 这类被上百条依赖的不至于把速览撑爆）。空态
        如实披露：依赖数据只覆盖"拉取过依赖"的条目。"""
        ids = sorted(self._dependents_map().get(mid, []))
        if not ids:
            return ('<span style="color:#8a8a8f">（账本内暂无条目依赖'
                    "它——依赖数据只覆盖拉取过依赖的条目，没拉过的"
                    "不算）</span>")
        parts: list[str] = []
        for d in ids[:6]:
            m = self._repo.get_mod(d)
            title = (m.title if m is not None and m.title else str(d))
            parts.append(f'<a href="{d}">{title}（{d}）</a>')
        if len(ids) > 6:
            parts.append(f'<span style="color:{_DIM}">…共 {len(ids)} 个'
                         "（全表见下方区块）</span>")
        return "<br>".join(parts)

    def _fill_dependents(self) -> None:
        """被依赖区重灌（一行一个依赖者）。空态一行灰字如实披露：
        依赖数据只覆盖"拉取过依赖"的条目——没拉过的 mod 的依赖
        关系账本不知道，「暂无」不是「绝对没有」。行多（如 Harmony
        被上百条依赖）= 滚动区封顶内部滚，整页留给依赖表。"""
        lay = self._dependents_lay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        mod = self._mod
        if mod is None:
            self._dependents_scroll.hide()
            return
        self._dependents_scroll.show()
        ids = sorted(self._dependents_map().get(mod.mod_id, []))
        if not ids:
            empty = QLabel("（账本内暂无条目依赖它——依赖数据只覆盖"
                           "拉取过依赖的条目，没拉过的不算）",
                           self._dependents_box)
            empty.setStyleSheet(f"color: {_DIM};")
            empty.setWordWrap(True)
            lay.addWidget(empty)
        else:
            for d in ids:
                m = self._repo.get_mod(d)
                title = (m.title if m is not None and m.title else str(d))
                if m is not None:
                    word = status_zh(m.status)
                    color = self._mini_status_color(word)
                else:
                    word, color = "—", "#8a8a8f"
                row = QLabel(
                    f'<a href="{d}">{title}（{d}）</a>'
                    f' · <span style="color:{color}">{word}</span>',
                    self._dependents_box)
                row.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                    | Qt.TextInteractionFlag.LinksAccessibleByMouse)
                # href 是编号不是网址：绝不 openExternalLinks
                row.linkActivated.connect(self._on_dependent_link)
                lay.addWidget(row)
        lay.addStretch(1)  # 清空时连 stretch 一起被清了，补回
        # 高度贴内容、有界 28~160px：行少不占冤枉地；行多封顶内部滚
        content = 2 * self._dependents_scroll.frameWidth()
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                content += w.sizeHint().height() + lay.spacing()
        self._dependents_scroll.setFixedHeight(
            min(max(content, 28), 160))

    def _on_dependent_link(self, href: str) -> None:
        """被依赖清单链接点击 → 库页定位选中。正向表 + 反向表 =
        双向路标：跳到依赖者后，它卡片里的「被谁依赖」就是回程路。"""
        try:
            mid = int(href)
        except ValueError:
            return
        self.mod_link_requested.emit(mid)
    # ---- 依赖速览（详情页的详情页）：单击行就地看，不跳页 ----

    @staticmethod
    def _mini_status_color(word: str) -> str:
        """速览状态词的配色（与 _build_dep_rows 五色同拍）。"""
        if word in ("已下载", "远端在架"):
            return "#46a758" if word == "已下载" else "#5aa9ff"
        if word == "已删除":
            return "#c8a03c"
        if word == "远端查无":
            return "#8a8a8f"
        return "#e5484d"  # 未下载 / 账外·未查 一并红示（缺米的状态）
    def _on_dep_row_clicked(self, item) -> None:
        """单击依赖行 → 展开该依赖的速览折叠面板（不跳页）。
        再点同一行 = 收起（折叠开关）；点别行 = 换内容；勾选列
        （列 0）的点击只管勾选不展开；双击仍是转跳。"""
        if item is None or item.column() == 0:
            return
        t = self.sender()
        if not isinstance(t, QTableWidget):
            return
        it = t.item(item.row(), 1)
        mid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if not mid:
            return
        mid = int(mid)
        if (self._dep_mini_frame.isVisible()
                and getattr(self, "_mini_mid", None) == mid):
            self._hide_dep_mini()  # 同一行再点 = 收起
            return
        self._show_dep_mini(mid)

    def _dep_chain_html(self, deps: list[int]) -> str:
        """依赖编号清单 → 前几个的可点链接串（速览「自身依赖」用；
        点 = 转跳到该 mod 的库页详情——面板内依赖链接同一口径）。"""
        names: list[str] = []
        for d in sorted(deps)[:6]:
            dm = self._repo.get_mod(d)
            t = dm.title if dm is not None and dm.title else str(d)
            names.append(f'<a href="{d}">{t}（{d}）</a>')
        tail = f" …共 {len(deps)} 个" if len(deps) > 6 else ""
        return ("、".join(names) + tail) if names else "—"

    def _show_dep_mini(self, mid: int) -> None:
        """速览装配（单击依赖行触发，不跳页）：账内取账本全字段，
        并补「自身依赖」（它还要装什么——转跳前先知道这条链有多长，
        2026-10 补上的缺口）与「被谁依赖」（双向路标）；账外取
        【查远端信息】回执——没查过的给指路。"""
        self._mini_mid = mid
        v = self._mini_vals
        m = self._repo.get_mod(mid)
        if m is not None:
            self._mini_title.setText(m.title or f"（无标题）· {mid}")
            v["mid"].setText(str(mid))
            word = status_zh(m.status)
            v["status"].setText(word)
            v["status"].setStyleSheet(
                f"color: {self._mini_status_color(word)}; font-weight: 600;")
            cv = getattr(m, "confirmed_version", None)
            if cv:
                src = getattr(m, "confirmed_source", "") or ""
                w2 = CONFIRMED_SOURCE_ZH.get(src, src) or "—"
                v["local"].setText(
                    f"{relative_time(cv)}（{abs_time(cv)}）· {w2}")
            else:
                v["local"].setText("未知")
            rt = getattr(m, "time_updated", None)
            v["remote"].setText(
                f"{relative_time(rt)}（{abs_time(rt)}）"
                if rt else "从未检测")
            v["size"].setText(
                f"本地 {fmt_size(m.local_size) if m.local_size else '—'}"
                f" · 远端 {fmt_size(m.file_size) if m.file_size else '—'}")
            v["checked"].setText(self._ts(m.last_checked_at))
            tags = getattr(m, "tags", None) or []
            v["tags"].setText(" ".join(tags) if tags else "—")
            sub = getattr(m, "subscriptions", None)
            fav = getattr(m, "favorited", None)
            v["subs"].setText(
                f"{sub:,} · 收藏 {fav:,}"
                if sub is not None and fav is not None else "—")
            # —— 自身依赖（它自己还要装什么）——
            st2, sub_deps = self._deps_state(m)
            if st2 == "list":
                v["subdeps"].setText(self._dep_chain_html(sub_deps))
            elif st2 == "empty":
                v["subdeps"].setText("无依赖")
            else:
                v["subdeps"].setText(
                    '<span style="color:#8a8a8f">未拉取过它的依赖——'
                    "到【依赖检测】页拉取后可见</span>")
            # —— 被谁依赖（双向路标）——
            v["revdeps"].setText(self._dependents_html(mid))
            note = getattr(m, "note", "") or ""
            v["note"].setText(
                (note[:80] + "…") if len(note) > 80 else (note or "—"))
            v["note"].setToolTip(note or "")
            self._btn_mini_open.setEnabled(True)
            self._btn_mini_send.setEnabled(False)  # 账内送登记=白送
        else:
            it = self._dep_meta.get(mid)
            if it is not None and it.get("result") == 1:
                self._mini_title.setText(
                    it.get("title") or f"（账外条目）· {mid}")
                v["status"].setText("远端在架（未入账）")
                v["status"].setStyleSheet(
                    "color: #5aa9ff; font-weight: 600;")
                v["remote"].setText(
                    relative_time(it["time_updated"])
                    if it.get("time_updated") else "—")
                v["size"].setText(
                    f"远端 {fmt_size(it.get('file_size'))}"
                    if it.get("file_size") else "远端 —")
                v["note"].setText(
                    "Steam 远端有此条、本地账本未收录——要收录点下方"
                    "【送登记】，或回清单双击开工坊网页看看。")
            elif it is not None:
                self._mini_title.setText(f"（账外条目）· {mid}")
                v["status"].setText("远端查无")
                v["status"].setStyleSheet(
                    "color: #8a8a8f; font-weight: 600;")
                v["note"].setText(
                    "Steam 远端查无此条（可能已下架）——接口的正常"
                    "回答，不是错误。")
            else:
                self._mini_title.setText(f"（账外条目）· {mid}")
                v["status"].setText("账外 · 未查")
                v["status"].setStyleSheet(
                    "color: #8a8a8f; font-weight: 600;")
                v["note"].setText(
                    "本地无远端信息——回清单点【查远端信息】补查，"
                    "或双击开工坊网页看。")
            v["local"].setText("—")
            v["checked"].setText("—")
            v["tags"].setText("—")
            v["subs"].setText("—")
            v["subdeps"].setText("—（账本没有它的依赖数据）")
            v["revdeps"].setText(self._dependents_html(mid))
            v["note"].setToolTip("")
            self._btn_mini_open.setEnabled(False)
            self._btn_mini_send.setEnabled(True)
        self._dep_mini_frame.setVisible(True)

    def _hide_dep_mini(self) -> None:
        """速览收起：面板藏起，依赖清单原位不动（再点行可再展开）。"""
        if getattr(self, "_dep_mini_frame", None) is not None:
            self._dep_mini_frame.setVisible(False)

    def _on_mini_open(self) -> None:
        if getattr(self, "_mini_mid", None):
            self.mod_link_requested.emit(self._mini_mid)

    def _on_mini_web(self) -> None:
        if getattr(self, "_mini_mid", None):
            QDesktopServices.openUrl(QUrl(workshop_url(self._mini_mid)))

    def _on_mini_send(self) -> None:
        if getattr(self, "_mini_mid", None):
            self.send_to_register_requested.emit([self._mini_mid])

    def _render_dep_table(self, t: QTableWidget, deps: list[int]) -> None:
        """依赖表渲染单源：卡片表与子页表共用（勾选记编号不记行号
        ——重建表格后勾选不掉，站规）。行数据来自 _build_dep_rows。"""
        t.setRowCount(0)
        if not deps:
            return
        rows = self._build_dep_rows(deps)
        t.setRowCount(len(rows))
        for r, (mid, name, st, color, size, upd, in_ledger, tip) \
                in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, mid)
            c1 = QTableWidgetItem(str(mid))
            c1.setData(Qt.ItemDataRole.UserRole, mid)
            cells = (chk, c1, QTableWidgetItem(name),
                     QTableWidgetItem(st), QTableWidgetItem(size),
                     QTableWidgetItem(upd))
            cells[3].setForeground(color)
            for c, cell in enumerate(cells):
                t.setItem(r, c, cell)
            t.item(r, 0).setToolTip(tip)
            t.item(r, 1).setToolTip(
                ("双击跳到该 mod 的详情" if in_ledger
                 else "账外条目：双击打开工坊网页") + "\n" + tip)

    def _on_tab_changed(self, idx: int) -> None:
        """页签点击 → 切栈页。"""
        self._stack.setCurrentIndex(self._TAB_TO_PAGE.get(idx, 1))

    def _sync_tabs(self) -> None:
        """栈页 → 页签回写（程序性换页后调用）；屏蔽信号防回环。
        栈页 0（无选中）没有对应页签，跳过。"""
        tab = self._PAGE_TO_TAB.get(self._stack.currentIndex())
        if tab is None:
            return
        self._tabs.blockSignals(True)
        self._tabs.setCurrentIndex(tab)
        self._tabs.blockSignals(False)
    def _fill_deps_page(self, mod) -> None:
        """依赖页（栈页 2）重灌：表格 + 「被谁依赖」+ 按钮统一摆正；
        速览面板顺手收起（切 mod / 查远端回执重灌都走这里，防旧
        内容串档）。空态说明住这里；渲染走 _render_dep_table 单源。"""
        self._hide_dep_mini()
        state, deps = self._deps_state(mod)
        title = mod.title or "（无标题）"
        self._deps_title.setText(
            f"依赖 · {title}"
            + (f"（{len(deps)} 个）" if state == "list" else ""))
        self._render_dep_table(self._deps_table,
                               deps if state == "list" else [])
        self._fill_dependents()

        if state != "list":
            self._deps_hint.setText(
                "还没拉取过依赖——到【依赖检测】页拉取一次，清单就会"
                "记在这里。" if state == "none" else
                "最近一次拉取时该 mod 没有必需物品。多档案账本里若"
                "本档案还没拉取过（是别的档案拉的），这里可能是"
                "「未拉取」——本档案拉取一遍即明确。已入账的条目双击"
                "可在 mod 库页内转跳。")
            self._sync_dep_actions(0, 0)
            return
        self._deps_hint.setText(
            "绿=本地有｜红=账上没下载｜黄=已删除（可恢复）｜"
            "蓝=远端在架（查过）｜灰=账外未查/远端查无。"
            "单击行 = 就地速览（不跳页，再点同一行收起）；"
            "双击行 = 转跳；右键更多动作；下方「被谁依赖」点 = 跳转。")
        n_out = sum(1 for x in self._build_dep_rows(deps) if not x[6])
        self._sync_dep_actions(len(deps), n_out)
    def _sync_dep_actions(self, n_total: int, n_out: int) -> None:
        """依赖页按钮的启用/文字统一口。查询进行中只禁查询按钮并
        显示进行语——查询对象是"开始那一刻"的清单，切 mod 不打断
        （回执按编号存，跨 mod 无害）。"""
        has = n_total > 0
        busy = self._fetch_worker is not None
        self._btn_deps_fetch.setEnabled(has and not busy)
        self._btn_deps_fetch.setText("查远端信息…" if not busy
                                     else f"正在查询（{n_total} 个）…")
        self._btn_deps_copy.setEnabled(has)
        self._btn_deps_send.setEnabled(n_out > 0)
        self._btn_deps_send.setText(f"送登记账外编号（{n_out}）…" if n_out
                                    else "送登记账外编号…")

    # ---- 查远端信息（匿名批查；与依赖检测页【查远端信息】同哲学）----

    @staticmethod
    def _meta_item_to_entry(item) -> dict:
        """匿名 WorkshopItem → 回执条目（RemoteQueryWorker 的
        to_entry 注入点；只留显示要用的字段）。"""
        return {"mod_id": item.mod_id, "result": item.result,
                "title": item.title, "time_updated": item.time_updated,
                "file_size": item.file_size}

    def _on_fetch_dep_meta(self) -> None:
        """【查远端信息】：对当前依赖清单做一轮匿名批查
        （GetPublishedFileDetails，100/批无需 key）。复用
        RemoteQueryWorker（分批/限流/取消全包）与
        NET_GATE_DEPENDENCIES 一把闸（与依赖拉取互斥）；回执只进
        内存 _dep_meta，不入账——账外编号的归宿是【入账中心 · 登记】，
        账内条目的账本数据照旧（回执只补显示缺口，绝不冒充账本）。"""
        if self._mod is None:
            return
        deps = self._deps_state(self._mod)[1]
        if not deps:
            return
        if self._fetch_worker is not None:
            QMessageBox.information(self, "查远端信息",
                                    "上一轮查询还在进行——等它结束。")
            return
        if self._settings is not None:
            interval = self._settings.get_int("api_request_interval_ms", 200)
            retries = self._settings.get_int("api_max_retries", 3)
        else:
            interval, retries = 200, 3
        from core import netGate
        from core import constants
        busy = netGate.try_acquire(constants.NET_GATE_DEPENDENCIES)
        if busy is not None:
            QMessageBox.information(
                self, "查远端信息",
                f"另有联网任务在进行（{busy}）：等它结束后再查。")
            return

        def make_query(cancel_event):
            from core.steamApiClient import SteamApiClient
            client = SteamApiClient(interval_ms=interval, max_retries=retries,
                                    cancel_event=cancel_event)
            return lambda batch: client.query_details(batch)

        from gui.remoteQueryWorker import RemoteQueryWorker
        self._fetch_worker = RemoteQueryWorker(
            deps, make_query=make_query, to_entry=self._meta_item_to_entry,
            interval_ms=interval, max_retries=retries, parent=self)
        self._fetch_worker.succeeded.connect(self._on_dep_meta_succeeded)
        self._fetch_worker.failed.connect(self._on_dep_meta_failed)
        self._fetch_worker.stopped.connect(self._on_dep_meta_stopped)
        self._fetch_worker.finished.connect(self._on_dep_meta_finished)
        self._fetch_worker.start()
        n_out = sum(1 for x in self._build_dep_rows(deps) if not x[6])
        self._sync_dep_actions(len(deps), n_out)

    def _on_dep_meta_succeeded(self, entries: list) -> None:
        got = {e["mod_id"]: e for e in entries
               if e.get("mod_id") is not None}
        self._dep_meta.update(got)
        n_live = sum(1 for e in got.values() if e.get("result") == 1)
        self._meta_note = (f"远端信息已补：{len(got)} 条（在架 {n_live}、"
                           f"查无 {len(got) - n_live}）——回执只进显示、"
                           "不入账本；账外条目要收录请送【入账中心 · 登记】。")

    def _on_dep_meta_failed(self, message: str) -> None:
        self._meta_note = (f"远端信息查询失败：{message}\n"
                           "（依赖清单不受影响，可稍后再查。）")

    def _on_dep_meta_stopped(self) -> None:
        self._meta_note = "已停止查询（本次回执未使用）。"

    def _on_dep_meta_finished(self) -> None:
        """收尾纪律（0xC0000409 家族口径）：finished 里摘引用 → 置
        None → wait；闸归还挂 finished 必经处。随后统一重灌两表 +
        落回执信息行。"""
        w = self._fetch_worker
        self._fetch_worker = None
        if w is not None:
            w.wait()
        from core import netGate
        from core import constants
        netGate.release(constants.NET_GATE_DEPENDENCIES)
        if self._mod is not None:
            self._fill_deps_page(self._mod)
        note = self._meta_note
        self._meta_note = ""
        if note:
            self._deps_hint.setText(note)

    def shutdown(self) -> None:
        """关窗收尾：查远端信息轮请停（等 1.5 秒，等不到 park 保活
        ——netGate 家族纪律）。由 mod 库页 shutdown 转调
        （MainWindow.closeEvent 的页面循环会先到 modListPage）。"""
        w = getattr(self, "_fetch_worker", None)
        if w is not None:
            w.stop()
            if not w.wait(1500):
                netGate.park(w)
            self._fetch_worker = None
        w2 = getattr(self, "_trans_worker", None)
        if w2 is not None:
            w2.stop()
            if not w2.wait(1500):
                netGate.park(w2)
            self._trans_worker = None


    # ---- 双击 / 勾选 / 右键（D47 三段式）----

    def _on_dep_dclicked(self, item) -> None:
        """依赖行双击（卡片表与子页表共用，sender 取来源表）：账内 →
        发信号让库页定位选中（show_mod 原位重灌 = 沿依赖链连逛）；
        账外 → 开工坊网页。分支按双击那一刻的库内现状态定。"""
        t = self.sender()
        if not isinstance(t, QTableWidget) or item is None:
            return
        it = t.item(item.row(), 1)
        mid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if not mid:
            return
        if self._repo.get_mod(mid) is None:
            QDesktopServices.openUrl(QUrl(workshop_url(mid)))
        else:
            self.mod_link_requested.emit(mid)

    def _checked_dep_ids(self, t: QTableWidget) -> list[int]:
        """勾选行的依赖编号（去重升序）——复制与送登记勾选两路共用。"""
        ids: set[int] = set()
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                rid = it.data(Qt.ItemDataRole.UserRole)
                if rid is not None:
                    ids.add(int(rid))
        return sorted(ids)

    def _set_all_checks(self, t: QTableWidget, checked: bool) -> None:
        state = (Qt.CheckState.Checked if checked
                 else Qt.CheckState.Unchecked)
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None:
                it.setCheckState(state)

    def _copy_checked_dep_ids(self, t: QTableWidget) -> None:
        ordered = self._checked_dep_ids(t)
        if not ordered:
            QMessageBox.information(self, "复制勾选行", "先勾选若干行。")
            return
        QApplication.clipboard().setText(
            "\n".join(str(i) for i in ordered) + "\n")

    def _send_checked_deps_register(self, t: QTableWidget) -> None:
        """勾选行直接送【入账中心 · 登记】（预填+解析预览，不写库）。
        勾了账内编号也无妨——入账中心五桶预览自会归入「已在册」。"""
        ordered = self._checked_dep_ids(t)
        if not ordered:
            QMessageBox.information(self, "送登记", "先勾选若干行。")
            return
        self.send_to_register_requested.emit(ordered)

    def _on_deps_menu(self, pos) -> None:
        """依赖表右键（D47 三段式）：勾选总控 + 单行动作 + 勾选批
        动作。卡片表与子页表共用（sender 取来源表）。"""
        t = self.sender()
        if not isinstance(t, QTableWidget):
            return
        row = t.rowAt(pos.y())
        mid = None
        if row >= 0:
            it = t.item(row, 1)
            mid = it.data(Qt.ItemDataRole.UserRole) \
                if it is not None else None
        menu = QMenu(self)
        a_all = QAction("全选", menu)
        a_all.triggered.connect(lambda: self._set_all_checks(t, True))
        menu.addAction(a_all)
        a_none = QAction("清空勾选", menu)
        a_none.triggered.connect(lambda: self._set_all_checks(t, False))
        menu.addAction(a_none)
        if mid is not None:
            menu.addSeparator()
            act_url = QAction("打开工坊页面", menu)
            act_url.triggered.connect(
                lambda: QDesktopServices.openUrl(QUrl(workshop_url(mid))))
            menu.addAction(act_url)
            act_copy = QAction("复制编号", menu)
            act_copy.triggered.connect(
                lambda: QApplication.clipboard().setText(str(mid)))
            menu.addAction(act_copy)
        menu.addSeparator()
        a3 = QAction("复制勾选行的编号", menu)
        a3.setToolTip("先勾选若干行再点（每行一个）")
        a3.triggered.connect(lambda: self._copy_checked_dep_ids(t))
        menu.addAction(a3)
        a4 = QAction("送登记勾选行…", menu)
        a4.setToolTip("把勾选编号直接送【入账中心 · 登记】预填并解析"
                      "预览（不写库）")
        a4.triggered.connect(lambda: self._send_checked_deps_register(t))
        menu.addAction(a4)
        menu.exec(t.viewport().mapToGlobal(pos))

    def _copy_dep_ids(self) -> None:
        """复制依赖清单全部编号（每行一个）：送【入账中心 · 登记】
        或【依赖检测】页对表的通用入口。"""
        if self._mod is None:
            return
        deps = self._deps_state(self._mod)[1]
        if deps:
            QApplication.clipboard().setText(
                "\n".join(str(i) for i in sorted(deps)) + "\n")

    def _send_outdeps_register(self) -> None:
        """「送登记账外编号」：当前依赖清单里账外（不在账本）的编号
        直接送【入账中心 · 登记】——预填+解析预览不写库，登记决定权
        在人。"""
        if self._mod is None:
            return
        rows = self._build_dep_rows(self._deps_state(self._mod)[1])
        outs = [int(x[0]) for x in rows if not x[6]]
        if not outs:
            QMessageBox.information(self, "送登记",
                                    "清单里没有账外编号——依赖全部在册。")
            return
        self.send_to_register_requested.emit(outs)

    def _fill_verdicts(self, mod_id: int) -> None:
        """最近判决（D22）。repo.list_verdicts 约定返回新 → 旧。
        判决史是展示性信息，取不到不拦详情——但"传错对象"必须露马脚：
        曾实证把 QSplitter 当 repo 传进来，AttributeError 被吞、
        "最近判决"永远暂无（B3）。"""
        if not hasattr(self._repo, "list_verdicts"):
            self._verdicts.setText("（判决史不可用：面板没接到账本仓库）")
            return
        try:
            rows = self._repo.list_verdicts(mod_id)
        except Exception:
            rows = []
        if not rows:
            self._verdicts.setText(
                "（暂无——批次收尾确认、认领、手动设定时各记一条）")
            return
        lines: list[str] = []
        for vrow in rows[:_VERDICT_SHOW]:
            kind_raw = getattr(vrow, "kind", "")
            kind = VERDICT_KIND_ZH.get(str(kind_raw), str(kind_raw) or "?")
            when = getattr(vrow, "occurred_at", None)
            vw = getattr(vrow, "version_written", None)
            note = str(getattr(vrow, "note", "") or "")
            line = f"{abs_time(when)} {kind}"
            if vw:
                line += f" 版本 {abs_time(vw)}"
            if note:
                line += f" {note}"
            lines.append(line)
        self._verdicts.setText("\n".join(lines))
    # ---------- 描述页（刀2/刀3）：三态渲染 + 翻译 ----------
    # 数据源 = mods 表描述四列（刀1 落库）；本页零联网只读账本，唯一
    # 联网动作是「翻译本条」（TranslateWorker + NET_GATE_TRANSLATE 闸）。
    # description_trans 是 JSON 数组（与 text_segments 的 text 块顺序
    # 对齐，null=该段翻译失败），面板只读不写——落库走 translation_ready
    # 信号（写口 A）。

    def _build_desc_page(self) -> QWidget:
        page = QWidget(self)
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self._desc_mode_btns: dict[str, QPushButton] = {}
        for key, label, tip in (
                ("original", "原文",
                 "只看工坊描述原文（BBCode 已转链接行——图片/视频不下载，"
                 "点开浏览器看）"),
                ("translated", "译文", "只看翻译（未翻译时不可用）"),
                ("compare", "对照",
                 "一段原文一段译文对照着看（未翻译时不可用）"),
        ):
            btn = QPushButton(label, page)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)  # 同父互斥 → 单选钮手感
            btn.setToolTip(tip)
            btn.clicked.connect(
                lambda _=False, k=key: self._set_desc_mode(k))
            bar.addWidget(btn)
            self._desc_mode_btns[key] = btn
        bar.addStretch(1)
        self._btn_translate = QPushButton("翻译本条", page)
        self._btn_translate.setToolTip(
            "把描述里的文本段打包发给翻译引擎（微软 Edge 内置通道，无需 "
            "key，通常一两次请求完成；Bing 网页通道降为备选）。BBCode "
            "骨架不动——图片/视频/链接行不参与翻译、原位保留。"
            "与其它联网功能共用互斥闸；翻译中再点一次 = 停止。")

        self._btn_translate.clicked.connect(self._on_translate_clicked)
        bar.addWidget(self._btn_translate)
        v.addLayout(bar)
        # 黄条：原文版本戳落后于远端观测且已有译文（文案静态，一次设死）
        self._desc_stale = QLabel(page)
        self._desc_stale.setWordWrap(True)
        self._desc_stale.setStyleSheet(
            "color: #f5a623; background: rgba(245,166,35,0.12);"
            "border-radius: 4px; padding: 6px 8px;")
        self._desc_stale.setText(
            "⚠ 原文可能已变动——译文基于旧版原文（bump 也可能只更新了"
            "文件没动描述）。【重新翻译】只重译已存的这版原文；要更新"
            "原文本身，跑一轮【更新检测】或【拉取依赖】。")
        self._desc_stale.setVisible(False)
        v.addWidget(self._desc_stale)
        # 一次性事件行（翻译进度 / 结果 / 失败原因），空 = 隐藏
        self._desc_note = QLabel(page)
        self._desc_note.setWordWrap(True)
        self._desc_note.setStyleSheet(
            f"color: {_DIM}; font-size: {font_px(11)}px;")
        self._desc_note.hide()
        v.addWidget(self._desc_note)
        self._desc_browser = QTextBrowser(page)
        # 链接行点开浏览器：显式走 anchorClicked → QDesktopServices
        # 单一路径。setOpenLinks(False) 保证点击一定发信号，不被
        # QTextBrowser 的内部导航吞掉——原 setOpenExternalLinks(True)
        # 依赖属性行为，合并/版本差异下有"看着是链接点了没反应"的坑
        self._desc_browser.setOpenLinks(False)
        self._desc_browser.anchorClicked.connect(self._open_desc_link)

        v.addWidget(self._desc_browser, 1)
        return page
    def _open_desc_link(self, url: QUrl) -> None:
        """描述页链接行点击 → 系统默认浏览器打开。打不开在事件行
        留痕，不再出现"点了没任何反应"的无声失败。"""
        u = url
        if not u.scheme():  # 少见的相对形态，兜成 https
            u = QUrl("https://" + u.toString())
        if not QDesktopServices.openUrl(u):
            self._set_desc_note(
                f"链接打不开：{u.toString()}"
                "（系统没有可用的默认浏览器？）")

    def _set_desc_note(self, text: str) -> None:
        self._desc_note.setText(text)
        self._desc_note.setVisible(bool(text))

    def _desc_stale_state(self, mod) -> bool:
        """黄条判据单源：description_ts ≠ time_updated 且已有译文。
        宁误报勿漏报（bump 未必改了描述）；重译不改原文版本戳，黄条
        在下一轮检测把 description_ts 追上之前一直在——正确语义。"""
        return (mod is not None and bool(self._desc_segs)
                and mod.description_ts is not None
                and mod.time_updated is not None
                and mod.description_ts != mod.time_updated)

    def _fill_description(self, mod) -> None:
        """切 mod 重灌描述页：解析一次缓存，三态切换只是换渲染。
        默认态 = 有译文给「对照」（合一页签的本意），没译文给「原文」。
        坏 JSON 当没翻译，绝不炸面板。"""
        self._set_desc_note("")
        self._desc_blocks = (parse_description(mod.description)
                             if mod is not None else [])
        segs = None
        if mod is not None and mod.description_trans:
            try:
                loaded = json.loads(mod.description_trans)
                segs = loaded if isinstance(loaded, list) else None
            except Exception:
                segs = None
        self._desc_segs = segs
        self._desc_stale.setVisible(self._desc_stale_state(mod))
        has_desc = bool(self._desc_blocks)
        for key, btn in self._desc_mode_btns.items():
            btn.setEnabled(has_desc
                           and (key == "original" or segs is not None))
        self._set_desc_mode(
            "compare" if (has_desc and segs is not None) else "original")
        self._sync_translate_actions()

    def _set_desc_mode(self, mode: str) -> None:
        """三态切换（纯渲染，不重解析）。译文/对照在没译文时守卫回
        原文——按钮禁用之外再拦一道，状态永不自相矛盾。"""
        btn = self._desc_mode_btns.get(mode)
        if btn is not None:
            btn.setChecked(True)
        blocks = getattr(self, "_desc_blocks", [])
        segs = getattr(self, "_desc_segs", None)
        if not blocks:
            self._desc_mode = "original"
            self._desc_browser.setHtml(
                '<span style="color:#8a8a8f">（本条还没有描述原文——'
                "跑一轮【更新检测】后自动带上；本页只读"
                "账本，不联网。）</span>")
            return
        if mode in ("translated", "compare") and segs is None:
            mode = "original"
            self._desc_mode_btns["original"].setChecked(True)
        self._desc_mode = mode
        if mode == "compare":
            self._desc_browser.setHtml(
                blocks_to_html(blocks, translations=segs, compare=True))
        elif mode == "translated":
            self._desc_browser.setHtml(
                blocks_to_html(blocks, translations=segs))
        else:
            self._desc_browser.setHtml(blocks_to_html(blocks))

    # ---- 翻译（刀3）：按钮 = 启动/停止，worker 收尾纪律照旧 ----

    def _sync_translate_actions(self) -> None:
        """翻译按钮统一口：空闲 = 翻译本条/重新翻译…；进行中 = 停止 ⏹
        （再点即停）。描述里真有文本段（text 块）才可译。"""
        has = bool(text_segments(getattr(self, "_desc_blocks", [])))
        if self._trans_worker is not None:
            self._btn_translate.setText("停止 ⏹")
            self._btn_translate.setEnabled(True)
            return
        self._btn_translate.setText(
            "重新翻译…" if self._desc_segs is not None else "翻译本条")
        self._btn_translate.setEnabled(has)

    def _on_translate_clicked(self) -> None:
        if self._trans_worker is not None:  # 进行中再点 = 停止
            self._trans_worker.stop()
            return
        if self._mod is None:
            return
        segs_src = text_segments(self._desc_blocks)
        if not segs_src:
            QMessageBox.information(
                self, "翻译", "这条描述没有可翻译的文本段（只有链接行）。")
            return
        if self._settings is not None:
            interval = self._settings.get_int("api_request_interval_ms", 200)
            retries = self._settings.get_int("api_max_retries", 3)
        else:
            interval, retries = 200, 3
        busy = netGate.try_acquire(NET_GATE_TRANSLATE)
        if busy is not None:
            QMessageBox.information(
                self, "翻译",
                f"另有联网任务在进行（{busy}）：等它结束后再翻译。")
            return
        self._trans_mid = self._mod.mod_id  # 归属：回执只对这条 mod 生效
        self._trans_total = len(segs_src)
        self._trans_done = 0
        self._trans_worker = TranslateWorker(
            segs_src, provider_name="edge", interval_ms=interval,
            max_retries=retries, parent=self)
        self._trans_worker.progress.connect(self._on_trans_progress)
        self._trans_worker.succeeded.connect(self._on_trans_succeeded)
        self._trans_worker.failed.connect(self._on_trans_failed)
        self._trans_worker.stopped.connect(self._on_trans_stopped)
        self._trans_worker.finished.connect(self._on_trans_finished)
        self._trans_worker.start()
        self._sync_translate_actions()
        self._set_desc_note(
            f"翻译中（{self._trans_total} 段，Edge 通道）…")

    def _on_trans_progress(self, done: int, total: int) -> None:
        self._trans_done, self._trans_total = done, total
        self._set_desc_note(f"翻译中（{done}/{total}，Edge 通道）…")

    def _on_trans_succeeded(self, segments: list) -> None:
        """回执落位：内存先行（还是这条 mod 就地重灌），落库交信号
        （写口 A）。emit 无条件发——用户切走了 mod 也照存，译文
        归属发翻译时那条，绝不串到当前显示的条目上。
        三态按钮当场重开 + 自动落对照态：按钮 enable 此前只在
        _fill_description（切 mod 重灌）里做，就地翻译完成时按钮
        仍是灰的（2026-10 用户实测点不动），补在这里。走到本槽
        = 至少一段成功（全灭走 failed 信号），放心全开。"""
        mid = getattr(self, "_trans_mid", None)
        if mid is not None and self._mod is not None \
                and self._mod.mod_id == mid:
            self._desc_segs = list(segments)
            for btn in self._desc_mode_btns.values():
                btn.setEnabled(True)
            self._desc_stale.setVisible(self._desc_stale_state(self._mod))
            self._set_desc_mode("compare")  # 完成即对照——效果最直观
        if mid is not None:
            self.translation_ready.emit(mid, list(segments))
        n_fail = sum(1 for s in segments if s is None)
        self._set_desc_note(
            f"翻译完成：{len(segments)} 段"
            + (f"（{n_fail} 段失败，显示「⚠ 该段翻译失败」+原文）"
               if n_fail else "。"))
        self._sync_translate_actions()

    def _on_trans_failed(self, message: str) -> None:
        self._set_desc_note(f"翻译失败：{message}")

    def _on_trans_stopped(self) -> None:
        self._set_desc_note("已停止翻译（本次未产生译文）。")

    def _on_trans_finished(self) -> None:
        """收尾纪律（netGate 家族）：finished 必发 → 摘引用 → 置 None
        → wait → 闸归还。"""
        w = self._trans_worker
        self._trans_worker = None
        if w is not None:
            w.wait()
        netGate.release(NET_GATE_TRANSLATE)
        self._sync_translate_actions()

    def _on_open_folder(self) -> None:
        if self._mod is not None:
            self.open_folder_requested.emit(self._mod.mod_id)
