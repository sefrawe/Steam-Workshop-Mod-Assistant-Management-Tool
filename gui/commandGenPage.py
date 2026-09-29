"""命令生成页
"""
r"""把当前游戏勾选的 mod 拼成 steamcmd 下载命令，交给用户手动粘贴执行
（gui/commandGenPage.py）。

【本页是干什么的】
从上到下四块：
1) 游戏信息行：当前档案 + "重新载入"按钮；
2) 用法提示行：三步说明 + "复制登录命令"按钮（内容来自设置页，原样复制）；
3) 三组可折叠分区（需要更新 / 已收录 / 已最新）+ 页内筛选框：
   分区标题常显"勾 x / 共 n"，收起也看得到这组勾了多少；
4) 底部：只读预览框（实时显示将要复制的内容）+ 统计 + 复制 / 另存按钮。

使用方法（也写在界面提示里）：
① 自己打开 steamcmd 并登录（账号必须拥有该游戏，否则下载会报错）；
② 在本页勾选 mod，点"复制命令"；
③ 粘贴到 steamcmd 窗口回车。多行文本会被终端一行一行依次执行，
   前一条下载完才开始下一条，所以 mod 数量多少都没有限制。

【v2.41.1 显示修复（对照 collapsibleSection.py 原文后定稿）】
v2.41 首版显示翻车，根因两条，都记进本版注释防再犯：
- 主凶：分区容器（整页滚动区的内容页）末尾没加 addStretch(1)。
  QScrollArea + widgetResizable 会把内容页拉高到视口高度，三个
  分区都是默认尺寸策略、跟着被拉高；共享件内部是"固定高度的内容
  区"，被拉高的多余空间悬在布局里 → 大片空白、工具行漂移。
  修复 = 容器末尾补 stretch，分区保持自然高度；
- 从犯：行高按 26px/行拍脑袋，实际每行（复选框 + ↗ 按钮）约 28px，
  高度数学不准 → 内滚区被挤、首行被裁。修复 = 每行固定 28px、
  分区高度按精确公式算（行数 × 28 + 工具行 28 + 余量），不再猜。

【与共享件的契约（照 collapsibleSection.py 原文，勿凭记忆）】
- set_content(控件, 高度)：每分区只调一次；共享件会把控件
  setFixedHeight(高度)——所以高度必须一次性算准，"内容多时靠
  控件内部滚动"（本页 = 内容区里的 QScrollArea 承担内滚）；
- set_title / set_expanded / expand_changed(bool)：标题常显统计、
  程序性收展、收展状态登记，用法不变。

【本版（打包前 todo 5）UI 大改清单】
- 三组清单从常开 GroupBox 升级为可折叠分区（共享件单源 = 决策
  69⑨ 同件）：默认收展 = 行数 ≤15 展开、大组收起；用户手动收展
  记在页面级字典，"重新载入"重建后照旧恢复（分区是重建产物，
  状态不能跟着控件一起被销毁——与异常页桶④下拉同一落点理由）；
- 分区标题常显统计："已收录（勾 97 / 共 97）"——收起也看得到；
- 页内筛选框：按标题或编号即时过滤（只隐藏不清勾选）；
  全选/清空 = 只作用于当前筛出的行（用筛选时登记的 visible 布尔，
  绝不读 isVisible——分区收起时控件全部"不可见"，按控件可见性
  收集会在折叠状态下全军覆没）；
- "已收录"组内置白话说明：正常排队不是异常，下完去扫描本地转正
  （todo 6"事实进提示"清一角 + 用户拍板的安家决定）；
- focus_ids 落勾后自动摊开含勾选的分区（决策 22⑤）；
- 刻意不做排序：命令顺序由勾选集合按"组序 → 行序"收集，与显示
  顺序无关（异常处理页同款取舍）。

【合同不变（MainWindow 与其他页的接口）】
- set_game(game)：切档案时调用，None 同样受理；
- focus_ids(mod_ids)：右键跳转时调用，取消全部勾选只勾传入 id；
- 勾选收集顺序（需要更新 → 已收录 → 已最新，组内按编号）不变；
- 分组、拼命令全在 core/commandBuilder.py（纯函数、可单测），
  本页只管界面和落盘；只读 ModRepository，GUI 层零 SQL；不联网；
- 复制 / 另存仍向 operations_log 记完整命令原文 result="generated"；
- 另存 txt 仍 utf-8、默认存系统"文档"目录（理由见 _on_save 注释）。

【无后台线程】本页全部动作同步毫秒级，不需要 shutdown 收尾。
"""
from datetime import date
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.commandBuilder import build_copy_text, group_mods
from gui.collapsibleSection import CollapsibleSection
from gui.consolePanel import LogBus

