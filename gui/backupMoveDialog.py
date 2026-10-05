"""备份搬家指引
"""
"""
gui/backupMoveDialog.py · 把备份整体搬到新位置的命令生成器
（全程只读，本窗口不做任何盘上写操作）。

解决的实际问题：备份默认放在 steamcmd 所在的盘。那个盘快满时
想把备份挪去别的盘，直接剪切粘贴会弄断账——档案里记的备份目录
还是旧位置，备份页的「盘上」列会集体失联。

思路：不动记录、不改档案字段，而是在旧位置留一个"目录联接"
（junction，可理解为文件系统的路标）指到新位置。这样：
- 档案里记的旧路径照样可达（透过联接落到新盘），所有备份记录
  原样对上；
- 备份引擎继续往旧路径写，实际落在新盘，无感知；
- 联接的目标在另一块硬盘上也成立（搬家场景正是跨盘）。

与另外两个入口的分工（三个入口都能碰"备份位置"，管的事不同）：
- 本窗口【备份搬家】：备份都还在，只是想换位置——物理搬运 +
  建联接，档案字段一个不动；
- 【重定位备份目录】：盘上已经找不到备份（记录失联），指认新
  位置，只改档案字段、不动文件；
- 【编辑档案】：建档前预设位置、日常修正。

所有命令由用户自己在 cmd 里执行（复制按钮 + "已复制 ✓"反馈 +
日志回执）。主流程五步：① robocopy 复制（失败重试 2 次、16 线程，
与备份引擎同口径）→ ② ren 旧目录改名让路（改名不是删除——出错
改回名字就回到原样）→ ③ mklink /J 原地建联接指到新位置（不需要
管理员权限）→ ④ 回本工具验收（「盘上」列 + 做一次小备份）→
⑤ 用几天确认无误后，用户自行删除改名保留的旧目录——这步的
删除命令故意不给复制按钮：百 GB 级的删除，手打一遍等于多一道
闸门。

判定逻辑就地实现、不复用 steamPaths 的联接判定：那里判的是
"游戏目录 ↔ 下载目录"一对路径的关系，这里只看旧位置单侧的形态
（普通目录/空目录/联接/文件/不存在），问题不同，硬套反而绕。
"""

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from core import steamPaths
from core.models import Game
from gui.consolePanel import LogBus

# appSettings 里 steamcmd 程序路径的键名（与 backupPage._make_manager 用的
# 是同一个；设置页改键名时这里要跟着动）
_KEY_STEAMCMD = "steamcmd_path"

# robocopy 的参数，与备份引擎同口径（R5）：
# /E 连空子目录一起复制
# /XJ 不钻进联接里复制（防联接环把复制变成无底洞）
# /R:2 /W:5 失败重试 2 次、每次等 5 秒（robocopy 默认重试一百万次，会卡死人）
# /MT:16 16 线程并行复制
# 结束码 0~7 都算成功、8 起才是失败，步骤文案里以"Failed 为 0"表述
_ROBOCOPY_ARGS = "/E /XJ /R:2 /W:5 /MT:16"

# 旧位置形态 → (颜色, 结论一句话)。命令与步骤在 _refresh 里按形态分派
_STATE_LOOKS = {
    "real_dir": ("#f5a623", "可搬家：旧位置是真实目录，里面有备份内容"),
    "empty_dir": ("#46a758", "可接通：旧位置是空目录，无需复制，两步就能接上"),
    "linked": ("#46a758", "旧位置已经是联接（之前搬家留下的）——本次是「换目标」"),
    "missing": ("#e5484d", "旧位置当前不存在——没有可搬的东西"),
    "file": ("#e5484d", "旧位置被一个普通文件占用，无法操作"),
}


