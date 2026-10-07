"""首次使用向导
"""
"""
gui/firstUsePage.py · 左导航「分步向导」组：四步引导页——定位
steamcmd → 建档 → 接通读取目录 → 纳入客户端已有的 mod。四张步骤卡
可跳步、可重入：老手缺哪步走哪步，已完成的步骤亮绿状态，重复进入
不产生副作用。

页面只做串联，不抄第二份实现（与「加入新 mod」同一单源纪律）：
- ①②③ 全是既有能力的入口：设置页（发 settings_requested 信号）、
  建档对话框（转调左上角档案切换器的现成对话框）；③的"接通"在
  V2 = 跳【联接检测】页（原连接指引对话框已退役，页面版能力一致：
  检测通过会自动记录游戏读取目录）——信号名 link_guide_requested
  保留不动，落点由主窗口接到联接检测页；
- ④ 是本页唯一的新功能，逻辑住在 workflows/intakeFlow（零 Qt，
  pytest 覆盖）：读 Steam 客户端库的 appworkshop_<appid>.acf 已安装
  清单（core/localScanner 解析），与账本对表，只补缺不覆盖。

V2 判决制口径（与 V1 的不同）：
- 纳入只登记「待下载」（tracked），一个版本字段都不碰——客户端
  账本里的时间是安装事实，不是版本判决；真实版本等下载后在批次
  收尾的确认清单里背书，或跑一次更新检测自然补全（四桶无"补版本
  线索"，拍板记录）；
- 纳入后的闭环两条路：命令贴到程序底部的 steamcmd 终端 → 走本工具
  批次链，收尾自动弹确认清单；自己开终端跑 → 到【入账中心】点
  【扫描游戏目录】认领。没有"扫描确认"这个动作了。

为什么纳入的是"客户端订阅"：向导服务的场景是"已经在玩、在 Steam
里订阅了 mod"的用户——他们的 mod 内容躺在客户端自己的库目录里，
steamcmd 和本工具对此一无所知。纳入后管理权交接到本工具（退订指引
见第④步完成文案）。一个边界要说透：客户端账本只统计"已安装"的
条目；只订阅了、客户端还没下载完的内容不会出现在这里——本来也
没有文件，无从纳入。
"""
import sqlite3
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.appSettings import AppSettings
from core.commandBuilder import build_copy_text
from core.localScanner import locate_acf, scan_acf
from core.models import Game
from core.steamPaths import client_library_roots, workshop_content_dir
from gui.consolePanel import LogBus
from workflows.intakeFlow import ClientIntakePlan, apply_intake, classify_client_items
from gui.theme import font_px  # 字号单源（D25）

# 与加入新 mod 页、日常更新页一致的配色，按用途命名
_C_OK = "#46a758"     # 完成 / 就绪
_C_FAIL = "#e5484d"   # 错误 / 硬拦
_C_WARN = "#f5a623"   # 需要注意（未配置、未确认）
_C_INFO = "#d4d4d4"   # 进行中
_C_MUTED = "#8a8a8f"  # 说明文字 / 等待

_ID_LIMIT = 20  # 编号最多原样列出多少个，超出折成"…"

