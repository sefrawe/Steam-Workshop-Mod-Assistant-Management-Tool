"""mod 全字段详情"""
r"""只读面板：26 个字段全展示，时间戳"原始值+可读值"并列。
界面文案全部汉字，数据库字段名放进标签的悬浮提示——不脏界面，
又保留对照库表的调试能力。所有文本可选中复制。

分节按"人看的角度"重排（T19②）：标题打头——人认 mod 靠名字
不靠编号；本地与远端版本信息合进同一节"版本对照"——本工具的
核心问题就是"本地比远端旧没旧"，两本账并排才好对；纯展示性的
Steam 数据（订阅/收藏/浏览/标签/预览图）单独成节垫后。
只动显示顺序，不动数据。

手动确认空态（T19④）：由【手动确认入账】的 mod 没有 acf 本地
事实，版本对照节顶部给一行说明——不是"数据丢了"，是"本来
就没有"；补全路径 = steamcmd 重下后扫描（决策 24 的界面表达）。
"""
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.models import Mod
from gui.formatters import abs_time, fmt_size, status_zh

_MANUAL_CONFIRM_NOTE = (
    "本条目由【手动确认入账】：没有 acf 本地事实，版本以确认时的"
    "断言为准。\n用 steamcmd 重新下载并在 mod 库页点【扫描本地】后，"
    "以下字段会自动补全。")


class ModDetailPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll)
        self._host = QWidget(self._scroll)
        self._scroll.setWidget(self._host)
        self._form = QFormLayout(self._host)
        self._form.setContentsMargins(12, 12, 12, 12)
        self.set_mod(None)

    # ---------- 对外 ----------
    def set_mod(self, m: Mod | None) -> None:
        self._clear()
        if m is None:
            self._form.addRow(QLabel("点击左侧列表查看 mod 详情"))
            return
        self._section("基本档案")
        self._kv("标题", "title", m.title)
        self._kv("Mod 编号", "mod_id", m.mod_id)
        self._kv("状态", "status", status_zh(m.status), raw=m.status)
        self._kv("所属游戏", "game_id", m.game_id)
        self._kv("作者", "creator", m.creator)
        self._kv("工坊链接", "url", m.url)
        self._kv("创建时间", "time_created", m.time_created, "ts")
        self._kv("收录时间", "first_tracked_at", m.first_tracked_at, "ts")

        self._section("版本对照（本地 ↔ 远端）")
        if m.version_unknown:  # T18 判定单源：downloaded 且无 acf 本地版本
            note = QLabel(_MANUAL_CONFIRM_NOTE, self._host)
            note.setWordWrap(True)  # 可能变长的标签一律开换行
            note.setStyleSheet("color: #f5a623;")  # 与全局警告色同源
            self._form.addRow(note)
        self._kv("本地安装时间", "local_timeupdated", m.local_timeupdated, "ts")
        self._kv("清单号", "manifest", m.manifest)
        self._kv("本地大小", "local_size", m.local_size, "size")
        self._kv("本地路径", "local_path", m.local_path)
        self._kv("远端更新时间", "time_updated", m.time_updated, "ts")
        self._kv("上次远端更新", "last_time_updated", m.last_time_updated, "ts")
        self._kv("远端大小", "file_size", m.file_size, "size")
        self._kv("上次检测时间", "last_checked_at", m.last_checked_at, "ts")

        self._section("Steam 数据")
        self._kv("订阅数", "subscriptions", m.subscriptions)
        self._kv("收藏数", "favorited", m.favorited)
        self._kv("浏览数", "views", m.views)
        self._kv("标签", "tags", m.tags, "json")
        self._kv("预览图链接", "preview_url", m.preview_url)

        self._section("标记与备注")
        self._kv("特别关注", "is_special", m.is_special, "bool")
        self._kv("颜色标记", "color_tag", m.color_tag)
        self._kv("备注", "note", m.note)

        self._section("软删除")
        self._kv("删除时间", "deleted_at", m.deleted_at, "ts")
        self._kv("删除前快照", "deleted_last_state", m.deleted_last_state, "json")

    # ---------- 内部 ----------
    def _clear(self) -> None:
        while self._form.count():
            item = self._form.takeAt(0)
            if (w := item.widget()) is not None:
                w.deleteLater()

    def _section(self, title: str) -> None:
        lbl = QLabel(title)
        lbl.setStyleSheet("font-weight: 600; margin-top: 10px;")
        self._form.addRow(lbl)

    def _kv(self, label_zh: str, field: str, value, kind: str = "raw",
            raw=None) -> None:
        match kind:
            case "size":
                # 可读值前置：先看"13.7 MiB"，需要核对库内原值再看括号
                text = f"{fmt_size(value)}（{value}）" if value is not None else "—"
            case "ts":
                text = f"{abs_time(value)}（{value}）" if value else "—"
            case "json":
                text = json.dumps(value, ensure_ascii=False) if value else "—"
            case "bool":
                text = "是" if value else "否"
            case _:
                text = "—" if value is None else str(value)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        if raw is not None and str(raw) != text:
            lbl.setToolTip(f"库内值：{raw}")  # 中文显示与库内原值不同时，悬浮可查
        name_lbl = QLabel(label_zh)
        name_lbl.setToolTip(f"数据库字段：{field}")
        self._form.addRow(name_lbl, lbl)
