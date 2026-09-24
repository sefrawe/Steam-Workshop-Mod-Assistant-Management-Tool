"""统计页
"""
r"""gui/statsPage.py · 当前档案 mod 库的统计快照（T19⑲）。

定位：mod 库页顶部统计条瘦身后（只留筛选结果数），"一眼扫"升级成
"一页看全"——状态分布、容量、标签、更新节奏、失效归档都在这里。
全部数字由现有 repo 接口在 Python 端聚合（几百条规模毫秒级），
零新 SQL、零新契约方法；需更新数复用 ModListModel._update_state，
与列表「更新」列同一判定单源。

口径（每节注明，避免"数字对不上"的错觉）：
- 概览含已删除（软删除也是库内事实）；
- 容量含已删除——软删除不删文件，磁盘占用是真实的；
- 标签与更新节奏不含已删除（看的是"现役库"的构成与节奏）；
- 备份总占用是全部档案合计（sum_backup_bytes 没有档案维度）。

刷新时机：切换档案自动刷；右上【刷新】手动刷
（扫描/检测等其他页面改了数据后回来点一下即可）。
"""
import time
from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.models import Game
from gui.formatters import fmt_size
from gui.modListModel import ModListModel

_DAY = 86400

_RHYTHM_BUCKETS = [("7 天内", 7), ("30 天内", 30), ("90 天内", 90), ("1 年内", 365)]


class StatsPage(QWidget):
    """统计页：无可写状态，set_game 即重算。"""

    def __init__(self, repo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game: Game | None = None
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("统计", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        btn = QPushButton("刷新", self)
        btn.setToolTip("重新计算全部数字；其他页面改了数据后回来点一下")
        btn.clicked.connect(self._refresh)
        head.addWidget(btn)
        root.addLayout(head)

        # T19⑧ 教训：内容装滚动区——条目多的档案里各节总高度会超过
        # 窗口，硬排会被裁掉
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(12)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._refresh()

    # ---------- 对外 ----------

    def set_game(self, game: Game | None) -> None:
        """MainWindow 的 _on_game_changed 按 set_game 有无自动广播，
        本页与各页同口径：切档案即重算。"""
        self._game = game
        self._refresh()

    # ---------- 渲染 ----------

    def _refresh(self) -> None:
        self._clear(self._body)
        if self._game is None:
            self._add_hint("请先在左上角选择游戏档案。")
            return
        rows = self._repo.list_mods(self._game.app_id)
        alive = [m for m in rows if m.status != "deleted"]

        self._add_section("概览", self._overview_lines(rows))
        self._add_section("容量", self._capacity_lines(rows))
        self._add_section("标签 Top 10", self._tag_lines(alive))
        self._add_section("更新节奏", self._rhythm_lines(alive))
        self._add_section("失效归档", self._failed_lines())

        foot = QLabel(f"以上为 {time.strftime('%H:%M:%S')} 时刻的快照。", self)
        foot.setStyleSheet("color: gray;")
        self._body.addWidget(foot)
        self._body.addStretch(1)

    def _add_section(self, title: str, lines: list[str]) -> None:
        head = QLabel(title, self)
        head.setStyleSheet("font-size: 14px; font-weight: 600;")
        self._body.addWidget(head)
        body = QLabel("\n".join(lines), self)
        body.setWordWrap(True)
        # 文字可选中复制——把统计数字贴给别人看是真实场景
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.addWidget(body)

    def _add_hint(self, text: str) -> None:
        lbl = QLabel(text, self)
        lbl.setStyleSheet("color: gray;")
        self._body.addWidget(lbl)
        self._body.addStretch(1)

    @staticmethod
    def _clear(layout: QVBoxLayout) -> None:
        """清空重建：统计节是纯 QLabel，deleteLater 即可，无嵌套布局。"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    # ---------- 各节数据（口径见文件头） ----------

    def _overview_lines(self, rows) -> list[str]:
        per = Counter(m.status for m in rows)
        n_update = sum(1 for m in rows
                       if ModListModel._update_state(m) == "需更新")
        return [
            f"mod 总数：{len(rows)}（含已删除）",
            f"已下载 {per['downloaded']}｜已收录 {per['tracked']}"
            f"｜已删除 {per['deleted']}｜已失败 {per['failed']}",
            f"需更新：{n_update}（与列表「更新」列同一判定）",
            f"特别关注：{sum(1 for m in rows if m.is_special)}",
        ]

    def _capacity_lines(self, rows) -> list[str]:
        local_total = sum(m.local_size or 0 for m in rows)
        remote_total = sum(m.file_size or 0 for m in rows)
        lines = [
            f"本地内容合计：{fmt_size(local_total)}"
            "（含已删除——软删除不删文件，磁盘占用是真实的）",
            f"远端登记合计：{fmt_size(remote_total)}"
            "（API 登记值，未下载的也计入）",
            f"备份总占用：{fmt_size(self._repo.sum_backup_bytes())}"
            "（全部档案合计）",
        ]
        top = sorted((m for m in rows
                      if m.status != "deleted" and m.local_size),
                     key=lambda m: m.local_size, reverse=True)[:10]
        if top:
            lines.append("最大的 10 个：")
            lines += [f"　{m.title or m.mod_id}（{m.mod_id}）— "
                      f"{fmt_size(m.local_size)}" for m in top]
        else:
            lines.append("最大的 10 个：（暂无带本地大小的条目）")
        return lines

    def _tag_lines(self, alive) -> list[str]:
        tags = Counter(t for m in alive if m.tags for t in m.tags)
        if not tags:
            return ["（还没有标签数据——跑一次更新检测补齐）"]
        return [f"{name} ×{n}" for name, n in tags.most_common(10)]

    def _rhythm_lines(self, alive) -> list[str]:
        now = int(time.time())
        order = [name for name, _ in _RHYTHM_BUCKETS] + ["更早", "未知"]
        counts = dict.fromkeys(order, 0)
        for m in alive:
            if not m.time_updated:
                counts["未知"] += 1
                continue
            age = now - m.time_updated
            for name, days in _RHYTHM_BUCKETS:
                if age <= days * _DAY:
                    counts[name] += 1
                    break
            else:
                counts["更早"] += 1
        parts = "｜".join(f"{name} {counts[name]}" for name in order)
        return [f"距远端最近一次更新：{parts}",
                "（不含已删除条目；「未知」= 从未取得远端更新时间）"]

    def _failed_lines(self) -> list[str]:
        failed = self._repo.list_failed(self._game.app_id)
        if not failed:
            return ["归档 0 条。"]
        latest = time.strftime("%Y-%m-%d",
                               time.localtime(failed[0].detected_at))
        return [f"归档 {len(failed)} 条，最近一条 {latest}；"
                "处理与关联替换入口在「异常处理」页。"]
