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
