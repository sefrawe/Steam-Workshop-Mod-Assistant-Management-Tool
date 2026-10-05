"""游戏档案编辑
"""
r"""gui/gameEditDialog.py · 编辑当前游戏档案的基本字段（T19⑪）。

做这个界面的直接动机（决策 27 连接指引期间暴露）：历史上手填过下载目录
的档案，可能把"游戏自己的 mod 目录"错记成了下载目录——而档案建好之后
一直没有任何应用内修正通道，只能动数据库。本界面补上这个口子，
顺带覆盖三件日常小事：改名、预设/修正备份目录、修正游戏 mod 目录。

下载目录是唯一不给自由编辑的字段（决策 21 的精神延伸）：它的正确值
由 steamcmd 程序位置唯一决定，自由编辑等于重新打开"记成游戏侧路径"
那个错误通道。所以这里只并排显示"档案记录值"和"按当前 steamcmd 位置
推导值"，不一致时给一个【改为推导值】按钮，一键回到标准口径；
推导不出（设置页没填 steamcmd）就只显示现状、不出按钮，绝不瞎猜。

字段 → 保存方式（决策 22⑥：影响扫描/命令/备份去向的才弹确认）：
- 名称、游戏 mod 目录：纯记录字段，直接保存，不弹窗
- 备份目录：影响之后的备份去向 → 弹窗说清影响面（已有备份记录按
  相对路径解析，文件没跟着搬「盘上」列会失联，届时用重定位/搬家）
- 下载目录改为推导值：影响本地扫描与下载命令 → 并入同一张确认弹窗

写库走仓库正门：所有改动合成一次 repo.update_game 调用（与重定位
对话框同一扇门，本文件零 SQL）。accept() 只在写库成功后发出，
调用方拿到 Accepted 即可放心刷新。

与备份位置相关入口的分工（决策 30，三个入口各管一段）：
- 本对话框：预设与常规修正档案字段（改的是"账"）
- 【备份搬家】：备份文件都在、想整体换盘（搬的是"物"，账不动）
- 【重定位备份目录】：记录失联后指认新位置（账物对不上时改账）
"""
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import steamPaths
from core.models import Game
from gui.logBus import LogBus
from gui.theme import font_px  # 字号单源（D25）


# 结论文字的颜色（与重定位对话框同一套配色）
_C_OK = "#46a758"
_C_WARN = "#f5a623"
_C_MUTED = "#8a8a8f"


def _norm(path: str) -> str:
    """路径比较的规整：Windows 不分大小写，分隔符与多余斜杠归一，
    两边都先去引号（资源管理器"复制文件地址"常带引号）。"""
    return os.path.normcase(
        os.path.normpath(str(path or "").strip().strip('"').strip()))


