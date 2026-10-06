"""界面主题
"""
r"""gui/theme.py —— 主题应用。

三态存设置键 theme_mode（appSettings.DEFAULTS，默认 "auto"）：
- auto = 跟随系统：装了 pyqtdarktheme-fork就用它的 auto 精确跟随；
  未装库时 Qt 6.5+ 在 Windows 深色模式下自动深色（现状行为，静默降级）；
- dark / light = 固定深/浅色，配色由 pyqtdarktheme-fork 提供，本模块
  （亮色首版即它的 light；精修将来在它的变量上做）。
★v2.28 例外：斑马纹双色 _ZEBRA_COLORS 由本模块自持——QSS 主题库下
app.palette() 与样式表脱节（踩坑51），模型给色的事实源必须是显式色表；
QSS 未生效时 zebra_colors() 返回 None，宁无斑马纹不猜色。


setup_theme 可重复调用——运行中切换不重启；"装了但应用失败"
才打印提示。启动路径：main.py 调 apply_theme(app)（读设置）；
设置页切换后调 reapply_theme()（无参，内部自取 QApplication）。
"""
_KEY = "theme_mode"
_VALID = ("auto", "dark", "light")


def current_mode() -> str:
    """读当前主题模式；缺键/非法值一律回落 auto（= 现状行为）。"""
    try:
        from core.appSettings import AppSettings
        v = str(AppSettings().get(_KEY) or "").strip().lower()
    except Exception:
        return "auto"
    return v if v in _VALID else "auto"


def apply_theme(app) -> str:
    """启动时应用主题（main.py 调一次）；返回设置里的模式。"""
    return _setup(app, current_mode())


