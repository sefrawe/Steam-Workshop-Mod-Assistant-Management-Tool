"""卸载与清理指引页
"""
r"""gui/uninstallPage.py —— 功能模块「卸载与清理」（决策 70）。

本软件是便携软件：账本、设置、界面记忆全部住在软件文件夹里
（data/ 与 config/）。所以卸载只有一步——资源管理器里删掉整个
软件文件夹；本页把"删之前该知道的事"摆清楚，三张卡：

① 界面记忆的存放位置：老版本的界面记忆存在 Windows 注册表里。
   main.py 启动时已自动把旧键搬进软件文件夹（gui/sessionStore，
   幂等）；本卡显示状态，注册表里还有旧键时给出一键迁移。
② 注册表残留指引：迁移后注册表键成为无害残留（不再被读取）。
   想删干净给出 regedit 路径与步骤——不代删，删不删用户定。
③ 卸载盘点：逐文件列出 data/ 与 config/ 里的数据文件、各自是
   什么、多大——盘点只读，删除动作只有一个：删整个文件夹。

回程票（两条都说清）：账本想带走 → 到【换机迁移】第①步导出
完整账本；界面记忆想还原 → 删掉文件夹内的会话文件再重启，会
从注册表原稿再迁一次（迁移不销毁原稿）。

工坊内容、备份区、steamcmd 本体是用户资产，在软件文件夹之外，
删文件夹碰不到——说明块里说死，防误删恐慌。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)
from core.appPaths import app_root
from gui.consolePanel import LogBus
from gui.formatters import fmt_size
from gui.sessionStore import (
    last_startup_migration, migrate_from_registry, registry_has_keys,
    registry_key_display, session_file_exists, session_file_path,
    startup as session_startup,
)
from workflows.uninstallFlow import scan_folder

# 与批量下载步骤卡片、日常更新模块页一致的配色
_C_OK = "#46a758"    # 完成 / 一切正常
_C_WARN = "#f5a623"  # 需要注意（有残留、有待迁移）
_C_MUTED = "#8a8a8f" # 说明文字 / 无事可做


class UninstallPage(QWidget):
    """「卸载与清理」页：迁移界面记忆 → 指引注册表残留 → 盘点文件夹。"""

    def __init__(self, repo, settings=None, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo      # 保留统一构造签名（本页不直接读库）
        self._settings = settings
        self._log = log or LogBus()
        self._build_ui()
        self.refresh()

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

        title = QLabel("卸载与清理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（功能模块页的三行规矩：流程 / 为什么 / 边界）
        note = QFrame(self)
        note.setObjectName("un_note")
        note.setStyleSheet("QFrame#un_note { border: 1px solid #3a3a3a;"
                           " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "本软件是便携软件：账本、设置、界面记忆全部住在软件文件夹里"
            "（data/ 与 config/）。卸载只有一步——在资源管理器里把软件"
            "整个文件夹删掉；没有隐藏的安装目录，不写系统服务。",
            "删之前看一眼第③卡盘点：账本想带走，先到【换机迁移】第①步"
            "导出完整账本；界面记忆（折叠、列宽）随文件夹走，不用备份。",
            "工坊内容（steamcmd 的 content）、备份区（mod_backups）、"
            "steamcmd 本体都是你的资产，在软件文件夹之外——删软件文件夹"
            "碰不到它们。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # ① 界面记忆的存放位置
        self._d1, box1 = self._make_card("step1", "① 界面记忆的存放位置")
        self._d1.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)  # 路径可选中复制
        row1 = QHBoxLayout()
        self._btn_recheck = QPushButton("重新检查", self)
        self._btn_recheck.setToolTip("重新检查会话文件与注册表残留的状态")
        self._btn_recheck.clicked.connect(self.refresh)
        self._btn_migrate = QPushButton("从注册表迁入", self)
        self._btn_migrate.setToolTip(
            "把旧版本存在 Windows 注册表里的界面记忆搬进软件文件夹"
            "（迁过一次就不会再动）")
        self._btn_migrate.clicked.connect(self._on_migrate)
        row1.addWidget(self._btn_recheck)
        row1.addWidget(self._btn_migrate)
        row1.addStretch(1)
        box1.addLayout(row1)

        # ② 注册表残留指引
        self._d2, box2 = self._make_card("step2", "② 注册表残留（旧版记忆）")
        self._d2.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)  # 键路径可手动选中
        row2 = QHBoxLayout()
        self._btn_copy_reg = QPushButton("复制注册表路径", self)
        self._btn_copy_reg.setToolTip(
            "把注册表键的完整路径复制到剪贴板，粘到 regedit 地址栏即可定位")
        self._btn_copy_reg.clicked.connect(self._on_copy_reg)
        row2.addWidget(self._btn_copy_reg)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ③ 卸载盘点
        self._d3, box3 = self._make_card("step3", "③ 盘点：软件留下了什么")
        row3 = QHBoxLayout()
        self._btn_scan = QPushButton("盘点软件留下的文件", self)
        self._btn_scan.setToolTip(
            "只读列出 data/ 与 config/ 里的数据文件、各自是什么、多大；"
            "不做任何删除")
        self._btn_scan.clicked.connect(self._on_scan)
        row3.addWidget(self._btn_scan)
        row3.addStretch(1)
        box3.addLayout(row3)
        self._scan_summary = QLabel("", self)
        self._scan_summary.setWordWrap(True)
        self._scan_summary.setStyleSheet("border:none;")
        self._scan_summary.setVisible(False)
        box3.addWidget(self._scan_summary)
        self._scan_list = QListWidget(self)
        self._scan_list.setVisible(False)
        box3.addWidget(self._scan_list)

        # 页脚：软件文件夹位置（卸载动作的唯一目标，路径可选中）
        self._folder_label = QLabel("", self)
        self._folder_label.setWordWrap(True)
        self._folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._folder_label.setStyleSheet(
            f"border:none; color:{_C_MUTED}; font-size:12px;")
        root.addWidget(self._folder_label)
        root.addStretch(1)

    # ---------- 对外 ----------
    def refresh(self) -> None:
        """重读会话文件/注册表状态并刷新三张卡（切到本页时可调）。"""
        self._refresh_cards()

    def set_game(self, game) -> None:
        """本页与档案无关（统一构造签名，广播来了不做事）。"""

    # ---------- 内部 ----------
    def _on_migrate(self) -> None:
        # 走 startup()：顺手把本进程的落点也切过去（幂等，重复调用安全）
        n = session_startup()
        if n:
            self._log.ok(f"已把 {n} 条界面记忆从注册表迁入软件文件夹")
        else:
            self._log.info("注册表没有需要迁移的界面记忆（或已迁过）")
        self._refresh_cards()

    def _on_copy_reg(self) -> None:
        QApplication.clipboard().setText(registry_key_display())
        self._log.info("已复制注册表键路径（regedit 地址栏粘贴可定位）")

    def _on_scan(self) -> None:
        rep = scan_folder(app_root())
        self._scan_list.clear()
        for e in rep.entries:
            self._scan_list.addItem(
                f"{e.rel_path}（{fmt_size(e.size_bytes)}）—— {e.role}")
        if not rep.entries:
            self._scan_summary.setText(
                "还没留下任何数据文件（账本/设置都还没生成过）——"
                "删除软件文件夹即完成卸载。")
            self._scan_list.setVisible(False)
        else:
            self._scan_summary.setText(
                f"共 {len(rep.entries)} 个文件，合计 {fmt_size(rep.total_bytes)}"
                "——全部住在软件文件夹内，随文件夹删除而清空；"
                "文件夹之外只剩第②卡的注册表残留。")
            self._scan_list.setVisible(True)
        self._scan_summary.setVisible(True)
        self._log.info(f"卸载盘点完成：{len(rep.entries)} 个数据文件，"
                       f"合计 {fmt_size(rep.total_bytes)}")

    def _refresh_cards(self) -> None:
        has_file = session_file_exists()
        has_reg = registry_has_keys()
        n_auto = last_startup_migration()
        # —— ① 界面记忆的存放位置 ——
        if has_file:
            text = (f"界面记忆已存放在软件文件夹内：\n"
                    f"{session_file_path()}\n"
                    "折叠状态、列宽这些记忆随文件夹一起搬走或删除。")
            color = _C_OK
            if n_auto:
                text += f"\n（本次启动已自动从注册表迁入 {n_auto} 条。）"
            if has_reg:
                text += ("\n注册表里仍留着旧版记忆键（迁移不删原稿，"
                         "已不再读取）——清理方法见第②卡；想还原，删掉"
                         "上面那个会话文件再重启即可（回程票）。")
            self._btn_migrate.setVisible(False)
        elif has_reg:
            text = ("检测到旧版本存在 Windows 注册表里的界面记忆——点"
                    "【从注册表迁入】把它搬进软件文件夹；迁入后本软件的"
                    "界面记忆就都归文件夹管了。")
            color = _C_WARN
            self._btn_migrate.setVisible(True)
        else:
            text = ("暂无界面记忆（折叠状态、列宽等还没用过）——"
                    "首次产生时会直接存进软件文件夹。")
            color = _C_MUTED
            self._btn_migrate.setVisible(False)
        self._set_card(self._d1, text, color)
        # —— ② 注册表残留 ——
        reg_path = registry_key_display()
        if has_reg:
            self._set_card(self._d2,
                f"注册表键：{reg_path}\n"
                "里面是旧版存下的界面记忆字符串。迁入文件夹后这里不再被"
                "读取，留着无害；想删干净：Win+R 输入 regedit 回车 → "
                "把上面的路径粘进顶部地址栏回车 → 左侧树里右键该键 → "
                "删除。只删本软件名下这一节，别的不动。", _C_WARN)
            self._btn_copy_reg.setEnabled(True)
        else:
            self._set_card(self._d2,
                f"注册表没有本软件的残留键（{reg_path} 不存在或为空）"
                "——无需处理。", _C_MUTED)
            self._btn_copy_reg.setEnabled(False)
        # —— 页脚 ——
        self._folder_label.setText(
            f"软件文件夹：{app_root()}\n"
            "卸载 = 在资源管理器里删除上面这个文件夹（先看第③卡盘点；"
            "账本想带走先去【换机迁移】导出完整账本）。")

    # ---------- 卡片基建（样式与日常更新页同款）----------
    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张步骤卡，返回（状态行, 内容布局）。样式选择器限定到
        本框（QFrame#card_xxx）：QLabel 也是 QFrame 子类，不限定会把
        边框画到卡里每行字上——既有页面的同款处理。"""
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
