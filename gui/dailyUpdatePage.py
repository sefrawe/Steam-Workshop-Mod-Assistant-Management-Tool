"""日常更新功能模块
"""
"""
gui/dailyUpdatePage.py · 左导航「分步向导」组：日常主线引导页。

把"更新检测 → 确认动作 → 备份+下载 → 确认入账"这条日常主线装进
一个页面（决策 26 一条龙的界面壳）。本页零逻辑复制（单源纪律）：
- 检测链只有【更新检测】页那一份——第②步按钮只发 check_requested
  信号，主窗口转调检测页后台开测（不跳页）；进度 / 中断 / 完成
  三个信号由主窗口在装配本页时直连过来，进度条实时显示（决策 42：
  本页是第二块表盘，不是第二套计算）；
- 确认与下载 = V2 现成的两条链：检测完成后主窗口弹「更新确认清单」
  （勾选条目、底部选动作：备份+更新 / 仅更新，可分批反复执行）；
  批次收尾自动弹「确认清单」（终端判决等背书）。本地版本的唯一
  写入口是确认门——本页不碰任何版本字段。

与 V1 的不同（V2 判决制适配）：
- 原第④步"复扫入账"不存在了——V2 没有扫描回填，批次结束弹的是
  确认清单，勾选 = 背书入账；当场关掉也不丢，【入账中心 · 待确认】
  区一直等着。第④卡改为说明确认入账的落点；
- V1 的 on_batch_finished（批次结果回叫）删除：批次结果由底部
  控制台的批次卡片展示，本页不再复制一份（改善项：批次结果回叫
  模块页，挂改善项池）。

卡片语义：②卡显示"最近一次"检测的进展；③④卡是固定操作指引。
无论这批从本模块还是更新检测页发起，清单与批次都走同一条链。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus
from gui.theme import font_px  # 字号单源（D25）

# 与批量下载步骤卡片、加入新 mod 页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 一切正常
_C_FAIL = "#e5484d"   # 出错
_C_WARN = "#f5a623"   # 需要注意（有失败/超时、被取消）
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行


class DailyUpdatePage(QWidget):
    """「日常更新」模块页：三步引导 + 检测进度仪表盘。"""

    # 第②步【开始检测】：主窗口接——转调更新检测页在后台开测，不跳页
    check_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 本页暂不读设置，参数保留与其他页一致
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        self._build_ui()
        self._reset_flow()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 整页装进滚动区（与 migrationPage 同款）
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("日常更新", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"；
        # 文案红线：只提用户看得见的东西，不提脚本编号与代码结构）
        note = QFrame(self)
        note.setObjectName("daily_note")
        note.setStyleSheet(
            "QFrame#daily_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：① 确认档案 → ② 点【开始检测】在后台检测本档案全部"
            " mod（本页进度条实时显示）→ 检测完成弹出更新确认清单"
            " → ③ 在清单里勾选、选动作（备份+更新 / 仅更新）、点"
            "【执行选中】（清单不关，可分批执行）→ 批次在底部控制台"
            "执行 → ④ 批次结束自动弹确认清单，勾选入账。",
            "为什么这么做：更新前先把当前版本复制进备份区，新版不满意"
            "随时到【备份与恢复】页回滚；检测、下载、确认用的全是既有"
            "页面，本页把它们串成一趟，进度和结果也汇总在这里。",
            "术语与关系：确有新版本 = 远端版本比你确认过的本地版本新"
            "（本地版本只认你的背书——批次收尾清单勾选、或 mod 库页"
            "右键「设定本地版本」；检测只写远端一侧，检测到但没下载，"
            "下次检测还会再报）；「特别关注」的 mod 出新版本会记一条"
            "提醒（同一个版本只提醒一次）。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet(f"border:none; color:#8a8a8f; font-size: {font_px(12)}px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # 四张固定步骤卡片（整页常驻，状态随流程更新）
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 档案卡：只显示归属与空态引导
        self._d1, _ = self._make_card("step1", "① 确认目标档案")

        # ② 检测卡：按钮 + 进度条 + 状态行
        self._d2, box2 = self._make_card("step2", "② 更新检测")
        row2 = QHBoxLayout()
        self._btn_check = QPushButton("开始检测（后台运行）", self)
        self._btn_check.setToolTip(
            "在后台检测本档案的全部 mod：逐个比对远端与你确认过的"
            "本地版本，本页进度条实时显示；逐条播报见底部控制台 · "
            "运行日志，发现新版本会弹出更新确认清单")
        self._btn_check.clicked.connect(lambda: self.check_requested.emit())
        row2.addWidget(self._btn_check)
        row2.addStretch(1)
        box2.addLayout(row2)

        # 检测进度条：没在检测时隐藏、不占布局空间。数据由更新检测页
        # 的进度信号直连（决策 42：本页是第二块表盘，不是第二套计算）
        self._bar = QProgressBar(self)
        self._bar.setVisible(False)
        self._bar.setFormat("已比对 %v / %m")
        box2.addWidget(self._bar)

        # ③ ④ 两张卡是固定操作指引（清单与批次都走既有链，本页不复制）
        self._d3, _ = self._make_card("step3", "③ 备份 + 下载（更新确认清单里执行）")
        self._d4, _ = self._make_card("step4", "④ 确认入账（批次收尾的确认清单）")

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用 / 检测页信号直连） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播（主窗口按 set_game 自动分发，本页零接线成本）。"""
        self._game = game
        self._reset_flow()

    def on_check_started(self) -> None:
        """检测已受理（主窗口在检测页受理成功后调用）：进度条先进
        忙态（总数未知，来回滚动），按钮锁住防重复点击。"""
        self._bar.setRange(0, 0)
        self._bar.setVisible(True)
        self._btn_check.setEnabled(False)
        self._set_card(self._d2, "检测进行中：正在等待第一批查询结果…", _C_INFO)

    def on_check_progress(self, done: int, total: int) -> None:
        """检测进度（更新检测页 progress_changed 直连本方法）。数据与
        检测页自己的进度条同源同刻——本页只是第二块表盘；从检测页
        手动开测时本页进度条也会跟着亮起来。"发现几个"要等全部查完
        才知道，进行中只报比对数。"""
        self._bar.setVisible(True)
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        self._btn_check.setEnabled(False)  # 进行中一律不可再点
        self._set_card(self._d2, f"检测进行中：已比对 {done}/{total}…", _C_INFO)

    def on_check_interrupted(self, reason: str) -> None:
        """检测非成功收尾（手动停止 / 网络层失败）：收起进度条、
        恢复按钮，卡片如实记录原因。成功收尾走 on_check_done。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)
        self._set_card(self._d2, f"检测已中断：{reason}", _C_WARN)

    def on_check_done(self, found: int) -> None:
        """检测完成的回叫（更新检测页 checks_finished 直连）。
        发现 0 个也要说清楚——不更新的日子要能看到"查过了、都是新
        的"。发现 >0 时本卡文案马上会被 on_updates_found 覆盖成
        更详细的指引（清单弹出 / 自动开批）。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)
        if found:
            self._set_card(self._d2,
                           f"检测完成：发现 {found} 个 mod 有新版本。",
                           _C_INFO)
        else:
            self._set_card(self._d2,
                           "检测完成：没有发现新版本（或档案没有可检测"
                           "的条目）。",
                           _C_OK)

    def on_updates_found(self, app_id: int, n: int, auto: bool) -> None:
        """确有新版本（主窗口 _on_updates_found 转发，只在发现 >0 时）。
        auto=True = 设置勾了「发现更新后自动开始下载」：主窗口直接开批
        （自动批次刻意不带备份——无人值守的批次不夹带备份阶段；想带
        备份，关掉自动勾选走人工路）。False = 更新确认清单已弹出，
        等用户在里面勾选与执行（清单不关、可分批反复执行）。"""
        g = self._repo.get_game(app_id)
        who = g.name if g is not None else str(app_id)
        if auto:
            self._set_card(
                self._d2,
                f"发现 {n} 个 mod 有新版本（档案：{who}）——已按设置"
                "直接开批下载（自动批次不带备份；进度见底部控制台 ·"
                "「下载批次」）。",
                _C_INFO)
        else:
            self._set_card(
                self._d2,
                f"发现 {n} 个 mod 有新版本（档案：{who}）——更新确认"
                "清单已弹出：勾选条目、底部选动作（备份+更新 / 仅更新）、"
                "点【执行选中】；清单可以反复操作，分几批执行都行。",
                _C_INFO)

    # ---------- 内部：清场与卡片基建（样式与加入新 mod 页同款） ----------
    def _reset_flow(self) -> None:
        """初始化与切档案共用：按钮和四张卡回到"没开始"的状态。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)

        if self._game is None:
            self._set_card(self._d1, "未选择档案——请先在左上角添加或选择游戏档案",
                           _C_WARN)
            self._set_card(self._d2, "等待选择档案。", _C_MUTED)
        else:
            self._set_card(
                self._d1,
                f"当前档案：{self._game.name}（{self._game.app_id}）——"
                "检测与下载都以这个档案为准；换游戏请先在左上角切换",
                _C_OK)
            self._set_card(
                self._d2,
                "点【开始检测】在后台比对远端版本与你确认过的本地版本，"
                "本页进度条实时显示；完成后自动弹出更新确认清单。",
                _C_MUTED)

        # ③④ 固定操作指引（不随流程变——清单和批次都由既有链接管）
        self._set_card(
            self._d3,
            "在更新确认清单里勾选条目、底部选动作、点【执行选中】："
            "选「备份+更新」的条目先备份当前版本再下载（本地版本未"
            "确认的条目会被备份守卫剔出批次，先到 mod 库页右键"
            "「设定本地版本」）；批次在底部控制台 ·「下载批次」里"
            "执行（需要 steamcmd 已启动并登录）。",
            _C_MUTED)
        self._set_card(
            self._d4,
            "批次结束后自动弹出确认清单：终端明确报成功的下载在这里"
            "排队，勾选 = 本地版本入账（背书须显式）；当场关掉也不丢，"
            "【入账中心 · 待确认】区随时接着处理。",
            _C_MUTED)

    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张固定步骤卡，返回（状态行, 内容布局）。
        样式选择器限定到本框（QFrame#card_xxx）：QLabel 也是 QFrame
        子类，不限定会把边框画到卡里每行字上——既有页面的同款处理。"""
        frame = QFrame(self)
        frame.setObjectName(f"card_{name}")
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        detail = QLabel("", frame)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"border:none; color:{_C_MUTED};")
        box.addWidget(title_lbl)
        box.addWidget(detail)
        self._cards_box.addWidget(frame)
        return detail, box

    def _set_card(self, detail: QLabel, text: str, color: str) -> None:
        detail.setText(text)
        detail.setStyleSheet(f"border:none; color:{color};")
