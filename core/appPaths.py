"""程序数据位置"""
r"""全项目的"数据放哪"只在本模块回答一次（与 steamPaths 管 steamcmd
路径同一思路，这里管的是本软件自己的家）。

两种运行形态（T17 拍板：便携模式）：

- 源码运行（开发期 python main.py / pytest）：
  数据根 = 项目根目录（core/ 的上一级）。data/、config/ 都在项目根，
  与历史行为完全一致，开发环境零影响。

- 打包运行（PyInstaller 出的 exe / Linux 包）：
  Windows：数据根 = exe 所在的文件夹。账本、设置跟着软件走：整个
  文件夹拷去别的机器，数据原样带走——这就是"便携"的含义。
  为什么不能用程序文件所在的位置：单文件版 exe 每次启动把自己解压
  到临时目录、退出就删，数据写进去等于每次清零；文件夹版的内部
  目录属于"程序尸体"，升级时整个换掉。
  Linux（AppImage/deb）：程序本体装在只读挂载/系统目录（/opt）里，
  "数据跟着程序走"写不进去。按 XDG 惯例放用户家目录
  ~/.local/share/SteamWorkshopTool/（XDG_DATA_HOME 可覆盖）——
  同样位置可预期、可备份，升级不丢数据。

为什么写不了就拒绝启动、而不是悄悄换地方（比如 %APPDATA%）：
  便携模式的价值就在"数据位置永远可预期"。偷偷搬家，用户日后
  备份/迁移时找不到数据，比当场报错糟糕得多。与决策 29"备份位置
  指针不静默自愈"同一哲学：把原因说清楚，让用户自己决定挪不挪。

本模块纯逻辑零 Qt：main.py 的启动守卫和 pytest 共用。
"""
import os
import sys
from pathlib import Path


def app_root() -> Path:
    """程序的家目录：源码运行 = 项目根；打包运行 = exe 所在文件夹
    （Windows 便携模式）/ XDG 数据目录（Linux 打包，见文件头）。"""
    if getattr(sys, "frozen", False):
        if sys.platform != "win32":
            # Linux 打包（AppImage/deb）：程序本体在只读挂载或 /opt，
            # 数据按 XDG 惯例放用户家目录，位置可预期、可备份
            xdg_root = os.environ.get("XDG_DATA_HOME") or \
                str(Path.home() / ".local" / "share")
            return Path(xdg_root) / "SteamWorkshopTool"
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

def _is_system_location(path: Path) -> bool:
    """判断路径是否位于系统保护目录（Program Files、Windows 等）。

    这些目录对普通权限进程有写入保护（UAC），是「数据目录不可写」的
    经典成因之一；与之并列的另一成因是安全软件对无签名新程序的观察期
    拦截——普通文件夹也会发生（实测：同一文件夹记事本能建文件，本程序
    却被拦）。两种成因的解法不同（挪文件夹 vs 白名单/管理员），文案
    必须分开说，不能让普通文件夹里的用户白挪一遍。
    """
    if sys.platform != "win32":
        return False
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for env_name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)",
                     "ProgramW6432", "ProgramData"):
        value = os.environ.get(env_name)
        if not value:
            continue
        try:
            root = Path(value).resolve()
        except Exception:
            continue
        if resolved == root or root in resolved.parents:
            return True
    return False
def writability_problem() -> str | None:
    """启动守卫（T17）：探测数据/配置目录能不能创建、能不能写入。

    能 → 返回 None（放行）；不能 → 返回一段可直接弹窗的中文说明。

    探测是真刀真枪的：建目录、写一个几字节的小文件、随手删掉——
    毫秒级，比任何"猜权限"的写法都可靠。

    失败文案按实际位置分流（v9）：
    - 系统保护目录（Program Files 等）→ 写入保护是目录属性，
      「挪去普通文件夹」是正解，管理员启动只是绕道；
    - 普通文件夹 → 权限本身正常，被拦大概率是安全软件对无签名
      新程序的观察期（实测：同文件夹记事本能建文件），挪文件夹
      救不了，白名单或管理员启动才是出路。
    """
    from core.i18n import t  # 函数内导入：core.i18n 顶层要用 app_root，
    # 顶层互导会成环
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
            header = t('程序无法在当前位置保存数据（账本、设置等将无处可写），因此拒绝启动。\n\n出问题的目录：')
            if sys.platform == "win32":
                if _is_system_location(d):
                    cause = t('原因说明：\n· 本软件被放在受系统保护的目录（Program Files、Windows 等）。这些位置对普通权限的程序有写入保护，账本、设置随时可能写丢，程序因此拒绝启动。')
                    advice = t('解决方法：把整个软件文件夹移动到一个普通文件夹（例如 D:\\SteamModTool）后重新运行；或以管理员身份启动（点下方「以管理员身份重启」，或右键 exe → 以管理员身份运行）。')
                else:
                    cause = t('原因说明：\n· 当前文件夹是普通文件夹，写入权限本身正常——最可能的原因是安全软件对「刚打包的无签名程序」的观察期拦截：临时禁止本程序创建数据文件，与文件夹位置、数据安全无关，程序成功运行几次后通常会自行解除。')
                    advice = t('解决方法：把本软件文件夹加入杀毒软件/Windows 安全中心的白名单后重新运行；或以管理员身份启动（点下方「以管理员身份重启」，或右键 exe → 以管理员身份运行）。')
            else:
                # Linux：没有杀软观察期生态，通常是属主/权限问题，
                # 沿用原指引（键与翻译都不变）
                cause = ""
                advice = t('解决办法：确认上面目录的属主是你当前的用户、且可写；\n或设置 XDG_DATA_HOME 环境变量把数据目录指到别处，然后重新运行。')
            return (header + f"{d}" + t('\n系统原因：') + f"{exc}"
                    + (("\n\n" + cause) if cause else "")
                    + "\n\n" + advice)
    return None
