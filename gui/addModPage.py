"""加入新 mod 功能模块
"""
"""
四步引导页：把"收集网址 → 入账 → 下载 → 盘点"整条链装进一个页面，
按步骤卡片逐段推进。逻辑全部住在 workflows/addModFlow（纯逻辑零 Qt，
pytest 覆盖）；本文件只做界面与流程串联——解析、入库、命令、盘点、
下载全部调引擎或委托既有链，不抄第二份。

与基础功能页的单源对照（v2.17 对账结论 + V2 判决制适配）：
- 解析 = core.urlParser.parse_lines（与【网址批量导入】同一份）；
- 入库 = ModRepository.add_mod 整批事务（与【网址批量导入】同入口）；
- 命令 = core.commandBuilder.build_copy_text（与【下载命令生成】同源）；
- 下载 = 批量下载控制器（download_requested 信号，契约与【mod 库】页
  【下载选中项】同款）——③步【开批下载】一键交批，批次收尾自动弹
  「确认清单」，勾选 = 本地版本入账（判决制的闭环）；
- 盘点（第④步）= addModFlow.bucket_statuses 只读盘点本批编号的账本
  现状（已下载 / 待下载分布）——只读账本不写任何状态（盘面事实的
  确认由入账中心的盘点认领链承担，本页不越界）。V1 的"扫描确认"
  （读 acf 自动回填状态）在判决制下已退役：账本状态只经确认门写入，
  机器不替人背书。

浏览器标签页采集（V1 完善功能归位）：依赖 core/tabCollector（实时
采集浏览器标签页，已收编进 V2）。第②步【从浏览器取标签页…】弹
页内选择器（gui/browserPickDialog，内嵌与【工作台 · 从浏览器取
网址】独立页同一份 BrowserTabPage）；勾选清单经 open_browser_picker
→ 主窗口 → receive_external_lines 回填本步并自动解析预览。采集
占用键鼠与中断方式见选择器页内说明。


防呆要点（继承自引擎与既有页面的同款规矩）：
- 入库前强制重新解析：预览属于"解析那一刻"的账本，中间可能变化；
- 切换档案即清场：解析结果、批次编号、命令框全部作废并明说一声
  （与【网址批量导入】同款提示）；输入框文本刻意保留（文本本身
  无档案归属，切回来重新解析即可）；
- 读文件用 utf-8-sig：自动吞掉 Windows 记事本存盘时加的 BOM 头，
  否则第一个编号会进"认不出"堆（基础功能页踩过的坑，此处对齐）；
- 数据库撞车（其他页面刚好导入了同一批编号）会整批回滚，弹窗说明
  后重新解析即可，绝不会出现"导了一半"。
"""
import sqlite3
from pathlib import Path
from core import tabCollector

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.appSettings import AppSettings
from core.formatters import status_zh
from core.models import Game
from gui.consolePanel import LogBus
from workflows import addModFlow
from gui.theme import font_px  # 字号单源（D25）

# 与批量下载步骤卡片、日常更新页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 一致
_C_FAIL = "#e5484d"   # 错误 / 硬拦
_C_WARN = "#f5a623"   # 需要注意（认不出的片段、还没入账的编号）
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 待执行

_ID_LIMIT = 20  # 明细里编号/片段最多原样列出多少个，超出折成"…"