# 组键 not_downloaded 与 core/commandBuilder.group_mods 的返回键一致，别改；
# 界面标题改"已收录"（T19⑦）：与 mod 库页状态列同词。语义 = 库里已登记、
# 本地还没有下载记录（含手动确认入账、尚未用 steamcmd 下载过的 mod）
_GROUP_ORDER = [
    ("needs_update", "需要更新"),
    ("not_downloaded", "已收录"),
    ("up_to_date", "已最新"),
]

# 登录命令在设置页里的键名（appSettings.DEFAULTS / settingsPage._FIELDS 同名）
_LOGIN_CMD_KEY = "steamcmd_login_cmd"

# ---- 分区高度精确数学（v2.41.1：不再拍脑袋）----
# 每行固定 28px（复选框 + ↗ 按钮的实际高度，行控件 setFixedHeight 锁死）；
# 工具行（全选/清空）固定 28px；再加布局间距与少量余量。
# 分区内容高度 = min(上限, max(下限, 28×行数 + 38))，"已收录"组因顶部
# 说明行额外 +40（说明行字数固定，两行以内放得下，超出只是内容区
# 里多滚一点，不影响外布局）。
_COLLAPSE_IF_OVER = 15   # 超过这么多行默认收起（用户手动收展后以用户为准）
_ROW_H = 28
_TOOLS_H = 28
_SEC_MIN_H = 120
_SEC_MAX_H = 400
_TIP_ALLOWANCE = 40      # "已收录"说明行的固定高度预算

_USAGE_TEXT = (
    "使用方法：① 自己打开 steamcmd 并登录（账号必须拥有该游戏）→ "
    "② 在下面勾选 mod，点右下角“复制命令”→ "
    "③ 粘贴到 steamcmd 窗口回车，命令会一行一行依次执行。"
)


