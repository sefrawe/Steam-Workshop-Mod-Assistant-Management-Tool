"""主题应用"""
"""
优先尝试 pyqtdarktheme；未安装不算错误——Qt 6.5+ 在 Windows
深色模式下会自动使用深色配色（当前界面即此效果），缺库保持静默。
只有"装了但应用失败"才提示。统一主题留到视觉打磨阶段。
"""


def apply_theme(app) -> None:
    try:
        import pyqtdarktheme as qdarktheme
    except ImportError:
        return  # 未安装：Qt 自动跟随系统深浅色，属预期状态
    try:
        qdarktheme.setup_theme("dark")
    except Exception as exc:
        import sys
        print(f"[theme] 深色主题应用失败，改用系统配色：{exc}", file=sys.stderr)
