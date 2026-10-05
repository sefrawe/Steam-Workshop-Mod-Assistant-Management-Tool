"""联接检测页
"""
r"""gui/junctionCheckPage.py · 给"游戏自己的 mod 目录"和"steamcmd 下载
目录"牵线的操作指引（V2：由 V1 的连接指引对话框升级成导航页）。

背景：部分游戏只从自己的目录读 mod（如 CK3 读 Documents 下的 mod
文件夹），而 steamcmd 下载的内容在 steamapps\workshop\content\<appid>
——两边对不上，游戏就看不见下载的 mod。标准解法是建一个目录联接
（junction）。两种拓扑都成立，本页都认识（判定在
core/steamPaths.junction_state，本文件零判断）：
  正向：游戏 mod 目录（联接） --mklink /J--> 下载目录（实体）
  反向：下载目录（联接） --mklink /J--> 游戏 mod 目录（实体）
反向拓扑不是纸上谈兵：实测用户环境就是"steamcmd 侧 content\<appid>
是联接、游戏侧是真实目录"——判定函数 v1 只认正向，把这种环境误报
成"需搬运再建链"（照做等于对着同一份数据自己搬自己），v2 起双向都认。

本页只做四件事：
1. 实况判定：把游戏 mod 目录的当前路径填进来，判定它处于哪种状态
2. 产出命令：按状态生成 mklink /J（建链）、rmdir（拆链/移除）、
   mkdir（接通悬空反向联接）命令，一键复制。命令永远由用户自己在
   cmd 里执行——本页全程只读，不做任何盘上写操作
3. 步骤说明：按状态给出对应的操作顺序
4. 检测通过写档案：判定为"已连接"（正向 linked / 反向
   linked_reverse）时，把该路径写进档案现成的 game_mod_dir 字段
   ——该字段的定义就是"游戏读取 mod 的目录"，两种拓扑下这个值都
   等于用户刚填、且刚被判"已连接"的那个路径，写账只是把刚确认的
   事实落到现有字段（走 repo.update_game 正门，不加字段、不升
   schema 版本）。写库、去重、日志全在本页，零外部接线。

半通状态（linked_reverse_missing 等）不写档案：游戏此刻还读不到
mod，把目录记进账等于把"没接通"记成"已接通"。

rmdir 的安全性（已写进界面文案）：rmdir 对联接只摘链接本体、不动
实体内容；对非空的真实目录会直接报错拒绝——两条都是保护。

联接应指向的"实体"由本页按 steamcmd 当前位置现推导（core 的权威
公式），推导不出才退回档案里记录的下载目录。历史上手填过下载目录
的档案可能记录的是游戏侧路径，现推导让本指引与这类历史数据解耦；
两者不一致时界面提示一句，但不擅自改档案记录。

【与 V1 对话框的差别】
- 跟当前档案走：主窗口切档案时 set_game 重填实体路径；
- 输入框预填：档案里存过 game_mod_dir 就直接预填进框（V1 刻意
  每次现填——对话框每次重开、什么都不知道；页面常驻，档案里刚
  确认过的事实就该拿来用，熟手省一次粘贴。检测前反正会重新判定，
  预填不带来任何"拿旧结论当新事实"的风险）；
- 检测通过直接写档案（页面自己持 repo），不再需要信号绕主窗口。
"""
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core import steamPaths
from core.models import Game
from gui.logBus import LogBus
from gui.theme import font_px  # 字号单源（D25）

