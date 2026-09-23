"""连接指引对话框（T13）
"""
"""
给"游戏自己的 mod 目录"和"steamcmd 下载目录"牵线的操作指引。

背景（决策 21④）：部分游戏只从自己的目录读 mod（如 CK3 读
Documents 下的 mod 文件夹），而 steamcmd 下载的内容在
steamapps\\workshop\\content\\<appid>——两边对不上，游戏就看不见
下载的 mod。标准解法是建一个目录联接（junction）。两种拓扑都成立，
本对话框都认识（判定在 core/steamPaths.junction_state，本文件零判断）：

    正向：游戏 mod 目录（联接） --mklink /J--> 下载目录（实体）
    反向：下载目录（联接）     --mklink /J--> 游戏 mod 目录（实体）

反向拓扑不是纸上谈兵：实测用户环境就是"steamcmd 侧 content\\<appid>
是联接、游戏侧是真实目录"——steamcmd 写入经联接落到游戏目录，
游戏直接读真实目录。判定函数 v1 只认正向，把这种环境误报成
"需搬运再建链"（照做等于对着同一份数据自己搬自己），v2 起双向都认。

本对话框只做三件事：
1. 实况判定：把游戏 mod 目录的当前路径填进来，判定它处于哪种状态
   （已连接 / 接了一半 / 指错位置 / 空目录 / 有内容的真实目录 /
   不存在 / 被文件占用）
2. 产出命令：按状态生成 mklink /J（建链）、rmdir（拆链/移除）、
   mkdir（接通悬空反向联接）命令，一键复制。命令永远由用户自己在
   cmd 里执行——本对话框全程只读，不做任何盘上写操作（R4 天然满足）
3. 步骤说明：按状态给出对应的操作顺序

rmdir 的安全性（已写进界面文案）：rmdir 对联接只摘链接本体、不动
实体内容；对非空的真实目录会直接报错拒绝——两条都是保护。

联接应指向的"实体"由调用方传入：游戏切换器按 steamcmd 当前位置
现推导（决策 21 的权威公式），推导不出才退回档案里记录的下载目录。
历史上手填过下载目录的档案可能记录的是游戏侧路径，现推导让本指引
与这类历史数据解耦；两者不一致时界面提示一句，但不擅自改档案记录。

界面约定：路径输入框右侧不放功能按钮——那个位置按桌面惯例是
"目录选择…"，检测按钮单独放在输入框下方一行（用户反馈）。

联接位置每次由用户现填、不持久化：档案表没有这个字段，避免为它
动 schema；真常用再加字段（见记事本台账）。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,

)

from core import steamPaths
from core.models import Game

# 状态 → (颜色, 结论一句话)。命令显隐与步骤文案在 _refresh 里按状态分派
_STATE_LOOKS = {
    "linked": ("#46a758", "✓ 已连接：游戏 mod 目录是联接，且正确指向下载目录"),
    "linked_reverse": ("#46a758",
                       "✓ 已连接（反向拓扑）：下载目录本身是联接，指向游戏 mod 目录"
                       "——无需任何操作"),
    "linked_reverse_missing": ("#f5a623",
                               "接了一半：下载目录是联接、也指对了位置，"
                               "但游戏 mod 目录当前不存在（steamcmd 写入会失败）"),
    "wrong_target": ("#e5484d",
                     "✗ 指错了地方：游戏 mod 目录是联接，但没有指向本档案的下载目录"),
    "wrong_target_reverse": ("#e5484d",
                             "✗ 下载目录本身是个联接，但指向了别处——"
                             "steamcmd 的内容写不进游戏 mod 目录"),
    "empty_dir": ("#f5a623", "未连接：位置存在但是个空目录（建链前需先移除它）"),
    "real_dir": ("#f5a623", "未连接：这是真实目录且里面有内容（需先搬运再建链）"),
    "missing": ("#f5a623", "未连接：该位置还不存在（可直接建链）"),
    "file": ("#e5484d", "✗ 该位置被一个普通文件占用，无法建联接"),
    "no_target": ("#e5484d",
                  "✗ 推导不出下载目录（steamcmd 未配置），档案里也没有记录——"
                  "先到设置页填 steamcmd 程序路径，或重新建档"),
}


class LinkGuideDialog(QDialog):
    """单个档案的连接指引。构造时确定档案与目标实体，生命周期内不变。"""

    def __init__(self, game: Game, parent: QWidget | None = None,
                 *, target_dir: str | None = None) -> None:
        super().__init__(parent)
        self._game = game
        # 联接应指向的实体目录：优先用调用方按 steamcmd 现推导的值，
        # 没有再退回档案记录值。统一去引号/空白，与 junction_state 同口径
        self._target = str(target_dir or game.download_dir or "") \
            .strip().strip('"').strip()
        self.setWindowTitle(f"连接指引 — {game.name}（{game.app_id}）")
        self.setMinimumWidth(660)
        self._build_ui()
        self._show_idle()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # T19⑧：内容装进滚动区——提示/步骤/命令块加起来高过小窗时会被裁掉
        # （实测步骤文案显示不全）；Close 按钮留在滚动区外，任何高度都可见
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

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

        if self._target:
            self._target_label = QLabel(
                "下载目录（联接应指向的实体）：\n" + self._target, self)
        else:
            self._target_label = QLabel(
                "下载目录（联接应指向的实体）：（档案没有下载目录）", self)
        self._target_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self._target_label)

        # 档案记录值与现推导值不一致时的提示（历史手填数据会走到这里）。
        # 只提示不改档案——修正通道留给将来的档案编辑界面
        stored = str(self._game.download_dir or "").strip().strip('"').strip()
        if self._target and stored and \
                stored.casefold() != self._target.casefold():
            note = QLabel(
                f"注：档案里记录的下载目录是 {stored}，与上面按 steamcmd "
                "当前位置推导的值不同——本指引以推导值为准。", self)
            note.setWordWrap(True)
            note.setStyleSheet("color: gray;")
            v.addWidget(note)

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
        v.addWidget(row)

        # 检测按钮单独一行放在输入框下方（用户反馈：输入框右侧那个位置
        # 按桌面惯例是"目录选择…"，放检测键会被误当成浏览目录按钮）
        btn_row = QWidget(self)
        bh = QHBoxLayout(btn_row)
        bh.setContentsMargins(0, 0, 0, 0)
        self._check_btn = QPushButton("检测", btn_row)
        self._check_btn.clicked.connect(self._on_check)
        bh.addWidget(self._check_btn)
        bh.addStretch(1)
        v.addWidget(btn_row)

        self._state_label = QLabel("", self)
        self._state_label.setWordWrap(True)
        v.addWidget(self._state_label)

        self._detail_label = QLabel("", self)
        self._detail_label.setWordWrap(True)
        v.addWidget(self._detail_label)

        # 两个命令块：标题、内容、显隐全部由 _refresh 按状态分派
        self._build_box, self._build_title, self._build_edit = \
            self._make_cmd_block()
        self._remove_box, self._remove_title, self._remove_edit = \
            self._make_cmd_block()
        v.addWidget(self._build_box)
        v.addWidget(self._remove_box)

        self._steps_label = QLabel("", self)
        self._steps_label.setWordWrap(True)
        v.addWidget(self._steps_label)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


    def _make_cmd_block(self) -> tuple[QWidget, QLabel, QPlainTextEdit]:
        """造一个命令块（标题 + 命令文本 + 复制按钮），初始隐藏。
        复制按钮闭包引用文本框本体，点击时取当前内容，无需中间变量。
        """
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        head = QHBoxLayout()
        title = QLabel("", box)
        title.setStyleSheet("font-weight: 600;")
        btn = QPushButton("复制", box)
        btn.setFixedWidth(64)
        head.addWidget(title, 1)
        head.addWidget(btn)
        v.addLayout(head)
        edit = QPlainTextEdit(box)
        edit.setReadOnly(True)
        edit.setFixedHeight(56)
        v.addWidget(edit)
        btn.clicked.connect(lambda _=False, e=edit: self._copy(e.toPlainText()))
        box.setVisible(False)
        return box, title, edit

    # ---------- 状态与刷新 ----------

    def _show_idle(self) -> None:
        """初始 / 输入被改动后的待判定状态：清空结论、藏起命令。
        改了路径还挂着上一条的绿勾是会出事的，必须立刻清掉。
        """
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
        状态 → 行动的对照表就铺在这一个方法里，对照
        JunctionReport 的 docstring 看即可。
        """
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
            build_title = "第 2 步：建链命令（在 cmd 里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            remove_title = "第 2 步：移除已搬空的目录（在 cmd 里执行）"
            remove_cmd = f'rmdir "{link}"'
            steps = ("① 把该目录里的内容整体移动到下载目录（资源管理器剪切"
                     "粘贴即可）；移动过去的非编号 mod 会出现在核验页"
                     "「盘有账无」清单里，属正常，按决策 24 手动确认即可收编；"
                     "② 移空后：先执行移除命令，再执行建链命令。"
                     "建议 steamcmd 空闲时操作。")
        elif report.state == "missing":
            show_build = True
            build_title = "建链命令（在 cmd 命令提示符里执行）"
            build_cmd = f'cmd /c mklink /J "{link}" "{target}"'
            steps = ("① 确认上方路径的上级文件夹已存在（路径拼错是建链失败"
                     "最常见的原因）；② 在 cmd 里执行建链命令。"
                     "建议 steamcmd 空闲时操作。")
        elif report.state == "file":
            steps = ("确认这个文件是什么——若是无关文件，删除或改名后再点"
                     "【检测】。本对话框不会替你删除任何东西。")
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

    def _copy(self, text: str) -> None:
        QApplication.clipboard().setText(text)
