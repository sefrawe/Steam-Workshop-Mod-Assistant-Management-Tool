"""欢迎页"""
"""gui/welcomePage.py · 启动默认页：项目介绍 + 术语速查 + 模块总表。
纯静态展示：不接数据库、不接设置、没有任何按钮——改内容只改文案。
定位是"第一次打开的小白"和"三个月后忘了怎么用的自己"共用的说明书。
整页装进滚动区（内容总高必超窗口，统计页同款处理）；可能变长的
标签一律开自动换行（踩坑⑨）；正文可选中复制。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class WelcomePage(QWidget):
    """欢迎页：无可写状态、无 set_game、无 shutdown——
    主窗口的广播循环与收尾循环都会自动跳过它，零接线成本。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Steam 创意工坊 Mod 辅助管理工具", self)
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        root.addWidget(title)

        # 滚动区托管全部内容（教训：内容高于窗口时硬排会被裁掉）
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 8, 0, 0)
        self._body.setSpacing(12)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._fill()

    # ---------- 内容 ----------

    def _fill(self) -> None:
        self._section("这是什么工具", [
            "管理 Steam 创意工坊 mod 的本地工具：登记想要的 mod、检测更新、"
            "生成下载命令、确认下载结果——全程不需要打开 Steam 客户端。",
            "所有记录都保存在本地数据库（本说明书里叫「账本」）；下载靠"
            "官方命令行工具 steamcmd，在本窗口底部的终端面板里完成。",
            "三条提醒：",
            "　① 软件只生成命令文本，由你复制粘贴到终端执行——绝不代执行，"
            "也不碰 Steam 客户端；",
            "　② 用本工具管理的游戏，请不要再在 Steam 客户端里订阅同样的 "
            "mod（两处各存一份、互不知情，版本会乱）；",
            "　③ 终端里请使用英文命令，避免输入中文。",
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
            "档案　　一个游戏的专属管理空间（左上角下拉切换）。",
            "AppID　 游戏在 Steam 的数字编号。例：RimWorld = 294100，"
            "十字军之王3 = 1158310。",
            "账本　　本软件的本地数据库：每个 mod 的编号、状态、版本、"
            "备注都记在这里。",
            "状态　　已收录 = 账上有编号、还没确认下载到硬盘；已下载 = "
            "扫描确认文件真的在盘上；已删除 = 软删除（记录保留、可恢复）；"
            "已失败 = 远端失效等，进异常处理。",
            "扫描　　读取 steamcmd 的账实文件，把硬盘上的真实情况写回账本。"
            "「盘上真相」只认这个文件。",
            "acf　　 steamcmd 自己维护的工坊账本文件"
            "（appworkshop_<AppID>.acf）。本软件只读它，永不修改。",
            "连接　　一条 Windows 目录联接（junction），把 steamcmd 的"
            "下载目录接到游戏读取 mod 的目录——下载完游戏就能看到。"
            "详见「游戏」菜单 → 连接指引。",
        ])
        self._section("功能模块总表", [
            "首次使用 —— 定位 steamcmd、建第一个档案、连接目录、"
            "纳入已有 mod。……… 开发中（现阶段手工三步见下节）",
            "加入新 mod —— 收集网址 → 入账 → 生成命令 → 下载后确认。"
            "……… ✅ 已上线",
            "日常更新 —— 检测更新 → 下载 → 自动复扫确认。……… 开发中"
            "（基础功能已备齐：更新检测页 + 批量下载一条龙）",
            "删除 mod —— 软删除、恢复与清理。……… 开发中"
            "（mod 库页右键已有软删除）",
        ])
        self._section("首次使用三步（现阶段手工版）", [
            "1. 「设置」页填写 steamcmd 程序路径：从 Valve 官方下载 "
            "steamcmd.exe，放进一个纯英文路径的空文件夹。",
            "2. 「游戏」菜单 → 添加游戏档案（临时）：填游戏 AppID 和名字"
            "即建档，下载目录自动推导。",
            "3. 「游戏」菜单 → 连接指引：把游戏的 mod 目录接到下载目录"
            "（命令已生成好，复制到 cmd 里执行即可）。",
            "之后到「功能模块 → 加入新 mod」或「基础功能 → 网址批量导入」"
            "录入 mod 就能用了。",
        ])
        self._body.addStretch(1)

    # ---------- 基建 ----------

    def _section(self, title: str, lines: list[str]) -> None:
        head = QLabel(title, self)
        head.setStyleSheet("font-size: 15px; font-weight: 600;")
        self._body.addWidget(head)
        body = QLabel("\n".join(lines), self)
        body.setWordWrap(True)  # 可能变长的标签一律开换行
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body.addWidget(body)
