"""mod 全字段详情"""
"""
只读面板：26个字段全部展示，时间戳"原始值+可读值"并列。界面文案全部
汉字，数据库字段名放进标签的悬浮提示——不脏界面，又保留对照库表的
调试能力。所有文本可选中复制。
"""
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from core.models import Mod
from gui.formatters import abs_time, fmt_size, status_zh


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
        self._kv("Mod 编号", "mod_id", m.mod_id)
        self._kv("所属游戏", "game_id", m.game_id)
        self._kv("状态", "status", status_zh(m.status), raw=m.status)
        self._kv("标题", "title", m.title)
        self._kv("工坊链接", "url", m.url)
        self._kv("作者", "creator", m.creator)
        self._kv("创建时间", "time_created", m.time_created, "ts")
        self._kv("收录时间", "first_tracked_at", m.first_tracked_at, "ts")

        self._section("远端信息（Steam）")
        self._kv("远端更新时间", "time_updated", m.time_updated, "ts")
        self._kv("上次远端更新", "last_time_updated", m.last_time_updated, "ts")
        self._kv("远端大小", "file_size", m.file_size, "size")
        self._kv("订阅数", "subscriptions", m.subscriptions)
        self._kv("收藏数", "favorited", m.favorited)
        self._kv("浏览数", "views", m.views)
        self._kv("标签", "tags", m.tags, "json")
        self._kv("预览图链接", "preview_url", m.preview_url)
        self._kv("上次检测时间", "last_checked_at", m.last_checked_at, "ts")

        self._section("本地信息（acf）")
        self._kv("本地安装时间", "local_timeupdated", m.local_timeupdated, "ts")
        self._kv("清单号", "manifest", m.manifest)
        self._kv("本地大小", "local_size", m.local_size, "size")
        self._kv("本地路径", "local_path", m.local_path)

        self._section("标记与备注")
        self._kv("特别关注", "is_special", m.is_special, "bool")
        self._kv("备注", "note", m.note)
        self._kv("颜色标记", "color_tag", m.color_tag)

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

    def _kv(self, label_zh: str, field: str, value, kind: str = "raw", raw=None) -> None:
        match kind:
            case "ts":
                text = f"{value}（{abs_time(value)}）" if value else "—"
            case "size":
                text = f"{value}（{fmt_size(value)}）" if value is not None else "—"
            case "json":
                text = json.dumps(value, ensure_ascii=False) if value else "—"
            case "bool":
                text = "是" if value else "否"
            case _:
                text = "—" if value is None else str(value)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if raw is not None and str(raw) != text:
            lbl.setToolTip(f"库内值：{raw}")   # 中文显示与库内原值不同时，悬浮可查
        name_lbl = QLabel(label_zh)
        name_lbl.setToolTip(f"数据库字段：{field}")
        self._form.addRow(name_lbl, lbl)
