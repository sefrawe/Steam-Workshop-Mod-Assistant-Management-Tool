"""游戏退场功能模块
"""
r"""gui/gameExitPage.py —— 功能模块「游戏退场」（决策 96）。

五步引导壳，零新引擎（决策 34 第二档，migrationPage 同款形态）：
①留后路（导出分享包/完整账本，转调主窗口既有方法）→ ②备份处置
（备份总览正门按本档案过滤）→ ③下载内容处置（gameExitFlow 受四道
红线保护的整目录删除）→ ④游戏侧处置（指引不代劳）→ ⑤删除档案
（gameDeleteDialog 现成清账）。

四个转调信号，主窗口逐一转调既有方法；唯一的新破坏性动作是第③步
的 content 整目录删除——引擎侧四道红线 + 界面确认弹窗（展示完整
路径 + steamcmd 在跑警告），删除放后台线程、没有停止点、跑动期间
锁全部按钮（gameDeleteDialog 同款纪律）；shutdown() 供主窗口退出
时等线程收尾（MainWindow.closeEvent 的页面循环自动调用）。

只与 ModRepository / gameExitFlow 接口交互，GUI 层零 SQL。
"""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.appSettings import AppSettings
from core.models import Game
from gui.consolePanel import LogBus
from gui.formatters import fmt_size
from workflows import gameExitFlow

# 与批量下载步骤卡片、日常更新模块页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 无需处理
_C_FAIL = "#e5484d"   # 红线拒绝 / 反向拓扑警示
_C_WARN = "#f5a623"   # 需要注意 / 还没有档案
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行


class _WipeWorker(QThread):
    """content 整目录删除线程：调引擎删一个目录，没有停止点
    （gameDeleteDialog 同款纪律）。done 的 ok=False = 引擎红线拒绝
    或操作层面失败（详情是人话，不是 bug）；crashed 才是预期外异常。"""
    done = Signal(bool, str)
    crashed = Signal(str)

    def __init__(self, download_dir: str, app_id: int) -> None:
        super().__init__()
        self._download_dir = download_dir
        self._app_id = app_id

    def run(self) -> None:
        try:
            ok, detail = gameExitFlow.delete_content_dir(
                self._download_dir, app_id=self._app_id)
        except Exception as exc:  # 预期外：bug 性质
            self.crashed.emit(str(exc))
            return
        self.done.emit(ok, detail)


