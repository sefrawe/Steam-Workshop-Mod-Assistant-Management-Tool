"""程序入口
"""
r"""main.py —— 启动顺序：建 app + 存储坐标 → 图标 → 单实例锁 → 写路径守卫
→ 会话落点 → 主题 → 主窗口。

单实例锁（R12）为什么放这么前：双开虽有 WAL + 事务兜底不至于写坏账，
但两边各开各的 steamcmd、各自指挥下载，只会互相干扰——锁住比兜底好。
V2 锁名带 V2（断代拍板，见下）：迁移期 V1 与 V2 并存使用，各用各的锁，
两代可以同时开着对照；同一代内部仍然单实例。

T17 写路径守卫为什么这么前：主窗口一构造就会读配置、开数据库，数据
目录写不了（典型：软件被放进 Program Files 这类系统目录）晚一步就是
带着"随时写丢数据"的隐患继续跑。先探测，探不过弹窗说明原因、直接
退出——不静默把数据挪去别处（数据位置可预期是便携模式的命根子，
与决策 29 同一哲学）。源码运行时项目目录天然可写，守卫恒通过。

V2 断代拍板（本轮生效；V2 首次发布后按"一经发布不再改"冻结）：
- 应用名带 V2（存储坐标：会话文件 = config/<组织名>/<应用名>.ini，
  决策 70②）；锁名与 AppUserModelID 同步带 V2。
- 理由：① 会话记忆不背 V1 的账——断代重开，从零开始正是想要的行为；
  ② V1/V2 并存期互不顶锁；③ 任务栏身份各自独立、分组不串。
- 为什么此刻改零代价：V2 全新文件夹本来就没有旧会话记忆可丢
  （踩坑㊽族"改名 = 记忆搬家"在这里恰好是目的本身）；注册表旧键
  迁移在新名字下自然空操作——V1 的注册表残留属于 V1 的名字，
  V2 断代不认。
"""
import ctypes
import sys

from PySide6.QtCore import QDir, QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from core import appPaths

# R12：锁文件名带项目特征 + 代际特征，避免与 V1（或别的软件）撞名
_LOCK_PATH = QDir.temp().absoluteFilePath(
    "SteamWorkshopModAssistantV2.single-instance.lock")


def main() -> int:
    app = QApplication(sys.argv)
    # —— 存储坐标（不是显示名！禁止改成标题/仓库 URL 等好看的形式，
    # 改了会话记忆静默搬家/丢失——踩坑㊽族。V2 取值 = 断代拍板）——
    app.setApplicationName("Steam Workshop Mod Assistant V2")
    app.setOrganizationName("sefrawe")
    app.setApplicationVersion("2.0.0")  # V2 版本线起步值，打包轮终拍

    # ---- 图标（运行时两件）：任务栏归属 + 窗口图标 ----
    # AppUserModelID：向 Windows 声明独立应用身份。V2 用自己的 ID——
    # 与 V1 是两个应用，任务栏分组/固定互不串；本 ID 自 V2 首次发布
    # 起冻结，永不改（改 = 换应用，固定/分组记忆全部作废）。
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sefrawe.SteamWorkshopModAssistantV2.1")

    # 窗口图标：标题栏/任务栏/所有弹窗的共同来源。V2 的 icon/ 资源
    # 还没复制（归打包/美术轮），缺失时 stderr 留一行再跳过——
    # QIcon 加载失败不报错、静默回退默认图标，缺图至少要留痕
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
            "本工具（V2）已有另一个窗口在运行（可能最小化在任务栏）。\n\n"
            "请切换到那个窗口使用——同时开两个会互相干扰"
            "（两边同时指挥 steamcmd、同时写同一本账）。")
        return 0

    # ---- T17 写路径守卫：必须赶在 AppSettings / 数据库之前 ----
    # 弹窗需要 QApplication 已存在，所以顺序是：建 app → 守卫 → 其余
    problem = appPaths.writability_problem()
    if problem is not None:
        QMessageBox.critical(None, "无法启动：数据目录不可写", problem)
        return 1

    # ---- 决策 70：会话记忆落点 = 软件文件夹内 config/ 下的 ini ----
    # 必须赶在 MainWindow 之前：主窗口构造期就有 QSettings 读写，
    # 落点先定下来记忆才能落进文件夹。V2 新应用名下注册表无旧键，
    # 注册表迁移恒为 0 条（断代不认 V1 旧键）；但落点切换本身
    # （INI 格式 + config/ 目录）必须照做——这是"会话记忆跟着
    # 文件夹走"的机制本体
    from gui.sessionStore import startup as session_startup
    session_startup()

    from gui.theme import apply_theme, apply_font_scale
    apply_theme(app)
    apply_font_scale(app)  # D25 界面字号缩放：必须在主窗口构造之前

    from gui.MainWindow import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
