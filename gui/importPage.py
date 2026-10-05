"""网址批量导入页
"""
r"""gui/importPage.py · 基础功能页（V2 消费版）。

工坊网址（或纯数字编号）粘贴 / 从文本文件读入 → 解析预览 → 批量
登记为「待下载」，归属当前选中的游戏档案。只写编号与链接，标题等
信息由更新检测补全。

【本页是干什么的】
账本的"登记处"：把在别处看到的 mod（工坊网址、编号、steamcmd 下载
命令行）批量登记进当前档案。只负责"让工具认识这个 mod"——不下载、
不确认。登记后的完整链路（V2 口径）：【下载命令生成】勾选 → 复制
命令去 steamcmd 下载 → 下完到【入账中心】点【扫描游戏目录】——
盘点把新下载的目录摆成待认领，确认后转「已下载」（版本号按盘面
记载一并入账）。这条链路也写在页面的"先读我"折叠卡里。

【防呆核心（两条，缺一不可）】
1) 解析结果只属于"解析那一刻选中的游戏"。之后无论切到别的档案、
   还是切到空态再新建档案，旧的解析结果一律作废——否则会把 A 游戏
   的 mod 记到 B 游戏名下，错档数据混进库后很难事后清理。
   （三情形演进史见 set_game 内注释：a→b/c 的缝是后来堵上的。）
2) 下载命令行自带归属（workshop_download_item <appid> <编号>）。
   解析器（core/urlParser.parse_lines）把命令行里的 AppID 单独带回
   report.command_app_ids——本页拿它与当前档案比对，发现别的游戏的
   命令行就整批拦截。为什么整批不精确到行：解析器把命令里的编号挖
   出后混进总清单，无法区分哪个编号来自哪条命令，赌不如拦。
   纯网址/纯数字本身不带归属信息，不拦。

【合同不变】
- imported(int)：成功导入后发射，主窗口借此跳转 mod 库页并刷新；
- set_game(game)：None 同样受理，三情形作废逻辑原样保留；
- 导入 = 整批一个事务（repo.transaction），中途失败全部回滚；
- 读入 txt 仍 utf-8-sig 吞 BOM；本页不写 operations_log（登记类
  动作无"命令"可记）。

【V2 相对 V1 的改动】
- import 路径：LogBus 独立成 gui/logBus；网址模板按 D37 一级单源
  走 core/constants（parse_lines 仍住 core/urlParser）；
- 状态词按 D30：tracked 的显示词是「待下载」（V1 的「已收录」按新
  口径改，库内值仍是 "tracked"，零数据迁移）；
- 收尾链路文案：扫描入口从"mod 库页【扫描本地】"改指入账中心
  【扫描游戏目录】（mod 库页扫描按钮在 V2 已摘除）。
"""
import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.constants import WORKSHOP_URL_TEMPLATE   # D37 一级单源
from core.models import Game, Mod
from core.urlParser import parse_lines
from gui.collapsibleSection import CollapsibleSection
from gui.logBus import LogBus   # V2：LogBus 独立成文件
from gui.theme import font_px  # 字号单源（D25）

# "先读我"卡的内容（六段）：与页面提示、文件头分工——提示一行导览，
# 卡里讲全。文案原则：只说用户看得见的东西，不写内部术语。
_GUIDE_PARAS = (
    "这页是什么：账本的登记处——把在别处看到的 mod（工坊网址或编号）"
    "批量登记进当前档案，登记后状态 =「待下载」（与 mod 库页状态列"
    "同词）。只负责“让工具认识”，不下载、不确认。",

    "吃什么输入：工坊网址（每行一条）／纯数字编号／整段空格分隔的"
    "混合文本／txt 文件（自动兼容记事本的 BOM 头）；也认 steamcmd "
    "下载命令行（workshop_download_item 开头的行）。",

    "登记后走哪：【下载命令生成】页勾选 → 点【复制命令】粘进 "
    "steamcmd 执行 → 下完到【入账中心】点【扫描游戏目录】：盘点会把"
    "新下载的目录摆成待认领，确认后转「已下载」（版本号按盘面记载"
    "一并入账）。",

    "标题去哪补：登记只记编号和链接（所以快）；标题、作者等要联网"
    "才有——导入完点【更新检测】自动补全。",

    "与「加入新 mod」（功能模块 → 加入新 mod）的分工：同一套底层"
    "（同一个解析器、同一本账），那边是一个个加的引导壳，本页是"
    "批量粘贴的入口——两条路进来的 mod 长得一模一样。",

    "防呆规则：解析结果只属于“解析那一刻选中的档案”，切了档案旧"
    "结果就作废；粘了别的游戏的下载命令会被整批拦下（防错档），"
    "请切到对应档案再粘贴，或删掉那些命令行。",
)