# 状态 → (颜色, 结论一句话)。命令显隐与步骤文案在 _refresh 里按状态分派
_STATE_LOOKS = {
    "linked": ("#46a758", "✓ 已连接：游戏 mod 目录是联接，且正确指向下载目录"),
    "linked_reverse": ("#46a758", "✓ 已连接（反向拓扑）：下载目录本身是联接，指向游戏 mod 目录"
                       "——无需任何操作"),
    "linked_reverse_missing": ("#f5a623", "接了一半：下载目录是联接、也指对了位置，"
                               "但游戏 mod 目录当前不存在（steamcmd 写入会失败）"),
    "wrong_target": ("#e5484d", "✗ 指错了地方：游戏 mod 目录是联接，但没有指向本档案的下载目录"),
    "wrong_target_reverse": ("#e5484d", "✗ 下载目录本身是个联接，但指向了别处——"
                             "steamcmd 的内容写不进游戏 mod 目录"),
    "empty_dir": ("#f5a623", "未连接：位置存在但是个空目录（建链前需先移除它）"),
    "real_dir": ("#f5a623", "未连接：这是真实目录且里面有内容（需先搬运再建链）"),
    "missing": ("#f5a623", "未连接：该位置还不存在（可直接建链）"),
    "file": ("#e5484d", "✗ 该位置被一个普通文件占用，无法建联接"),
    "no_target": ("#e5484d", "✗ 推导不出下载目录（steamcmd 未配置），档案里也没有记录——"
                  "先到设置页填 steamcmd 程序路径，或重新建档"),
}


