"""命令生成页"""
"""
把当前游戏勾选的 mod 拼成 steamcmd 下载命令，交给用户手动粘贴执行。

页面从上到下四块：
  1) 游戏信息行：当前档案 + "重新载入"按钮；
  2) 用法提示行：三步说明 + "复制登录命令"按钮（内容来自设置页，原样复制）；
  3) 三组勾选清单（需要更新 / 未下载 / 已最新）：
     前两组默认勾上，"已最新"默认不勾（想强制重下可手动勾）；
  4) 底部：只读预览框（实时显示将要复制的内容）+ 统计 + 复制 / 另存按钮。

使用方法（也写在界面提示里）：
  ① 自己打开 steamcmd 并登录（账号必须拥有该游戏，否则下载会报错）；
  ② 在本页勾选 mod，点"复制命令"；
  ③ 粘贴到 steamcmd 窗口回车。多行文本会被终端一行一行依次执行，
     前一条下载完才开始下一条，所以 mod 数量多少都没有限制。

分工与边界（记事本架构约定）：
  - 分组、拼命令全在 core/commandBuilder.py（纯函数、可单测），本页只管界面和落盘；
  - 只读 ModRepository（list_mods），GUI 层零 SQL；
  - 不联网、不修改 mods 表；
  - 每次复制 / 另存向 operations_log 记一笔，result="generated"
    （意思是"命令已交到用户手里"，与将来真正执行的 success/error 区分开）；
  - 另存的 txt 用 utf-8：内容纯 ASCII（数字和英文），steamcmd 读没有任何编码问题。
"""

from datetime import date
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.commandBuilder import build_copy_text, group_mods
from gui.consolePanel import LogBus

# 三组的展示顺序和标题（键要和 commandBuilder.group_mods 的返回值对上）
_GROUP_ORDER = [
    ("needs_update", "需要更新"),
    ("not_downloaded", "未下载"),
    ("up_to_date", "已最新"),
]

# 登录命令在设置页里的键名（appSettings.DEFAULTS / settingsPage._FIELDS 同名）
_LOGIN_CMD_KEY = "steamcmd_login_cmd"

_USAGE_TEXT = (
    "使用方法：① 自己打开 steamcmd 并登录（账号必须拥有该游戏）→ "
    "② 在下面勾选 mod，点右下角“复制命令”→ "
    "③ 粘贴到 steamcmd 窗口回车，命令会一行一行依次执行。"
)


