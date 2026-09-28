"""欢迎页
"""
r"""gui/welcomePage.py · 启动默认页：项目门面 + 小白说明书。 纯静态展示：不接数据库、不接设置、没有任何按钮——改内容只改文案。 定位是"第一次打开的小白"和"三个月后忘了怎么用的自己"共用的说明书。 整页装进滚动区（内容总高必超窗口，统计页同款处理）；可能变长的 标签一律开自动换行（踩坑⑨）；正文可选中复制。 v2.38 重写（决策 71，打包前 todo 0）： ① 页首 = 图标 + 标题 + 版本 + 一句话定位。图标读 icon/icon.png （resource_path 单源，与 main.py 同一门）；版本号读 QApplication.applicationVersion()（main.py 已设 1.0.0，这里 不硬编码——将来升版本只改 main.py 一处，欢迎页与「关于」 对话框自动跟上）。图标或版本任何一样缺失都静默降级：少显示 一块装饰，绝不挡住说明书本体（stderr 留一行，照 main.py 兜底）。 ② 新增「项目主页」节：开源地址做成可点击链接（点击直接开系统 浏览器），正文依旧可选中复制。地址常量 PROJECT_URL 在本文件 定义（唯一定义点），MainWindow 的「关于」对话框 import 同一份， 不复制第二份（决策 61④ 单源同思路）。 ③ 排版改造：原"整节挤一个 QLabel"改为每行一个 QLabel——某行 自动换行变高时不再把别的行顶歪；节间距靠节头上边距拉开； 分隔线用 QFrame（取色自系统调色板，深浅主题都安全）。美化 只动字号/字重/间距/分组，零硬编码颜色——不抢主题的活 （决策 68 / 踩坑51 的教训同样适用于这里）。 ④ 内容纠偏与增补：「功能模块总表」旧版还停在 v2.16 时代 （日常更新/删除 mod 标"开发中"、后四个模块缺席），按导航树 现状全部点亮；「这是什么工具」补三句白话工作原理（检测更新 /下载/确认）与绿色软件说明；术语速查补 steamcmd 与 备份 两条（manifest 刻意不收——小白用不上）。 决策 53/56：原"首次使用三步（现阶段手工版）"整节撤除改指 「首次使用」向导（v2.20 上线），该结构本轮保留。 """
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core import appPaths

# ---- 本文件的两个"身份证"常量（欢迎页与「关于」对话框共用）----

# 开源项目地址唯一定义点：要改地址只改这一行。
PROJECT_URL = "https://github.com/sefrawe/Steam-Workshop-Mod-Assistant-Management-Tool"

# 图标相对路径（配合 appPaths.resource_path 使用：源码态=项目根，
# 打包态=随包资源目录）。main.py 启动段用的是同一份文件、同一个
# 字符串——main.py 在 import gui 之前就要设图标，不宜反向依赖 gui
# 包，所以两边各写字面量、靠这条注释锁定一致；这里再定义一份常量
# 供本页与「关于」对话框取用。
ICON_REL = "icon/icon.png"

# 页首图标的显示边长（像素）。原图 1646²，缩小交给 Qt 平滑缩放。
_ICON_SIZE = 72


