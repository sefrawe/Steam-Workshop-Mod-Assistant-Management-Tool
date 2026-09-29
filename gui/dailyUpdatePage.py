"""日常更新功能模块
"""
"""
三步引导页：把"更新检测 → 确认动作 → 备份+下载 → 复扫入账"这条
日常主线装进一个页面（决策 26 一条龙的界面壳 + 决策 40 备份联动，
T22）。

分工口径（与「加入新 mod」模块同一套纪律，本页零逻辑复制）：
- 检测链只有【更新检测】页那一份——第②步按钮只发 check_requested
  信号，请主窗口在后台开测（不跳页）；进度由检测页的进度信号原样
  转发过来，本页进度条实时显示——模块页的过程可见性对齐基础功能
  页（决策 42），检测逻辑一行不复制；
- 确认清单是 gui/updateSelectDialog（T20c/决策 40）：勾选条目 +
  底部统一选动作（备份+更新 / 仅更新，不勾 = 不动），执行不关窗、
  可分批反复执行；
- 备份与下载全链住在批量下载控制器（备份阶段=决策 40）：选了
  "备份+更新"的条目先备份旧版本再下载，备份失败的不动它——
  第③步卡片只显示回叫来的批次汇总；
- 复扫入账复用【mod 库】页的扫描链（quiet 版）：批次结束后自动
  执行——第④步卡片只显示复扫结论。

卡片语义：这里显示的是"最近一次"的进展——检测发现几个、批次
下载结果、复扫完没完。无论这批从本模块还是 mod 库页发起，批次
收尾都会回叫到这里（无条件显示，不判断来源）。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus

# 与批量下载步骤卡片、加入新 mod 页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 一切正常
_C_FAIL = "#e5484d"   # 出错
_C_WARN = "#f5a623"   # 需要注意（有失败/超时、被取消、复扫没跑）
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行


class DailyUpdatePage(QWidget):
    """「日常更新」模块页：三步引导 + 最近进展仪表盘。"""

    # 第②步【开始检测】：主窗口接——在后台开检测，不跳页
    check_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
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
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("日常更新", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（新规：每个功能模块页自带"流程 / 为什么 / 术语与关系"；
        # 文案红线：只提用户看得见的东西，不提脚本编号与代码结构）
        note = QFrame(self)
        note.setObjectName("daily_note")
        note.setStyleSheet("QFrame#daily_note { border: 1px solid #3a3a3a;"
                           " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
                "流程：① 确认档案 → ② 点【开始检测】在后台检测本档案全部"
                " mod（本页进度条实时显示）→ ③ 在弹出的清单里勾选条目、底部"
                "选动作、点【执行选中】（清单不关，可分批执行）→ 批量下载"
                "（底部控制台可见）→ ④ 批次结束后自动复扫入账。",
                "为什么这么做：更新前先把当前版本复制进备份区，新版不满意"
                "随时可以回滚（到【备份管理】页恢复）；检测、下载、扫描"
                "每一步用的都是既有的页面，本页把它们串成一趟，进度和结果"
                "也汇总在这里。",
                "术语与关系：确有新版本 = 远端版本比本地新（本地版本只认"
                "扫描回填，检测不改它——检测到但没下载，下次检测还会再报）；"
                "「特别关注」的 mod 出新版本会记一条提醒（同一个版本只提醒"
                "一次）；备份的查看与恢复在【备份管理】页。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
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
            "在后台检测本档案的全部 mod：逐个比对远端与本地版本，本页"
            "进度条实时显示进度；逐条播报见底部控制台 · 运行日志，"
            "完成后弹出确认清单")
        self._btn_check.clicked.connect(lambda: self.check_requested.emit())
        row2.addWidget(self._btn_check)
        row2.addStretch(1)
        box2.addLayout(row2)
        # 检测进度条：没在检测时隐藏、不占布局空间。数据由更新检测页
        # 的进度信号转发（决策 42：本页是第二块表盘，不是第二套计算）
        self._bar = QProgressBar(self)
        self._bar.setVisible(False)
        self._bar.setFormat("已比对 %v / %m")
        box2.addWidget(self._bar)

        # ③ ④ 两步全自动：卡片只显示回叫来的结论
        self._d3, _ = self._make_card("step3", "③ 备份 + 批量下载（确认清单后自动进行）")
        self._d4, _ = self._make_card("step4", "④ 复扫入账（批次结束后自动进行）")

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------

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
        self._set_card(self._d2,
                       "检测进行中：正在等待第一批查询结果…", _C_INFO)

    def on_check_progress(self, done: int, total: int) -> None:
        """检测进度（检测页 progress_changed 直连到本方法）。数据与
        检测页自己的进度条同源同刻——本页只是第二块表盘；从检测页
        手动开测时本页进度条也会跟着亮起来。"发现几个"要等全部查完
        才知道，进行中只报比对数。"""
        self._bar.setVisible(True)
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
        self._btn_check.setEnabled(False)  # 进行中一律不可再点
        self._set_card(self._d2,
                       f"检测进行中：已比对 {done}/{total}…", _C_INFO)

    def on_check_interrupted(self, reason: str) -> None:
        """检测非成功收尾（手动停止 / 网络层失败）：收起进度条、
        恢复按钮，卡片如实记录原因。成功收尾走 on_check_done。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)
        self._set_card(self._d2, f"检测已中断：{reason}", _C_WARN)

    def on_check_done(self, found: int) -> None:
        """检测完成的回叫（主窗口 _on_checks_finished 转发）。
        发现 0 个也要说清楚——不更新的日子要能看到"查过了、都是新的"。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)
        if found:
            self._set_card(self._d2,
                           f"检测完成：发现 {found} 个 mod 有新版本。",
                           _C_INFO)
        else:
            self._set_card(self._d2,
                           "检测完成：没有发现新版本（或档案没有可检测"
                           "的条目）。", _C_OK)

    def on_updates_found(self, app_id: int, n: int, auto: bool) -> None:
        """确认有新版本（主窗口 _on_updates_found 转发）。auto=True =
        设置勾了「发现更新后自动开始下载」，跳过清单全部先备份再更新；
        False = 确认清单已弹出，等用户在里面勾选与执行（清单不关、
        可分批反复执行，每批收尾照常回叫③④卡）。"""
        g = self._repo.get_game(app_id)
        who = g.name if g is not None else str(app_id)
        if auto:
            self._set_card(self._d2,
                           f"发现 {n} 个 mod 有新版本（档案：{who}）——"
                           "已按设置跳过询问，全部先备份再更新。", _C_INFO)
        else:
            self._set_card(self._d2,
                           f"发现 {n} 个 mod 有新版本（档案：{who}）——"
                           "确认清单已弹出：勾选条目、底部选动作、点"
                           "【执行选中】；清单可以反复操作，分几批执行"
                           "都行。", _C_INFO)

    def on_batch_finished(self, game_name: str | None, summary: dict,
                          rescanned: bool) -> None:
        """批次收尾回叫（主窗口在复扫之后调用，无论批次从哪发起）。
        summary 字段与批次汇总同源：ok/failed/timeout 清单 + total +
        stopped + error；backup_failed = 因备份失败没参与下载的条目
        （决策 40，它们的内容原样未动）。"""
        ok_n = len(summary.get("ok") or [])
        fail_n = len(summary.get("failed") or [])
        to_n = len(summary.get("timeout") or [])
        total = summary.get("total", ok_n + fail_n + to_n)
        bak_fail = summary.get("backup_failed") or []
        who = game_name or "档案未知"

        # ③ 备份+下载卡：批次结果
        error = summary.get("error")
        if error:
            self._set_card(self._d3,
                           f"最近一批（{who}）出错收尾：{error}", _C_FAIL)
        else:
            text = (f"最近一批（{who}）：成功 {ok_n} · 失败 {fail_n} · "
                    f"超时 {to_n}（共 {total}）")
            if bak_fail:
                text += f"；另有 {len(bak_fail)} 个因备份失败未下载" \
                        "（原内容未动，日志里有原因）"
            if summary.get("stopped"):
                self._set_card(self._d3, text + "——已停止", _C_WARN)
            elif fail_n == 0 and to_n == 0 and not bak_fail:
                self._set_card(self._d3, text, _C_OK)
            else:
                self._set_card(self._d3,
                               text + "（有未完成的条目，可重跑）", _C_WARN)

        # ④ 复扫卡：复扫结论（没复扫也要说清为什么、去哪补）
        if rescanned:
            self._set_card(self._d4,
                           f"复扫完成（{who}）：本地版本已对齐；"
                           "mod 库页点【刷新】即可看到最新状态。", _C_OK)
        else:
            self._set_card(self._d4,
                           "本批没有自动复扫（开关已关，或批次所属档案"
                           "已不存在）——需要时到【mod 库 · 刷新 ▾ · "
                           "扫描本地】手动确认。", _C_WARN)

    # ---------- 内部：清场与卡片基建（样式与加入新 mod 页同款） ----------

    def _reset_flow(self) -> None:
        """初始化与切档案共用：按钮和四张卡回到"没开始"的状态。
        ③④的"最近一批"结论刻意保留（批次是既成事实，不属于档案）。"""
        self._bar.setVisible(False)
        self._btn_check.setEnabled(self._game is not None)
        if self._game is None:
            self._set_card(self._d1,
                           "未选择档案——请先在左上角添加或选择游戏档案",
                           _C_WARN)
            self._set_card(self._d2, "等待选择档案。", _C_MUTED)
        else:
            self._set_card(
                self._d1,
                f"当前档案：{self._game.name}（{self._game.app_id}）——"
                "检测与下载都以这个档案为准；换游戏请先在左上角切换",
                _C_OK)
            self._set_card(self._d2,
                           "点【开始检测】在后台比对远端与本地版本，本页"
                           "进度条实时显示；完成后弹出确认清单。", _C_MUTED)
        self._set_card(self._d3,
                       "在清单里勾选条目、底部选动作、点【执行选中】："
                       "批次在底部控制台 ·「下载批次」里执行（需要 "
                       "steamcmd 已启动并登录）；选「备份+更新」的先"
                       "备份当前版本再下载；清单不关，可分批执行。",
                       _C_MUTED)
        self._set_card(self._d4,
                       "批次结束后自动复扫入账（默认开），mod 库页点"
                       "【刷新】即可看到最新版本。", _C_MUTED)

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
