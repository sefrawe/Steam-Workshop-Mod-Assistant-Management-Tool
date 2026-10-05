"""设置页
"""
r"""config/GlobalSettings.json 的读写界面（程序内唯一配置入口
core/appSettings 的界面侧）。

字段分五类，校验规则不同：
- 路径类（kind="file"/"dir"）：带浏览按钮，检查存在性，填错也可
  保存——使用相关功能前修正即可；
- 数字类（kind="number"）：必须正整数，保存前校验，填错整批不
  落盘。数字填错比路径填错更糟：读取层会静默回退默认值，用户
  以为改成功了实际没生效，所以必须在保存这关拦住；
- 文本类（kind="text"/"text_multi"）：不做任何校验。登录命令是
  多行：一行一条、可存多个账号，第一行 = 批量下载自动登录发送
  的命令，其余行在 steamcmd 终端的「发送登录命令」下拉框里选发；
  内容对不对只有 steamcmd 自己知道；空着也允许——用到它的功能
  自己会提示去填；
- 开关类（kind="bool"）：勾选框，存 "1"/"0"，hint 放 tooltip，
  目前用于 mod 库页列显示等；
- 关键词类（local_title_warn_keywords）：中英文逗号或空格分隔，
  保存时中文逗号归一为半角（见 _save）——界面允许顺手打中文
  逗号，入库口径保持单一，消费端只按半角逗号/空白切分。

目录推导原则：本地扫描和建档的目录都从 steamcmd 程序路径推导
（core/steamPaths.py），界面上只保留这一把钥匙；
steam_client_library 只关 Steam 客户端的订阅记录，与 steamcmd
目录树无关。

布局注意：长文字的 QLabel 必须开 setWordWrap(True)——不开换行的
标签会把整行文字宽度当作"最小宽度"上报，一条一百多字的提示就能
把主窗口撑宽到缩不回去。凡可能变长的文字（灰字提示、保存结果行）
一律换行。字段区整体装进 QScrollArea；每项输入框下常驻一行小字
说明（灰、11px，文本就是 _FIELDS 的 hint），校验状态单独一行、
只对已填内容说话——说明层常驻、校验层按需，两层不互相顶掉。
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget, QComboBox,
)

from gui.theme import reapply_theme, font_px
from core.appSettings import DEFAULTS, AppSettings
from core.steamPaths import ensure_steamcmd_exe

# key / 中文标签 / 空值时的灰色提示 / 字段类型
# （"file"=文件 / "dir"=目录 / "number"=正整数 / "text"=普通文本不校验
# / "text_multi"=多行文本不校验（决策 95 登录命令）/ "bool"=勾选开关，
# hint 进 tooltip）
# 注意：这里的键集必须与 appSettings.DEFAULTS 完全一致（决策 12）
_FIELDS = [
    ("steamcmd_path", "steamcmd 程序",
     "steamcmd.exe 完整路径（请先自行安装），"
     "如 C:\\Program Files\\SteamCMD\\steamcmd.exe；"
     "也可以直接填包含 steamcmd.exe 的文件夹——保存时自动补全 exe 文件名。"
     "命令生成、本地扫描、新建档案时的下载目录推导，都按它定位",
     "file"),

    # 决策 95：登录命令改多行——一行一条、可存多个账号；第一行 =
    # 批量下载自动登录发送的命令，其余行在 steamcmd 终端的
    # 「发送登录命令」下拉框里选发
    ("steamcmd_login_cmd", "steamcmd 登录命令",
     "一行一条，可存多个账号的登录命令（例：login 你的用户名）。"
     "第一行 = 批量下载自动登录发送的命令；其余行在 steamcmd 终端的"
     "「发送登录命令」下拉框里选发。"
     "首次登录需在 steamcmd 内输入密码与邮箱验证码，成功一次后本机缓存，"
     "之后只需此命令等待验证即可；把密码写进命令会明文保存在本机"
     "设置文件里，注意保管。"
     "账号必须拥有对应游戏，否则下载其创意工坊内容会报错",
     "text_multi"),

    ("steam_client_library", "Steam 客户端库目录",
     "Steam 客户端存放游戏内容库的位置（如 D:\\SteamLibrary），"
     "「纳入已有 mod」向导按它读取客户端的订阅记录；"
     "选库根、steamapps 或 workshop 层都可以。"
     "它只关系到 Steam 客户端，与 steamcmd 的目录无关",
     "dir"),

    ("api_request_interval_ms", "API 请求间隔（毫秒）",
     "批量查询 Steam 工坊接口时，两次请求之间的等待时间；"
     "太小可能被服务器限流",
     "number"),

    ("api_max_retries", "API 重试次数",
     "请求被服务器限流（429/503）时的自动重试上限，"
     "每次重试间隔会逐渐拉长",
     "number"),

    ("slow_update_days", "慢更新提醒阈值（天）",
     "距上次已知更新超过这个天数的 mod，在更新检测结果里标红提示"
     "（作者更新节奏慢，值得留意）",
     "number"),

    ("snapshot_keep", "快照保留条数",
     "每个 mod 保留的历史版本记录条数；检测到新版本时自动淘汰更旧的记录。"
     "修改后重启程序才生效（条数在启动时读入一次）",
     "number"),

    # 界面字号缩放（重启生效，与 snapshot_keep 同一惯例：启动读一次）。
    # 用百分数绕开 number 类"正整数"校验，不新增字段类型；
    # 超出合理范围的值在启动应用处被忽略（等于 100）
    ("ui_font_scale", "界面字号缩放（%）",
     "整体字号缩放百分比，100 = 默认（建议 80~200，超出范围不生效）。"
     "改后重启程序生效（字号在启动时统一应用一次）",
     "number"),

    # 备份保留策略两键（与 core/appSettings.DEFAULTS、备份页兜底同口径）
    ("backup_keep_per_mod", "备份每 mod 保留份数",
     "每个 mod 保留的备份份数；超出后自动淘汰最旧的备份（钉住的除外）",
     "number"),
    ("backup_total_quota_gb", "备份总配额（GB）",
     "全部备份合计的容量上限；超出后从最旧的备份开始清腾（钉住的除外）",
     "number"),

    # ↓ mod 库页列显示开关（T19⑯）：编号与标题两列永远显示，不设开关；
    # 改完点保存，回 mod 库页点【刷新】生效，无需重启
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
    ("mod_col_color", "列表显示：颜色列", "关闭后 mod 库页隐藏「颜色」列；改完点保存，回 mod 库页点【刷新】生效", "bool"),

    # 批次自动登录开关（决策 91，全局开关）：关掉的使用场景 = 换账号
    # 下载——终端手动 login 另一个账号后再开批次，批次不再自动把
    # 设置里的登录命令抢先发出去
    ("batch_auto_login", "批量下载自动登录",
     "开：批次开始前自动发送设置页里的登录命令（默认，维持原行为）；\n"
     "关：批次直接发下载命令——换账号时先关掉它，到 steamcmd 终端手动"
     "执行 login 另一个账号再开批次；没登录的话批次会停在「需要登录」"
     "等你处理",
     "bool"),

    # 批次前清缓存开关（决策 100）：默认开 = RimSort 同款对策同默认
    ("steamcmd_clear_cache_before_batch", "批次前清理 steamcmd 缓存",
     "开：每批下载开始前自动清空 steamcmd 的下载缓存（depotcache 与 "
     "workshop\\downloads）——缓存会把已删除的 mod 原样装回下载目录"
     "（本工具删过的 mod 在之后某批下载时\"复活\"进账本），清掉即断根；"
     "对部分\"下载失败\"也有改善。代价：缓存里的共享数据块会重新下载，"
     "短时间反复安装同一批 mod 时多耗流量。默认开（与 RimSort 同默认）",
     "bool"),

    # 控制台自动弹出开关（决策 58 的就地开关 console_auto_show 提进设置页）
    ("console_auto_show", "有新消息时弹出控制台",
     "开：产生新日志时，底部的控制台自动弹出让你看见（默认，原行为）；\n"
     "关：控制台不再自动弹出——非当前标签页会亮红点提醒，想看时自己点开",
     "bool"),

    # 高级筛选关窗清空开关（决策 97）：默认开 = 现状（关窗即清空，原行为）
    ("advsearch_autoclear", "关闭高级筛选时自动清空条件",
     "开：关闭高级筛选窗口即清空全部条件，列表恢复全量（默认，原行为）；\n"
     "关：关窗保留条件，重开窗口接着用——mod 库页工具条的「高级筛选 ✕」"
     "指示按钮和窗口里的【清除全部】随时可手动清",
     "bool"),

    # 本地标题提醒词表（桶C，维护版）：标题检测页按词表扫本地标题，
    # 命中即黄字提醒。留空 = 停用；默认值与
    # workflows/exceptionFlow.DEFAULT_TITLE_KEYWORDS 同文
    ("local_title_warn_keywords", "本地标题提醒关键词",
     "mod 库里标题含这些词的条目，在【标题检测】页黄字提醒（离线、"
     "毫秒级）。中英文逗号或空格分隔（中文逗号保存时自动转半角），"
     "大小写不影响匹配；留空 = 停用。默认：abandoned, deprecated, "
     "discontinued, unmaintained, outdated——像 \"Abandoned Mines\" "
     "这类地图名也会被命中，只是提醒不是判定，误报就把对应词删掉",
     "text"),
]


class SettingsPage(QWidget):

    def __init__(self, settings: AppSettings,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._edits: dict[str, QLineEdit] = {}
        # 多行文本（决策 95）：登录命令一项。QPlainTextEdit 的读写
        # 接口与 QLineEdit 不同（toPlainText/setPlainText），分开收，
        # 统一读取走 _field_text 助手
        self._multi_edits: dict[str, QPlainTextEdit] = {}
        self._checks: dict[str, QCheckBox] = {}
        self._kinds: dict[str, str] = {}
        self._statuses: dict[str, QLabel] = {}
        self._build_ui()
        self._load_to_ui()
        # 切换信号在初始值就位后再接：_load 里 setCurrentIndex 不会
        # 触发落盘与重应用（构造期不空跑一次主题）
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("全局设置")
        title.setStyleSheet(f"font-size: {font_px(18)}px; font-weight: 600;")
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

            if kind == "text_multi":
                # 多行文本（决策 95）：登录命令一行一条、可存多个
                # 账号。读写接口与 QLineEdit 不同，单独收进
                # _multi_edits，统一读取走 _field_text 助手
                edit = QPlainTextEdit()
                edit.setFixedHeight(72)   # 约四行：多个账号一眼可见
                edit.textChanged.connect(self._refresh_states)
                self._multi_edits[key] = edit
            else:
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
                    lambda _=False, e=edit, f=(kind == "file"):
                    self._browse(e, f))
                h = QHBoxLayout()
                h.setContentsMargins(0, 0, 0, 0)
                h.addWidget(edit, 1)
                h.addWidget(browse)
                v.addLayout(h)
            else:
                v.addWidget(edit)

            desc = QLabel(hint)
            desc.setWordWrap(True)   # 必须换行，否则整句长度变成窗口最小宽度
            desc.setStyleSheet(f"color: gray; font-size: {font_px(11)}px;")
            v.addWidget(desc)

            status = QLabel("")
            status.setWordWrap(True)
            self._statuses[key] = status
            v.addWidget(status)
            form.addRow(label, field)

        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # 界面主题三态（决策 66）：独立于 _FIELDS 校验体系（无需校验、
        # 且即时生效不走"保存"），一行横排放按钮区上方
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("界面主题："))
        self._theme_combo = QComboBox()
        self._theme_combo.addItem("跟随系统", "auto")
        self._theme_combo.addItem("深色", "dark")
        self._theme_combo.addItem("亮色", "light")
        self._theme_combo.setToolTip(
            "跟随系统：Windows 深浅色切换时软件跟着变（推荐）；\n"
            "深色 / 亮色：固定配色，切换即时生效并记住")
        theme_row.addWidget(self._theme_combo)
        theme_row.addStretch(1)
        root.addLayout(theme_row)

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
            edit.setText(str(path))   # Path 的字符串形式，跟原写法等价但更直白

    # ---------- 数据 ----------
    def _load_to_ui(self) -> None:
        for key, edit in self._edits.items():
            edit.setText(self._settings.get(key))
        # 多行文本（决策 95）：整块原样回填——库里存的就是逐行
        # 清理过的（见 _save），这里不必再加工
        for key, multi in self._multi_edits.items():
            multi.setPlainText(self._settings.get(key))
        # 开关项：存 "1"/"0"；缺键/其他值按开（默认全显示）
        for key, cb in self._checks.items():
            cb.setChecked(self._settings.get(key) != "0")
        # 记住打开时的开关状态：保存时对比，才知道"这次有没有动列显示"
        self._bools_at_load = {k: cb.isChecked()
                               for k, cb in self._checks.items()}
        # 主题三态初始值（缺键/非法值回落 auto）
        mode = str(self._settings.get("theme_mode") or "auto").strip()
        i = self._theme_combo.findData(mode)
        self._theme_combo.setCurrentIndex(max(i, 0))

    def _field_text(self, key: str) -> str:
        """读字段现值：单行走 QLineEdit.text()，多行走 toPlainText()。
        _refresh_states 等不关心控件差异的地方统一从这走。"""
        multi = self._multi_edits.get(key)
        if multi is not None:
            return multi.toPlainText()
        edit = self._edits.get(key)
        return edit.text() if edit is not None else ""

    @staticmethod
    def _number_ok(text: str) -> bool:
        """正整数才算过——间隔、次数、天数、条数都没有 0 和负数的意义。"""
        try:
            return int(text) > 0
        except ValueError:
            return False

    def _refresh_states(self) -> None:
        for key, status in self._statuses.items():
            text = self._field_text(key).strip()
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
            elif kind in ("text", "text_multi"):
                # 文本项没有"对错"概念，填了就算数——
                # 内容对不对只有 steamcmd 用起来才知道
                valid = True
                ok_text = "已填写（使用时原样传递，不校验内容）"
                bad_text = ok_text
            else:   # number
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
                edit.setText(value)   # 界面同步显示保存后的真实值
            if key == "local_title_warn_keywords":
                # 中文逗号归一半角：界面允许顺手打中文逗号，入库
                # 口径保持单一——消费端（标题检测页，本版已落位）
                # 按半角逗号/空白切分；手改 JSON 绕过这里的由消费端兜底
                value = value.replace("，", ",")
            self._settings.set(key, value)

        # 多行文本（决策 95）：逐行 strip、丢空行后按 \n 拼回入库——
        # 下游（批次取首行、终端下拉框）拿到的每行都是干净命令
        for key, multi in self._multi_edits.items():
            lines = [ln.strip() for ln in multi.toPlainText().splitlines()]
            self._settings.set(key, "\n".join(ln for ln in lines if ln))

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
        msg = (f"已保存 {time.strftime('%H:%M:%S')} → "
               f"{self._settings.path}")
        if changed:
            msg += "\n列显示有改动：回 mod 库页点【刷新】生效"
        self._saved_label.setText(msg)
        self._saved_label.setStyleSheet("color: #46a758;")

    def _on_theme_changed(self) -> None:
        """主题三态（决策 66）：改动即落盘并重应用——qdarktheme 支持
        运行中切换，不必重启。它不属于"点保存才生效"的字段体系（那套
        面向要校验的配置项），这里给即时反馈；_save/_reset 不碰它。"""
        mode = self._theme_combo.currentData()
        self._settings.set("theme_mode", mode)
        self._settings.save()
        reapply_theme()
        self._saved_label.setText(
            f"主题已切换为「{self._theme_combo.currentText()}」并保存")
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
        # 多行文本（决策 95）：清空 = 默认值（默认本来就是空）
        for multi in self._multi_edits.values():
            multi.clear()
        for cb in self._checks.values():
            cb.setChecked(True)
        self._saved_label.setText("已恢复默认值，点【保存】写入文件")
        self._saved_label.setStyleSheet("color: gray;")
