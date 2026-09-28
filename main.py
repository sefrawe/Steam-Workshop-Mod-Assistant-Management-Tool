"""程序入口"""
"""启动顺序：建 app + 设图标 → 单实例锁 → 写路径守卫 → 会话落点 → 主题 → 主窗口。

单实例锁（R12 收口）放最前：双开虽有 WAL+事务兜底不至于写坏账，
但两边各开各的 steamcmd 终端、各自指挥下载，只会互相干扰——
锁住比兜底好。锁文件放系统临时目录，与数据目录可写性无关
（数据目录不可写的机器也要能弹出"已在运行"的提示框）；
QLockFile 自带陈旧锁识别（进程已死即视为无锁），崩溃不留死锁。

T17 守卫为什么这么前：主窗口一构造就会读配置文件、开数据库，
要是数据目录写不了（典型：软件被放进 Program Files 这类系统
目录），晚一步就是带着"随时写丢数据"的隐患继续跑。所以先探测，
探不过就弹窗说明原因、直接退出——不静默把数据挪去别处
（数据位置可预期是便携模式的命根子，与决策 29 同一哲学）。

源码运行（python main.py）时项目目录天然可写，守卫恒通过，
开发期零影响。打包相关（schema.sql 随包、exe 文件图标）归打包
批次处理；运行时图标（任务栏归属 + 窗口图标）已在本文件启动段设置。
"""
import ctypes
import sys

from PySide6.QtCore import QDir, QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from core import appPaths

# R12：锁文件名带项目特征，避免与别的软件撞名
_LOCK_PATH = QDir.temp().absoluteFilePath(
    "SteamWorkshopModAssistant.single-instance.lock")


def main() -> int:
    app = QApplication(sys.argv)
    # 注意：下面两个名字是存储坐标（会话文件 = config/<组织名>/<应用名>.ini，
    # 决策 70②），不是显示名——禁止改成标题/仓库 URL 等好看的形式，
    # 改了会话记忆静默搬家/丢失（踩坑㊽族）。窗口标题在 MainWindow。
    app.setApplicationName("Steam Workshop Mod Assistant")
    app.setOrganizationName("sefrawe")
    app.setApplicationVersion("1.0.0")  # 与 T17 版本拍板同步；--version-file 同源

    # ---- 图标（运行时两件）：任务栏归属 + 窗口图标 ----
    # AppUserModelID：向 Windows 声明本程序的独立应用身份。不设的话
    # 源码运行任务栏挂 Python 默认图标；打包后任务栏图标归属也靠它。
    # ID 一经发布不再改——改了等于换应用，任务栏固定/分组记忆全部作废。
    # 与上面 Qt 两个名字互相独立、无需镜像（shell 身份证 ≠ 存储坐标）。
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sefrawe.SteamWorkshopModAssistant.1")

    # 窗口图标：标题栏 / 任务栏 / 所有弹窗（含下方"程序已在运行"）的共同来源。
    # exe 文件图标（资源管理器/快捷方式）归打包轮 --icon——同一套美术、两条管线。
    # 存在性检查不是多余：QIcon 加载失败不报错、静默回退默认图标，
    # 缺图至少 stderr 留一行（踩坑㊽族：静默降级会掩盖集成失败）。
    _icon_path = appPaths.resource_path("icon/icon.png")
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
    else:
        print(f"[main] 图标缺失，跳过窗口图标：{_icon_path}", file=sys.stderr)

    # ---- R12 单实例锁：赶在守卫与主窗口之前 ----
    # tryLock(0) = 不等待，抢不到立刻报；锁对象必须活到程序退出
    # （main 函数局部变量正好如此），中途被回收等于没锁
    lock = QLockFile(_LOCK_PATH)
    if not lock.tryLock(0):
        QMessageBox.information(
            None, "程序已在运行",
            "本工具已有另一个窗口在运行（可能最小化在任务栏）。\n\n"
            "请切换到那个窗口使用——同时开两个会互相干扰"
            "（两边同时指挥 steamcmd、同时写同一本账）。")
        return 0

    # ---- T17 写路径守卫：必须赶在 AppSettings / 数据库之前 ----
    # 弹窗需要 QApplication 已存在，所以顺序是：建 app → 守卫 → 其余
    problem = appPaths.writability_problem()
    if problem is not None:
        QMessageBox.critical(None, "无法启动：数据目录不可写", problem)
        return 1

    # ---- 决策 70：会话记忆落点切到软件文件夹（config/），并迁移注册表旧键 ----
    # 必须赶在 MainWindow 之前：主窗口构造期就有 QSettings 读写（面板显隐、
    # 导航折叠），落点先定下来这些记忆才能落到文件夹里。放在写路径守卫
    # 之后——config/ 刚被守卫确认可写，迁移才有落点。
    from gui.sessionStore import startup as session_startup
    session_startup()

    from gui.theme import apply_theme  # 失败自动降级，见 theme.py 注释
    apply_theme(app)

    from gui.MainWindow import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