# —— 安装引导卡文案：内容改文案只动下面四个常量，布局与接线不用碰。
# 顺序 = 先 steamcmd（本工具一切功能的地基），后 Watt Toolkit（浏览器
# 访问工坊页的前置）。本页不检测装没装——第三方软件检测不了，也不该
# 装作能检测；两张卡是纯说明，永远显示
_GUIDE_STEAMCMD_TITLE = "准备 · 安装 steamcmd"
_GUIDE_STEAMCMD = (
    "安装 steamcmd（Steam 命令行工具）是本工具的前置条件："
    "官网：https://developer.valvesoftware.com/wiki/SteamCMD#Downloading_SteamCMD\n"
    "下载后解压得到steamcmd.exe，先别急着点进去，首次运行会自动安装到当前目录下，应当把它移到一个固定目录（如 D:\\steamcmd）再运行，\n"
    "请慎重选择目录，事关本软件的 mod 下载与备份和文件夹连接等功能，用一段时间后改变非常麻烦；不能装在带中文的路径，会闪退。\n"
    "调整好位置后运行 steamcmd.exe 让它安装完成，务必关闭watt Toolkit等加速器，否则无法安装，提示无法下载；"
    "装好后建议登录一次（steamcmd +login <用户名>），第一次登录会要求输入密码和验证码，就有了本地缓存（steamcmd自身行为，与此软件无关），之后登录只用输入登录命令即可；"
    "额外提示：使用steamcmd下载mod需要登录的账号拥有对应游戏，否则报错。千万不能输入中文，否则会一直输出“？？？？？？”只能关闭软件\n"
    "装好后回下方第①步填路径。"
)
_GUIDE_WATT_TITLE = "推荐 · Watt Toolkit（原 Steam++）"
_GUIDE_WATT = (
    "Watt Toolkit 是一个网络加速工具，可以加速Steam、github等相关平台。"
    "官网：https://steampp.net/ \n"
    "安装后启动点击网络加速，勾选并启动 Steam 相关加速，这样浏览器才能正常打开工坊页和提高访问相关api（比如日常更新模块的获取更新信息）成功率。而有些api访问在watt Toolkit启动时会失败，比如异常处理模块深度联网检测时访问的api。"
    "网络加速页，头像下面的“网络加速”的傍边有个“脚本配置”，点进去后点“脚本工坊”，登录后，推荐安装“steam创意工坊大图修复”\n"
    "注意：Watt Toolkit 不是本工具的前置条件，没装也能用，但装了就能访问创意工坊和提高访问相关api的成功率。"
)


