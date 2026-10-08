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
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices,QAction
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,QApplication
)

from core import constants
from core.models import Game
from gui.logBus import LogBus
from workflows import exceptionFlow
from gui.theme import font_px  # 字号单源（D25）
from core.urlParser import workshop_url
_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_MUTED = "#8a8a8f"


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

        tip = QLabel(
            "本地账本标题里含提醒关键词的条目（词表在设置页"
            "「本地标题提醒关键词」自行增删，留空 = 停用）。\n"
            "作者常把 Abandoned / Deprecated 写进标题表示不再维护——"
            "但地图名也可能含这些词（如 \"Abandoned Mines\"），命中词"
            "在行内明示，只是提醒不是判定。离线毫秒级，不联网。",
            self)
        tip.setWordWrap(True)   # 踩坑⑨：可能变长的标签一律开换行
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)
        # self.root_layout = root
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

        self._list = QListWidget(self)
        self._list.setWordWrap(False)
        self._list.setAlternatingRowColors(True)
        self._list.setFixedHeight(240)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_row_menu)
        root.addWidget(self._list)

        note = QLabel(
            "改词表：设置页 →「本地标题提醒关键词」。远端侧的弃坑检测"
            "在【远端健康】页（联网深检时判定），两处词表相互独立。",
            self)
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        root.addWidget(note)
        root.addStretch(1)

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
        self._list.clear()
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
        self._list.clear()
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
        for mid, kws in sorted(lt.hits.items()):
            it = QListWidgetItem(
                f"{mid} {self._title_of(mid)} · 命中：" + "、".join(kws))
            it.setData(Qt.ItemDataRole.UserRole, mid)
            self._list.addItem(it)
        self._log.warn(
            f"标题检测（{self._game.name}）：{lt.total} 个标题含关键词")

    def _title_of(self, mid: int) -> str:
        m = self._mods_by_id.get(mid)
        return (m.title if m is not None and m.title else None) or "（无标题）"

    # ---------- 行菜单（查看类，与 V1 桶C 行同款）----------
    def _on_row_menu(self, pos) -> None:
        it = self._list.itemAt(pos)
        if it is None:
            return
        mid = it.data(Qt.ItemDataRole.UserRole)
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
        menu.exec(self._list.mapToGlobal(pos))

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
