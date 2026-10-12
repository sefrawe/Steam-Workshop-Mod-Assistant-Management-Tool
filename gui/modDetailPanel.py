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

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget, QAbstractItemView, QApplication, QHeaderView, QTableWidget, QTableWidgetItem, QHBoxLayout, QPushButton,
    QMessageBox, QTabBar,
)


from core.formatters import abs_time, fmt_size, relative_time, status_zh
from core.urlParser import workshop_url
from gui.theme import font_px  # 字号单源（D25）
from core.constants import (CONFIRMED_SOURCE_ZH, STATUS_DELETED,
                            STATUS_DOWNLOADED, VERDICT_KIND_ZH)
from PySide6.QtGui import QColor
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
    # 页签 ↔ 栈页映射（栈页：0=无选中占位 1=基本信息 2=依赖
    # 3=描述占位 4=翻译占位；页签：0基本信息 1描述 2翻译 3依赖）
    _TAB_TO_PAGE = {0: 1, 1: 3, 2: 4, 3: 2}
    _PAGE_TO_TAB = {1: 0, 3: 1, 4: 2, 2: 3}

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

    def __init__(self, repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._mod = None
        self._download_dir: str | None = None

        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget(self)
        # 页签导航（基本信息/描述/翻译/依赖）：点页签切下方内容区；
        # 描述/翻译 = 占位页（模块冻结中），依赖 = 原子页
        self._tabs = QTabBar(self)
        for _n in ("基本信息", "描述", "翻译", "依赖"):
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
                        lambda _u: self._open_deps_page())

                self._form.addRow(name, val)
                self._vals[key] = val

        wrap = QScrollArea(page)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.Shape.NoFrame)
        wrap.setWidget(body)
        pv.addWidget(wrap)
        self._stack.addWidget(page)
        # —— 栈页 2：依赖子页（⑪：明细唯一形态）——描述、翻译将来
        # 同模板各占一页：顶部返回+标题，中部内容区，底部操作行。
        # 正文速览卡从此不再随内容膨胀；折叠方案随子页定稿退役
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
        self._deps_table = QTableWidget(0, 5, deps_page)
        self._deps_table.setHorizontalHeaderLabels(
            ["编号", "名称", "状态", "大小", "远端更新"])
        self._deps_table.verticalHeader().setVisible(False)
        self._deps_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._deps_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._deps_table.setWordWrap(False)
        _dh = self._deps_table.horizontalHeader()
        _dh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        _dh.setSectionsClickable(False)
        for col, width in ((0, 100), (2, 70), (3, 80), (4, 90)):
            self._deps_table.setColumnWidth(col, width)
        self._deps_table.itemDoubleClicked.connect(self._on_dep_dclicked)
        dv.addWidget(self._deps_table, 1)
        dep_acts = QHBoxLayout()
        self._btn_deps_copy = QPushButton("复制全部编号", deps_page)
        self._btn_deps_copy.setToolTip(
            "依赖清单的工坊编号逐行复制（每行一个）——可贴进"
            "【入账中心 · 登记】收录，或【依赖检测】页对表")
        self._btn_deps_copy.clicked.connect(self._copy_dep_ids)
        dep_acts.addWidget(self._btn_deps_copy)
        self._btn_deps_send = QPushButton("送登记账外编号…", deps_page)
        self._btn_deps_send.setToolTip(
            "把清单里「账外」的依赖编号直接送【入账中心 · 登记】："
            "预填粘贴框、自动解析预览（不写库）——核对后点"
            "【登记新编号】入账")
        self._btn_deps_send.clicked.connect(self._send_outdeps_register)
        dep_acts.addWidget(self._btn_deps_send)

        dep_acts.addStretch(1)
        dv.addLayout(dep_acts)
        self._pg_deps = self._stack.addWidget(deps_page)  # 页号（=2）
        # 描述 / 翻译占位页：页签先架好，点过去见说明，不空指针
        ph_desc = QWidget(self)
        dv2 = QVBoxLayout(ph_desc)
        lbl_desc = QLabel("描述子页：规划中（拉取、展示与翻译联动待定）。",
                          ph_desc)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {_DIM};")
        dv2.addWidget(lbl_desc)
        dv2.addStretch(1)
        self._pg_desc = self._stack.addWidget(ph_desc)  # 页号（=3）
        ph_trans = QWidget(self)
        tv2 = QVBoxLayout(ph_trans)
        lbl_trans = QLabel("翻译子页：规划中（与描述同模板，另行评审）。",
                           ph_trans)
        lbl_trans.setWordWrap(True)
        lbl_trans.setStyleSheet(f"color: {_DIM};")
        tv2.addWidget(lbl_trans)
        tv2.addStretch(1)
        self._pg_trans = self._stack.addWidget(ph_trans)  # 页号（=4）

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
        staying = self._stack.currentIndex() in (
            self._pg_deps, self._pg_desc, self._pg_trans)
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
        # —— 依赖（必需物品）——入口行摘要；子页若开着原位重灌
        v["deps"].setText(self._deps_entry(mod))
        self._sync_tabs()
        if staying:
            self._fill_deps_page(mod)
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
    # ---------- 依赖（必需物品）：入口行 + 子页（⑪） ----------

    def _deps_state(self, mod) -> tuple[str, list[int]]:
        """依赖三态判定（口径单源：入口行与子页共用）。契约口径
        list_dependencies 对"从未拉取"与"确无依赖"同返 []（契约原文），
        单靠它分不出两态——用全局拉取时间兜底（零契约改动）：
        - "none" 未获取：全账本从没成功拉取过（latest_dependency_fetch
          为 None）——此时任何 mod 都必然没拉过；查询异常同按未获取；
        - "empty" 无依赖：全局拉取过而本条清单为空；
        - "list"：有清单。
        多档案下全局口径分不清哪个档案拉的：B 档案拉过后，没拉过的
        A 档案条目会误报"无依赖"——子页空态里已披露；根治（每 mod
        拉取戳）在改善项池。"""
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
        """编号清单 → 行数据（单源：入口行的分布计数与子页表格都从
        这份行数据取数，口径绝不两份）。行 = (编号, 名称, 状态词,
        大小, 远端更新, 颜色, 账内?)。五色定稿：绿=已下载、红=账上
        没下载、黄=已删除（可恢复）、灰=账外（远端状态本地无从
        得知——依赖检测页点查询后蓝=远端在架）。"""
        rows: list[tuple] = []
        for d in sorted(deps):
            m = self._repo.get_mod(d)
            if m is None:
                rows.append((str(d), "—", "账外", "—", "—",
                             QColor("#8a8a8f"), False))
            elif m.status == STATUS_DOWNLOADED:
                rows.append((str(d), m.title or "—", "已下载",
                             fmt_size(m.local_size or m.file_size),
                             relative_time(m.time_updated)
                             if m.time_updated else "—",
                             QColor("#46a758"), True))
            elif m.status == STATUS_DELETED:
                rows.append((str(d), m.title or "—", "已删除",
                             fmt_size(m.local_size or m.file_size),
                             relative_time(m.time_updated)
                             if m.time_updated else "—",
                             QColor("#c8a03c"), True))
            else:  # tracked / failed：账上在管、本地缺失
                rows.append((str(d), m.title or "—", "未下载",
                             fmt_size(m.file_size),
                             relative_time(m.time_updated)
                             if m.time_updated else "—",
                             QColor("#e5484d"), True))
        return rows

    def _deps_entry(self, mod) -> str:
        """页 1 依赖入口行（明细住子页，本行只做摘要+入口）。
        未获取/无依赖 = 灰色短词无链接（详细说明在子页空态）。"""
        state, deps = self._deps_state(mod)
        if state == "none":
            return ('<span style="color:#8a8a8f">未获取——到'
                    "【依赖检测】页拉取后入账</span>")
        if state == "empty":
            return '<span style="color:#8a8a8f">无依赖</span>'
        rows = self._build_dep_rows(deps)
        counts: dict[str, int] = {}
        for x in rows:
            counts[x[2]] = counts.get(x[2], 0) + 1
        return (f"{len(rows)} 个"
                f"（本地 {counts.get('已下载', 0)} · "
                f"缺 {counts.get('未下载', 0)} · "
                f"已删 {counts.get('已删除', 0)} · "
                f"账外 {counts.get('账外', 0)}）　"
                '<a href="deps">查看与操作 →</a>')
    def _open_deps_page(self) -> None:
        """入口行链接落点：灌表 + 切到依赖栈页。此前只灌表没切页
        ——链接点了"没反应"，缺的就是第二行。"""
        if self._mod is not None:
            self._fill_deps_page(self._mod)
            self._stack.setCurrentIndex(self._pg_deps)
            self._sync_tabs()

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
        """依赖子页灌表。空态说明住这里（页 1 只留短词）；有清单时
        提示行 = 颜色图例 + 双击说明。"""
        state, deps = self._deps_state(mod)
        title = mod.title or "（无标题）"
        t = self._deps_table
        if state != "list":
            self._deps_title.setText(f"依赖 · {title}")
            self._deps_hint.setText(
                "还没拉取过依赖——到【依赖检测】页拉取一次，清单就会"
                "记在这里。" if state == "none" else
                "最近一次拉取时该 mod 没有必需物品。多档案账本里若"
                "本档案还没拉取过（是别的档案拉的），这里可能是"
                "「未拉取」——本档案拉取一遍即明确。")
            t.setRowCount(0)
            self._btn_deps_copy.setEnabled(False)
            self._btn_deps_send.setEnabled(False)

            return
        rows = self._build_dep_rows(deps)
        t.setRowCount(len(rows))
        for r, (mid, name, st, size, upd, color, in_ledger) in enumerate(rows):
            c0 = QTableWidgetItem(mid)
            c0.setData(Qt.ItemDataRole.UserRole, int(mid))
            cells = (c0, QTableWidgetItem(name), QTableWidgetItem(st),
                     QTableWidgetItem(size), QTableWidgetItem(upd))
            cells[2].setForeground(color)
            for c, cell in enumerate(cells):
                t.setItem(r, c, cell)
            t.item(r, 0).setToolTip(
                "双击跳到该 mod 的详情" if in_ledger else
                "账外条目：双击打开工坊网页；复制编号到【入账中心 · "
                "登记】可收录")
        self._deps_title.setText(f"依赖 · {title}（{len(deps)} 个）")
        self._deps_hint.setText(
            "绿=本地有｜红=账上没下载｜黄=已删除（可恢复）｜"
            "灰=账外未查。双击行：账内跳详情、账外开工坊网页。")
        self._btn_deps_copy.setEnabled(bool(rows))
        n_out = sum(1 for x in rows if not x[6])  # x[6] = 账内?
        self._btn_deps_send.setEnabled(n_out > 0)
        if n_out:
            self._btn_deps_send.setText(f"送登记账外编号（{n_out}）…")


    def _on_dep_dclicked(self, item) -> None:
        """依赖行双击：账内 → 发信号让库页定位选中（show_mod 在子页
        原位重灌 = 沿依赖链连逛）；账外 → 开工坊网页。分支按双击
        那一刻的库内现状态定。就地 import：本面板此前无浏览器动作，
        免动文件头 import 区。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        it = self._deps_table.item(item.row(), 0)
        mid = it.data(Qt.ItemDataRole.UserRole) \
            if it is not None else None
        if not mid:
            return
        if self._repo.get_mod(mid) is None:
            QDesktopServices.openUrl(QUrl(workshop_url(mid)))
        else:
            self.mod_link_requested.emit(mid)

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

    def _on_open_folder(self) -> None:
        if self._mod is not None:
            self.open_folder_requested.emit(self._mod.mod_id)
