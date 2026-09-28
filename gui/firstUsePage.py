"""首次使用向导
"""
"""gui/firstUsePage.py · 四步引导页：定位 steamcmd → 建档 → 接通读取目录
→ 纳入客户端已有的 mod。四张步骤卡可跳步、可重入（拍板③）：老手缺哪步
走哪步，已完成的步骤亮绿状态，重复进入不产生副作用。

页面只做串联，不抄第二份实现（与「加入新 mod」同一单源纪律）：
- ①②③ 全是既有能力的入口：设置页（发 settings_requested 信号）、
  建档对话框与连接指引（转调左上角档案切换器里的现成对话框——
  发 add_game_requested / link_guide_requested，主窗口接线转调，
  建档后的刷新广播、连接检测通过后的写档全走既有链，本页零重复）；
- ④ 是本页唯一的新功能，逻辑住在 workflows/intakeFlow（零 Qt，
  pytest 已覆盖）：读 Steam 客户端库的 appworkshop_<appid>.acf，
  解析订阅条目 → 与账本对表 → 只补缺不覆盖入库。

为什么纳入的是"客户端订阅"：向导服务的场景是"已经在玩、在 Steam
里订阅了 mod"的用户——他们的 mod 内容躺在客户端自己的库目录里，
steamcmd 和本工具对此一无所知。纳入后这批 mod 以「已收录」进账
（带客户端下载时的版本线索），之后用 steamcmd 补下载、扫描确认，
管理权就完整交接到本工具（退订指引见第④步文案）。

一个边界要说透：客户端账本只统计"已安装"的条目；只订阅了、客户端
还没下载完的内容不会出现在这里——本来也没有文件，无从纳入。
"""
import sqlite3
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QComboBox
)

from core.commandBuilder import build_copy_text
from core.localScanner import locate_acf, scan_acf
from core.models import Game
from core.steamPaths import client_library_roots, workshop_content_dir

from gui.consolePanel import LogBus
from workflows.intakeFlow import ClientIntakePlan, apply_intake, classify_client_items

# 与加入新 mod 页、日常更新页一致的配色，按用途命名
_C_OK = "#46a758"      # 完成 / 就绪
_C_FAIL = "#e5484d"    # 错误 / 硬拦
_C_WARN = "#f5a623"    # 需要注意（未配置、未确认）
_C_INFO = "#d4d4d4"    # 进行中
_C_MUTED = "#8a8a8f"   # 说明文字 / 等待
_ID_LIMIT = 20         # 编号最多原样列出多少个，超出折成"…"

# —— 安装引导卡文案（决策 65）：内容待填，改文案只动下面四个常量，
# 布局与接线不用碰。顺序=先 steamcmd（本工具一切功能的地基），后
# Watt Toolkit（原 Steam++；浏览器访问工坊页的前置，网址批量导入、
# 从浏览器取网址都吃它的加速）。本页不检测装没装——第三方软件
# 检测不了，也不该装作能检测；两张卡是纯说明，永远显示
_GUIDE_STEAMCMD_TITLE = "准备 · 安装 steamcmd"
_GUIDE_STEAMCMD = (
    "安装 steamcmd（Steam 命令行工具）是本工具的前置条件："
    "官网：https://developer.valvesoftware.com/wiki/SteamCMD#Downloading_SteamCMD\n"
    "下载后解压得到steamcmd.exe，先别急着点进去，首次运行会自动安装到当前目录下，应当把它移到一个固定目录（如 D:\\steamcmd）再运行，\n"
    "请慎重选择目录，事关本软件的 mod 下载与备份和文件夹连接等功能，用一段时间后改变非常麻烦；不能装在带中文的路径，会闪退。\n"
    "调整好位置后运行 steamcmd.exe 让它安装完成，务必关闭watt Toolkit等加速器，否则无法安装，提示无法下载；"
    "装好后建议登录一次（steamcmd +login <用户名>），第一次登录会要求输入密码和验证码，就有了本地缓存（steamcmd自身行为，与此软件无关），之后登录只用输入登录命令即可；"
    "额外提示：使用steamcmd下载mod需要登录的账号拥有对应游戏，否则报错（目前已知 rimworld 除外）。千万不能输入中文，否则会一直输出“？？？？？？”只能关闭软件\n"
    "装好后回下方第①步填路径。"
)
_GUIDE_WATT_TITLE = "推荐 · Watt Toolkit（原 Steam++）"
_GUIDE_WATT = (
    "Watt Toolkit 是一个网络加速工具，可以加速Steam、github等相关平台。"
    "官网：https://steampp.net/ \n"
    "安装后启动点击网络加速，勾选并启动 Steam 相关加速，这样浏览器才能正常打开工坊页和提高访问相关api（比如日常更新模块的获取更新信息）成功率。"
    "网络加速页，头像下面的“网络加速”的傍边有个“脚本配置”，点进去后点“脚本工坊”，登录后，推荐安装“steam创意工坊大图修复”\n"
    "注意：Watt Toolkit 不是本工具的前置条件，没装也能用，但装了就能访问创意工坊和提高访问相关api的成功率。"
)

