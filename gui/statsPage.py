"""统计页
"""
r"""gui/statsPage.py · 当前档案 mod 库的统计快照。

定位：mod 库页顶部统计条瘦身后（只留筛选结果数），"一眼扫"升级
成"一页看全"——状态分布、容量、标签、更新节奏、失效归档都在
这里。全部数字由现有 repo 接口在 Python 端聚合（几百条规模毫秒
级），零新 SQL、零新契约方法；需更新数与 mod 库页「更新」列同一把尺（D2 公式，_needs_update 镜像实现——V2 modListModel 无同名方法）。

口径（每节注明，避免"数字对不上"的错觉）：
- 概览含已删除（软删除也是库内事实）；
- 容量含已删除——软删除不删文件，磁盘占用是真实的；本地占用
  来自盘点回填（local_size），未盘点的条目按 0 计；
- 标签与更新节奏不含已删除（看的是"现役库"的构成与节奏）；
- 备份总占用是全部档案合计（sum_backup_bytes 没有档案维度）。

图表（可选件）：概览/标签 Top10/更新节奏三节加 QtCharts 横条图；
缺 PySide6-Addons 时自动降级为纯文字，其余功能不受影响。文字行
保留：数字可选中复制，图表管"比例感"、文字管"精确值"。容量
Top10 与失效归档刻意不画：列表已直观 / 0 条画空图难看。

刷新时机：切换档案自动刷；右上【刷新】手动刷。
"""

import time
from collections import Counter

try:
    # 可选件：缺 PySide6-Addons 只降级为纯文字，不拦启动
    from PySide6.QtCharts import (
        QAbstractBarSeries,
        QBarCategoryAxis,
        QBarSet,
        QChart,
        QChartView,
        QHorizontalBarSeries,
        QValueAxis,
    )
    _HAS_CHARTS = True
except ImportError:  # pragma: no cover - 环境差异路径
    _HAS_CHARTS = False

from PySide6.QtCore import QMargins, Qt
from PySide6.QtGui import  QPalette
from PySide6.QtGui import QPainter

from gui.theme import current_mode, system_prefers_dark


from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.models import Game
from core.formatters import fmt_size

_DAY = 86400
_RHYTHM_BUCKETS = [("7 天内", 7), ("30 天内", 30), ("90 天内", 90),
                   ("1 年内", 365)]