class CommandGenPage(QWidget):
    """命令生成页：选 mod → 预览 → 复制 / 另存。"""

    def __init__(self, repo, settings, parent=None, *, log=None):
        super().__init__(parent)
        self._repo = repo        # ModRepository：读 mod 清单、写 operations_log
        self._settings = settings  # AppSettings：读登录命令
        # 日志总线：MainWindow 注入；单独调试本页时兜底建一个
        #（消息没人显示但不崩，"发进空气 = 无操作"）
        self._log = log if log is not None else LogBus()
        self._game = None        # 当前游戏档案（切游戏时由 MainWindow 调 set_game 换进来）

        # ---- 三组分区的登记表（每次"重新载入"重建）----
        # 勾选与行控件：{组键: [{"mod_id","title","widget","checkbox","visible"}, ...]}
        # visible = 是否通过当前筛选（自己登记，绝不读 isVisible——
        # 分区收起时控件全部"不可见"，读控件会全军覆没，见 _set_group）
        self._rows = {key: [] for key, _ in _GROUP_ORDER}
        self._sections = {}      # 组键 → CollapsibleSection（改标题 / 程序性收展用）
        self._filter_empty = {}  # 组键 → 筛空占位行
        # 用户手动收展记忆：{组键: True/False}，跨"重新载入"重建恢复；
        # 首次见到某组（字典里还没有）才按行数套默认规则
        self._sec_state: dict[str, bool] = {}

        self._init_ui()

    # ---------------- 搭骨架 ----------------

    def _init_ui(self):
        # v2.41.4 整页滚动壳：只包第 1~3 块——预览框与【复制命令】
        # 留在壳外固定（复制是最高频动作，不能随滚动沉底）。
        # 与中部三分区的内滚两层各管各的：窗口高时外层零滚动，
        # 矮时页级滚动条出现，分区照旧内滚
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        page_scroll = QScrollArea(self)
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page_body = QWidget(page_scroll)
        page_scroll.setWidget(page_body)
        outer.addWidget(page_scroll, 1)
        root = QVBoxLayout(page_body)

        # 第 1 块：游戏信息 + 重新载入
        top = QHBoxLayout()
        self._lbl_game = QLabel("（还没有选择游戏）")
        btn_reload = QPushButton("重新载入")
        btn_reload.setToolTip(
            "从数据库重新读取当前游戏的 mod 清单并重建勾选状态"
            "（别的页面改过备注/状态后，点这里同步最新数据）")
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

        # 第 2.5 块：页内筛选框——三组清单共用一个过滤词
        filter_row = QHBoxLayout()
        lbl_filter = QLabel("筛选")
        lbl_filter.setStyleSheet("color: gray;")
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(
            "按 mod 标题或编号过滤下面的清单…（即时生效；只隐藏行，"
            "不清勾选；全选/清空只作用于筛出的行）")
        self._filter.setClearButtonEnabled(True)
        self._filter.setToolTip(
            "三组清单同时过滤：标题包含或编号包含都算命中；"
            "清空输入框（或点右侧 ×）恢复全部行")
        self._filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(lbl_filter)
        filter_row.addWidget(self._filter, 1)
        root.addLayout(filter_row)

        # 第 3 块：三个可折叠分区（滚动区兜底，每次刷新都重建）
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
        outer.addWidget(self._preview)  # 壳外常驻：矮窗口也能边勾边看命令

        bottom = QHBoxLayout()
        self._lbl_summary = QLabel("已勾选 0 个")
        btn_save = QPushButton("另存为 txt")
        btn_save.setToolTip("把当前勾选对应的命令存成 txt 文件，内容与复制的完全一致")
        btn_copy = QPushButton("复制命令")
        btn_copy.setToolTip("把当前勾选对应的命令复制进剪贴板，去 steamcmd 窗口粘贴回车即可")
        btn_copy.setDefault(True)
        btn_save.clicked.connect(self._on_save)
        btn_copy.clicked.connect(self._on_copy)
        bottom.addWidget(self._lbl_summary, 1)
        bottom.addWidget(btn_save)
        bottom.addWidget(btn_copy)
        outer.addLayout(bottom)  # 壳外常驻：【复制命令】永远一键可达

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
        """从 mod 库页 / 异常处理页右键跳过来时调用：取消全部勾选，
        只勾选传入的这些 id。找不到的 id（已删除/失败的 mod 不在清单里）
        在控制台说明。含有勾选的分区自动摊开——跳转来意要看得见
        （决策 22⑤），别让用户勾完了对着的却是收起的组。"""
        id_set = {int(i) for i in mod_ids}
        found = set()
        hit_groups = []
        for key, _title in _GROUP_ORDER:
            hit = False
            for row in self._rows.get(key, []):
                checked = row["mod_id"] in id_set
                row["checkbox"].setChecked(checked)  # 顺路完成"清空其余"
                if checked:
                    found.add(row["mod_id"])
                    hit = True
            if hit:
                hit_groups.append(key)
        missing = id_set - found
        if missing:
            self._log.warn(
                f"这些 mod 不在可下载清单中（可能已删除/失败）：{sorted(missing)}")
        for key in hit_groups:
            self._sec_state[key] = True  # 登记用户"应该看到展开"的选择
            sec = self._sections.get(key)
            if sec is not None:
                sec.set_expanded(True)

    def _reload(self):
        """从数据库重读当前游戏的 mod，按三组重建可折叠分区。
        勾选按默认规则重置（需要更新 + 已收录 默认勾上）——"重建勾选
        状态"是【重新载入】按钮的既有语义；收展记忆不受影响。"""
        if not self._require_game():
            return
        # 游戏主键就是 app_id（与 mod 库页同款调用）
        mods = self._repo.list_mods(self._game.app_id)
        groups = group_mods(mods)
        self._clear_body()
        for key, title in _GROUP_ORDER:
            self._make_section(key, title, groups[key])
        # v2.41.1 主修复：容器末尾必须补 stretch。
        # 没有它，QScrollArea(widgetResizable) 把内容页拉高到视口高度、
        # 三个分区（默认尺寸策略）跟着被拉高，共享件里"固定高度的内容
        # 区"周围悬出大片空白——首版显示翻车的根因。
        # stretch 由 _clear_body 一并清掉（spacer 不是 widget，跳过即可）
        self._body_layout.addStretch(1)
        # 重建完按当前筛选词过一遍（筛选框不因重建清空），再刷统计
        self._apply_filter()
        self._refresh_stats()

    # ---------------- 分区构建 ----------------

    def _section_height(self, key: str, n_rows: int) -> int:
        """分区内容区的固定高度（共享件 set_content 会 setFixedHeight）。
        精确数学：行数×28 + 工具行 28 + 布局余量，再夹在上下限之间；
        "已收录"组因顶部说明行 +40。空组建 56（只放一行静态说明）。"""
        if n_rows == 0:
            return 56
        h = _ROW_H * n_rows + _TOOLS_H + 10
        h = min(_SEC_MAX_H, max(_SEC_MIN_H, h))
        if key == "not_downloaded":
            h += _TIP_ALLOWANCE
        return h

    def _make_section(self, key, title, rows):
        """建一个可折叠分区：内容区自上而下 =
        说明行（仅已收录组）→ 工具行（全选/清空）→ 筛空占位行 →
        行清单（固定高度内滚区）。行登记进 self._rows 供筛选与收集。
        每行 setFixedHeight(28)——高度数学的前提，动它先改 _ROW_H。"""
        content = QWidget(self)
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)

        # ---- "已收录"白话说明行（todo 6"事实进提示"的一角）----
        # 放内容区顶部：展开才可见，随分区收起一起消失，不占外部空间。
        # 高度预算已计入 _section_height 的 +40
        if key == "not_downloaded":
            tip = QLabel(
                "「已收录」= 已入库、还没下载的正常排队（与 mod 库页状态列"
                "同词），不是异常。默认已全部勾上——点右下角【复制命令】"
                "去 steamcmd 执行；下完到【mod 库】页点【扫描本地】即自动"
                "转为「已下载」。", content)
            tip.setWordWrap(True)  # 踩坑⑨
            tip.setStyleSheet("color: gray; font-size:12px;")
            cv.addWidget(tip)

        if not rows:
            # 空组：不给工具行也不给内滚区（全选/清空无意义），
            # 一行静态说明 + 56px 固定高度即可
            lbl = QLabel("（本组当前没有条目）", content)
            lbl.setStyleSheet("color: gray;")
            cv.addWidget(lbl)
        else:
            # ---- 工具行：全选 / 清空（只作用"当前筛出的行"，见 _set_group）----
            tools = QWidget(content)
            tools.setFixedHeight(_TOOLS_H)
            th = QHBoxLayout(tools)
            th.setContentsMargins(0, 0, 0, 0)
            btn_all = QPushButton("全选", tools)
            btn_all.setToolTip(
                "勾选本组当前显示的 mod（有筛选词时只作用于筛出的行；"
                "没有筛选 = 全组）")
            btn_none = QPushButton("清空", tools)
            btn_none.setToolTip(
                "取消本组当前显示的 mod 的勾选（有筛选词时只作用于筛出的行）")
            # 用 k=key 把"当前组名"固定住，避免按钮触发时读到循环变量的最后值
            btn_all.clicked.connect(
                lambda _=False, k=key: self._set_group(k, True))
            btn_none.clicked.connect(
                lambda _=False, k=key: self._set_group(k, False))
            th.addStretch(1)
            th.addWidget(btn_all)
            th.addWidget(btn_none)
            cv.addWidget(tools)

            # ---- 筛空占位行：筛选激活且本组零命中时显示（决策 22④）----
            empty_lbl = QLabel(
                "（没有匹配当前筛选的行——清空上方筛选框即可全部显示）",
                content)
            empty_lbl.setStyleSheet("color: gray;")
            empty_lbl.setVisible(False)
            cv.addWidget(empty_lbl)
            self._filter_empty[key] = empty_lbl

            # ---- 行清单（放进内滚区：行多时靠它内部滚动，
            # 分区高度不随内容涨——这正是共享件 set_content 的契约）----
            rows_widget = QWidget(content)
            rv = QVBoxLayout(rows_widget)
            rv.setContentsMargins(0, 0, 0, 0)
            rv.setSpacing(0)
            for m in rows:
                # 标题还没补全（没跑过更新检测）时显示"（无标题）"而不是
                # "None"——和 mod 库页列表的兜底口径保持一致
                cb = QCheckBox(f"{m.mod_id} {m.title or '（无标题）'}", rows_widget)
                cb.setToolTip(f"{m.mod_id}　{m.title or '（无标题）'}")
                # 推荐勾选：需要更新 + 已收录 默认勾上；已最新默认不勾
                cb.setChecked(key in ("needs_update", "not_downloaded"))
                cb.toggled.connect(self._refresh_stats)
                # T25：每行带 ↗ 直达工坊页面——选 mod 做某事的清单
                # 必须能顺手查详情
                row_w = QWidget(rows_widget)
                row_w.setFixedHeight(_ROW_H)  # 高度数学的前提
                rh = QHBoxLayout(row_w)
                rh.setContentsMargins(0, 0, 0, 0)
                rh.addWidget(cb, 1)
                btn_open = QPushButton("↗", row_w)
                btn_open.setFixedWidth(28)
                btn_open.setToolTip("在浏览器打开该 mod 的创意工坊页面")
                btn_open.setEnabled(bool(m.url))
                if m.url:
                    btn_open.clicked.connect(
                        lambda _=False, u=m.url:
                        QDesktopServices.openUrl(QUrl(u)))
                rh.addWidget(btn_open)
                rv.addWidget(row_w)
                # 登记这行：筛选/收集全走登记表，不按行号反查（id 才稳）
                self._rows[key].append({"mod_id": m.mod_id,
                                        "title": m.title or "",
                                        "widget": row_w,
                                        "checkbox": cb,
                                        "visible": True})
            inner = QScrollArea(content)
            inner.setWidgetResizable(True)
            inner.setFrameShape(QScrollArea.Shape.NoFrame)
            inner.setWidget(rows_widget)
            cv.addWidget(inner, 1)

        # ---- 默认收展：用户选过听用户的，没选过按行数套规则 ----
        if key in self._sec_state:
            expanded = self._sec_state[key]
        else:
            expanded = len(rows) <= _COLLAPSE_IF_OVER
        self._sec_state[key] = expanded

        section = CollapsibleSection(title, self)
        # set_content 每分区只调一次；高度按精确公式（v2.41.1）
        section.set_content(content, self._section_height(key, len(rows)))
        # expand_changed 先连、再设初值——此后用户的手动收展都会登记
        # （k=key 固定组名，理由同上）
        section.expand_changed.connect(
            lambda on, k=key: self._sec_state.__setitem__(k, on))
        section.set_expanded(expanded)
        self._sections[key] = section
        self._body_layout.addWidget(section)

    def _clear_body(self):
        """清空中部滚动区里的所有旧分区，准备重建。
        收展记忆 self._sec_state 刻意不清——它要跨重建存活。
        _reload 末尾加的 stretch 是 spacer（不是 widget），在这里
        被 takeAt 取出后 w 为 None，自然跳过，由下次 _reload 重加。"""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows = {key: [] for key, _ in _GROUP_ORDER}
        self._sections = {}
        self._filter_empty = {}

    # ---------------- 筛选与勾选 ----------------

    def _apply_filter(self, *_):
        """筛选框唯一出口：三组同时过滤，行只隐藏不删除；
        顺手刷新标题统计（"筛出 m"）与筛空占位行。"""
        q = self._filter.text().strip().casefold()
        for key, _title in _GROUP_ORDER:
            hits = 0
            for row in self._rows[key]:
                ok = (not q
                      or q in row["title"].casefold()
                      or q in str(row["mod_id"]))
                row["visible"] = ok          # 登记判定结果（_set_group 用）
                row["widget"].setVisible(ok)  # 只隐藏，勾选原样保留
                if ok:
                    hits += 1
            empty = self._filter_empty.get(key)
            if empty is not None:
                empty.setVisible(bool(q) and hits == 0)
        self._refresh_stats()

    def _set_group(self, key, checked):
        """一键勾选/清空"当前筛出的行"。
        绝不读控件的 isVisible()：分区收起时行控件全部不可见，
        按控件可见性收集会在折叠状态下清成空——用筛选时登记的
        visible 布尔（它不受收展影响）。"""
        for row in self._rows[key]:
            if row["visible"]:
                row["checkbox"].setChecked(checked)

    def _refresh_stats(self):
        """刷新分区标题统计、底部总统计和预览框（勾选框一变就走这里，
        一处算清别处只读）。"""
        q = self._filter.text().strip().casefold() if self._filter else ""
        total = 0
        parts = []
        for key, title in _GROUP_ORDER:
            rows = self._rows.get(key, [])
            n = sum(1 for r in rows if r["checkbox"].isChecked())
            total += n
            label = f"{title}（勾 {n} / 共 {len(rows)}"
            if q:
                label += f" · 筛出 {sum(1 for r in rows if r['visible'])}"
            label += "）"
            sec = self._sections.get(key)
            if sec is not None:
                sec.set_title(label)  # 标题常显：收起也看得到勾了多少
            parts.append(f"{title} {n}")
        self._lbl_summary.setText(f"已勾选 {total} 个（{' + '.join(parts)}）")
        self._preview.setPlainText(self._current_text() or "")

    def _checked_ids(self):
        """按"需更新 → 未下载 → 已最新"的顺序收齐所有勾选的 mod id。
        遍历登记表（组序固定、组内按编号）——与显示筛选无关，
        被筛掉的行只要勾着就照常进命令。"""
        ids = []
        for key, _title in _GROUP_ORDER:
            for row in self._rows.get(key, []):
                if row["checkbox"].isChecked():
                    ids.append(row["mod_id"])
        return ids

    def _current_text(self):
        """当前勾选对应的命令文本；没选游戏时返回 None。"""
        if self._game is None:
            return None
        return build_copy_text(self._game.app_id, self._checked_ids())

    def _require_game(self) -> bool:
        """动作入口的公共守卫（T15 批 2）：没选档案时弹窗指路，不只写
        底部日志——控制台可能关着，"点了没反应"比日志更常见。
        与 mod 库页 v2.29 同一口径。"""
        if self._game is not None:
            return True
        QMessageBox.information(
            self, "请先选择档案",
            "命令按当前游戏档案生成——请先在左上角添加或选择游戏档案。")
        return False

    # ---------------- 三个动作 ----------------

    def _on_copy_login(self):
        """复制设置页里的登录命令，原样进剪贴板（我们不解析、不拼装内容）。"""
        cmd = str(self._settings.get(_LOGIN_CMD_KEY) or "").strip()
        if not cmd:
            QMessageBox.information(
                self, "还没填登录命令",
                "设置页 → steamcmd 登录命令 还没有填写。\n"
                "填好后回来点【复制登录命令】即可。")
            self._log.warn("登录命令为空：未复制，请先到设置页填写")
            return
        QApplication.clipboard().setText(cmd)
        self._log.ok("已复制登录命令，打开 steamcmd 后先粘贴这条回车")

    def _on_copy(self):
        """把勾选的下载命令复制进剪贴板。"""
        if not self._require_game():
            return
        ids = self._checked_ids()
        if not ids:
            QMessageBox.information(self, "提示", "还没勾选任何 mod。")
            return
        text = self._current_text()  # 上面已挡掉无游戏的情况，这里必非 None
        QApplication.clipboard().setText(text)
        self._log.ok(f"已复制 {len(ids)} 条下载命令，去 steamcmd 窗口粘贴回车即可")
        self._log_generated(text)

    def _on_save(self):
        """把勾选的下载命令另存为 txt 文件，内容与复制到剪贴板的完全一致。"""
        if not self._require_game():
            return
        ids = self._checked_ids()
        if not ids:
            QMessageBox.information(self, "提示", "还没勾选任何 mod。")
            return
        default_name = f"下载命令_{date.today():%Y%m%d}.txt"
        # 默认存到系统"文档"目录，而不是游戏的下载目录（download_dir）。
        # 原因（两条，本质都是"别把命令文件混进 mod 内容"）：
        # 1) download_dir 指向 steamcmd 的 content\<appid>\，这棵目录树
        #    有一条硬规矩：里面只放以 mod 编号命名的文件夹。将来核验页
        #    做账实对比时，编号之外的文件会被当成异常内容报出来——
        #    自己生成的 txt 自己触发警报，纯添乱；
        # 2) 游戏侧的 mod 目录通过链接指到这里，放进去的 txt 会跟着
        #    出现在游戏目录里，可能被游戏或启动器扫到。
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        start_dir = docs if docs else str(Path.home())
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为下载命令",
            str(Path(start_dir) / default_name),
            "文本文件 (*.txt)",
        )
        if not path:
            return
        text = self._current_text()
        Path(path).write_text(text, encoding="utf-8")
        self._log.ok(f"已保存 {len(ids)} 条下载命令：{path}")
        self._log_generated(text)

    # ---------------- 记账 ----------------

    def _log_generated(self, command_text):
        """往 operations_log 记一笔"已生成"，记完整命令原文（记账粒度）。
        为什么记原文不记"N 条"统计：
        - 审计可回溯：将来想知道"某个 mod 为什么被下载过"，
          翻这条记录能直接看到当时给出的每一行命令；
        - 可以重放：记下来的就是能直接粘进 steamcmd 的命令本身，
          纯 workshop_download_item 行。登录命令绝不进这张表（红线），
          所以这里天生安全。
        repo 约定：add_operation(命令文本) 先登记拿 op_id，
        finish_operation(op_id, result=...) 事后回填——
        本页没有真正的执行环节，登记和回填连着做，
        result 用 "generated" 标记"已生成、未执行"。
        "这次是复制还是另存、另存到了哪"属于写给人的过程叙述，
        控制台日志里已经说过（数据库存结构化事实、控制台说人话，
        两层不混），数据库不再重复记。
        记账失败不影响已完成的复制/另存，但必须在控制台喊出来
        （不静默吞错误）。
        """
        try:
            op_id = self._repo.add_operation(command_text)
            self._repo.finish_operation(op_id, result="generated")
        except Exception as exc:
            self._log.error(f"写 operations_log 失败（复制/另存本身不受影响）：{exc}")
