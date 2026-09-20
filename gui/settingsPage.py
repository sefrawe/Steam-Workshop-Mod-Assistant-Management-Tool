"""设置页
"""
"""
config/GlobalSettings.json 的读写界面。

字段分两类，校验规则不同：
- 路径类（steamcmd / Steam 库 / 默认下载目录）：带浏览按钮，检查存在性，
  填错也可保存——使用相关功能前修正即可
- 数字类（请求间隔 / 重试次数 / 慢更新阈值 / 快照条数）：必须正整数，
  保存前校验，填错整批不落盘。数字填错比路径填错更糟：读取层会静默
  回退默认值，用户以为改成功了实际没生效，所以必须在保存这关拦住
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core.appSettings import DEFAULTS, AppSettings

# key / 中文标签 / 空值时的灰色提示 / 字段类型（"file"=文件 / "dir"=目录 / "number"=正整数）
_FIELDS = [
    ("steamcmd_path", "steamcmd 程序",
     "steamcmd.exe 完整路径，命令生成与终端功能使用（请先自行安装）"
     "如 C:\\Program Files\\SteamCMD\\steamcmd.exe", "file"),
    ("steam_library_path", "Steam 库目录",
     "steamapps 所在目录，扫描本机已装 mod 时使用，"
     "如 C:\\Program Files\\Steam\\steamapps", "dir"),
    ("default_download_dir", "默认下载目录",
     "新建游戏档案时自动预填的 mod 下载目录，"
     "如 C:\\Users\\YourName\\Documents\\SteamMods", "dir"),
    ("api_request_interval_ms", "API 请求间隔（毫秒）",
     "批量查询 Steam 工坊接口时，两次请求之间的等待时间；太小可能被服务器限流",
     "number"),
    ("api_max_retries", "API 重试次数",
     "请求被服务器限流（429/503）时的自动重试上限，每次重试间隔会逐渐拉长",
     "number"),
    ("slow_update_days", "慢更新提醒阈值（天）",
     "距上次已知更新超过这个天数的 mod，在更新检测结果里标红提示"
     "（作者更新节奏慢，值得留意）", "number"),
    ("snapshot_keep", "快照保留条数",
     "每个 mod 保留的历史版本记录条数；检测到新版本时自动淘汰更旧的记录",
     "number"),
]


class SettingsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._edits: dict[str, QLineEdit] = {}
        self._kinds: dict[str, str] = {}
        self._statuses: dict[str, QLabel] = {}
        self._hints: dict[str, str] = {}
        self._build_ui()
        self._load_to_ui()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("全局设置")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        root.addLayout(form)

        for key, label, hint, kind in _FIELDS:
            edit = QLineEdit()
            edit.textChanged.connect(self._refresh_states)
            self._edits[key] = edit
            self._kinds[key] = kind
            if kind == "number":
                # 数字不用那么宽的输入框，视觉上和路径行区分开
                edit.setMaximumWidth(160)
                form.addRow(label, edit)
            else:
                # 路径行：输入框 + 浏览按钮并排
                edit.setMinimumWidth(420)
                browse = QPushButton("浏览…")
                browse.clicked.connect(
                    lambda _=False, e=edit, f=(kind == "file"): self._browse(e, f))
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.addWidget(edit, 1)
                h.addWidget(browse)
                form.addRow(label, row)
            status = QLabel(hint)
            status.setStyleSheet("color: gray;")
            self._statuses[key] = status
            self._hints[key] = hint
            form.addRow("", status)

        btn_row = QWidget()
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 6, 0, 0)
        save = QPushButton("保存")
        save.clicked.connect(self._save)
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self._reset)
        h.addWidget(save)
        h.addWidget(reset)
        h.addStretch(1)
        root.addWidget(btn_row)

        self._saved_label = QLabel("")
        root.addWidget(self._saved_label)

        note = QLabel(
            "说明：设置保存在 config/GlobalSettings.json，各功能在使用时读取；"
            "若修改某项后未生效，重启程序即可。")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        root.addWidget(note)
        root.addStretch(1)

    def _browse(self, edit: QLineEdit, is_file: bool) -> None:
        if is_file:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择程序", edit.text() or "",
                "可执行文件 (*.exe);;所有文件 (*)")
        else:
            path = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or "")
        if path:
            edit.setText(Path(path).__str__())

    # ---------- 数据 ----------
    def _load_to_ui(self) -> None:
        for key, edit in self._edits.items():
            edit.setText(self._settings.get(key))

    @staticmethod
    def _number_ok(text: str) -> bool:
        """正整数才算过——间隔、次数、天数、条数都没有 0 和负数的意义。"""
        try:
            return int(text) > 0
        except ValueError:
            return False

    def _refresh_states(self) -> None:
        for key, status in self._statuses.items():
            text = self._edits[key].text().strip()
            kind = self._kinds[key]
            if not text:
                status.setText(self._hints[key])
                status.setStyleSheet("color: gray;")
                continue
            if kind == "file":
                valid = Path(text).is_file()
                ok_text, bad_text = "路径有效", "路径不存在（可先保存，使用相关功能前修正即可）"
            elif kind == "dir":
                valid = Path(text).is_dir()
                ok_text, bad_text = "路径有效", "路径不存在（可先保存，使用相关功能前修正即可）"
            else:  # number
                valid = self._number_ok(text)
                ok_text, bad_text = "数值有效", "需要正整数，保存前请修正"
            status.setText(ok_text if valid else bad_text)
            status.setStyleSheet("color: #46a758;" if valid else "color: #e5484d;")

    def _save(self) -> None:
        # 数字项先整体过一遍：任何一项不合法就整批不落盘（理由见文件头）
        bad_keys = [key for key, kind in self._kinds.items()
                    if kind == "number"
                    and not self._number_ok(self._edits[key].text().strip())]
        if bad_keys:
            labels = "、".join(label for key, label, _hint, _kind in _FIELDS
                              if key in bad_keys)
            self._saved_label.setText(f"以下设置需要正整数，未保存：{labels}")
            self._saved_label.setStyleSheet("color: #e5484d;")
            return
        for key, edit in self._edits.items():
            self._settings.set(key, edit.text().strip())
        self._settings.save()
        self._saved_label.setText(
            f"已保存 {time.strftime('%H:%M:%S')} → {self._settings.path}")
        self._saved_label.setStyleSheet("color: #46a758;")

    def _reset(self) -> None:
        # 路径清空 = 默认值本来就是空；数字填回默认值，而不是留空
        # （留空虽然也能被读取层兜底，但界面上显示具体默认数更直观）
        for key, edit in self._edits.items():
            if self._kinds[key] == "number":
                edit.setText(DEFAULTS[key])
            else:
                edit.clear()
        self._saved_label.setText("已恢复默认值，点【保存】写入文件")
        self._saved_label.setStyleSheet("color: gray;")