class JunctionCheckPage(QWidget):
    """单个档案的连接指引（页面版）。档案随主窗口切换，路径与
    实体随之刷新；判定逻辑与命令产出与 V1 对话框一字不差。"""
    # 检测通过写档案后发给主窗口：让档案切换器重读最新档案信息
    # （带参数 = 刚写入的档案 AppID；主窗口那边只管刷新，不看参数）
    game_dir_written = Signal(int)

    def __init__(self, repo, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings          # 读"steamcmd 程序路径"做推导用
        self._log = log or LogBus()
        self._game: Game | None = None
        # 联接应指向的实体目录：随档案现推导，统一去引号/空白，
        # 与 junction_state 同口径
        self._target = ""
        self._build_ui()
        self._show_idle()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 内容装进滚动区——提示/步骤/命令块加起来高过窗口时会被裁掉
        # （V1 实测步骤文案显示不全）；本页无"关闭"键，全部可滚
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        v = QVBoxLayout(body)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(6)

        title = QLabel("联接检测", self)
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
        v.addWidget(title)

        self._game_label = QLabel("", self)
        v.addWidget(self._game_label)

        tip = QLabel(
            "部分游戏只从自己的目录读 mod（如 CK3：Documents 下的 mod 文件夹），"
            "而 steamcmd 下载的内容在 steamcmd 的工坊 content 目录里。\n"
            "办法：用目录联接（junction）把两边接通——联接建在哪侧都行"
            "（本工具两种方向都认识），游戏就能读到全部下载内容。\n"
            "不知道路径？查该游戏的 mod 安装说明——路径随游戏而定，本工具无法代查。",
            self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        v.addWidget(tip)

        self._target_label = QLabel("", self)
        self._target_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        # 下载目录路径可能很长：允许换行，窄窗口下才不会把页面
        # 撑出横向滚动条——只留纵向滚动，页面才是"紧凑可滚"
        self._target_label.setWordWrap(True)
        v.addWidget(self._target_label)

        # 档案记录值与现推导值不一致时的提示（历史手填数据会走到这里）。
        # 只提示不改档案——修正通道留给档案编辑界面
        self._mismatch_note = QLabel("", self)
        self._mismatch_note.setWordWrap(True)
        self._mismatch_note.setStyleSheet("color: gray;")
        self._mismatch_note.hide()
        v.addWidget(self._mismatch_note)
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel("游戏 mod 目录（联接所在位置）：", row))
        self._input = QLineEdit(row)
        self._input.setPlaceholderText(
            r"如 C:\Users\<用户名>\Documents\Paradox Interactive\Crusader Kings III\mod")
        self._input.returnPressed.connect(self._on_check)
        self._input.textEdited.connect(self._on_edited)
        h.addWidget(self._input, 1)
        # 检测键与输入框同行：本页没有"目录选择…"浏览键，不存在
        # 旧注释担心的误认问题；省下整行高度，页面更紧凑
        self._check_btn = QPushButton("检测", row)
        self._check_btn.setToolTip(
            "判定你填的目录现在处于哪种连接状态，"
            "并按状态给出对应的操作步骤与命令。")
        self._check_btn.clicked.connect(self._on_check)
        h.addWidget(self._check_btn)
        v.addWidget(row)


        self._state_label = QLabel("", self)
        self._state_label.setWordWrap(True)
        v.addWidget(self._state_label)

        self._detail_label = QLabel("", self)
        self._detail_label.setWordWrap(True)
        # 解析出的目标路径要能选中复制，方便和自己的目录核对
        self._detail_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self._detail_label)

        # 两个命令块：标题、内容、显隐全部由 _refresh 按状态分派
        self._build_box, self._build_title, self._build_edit = \
            self._make_cmd_block()
        self._remove_box, self._remove_title, self._remove_edit = \
            self._make_cmd_block()

        # 命令块摆放次序：拆除块在上、建链块在下。
        # 带"第1步/第2步"编号的状态里，拆除块都是第 1 步（先拆错的才能
        # 建新的），必须先摆在上面；建链块固定在上等于让人先做第 2 步
        # ——mklink 会因"目标已存在"直接报错
        self._cmd_layout = v   # 存一份引用：_refresh 里个别状态还要微调次序

        # 步骤说明摆在命令块上方：像"需先搬运再建链"这种状态，执行顺序是
        # 搬运（说明）→ 移除 → 建链，说明压在命令后面时用户先看到两条
        # 命令、滚到底才明白第 1 步是什么——阅读顺序必须等于执行顺序
        self._steps_label = QLabel("", self)
        self._steps_label.setWordWrap(True)
        self._steps_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self._steps_label)
        v.addWidget(self._remove_box)
        v.addWidget(self._build_box)
        # 收尾弹簧（关键）：内容不足一屏时，多余高度全部归它，
        # 上面的内容保持紧凑贴顶——不给的话多余高度会摊给每个
        # 标签，而标签文字默认垂直居中，页面上就出现大片空档
        # （实测：标题到"当前游戏"之间空出一百多像素）。内容
        # 超过一屏时弹簧自动归零，纵向滚动照常，互不影响。
        # _refresh 里挪命令块次序的代码以"步骤标签"当锚点，
        # 弹簧挂在最末尾，不受挪动影响，那边不用改。
        v.addStretch(1)

    def _make_cmd_block(self) -> tuple[QWidget, QLabel, QPlainTextEdit]:
        """造一个命令块（标题 + 命令文本 + 复制按钮），初始隐藏。
        复制按钮闭包引用文本框本体，点击时取当前内容，无需中间变量。"""
        box = QWidget(self)
        bv = QVBoxLayout(box)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        head = QHBoxLayout()
        t = QLabel("", box)
        t.setStyleSheet("font-weight: 600;")
        btn = QPushButton("复制", box)
        btn.setToolTip("复制这条命令，粘贴到 cmd 窗口里回车执行。")
        btn.setFixedWidth(64)
        head.addWidget(t, 1)
        head.addWidget(btn)
        bv.addLayout(head)
        edit = QPlainTextEdit(box)
        edit.setReadOnly(True)
        edit.setFixedHeight(56)
        bv.addWidget(edit)
        btn.clicked.connect(lambda _=False, e=edit: self._copy(e.toPlainText()))
        box.setVisible(False)
        return box, t, edit

    # ---------- 对外（MainWindow 调用）----------
    def set_game(self, game: Game | None) -> None:
        """切档案：重推实体路径、重填输入框、清掉上一份判定结论。
        换了档案还挂着上一份"已连接"绿勾是会出事的，必须清。"""
        self._game = game
        self._show_idle()
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._target_label.setText("下载目录（联接应指向的实体）：（未选择档案）")
            self._target = ""
            self._mismatch_note.hide()
            self._input.clear()
            self._check_btn.setEnabled(False)
            return
        self._check_btn.setEnabled(True)
        self._game_label.setText(f"当前游戏：{game.name}（{game.app_id}）")
        self._target = self._derive_target(game)
        self._target_label.setText(
            "下载目录（联接应指向的实体）：\n" + (self._target or "（推导不出，档案里也没有记录）"))
        # 历史手填提示：档案记录值与现推导值不同时说一句
        stored = str(game.download_dir or "").strip().strip('"').strip()
        if self._target and stored and \
                stored.casefold() != self._target.casefold():
            self._mismatch_note.setText(
                f"注：档案里记录的下载目录是 {stored}，与上面按 steamcmd "
                "当前位置推导的值不同——本页以推导值为准。")
            self._mismatch_note.show()
        else:
            self._mismatch_note.hide()
        # 预填：档案里存过游戏侧目录就直接进框（页面化红利，见文件头）。
        # setText 是程序性赋值，不触发 textEdited，不会误清状态
        prefilled = str(game.game_mod_dir or "").strip().strip('"').strip()
        self._input.setText(prefilled)

    def _derive_target(self, game: Game) -> str:
        """联接应指向的实体：优先按 steamcmd 当前位置现推导（core 的
        权威公式），推导不出退回档案记录值。与 gameSwitcher 同一款。"""
        steamcmd_exe = self._settings.get("steamcmd_path") \
            if self._settings else ""
        derived = steamPaths.workshop_content_dir(steamcmd_exe, game.app_id)
        return str(derived or game.download_dir or "") \
            .strip().strip('"').strip()

    # ---------- 状态与刷新 ----------
    def _show_idle(self) -> None:
        """初始 / 输入被改动后的待判定状态：清空结论、藏起命令。
        改了路径还挂着上一条的绿勾是会出事的，必须立刻清掉。"""
        self._state_label.setText("在上方填入游戏 mod 目录后点【检测】。")
        self._state_label.setStyleSheet("color: gray;")
        self._detail_label.setText("")
        self._steps_label.setText("")
        self._build_box.setVisible(False)
        self._remove_box.setVisible(False)

    def _on_edited(self) -> None:
        self._show_idle()
        self._state_label.setText("内容已改动，点【检测】重新判定。")

    def _on_check(self) -> None:
        if not self._input.text().strip().strip('"').strip():
            self._show_idle()
            return
        # 第二个参数用 self._target（现推导的实体），不是档案记录值——
        # 两者可能不一致（见文件头说明）
        report = steamPaths.junction_state(self._input.text(), self._target)
        self._refresh(report)

    def _refresh(self, report: steamPaths.JunctionReport) -> None:
        """按判定结果分派：结论颜色、说明行、命令块、步骤文案。
        状态 → 行动的对照表就铺在这一个方法里，对照 JunctionReport
        的 docstring 看即可。"""
        color, text = _STATE_LOOKS.get(
            report.state, ("gray", f"未知状态：{report.state}"))
        self._state_label.setText(text)
        self._state_label.setStyleSheet(f"color: {color}; font-weight: 600;")

        link = self._input.text().strip().strip('"').strip()
        target = self._target
        detail = ""
        build_title = ""
        build_cmd = ""
        remove_title = ""
        remove_cmd = ""
        show_build = False
        show_remove = False
        steps = ""

        if report.state == "linked":
            detail = f"解析目标：{report.detail}"
            show_remove = True
            remove_title = "拆除命令（如需断开连接才使用；在 cmd 里执行）"
            remove_cmd = f'rmdir "{link}"'
            steps = ("当前无需任何操作。若以后想断开：在 cmd 里执行拆除命令——"
                     "rmdir 只删除联接本身，不会动下载目录里的内容。")
        elif report.state == "linked_reverse":
            # 反向拓扑：联接在下载目录侧，拆除时动的也是那一侧
            detail = f"联接位于（下载目录侧）：{target}"
            show_remove = True
            remove_title = "拆除命令（如需断开连接才使用；在 cmd 里执行）"
            remove_cmd = f'rmdir "{target}"'
            steps = ("当前无需任何操作：steamcmd 经联接写入，内容实际存放在"
                     "游戏 mod 目录，游戏直接读真实目录。若以后想断开：在 cmd "
                     "里执行拆除命令——rmdir 只删除联接本身，不动游戏目录里的"
                     "内容；拆掉后 steamcmd 下次下载会在原位重建真实下载目录。")
        elif report.state == "linked_reverse_missing":
            # 反向接了一半：只差游戏目录本身。绝不能建议 mklink 游戏→下载
            # （下载目录已是联接，再套一层就是环）——接通方式是补建目录
            detail = f"联接位于（下载目录侧）：{target}"
            show_build = True
            show_remove = True
            build_title = "接通命令：创建游戏 mod 目录（在 cmd 里执行）"
            build_cmd = f'mkdir "{link}"'
            remove_title = "或：拆除下载目录侧的联接（改用其他拓扑前先拆）"
            remove_cmd = f'rmdir "{target}"'
            steps = ("下载目录侧的联接已经指对了位置，只差游戏 mod 目录本身："
                     "在 cmd 里执行接通命令，建一个普通文件夹即可接上"
                     "（原内容若已被删除则无法找回）。也可以先拆除联接，"
                     "再按正向拓扑重新规划。")
        elif report.state == "wrong_target":
            detail = f"解析目标：{report.detail}"
            show_build = True
            show_remove = True
            build_title = "第 2 步：建链命令（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            remove_title = "第 1 步：拆除现有联接（在 cmd 里执行）"
            remove_cmd = f'rmdir "{link}"'
            steps = ("① 先核对上方的解析目标，确认它确实不是本档案的下载目录；"
                     "② 先拆除再建链。建议 steamcmd 空闲时操作。")
        elif report.state == "wrong_target_reverse":
            # 反向指错：联接在下载目录侧，重建也在那一侧原地指对
            detail = (f"联接位于（下载目录侧）：{target}\n"
                      f"解析目标：{report.detail}")
            show_build = True
            show_remove = True
            build_title = "第 2 步：原地重建反向联接（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{target}" "{link}"'
            remove_title = "第 1 步：拆除指错的联接（在 cmd 里执行）"
            remove_cmd = f'rmdir "{target}"'
            steps = ("steamcmd 的内容正写进「解析目标」那个位置，游戏 mod 目录"
                     "接不到。两条路二选一：① 保留反向拓扑——依次执行下面两条"
                     "命令，把下载目录侧的联接原地指对（数据无需搬运；若游戏 "
                     "mod 目录当前还不存在，第 2 步执行完再创建它即可）；"
                     "② 改用常规拓扑——拆除联接后让 steamcmd 重新生成真实"
                     "下载目录，再按正向建链（需先搬运内容）。"
                     "建议 steamcmd 空闲时操作。")
        elif report.state == "empty_dir":
            show_build = True
            show_remove = True
            build_title = "第 2 步：建链命令（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            remove_title = "第 1 步：移除空目录（在 cmd 里执行）"
            remove_cmd = f'rmdir "{link}"'
            steps = ("① 先执行移除命令——rmdir 只删得动空目录，里面有东西会"
                     "报错拒绝（这是保护）；② 再执行建链命令。"
                     "建议 steamcmd 空闲时操作。")
        elif report.state == "real_dir":
            show_build = True
            show_remove = True
            # 三步重编号：两块命令都带步号，第 1 步搬运只活在说明里——
            # 照页面读必须先看到"第 1 步"
            build_title = "第 3 步：建链命令（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            remove_title = "第 2 步：移除已搬空的目录（在 cmd 里执行）"
            remove_cmd = f'rmdir "{link}"'
            # 步骤里直接写明第 1 步搬到哪（路径可选中复制）；
            # rmdir 报错是保护这点一并说清。
            # ★ V2 口径：收编不再走"右键确认（手动）"——统一指路入账中心
            steps = (
                "第 1 步：把这个目录里的内容整体移动到下载目录：\n"
                f"{target}\n"
                "（资源管理器剪切 → 粘贴即可）。有工坊编号的 mod 搬过去"
                "账本照常认识；没有编号的（手动放进去的）会出现在核验页"
                "「账本外」清单里，属正常——到【入账中心】点【扫描游戏"
                "目录】即可收编。\n"
                "第 2 步：移空后执行移除命令。rmdir 只删得动空目录——"
                "没搬完它会报错拒绝，这是保护，搬完再执行即可。\n"
                "第 3 步：执行建链命令。建议 steamcmd 空闲时操作。")
        elif report.state == "missing":
            show_build = True
            build_title = "建链命令（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            steps = ("① 确认上方路径的上级文件夹已存在（路径拼错是建链失败"
                     "最常见的原因）；② 在 cmd 里执行建链命令。"
                     "建议 steamcmd 空闲时操作。")
        elif report.state == "file":
            steps = ("确认这个文件是什么——若是无关文件，删除或改名后再点"
                     "【检测】。本工具不会替你删除任何东西。")
        elif report.state == "no_target":
            steps = "先解决档案的下载目录问题，再回来做连接。"

        self._detail_label.setText(detail)
        self._build_title.setText(build_title)
        self._build_edit.setPlainText(build_cmd)
        self._remove_title.setText(remove_title)
        self._remove_edit.setPlainText(remove_cmd)
        self._build_box.setVisible(show_build)
        self._remove_box.setVisible(show_remove)
        self._steps_label.setText(steps)

        # 命令块次序微调：默认拆除块在上（构造时已按此摆放），唯一例外
        # 是反向"接了一半"——那个状态的推荐动作是接通（mkdir 补建目录），
        # 拆除块只是备选（标题以"或："开头），接通块摆上面才顺着读；
        # 要是把拆除命令摆在前面，用户先照着拆掉的是一条"本来指对了"
        # 的联接，白折腾。其余状态一律拆除块在上。
        # 次序已经对了就不动，反复点【检测】不会来回跳。
        top, bottom = (
            (self._build_box, self._remove_box)
            if report.state == "linked_reverse_missing"
            else (self._remove_box, self._build_box))
        lay = self._cmd_layout
        if lay.indexOf(top) > lay.indexOf(bottom):
            lay.removeWidget(top)
            lay.removeWidget(bottom)
            # 重排时插在步骤说明后面一格——锚点不带 +1 会把命令块
            # 插回步骤前面
            i = lay.indexOf(self._steps_label)
            lay.insertWidget(i + 1, top)
            lay.insertWidget(i + 2, bottom)

        # ---- 检测通过 → 写档案（V1 靠信号交主窗口写，页面自己持 repo 直接写）----
        # 只有两种"全通"状态算通过：正向 linked（游戏侧联接指对）、
        # 反向 linked_reverse（下载侧联接指对、游戏侧实体在位）。
        # 半通态不写——游戏此刻还读不到 mod，把目录记进账等于把
        # "没接通"记成"已接通"。要写的都是 link：用户填的、且刚被判
        # "已连接"的游戏侧目录。同值重复检测由 _persist 去重。
        if report.state in ("linked", "linked_reverse"):
            self._persist(link)

    def _persist(self, link: str) -> None:
        """检测通过后把"游戏实际读取 mod 的目录"写进档案 game_mod_dir
        字段（走 update_game 正门）。同值不重复写库、不刷日志。"""
        if self._game is None:
            return
        current = str(self._game.game_mod_dir or "").strip().strip('"').strip()
        if current == link:
            return   # 去重：重复检测同一路径，安静收工
        self._repo.update_game(self._game.app_id, game_mod_dir=link)
        self._game = self._repo.get_game(self._game.app_id)   # 内存同步
        self._log.ok(
            f"联接检测通过：游戏读取 mod 的目录已记入档案：{link}")
        # 通知主窗口同步档案切换器：它手里的档案信息还停在写库前，
        # 不同步的话编辑档案会一直显示旧值、直到重启
        self.game_dir_written.emit(self._game.app_id)


    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)