def reapply_theme() -> str:
    """设置页切换后重应用：setup_theme 可重复调用，无需重启。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:  # 理论不可达：设置页活着就有 QApplication
        return current_mode()
    return _setup(app, current_mode())
def _setup(app, mode: str) -> str:
    global _ACTIVE_MODE
    try:
        import qdarktheme
        # 注意：包名 pyqtdarktheme(-fork)，import 名是 qdarktheme
    except ImportError:
        if mode != "auto":
            # 没库还想固定深/浅：说一声再静默降级
            import sys
            print("[theme] 未找到可用的 qdarktheme 模块，无法固定深/浅色，"
                  "改用系统配色（pip install pyqtdarktheme-fork）", file=sys.stderr)
        _ACTIVE_MODE = None  # QSS 未生效：斑马纹按"无"处理（踩坑51）
        return mode  # auto = 交给 Qt 自带跟随
    try:
        qdarktheme.setup_theme(mode)
    except Exception as exc:
        import sys
        print(f"[theme] 主题应用失败（{mode}），改用系统配色：{exc}", file=sys.stderr)
        _ACTIVE_MODE = None  # 应用失败同上
        return mode
    _ACTIVE_MODE = mode  # QSS 真实生效的档位，zebra_colors 的唯一依据
    return mode

# —— 斑马纹双色（v2.28）——
# QSS 主题库下 app.palette() 与样式表是两套系统、可以脱节（实证：亮色下
# Base 残留深色、AlternateBase 已换浅色，一张表两代混血）——palette 从此
# 不作颜色事实源。斑马纹由本表自持；色值用文末验收节的探针按 fork 实际
# 底色校准。
_ZEBRA_COLORS = {
    "light": ("#ffffff", "#f2f2f2"),
    "dark": ("#1e1e1e", "#272727"),  # 初值近似，按探针/观感校准
}
_ACTIVE_MODE: str | None = None  # QSS 实际生效的模式；None = 未生效


def zebra_colors() -> tuple[str, str] | None:
    """斑马纹双色 (基础色, 交替色)；QSS 未生效时返回 None（= 不画斑马纹）。

    模型按行奇偶取用（modListModel.data 消费）。auto 档用
    styleHints().colorScheme() 即时判定系统深浅，与 qdarktheme 的 auto
    同源；系统深浅切换时 fork 自动重应用 → QSS 换新触发重绘 → data()
    重新取色，斑马纹跟手。QSS 未生效（库缺席 / 应用失败）返回 None。
    """
    if _ACTIVE_MODE is None:
        return None
    mode = _ACTIVE_MODE
    if mode == "auto":
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication
            scheme = QGuiApplication.styleHints().colorScheme()
            mode = "dark" if scheme == Qt.ColorScheme.Dark else "light"
        except Exception:
            mode = "light"  # 判定不了按浅色（Windows 实测基本可判定）
    return _ZEBRA_COLORS[mode]
def system_prefers_dark() -> bool:
    """auto 档系统深浅判定（公共版）：读注册表 AppsUseLightTheme，
    与换肤引擎 auto 同源（决策 66⑧）。backupPage/statsPage 的私有份
    已收敛入本函数（收官轮）；新消费方一律用本函数
    （WORKSHOP_URL_TEMPLATE 八份归一的同款先例）。"""

    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except Exception:
        return False
# —— 界面字号缩放（D25）——
# 两把尺同源：apply_font_scale 管全局字体（没写样式的控件吃它），
# font_px 管样式表里写死的 px（QSS 优先级高，setFont 拉不动，
# 必须逐处现算）。值都以启动时读取为准——改设置后重启生效。
_SCALE_CACHE: float | None = None


def _font_scale() -> float:
    """读「界面字号缩放」百分比 → 倍率。缺键/非法/范围外一律 1.0
    （范围 0.5~2.0，与设置页建议 80~200 同一口径）。读一次就缓存：
    全局字体与样式表字号必须同一把尺，中途改文件也不许两套值。"""
    global _SCALE_CACHE
    if _SCALE_CACHE is None:
        from core.appSettings import AppSettings
        raw = str(AppSettings().get("ui_font_scale") or "").strip()
        try:
            v = float(raw) / 100.0
        except ValueError:
            v = 1.0
        _SCALE_CACHE = v if 0.5 <= v <= 2.0 else 1.0
    return _SCALE_CACHE


def apply_font_scale(app) -> None:
    """全局字号缩放：main.py 在主窗口构造之前调用（布局度量随全局
    字体走）。只影响没写样式表的控件——菜单/树/表格/按钮/大多数
    正文；样式表写死 font-size 的标签由 font_px() 接管。"""
    scale = _font_scale()
    if scale == 1.0:
        return
    f = app.font()
    f.setPointSizeF(f.pointSizeF() * scale)
    app.setFont(f)


def font_px(base: int) -> int:
    """样式表字号换算：页面里写死的 font-size 一律用它现算，例：
    setStyleSheet(f"font-size: {font_px(18)}px; ...")
    页面构造发生在启动时，值与全局字体同刻同源。"""
    return max(1, round(base * _font_scale()))

# —— 运行日志按级别着色（consolePanel 消费）——
# 与斑马纹同一哲学（踩坑 51）：颜色事实源收在本模块、按当前生效主题
# 档位显式给色。深色套 = consolePanel 旧硬编码值（历史行为不变）；
# 亮色套按白底对比度校准——info #d4d4d4 与 warn #f5a623 在白底几乎
# 不可见（实测报障），各加深到可读档。
_LOG_COLORS: dict[str, dict[str, str]] = {
    "dark": {
        "info": "#d4d4d4",   # 普通信息
        "ok": "#46a758",     # 成功结论
        "warn": "#f5a623",   # 警告
        "error": "#e5484d",  # 错误
        "ts": "#888888",     # 行首时间戳
    },
    "light": {
        "info": "#555555",   # 深灰：白底清晰可读
        "ok": "#2e7d32",     # 绿加深一档
        "warn": "#b45309",   # 橙转赭，白底可读
        "error": "#d13438",  # 红加深一档
        "ts": "#767676",     # 时间戳中性灰
    },
}

def log_colors() -> dict[str, str]:
    """运行日志着色（键 = LogBus 四级别名 + "ts"）。QSS 未生效
    （库缺席/应用失败）按深色套 = 历史默认行为；auto 档判定与
    zebra_colors 同源（styleHints().colorScheme()）。"""
    mode = _ACTIVE_MODE
    if mode == "auto":
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication
            scheme = QGuiApplication.styleHints().colorScheme()
            mode = "dark" if scheme == Qt.ColorScheme.Dark else "light"
        except Exception:
            mode = "dark"
    return _LOG_COLORS.get(mode or "dark", _LOG_COLORS["dark"])