class AddModPage(QWidget):
    # 【开批下载】：把本批编号交给批量下载控制器——与【mod 库】页
    # 【下载选中项】同一份信号契约（app_id, ids），同一批发起方式
    download_requested = Signal(int, list)
    # 【核验命令可行性…】：跳【快速命令查询】页并预填命令文本
    # （MainWindow 落点 _goto_quick_cmd——文本只填不跑，防呆不省）
    quick_command_requested = Signal(str)
    # 【从浏览器取标签页…】：弹页内选择器（BrowserPickDialog，内嵌
    # 与独立页同一份 BrowserTabPage）——V1 完善功能归位（tabCollector
    # 已收编）；依赖预检在 _go_browser，缺包就地给安装指引不弹空窗
    open_browser_picker = Signal()


    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings  # 本页暂不读设置，保留参数与其他页一致
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        # 第②步的解析结果（含防错档比对结论），第③步入库前会重新算
        self._preview: addModFlow.ParsePreview | None = None
        # 第③步入库成功的编号与所属档案：第④步盘点用。
        # 批次认"入库那一刻"的档案——之后切了档案也不串
        self._batch_ids: list[int] = []
        self._batch_app_id: int | None = None
        self._build_ui()
        self._reset_flow()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 整页装进滚动区（与 migrationPage 同款）：窗口矮就出滚动条
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        root = QVBoxLayout(body)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("加入新 mod", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"；
        # 文案红线：只提用户看得见的东西）
        note = QFrame(self)
        note.setObjectName("mod_note")
        note.setStyleSheet(
            "QFrame#mod_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
            "流程：① 确认档案 → ② 粘贴网址或读入文本、解析预览 → "
            "③ 入库 + 生成命令 →【开批下载】（批次结束后自动弹确认"
            "清单，勾选 = 入账）或复制命令到终端手动执行，跑完回来点"
            "【盘点本批状态】看看这批编号在账本里的现状。",
            "为什么这么做：解析只读不写——先看清新增/已在库/认不出再"
            "入库，防重复、防错档；命令与入库同源生成，账和命令永远"
            "一致；下载交给批次控制器执行，入账由收尾确认清单背书——"
            "本地版本只认你的勾选，机器不替人确认。",
            "术语与关系：待下载 = 账上有号、还没下载；已下载 = 文件在"
            "盘且你在确认清单里背过书。解析与【入账中心 · 登记】同一份；"
            "命令与【下载命令生成】同一份；下载与【mod 库 · 下载选中"
            "项】同一条链。",
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

        # ② 解析卡：明细行 + 输入框 + 按钮行
        self._d2, box2 = self._make_card("step2", "② 粘贴网址或读入文本")
        self._preview_detail = QLabel("", self)
        self._preview_detail.setWordWrap(True)
        self._preview_detail.setStyleSheet(f"border:none; font-size: {font_px(12)}px;")
        box2.addWidget(self._preview_detail)
        self._input = QPlainTextEdit(self)
        self._input.setPlaceholderText(
            "粘贴工坊网址或 steamcmd 下载命令（之前生成的命令文件内容"
            "可直接粘贴）：每行一条，整行空格分隔也行")
        self._input.setMaximumHeight(110)
        box2.addWidget(self._input)
        row2 = QHBoxLayout()
        self._btn_parse = QPushButton("解析预览", self)
        self._btn_parse.setToolTip(
            "解析输入内容并与当前档案的账本对表：分出新增/已在库/认不出"
            "三堆，顺便核对命令所属游戏。只解析不写入任何数据")
        self._btn_parse.clicked.connect(self._on_parse_clicked)
        self._btn_file = QPushButton("从文本文件读入…", self)
        self._btn_file.setToolTip(
            "从 txt 文件读入内容填入输入框并自动解析"
            "（网址清单、命令清单都行；记事本存的文件也能读）")
        self._btn_file.clicked.connect(self._on_load_file_clicked)
        self._btn_clear = QPushButton("清空", self)
        self._btn_clear.setToolTip(
            "清空第②步的输入框和解析结果（不影响已入库的批次）")
        self._btn_clear.clicked.connect(self._on_clear_clicked)
        self._btn_browser = QPushButton("从浏览器取标签页…", self)
        self._btn_browser.setToolTip(
            "弹出选择器读取浏览器当前打开的标签页：勾选后自动填进"
            "上面的输入框并解析预览（实时采集会占用鼠标键盘，"
            "选择器里有说明）")
        self._btn_browser.clicked.connect(self._go_browser)

        row2.addWidget(self._btn_parse)
        row2.addWidget(self._btn_file)
        row2.addWidget(self._btn_browser)

        row2.addWidget(self._btn_clear)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ③ 入库卡：入库按钮 + 只读命令文本框 + 复制/核验/开批按钮
        self._d3, box3 = self._make_card("step3", "③ 入库并生成下载命令")
        row_reg = QHBoxLayout()
        self._btn_register = QPushButton("入库并生成命令", self)
        self._btn_register.setToolTip(
            "把预览出的新编号整批登记为「待下载」，并生成对应的 "
            "steamcmd 下载命令。整批一个事务：任何一条失败全部回滚，"
            "不会导入一半")
        self._btn_register.clicked.connect(self._on_register_clicked)
        row_reg.addWidget(self._btn_register)
        row_reg.addStretch(1)
        box3.addLayout(row_reg)
        self._cmd_view = QPlainTextEdit(self)
        self._cmd_view.setReadOnly(True)
        self._cmd_view.setMaximumHeight(120)
        self._cmd_view.setPlaceholderText("（入库成功后，下载命令显示在这里）")
        box3.addWidget(self._cmd_view)
        row3 = QHBoxLayout()
        self._btn_copy = QPushButton("复制命令", self)
        self._btn_copy.setToolTip(
            "把命令文本复制到剪贴板；到程序底部的 steamcmd 终端里登录后"
            "粘贴回车即可开始下载")
        self._btn_copy.clicked.connect(self._on_copy_clicked)
        self._btn_batch = QPushButton("开批下载", self)
        self._btn_batch.setToolTip(
            "把本批编号交给底部控制台 ·「下载批次」逐条下载（与【mod 库】"
            "页【下载选中项】同一条链）：\n需要 steamcmd 已启动并登录，"
            "未启动时会提示；\n批次结束后自动弹确认清单，勾选 = 入账")
        self._btn_batch.clicked.connect(self._on_batch_download_clicked)
        self._btn_verify = QPushButton("核验命令可行性…", self)
        self._btn_verify.setToolTip(
            "把上面生成的命令送进「快速命令查询」联网核实：游戏与 mod "
            "是否对应、条目是否已被下架——确认没问题再复制去 steamcmd"
            "（联网走官方接口；本页勾选与批次不受影响）")
        self._btn_verify.clicked.connect(self._on_verify_clicked)
        row3.addWidget(self._btn_copy)
        row3.addWidget(self._btn_verify)
        row3.addWidget(self._btn_batch)
        row3.addStretch(1)
        box3.addLayout(row3)

        # ④ 盘点卡：说明 + 只读盘点按钮（判决制：无复扫，账本状态
        # 只经确认门写入；这里只看现状）
        self._d4, box4 = self._make_card("step4", "④ 盘点本批状态（只读）")
        note4 = QLabel(
            "点【开批下载】的话：批次结束后自动弹确认清单，勾选 = 入账。"
            "复制命令到终端手动跑的，跑完（并在确认清单里勾选入账）回来"
            "点下方按钮，看这批编号在账本里是已下载还是待下载。本步只读"
            "账本，不写任何状态。", self)
        note4.setWordWrap(True)
        note4.setStyleSheet(f"border:none; font-size: {font_px(12)}px;")
        box4.addWidget(note4)
        row4 = QHBoxLayout()
        self._btn_status = QPushButton("盘点本批状态", self)
        self._btn_status.setToolTip(
            "只读盘点本批编号在账本里的现状：已下载（收尾确认清单里"
            "背书过的）/ 待下载（还没入账）的分布")
        self._btn_status.clicked.connect(self._on_check_status_clicked)
        row4.addWidget(self._btn_status)
        row4.addStretch(1)
        box4.addLayout(row4)

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用 / 跨页入口） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播（主窗口按 set_game 自动分发，本页零接线成本）。
        解析结果与批次都归属档案——切换即清场并明说一声（与【网址批量
        导入】同款提示）；输入框文本刻意保留。"""
        had_preview = self._preview is not None
        had_batch = bool(self._batch_ids)
        self._game = game
        self._reset_flow()
        if (had_preview or had_batch) and game is not None:
            self._set_card(self._d2, "档案已切换：原解析结果与本页批次已"
                           "作废，请重新解析预览。", _C_WARN)
            self._log.info("档案已切换：加入新 mod 的解析结果与批次已作废")

    def receive_external_lines(self, lines: list[str]) -> None:
        """跨页交接入口（浏览器选择器回填用；改善项池——tabCollector
        迁入后恢复"从浏览器取标签页"按钮即可接上）：填入第②步输入框
        并自动解析预览。只填与预览，不替用户入库——第③步按钮仍需亲手
        点（防呆不省，与解析→入库两段式一致）。"""
        """addModPage.py——receive_external_lines 的 docstring 说“改善项池——tabCollector 迁入后恢复‘从浏览器取标签页’按钮即可接上”，但按钮与信号本批已是活的。docstring 首句改为："""
        """跨页交接入口（浏览器选择器回填用）：填入第②步输入框并自动解析预览。…"""
        self._input.setPlainText("\n".join(lines))
        self._log.info(f"收到 {len(lines)} 条外部网址，已自动解析预览")
        self._run_preview()

    # ---------- 流程：清场（初始化与切档案共用） ----------
    def _reset_flow(self) -> None:
        self._preview = None
        self._batch_ids = []
        self._batch_app_id = None
        self._preview_detail.setText("")
        self._cmd_view.clear()
        self._btn_register.setEnabled(False)
        self._btn_register.setText("入库并生成命令")
        self._btn_copy.setEnabled(False)
        self._btn_verify.setEnabled(False)
        self._btn_batch.setEnabled(False)
        self._btn_status.setEnabled(False)
        self._btn_parse.setEnabled(self._game is not None)
        self._btn_file.setEnabled(self._game is not None)
        self._btn_browser.setEnabled(self._game is not None)

        if self._game is None:
            self._set_card(self._d1,
                           "未选择档案——请先在左上角添加或选择游戏档案",
                           _C_WARN)
            self._set_card(self._d2, "等待选择档案。", _C_MUTED)
        else:
            self._set_card(
                self._d1,
                f"当前档案：{self._game.name}（{self._game.app_id}）"
                "——本页的解析、入库、下载、盘点都以这个档案为准；"
                "换游戏请先在左上角切换",
                _C_OK)
            self._set_card(
                self._d2,
                "把网址或命令文本粘贴到下方，点【解析预览】；这一步只"
                "解析与对表，不写入任何数据。",
                _C_MUTED)
            self._set_card(
                self._d3,
                "先在第②步解析预览，确认无误后再入库。下载命令粘贴进 "
                "steamcmd 前，可以点【核验命令可行性】联网验证（游戏与 "
                "mod 是否对应、条目是否被下架）。",
                _C_MUTED)
            self._set_card(
                self._d4,
                "入库后点【开批下载】（批次结束后自动弹确认清单，勾选 "
                "= 入账），或复制命令到终端手动执行后回来点【盘点本批"
                "状态】。",
                _C_MUTED)

    # ---------- 流程：② 解析预览 ----------
    def _on_parse_clicked(self) -> None:
        self._run_preview()

    def _on_clear_clicked(self) -> None:
        """清空第②步（与【网址批量导入】页的清空同款口径）：只清本步
        的输入与解析结果，不碰③④——已入库的批次是既成事实。"""
        self._input.clear()
        self._preview = None
        self._preview_detail.setText("")
        self._btn_register.setEnabled(False)
        self._btn_register.setText("入库并生成命令")
        self._set_card(self._d2,
                       "已清空。粘贴新的网址或命令文本，点【解析预览】。",
                       _C_MUTED)
    def _go_browser(self) -> None:
        """弹出浏览器选择器（对话框内嵌，不跳页）。弹之前先查依赖：
        没装就地弹窗教安装——功能模块要能独立走到头，小白不需要理解
        基础功能页。"""
        missing = tabCollector.missing_packages()
        if missing:
            QMessageBox.information(
                self, "需要先安装采集依赖",
                "实时采集缺 " + "、".join(missing) + "。\n\n"
                                                    "安装命令（复制到 cmd 执行）：\n"
                                                    "pip install pywinauto pyautogui pyperclip psutil\n\n"
                                                    "装完后无需重启本工具，再点一次本按钮即可继续。")
            return
        self.open_browser_picker.emit()

    def _on_load_file_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文本文件", "", "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            # utf-8-sig：自动吞掉 Windows 记事本存盘时加的 BOM 头，
            # 否则第一个编号解析失败进"认不出"（与【网址批量导入】
            # 页同款处理）。读不出的字节以占位符代替而不是让整个
            # 文件报废——认不出的片段会在预览明细里如实列出
            text = Path(path).read_text(encoding="utf-8-sig",
                                        errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, "读取失败", f"无法读取文件：\n{exc}")
            return
        self._input.setPlainText(text)
        self._log.info(f"已读入文本文件：{path}")
        self._run_preview()

    def _run_preview(self, quiet: bool = False
                     ) -> addModFlow.ParsePreview | None:
        """解析输入框当前内容并渲染预览。入库前也会调它（quiet 只跳过
        日志）：引擎约定"入库前必须重新预览"——中间账本可能变化，
        重新解析顺手把刚入库的编号分进跳过堆，天然防重复入库。
        返回 None = 输入为空（说明已在卡片/日志给出）。"""
        if self._game is None:
            return None
        lines = self._input.toPlainText().splitlines()
        if not any(line.strip() for line in lines):
            if not quiet:
                self._log.warn("没有可解析的内容：请先粘贴网址或读入文本文件")
            return None
        preview = addModFlow.preview_input(
            lines, self._repo, app_id=self._game.app_id)
        self._preview = preview
        self._render_preview(preview, quiet)
        self._btn_register.setEnabled(
            preview.has_new and not preview.app_id_mismatch)
        return preview

    def _render_preview(self, p: addModFlow.ParsePreview,
                        quiet: bool = False) -> None:
        assert self._game is not None
        gname = f"{self._game.name}（{self._game.app_id}）"
        # 状态行：错档硬拦最优先——见到就必须拦，绝不默默入库
        if p.app_id_mismatch:
            apps = "、".join(str(a) for a in p.command_app_ids)
            self._set_card(
                self._d2,
                f"⚠ 命令所属游戏（AppID {apps}）与当前档案「{gname}」"
                "不一致——已阻止入库。工坊编号只属于一个游戏；请确认要"
                "加入的是哪个游戏，切换到对应档案后重新解析。",
                _C_FAIL)
            self._log.error(
                f"解析被拦：命令所属游戏（AppID {apps}）与当前档案"
                f"「{gname}」不一致")
        else:
            self._set_card(
                self._d2,
                f"识别 {len(p.mod_ids)} 个编号：新增 {len(p.new_ids)} · "
                f"已在库跳过 {len(p.dup_ids)} · 认不出 {len(p.invalid)}",
                _C_OK if p.has_new else _C_INFO)
            if p.command_app_ids and not quiet:
                apps = "、".join(str(a) for a in p.command_app_ids)
                self._log.info(f"命令所属游戏（AppID {apps}）与当前档案一致")
        # 入库按钮带计数（与【网址批量导入】页"导入（N 个）"同款）
        if p.has_new:
            suffix = (f"（{len(p.new_ids)} 个，已被拦）"
                      if p.app_id_mismatch else f"（{len(p.new_ids)} 个）")
            self._btn_register.setText("入库并生成命令" + suffix)
        else:
            self._btn_register.setText("入库并生成命令")
        # 明细区：三堆各一行（超长截断，认不出的原样展示）
        parts: list[str] = []
        if p.new_ids:
            label = ("新增（已被拦，未入库）："
                     if p.app_id_mismatch else "新增（将入库）：")
            parts.append(label + _ids_text(p.new_ids))
        if p.dup_ids:
            parts.append("已在库（自动跳过）：" + _ids_text(p.dup_ids))
        if p.invalid:
            head = "、".join(p.invalid[:_ID_LIMIT])
            if len(p.invalid) > _ID_LIMIT:
                head += f" …（共 {len(p.invalid)} 段）"
            parts.append("认不出的片段（请自行核对）：" + head)
        self._preview_detail.setText("\n".join(parts))
        if p.has_new and not quiet:
            self._log.info(
                f"解析完成：{len(p.mod_ids)} 个编号，新增 {len(p.new_ids)}"
                f"，已在库跳过 {len(p.dup_ids)}，认不出 {len(p.invalid)}")

    # ---------- 流程：③ 入库 + 命令 + 下载 ----------
    def _on_register_clicked(self) -> None:
        if self._game is None:
            return
        # 入库前重新解析（引擎契约）：文本可能改过、账本可能变了。
        # 返回 None（空输入）或按钮本不该亮的状态都就地止损
        preview = self._run_preview(quiet=True)
        if preview is None or not preview.has_new or preview.app_id_mismatch:
            return
        app_id = self._game.app_id
        ids = list(preview.new_ids)
        try:
            n = addModFlow.register_mods(self._repo, app_id, ids)
        except sqlite3.IntegrityError:
            # 预览之后别的页面恰好导入了同一批编号：整批回滚（引擎保证
            # 不会导一半）。重新解析后这批会进"已在库跳过"堆，再入库
            # 就只剩真正没入过的
            QMessageBox.warning(
                self, "入库失败",
                "部分编号刚被其他页面导入过，本次已整批回滚，未写入任何"
                "数据。\n请重新点【解析预览】后再入库。")
            self._log.error("入库失败：编号与其他页面撞车，已整批回滚")
            return
        text = addModFlow.build_commands_text(app_id, ids)
        self._batch_ids = ids
        self._batch_app_id = app_id
        self._cmd_view.setPlainText(text)
        self._btn_copy.setEnabled(bool(text))
        self._btn_verify.setEnabled(bool(text))
        self._btn_batch.setEnabled(bool(ids))
        self._btn_status.setEnabled(True)
        self._set_card(
            self._d3,
            f"已入库 {n} 条（状态「{status_zh('tracked')}」），下载命令已"
            f"生成（{len(ids)} 行）。点【开批下载】交给控制台执行，或"
            "【复制命令】到终端手动粘贴。",
            _C_OK)
        self._log.ok(f"已入库 {n} 个新 mod，下载命令已生成（{len(ids)} 行）")

    def _on_verify_clicked(self) -> None:
        """【核验命令可行性…】：跳到独立成页的【快速命令查询】并预填
        第③步生成的命令原文，查询由用户亲手点——命令行里旧 AppID 以
        重查结果为准，游戏与 mod 对不对得上、条目是否被下架一眼看全。
        文本只填不跑，防呆不省。"""
        text = self._cmd_view.toPlainText().strip()
        self.quick_command_requested.emit(text)

    def _on_copy_clicked(self) -> None:
        text = self._cmd_view.toPlainText()
        if not text.strip():
            return
        QApplication.clipboard().setText(text)
        self._log.info(f"已复制 {text.count(chr(10)) + 1} 行下载命令到剪贴板："
                       "去终端粘贴执行")

    def _on_batch_download_clicked(self) -> None:
        """【开批下载】：把本批编号交给批量下载控制器（与【mod 库】页
        【下载选中项】同一条链、同一份信号契约）。本页不认识控制器——
        steamcmd 没启动等拒绝情形由它写日志并弹控制台指路。批次收尾
        自动弹「确认清单」，勾选 = 本地版本入账。
        新入库的 mod 没有旧内容，无需备份（备份联动只进日常更新的
        "备份+更新"动作）。
        卡片文案刻意中性（"已请求"而非"已开始"）：请求可能被拒，
        原因以日志为准，卡片不说满话。"""
        if not self._batch_ids or self._batch_app_id is None:
            return  # 按钮本该已禁用；双保险
        self._set_card(
            self._d3,
            f"已请求批量下载（{len(self._batch_ids)} 个）——进度与结果"
            "见底部控制台 ·「下载批次」；批次结束后自动弹确认清单，"
            "勾选 = 本地版本入账。再回本页点【盘点本批状态】可看现状。",
            _C_INFO)
        self._log.info(
            f"请求批量下载（加入新 mod 批次，{len(self._batch_ids)} 个"
            "编号）……")
        self.download_requested.emit(self._batch_app_id,
                                     list(self._batch_ids))

    # ---------- 流程：④ 盘点本批状态（只读） ----------
    def _on_check_status_clicked(self) -> None:
        """【盘点本批状态】：用引擎 bucket_statuses 只读盘点本批编号
        在账本里的现状——已下载（收尾确认清单里背书过的）/ 待下载
        （还没入账）分布。只读，不写任何状态；盘面文件事实归入账中心
        的盘点认领，本页不越界。批次归属 = 入库那一刻的档案，中途切了
        档案批次已作废，按钮也早被清场置灰。"""
        if not self._batch_ids or self._batch_app_id is None:
            return  # 按钮本该已禁用；双保险
        b = addModFlow.bucket_statuses(
            self._repo, self._batch_app_id, self._batch_ids)
        total = len(self._batch_ids)
        done_n = len(b.downloaded)
        wait_n = len(b.waiting)
        odd = b.failed + b.unexpected
        if done_n == total:
            self._set_card(
                self._d4,
                f"{done_n}/{total} 个已是「{status_zh('downloaded')}」"
                "——本批全部入账，闭环完成。",
                _C_OK)
            self._log.ok(f"加入新 mod 批次盘点：{total}/{total} 已下载")
            return
        seg = [f"已下载 {done_n}/{total}"]
        color = _C_WARN
        if wait_n:
            seg.append(f"{wait_n} 个仍「{status_zh('tracked')}」"
                       "（命令可能没跑完，或跑完还没在收尾确认清单里"
                       "勾选入账）")
        if odd:
            seg.append(f"{len(odd)} 个状态异常（编号 {_ids_text(odd)}；"
                       "正常流程到不了这个状态，到【mod 库】页核对现状）")
            color = _C_FAIL
        self._set_card(self._d4, "；".join(seg) + "。", color)
        self._log.info(
            f"加入新 mod 批次盘点：已下载 {done_n} · 待下载 {wait_n}"
            + (f" · 异常 {len(odd)}" if odd else ""))

    # ---------- 卡片基建（样式与批量下载步骤卡片同款） ----------
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


def _ids_text(ids: list[int], limit: int = _ID_LIMIT) -> str:
    """编号清单 → 展示文本：最多列 limit 个，超出折成"…"。"""
    head = "、".join(str(i) for i in ids[:limit])
    if len(ids) > limit:
        head += f" …（共 {len(ids)} 个）"
    return head
