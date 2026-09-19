"""设置页"""
"""
config/GlobalSettings.json 的读写界面：steamcmd 路径、Steam 库目录、
默认下载目录。路径框带浏览按钮与存在性提示（绿色有效 / 红色不存在，
填错也可保存，使用相关功能前修正即可）。
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core.appSettings import AppSettings

# key / 中文标签 / 空值时的灰色提示 / 是否应为文件
_FIELDS = [
    ("steamcmd_path", "steamcmd 程序",
     "steamcmd.exe 完整路径，命令生成与终端功能使用（请先自行安装）"
     "如 C:\\Program Files\\SteamCMD\\steamcmd.exe", True),
    ("steam_library_path", "Steam 库目录",
     "steamapps 所在目录，扫描本机已装 mod 时使用，"
     "如 C:\\Program Files\\Steam\\steamapps", False),
    ("default_download_dir", "默认下载目录",
     "新建游戏档案时自动预填的 mod 下载目录，"
     "如 C:\\Users\\YourName\\Documents\\SteamMods", False),
]



class SettingsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._edits: dict[str, QLineEdit] = {}
        self._statuses: dict[str, QLabel] = {}
        self._hints: dict[str, str] = {}
        self._isfile: dict[str, bool] = {}
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

        for key, label, hint, is_file in _FIELDS:
            edit = QLineEdit()
            edit.setMinimumWidth(420)
            edit.textChanged.connect(self._refresh_states)
            browse = QPushButton("浏览…")
            browse.clicked.connect(lambda _=False, e=edit, f=is_file: self._browse(e, f))
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(edit, 1)
            h.addWidget(browse)

            status = QLabel(hint)
            status.setStyleSheet("color: gray;")
            self._edits[key] = edit
            self._statuses[key] = status
            self._hints[key] = hint
            self._isfile[key] = is_file
            form.addRow(label, row)
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

    def _refresh_states(self) -> None:
        for key, status in self._statuses.items():
            text = self._edits[key].text().strip()
            if not text:
                status.setText(self._hints[key])
                status.setStyleSheet("color: gray;")
                continue
            p = Path(text)
            valid = p.is_file() if self._isfile[key] else p.is_dir()
            if valid:
                status.setText("路径有效")
                status.setStyleSheet("color: #46a758;")
            else:
                status.setText("路径不存在（可先保存，使用相关功能前修正即可）")
                status.setStyleSheet("color: #e5484d;")

    def _save(self) -> None:
        for key, edit in self._edits.items():
            self._settings.set(key, edit.text().strip())
        self._settings.save()
        self._saved_label.setText(
            f"已保存 {time.strftime('%H:%M:%S')} → {self._settings.path}")
        self._saved_label.setStyleSheet("color: #46a758;")

    def _reset(self) -> None:
        for edit in self._edits.values():
            edit.clear()
        self._saved_label.setText("已恢复为默认（空）值，点【保存】写入文件")
        self._saved_label.setStyleSheet("color: gray;")