class GameExitPage(QWidget):
    """「游戏退场」模块页：五步引导，随当前档案联动。"""

    # ①a 导出分享包（主窗口转调 _export_sharepack：当前档案）
    share_out_requested = Signal()
    # ①b 导出完整账本（主窗口转调 _export_ledger：全部档案全量）
    ledger_export_requested = Signal()
    # ② 打开备份总览并按本档案过滤（删除对话框 overview_requested 同一落点）
    overview_requested = Signal(int)
    # ⑤ 删除当前档案（转调档案切换器的删除对话框，「游戏」菜单同一份）
    delete_archive_requested = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        self._survey: gameExitFlow.ExitSurvey | None = None
        self._worker: _WipeWorker | None = None
        self._build_ui()
        self._refresh()

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

        title = QLabel("游戏退场", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 红线"）
        note = QFrame(self)
        note.setObjectName("gameexit_note")
        note.setStyleSheet(
            "QFrame#gameexit_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：①（可选）导出分享包或完整账本留后路 → ② 备份处置 →"
            " ③ 删除下载内容（content 下载目录）→ ④ 游戏侧处置（指引"
            "不代劳）→ ⑤ 删除档案清账。顺序从软到硬：先留后路、再动"
            "磁盘、最后清账；左上角切到哪个档案，退的就是哪个游戏。",
            "为什么这么做：一个游戏退场要动的东西散在四处（备份区、"
            "content 下载目录、游戏自己的 mod 目录、账本档案），本页"
            "把它们串成一条线；每一步都是既有功能的入口，唯一的新删除"
            "动作（第③步）带四道红线保护（见下）。",
            "红线：content 下载目录本身是联接（反向拓扑）时绝不递归"
            "删除——真实内容在游戏侧，只给 rmdir 摘链命令；目录树里"
            "发现任何联接一律拒绝整删；steamcmd 正在跑只警告不拦"
            "（被误删的内容重新下载即可回来）。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行（踩坑⑨）
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        # 五张固定步骤卡片（整页常驻，随档案切换与盘点刷新）
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 留后路（可选）
        self._d1, box1 = self._make_card("step1", "① 留后路（可选）")
        self._btn_share = self._add_button(
            box1, "导出分享包（当前游戏）…",
            "把当前游戏的收录清单（含备注、标签、特别关注）导出为 "
            "JSON；纯读操作。与「文件」菜单同名动作是同一份")
        self._btn_share.clicked.connect(self.share_out_requested.emit)
        self._btn_ledger = self._add_button(
            box1, "导出完整账本…",
            "全部档案与全部 mod 记录（含快照、备份登记、操作日志）"
            "导出为一个 JSON；纯读操作。与「文件」菜单同名动作是同一份")
        self._btn_ledger.clicked.connect(self.ledger_export_requested.emit)

        # ② 备份处置
        self._d2, box2 = self._make_card("step2", "② 备份处置")
        self._btn_overview = self._add_button(
            box2, "打开备份总览（按本游戏过滤）…",
            "备份总览页里可以逐份钉住、删登记、删文件（勾选操作）；"
            "第⑤步的删除档案对话框里也有「同时删除磁盘备份」勾选，"
            "想一步到位可以留在那一步")
        self._btn_overview.clicked.connect(self._on_overview)

        # ③ 下载内容处置（反向拓扑时的摘链命令标签常驻卡内，按需显隐）
        self._d3, box3 = self._make_card("step3", "③ 删除下载内容（content）")
        self._lbl_cmd3 = QLabel("", self)
        self._lbl_cmd3.setWordWrap(True)
        self._lbl_cmd3.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_cmd3.setStyleSheet("border:none; color:#d4d4d4;")
        self._lbl_cmd3.hide()
        box3.addWidget(self._lbl_cmd3)
        self._btn_wipe = self._add_button(
            box3, "删除下载内容…",
            "整目录删除本游戏的 content 下载目录（确认弹窗会展示完整"
            "路径）；删除后重新下载即可找回")
        self._btn_wipe.clicked.connect(self._on_wipe_content)

        # ④ 游戏侧处置（纯指引，无破坏性按钮——决策 69⒊ 纪律）
        self._d4, box4 = self._make_card(
            "step4", "④ 游戏侧处置（指引，不代劳）")
        self._lbl_cmd4 = QLabel("", self)
        self._lbl_cmd4.setWordWrap(True)
        self._lbl_cmd4.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_cmd4.setStyleSheet("border:none; color:#d4d4d4;")
        self._lbl_cmd4.hide()
        box4.addWidget(self._lbl_cmd4)

        # ⑤ 删除档案（清账收尾）
        self._d5, box5 = self._make_card("step5", "⑤ 删除档案（清账收尾）")
        self._btn_delete = self._add_button(
            box5, "删除当前游戏档案…",
            "打开删除档案对话框（「游戏」菜单同一份）：先盘点再删、"
            "自动生成数据库快照兜底、可勾选同时删除磁盘备份文件")
        self._btn_delete.clicked.connect(self.delete_archive_requested.emit)

        root.addStretch(1)

    # ---------- 对外（MainWindow / 广播调用） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播：重盘点并刷新全部卡片。"""
        self._game = game
        self._refresh()

    def refresh(self) -> None:
        """主窗口切到本页时调用：联接形态、盘上有无、steamcmd 是否
        在跑都可能刚变过，重新盘点。"""
        self._refresh()

    def shutdown(self) -> None:
        """主窗口退出前调用：删除没有停止点，等它跑完。"""
        if self._worker is not None:
            self._worker.wait()

    # ---------- 槽 ----------
    def _on_overview(self) -> None:
        if self._game is not None:
            self.overview_requested.emit(self._game.app_id)

    def _on_wipe_content(self) -> None:
        s = self._survey
        if s is None or self._worker is not None:
            return
        if not s.content_exists or s.content_is_link:
            return  # 按钮本应置灰；双保险
        lines = [
            f"将整目录删除下载内容目录：\n{s.download_dir}\n",
            "里面是该游戏经 steamcmd 下载的全部 mod 内容；删除后重新"
            "下载即可找回。",
        ]
        if s.steamcmd_running:
            lines.append("注意：steamcmd 正在运行——此刻删除可能与下载"
                         "争用；被误删的条目重跑下载命令即可回来。")
        lines.append("\n确定继续吗？")
        ret = QMessageBox.question(
            self, "删除下载内容", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            self._log.info("已取消删除下载内容")
            return
        self._log.info(f"开始删除下载内容目录：{s.download_dir}")
        self._worker = _WipeWorker(s.download_dir, s.app_id)
        self._worker.done.connect(self._on_wipe_done)
        self._worker.crashed.connect(self._on_wipe_crashed)
        self._worker.finished.connect(self._on_wipe_finished)
        self._reset_flow()  # 忙态：全部按钮锁定（跑动期间没有停止点）
        self._worker.start()

    def _on_wipe_done(self, ok: bool, detail: str) -> None:
        (self._log.ok if ok else self._log.warn)(f"下载内容处置：{detail}")

    def _on_wipe_crashed(self, message: str) -> None:
        self._log.error(f"删除下载内容时出现预期外错误：{message}")
        QMessageBox.critical(self, "游戏退场",
                             f"执行中断：{message}\n\n"
                             "若目录已部分删除，剩余部分可手动清理；"
                             "档案账目未受影响。")

    def _on_wipe_finished(self) -> None:
        w = self._worker
        self._worker = None
        if w is not None:
            w.wait()
        self._refresh()  # 收尾后重盘点：卡片与按钮按最新现状刷新

    # ---------- 内部：盘点与卡片 ----------
    def _steamcmd_path(self) -> str | None:
        if self._settings is None:
            return None
        return self._settings.get("steamcmd_path") or None

    def _refresh(self) -> None:
        if self._game is None:
            self._survey = None
        else:
            try:
                self._survey = gameExitFlow.survey(
                    self._repo, self._game, self._steamcmd_path())
            except ValueError:
                # 档案在广播与盘点之间刚被删（极端竞态）：按无档案处理
                self._game = None
                self._survey = None
        if self._survey is not None and self._survey.dir_rederived:
            self._log.info(
                f"「{self._survey.game_name}」的下载目录已按当前 "
                "steamcmd 位置重推导（仅本次退场使用，不改档案）："
                f"{self._survey.download_dir}")
        self._reset_flow()

    def _reset_flow(self) -> None:
        """初始化 / 切档案 / 收尾共用：按盘点结果分派五张卡。"""
        busy = self._worker is not None
        game = self._game
        s = self._survey
        has = game is not None and s is not None

        # 按钮可用性：忙 = 全锁；无档案锁"要档案"的那几颗
        self._btn_share.setEnabled(not busy and game is not None)
        self._btn_ledger.setEnabled(not busy)
        self._btn_overview.setEnabled(not busy and has)
        self._btn_wipe.setEnabled(
            not busy and has and s.content_exists and not s.content_is_link)
        self._btn_delete.setEnabled(not busy and has)

        # ① 留后路
        if game is None:
            self._set_card(self._d1,
                "先在左上角选中要退场的游戏档案（导出完整账本不受此限，"
                "随时可用）。", _C_WARN)
        else:
            self._set_card(self._d1,
                "可选。分享包 = 当前游戏的收录清单；完整账本 = 全部档案"
                "全量（含快照与备份登记）。带走了，日后想回来也有得恢复；"
                "不带走也不影响后面步骤。", _C_MUTED)

        # ② 备份处置
        self._lbl_cmd3.hide()
        self._lbl_cmd4.hide()
        if not has:
            self._set_card(self._d2,
                "选中档案后，这里显示该游戏的备份登记情况。", _C_WARN)
        elif s.backup_count == 0:
            self._set_card(self._d2,
                "本游戏没有备份登记——本步无需处理（想全局整理备份"
                "仍可打开总览页）。", _C_OK)
        else:
            tip = ""
            if s.backup_found < s.backup_count:
                tip = (f"（盘上只找到 {s.backup_found} 份，"
                       "其余多半已被手动删除）")
            self._set_card(self._d2,
                f"本游戏有备份登记 {s.backup_count} 份，登记合计约 "
                f"{fmt_size(s.backup_bytes)}{tip}。到总览页逐份决定"
                "去留；或留到第⑤步勾选一并带走。", _C_MUTED)

        # ③ 下载内容处置：按下载目录真实形态分派
        if not has:
            self._set_card(self._d3,
                "选中档案后，这里按下载目录的真实形态给动作。", _C_WARN)
        elif s.content_is_link:
            # 反向拓扑：按钮已置灰，给摘链命令
            self._set_card(self._d3,
                "下载目录本身是目录联接（反向拓扑）：真实内容在游戏 "
                "mod 目录侧。这里绝不能递归删除——用下面的命令把联接"
                "摘掉即可（只摘链，不碰任何真实文件）；真实内容去第④"
                "步处置。", _C_FAIL)
            self._lbl_cmd3.setText(
                f'rmdir "{s.download_dir}"    （选中复制到 cmd 里执行）')
            self._lbl_cmd3.show()
        elif not s.content_exists:
            self._set_card(self._d3,
                "下载目录在盘上不存在（从未下载过，或内容已在别处删除）"
                "——本步无需处理。", _C_OK)
        else:
            extra = ("注意：steamcmd 正在运行，确认时会有提示。"
                     if s.steamcmd_running else "")
            self._set_card(self._d3,
                f"content 下载目录在盘上（实体目录）：{s.download_dir}"
                f"　{extra}整目录删除后想反悔，重新下载即可找回。",
                _C_MUTED)

        # ④ 游戏侧处置：按游戏 mod 目录真实形态给指引
        if not has:
            self._set_card(self._d4,
                "选中档案后，这里按游戏 mod 目录的真实形态给指引。",
                _C_WARN)
        elif not s.game_mod_dir:
            self._set_card(self._d4,
                "档案没有登记游戏 mod 目录（「游戏 → 编辑档案」可补）。"
                "只有只从自己目录读 mod 的游戏才有这一步要处理。",
                _C_MUTED)
        elif s.game_mod_dir_is_link:
            self._set_card(self._d4,
                "游戏 mod 目录是联接（正向拓扑）：mod 文件实际住在 "
                "steamcmd 下载目录里，游戏目录本身没有真东西。用下面的"
                "命令把联接摘掉即可还原原状；摘链不删除任何真实文件"
                "（第③步删过下载目录的话，联接此刻是悬空的，照样可摘）。",
                _C_MUTED)
            self._lbl_cmd4.setText(
                f'rmdir "{s.game_mod_dir}"    （选中复制到 cmd 里执行）')
            self._lbl_cmd4.show()
        elif s.content_is_link:
            self._set_card(self._d4,
                "游戏 mod 目录是真实目录：本游戏是反向拓扑，mod 文件就"
                "住在这里。是否删除、连同配置与存档如何处置，由你在资源"
                "管理器自行决定（本工具不代劳）。路径可选中复制：",
                _C_MUTED)
            self._lbl_cmd4.setText(s.game_mod_dir)
            self._lbl_cmd4.show()
        else:
            self._set_card(self._d4,
                "游戏 mod 目录是真实目录：里面可能有 mod、配置或存档，"
                "是否删除由你自行决定（本工具不代劳）。路径可选中复制：",
                _C_MUTED)
            self._lbl_cmd4.setText(s.game_mod_dir)
            self._lbl_cmd4.show()

        # ⑤ 删除档案
        if not has:
            self._set_card(self._d5, "同上：先有档案，才有账可清。",
                           _C_WARN)
        else:
            self._set_card(self._d5,
                f"最后清账：删除「{s.game_name}（{s.app_id}）」名下全部"
                f"账目——mod 记录 {s.mod_total} 条（含软删除）、版本"
                "快照、备份登记、失效归档。对话框会再盘点一次并自动生成"
                "数据库快照兜底；content 下载目录与游戏侧目录不受它影响"
                "（③④步已各自处理）。", _C_MUTED)

    # ---------- 卡片基建（migrationPage 同款） ----------
    def _add_button(self, box: QVBoxLayout, text: str,
                    tooltip: str) -> QPushButton:
        """往卡片内容布局里加一颗带 tooltip 的按钮，返回它。"""
        btn = QPushButton(text, self)
        btn.setToolTip(tooltip)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        box.addLayout(row)
        return btn

    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张固定步骤卡，返回（状态行, 内容布局）。样式选择器限定
        到本框（QFrame#exit_card_xxx）：QLabel 也是 QFrame 子类，不限定
        会把边框画到卡里每行字上——既有页面的同款处理。"""
        frame = QFrame(self)
        frame.setObjectName(f"exit_card_{name}")
        frame.setStyleSheet(
            f"QFrame#exit_card_{name} {{ border: 1px solid #3a3a3a;"
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