class GameEditDialog(QDialog):
    """游戏档案编辑：名称 / 游戏 mod 目录 / 备份目录自由编辑，
    下载目录只提供"改为推导值"。保存 = 一次 update_game 调用。"""

    def __init__(self, repo, game: Game, settings,
                 parent: QWidget | None = None,
                 *, log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game = game
        self._settings = settings
        self._log = log or LogBus()

        # 打开时刻的原始值，保存时用来判断"改没改"。路径先去引号，
        # 比较时用 _norm——大小写、末尾斜杠这类差异不算改动
        self._orig_name = (game.name or "").strip()
        self._orig_gm = str(getattr(game, "game_mod_dir", "") or "") \
            .strip().strip('"').strip()
        self._orig_bk = str(game.backup_dir or "") \
            .strip().strip('"').strip()

        # 下载目录的推导值：设置页填了 steamcmd 才有；None = 推导不出
        self._steamcmd_exe = (settings.get("steamcmd_path") if settings else "") or ""
        self._derived = steamPaths.workshop_content_dir(
            self._steamcmd_exe, game.app_id)

        self._dd_pending = False  # 用户点了【改为推导值】、等待保存

        self.setWindowTitle(f"编辑档案 — {game.name}（{game.app_id}）")
        self.setMinimumWidth(560)
        self._build_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        # 只读档案信息。created_at 用 getattr 兜底：models.py 本轮没核对，
        # 字段在就显示、不在就省略——纯展示信息，不值得为它冒险
        info_lines = [f"AppID：{self._game.app_id}"]
        created = getattr(self._game, "created_at", None)
        if created:
            info_lines.append(
                "创建时间："
                + time.strftime("%Y-%m-%d %H:%M", time.localtime(created)))
        info = QLabel("\n".join(info_lines), self)
        info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        info.setStyleSheet("color: gray;")
        v.addWidget(info)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        # ---- 名称：自由编辑，直接保存（纯记录字段） ----
        self._name_edit = QLineEdit(self._orig_name)
        self._name_edit.setPlaceholderText("显示在下拉框和各页标题里的名字")
        form.addRow("名称", self._name_edit)

        # ---- 游戏 mod 目录：自由编辑，直接保存（纯记录字段） ----
        # 这就是连接指引里要填的那个路径——游戏从这里读 mod
        self._gm_edit = QLineEdit(self._orig_gm)
        self._gm_edit.setPlaceholderText(
            r"游戏自己的 mod 文件夹，如 C:\Users\<用户名>\Documents\..."
            "（可留空）")
        form.addRow("游戏 mod 目录", self._wrap_with_browse(self._gm_edit))

        # ---- 备份目录：自由编辑，保存前弹影响确认 ----
        self._bk_edit = QLineEdit(self._orig_bk)
        self._bk_edit.setPlaceholderText("留空 = 每次按 steamcmd 位置自动推导")
        bk_field = self._wrap_with_browse(self._bk_edit)
        default_root = steamPaths.backup_root_default(
            self._steamcmd_exe, self._game.app_id)

        if default_root:
            bk_hint = f"（当前推导：{default_root}）"
        else:
            bk_hint = "（设置页没填 steamcmd，暂推导不出）"
        bk_desc = QLabel("留空 = 每次按 steamcmd 位置自动推导" + bk_hint,
                         bk_field)
        bk_desc.setWordWrap(True)
        bk_desc.setStyleSheet(f"color: gray; font-size: {font_px(11)}px;")
        bk_field.layout().addWidget(bk_desc)
        form.addRow("备份目录", bk_field)

        # ---- 下载目录：不开放编辑，只显示记录值与推导值的对比 ----
        dd_field = QWidget(self)
        dd_v = QVBoxLayout(dd_field)
        dd_v.setContentsMargins(0, 0, 0, 0)
        dd_v.setSpacing(2)
        self._dd_current = QLabel(self._game.download_dir or "（空）", dd_field)
        self._dd_current.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        dd_v.addWidget(self._dd_current)
        self._dd_state = QLabel("", dd_field)
        self._dd_state.setWordWrap(True)
        dd_v.addWidget(self._dd_state)
        # 按钮先创建、再挂说明与接线：先有对象才能往它身上挂东西
        # （顺序反了 = 对象还不存在就访问它，实测就是这个错）
        self._dd_btn = QPushButton("改为推导值", dd_field)
        self._dd_btn.setToolTip(
            "把下载目录改成按 steamcmd 当前位置推导的标准值"
            "（下载目录永远由 steamcmd 位置决定，不开放手填）。"
            "点了之后还要点下方【保存】才真正写入。")
        self._dd_btn.clicked.connect(self._toggle_dd)

        dd_h = QHBoxLayout()
        dd_h.setContentsMargins(0, 0, 0, 0)
        dd_h.addWidget(self._dd_btn)
        dd_h.addStretch(1)
        dd_v.addLayout(dd_h)
        dd_desc = QLabel(
            "由 steamcmd 位置唯一决定，这里不开放手填——"
            "历史错值正是手填造成的。推导不出时保留现值，绝不瞎猜。",
            dd_field)
        dd_desc.setWordWrap(True)
        dd_desc.setStyleSheet(f"color: gray; font-size: {font_px(11)}px;")
        dd_v.addWidget(dd_desc)
        form.addRow("下载目录", dd_field)

        v.addLayout(form)
        v.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)

        ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("保存")
        bb.setCenterButtons(True)  # 保存/取消整组居中（默认靠右）

        cancel = bb.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("取消")  # 没装 Qt 中文翻译时的兜底

        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        # 建档时 steamcmd 还没配置的档案，下载目录在账本里是空串——
        # 没有人会故意填空，这里直接替用户进入"改为推导值"的待保存
        # 状态：打开对话框就能看到将要写入的值，点【保存】一次落库；
        # 不点保存就什么都不发生（只动了内存，账本零改动）。
        # steamcmd 至今没配置（推导不出）时不进这个状态，走下面
        # _refresh_dd 的"推导不出"分支，维持空值并如实说明。
        if not (self._game.download_dir or "").strip() \
                and self._derived is not None:
            self._dd_pending = True

        self._refresh_dd()

    def _wrap_with_browse(self, edit: QLineEdit) -> QWidget:
        """造"输入框 + 目录选择按钮"的一行容器（设置页同款结构）。"""
        field = QWidget(self)
        v = QVBoxLayout(field)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        btn = QPushButton("目录选择…", field)
        btn.clicked.connect(lambda _=False: self._browse(edit))
        h.addWidget(btn)
        v.addLayout(h)
        return field

    def _browse(self, edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择目录", edit.text().strip())
        if d:
            edit.setText(d)

    # ---------- 下载目录行的显示 ----------
    def _refresh_dd(self) -> None:
        """下载目录行的状态显示，共四种：
        待保存改动(黄) / 推导不出(灰，不出按钮) / 一致(绿) / 不一致(黄+按钮)。

        主行永远显示"这个字段此刻的实效值"：待保存时就是将要写入的
        推导值（旧记录降级到黄字里交代一句），其余状态是账本现值。
        先显示"（空）"再在下面小字里给真值，会让人以为字段还是空的
        ——顺序反了，实测被用户点名。"""
        if self._dd_pending:
            # 待保存：主行直接亮出将要写入的推导值
            self._dd_current.setText(self._derived or "")
            old = str(self._game.download_dir or "").strip()
            origin = f"原记录：{old}" if old else "原记录：（空）"
            self._dd_state.setText(
                "上面这个推导值还没写进账本——点【保存】后生效"
                f"（{origin}）；不点保存则维持原状。")
            self._dd_state.setStyleSheet(f"color: {_C_WARN};")
            self._dd_btn.setText("不改了，保留原记录")
            self._dd_btn.setVisible(True)
        elif self._derived is None:
            self._dd_current.setText(self._game.download_dir or "（空）")
            self._dd_state.setText(
                "推导不出：设置页还没填 steamcmd 程序路径。保留档案现值。")
            self._dd_state.setStyleSheet(f"color: {_C_MUTED};")
            self._dd_btn.setVisible(False)
        elif _norm(self._derived) == _norm(self._game.download_dir):
            self._dd_current.setText(self._game.download_dir or "（空）")
            self._dd_state.setText("✓ 与按当前 steamcmd 位置推导的值一致")
            self._dd_state.setStyleSheet(f"color: {_C_OK};")
            self._dd_btn.setVisible(False)
        else:
            self._dd_current.setText(self._game.download_dir or "（空）")
            self._dd_state.setText(
                "与推导值不一致。按当前 steamcmd 位置推导应为：\n"
                f"{self._derived}\n"
                "（历史上手填过下载目录的档案常见这种情况；连接指引已经"
                "按推导值工作，改回后两边重新一致。）")
            self._dd_state.setStyleSheet(f"color: {_C_WARN};")
            self._dd_btn.setText("改为推导值")
            self._dd_btn.setVisible(True)

    def _toggle_dd(self) -> None:
        """点了【改为推导值】→ 标记待保存；再点一次 → 撤销标记。"""
        self._dd_pending = not self._dd_pending
        self._refresh_dd()

    # ---------- 保存 ----------

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "无法保存", "名称不能为空。")
            return

        # 收集改动：路径用 _norm 比较（大小写/斜杠差异不算改），
        # 写入库的值仍是用户输入的原样
        changes: dict[str, str] = {}
        if name != self._orig_name:
            changes["name"] = name
        gm = self._gm_edit.text().strip().strip('"').strip()
        if _norm(gm) != _norm(self._orig_gm):
            changes["game_mod_dir"] = gm
        bk = self._bk_edit.text().strip().strip('"').strip()
        if _norm(bk) != _norm(self._orig_bk):
            changes["backup_dir"] = bk
        if self._dd_pending:
            changes["download_dir"] = self._derived

        if not changes:
            QMessageBox.information(self, "编辑档案", "没有需要保存的改动。")
            return

        # 影响面确认（决策 22⑥）：动"备份去向"或"扫描/命令基准"才弹窗
        impact: list[str] = []
        if "download_dir" in changes:
            impact.append(
                f"下载目录将改为：\n{changes['download_dir']}\n"
                "影响：本地扫描与下载命令都按它定位；已记录的 mod 本地路径"
                "会暂时指向旧位置，重新扫描本地后自动回填。")
        if "backup_dir" in changes:
            impact.append(
                f"备份目录将改为：\n{changes['backup_dir']}\n"
                "影响：之后的新备份写到新位置；已有备份记录按相对路径解析，"
                "备份文件若没一起搬过去，「盘上」列会失联——届时用"
                "【重定位备份目录】指认，或用【备份搬家】把文件搬过去。")
        if impact:
            ret = QMessageBox.question(
                self, "确认保存",
                "以下修改会影响相关功能，确认保存？\n\n" + "\n\n".join(impact),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return

        # 仓库正门一次写入。包一层异常转译，与 backupPage._backup_database
        # 同款做法——正常情况永远走不到 except，走到了也给人话提示
        try:
            self._repo.update_game(self._game.app_id, **changes)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"写入数据库失败：{exc}")
            return

        self._log.ok(
            f"档案「{name}」（{self._game.app_id}）已更新："
            + "、".join(sorted(changes)))
        self.accept()