class ImportPage(QWidget):
    """imported(int)：成功导入后发射，主窗口借此跳转 mod 库页并刷新。"""

    imported = Signal(int)

    def __init__(self, repo, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._log = log or LogBus()   # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._new_ids: list[int] = []   # 本次解析出的、还没导入的新编号
        self._build_ui()

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
        root.setSpacing(6)

        title = QLabel("网址批量导入", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        # 页首一句话导览（详细说明在下面"先读我"卡里，不重复占屏）
        tip = QLabel(
            "把工坊网址/编号粘贴到下面（每行一个，空格分隔亦可），"
            "解析预览后批量登记为「待下载」。标题等详细信息导入后到"
            "【更新检测】自动补全——详细说明点下方【先读我】。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        # ---- "先读我"说明卡（可折叠，默认收起：输入框是本页主角）----
        root.addWidget(self._make_guide())

        self._input = QPlainTextEdit(self)
        self._input.setPlaceholderText(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213\n"
            "2216850785")
        root.addWidget(self._input, 1)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        load_btn = QPushButton("从文本文件读入…", btn_row)
        load_btn.setToolTip(
            "从 txt 文件读入网址/编号清单（自动兼容记事本的 BOM 头），"
            "追加到输入框并立即解析预览")
        load_btn.clicked.connect(self._on_load_file)
        clear_btn = QPushButton("清空", btn_row)
        clear_btn.setToolTip(
            "清空输入框与解析结果（还没导入的解析结果一并作废）")
        clear_btn.clicked.connect(self._on_clear)
        h.addWidget(load_btn)
        h.addWidget(clear_btn)
        h.addStretch(1)
        parse_btn = QPushButton("解析预览", btn_row)
        parse_btn.setToolTip(
            "解析输入框里的工坊网址/纯数字编号/下载命令行，与库内对表："
            "报出可导入多少、已在库跳过多少。解析结果只属于当前选中的"
            "档案，切档案即作废")
        parse_btn.clicked.connect(self._on_parse)
        h.addWidget(parse_btn)
        root.addWidget(btn_row)

        self._report = QLabel("", self)
        self._report.setWordWrap(True)
        root.addWidget(self._report)

        self._import_btn = QPushButton("导入（0 个）", self)
        self._import_btn.setToolTip(
            "把预览出的新编号整批登记为「待下载」；一个事务，失败整体"
            "回滚；已在库的编号自动跳过")
        self._import_btn.setEnabled(False)
        self._import_btn.clicked.connect(self._on_import)
        root.addWidget(self._import_btn)

    def _make_guide(self) -> CollapsibleSection:
        """建"先读我"折叠卡。共享件契约：set_content(控件, 高度) 每分区
        只调一次、高度固定、内容多时控件内部滚动——所以内容包进
        QScrollArea（无边框），六段说明再长也只是卡内滚动，页面高度
        不受影响。不连 expand_changed：本页不需要收展记忆，没人监听
        的信号是空操作。"""
        body = QWidget(self)
        bv = QVBoxLayout(body)
        bv.setContentsMargins(4, 4, 4, 4)
        bv.setSpacing(6)
        for text in _GUIDE_PARAS:
            lbl = QLabel(text, body)
            lbl.setWordWrap(True)   # 可能变长的标签开换行
            lbl.setStyleSheet(f"color: #8a8a8f; font-size: {font_px(12)}px;")
            bv.addWidget(lbl)
        bv.addStretch(1)
        wrap = QScrollArea(self)
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        wrap.setWidget(body)
        sec = CollapsibleSection(
            "先读我：本页怎么用 · 输入格式 · 登记后走哪", self)
        sec.set_content(wrap, 260)
        sec.set_expanded(False)   # 默认收起
        return sec

    # ---------- 对外（MainWindow 调用）----------
    def set_game(self, game: Game | None) -> None:
        # 第一步：防呆检查——游戏身份变了，还没导入的解析结果必须作废。
        #
        # "游戏身份变了"包括三种情况，缺一不可：
        #   a) 档案 A → 档案 B（AppID 不同）
        #   b) 档案 A → 空态（game 为 None）
        #   c) 空态 → 档案 B
        # 旧版本只处理了 a)：条件里要求"两头都不是 None"才去比对，
        # 结果"A 解析 → 切空态 → 新建 B → 点导入"这条链路
        # 会把 A 的解析结果导进 B 名下。b)、c) 现在一并堵上。
        game_changed = (
                (self._game is None) != (game is None)   # b/c：进出空态都算变
                or (self._game is not None and game is not None
                    and self._game.app_id != game.app_id)   # a：档案之间切换
        )
        if self._new_ids and game_changed:
            self._report.setText("游戏已切换，请重新解析预览后再导入。")
            self._report.setStyleSheet("color: #f76b15;")
            self._new_ids = []          # 旧结果直接扔掉
            self._import_btn.setEnabled(False)   # 导入按钮跟着熄灭
            self._import_btn.setText("导入（0 个）")

        # 第二步：正常更新自身状态和界面提示。
        # 同一个档案被重复 set_game 不算"变了"，导入照常有效，属预期。
        self._game = game
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
        else:
            self._game_label.setText(f"当前游戏：{game.name}（{game.app_id}）")

    # ---------- 槽 ----------
    def _on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择网址文本文件", "", "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            # utf-8-sig：自动吞掉 Windows 记事本存盘时加的 BOM 头，
            # 避免第一个编号解析失败
            text = Path(path).read_text(encoding="utf-8-sig",
                                        errors="replace")
        except OSError as exc:
            self._log.warn(f"读取文件失败：{path}")
            QMessageBox.warning(self, "读取失败", f"无法读取文件：{exc}")
            return
        current = self._input.toPlainText().rstrip()
        self._input.setPlainText(
            f"{current}\n{text.strip()}" if current else text.strip())
        self._on_parse()   # 读入后立即预览

    def _on_clear(self) -> None:
        self._input.clear()
        self._report.clear()
        self._new_ids = []
        self._import_btn.setEnabled(False)
        self._import_btn.setText("导入（0 个）")

    def _on_parse(self) -> None:
        # 每次解析都是重新开始：先清掉上一轮的结果和按钮状态
        self._new_ids = []
        self._import_btn.setEnabled(False)
        self._import_btn.setText("导入（0 个）")
        if self._game is None:
            self._report.setText("请先在左上角选择游戏档案，再解析导入。")
            self._report.setStyleSheet("color: #e5484d;")
            return
        report = parse_lines(self._input.toPlainText().splitlines())

        # ---- 红灯：别的游戏的下载命令行（防呆补齐）----
        # 命令行自带归属，解析器已把 AppID 单独带回 command_app_ids
        # ——这里与当前档案比对，发现异档命令行就整批拦截。
        # 为什么整批不精确到行：解析器把命令里的编号挖出后混进总清单
        # （report.mod_ids），无法区分哪个编号来自哪条命令——赌不如拦。
        # 纯网址/纯数字不带归属信息，不受此拦截。
        bad = sorted({a for a in report.command_app_ids
                      if a != self._game.app_id})
        if bad:
            ids_txt = "、".join(str(a) for a in bad)
            self._report.setText(
                f"发现别的游戏的下载命令（AppID：{ids_txt}）——"
                "为防错档，本批全部拦截、未登记任何内容。\n"
                f"请切到对应档案（左上角）再粘贴，"
                "或删掉那些命令行后重新解析。")
            self._report.setStyleSheet("color: #e5484d;")
            self._log.warn(
                f"解析拦截：发现异档下载命令（AppID {ids_txt}，"
                f"当前档案 {self._game.app_id}）——本批未登记")
            return

        if not report.mod_ids and not report.invalid:
            self._report.setText(
                "（没有可解析的内容——粘贴工坊网址或纯数字编号）")
            self._report.setStyleSheet("color: gray;")
            return

        # 空列表不查询（防 IN () 语法问题）
        existing = (set(self._repo.filter_existing_ids(report.mod_ids))
                    if report.mod_ids else set())
        self._new_ids = [i for i in report.mod_ids if i not in existing]
        dup = len(report.mod_ids) - len(self._new_ids)
        lines = [f"识别 {len(report.mod_ids)} 个编号："
                 f"可导入 {len(self._new_ids)} 个，"
                 f"已在库中（跳过）{dup} 个。"]
        if report.invalid:
            shown = "；".join(report.invalid[:5])
            lines.append(f"无法识别 {len(report.invalid)} 片段：{shown}"
                         + ("…" if len(report.invalid) > 5 else ""))
        self._log.info(
            f"解析预览：识别 {len(report.mod_ids)} 个，"
            f"可导入 {len(self._new_ids)} 个，跳过 {dup} 个"
            + (f"，无法识别 {len(report.invalid)} 个"
               if report.invalid else ""))
        self._report.setText("\n".join(lines))
        self._report.setStyleSheet(
            "color: #46a758;" if self._new_ids else "color: gray;")
        if self._new_ids:
            self._import_btn.setEnabled(True)
            self._import_btn.setText(f"导入（{len(self._new_ids)} 个）")

    def _on_import(self) -> None:
        game = self._game
        if game is None or not self._new_ids:
            return
        now = int(time.time())
        try:
            with self._repo.transaction():
                # 整批一个事务：中途任何一条失败，全部回滚，
                # 不会出现"导了一半"的中间状态
                for mid in self._new_ids:
                    self._repo.add_mod(Mod(
                        mod_id=mid,
                        game_id=game.app_id,
                        url=WORKSHOP_URL_TEMPLATE.format(mid),
                        status="tracked",
                        first_tracked_at=now,
                    ))
        except sqlite3.IntegrityError as exc:
            self._log.error(f"导入失败（数据库冲突）：{exc}")
            QMessageBox.warning(
                self, "导入失败",
                f"写入数据库时冲突：{exc}\n请重新解析后再试。")
            return
        count = len(self._new_ids)
        self._log.ok(f"已为「{game.name}」导入 {count} 个 mod")
        QMessageBox.information(
            self, "导入完成",
            f"已为「{game.name}」登记 {count} 个 mod（状态：待下载）。\n"
            "点【更新检测】即可补全标题等信息。")
        self._on_clear()
        self.imported.emit(count)
