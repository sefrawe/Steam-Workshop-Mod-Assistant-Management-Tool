"""会话记忆存储单源
"""
r"""gui/sessionStore.py —— QSettings 会话记忆的存放位置单源（决策 70）。

背景：全项目的界面记忆（主窗口面板显隐、导航折叠、mod 库列宽、
核验页折叠等）都用无参 QSettings() 读写。Qt 在 Windows 上的默认
落点是注册表（HKCU\Software\<组织名>\<应用名>）——便携形态下
这意味着：软件文件夹删干净了，注册表里还留着一小撮界面记忆；
整个文件夹拷去新机器，这些记忆也不会跟着走。

本模块把落点切到软件文件夹内（config/ 下的 ini 文件，与账本、
设置同一口径：数据永远可预期地跟着文件夹走），并负责把注册表里
的旧记忆一次性搬过来。搬家之后：
- 删除软件文件夹 = 界面记忆一起清零，注册表不再新增残留；
- 换机迁移拷文件夹时界面记忆随行（列宽/折叠不用重新调）。

用法（全项目只接两处）：
- main.py：QApplication 建好之后、任何窗口创建之前调用
  startup()——先定落点、再把注册表旧键搬进来（幂等：搬过或
  注册表本就没键时是空操作）；
- 其余代码照旧用无参 QSettings()，一行不用改——落点切换对全部
  既有调用点透明生效（setDefaultFormat/setPath 是进程级静态
  设置，只影响之后构造的 QSettings 对象）。

回程票：迁移不删注册表原稿。万一要还原，删掉文件夹内的会话
文件再重启，startup() 会从注册表原稿再迁一次。
"""
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings

from core.appPaths import config_dir
from workflows.uninstallFlow import registry_key_path

# 本次进程启动时自动迁移的条数（None = 还没跑过 startup）
_last_migrated: int | None = None


def session_file_path() -> Path:
    """会话记忆文件的实际落点：config/<组织名>/<应用名>.ini
    （QSettings 的 ini 命名规则；本函数与它保持同一公式，页面
    展示与迁移目标都用它，避免两处口径漂移）。"""
    org = (QCoreApplication.organizationName() or "").strip()
    app = (QCoreApplication.applicationName() or "").strip()
    if org and app:
        return config_dir() / org / f"{app}.ini"
    if app:
        return config_dir() / f"{app}.ini"
    return config_dir() / "session.ini"


def startup() -> int:
    """进程级落点切换 + 注册表旧键一次性迁移。返回迁移条数。

    必须在任何窗口（以及任何 QSettings()）构造之前调用——落点
    设置只影响之后构造的 QSettings。幂等：重复调用安全。
    """
    global _last_migrated
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat,
                      QSettings.Scope.UserScope, str(config_dir()))
    _last_migrated = migrate_from_registry()
    return _last_migrated


def last_startup_migration() -> int | None:
    """本次启动自动迁移的条数（卸载页展示用；None = 没跑过）。"""
    return _last_migrated


def _native() -> QSettings:
    """注册表侧旧存储（与项目里无参 QSettings 同一组织/应用名）。"""
    return QSettings(QSettings.Format.NativeFormat,
                     QSettings.Scope.UserScope,
                     QCoreApplication.organizationName(),
                     QCoreApplication.applicationName())


def migrate_from_registry() -> int:
    """把注册表旧键搬进软件文件夹内的会话文件。幂等且保守：
    会话文件已有内容时不搬不覆盖（以文件夹内的现值为准）。"""
    keys = _native().allKeys()
    if not keys:
        return 0
    target = QSettings(str(session_file_path()),
                       QSettings.Format.IniFormat)
    if target.allKeys():
        return 0
    for key in keys:
        target.setValue(key, _native().value(key))
    target.sync()
    return len(keys)


def session_file_exists() -> bool:
    return session_file_path().is_file()


def registry_has_keys() -> bool:
    return bool(_native().allKeys())


def registry_key_display() -> str:
    """注册表键的显示路径（卸载页第②卡与复制按钮用）。"""
    return registry_key_path(QCoreApplication.organizationName(),
                             QCoreApplication.applicationName())
