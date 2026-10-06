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

from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.constants import CONFIRMED_SOURCE_ZH, VERDICT_KIND_ZH
from core.formatters import abs_time, fmt_size, relative_time, status_zh
from gui.theme import font_px  # 字号单源（D25）
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from core.urlParser import WORKSHOP_URL_TEMPLATE  # 工坊链接模板单源（决策 61④）

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
                self._form.addRow(name, val)
                self._vals[key] = val

        wrap = QScrollArea(page)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.Shape.NoFrame)
        wrap.setWidget(body)
        pv.addWidget(wrap)
        self._stack.addWidget(page)
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
            return
        self._stack.setCurrentIndex(1)
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
            u = WORKSHOP_URL_TEMPLATE.format(mod_id)
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