class CommandGenPage(QWidget):
    """命令生成页：选 mod → 预览 → 复制 / 另存。"""

    def __init__(self, repo, settings, parent=None, *, log=None):
        super().__init__(parent)
        self._repo = repo          # ModRepository：读 mod 清单、写 operations_log
        self._settings = settings  # AppSettings：读登录命令
        # 日志总线：MainWindow 注入；单独调试本页时兜底建一个（消息没人显示但不崩）
        self._log = log if log is not None else LogBus()
        self._game = None          # 当前游戏档案（切游戏时由 MainWindow 调 set_game 换进来）
        # 勾选登记表：{组名: [(mod_id, 勾选框), ...]}，生成时按这里收答案
        self._checks = {key: [] for key, _ in _GROUP_ORDER}
        self._count_labels = {}    # 组名 → "已勾选 x / 共 n 个" 标签

        self._init_ui()

    # ---------------- 搭骨架 ----------------

    def _init_ui(self):
        root = QVBoxLayout(self)

        # 第 1 块：游戏信息 + 重新载入
        top = QHBoxLayout()
        self._lbl_game = QLabel("（还没有选择游戏）")
        btn_reload = QPushButton("重新载入")
        btn_reload.setToolTip("从数据库重新读取当前游戏的 mod 清单")
        btn_reload.clicked.connect(self._reload)
        top.addWidget(self._lbl_game, 1)
        top.addWidget(btn_reload)
        root.addLayout(top)

        # 第 2 块：用法提示 + 复制登录命令
        hint = QHBoxLayout()
        lbl_usage = QLabel(_USAGE_TEXT)
        lbl_usage.setWordWrap(True)
        self._btn_login = QPushButton("复制登录命令")
        self._btn_login.setToolTip(
            "复制设置页里填的 steamcmd 登录命令；"
            "还没填的话去设置页 → steamcmd 登录命令")
        self._btn_login.clicked.connect(self._on_copy_login)
        hint.addWidget(lbl_usage, 1)
        hint.addWidget(self._btn_login)
        root.addLayout(hint)

        # 第 3 块：三组勾选清单（滚动区，每次刷新都重建）
        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # 第 4 块：预览 + 统计 + 按钮
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("勾选 mod 后，这里实时显示将要复制的命令")
        self._preview.setMaximumHeight(110)
        root.addWidget(self._preview)

        bottom = QHBoxLayout()
        self._lbl_summary = QLabel("已勾选 0 个")
        btn_save = QPushButton("另存为 txt")
        btn_copy = QPushButton("复制命令")
        btn_copy.setDefault(True)
        btn_save.clicked.connect(self._on_save)
        btn_copy.clicked.connect(self._on_copy)
        bottom.addWidget(self._lbl_summary, 1)
        bottom.addWidget(btn_save)
        bottom.addWidget(btn_copy)
        root.addLayout(bottom)

    # ---------------- 数据进出 ----------------

    def set_game(self, game):
        """MainWindow 切换游戏时调用。game 可能为 None（还没添加档案）。"""
        self._game = game
        if game is None:
            self._lbl_game.setText("（还没有选择游戏）—— 请先添加档案")
            self._clear_body()
            self._preview.clear()
            self._lbl_summary.setText("已勾选 0 个")
            return
        self._lbl_game.setText(f"当前游戏：{game.name}（AppID {game.app_id}）")
        self._reload()

    def focus_ids(self, mod_ids):
        """从 mod 库页右键跳过来时调用：取消全部勾选，只勾选传入的这些 id。
        找不到的 id（已删除/失败的 mod 不在清单里）在控制台说明。"""
        id_set = {int(i) for i in mod_ids}
        found = set()
        for key, _ in _GROUP_ORDER:
            for mod_id, cb in self._checks.get(key, []):
                cb.setChecked(mod_id in id_set)  # 顺路完成"清空其余"
                if mod_id in id_set:
                    found.add(mod_id)
        missing = id_set - found
        if missing:
            self._log.warn(
                f"这些 mod 不在可下载清单中（可能已删除/失败）：{sorted(missing)}")

    def _reload(self):
        """从数据库重读当前游戏的 mod，按三组重建勾选框。"""
        if self._game is None:
            return
        # 游戏主键就是 app_id（与 mod 库页同款调用）
        mods = self._repo.list_mods(self._game.app_id)
        groups = group_mods(mods)

        self._clear_body()
        for key, title in _GROUP_ORDER:
            box = self._make_group_box(key, title)
            rows = groups[key]
            if not rows:
                box.layout().addWidget(QLabel("（无）"))
            for m in rows:
                cb = QCheckBox(f"{m.mod_id}  {m.title}")
                # 推荐勾选：需要更新 + 未下载 默认勾上；已最新默认不勾
                cb.setChecked(key in ("needs_update", "not_downloaded"))
                cb.toggled.connect(self._refresh_stats)
                box.layout().addWidget(cb)
                self._checks[key].append((m.mod_id, cb))
            self._body_layout.addWidget(box)

        self._refresh_stats()

    # ---------------- 勾选与统计 ----------------

    def _make_group_box(self, key, title):
        """建一个分组框：标题行（勾选统计 + 全选/清空）+ 内部勾选框容器。"""
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        head = QHBoxLayout()
        lbl = QLabel("共 0 个")
        btn_all = QPushButton("全选")
        btn_none = QPushButton("清空")
        # 用 k=key 把"当前组名"固定住，避免按钮触发时读到循环变量的最后值
        btn_all.clicked.connect(lambda _=False, k=key: self._set_group(k, True))
        btn_none.clicked.connect(lambda _=False, k=key: self._set_group(k, False))
        head.addWidget(lbl, 1)
        head.addWidget(btn_all)
        head.addWidget(btn_none)
        layout.addLayout(head)
        self._count_labels[key] = lbl
        return box

    def _clear_body(self):
        """清空中部滚动区里的所有旧内容，准备重建。"""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._checks = {key: [] for key, _ in _GROUP_ORDER}
        self._count_labels = {}

    def _set_group(self, key, checked):
        """一键全选/清空某一组。"""
        for _, cb in self._checks[key]:
            cb.setChecked(checked)

    def _refresh_stats(self):
        """刷新每组统计、底部总统计和预览框（勾选框一变就走这里，一处算清别处只读）。"""
        total = 0
        parts = []
        for key, title in _GROUP_ORDER:
            rows = self._checks.get(key, [])
            n = sum(1 for _, cb in rows if cb.isChecked())
            total += n
            if key in self._count_labels:
                self._count_labels[key].setText(f"已勾选 {n} / 共 {len(rows)} 个")
            parts.append(f"{title} {n}")
        self._lbl_summary.setText(f"已勾选 {total} 个（{' + '.join(parts)}）")
        self._preview.setPlainText(self._current_text() or "")

    def _checked_ids(self):
        """按"需更新 → 未下载 → 已最新"的顺序收齐所有勾选的 mod id。"""
        ids = []
        for key, _ in _GROUP_ORDER:
            for mod_id, cb in self._checks.get(key, []):
                if cb.isChecked():
                    ids.append(mod_id)
        return ids

    def _current_text(self):
        """当前勾选对应的命令文本；没选游戏时返回 None。"""
        if self._game is None:
            return None
        return build_copy_text(self._game.app_id, self._checked_ids())

    # ---------------- 三个动作 ----------------

    def _on_copy_login(self):
        """复制设置页里的登录命令，原样进剪贴板（我们不解析、不拼装内容）。"""
        cmd = str(self._settings.get(_LOGIN_CMD_KEY) or "").strip()
        if not cmd:
            self._log.warn("设置里还没填登录命令：设置页 → steamcmd 登录命令")
            return
        QApplication.clipboard().setText(cmd)
        self._log.ok("已复制登录命令，打开 steamcmd 后先粘贴这条回车")

    def _on_copy(self):
        """把勾选的下载命令复制进剪贴板。"""
        if self._game is None:
            self._log.warn("还没有选择游戏，无法生成命令")
            return
        ids = self._checked_ids()
        if not ids:
            QMessageBox.information(self, "提示", "还没勾选任何 mod。")
            return
        QApplication.clipboard().setText(self._current_text())
        self._log.ok(f"已复制 {len(ids)} 条下载命令，去 steamcmd 窗口粘贴回车即可")
        self._log_generated(ids, "复制到剪贴板")

    def _on_save(self):
        """把勾选的下载命令另存为 txt（对齐旧脚本 1.7 的落盘习惯）。"""
        if self._game is None:
            self._log.warn("还没有选择游戏，无法生成命令")
            return
        ids = self._checked_ids()
        if not ids:
            QMessageBox.information(self, "提示", "还没勾选任何 mod。")
            return
        default_name = f"下载命令_{date.today():%Y%m%d}.txt"
        start_dir = self._game.download_dir or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为下载命令", str(Path(start_dir) / default_name),
            "文本文件 (*.txt)",
        )
        if not path:
            return
        Path(path).write_text(self._current_text(), encoding="utf-8")
        self._log.ok(f"已保存 {len(ids)} 条下载命令：{path}")
        self._log_generated(ids, f"另存到 {path}")

    # ---------------- 记账 ----------------

    def _log_generated(self, ids, action_detail):
        """往 operations_log 记一笔"已生成"。

        repo 约定：add_operation(命令文本) 先登记拿 op_id，
        finish_operation(op_id, result=...) 事后回填——
        这里没有真正的执行环节，所以登记和回填连着做，
        result 用 "generated" 标记"已生成、未执行"。
        写失败不影响已完成的复制/另存，但必须在控制台喊出来（不静默吞错误）。
        """
        try:
            summary = (f"# 生成下载命令：{len(ids)} 条 "
                       f"workshop_download_item（{action_detail}）")
            op_id = self._repo.add_operation(summary)
            self._repo.finish_operation(op_id, result="generated")
        except Exception as e:
            self._log.error(f"写 operations_log 失败：{e}")
