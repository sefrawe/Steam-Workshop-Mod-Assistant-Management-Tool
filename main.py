"""入口"""
import sys


from PySide6.QtWidgets import QApplication


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Steam Workshop Mod Assistant")
    app.setOrganizationName("sefrawe")

    from gui.theme import apply_theme
    apply_theme(app)  # 失败自动降级，见 theme.py 注释

    from gui.MainWindow import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