class FirstUsePage(QWidget):
    # 三个转调信号：主窗口接线——设置页跳转 / 建档对话框 / 连接指引，
    # 实体都在 MainWindow 与左上角档案切换器手里，本页不持有它们
    settings_requested = Signal()
    add_game_requested = Signal()
    link_guide_requested = Signal()

    def __init__(self, repo, settings=None, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气 = 无操作）
        self._game: Game | None = None
        self._plan: ClientIntakePlan | None = None  # 第④步预览结果
        self._build_ui()
        self._refresh_all()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        title = QLabel("首次使用", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"的新规）
        note = QFrame(self)
        note.setObjectName("fu_note")
        note.setStyleSheet("QFrame#fu_note { border: 1px solid #3a3a3a;"
                           " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
                "流程：① 定位 steamcmd → ② 建立游戏档案 → ③ 接通游戏读取目录"
                " → ④ 纳入客户端已有的 mod。四步可跳步、可重入，缺哪步走哪步。",
                "为什么这么做：纳入走「先预览再写入」，只补缺不覆盖、已删除/"
                "已失效的不复活；纳入后是「已收录」，要等 steamcmd 下载并扫描"
                "确认才算「已下载」——客户端那份拷贝不算本地（账实口径以 "
                "steamcmd 目录为准）。",
                "术语与关系：客户端订阅记录 = 客户端库里的 appworkshop_<appid>"
                ".acf，与 steamcmd 的同名文件是两棵树；纳入后的下载与确认走"
                "「加入新 mod」同一条链（命令同源、扫描同源）。",
                "多前端共存：与这台 steamcmd 共用的其他前端下载的 mod，"
                "扫描时会被自动纳入账本（已收录/已下载如实对齐），不需要"
                "重复登记；本工具永远只读 steamcmd 的账实文件。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet("border:none; color:#8a8a8f; font-size:12px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)
        # —— 安装引导区（决策 65）：两张纯说明卡，置于四步之前——
        # 先有 steamcmd 才谈得上"定位"；Watt Toolkit 是浏览器访问
        # 工坊页的推荐前置。静态卡：不参与 _refresh_all，内容即常量
        _d_prep1 = self._make_card("prep_steamcmd", _GUIDE_STEAMCMD_TITLE)[0]
        _d_prep1.setText(_GUIDE_STEAMCMD)
        _d_prep2 = self._make_card("prep_watt", _GUIDE_WATT_TITLE)[0]
        _d_prep2.setText(_GUIDE_WATT)


        # ① steamcmd 卡：状态 + 去设置页 + 重新检查
        self._d1, box1 = self._make_card("step1", "① 定位 steamcmd")
        row1 = QHBoxLayout()
        self._btn_settings = QPushButton("去设置页填写")

        self._btn_settings.setToolTip("跳到「全局设置」填 steamcmd 程序路径；"
                                      "填完回来点【重新检查】")
        self._btn_settings.clicked.connect(self.settings_requested.emit)
        self._btn_recheck = QPushButton("重新检查")

        self._btn_recheck.setToolTip("重读设置刷新四张卡片的状态"
                                     "（从设置页回来后点它）")
        self._btn_recheck.clicked.connect(self._on_recheck)
        row1.addWidget(self._btn_settings)
        row1.addWidget(self._btn_recheck)
        row1.addStretch(1)
        box1.addLayout(row1)

        # ② 建档卡：状态 + 转调建档对话框
        self._d2, box2 = self._make_card("step2", "② 建立游戏档案")
        row2 = QHBoxLayout()
        self._btn_addgame = QPushButton("添加游戏档案…")

        self._btn_addgame.setToolTip("打开建档对话框（与左上角「＋ 添加游戏"
                                     "档案」同一份）：查重、自动查名、下载"
                                     "目录推导预览")
        self._btn_addgame.clicked.connect(self.add_game_requested.emit)
        row2.addWidget(self._btn_addgame)
        row2.addStretch(1)
        box2.addLayout(row2)

        # ③ 连接卡：状态 + 转调连接指引
        self._d3, box3 = self._make_card("step3", "③ 接通游戏读取目录")
        row3 = QHBoxLayout()
        self._btn_link = QPushButton("打开连接指引…")

        self._btn_link.setToolTip("打开连接指引对话框（与「游戏」菜单里同一"
                                  "份）：把游戏读取 mod 的目录和下载目录接"
                                  "通，检测通过会自动记录")
        self._btn_link.clicked.connect(self.link_guide_requested.emit)
        row3.addWidget(self._btn_link)
        row3.addStretch(1)
        box3.addLayout(row3)

        # ④ 纳入卡：本页唯一的新功能
        self._d4, box4 = self._make_card("step4", "④ 纳入客户端已有的 mod")
        self._intake_detail = QLabel("", self)
        self._intake_detail.setWordWrap(True)
        self._intake_detail.setStyleSheet("border:none; font-size:12px;")
        box4.addWidget(self._intake_detail)
        row_dir = QHBoxLayout()
        # ④ 目录框 = 可编辑下拉（T19 自动探测）：注册表 + libraryfolders.vdf
        # 探测到的客户端库预填成候选；设置里存过的排最前；都失败就空着
        # 手填——探测是预填便利，手填能力永不删
        self._dir_edit = QComboBox(self)
        self._dir_edit.setEditable(True)
        self._dir_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        seen: set[str] = set()
        prefill = ""
        if self._settings is not None:
            prefill = str(self._settings.get("steam_client_library") or "").strip()
        for cand in ([prefill] if prefill else []) + client_library_roots():
            if cand.casefold() not in seen:
                seen.add(cand.casefold())
                self._dir_edit.addItem(cand)
        self._dir_edit.lineEdit().setPlaceholderText(
            "Steam 客户端库目录，如 D:\\SteamLibrary——"
            "选库根、steamapps 或 workshop 层都行（可手动输入）")

        row_dir.addWidget(self._dir_edit, 1)
        self._btn_browse = QPushButton("浏览…", self)
        self._btn_browse.setToolTip("打开系统目录选择窗口，选 Steam 客户端库目录")
        self._btn_browse.clicked.connect(self._on_browse)

        self._btn_browse.clicked.connect(self._on_browse)
        row_dir.addWidget(self._btn_browse)
        box4.addLayout(row_dir)
        row4 = QHBoxLayout()
        self._btn_preview = QPushButton("解析预览", self)
        self._btn_preview.setToolTip("读客户端库里的 appworkshop_<appid>.acf，"
                                     "与当前档案账本对表；只读不写")
        self._btn_preview.clicked.connect(self._on_preview)
        self._btn_intake = QPushButton("纳入账本", self)
        self._btn_intake.setToolTip("把预览出的「待纳入」「待补版本线索」"
                                    "两类条目写进账本；只补缺不覆盖，写前有确认弹窗")
        self._btn_intake.setEnabled(False)
        self._btn_intake.clicked.connect(self._on_intake)
        self._btn_copy = QPushButton("复制下载命令", self)
        self._btn_copy.setToolTip("把纳入条目对应的 steamcmd 下载命令复制到"
                                  "剪贴板（与「下载命令生成」同源）")
        self._btn_copy.setEnabled(False)
        self._btn_copy.clicked.connect(self._on_copy)
        row4.addWidget(self._btn_preview)
        row4.addWidget(self._btn_intake)
        row4.addWidget(self._btn_copy)
        row4.addStretch(1)
        box4.addLayout(row4)

        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        """档案切换广播（主窗口按 set_game 自动分发）。
        预览结果归属档案——切换即作废并明说一声；客户端库目录的文本
        刻意保留（目录本身不随档案变，只是 acf 按档案区分）。"""
        had_plan = self._plan is not None
        self._game = game
        self._plan = None
        self._refresh_all()
        if had_plan and game is not None:
            self._log.info("档案已切换：纳入已有 mod 的预览已作废，请重新解析")

    def refresh(self) -> None:
        """公开的刷新入口（主窗口可在切到本页时调用，也可不接——
        卡片①上有【重新检查】按钮兜底）。空目录框才回填设置值，
        不覆盖用户正在编辑的文本。"""
        if self._settings is not None and not self._dir_edit.currentText().strip():
            self._dir_edit.setCurrentText(str(self._settings.get("steam_client_library") or "").strip())
        self._refresh_all()

    # ---------- 卡片刷新 ----------
    def _on_recheck(self) -> None:
        self._refresh_all()
        self._log.info("已重新检查首次使用向导各步状态")

    def _refresh_all(self) -> None:
        self._refresh_step1()
        self._refresh_step2()
        self._refresh_step3()
        self._refresh_step4()

    def _refresh_step1(self) -> None:
        exe = self._settings.get("steamcmd_path") if self._settings else ""
        if not exe:
            self._set_card(self._d1,
                           "未配置 steamcmd 程序路径——本工具的下载、扫描、"
                           "备份都靠它定位。", _C_WARN)
            return
        if not Path(exe).is_file():
            self._set_card(self._d1,
                           f"配置的 steamcmd 路径暂不存在：{exe}"
                           "（可先继续后面的步骤，用到前修正即可）", _C_WARN)
            return
        text = f"已定位：{exe}"
        if self._game is not None:
            d = workshop_content_dir(exe, self._game.app_id)
            if d:
                text += f"\n当前档案的下载目录：{d}"
        self._set_card(self._d1, text, _C_OK)

    def _refresh_step2(self) -> None:
        if self._game is None:
            self._set_card(self._d2,
                           "还没有游戏档案——需要游戏的 AppID"
                           "（商店页面网址里那串数字）。", _C_WARN)
        else:
            self._set_card(self._d2,
                           f"当前档案：{self._game.name}"
                           f"（{self._game.app_id}）——已就绪。", _C_OK)

    def _refresh_step3(self) -> None:
        if self._game is None:
            self._set_card(self._d3, "等待建档。", _C_MUTED)
        elif self._game.game_mod_dir:
            self._set_card(self._d3,
                           f"已记录游戏读取目录：{self._game.game_mod_dir}"
                           "（随时可重新检查）。", _C_OK)
        else:
            self._set_card(self._d3,
                           "尚未确认连接——部分游戏只从自己的目录读 mod，"
                           "需要把游戏读取目录和下载目录接通。"
                           "不确定就打开指引检测一次，不影响跳过。", _C_WARN)

    def _refresh_step4(self) -> None:
        has_game = self._game is not None
        self._btn_browse.setEnabled(has_game)
        self._btn_preview.setEnabled(has_game)
        if not has_game:
            self._btn_intake.setEnabled(False)
            self._btn_copy.setEnabled(False)
            self._set_card(self._d4, "等待建档。", _C_MUTED)
            return
        if self._plan is None:
            self._set_card(self._d4,
                           f"把 Steam 客户端库目录填在下面（{self._game.name}"
                           " 的订阅记录），点【解析预览】核对，再【纳入账本】。\n如果你没有通过steam安装mod，什么都没扫描到也正常\n"
                           "待确认的其他提醒：steamcmd和steam管理的mod从此分道扬镳，steamcmd和steam不能同时启动......",
                           _C_MUTED)

    # ---------- 第④步：预览与纳入 ----------
    def _on_browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择 Steam 客户端库目录", self._dir_edit.currentText().strip() or "")
        if path:
            self._dir_edit.setCurrentText(str(path))
            self._on_preview()  # 选完自动预览，少点一次

    def _on_preview(self) -> None:
        if self._game is None:
            return
        raw = self._dir_edit.currentText().strip()
        if not raw:
            QMessageBox.information(self, "解析预览",
                                    "先选择 Steam 客户端库目录。")
            return
        # locate_acf 认三种填写层（库根 / steamapps / workshop），
        # 与 steamcmd 扫描同一份定位逻辑——单源复用，不重写
        acf = locate_acf(raw, self._game.app_id)
        if acf is None:
            self._set_card(self._d4,
                           f"在所选目录下没找到 appworkshop_{self._game.app_id}"
                           ".acf——确认选的是客户端的库（含 steamapps/workshop"
                           " 层级），且这个游戏在该库里装过工坊内容。", _C_FAIL)
            self._log.warn(f"未找到客户端账本：{raw}"
                           f"（appworkshop_{self._game.app_id}.acf）")
            return
        try:
            result = scan_acf(acf)
        except ValueError as exc:  # 文件级问题显式转述，绝不静默
            self._set_card(self._d4, f"客户端账本读取失败：{exc}", _C_FAIL)
            self._log.error(f"客户端账本读取失败：{exc}")
            return
        existing = {m.mod_id: m
                    for m in self._repo.list_mods(self._game.app_id)}
        plan = classify_client_items(result.items, existing,
                                     app_id=self._game.app_id)
        self._plan = plan
        self._btn_intake.setEnabled(bool(plan.to_register or plan.to_fill_hint))
        self._btn_copy.setEnabled(False)  # 命令在纳入成功后才生成
        self._render_preview(plan, result)

    def _render_preview(self, plan: ClientIntakePlan, result) -> None:
        assert self._game is not None
        n_work = len(plan.to_register) + len(plan.to_fill_hint)
        lines = [f"客户端账本：{result.acf_path}",
                 f"已安装 {len(result.items)} 条："
                 f"待纳入 {len(plan.to_register)} · "
                 f"待补版本线索 {len(plan.to_fill_hint)} · "
                 f"已在册跳过 {len(plan.already_tracked)} · "
                 f"已下载跳过 {len(plan.already_downloaded)} · "
                 f"已删除/失效不复活 {len(plan.inactive)}"]
        # 跳过明细：疑似下载中断（要重下）与格式类（要排查）分开报数
        inter = len(result.interrupted)
        other = len(result.skipped) - inter
        if result.skipped:
            lines.append(f"（客户端账本里跳过 {len(result.skipped)} 条："
                         f"疑似下载中断 {inter}、格式不完整 {other}）")
        lines += [f"· {w}" for w in result.warnings]
        if not result.items:
            lines.append("客户端账本里没有已安装条目：只订阅了、客户端还没"
                         "下载完成的不会出现在这里——本来也没有文件。")
        elif plan.to_register:
            lines.append("待纳入编号：" + _ids_text(
                [i.mod_id for i in plan.to_register]))
        if n_work:
            self._set_card(self._d4,
                           f"解析完成，{n_work} 条可写入（只补缺不覆盖）——"
                           "核对下方明细后点【纳入账本】。", _C_INFO)
        else:
            self._set_card(self._d4,
                           "全部已在册，无事可做（重复纳入是安全的："
                           "要么跳过要么只补缺）。", _C_OK)
        self._intake_detail.setText("\n".join(lines))

    def _on_intake(self) -> None:
        if self._game is None or self._plan is None:
            return
        plan = self._plan
        n_work = len(plan.to_register) + len(plan.to_fill_hint)
        if not n_work:
            return
        ret = QMessageBox.question(
            self, "纳入账本",
            f"把 {n_work} 条纳入档案「{self._game.name}」？\n\n"
            "· 只补缺不覆盖：账里已有版本线索的一律不动\n"
            "· 已删除 / 已失效的条目不会复活")
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            rep = apply_intake(self._repo, plan)
        except sqlite3.IntegrityError:
            # 对表之后别的页面恰好登记了同一编号：整批回滚（引擎保证
            # 不会写一半），重新预览即可
            QMessageBox.warning(self, "纳入失败",
                                "部分编号刚被其他页面登记过，本次已整批回滚，"
                                "未写入任何数据。\n请重新点【解析预览】。")
            self._log.error("纳入失败：编号与其他页面撞车，已整批回滚")
            return
        # 命令覆盖"纳入后仍没有本地内容"的条目（新纳入 + 补了线索的）：
        # 已下载跳过的除外——steamcmd 已经管着它们了
        cmd_ids = ([i.mod_id for i in plan.to_register]
                   + [i.mod_id for i in plan.to_fill_hint])
        self._cmd_text = build_copy_text(self._game.app_id, cmd_ids) \
            if cmd_ids else ""
        self._btn_copy.setEnabled(bool(self._cmd_text))
        self._btn_intake.setEnabled(False)  # 本批已写完，重复点无意义
        # 选过的库目录记进设置，下次免选（新键 steam_client_library）
        if self._settings is not None:
            self._settings.set("steam_client_library",
                               self._dir_edit.currentText().strip())
            self._settings.save()
        self._set_card(
            self._d4,
            f"纳入完成：新入库 {rep.registered} · 补版本线索 "
            f"{rep.hints_filled}。接下来：\n"
            "① 【复制下载命令】到 steamcmd 执行（或到【mod 库】页勾选这批"
            "条目点【下载选中项】）；\n"
            "② 下载完到【mod 库】页点【扫描本地】确认；\n"
            "③ 确认后到 Steam 客户端把这批 mod 退订——不要两边同时订阅。",
            _C_OK)
        self._log.ok(f"纳入已有 mod 完成：新入库 {rep.registered} 条，"
                     f"补版本线索 {rep.hints_filled} 条"
                     + (f"；下载命令已生成（{len(cmd_ids)} 个编号）"
                        if self._cmd_text else ""))

    def _on_copy(self) -> None:
        text = getattr(self, "_cmd_text", "")
        if not text.strip():
            return
        QApplication.clipboard().setText(text)
        self._log.info(f"已复制 {text.count(chr(10)) + 1} 行下载命令到剪贴板："
                       "去终端粘贴执行")

    # ---------- 卡片基建（样式与加入新 mod 页同款） ----------
    def _make_card(self, name: str, title: str) -> tuple[QLabel, QVBoxLayout]:
        """造一张固定步骤卡，返回（状态行, 内容布局）。样式选择器限定到
        本框（QFrame#card_xxx），否则边框会画到卡里每行字上。"""
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
