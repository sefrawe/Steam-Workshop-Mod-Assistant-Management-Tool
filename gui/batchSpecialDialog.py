"""批量特别关注对话框
"""
"""
gui/batchSpecialDialog.py · T27(a)：贴一份清单，把库里的 mod 一次性标上"特别关注"。

典型场景：把旧脚本时代的"特殊 mod 清单"（ck3需删假中文的特殊mod.txt 这类，
每行一个工坊网址或纯数字编号）整段粘进文本框，先【解析预览】核对命中情况，
再【应用】——把"已在当前档案库、还没标特别关注"的条目一次标上。

三条防呆（为什么这么写）：
1. 只对"当前档案"动手。工坊网址里没有游戏 AppID，解析结果无法自证归属，
   所以对表范围锁死在弹出对话框那一刻的档案，顶部把档案名亮出来；
   属于其他档案的条目单独点名、一律跳过——不猜、不跨档案改数据。
2. 只"设"不"取消"。这张清单的语义是"这些要额外留意"，批量方向只有设；
   个别取消走 mod 库页操作菜单（T27(b)），对话框保持单一职责。
3. 解析走 core.urlParser 单源（决策 34d）。与网址批量导入页共用同一份
   识别规则（工坊网址 / 纯数字 / steamcmd 命令行都认），认不出的行
   原样列出给用户看——不猜、不纠正、不静默丢弃。

流程与数据先可见再动手（与档案删除前盘点同一哲学）：
解析+对表只分类不落库（classify_special_targets，pytest 直接测它）；
预览把五桶结果摆全；【应用】带确认弹窗、一个事务打包，要么全标要么不动。
"""
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.models import Game
from core.modRepository import ModRepository
from core.urlParser import parse_lines


@dataclass
class SpecialReport:
    """一次"解析 + 对表"的全部结论，五桶分开装：
    预览区照桶摆数字，【应用】只碰 to_set，其余桶一律不动。"""

    game_name: str = ""
    parsed_ids: list[int] = field(default_factory=list)      # 解析出的编号（去重、按出现顺序）
    invalid_lines: list[str] = field(default_factory=list)   # 认不出的原始行
    to_set: list[int] = field(default_factory=list)          # 已在库、待设特别关注
    already_special: list[int] = field(default_factory=list) # 已是特别关注（跳过）
    in_other_games: list[tuple[int, str]] = field(default_factory=list)  # (编号, 所属档案名)
    missing: list[int] = field(default_factory=list)         # 未收录（指路模块②，不代收录）


def classify_special_targets(repo: ModRepository, game: Game, text: str) -> SpecialReport:
    """解析文本并对当前档案对表分类。纯逻辑、零 Qt，对话框预览与 pytest 共用。

    本函数只读不写：分类归它，落库（set_special）是【应用】按钮的事——
    "看清楚"与"动手"分开，预览阶段对账本零副作用。
    """
    report = parse_lines(text.splitlines())
    rep = SpecialReport(game_name=game.name,
                        parsed_ids=report.mod_ids,
                        invalid_lines=report.invalid)

    if not rep.parsed_ids:
        return rep

    # 当前档案的全部 mod 一次取齐建索引：批量比对只走一趟 SQL，
    # 56 条清单不值得逐个 get_mod
    current = {m.mod_id: m for m in repo.list_mods(game.app_id)}
    # 全库范围再查一遍谁存在：与当前档案的差集 = 属于其他档案的条目，
    # 报告里要点名它们在哪个档案名下
    global_existing = repo.filter_existing_ids(rep.parsed_ids)

    for mid in rep.parsed_ids:
        mod = current.get(mid)
        if mod is not None:
            # 在当前档案：按是否已关注分桶（parse_lines 已去重，不会重复进桶）
            if mod.is_special:
                rep.already_special.append(mid)
            else:
                rep.to_set.append(mid)
        elif mid in global_existing:
            # 在库但属于别的档案：查明所属档案名给用户看，坚决不动
            other = repo.get_mod(mid)
            if other is not None:
                g = repo.get_game(other.game_id)
                name = (g.name if g is not None
                        else f"未知档案（game_id={other.game_id}）")
                rep.in_other_games.append((mid, name))
            else:
                rep.in_other_games.append((mid, "未知档案"))
        else:
            rep.missing.append(mid)
    return rep