class WelcomePage(QWidget):
    """欢迎页：无可写状态、无 set_game、无 shutdown——
    主窗口的广播循环与收尾循环都会自动跳过它，零接线成本。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        # ---- 页首：图标 + 标题/版本/一句话定位 ----
        # 放在滚动区外，滚动正文时门面不动（统计页没页首，本页特有）。
        root.addWidget(self._build_header())
        root.addWidget(self._hline())

        # ---- 滚动区托管全部正文（内容高于窗口时硬排会被裁掉）----
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 8, 0, 0)
        self._body.setSpacing(4)  # 行距紧凑；节与节的留白靠节头上边距拉开
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._fill()

    # ---------- 页首 ----------

    def _build_header(self) -> QWidget:
        """图标 + 标题 + 版本 + 一句话定位。图标加载失败就整块跳过，
        stderr 留一行——静默降级也要留痕（main.py 同款兜底口径）。"""
        head = QWidget(self)
        h = QHBoxLayout(head)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)

        pix = QPixmap(str(appPaths.resource_path(ICON_REL)))
        if not pix.isNull():
            icon = QLabel(head)
            # 平滑缩放：1646² 缩到 72²，不用 Smooth 会出锯齿
            icon.setPixmap(pix.scaled(
                _ICON_SIZE, _ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            icon.setFixedSize(_ICON_SIZE, _ICON_SIZE)
            h.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        else:
            # 图标只是装饰，缺了不拦着用软件（QIcon/QPixmap 加载失败
            # 都不报错、只会静默空白——所以这里显式检查并留痕）
            print(f"[welcomePage] 图标缺失，页首不显示图标："
                  f"{appPaths.resource_path(ICON_REL)}", file=sys.stderr)

        text = QVBoxLayout()
        text.setSpacing(2)

        title = QLabel("Steam 创意工坊 Mod 辅助管理工具", head)
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        text.addWidget(title)

        # 版本号读全局（main.py setApplicationVersion 设定）。
        # 读不到就不显示这一行——宁可少显示，不显示假信息。
        ver = QApplication.applicationVersion()
        if ver:
            ver_label = QLabel(f"版本 {ver}", head)
            ver_label.setStyleSheet("font-size: 12px;")
            text.addWidget(ver_label)

        slogan = QLabel(
            "管理 Steam 创意工坊 mod 的本地工具：登记、检测更新、生成"
            "下载命令、备份与恢复——全程不需要打开 Steam 客户端。", head)
        slogan.setWordWrap(True)
        text.addWidget(slogan)

        h.addLayout(text, 1)
        return head

    # ---------- 正文内容 ----------

    def _fill(self) -> None:
        self._section("这是什么工具", [
            "所有记录都保存在本地数据库（本说明书里叫「账本」）；下载靠"
            "官方命令行工具 steamcmd，在本窗口底部的终端面板里完成。",
            " ",
            "工作原理三句话：",
            "  ① 检测更新——本工具向 Steam 官方接口询问每个 mod 最新"
            "版本的更新时间，再和硬盘上 steamcmd 记录的本地时间对比："
            "谁的时间新，谁就是新版本；",
            "  ② 下载——本工具把要下载的 mod 整理成命令文本，你复制"
            "粘贴到底部终端（steamcmd）里执行；软件绝不代替你执行，"
            "也不碰 Steam 客户端；",
            "  ③ 确认——下载完成后扫描一遍 steamcmd 的账本文件，把"
            "「真的下载好了」写回本工具的账本。",
            " ",
            "本软件是绿色软件：账本、设置、日志等所有数据都保存在软件"
            "自己的文件夹里。想搬到别的电脑，把整个文件夹拷走即可；"
            "想卸载，删掉整个文件夹即可（左导航「卸载与清理」页有"
            "逐步说明）。",
            " ",
            "几条提醒：",
            "  ① 用本工具管理的游戏，请不要再在 Steam 客户端里订阅同样"
            "的 mod——两边各记各的账、互不知情，版本会乱；",
            "  ② 终端里请使用英文命令，避免输入中文；",
            "  ③ 与这台 steamcmd 共用的其他前端（如 RimSort）下载的 mod，"
            "扫描时会自动纳入账本，不需要重复登记；本工具永远只读 "
            "steamcmd 的账实文件。",
        ])
        self._section("两种用法（导航树的结构）", [
            "功能模块（导航树最上面的组）：一件事跟着步骤卡片从头走到尾，"
            "适合「知道目标、不知道步骤」。每个模块页顶部都自带说明块："
            "流程、为什么这么做、术语与相关基础功能的关系。",
            "基础功能（导航树其余各项）：一个功能一页直达，适合熟手。"
            "每个按钮都写了悬浮说明——鼠标停上去就能看到「做什么 + "
            "做完发生什么」。",
            "两条路不重复：模块里的每一步都是调用对应的基础功能，"
            "账本永远只有一本。",
        ])
        self._section("术语速查", [
            "档案 一个游戏的专属管理空间（左上角下拉切换）。",
            "AppID 游戏在 Steam 的数字编号。例：RimWorld = 294100，"
            "十字军之王3 = 1158310。",
            "账本 本软件的本地数据库：每个 mod 的编号、状态、版本、"
            "备注都记在这里。",
            "steamcmd Valve 官方免费的命令行下载工具，本软件的「下载"
            "引擎」——mod 全靠它从创意工坊下载到硬盘。需要你自己从 "
            "Valve 官网下载，放进一个纯英文路径的空文件夹（「首次使用」"
            "第①步会引导）。",
            "acf steamcmd 自己维护的工坊账本文件"
            "（appworkshop_<AppID>.acf）。本软件只读它，永不修改。"
            "Steam 客户端的库目录里也有一份同名文件，记的是客户端"
            "自己的订阅——「首次使用」第④步读它，两棵树互不相干。",
            "状态 已收录 = 账上有编号、还没确认下载到硬盘；已下载 = "
            "扫描确认文件真的在盘上；已删除 = 软删除（记录保留、可恢复）；"
            "已失败 = 远端失效等，进异常处理。",
            "扫描 读取 steamcmd 的账实文件，把硬盘上的真实情况写回账本。"
            "「盘上真相」只认这个文件。",
            "连接 一条 Windows 目录联接（junction），把 steamcmd 的"
            "下载目录接到游戏读取 mod 的目录——下载完游戏就能看到。"
            "详见「游戏」菜单 → 连接指引。",
            "备份 更新 mod 之前，先把硬盘上的旧版本完整复制一份存起来；"
            "万一新版本有问题，随时恢复回旧版。在「备份与恢复」页管理，"
            "可设置保留份数与总容量上限。",
        ])
        # 富文本加粗行（<b> 标签）。注意：这几行刻意不写 <、>、& 字符——
        # 一旦含标签整行按 HTML 解析，裸的尖括号会被当成标签吞掉
        # （术语表 acf 行里的 appworkshop_<AppID> 就是因此保持纯文本）。
        self._section("功能模块总表（全部已上线，与左导航「功能模块」组对应）", [
            "<b>加入新 mod</b> —— 收集 mod 网址，入账并生成下载命令；"
            "也支持从浏览器一键取标签页。下载后扫描确认。",
            "<b>首次使用</b> —— 从零开始：定位 steamcmd，建第一个游戏"
            "档案，接通游戏读取目录，把 Steam 客户端里已有的 mod 纳入账本。",
            "<b>日常更新</b> —— 检测更新 → 勾选确认（可选先备份旧版本）"
            "→ 批量下载 → 自动复扫入账。",
            "<b>清理与删除</b> —— 盘点「账上有、盘上没有」与「盘上多出来」"
            "的目录，安全处置（软删除可恢复）与清理。",
            "<b>恢复旧版本</b> —— 新版本出了问题？从备份里把旧版本找回来，"
            "三步走完。",
            "<b>换机迁移</b> —— 换电脑或整机搬家：导出账本 → 新机导入 → "
            "搬文件 → 重建连接，七步引导。",
            "<b>分享清单</b> —— 把自己的收录清单（含备注、标签、特别关注）"
            "导出发给别人；收到别人的清单也能一键并入。",
            "<b>卸载与清理</b> —— 绿色软件的「反安装」：会话记忆存在哪、"
            "注册表残留怎么清、软件文件夹里都有什么。",
        ])
        self._section("第一次用？走「首次使用」向导", [
            "左侧导航 → 功能模块 → 首次使用：四张步骤卡，可跳步、可重入，"
            "缺哪步走哪步，重复做不会有副作用：",
            "  ① 定位 steamcmd —— 「设置」页填 steamcmd 程序路径"
            "（Valve 官方下载，放进纯英文路径的空文件夹）；",
            "  ② 建立游戏档案 —— 填游戏 AppID 即建档，下载目录自动推导；",
            "  ③ 接通游戏读取目录 —— 连接指引生成 junction 命令，"
            "复制到 cmd 里执行；",
            "  ④ 纳入客户端已有的 mod —— 早就用 Steam 客户端订阅了一堆 "
            "mod？指认客户端库目录，向导读取订阅记录对表入账"
            "（只补缺、不覆盖、不复活已删除条目）。之后用 steamcmd 补"
            "下载、扫描确认，最后到 Steam 客户端把同一批 mod 退订"
            "——不要两边同时订阅。",
            "什么都不缺的熟手：直接去「加入新 mod」或「网址批量导入」"
            "录 mod 就能用了。",
        ])
        self._body.addWidget(self._hline())
        self._section("项目主页", [
            "本工具开源，代码与更新发布都在 GitHub 上。遇到问题、想提"
            "建议、想拿最新版本，请到项目主页：",
        ])
        link = QLabel(
            f'<a href="{PROJECT_URL}">{PROJECT_URL}</a>'
            "&nbsp;&nbsp;（点击直接用浏览器打开；也可以选中复制）", self)
        link.setWordWrap(True)
        # 点击链接交给系统默认浏览器（Qt 不代管网页）；
        # TextBrowserInteraction = 文本可选中复制 + 链接可点击，两全。
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self._body.addWidget(link)

        self._body.addStretch(1)

    # ---------- 基建 ----------

    @staticmethod
    def _hline() -> QFrame:
        """主题安全的水细分隔线：QFrame 线条取色自系统调色板，
        深色/亮色主题都不用管（零硬编码颜色）。"""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _section(self, title: str, lines: list[str]) -> None:
        """一节 = 节头 + 若干行，每行独立 QLabel。
        旧版整节挤一个 QLabel，任何一行折行都会影响整块排版；
        拆行后各管各的。节头上边距 12px = 节间留白（行距只有 4，
        靠这里拉开层级）。内容里的空字符串行会塌成 0 高，所以
        空行一律传一个空格 " "。"""
        head = QLabel(title, self)
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        head.setContentsMargins(0, 12, 0, 2)
        self._body.addWidget(head)
        for text in lines:
            lab = QLabel(text, self)
            lab.setWordWrap(True)  # 可能变长的标签一律开换行（踩坑⑨）
            lab.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self._body.addWidget(lab)
