"""程序入口"""
"""启动顺序三步：写路径守卫 → 主题 → 主窗口。

T17 守卫为什么放最前：主窗口一构造就会读配置文件、开数据库，
要是数据目录写不了（典型：软件被放进 Program Files 这类系统
目录），晚一步就是带着"随时写丢数据"的隐患继续跑。所以先探测，
探不过就弹窗说明原因、直接退出——不静默把数据挪去别处
（数据位置可预期是便携模式的命根子，与决策 29 同一哲学）。

源码运行（python main.py）时项目目录天然可写，守卫恒通过，
开发期零影响。打包相关（schema.sql 随包、图标等）归打包批次处理。
"""
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from core import appPaths


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Steam Workshop Mod Assistant")
    app.setOrganizationName("sefrawe")

    # ---- T17 写路径守卫：必须赶在 AppSettings / 数据库之前 ----
    # 弹窗需要 QApplication 已存在，所以顺序是：建 app → 守卫 → 其余
    problem = appPaths.writability_problem()
    if problem is not None:
        QMessageBox.critical(None, "无法启动：数据目录不可写", problem)
        return 1

    from gui.theme import apply_theme  # 失败自动降级，见 theme.py 注释
    apply_theme(app)

    from gui.MainWindow import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