class FirstUsePage(QWidget):
    # 三个转调信号：主窗口接线——设置页跳转 / 建档对话框 / 联接检测页
    # （信号名沿用 V1 的 link_guide_requested：V2 落点改为联接检测页，
    # 连接指引对话框已退役；内部名字不动，省两处同步）
    settings_requested = Signal()
    add_game_requested = Signal()
    link_guide_requested = Signal()
    # ⑤ 步三跳（主窗口接 _goto_page）：b 认领 / c 设基准 / d 首轮检测
    goto_account_center = Signal()
    goto_mod_list = Signal()
    goto_daily_update = Signal()

    def __init__(self, repo, settings: AppSettings | None,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
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

        title = QLabel("首次使用", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        root.addWidget(title)

        # 说明块（每个功能模块页自带"流程 / 为什么 / 术语与关系"）
        note = QFrame(self)
        note.setObjectName("fu_note")
        note.setStyleSheet(
            "QFrame#fu_note { border: 1px solid #3a3a3a;"
            " border-radius: 6px; }")
        nb = QVBoxLayout(note)
        nb.setContentsMargins(8, 6, 8, 6)
        nb.setSpacing(2)
        for text in (
                "流程：① 定位 steamcmd → ② 建立游戏档案 → ③ 接通游戏读取"
                "目录 → ④ 纳入客户端已有的 mod（Steam 订阅迁移）或 ⑤ 录入"
                "已有的 mod 文件（其余来源，一路指引到跑通第一轮日常更新）。"
                "各步可跳步、可重入，缺哪步走哪步。",
                "为什么这么做：纳入走「先预览再写入」，只补缺不覆盖、已删除/"
            "已失效的不复活；纳入后是「待下载」，下载完在批次收尾的确认"
            "清单里勾选背书才算「已下载」——客户端那份拷贝不算数（账实"
            "口径以 steamcmd 目录为准）。",
            "术语与关系：客户端订阅记录 = 客户端库里的 appworkshop_"
            "<appid>.acf，与 steamcmd 的同名文件是两棵树、互不知情；"
            "纳入后的下载与入账走「加入新 mod」同一条链（命令同源、"
            "批次收尾确认清单同一扇门）。",
            "多前端共存：与这台 steamcmd 共用的其他前端下载的 mod，"
            "到【入账中心】点【扫描游戏目录】会把它们摆成待认领候选，"
            "核对后认领入账即可，不需要重复登记；本工具永远只读 "
            "steamcmd 的账实文件。",
        ):
            lbl = QLabel(text, note)
            lbl.setWordWrap(True)  # 可能变长的标签一律开换行
            lbl.setStyleSheet(f"border:none; color:#8a8a8f; font-size: {font_px(12)}px;")
            nb.addWidget(lbl)
        root.addWidget(note)

        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)

        # —— 安装引导区：两张纯说明卡，置于四步之前——静态卡：
        # 不参与刷新，内容即常量
        _d_prep1 = self._make_card("prep_steamcmd", _GUIDE_STEAMCMD_TITLE)[0]
        _d_prep1.setText(_GUIDE_STEAMCMD)
        _d_prep2 = self._make_card("prep_watt", _GUIDE_WATT_TITLE)[0]
        _d_prep2.setText(_GUIDE_WATT)

        # ① steamcmd 卡：状态 + 去设置页 + 重新检查
        self._d1, box1 = self._make_card("step1", "① 定位 steamcmd")
        row1 = QHBoxLayout()
        self._btn_settings = QPushButton("去设置页填写")
        self._btn_settings.setToolTip("跳到「设置」页填 steamcmd 程序路径；"
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

        # ③ 连接卡：状态 + 跳联接检测页（V2：原连接指引对话框退役，
        # 页面版能力一致——检测通过会自动记录游戏读取目录）
        self._d3, box3 = self._make_card("step3", "③ 接通游戏读取目录")
        row3 = QHBoxLayout()
        self._btn_link = QPushButton("打开联接检测页…")
        self._btn_link.setToolTip("跳到「联接检测」页（与「游戏」菜单的"
                                  "连接指引同一落点）：判定游戏读取目录"
                                  "与下载目录的联接状态，按步骤接通；"
                                  "检测通过会自动记录")
        self._btn_link.clicked.connect(self.link_guide_requested.emit)
        row3.addWidget(self._btn_link)
        row3.addStretch(1)
        box3.addLayout(row3)
        # ③ 连接卡：状态 + 跳联接检测页 + 重新检查（联接检测页写库
        # 后本页不广播不自动知道——切回本页有进页钩子兜底，按钮再
        # 给个手动对账口，从检测页回来点一下即亮绿）
        self._d3, box3 = self._make_card("step3", "③ 接通游戏读取目录")
        row3 = QHBoxLayout()
        self._btn_link = QPushButton("打开联接检测页…")
        self._btn_link.setToolTip("跳到「联接检测」页（与「游戏」菜单的"
                                  "连接指引同一落点）：判定游戏读取目录"
                                  "与下载目录的联接状态，按步骤接通；"
                                  "检测通过会自动记录")
        self._btn_link.clicked.connect(self.link_guide_requested.emit)
        row3.addWidget(self._btn_link)
        self._btn_recheck3 = QPushButton("重新检查")
        self._btn_recheck3.setToolTip(
            "从联接检测页检测通过回来后点它：重读档案最新状态，"
            "第③步就会亮绿。切回本页时其实也会自动刷新，"
            "按钮是手动兜底")
        self._btn_recheck3.clicked.connect(self._on_recheck)
        row3.addWidget(self._btn_recheck3)
        row3.addStretch(1)
        box3.addLayout(row3)

        # ④ 纳入卡：本页唯一的新功能
        self._d4, box4 = self._make_card("step4", "④ 纳入客户端已有的 mod")
        self._intake_detail = QLabel("", self)
        self._intake_detail.setWordWrap(True)
        self._intake_detail.setStyleSheet(f"border:none; font-size: {font_px(12)}px;")
        box4.addWidget(self._intake_detail)
        row_dir = QHBoxLayout()
        # ④ 目录框 = 可编辑下拉：注册表 + libraryfolders.vdf 探测到的
        # 客户端库预填成候选；设置里存过的排最前；都失败就空着手填——
        # 探测是预填便利，手填能力永不删
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
        row_dir.addWidget(self._btn_browse)
        box4.addLayout(row_dir)

        row4 = QHBoxLayout()
        self._btn_preview = QPushButton("解析预览", self)
        self._btn_preview.setToolTip("读客户端库里的 appworkshop_<appid>.acf，"
                                     "与当前档案账本对表；只读不写")
        self._btn_preview.clicked.connect(self._on_preview)
        self._btn_intake = QPushButton("纳入账本", self)
        self._btn_intake.setToolTip("把预览出的「待纳入」条目写进账本"
                                    "（状态「待下载」）；只补缺不覆盖，"
                                    "写前有确认弹窗")
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
        # ⑤ 录入已有 mod 文件（静态引导卡）：无状态刷新、零业务逻辑，
        # 三个按钮 = 跳既有页面的既有入口，本页只串联（文件头"只做
        # 串联，不抄第二份实现"同纪律）。落点由主窗口接三个信号
        self._d5, box5 = self._make_card(
            "step5", "⑤ 录入已有的 mod 文件 → 跑通第一轮日常更新")
        guide = QLabel(
            "mod 文件已经在手里（以前用 steamcmd 下过、换机带来的、"
            "别的下载器下的——Steam 客户端订阅来的走第④步，不要两边"
            "重复），不用重新下载，四小步：\n"
            "a. 复制进下载目录：mod 文件夹（每个 mod 一个以工坊编号"
            "命名的文件夹）复制到本游戏的下载目录——第①步卡片显示的"
            "那个，steamcmd 和游戏都从它读 mod，就这一个目录；游戏若"
            "只认自己的目录，第③步的联接已处理。\n"
            "b. 认领入账：到【入账中心】点【扫描游戏目录】——盘上"
            "发现、账上没有的摆成「待认领」，逐条确认收录；知道编号"
            "不想扫盘的，直接用【登记】。\n"
            "c. 设定版本基准（防漏更新的关键一步）：到【mod 库】页"
            "全选刚认领的条目 → 操作 ▾ →【批量设定本地版本…】，时间"
            "设为「你最后一次确认全部 mod 都是最新」的那一天；从没"
            "整批确认过，就选最早下载日期再往前推几个月。宁早勿晚："
            "选早了，头一两轮可能把已是最新版的误报成「有更新」——"
            "重下一遍无害且自动纠正；选晚了才会永久漏报更新。\n"
            "d. 首轮日常更新：到【日常更新】点【开始检测】——远端"
            "信息（标题、大小、远端版本）自动补全回写账本，并报出"
            "所有比你版本基准更新的 mod，勾选执行即可。\n"
            "自检点：检测结果里「版本未知」应为 0——不是 0 = 有条目"
            "漏了 c 步，回 mod 库页补上，再检测一次。")
        guide.setWordWrap(True)
        guide.setStyleSheet(
            f"border:none; color:{_C_MUTED}; font-size: {font_px(12)}px;")
        box5.addWidget(guide)
        row5 = QHBoxLayout()
        self._btn_goto_account = QPushButton("去入账中心认领…")
        self._btn_goto_account.setToolTip(
            "跳到「入账中心」：【扫描游戏目录】认领盘上已有文件")
        self._btn_goto_account.clicked.connect(self.goto_account_center.emit)
        self._btn_goto_modlist = QPushButton("去 mod 库设基准…")
        self._btn_goto_modlist.setToolTip(
            "跳到「mod 库」：全选条目 → 操作 ▾ →【批量设定本地版本…】")
        self._btn_goto_modlist.clicked.connect(self.goto_mod_list.emit)
        self._btn_goto_daily = QPushButton("去日常更新检测…")
        self._btn_goto_daily.setToolTip(
            "跳到「日常更新」：【开始检测】补全远端信息并报出更新")
        self._btn_goto_daily.clicked.connect(self.goto_daily_update.emit)
        for b in (self._btn_goto_account, self._btn_goto_modlist,
                  self._btn_goto_daily):
            row5.addWidget(b)
        row5.addStretch(1)
        box5.addLayout(row5)

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
        """公开的刷新入口（主窗口切到本页时自动调——进页刷新钩子；
        卡片①上另有【重新检查】按钮兜底）。空目录框才回填设置值，
        不覆盖用户正在编辑的文本。"""
        if self._settings is not None and not self._dir_edit.currentText().strip():
            self._dir_edit.setCurrentText(
                str(self._settings.get("steam_client_library") or "").strip())
        self._refresh_all()

    # ---------- 卡片刷新 ----------
    def _on_recheck(self) -> None:
        self._refresh_all()
        self._log.info("已重新检查首次使用向导各步状态")

    def _refresh_all(self) -> None:
        """刷新四张卡。开头先从账本重读当前档案：联接检测页检测
        通过会把 game_mod_dir 写进库，但主窗口只让档案切换器静默
        重读、刻意不广播（广播会把检测页刚出的绿勾清掉）——本页
        手里的档案对象还停在写库前，不重读的话第③步会一直显示
        "未确认"，直到重启。进页钩子与【重新检查】按钮都经过
        这里，一处对齐两处生效。"""
        if self._game is not None:
            latest = self._repo.get_game(self._game.app_id)
            if latest is not None:
                self._game = latest
        self._refresh_step1()
        self._refresh_step2()
        self._refresh_step3()
        self._refresh_step4()

    def _refresh_step1(self) -> None:
        exe = self._settings.get("steamcmd_path") if self._settings else ""
        if not exe:
            self._set_card(self._d1, "未配置 steamcmd 程序路径——本工具的下载、"
                           "备份都靠它定位。", _C_WARN)
            return
        if not Path(exe).is_file():
            self._set_card(self._d1, f"配置的 steamcmd 路径暂不存在：{exe}"
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
            self._set_card(self._d2, "还没有游戏档案——需要游戏的 AppID"
                           "（商店页面网址里那串数字）。", _C_WARN)
        else:
            self._set_card(self._d2, f"当前档案：{self._game.name}"
                           f"（{self._game.app_id}）——已就绪。", _C_OK)

    def _refresh_step3(self) -> None:
        if self._game is None:
            self._set_card(self._d3, "等待建档。", _C_MUTED)
        elif self._game.game_mod_dir:
            self._set_card(self._d3, f"已记录游戏读取目录：{self._game.game_mod_dir}"
                           "（随时可重新检查）。", _C_OK)
        else:
            self._set_card(self._d3, "尚未确认连接——部分游戏只从自己的目录读 mod，"
                           "需要把游戏读取目录和下载目录接通。"
                           "不确定就打开检测页判一次，不影响跳过。", _C_WARN)

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
            self._set_card(
                self._d4,
                f"把 Steam 客户端库目录填在下面（{self._game.name}"
                " 的订阅记录），点【解析预览】核对，再【纳入账本】。\n"
                "· 没用 Steam 客户端装过 mod 的话，这里扫不到东西是正常的"
                "——只读客户端的订阅记录。\n"
                "· 两套账互不相通：Steam 客户端和 steamcmd 是两棵独立的树，"
                "各有各的工坊文件夹和记录，互不知情。同一个游戏不要两边"
                "同时订阅——两处各存一份，版本可能错乱。\n"
                "· 登录 steamcmd 会把 Steam 客户端顶下线（Steam 单点登录），"
                "属正常现象，回客户端重新登录即可。",
                _C_MUTED)

    # ---------- 第④步：预览与纳入 ----------
    def _on_browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择 Steam 客户端库目录",
            self._dir_edit.currentText().strip() or "")
        if path:
            self._dir_edit.setCurrentText(str(path))
            self._on_preview()  # 选完自动预览，少点一次

    def _on_preview(self) -> None:
        if self._game is None:
            return
        raw = self._dir_edit.currentText().strip()
        if not raw:
            QMessageBox.information(self, "解析预览", "先选择 Steam 客户端库目录。")
            return
        # locate_acf 认三种填写层（库根 / steamapps / workshop），
        # 与工坊目录布局同一份定位逻辑——单源复用，不重写
        acf = locate_acf(raw, self._game.app_id)
        if acf is None:
            self._set_card(
                self._d4,
                f"在所选目录下没找到 appworkshop_{self._game.app_id}"
                ".acf——确认选的是客户端的库（含 steamapps/workshop"
                " 层级），且这个游戏在该库里装过工坊内容。",
                _C_FAIL)
            self._log.warn(f"未找到客户端账本：{raw}"
                           f"（appworkshop_{self._game.app_id}.acf）")
            return
        try:
            result = scan_acf(acf)
        except ValueError as exc:
            # 文件级问题显式转述，绝不静默
            self._set_card(self._d4, f"客户端账本读取失败：{exc}", _C_FAIL)
            self._log.error(f"客户端账本读取失败：{exc}")
            return
        existing = {m.mod_id: m for m in self._repo.list_mods(self._game.app_id)}
        plan = classify_client_items(result.items, existing,
                                     app_id=self._game.app_id)
        self._plan = plan
        self._btn_intake.setEnabled(bool(plan.to_register))
        self._btn_copy.setEnabled(False)  # 命令在纳入成功后才生成
        self._render_preview(plan, result)

    def _render_preview(self, plan: ClientIntakePlan, result) -> None:
        assert self._game is not None
        n_work = len(plan.to_register)
        lines = [f"客户端账本：{result.acf_path}",
                 f"已安装 {len(result.items)} 条："
                 f"待纳入 {len(plan.to_register)} · "
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
            self._set_card(self._d4, f"解析完成，{n_work} 条可写入（只补缺"
                           "不覆盖）——核对下方明细后点【纳入账本】。",
                           _C_INFO)
        else:
            self._set_card(self._d4, "全部已在册，无事可做（重复纳入是安全的："
                           "要么跳过要么不动）。", _C_OK)
        self._intake_detail.setText("\n".join(lines))

    def _on_intake(self) -> None:
        if self._game is None or self._plan is None:
            return
        plan = self._plan
        n_work = len(plan.to_register)
        if not n_work:
            return
        ret = QMessageBox.question(
            self, "纳入账本",
            f"把 {n_work} 条纳入档案「{self._game.name}」？\n\n"
            "· 只补缺不覆盖：已在册的一律不动（版本字段一个不碰，\n"
            "  真实版本等下载后在确认清单里背书）\n"
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
        # 命令覆盖"纳入后还没有本地内容"的条目（= 新纳入的全部）：
        # 已下载跳过的除外——steamcmd 已经管着它们了
        cmd_ids = [i.mod_id for i in plan.to_register]
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
            f"纳入完成：新入库 {rep.registered} 条（状态「待下载」）。接下来：\n"
            "① 【复制下载命令】粘贴到程序底部的 steamcmd 终端执行——"
            "走本工具的批次链，下载完自动弹确认清单，勾选 = 入账；\n"
            "（也可以复制到自己开的终端里跑：跑完到【入账中心】点"
            "【扫描游戏目录】把它们认领入账）\n"
            "② 确认入账后，到 Steam 客户端把这批 mod 退订——不要两边"
            "同时订阅：不退订的话，客户端会按它自己的订阅清单继续维护"
            "那份拷贝（有更新就自动下载到客户端自己的目录），与本工具"
            "这份并存，版本各走各的还白占空间。",
            _C_OK)
        self._log.ok(f"纳入已有 mod 完成：新入库 {rep.registered} 条（待下载）"
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


def _ids_text(ids: list[int], limit: int = _ID_LIMIT) -> str:
    """编号清单 → 展示文本：最多列 limit 个，超出折成"…"。"""
    head = "、".join(str(i) for i in ids[:limit])
    if len(ids) > limit:
        head += f" …（共 {len(ids)} 个）"
    return head