class BatchSpecialDialog(QDialog):
    """贴清单 → 解析预览 → 应用 的三步模态对话框。

    模态（exec）：改账本的操作不该让界面别处同时插手；应用成功后
    accept() 关窗，调用方（mod 库页）读 self.report 写日志并刷新列表。
    """

    def __init__(self, repo: ModRepository, game: Game,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game = game
        self._report: SpecialReport | None = None  # 预览结果（应用按钮的依据）
        self.report: SpecialReport | None = None   # 应用成功后赋值，供调用方写日志
        self._build_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.setWindowTitle("批量特别关注")
        self.resize(560, 540)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        head = QLabel(
            f"对表目标：档案「{self._game.name}」（AppID {self._game.app_id}）。\n"
            "粘贴工坊网址 / 纯数字编号清单（每行一条，或空格分隔均可），"
            "先【解析预览】核对，再【应用】。只有「待设为特别关注」一桶会被写入。",
            self)
        head.setWordWrap(True)
        root.addWidget(head)

        self._edit = QPlainTextEdit(self)
        self._edit.setPlaceholderText(
            "例：\n"
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3173817085\n"
            "3219589892")
        root.addWidget(self._edit, 1)

        self._report_label = QLabel("", self)
        self._report_label.setWordWrap(True)
        self._report_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)  # 报告可选中复制走
        root.addWidget(self._report_label, 1)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._btn_preview = QPushButton("解析预览", btn_row)
        self._btn_preview.setToolTip("解析文本并对当前档案对表；只读操作，不改任何数据")
        self._btn_preview.clicked.connect(self._on_preview)
        h.addWidget(self._btn_preview)

        self._btn_apply = QPushButton("应用", btn_row)
        self._btn_apply.setToolTip("把「待设为特别关注」一桶写入账本；写前有确认弹窗")
        self._btn_apply.setEnabled(False)  # 预览出可设条目才亮
        self._btn_apply.clicked.connect(self._on_apply)
        h.addWidget(self._btn_apply)

        h.addStretch(1)
        btn_close = QPushButton("关闭", btn_row)
        btn_close.clicked.connect(self.reject)
        h.addWidget(btn_close)
        root.addWidget(btn_row)

    # ---------- 槽 ----------
    def _on_preview(self) -> None:
        text = self._edit.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "解析预览", "先粘贴清单再解析。")
            return
        self._report = classify_special_targets(self._repo, self._game, text)
        self._report_label.setText(_render_report(self._report))
        self._btn_apply.setEnabled(bool(self._report.to_set))

    def _on_apply(self) -> None:
        r = self._report
        if r is None or not r.to_set:
            return
        ret = QMessageBox.question(
            self, "批量特别关注",
            f"把 {len(r.to_set)} 个 mod 设为特别关注？\n\n"
            "其余桶（已是关注 / 其他档案 / 未收录 / 无法识别）一律不动。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 一个事务打包：要么全部标上、要么一条不动（repo 契约第 4 条）
        with self._repo.transaction():
            for mid in r.to_set:
                self._repo.set_special(mid, True)
        self.report = r
        self.accept()


def _render_report(r: SpecialReport) -> str:
    """把五桶结论翻译成人话。只摆事实 + 指路，不替用户做决定。"""
    if not r.parsed_ids:
        lines = ["没有解析出任何编号。"]
        if r.invalid_lines:
            lines.append("以下行无法识别（需要含 steamcommunity.com 与 id 参数的"
                         "工坊网址，或直接纯数字编号）：")
            lines += [f"· {line}" for line in r.invalid_lines]
        return "\n".join(lines)

    lines = [f"解析出 {len(r.parsed_ids)} 个编号："]
    lines.append(f"· 待设为特别关注：{len(r.to_set)} 条"
                 + ("——【应用】只写这一桶" if r.to_set else ""))
    lines.append(f"· 已是特别关注（跳过）：{len(r.already_special)} 条")
    if r.in_other_games:
        lines.append(f"· 属于其他档案（跳过）：{len(r.in_other_games)} 条——"
                     + "、".join(f"{mid}（{name}）" for mid, name in r.in_other_games))
    if r.missing:
        lines.append(f"· 未收录：{len(r.missing)} 条——"
                     + "、".join(map(str, r.missing)))
        lines.append("  （未收录的先入库才能标记：可到「功能模块 → 加入新 mod」"
                     "或「基础功能 → 网址批量导入」粘贴同一份清单）")
    if r.invalid_lines:
        lines.append(f"· 无法识别的行 {len(r.invalid_lines)} 条（原样列出，不代纠正）：")
        lines += [f"  {line}" for line in r.invalid_lines]
    return "\n".join(lines)