class BackupMoveDialog(QDialog):
    """备份搬家指引。构造时确定档案与设置，生命周期内不变。"""

    def __init__(self, game: Game, settings, parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._game = game
        self._settings = settings
        # T15 批 3：可选日志通道。不传时自建一条（照样写文件旁路，
        # 只是控制台看不见）——与备份重定位对话框同一套约定
        self._log = log or LogBus()
        self.setWindowTitle(f"备份搬家 — {game.name}（{game.app_id}）")
        self.setMinimumWidth(700)
        self._build_ui()
        self._show_idle()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        # T19⑧ 的教训：说明/命令/步骤加起来高过窗口时会被裁掉，
        # 内容装进滚动区；Close 留在滚动区外，任何高度都可见
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        tip = QLabel(
            "用途：备份所在的盘快满了（或想整理位置），把备份整体搬到新位置。\n"
            "做法：下面的命令由你在 cmd 里依次执行——复制到新位置、旧位置改名让路、"
            "原地建「目录联接」指回新位置。旧路径从此透过联接落到新盘：工具里记录的"
            "备份路径一个都不用改，所有备份记录照常对得上（联接跨硬盘也成立）。\n"
            "与【重定位备份目录】的分工：搬家 = 备份都在、只是想换位置；"
            "重定位 = 盘上找不到备份、记录失联后指认新位置。\n"
            "注意：新位置请选本地硬盘上的目录，不要选 OneDrive / 网盘同步目录。",
            self)
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray;")
        v.addWidget(tip)

        # 档案信息 + 预填说明（可选中复制，方便核对路径）
        stored = str(self._game.backup_dir or "").strip().strip('"').strip()
        info_lines = [f"当前游戏：{self._game.name}（{self._game.app_id}）"]
        if stored:
            info_lines.append(f"它记录的备份目录：{stored}")
        info = QLabel("\n".join(info_lines), self)
        info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(info)

        hint = QLabel(
            "旧位置默认预填所有游戏共用的备份根目录——一次搬家，所有游戏的备份"
            "一起过去；只想搬当前游戏的备份，就把旧位置改成它自己的备份目录"
            "（上面那行）。",
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        v.addWidget(hint)

        # 预填"旧位置"：优先用档案记录的备份目录的上一级（mod_backups 根）；
        # 档案还没记过备份目录时，按当前 steamcmd 位置推导（同样取上一级）。
        # 都拿不到就留空，让用户自己填
        guess = ""
        if stored:
            guess = str(Path(stored).parent)
        else:
            default_root = steamPaths.backup_root_default(
                self._settings.get(_KEY_STEAMCMD), self._game.app_id)
            if default_root:
                guess = str(Path(default_root).parent)

        # 两个路径行：输入框右侧按桌面惯例放"目录选择…"，
        # 检测按钮单独放下一行（与连接指引同一约定）。
        # 先建输入框再传参——海象写法（x := y）左边只能是裸名字，
        # 不能写 self.属性 := 值
        self._old_input = QLineEdit(body)
        self._new_input = QLineEdit(body)
        v.addWidget(self._make_path_row(
            "旧位置（备份现在在哪）：", guess,
            "选择备份现在的位置", self._old_input))
        v.addWidget(self._make_path_row(
            "新位置（想搬去哪）：", "", "选择新位置", self._new_input))

        btn_row = QWidget(body)
        bh = QHBoxLayout(btn_row)
        bh.setContentsMargins(0, 0, 0, 0)
        self._check_btn = QPushButton("检测并生成命令", btn_row)
        # T15 批 3：补 tooltip（决策 22①）
        self._check_btn.setToolTip(
            "按新旧两个位置的现状判定该走哪种搬法，"
            "把要执行的命令一条条列在下方（本对话框只出命令，不代执行）")
        self._check_btn.clicked.connect(self._on_check)
        bh.addWidget(self._check_btn)
        bh.addStretch(1)
        v.addWidget(btn_row)

        self._state_label = QLabel("", body)
        self._state_label.setWordWrap(True)
        v.addWidget(self._state_label)
        self._detail_label = QLabel("", body)
        self._detail_label.setWordWrap(True)
        v.addWidget(self._detail_label)

        # 三个命令块：主场景用满三条（复制/改名/建链），其他形态用几条藏几条
        self._blocks: list[tuple[QWidget, QLabel, QPlainTextEdit]] = []
        for _ in range(3):
            box, title, edit = self._make_cmd_block(body)
            self._blocks.append((box, title, edit))
            v.addWidget(box)

        self._steps_label = QLabel("", body)
        self._steps_label.setWordWrap(True)
        v.addWidget(self._steps_label)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _make_path_row(self, label_text: str, preset: str,
                       dialog_title: str, line: QLineEdit) -> QWidget:
        """造一行"标签 + 路径输入框 + 目录选择按钮"。"""
        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(label_text, row))
        line.setPlaceholderText(r"如 E:\mod_backups")
        if preset:
            line.setText(preset)
        line.textEdited.connect(self._on_edited)
        btn = QPushButton("目录选择…", row)
        # T15 批 3：补 tooltip（决策 22①）
        btn.setToolTip("打开系统目录选择窗口")
        btn.clicked.connect(
            lambda _=False, l=line, t=dialog_title: self._pick(l, t))
        h.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        h.addWidget(line, 1)
        return row

    def _make_cmd_block(self, parent: QWidget
                        ) -> tuple[QWidget, QLabel, QPlainTextEdit]:
        """造一个命令块（标题 + 命令文本 + 复制按钮），初始隐藏。

        复制按钮用默认参数把文本框本体绑进闭包，点击时取当前内容；
        T15 批 3 起同时带上按钮与标题——复制后按钮短暂显示"已复制 ✓"、
        控制台记一行日志（决策 22③：用户动作要有反馈）。
        """
        box = QWidget(parent)
        w = QVBoxLayout(box)
        w.setContentsMargins(0, 0, 0, 0)
        w.setSpacing(4)
        head = QHBoxLayout()
        title = QLabel("", box)
        title.setStyleSheet("font-weight: 600;")
        btn = QPushButton("复制", box)
        btn.setFixedWidth(64)
        # T15 批 3：补 tooltip（决策 22①）
        btn.setToolTip("复制这条命令到剪贴板，粘贴到 cmd 里执行")
        head.addWidget(title, 1)
        head.addWidget(btn)
        w.addLayout(head)
        edit = QPlainTextEdit(box)
        edit.setReadOnly(True)
        edit.setFixedHeight(56)
        w.addWidget(edit)
        btn.clicked.connect(
            lambda _=False, e=edit, b=btn, t=title:
            self._copy(e.toPlainText(), b, t))
        box.setVisible(False)
        return box, title, edit

    # ---------- 待判定 / 输入变动 ----------
    def _show_idle(self) -> None:
        """初始 / 输入被改动后的待判定状态：清空结论、藏起命令。

        改了路径还挂着上一条的结论是会出事的，必须立刻清掉。
        """
        self._state_label.setText("填好新旧两个位置后点【检测并生成命令】。")
        self._state_label.setStyleSheet("color: gray;")
        self._detail_label.setText("")
        self._steps_label.setText("")
        self._put_commands([])

    def _on_edited(self) -> None:
        self._show_idle()

    def _pick(self, line: QLineEdit, title: str) -> None:
        start = line.text().strip() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, title, start)
        if d:
            line.setText(d)
            # 程序设值不触发 textEdited，手动回到待判定状态
            self._show_idle()

    def _copy(self, text: str, btn=None, title_label=None) -> None:
        """复制命令文本到剪贴板 + 可见反馈。

        T15 批 3：按钮文字短暂变成"已复制 ✓"（1.5 秒后复原），
        控制台记一行日志。btn/title_label 是可选的（防御式：
        万一有调用点没带，复制本身照常生效）。
        """
        QApplication.clipboard().setText(text)
        if btn is not None:
            btn.setText("已复制 ✓")
            QTimer.singleShot(1500, lambda: btn.setText("复制"))
        if title_label is not None:
            step = title_label.text().split("（")[0]  # 去掉括号里的附注
            self._log.ok(f"已复制命令：{step}（粘贴到 cmd 执行）")
        else:
            self._log.ok("已复制命令到剪贴板（粘贴到 cmd 执行）")

    # ---------- 检测与分派 ----------
    def _on_check(self) -> None:
        old = self._old_input.text().strip().strip('"').strip()
        new = self._new_input.text().strip().strip('"').strip()
        # 两个框没填齐：说清"还差什么"，不出命令
        if not old or not new:
            self._show_idle()
            if not old and not new:
                msg = "把新旧两个位置都填上，再点【检测并生成命令】。"
            elif not old:
                msg = "还差旧位置（备份现在在哪里）。"
            else:
                msg = "还差新位置（想搬去哪里）。"
            self._state_label.setText(msg)
            return
        if old.casefold() == new.casefold():
            self._note("新旧位置是同一个地方，无需搬家。", "#e5484d")
            return
        # 嵌套拦截：新位置在旧位置里面（或反过来）时，复制会变成
        # "把目录复制进它自己的子目录"，永远复制不完，必须拦下。
        # 先 resolve 归一化再 casefold 比较（Windows 路径大小写不敏感，R14）
        o = str(Path(old).resolve()).rstrip("\\/").casefold()
        n = str(Path(new).resolve()).rstrip("\\/").casefold()
        if n.startswith(o + "\\") or o.startswith(n + "\\"):
            self._note(
                "新位置不能放在旧位置里面（或反过来）——"
                "两层套着的目录没法互相复制。", "#e5484d")
            return
        new_path = Path(new)
        if new_path.exists() and not new_path.is_dir():
            self._note("新位置被一个普通文件占用，换一个目录。", "#e5484d")
            return
        try:
            os.readlink(new)
        except OSError:
            pass  # 不是联接，正常
        else:
            self._note("新位置本身是个联接——请填它指向的最终实体目录。",
                       "#e5484d")
            return

        # ---- 新位置检查完毕，判旧位置的形态 ----
        # realpath 会穿透联接解析到最终实体，且不要求路径存在（与
        # steamPaths.junction_state 同一手法的单侧版）
        old_path = Path(old)
        resolved = old
        if old_path.is_dir():
            try:
                os.readlink(old)
            except OSError:
                # 普通目录：空还是非空，决定走"复制搬家"还是"两步接通"
                empty = next(old_path.iterdir(), None) is None
                state = "empty_dir" if empty else "real_dir"
            else:
                state = "linked"
                resolved = os.path.realpath(old)
                if resolved.casefold() == new.casefold():
                    self._note("新位置就是旧联接现在指的地方——"
                               "已经搬过了，无需操作。", "#46a758")
                    return
        elif old_path.exists():
            state = "file"
        else:
            state = "missing"
        self._refresh(state, old, new, resolved)

    def _refresh(self, state: str, old: str, new: str,
                 resolved: str) -> None:
        """按旧位置的形态分派：结论颜色、说明行、命令块、步骤文案。

        形态 → 行动的对照就铺在这一个方法里。
        """
        color, text = _STATE_LOOKS.get(state, ("gray", f"未知形态：{state}"))
        self._state_label.setText(text)
        self._state_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        detail = ""
        cmds: list[tuple[str, str]] = []
        steps = ""
        # 新位置已有内容时的通用提醒（复制是合并，不是清空）
        extra = ""
        np = Path(new)
        if np.is_dir() and next(np.iterdir(), None) is not None:
            extra = "\n（提示：新位置已有内容，复制会把两边合并，已有文件不会被删除。）"
        # ren 命令的第二个参数不能带路径，只能是要改成的名字——
        # 所以取旧路径的最后一段来拼
        tail = Path(old).name or "旧目录"
        if state == "real_dir":
            detail = f"旧位置：{old}"
            cmds = [
                ("第 1 步：复制到新位置（执行完看末尾 Failed 为 0 才继续）",
                 f'robocopy "{old}" "{new}" {_ROBOCOPY_ARGS}'),
                ("第 2 步：旧目录改名让路（改名不是删除，出错改回名字就恢复原样）",
                 f'ren "{old}" "{tail}_搬家旧目录"'),
                ("第 3 步：原地建联接指到新位置（与第 2 步连着执行）",
                 f'cmd /c mklink /J "{old}" "{new}"'),
            ]
            steps = (
                "① 打开 cmd（开始菜单搜「命令提示符」），按第 1→2→3 的顺序执行，"
                "顺序不能换；第 2 步到第 3 步之间旧路径会短暂消失，请连着做完。\n"
                "② 第 1 步结束后看输出末尾统计：Failed 为 0 才继续。不放心可以先只执行"
                "第 1 步，打开新位置核对文件都到了。\n"
                "③ 完成后回到本工具点【刷新】：「盘上」列应全部是 ✓；再做一次小备份，"
                "确认新文件真的落在新位置。\n"
                "④ 用几天、确认无误后，把第 2 步改名出来的旧文件夹删掉腾空间"
                "（资源管理器删除即可；或在 cmd 里手打 rmdir /S /Q 加上它的完整路径"
                "——这条删除命令故意不给复制按钮，大体量的删除多一道手写的闸门）。\n"
                "⑤ 第 2 步若报「已存在」：之前搬家留下的旧文件夹还占着这个名字，"
                "把命令里的名字换一个再执行。"
            )
        elif state == "empty_dir":
            detail = f"旧位置：{old}"
            cmds = [
                ("第 1 步：移除空目录（rmdir 只删得动空目录，里面有东西会报错，这是保护）",
                 f'rmdir "{old}"'),
                ("第 2 步：原地建联接指到新位置",
                 f'cmd /c mklink /J "{old}" "{new}"'),
            ]
            steps = (
                "旧位置是空目录，没有东西要复制：两步接通即可。\n"
                "完成后回本工具点【刷新】确认，之后的新备份会直接落在新位置。"
            )
        elif state == "linked":
            detail = f"旧联接现在指向：{resolved}"
            cmds = [
                ("第 1 步：把现有内容复制到新位置（执行完看末尾 Failed 为 0）",
                 f'robocopy "{resolved}" "{new}" {_ROBOCOPY_ARGS}'),
                ("第 2 步：摘掉旧联接（只摘链接本体；绝对不要加 /S）",
                 f'rmdir "{old}"'),
                ("第 3 步：原地重建联接指到新位置（与第 2 步连着执行）",
                 f'cmd /c mklink /J "{old}" "{new}"'),
            ]
            steps = (
                f"旧位置已经是一个联接（指向 {resolved}）——上次搬家留下的，"
                "本次是「换目标」：\n"
                "① 第 1 步把现有内容复制到新位置（Failed 为 0）。\n"
                "② 第 2、3 步连着执行，中间不要让本工具做任何备份/恢复操作："
                "摘旧联接（rmdir 不带 /S，只摘链接本体、绝不动里面的内容），"
                "再原地重建指到新位置。\n"
                "③ 回本工具点【刷新】验收；旧实体目录（上面指向的那个）"
                "确认无误后自行删除。"
            )
        elif state == "missing":
            steps = (
                "旧位置当前不存在，没有可搬的东西。\n"
                "如果备份页「盘上」列有失联记录（记录还指向这里），该用的是"
                "【重定位备份目录】——指认新位置、只改记录，不需要搬家。"
            )
        elif state == "file":
            steps = (
                "旧位置被一个普通文件占用（不是目录）。先弄清这个文件是什么、"
                "处理妥当后再点【检测并生成命令】。本对话框不会动任何东西。"
            )
        self._detail_label.setText(detail + extra)
        self._put_commands(cmds)
        self._steps_label.setText(steps)

    def _note(self, msg: str, color: str) -> None:
        """一行结论、不出命令（检查没通过 / 无需操作时用）。"""
        self._state_label.setText(msg)
        self._state_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._detail_label.setText("")
