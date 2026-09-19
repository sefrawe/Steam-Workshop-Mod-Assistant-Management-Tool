"""主题应用
"""
"""
深色主题走 pyqtdarktheme（记事本第二步决策）。
★ 该库已停更，与新版 PySide6 有已知兼容问题；任何异常一律
静默降级为系统默认配色——G1 不为外观停留，QSS 后备方案留给 M4。
"""
import sys


def apply_theme(app) -> None:
    try:
        import pyqtdarktheme as qdarktheme
        if hasattr(qdarktheme, "themedark"):      # 旧版 API
            qdarktheme.themedark(app)
        elif hasattr(qdarktheme, "setup_theme"):  # pyqtdarktheme-fork API
            qdarktheme.setup_theme("dark")
    except Exception as exc:  # 兼容性问题 → 静默降级，不阻塞启动
        print(f"[theme] 深色主题不可用，使用系统默认配色：{exc}", file=sys.stderr)
