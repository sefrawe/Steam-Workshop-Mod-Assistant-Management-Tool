"""程序数据位置"""
r"""全项目的"数据放哪"只在本模块回答一次（与 steamPaths 管 steamcmd
路径同一思路，这里管的是本软件自己的家）。

两种运行形态（T17 拍板：便携模式）：

- 源码运行（开发期 python main.py / pytest）：
  数据根 = 项目根目录（core/ 的上一级）。data/、config/ 都在项目根，
  与历史行为完全一致，开发环境零影响。

- 打包运行（PyInstaller 出的 exe）：
  数据根 = exe 所在的文件夹。账本、设置跟着软件走：整个文件夹拷去
  别的机器，数据原样带走——这就是"便携"的含义。
  为什么不能用程序文件所在的位置：单文件版 exe 每次启动把自己解压
  到临时目录、退出就删，数据写进去等于每次清零；文件夹版的内部
  目录属于"程序尸体"，升级时整个换掉。

为什么写不了就拒绝启动、而不是悄悄换地方（比如 %APPDATA%）：
  便携模式的价值就在"数据位置永远可预期"。偷偷搬家，用户日后
  备份/迁移时找不到数据，比当场报错糟糕得多。与决策 29"备份位置
  指针不静默自愈"同一哲学：把原因说清楚，让用户自己决定挪不挪。

本模块纯逻辑零 Qt：main.py 的启动守卫和 pytest 共用。
"""
import sys
from pathlib import Path


def app_root() -> Path:
    """程序的家目录：源码运行 = 项目根；打包运行 = exe 所在文件夹。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：sys.executable 就是用户双击的那个 exe。
        # 注意不能用 __file__——打包后它指向解包出来的临时/内部目录，
        # 数据写到那里会丢（见文件头说明）
        return Path(sys.executable).resolve().parent
    # 源码运行：本文件在 core/ 里，上一级就是项目根
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """数据目录：账本数据库（mods.db）和它的滚动备份都住这里。"""
    return app_root() / "data"


def config_dir() -> Path:
    """配置目录：GlobalSettings.json 住这里。"""
    return app_root() / "config"


def db_path() -> Path:
    """账本数据库的完整路径（交给 SQLiteRepository 的 db_path 参数）。
    单独给个函数：调用方不用自己拼，也避免 "data/mods.db" 这种
    相对路径写法再冒出来——相对路径会跟着"从哪个文件夹启动"变，
    这正是本模块要消灭的东西。"""
    return data_dir() / "mods.db"

def resource_path(rel: str) -> Path:
    """只读资源（icon/icon.png、schema.sql 等）的完整路径：跟着"包"走。

    与 app_root()（数据根）刻意分成两层：数据要可写，打包运行时落在
    exe 所在文件夹；资源只读、编译进包里，onefile 解包后住在临时目录
    （sys._MEIPASS），与数据根不在同一棵目录树——所以不能用 app_root()
    找资源。T17 打包后 schema.sql 的读取也走这里（资源路径唯一定义点）。

    源码运行：_MEIPASS 不存在，项目根下原样直取（icon/ 在项目根）。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / rel
    # 源码运行：本文件在 core/ 里，上一级就是项目根（与 app_root 同款算法）
    return Path(__file__).resolve().parent.parent / rel


def writability_problem() -> str | None:
    """启动守卫（T17）：探测数据/配置目录能不能创建、能不能写入。
    能 → 返回 None（放行）；不能 → 返回一段可直接弹窗的中文说明。
    探测是真刀真枪的：建目录、写一个几字节的小文件、随手删掉——
    毫秒级，比任何"猜权限"的写法都可靠。
    """
    for d in (data_dir(), config_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink()
            except OSError:
                pass  # 写得进但删不掉也算可写；小文件留着无害，
                      # 不为这个误报"不可写"把用户拦在门外
        except OSError as exc:
            # 典型场景：软件被放进 Program Files 等系统目录，
            # 普通权限的进程没有写盘资格
            return (
                "程序无法在当前位置保存数据（账本、设置等将无处可写），"
                "因此拒绝启动。\n\n"
                f"出问题的目录：{d}\n"
                f"系统原因：{exc}\n\n"
                "解决办法：把整个软件文件夹移动到一个普通文件夹"
                "（例如 D:\\SteamModTool），\n"
                "不要放在 Program Files、Windows 等系统目录里，"
                "然后重新运行。"
            )
    return None
