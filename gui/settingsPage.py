"""设置页
"""
r"""config/GlobalSettings.json 的读写界面。

字段分五类，校验规则不同：
- 路径类（kind="file"/"dir"）：带浏览按钮，检查存在性，填错也可保存——
  使用相关功能前修正即可
- 数字类（kind="number"）：必须正整数，保存前校验，填错整批不落盘。
  数字填错比路径填错更糟：读取层会静默回退默认值，用户以为改成功了
  实际没生效，所以必须在保存这关拦住
- 文本类（kind="text"）：普通单行文本，不做任何校验。
  目前只有 steamcmd 登录命令一项：它是"整行原样发给 steamcmd"的命令，
  内容对不对只有 steamcmd 自己知道，软件校验没有意义；
  空着也允许——用到它的功能自己会提示去填
- 开关类（kind="bool"）：勾选框，存 "1"/"0"，缺键按开（默认全开）。
  勾选状态即语义，不设说明/校验两层，hint 放 tooltip；
  目前用于 mod 库页列显示开关（T19⑯）

T10 之后不再有"Steam 库目录"和"默认下载目录"两项：
本地扫描和建档的目录都从 steamcmd 程序路径推导（决策 21），
推导逻辑在 core/steamPaths.py，界面上只保留这一把钥匙。

布局注意（踩坑 ⑨）：长文字的 QLabel 必须开 setWordWrap(True)。
不开换行的标签会把整行文字宽度当作"最小宽度"上报给窗口，
一条一百多字的提示就能把整个主窗口撑到一千三百像素以上、
缩不回去。凡是可能变长的文字（灰字提示、保存结果行）一律换行。

T19⑧⑨：字段区整体装进 QScrollArea（条目多 + 常驻说明，总高度轻松
超过小窗，硬排会被裁掉）；每项输入框下常驻一行小字说明（灰、11px，
文本就是 _FIELDS 的 hint），校验状态单独一行、只对已填内容说话——
说明层常驻、校验层按需，两层不再互相顶掉。
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.appSettings import DEFAULTS, AppSettings
from core.steamPaths import ensure_steamcmd_exe

# key / 中文标签 / 空值时的灰色提示 / 字段类型
# （"file"=文件 / "dir"=目录 / "number"=正整数 / "text"=普通文本不校验
#   / "bool"=勾选开关，hint 进 tooltip）
# 注意：这里的键集必须与 appSettings.DEFAULTS 完全一致（决策 12）
_FIELDS = [
    ("steamcmd_path", "steamcmd 程序",
     "steamcmd.exe 完整路径（请先自行安装），如 C:\\Program Files\\SteamCMD\\steamcmd.exe；"
     "也可以直接填包含 steamcmd.exe 的文件夹——保存时自动补全 exe 文件名。"
     "命令生成、本地扫描、新建档案时的下载目录推导，都按它定位",
     "file"),
    # hint = 决策 12 v1.6 原文；末句"账号必须拥有对应游戏"来自决策 19，
    # 属原文之外补充，要严格照原文可删
    ("steamcmd_login_cmd", "steamcmd 登录命令",
     "整行原样发给 steamcmd，例：login 你的用户名。"
     "首次登录需在 steamcmd 内输入密码与邮箱验证码，成功一次后本机缓存，"
     "之后只需此命令等待验证即可；把密码写进命令不推荐"
     "（会明文保存在本机设置文件里）。"
     "账号必须拥有对应游戏，否则下载其创意工坊内容会报错",
     "text"),
    ("steam_client_library", "Steam 客户端库目录",
     "Steam 客户端存放游戏内容库的位置（如 D:\\SteamLibrary），「首次使用 → 纳入已有 mod」"
     "按它读取客户端的订阅记录；选库根、steamapps 或 workshop 层都可以。"
     "它只关系到 Steam 客户端，与 steamcmd 的目录无关", "dir"),

    ("api_request_interval_ms", "API 请求间隔（毫秒）",
     "批量查询 Steam 工坊接口时，两次请求之间的等待时间；太小可能被服务器限流",
     "number"),
    ("api_max_retries", "API 重试次数",
     "请求被服务器限流（429/503）时的自动重试上限，每次重试间隔会逐渐拉长",
     "number"),
    ("slow_update_days", "慢更新提醒阈值（天）",
     "距上次已知更新超过这个天数的 mod，在更新检测结果里标红提示"
     "（作者更新节奏慢，值得留意）",
     "number"),
    ("snapshot_keep", "快照保留条数",
     "每个 mod 保留的历史版本记录条数；检测到新版本时自动淘汰更旧的记录。"
     "修改后重启程序才生效（条数在启动时读入一次）",
     "number"),
    # ↓ 两条 hint 为拟稿（记事本无原文），措辞可后调
    ("backup_keep_per_mod", "备份每 mod 保留份数",
     "每个 mod 保留的备份份数；超出后自动淘汰最旧的备份（钉住的除外）",
     "number"),
    ("backup_total_quota_gb", "备份总配额（GB）",
     "全部备份合计的容量上限；超出后从最旧的备份开始清腾（钉住的除外）",
     "number"),
    # ↓ mod 库页列显示开关（T19⑯）：编号与标题两列永远显示，不设开关；
    #   改完点保存，回 mod 库页点【刷新】生效，无需重启
    ("mod_col_status", "列表显示：状态列",
     "关闭后 mod 库页隐藏「状态」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_remote_ver", "列表显示：远端版本列",
     "关闭后 mod 库页隐藏「远端版本」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_local_ver", "列表显示：本地版本列",
     "关闭后 mod 库页隐藏「本地版本」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_update", "列表显示：更新列",
     "关闭后 mod 库页隐藏「更新」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_size", "列表显示：大小列",
     "关闭后 mod 库页隐藏「大小」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_tags", "列表显示：标签列",
     "关闭后 mod 库页隐藏「标签」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_special", "列表显示：特别关注列",
     "关闭后 mod 库页隐藏「特别关注」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
    ("mod_col_note", "列表显示：备注列",
     "关闭后 mod 库页隐藏「备注」列；改完点保存，回 mod 库页点【刷新】生效",
     "bool"),
]


class SettingsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._edits: dict[str, QLineEdit] = {}
        self._checks: dict[str, QCheckBox] = {}
        self._kinds: dict[str, str] = {}
        self._statuses: dict[str, QLabel] = {}
        self._build_ui()
        self._load_to_ui()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("全局设置")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        # T19⑧：字段区装进滚动区——保存按钮、保存结果、页脚说明留在
        # 滚动区外，任何窗口高度都够得着、看得见
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(scroll)
        form = QFormLayout(body)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)

        for key, label, hint, kind in _FIELDS:
            if kind == "bool":
                # 开关项：勾选框自带状态语义，不设说明/校验两层；
                # hint 放 tooltip（决策 22② 的等价满足）
                cb = QCheckBox()
                cb.setToolTip(hint)
                self._checks[key] = cb
                form.addRow(label, cb)
                continue

            edit = QLineEdit()
            edit.textChanged.connect(self._refresh_states)
            self._edits[key] = edit
            self._kinds[key] = kind

            if kind == "number":
                # 数字不用那么宽的输入框，视觉上和路径行区分开
                edit.setMaximumWidth(160)

            # 每项一个纵向容器：输入行 + 常驻说明 + 校验状态（T19⑨）。
            # 旧版说明和状态共用一个标签，一填内容说明就被校验文案顶掉；
            # 拆成两层后说明常驻、状态按需
            field = QWidget()
            v = QVBoxLayout(field)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)

            if kind in ("file", "dir"):
                # 路径行 = 输入框 + 浏览按钮并排；文本项直接放输入框。
                # 不设最小宽度：QLineEdit 横向本来就是扩张策略，
                # 会自动填满页面剩余宽度，设了反而把窗口顶宽（踩坑 ⑨）
                browse = QPushButton("浏览…")
                browse.clicked.connect(
                    lambda _=False, e=edit, f=(kind == "file"): self._browse(e, f))
                h = QHBoxLayout()
                h.setContentsMargins(0, 0, 0, 0)
                h.addWidget(edit, 1)
                h.addWidget(browse)
                v.addLayout(h)
            else:
                v.addWidget(edit)

            desc = QLabel(hint)
            desc.setWordWrap(True)  # 必须换行，否则整句长度变成窗口最小宽度
            desc.setStyleSheet("color: gray; font-size: 11px;")
            v.addWidget(desc)

            status = QLabel("")
            status.setWordWrap(True)
            self._statuses[key] = status
            v.addWidget(status)

            form.addRow(label, field)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # 按钮与反馈行在滚动区外：保存动作和它的结果永远可见
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
        # 保存提示里带完整配置文件路径，同样可能很长——换行防顶宽
        self._saved_label.setWordWrap(True)
        root.addWidget(self._saved_label)

        note = QLabel(
            "说明：设置保存在 config/GlobalSettings.json，各功能在使用时读取；"
            "若修改某项后未生效，重启程序即可。")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        root.addWidget(note)

    def _browse(self, edit: QLineEdit, is_file: bool) -> None:
        if is_file:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择程序", edit.text() or "",
                "可执行文件 (*.exe);;所有文件 (*)")
        else:
            path = QFileDialog.getExistingDirectory(
                self, "选择目录", edit.text() or "")
        if path:
            edit.setText(str(path))  # Path 的字符串形式，跟原写法等价但更直白

    # ---------- 数据 ----------

    def _load_to_ui(self) -> None:
        for key, edit in self._edits.items():
            edit.setText(self._settings.get(key))
        # 开关项：存 "1"/"0"；缺键/其他值按开（默认全显示）
        for key, cb in self._checks.items():
            cb.setChecked(self._settings.get(key) != "0")
        # 记住打开时的开关状态：保存时对比，才知道"这次有没有动列显示"
        self._bools_at_load = {k: cb.isChecked()
                               for k, cb in self._checks.items()}


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
                # 空值：状态行留白——解释已常驻在下方小字里（T19⑨），
                # 校验状态只对已填内容说话
                status.setText("")
                status.setStyleSheet("color: gray;")
                continue
            if kind == "file":
                p = Path(text)
                if p.is_dir():
                    # T21①：文件夹输入——里面有没有 steamcmd.exe 决定
                    # 是"可识别"还是"缺东西"，两种状态分开说清楚
                    has_exe = (p / "steamcmd.exe").is_file()
                    valid = has_exe
                    ok_text = "已识别文件夹：保存时自动补全为其中的 steamcmd.exe"
                    bad_text = ("文件夹里没有找到 steamcmd.exe"
                                "（可先保存，使用相关功能前修正即可）")
                else:
                    valid = p.is_file()
                    ok_text = "路径有效"
                    bad_text = "路径不存在（可先保存，使用相关功能前修正即可）"
            elif kind == "dir":
                valid = Path(text).is_dir()
                ok_text = "路径有效"
                bad_text = "路径不存在（可先保存，使用相关功能前修正即可）"
            elif kind == "text":
                # 文本项没有"对错"概念，填了就算数——
                # 内容对不对只有 steamcmd 用起来才知道
                valid = True
                ok_text = "已填写（使用时原样传递，不校验内容）"
                bad_text = ok_text
            else:  # number
                valid = self._number_ok(text)
                ok_text = "数值有效"
                bad_text = "需要正整数，保存前请修正"
            status.setText(ok_text if valid else bad_text)
            status.setStyleSheet(
                "color: #46a758;" if valid else "color: #e5484d;")

    def _save(self) -> None:
        # 数字项先整体过一遍：任何一项不合法就整批不落盘（理由见文件头）
        bad_keys = [key for key, kind in self._kinds.items()
                    if kind == "number"
                    and not self._number_ok(self._edits[key].text().strip())]
        if bad_keys:
            labels = "、".join(label for key, label, _h, _k in _FIELDS
                              if key in bad_keys)
            self._saved_label.setText(f"以下设置需要正整数，未保存：{labels}")
            self._saved_label.setStyleSheet("color: #e5484d;")
            return

        for key, edit in self._edits.items():
            value = edit.text().strip()
            if key == "steamcmd_path":
                # T21①：文件夹 → 自动补全为完整 exe 路径再入库。
                # 从此下游（终端启动 / steamPaths 推导 / 备份引擎）
                # 拿到的恒为完整 exe 路径
                value = ensure_steamcmd_exe(value)
                edit.setText(value)  # 界面同步显示保存后的真实值
            self._settings.set(key, value)

        # 开关项（T19⑯）：勾选 → "1"/"0"
        for key, cb in self._checks.items():
            self._settings.set(key, "1" if cb.isChecked() else "0")

        self._settings.save()
        # 列显示的生效时机是"回 mod 库页点刷新"，不在保存瞬间——
        # 动过开关就提醒一句，别让用户以为没生效
        changed = [k for k, cb in self._checks.items()
                   if cb.isChecked() != self._bools_at_load.get(k, True)]
        self._bools_at_load = {k: cb.isChecked()
                               for k, cb in self._checks.items()}
        msg = f"已保存 {time.strftime('%H:%M:%S')} → {self._settings.path}"
        if changed:
            msg += "\n列显示有改动：回 mod 库页点【刷新】生效"
        self._saved_label.setText(msg)
        self._saved_label.setStyleSheet("color: #46a758;")

    def _reset(self) -> None:
        # 数字填回默认值，而不是留空（留空虽然也能被读取层兜底，
        # 但界面上显示具体默认数更直观）；路径和文本项清空 = 默认值
        # 本来就是空；开关项默认全开
        for key, edit in self._edits.items():
            if self._kinds[key] == "number":
                edit.setText(DEFAULTS[key])
            else:
                edit.clear()
        for cb in self._checks.values():
            cb.setChecked(True)
        self._saved_label.setText("已恢复默认值，点【保存】写入文件")
        self._saved_label.setStyleSheet("color: gray;")