def _needs_update(m) -> bool:
    """需更新判定（D2 显示侧，与 mod 库页「更新」列同尺）：已下载、
    且远端 time_updated > confirmed_version。任一版本未知不算——
    从没查过远端的条目不冒充"需更新"（命令分组那边的保守口径是
    commandBuilder.group_mods 的事，两把尺职责不同，不许互改）。
    V2 的 modListModel 无 _update_state 方法名（规则内联在画格代码
    里），故此处镜像实现；下轮若动库页更新列判定须同步。"""
    return (m.status == "downloaded"
            and bool(m.confirmed_version) and bool(m.time_updated)
            and m.time_updated > m.confirmed_version)

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
        per = Counter(m.status for m in rows)
        self._add_chart(self._make_hbar(
            ["已下载", "已收录", "已删除", "已失败"],
            [per["downloaded"], per["tracked"],
             per["deleted"], per["failed"]]))

        self._add_section("容量", self._capacity_lines(rows))

        tag_counter = Counter(t for m in alive if m.tags for t in m.tags)
        self._add_section("标签 Top 10", self._tag_lines(tag_counter))
        self._add_chart(self._make_hbar(
            [name for name, _ in tag_counter.most_common(10)],
            [n for _, n in tag_counter.most_common(10)]))

        self._add_section("更新节奏", self._rhythm_lines(alive))
        counts = self._rhythm_counts(alive)
        order = [name for name, _ in _RHYTHM_BUCKETS] + ["更早", "未知"]
        self._add_chart(self._make_hbar(
            order, [counts[name] for name in order]))

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

    def _add_chart(self, chart_view) -> None:
        """图表可选：None（缺包/无数据）就跳过——文字行永远是兜底。"""
        if chart_view is not None:
            self._body.addWidget(chart_view)

    def _make_hbar(self, categories: list[str], values: list[int]):
        """横条图（QtCharts，可选件）：分类按传入顺序自上而下排列。
        缺 PySide6-Addons / 无数据 → None，调用方跳过。
        主题跟随：按调色板亮度挑 Dark/Light 图表主题；条色取
        Highlight（与全局强调色同源）；背景透明融入页面。
        注意：方法不写返回值类型注解——QtCharts 缺包时 QChartView
        这个名字不存在，注解会在类定义时炸 NameError。"""
        if not _HAS_CHARTS or not categories or not values:
            return None
        # ---- 主题判定走事实源（决策 66⑧）：设置键 theme_mode；auto 档
        # 读注册表。v2.43 初版用 palette().Window 亮度判定——踩坑51
        # 正面命中（深色界面拿到浅色 Window → 网格线刺眼亮白，截图实证）。
        mode = current_mode()
        if mode not in ("dark", "light"):
            mode = "dark" if system_prefers_dark() else "light"

        # ChartTheme 成员名跨 PySide6 版本不稳，且有两代命名并存：
        # "DarkTheme"（短名）与 "ChartThemeDark"（C++ 枚举值原名）。
        # 双候选都试，取得到才设，取不到保持默认（背景已透明，
        # 差异只在网格线深浅，纯观感）。
        # v2.43.5 勘误：上一版候选表忘了跟模式挂钩——亮色模式也会选中
        # ChartThemeDark（深色主题=白字），白底上标签全白（截图实证）。
        # 候选表按 mode 分列，两代命名各就各位（探针实证本机命中的是
        # C++ 原值名，即第一个）。
        _names = (("ChartThemeDark", "DarkTheme") if mode == "dark"
                  else ("ChartThemeLight", "LightTheme"))
        _theme = None
        for _name in _names:
            _theme = getattr(QChart.ChartTheme, _name, None)
            if _theme is not None:
                break

        # ---- 先建 chart、再设主题，顺序不能反（v2.43.2 指引把
        # setTheme 插在了 chart 建立之前——IDE"未解析的引用"是
        # 真问题；本机恰无成员才侥幸不炸，记事本已记）----
        chart = QChart()
        if _theme is not None:
            chart.setTheme(_theme)
        chart.setBackgroundVisible(False)  # 融入页面背景
        chart.setPlotAreaBackgroundVisible(False)
        chart.legend().setVisible(False)  # 单系列无图例
        chart.setMargins(QMargins(2, 2, 2, 2))

        # 横条图的分类轴自下而上排列：倒序喂入，第一项显示在最上面
        series = QHorizontalBarSeries()
        barset = QBarSet("")
        barset.append([int(v) for v in reversed(values)])
        # 条色：主题设上了就让主题配色（亮/深各自协调）；没设上才退回
        # 调色板 Highlight——palette 不跟 QSS 换装（踩坑51），亮色下
        # 会是突兀的紫红（你的亮色截图实证），只做兜底不做主路径。
        if _theme is None:
            barset.setColor(
                self.palette().color(QPalette.ColorRole.Highlight))

        series.append(barset)
        series.setLabelsVisible(True)  # 条端显示数值
        # v2.43.1：LabelsPosition 的成员名跨 PySide6 版本不稳（实测有
        # 版本 LabelsPosition 存在、OutsideEnd 却取不到 → 启动即炸，
        # 踩坑54 族"版本门槛 API 不能裸调"）。getattr 探测：取得到就
        # 设，取不到用默认位置——标签仍可见，最多位置略异，纯观感。
        # 同族问题：成员名两代命名并存（LabelsOutsideEnd = C++ 原值名，
        # OutsideEnd = 短名），双候选探测；连枚举类本身也防一手
        _lp = getattr(QAbstractBarSeries, "LabelsPosition", None)
        _pos = None
        if _lp is not None:
            for _name in ("LabelsOutsideEnd", "OutsideEnd"):
                _pos = getattr(_lp, _name, None)
                if _pos is not None:
                    break
        if _pos is not None:
            series.setLabelsPosition(_pos)

        series.setLabelsFormat("@value")

        chart.addSeries(series)

        cats = QBarCategoryAxis()
        cats.append([str(c) for c in reversed(categories)])
        chart.setAxisY(cats, series)
        val = QValueAxis()
        val.setLabelFormat("%d")
        val.setRange(0, max(values) * 1.3 + 1)  # 留出条端标签的空间
        chart.setAxisX(val, series)

        view = QChartView(chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setFixedHeight(30 * len(categories) + 50)
        return view

    def _add_hint(self, text: str) -> None:
        lbl = QLabel(text, self)
        lbl.setStyleSheet("color: gray;")
        self._body.addWidget(lbl)
        self._body.addStretch(1)

    @staticmethod
    def _clear(layout: QVBoxLayout) -> None:
        """清空重建：统计节是纯 QLabel / QChartView，deleteLater 即可。"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    # ---------- 各节数据（口径见文件头） ----------

    def _overview_lines(self, rows) -> list[str]:
        per = Counter(m.status for m in rows)
        n_update = sum(1 for m in rows
                       if _needs_update(m))
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
        top = sorted((m for m in rows if m.status != "deleted"
                      and m.local_size),
                     key=lambda m: m.local_size, reverse=True)[:10]
        if top:
            lines.append("最大的 10 个：")
            lines += [f"  {m.title or m.mod_id}（{m.mod_id}）— "
                      f"{fmt_size(m.local_size)}" for m in top]
        else:
            lines.append("最大的 10 个：（暂无带本地大小的条目）")
        return lines

    def _tag_lines(self, tags: Counter) -> list[str]:
        if not tags:
            return ["（还没有标签数据——跑一次更新检测补齐）"]
        return [f"{name} ×{n}" for name, n in tags.most_common(10)]

    @staticmethod
    def _rhythm_counts(alive) -> dict[str, int]:
        """更新节奏分桶（v2.43 从 _rhythm_lines 拆出）：文字行与图表
        共用同一份计数，两处数字永不打架。"""
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
        return counts

    def _rhythm_lines(self, alive) -> list[str]:
        counts = self._rhythm_counts(alive)
        order = [name for name, _ in _RHYTHM_BUCKETS] + ["更早", "未知"]
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
