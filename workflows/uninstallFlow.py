"""卸载盘点与注册表路径
"""
r"""workflows/uninstallFlow.py —— 卸载模块的逻辑半件（零 Qt，pytest 覆盖）。

本软件是便携形态：账本、设置、会话记忆全部住在软件文件夹里
（data/ 与 config/，定位单源 core/appPaths）。因此"卸载"的完整
语义只有两条：
1. 删除软件整个文件夹——账本、设置、界面记忆随之清空；
2. （仅旧版本用户）注册表里留着一小撮界面记忆键，给路径让用户
   自己决定删不删。

本模块回答两个问题：
- 【盘点】软件文件夹里留下了什么：把 data/ 与 config/ 逐文件
  列出，每个文件给一句"它是什么"（describe_file 是显示层与
  测试共用的单一事实来源）；
- 【指路】注册表键在哪：QSettings 在 Windows 原生格式下落在
  HKEY_CURRENT_USER\Software\<组织名>\<应用名>，本模块只做路径
  格式化（读不读得到是 gui/sessionStore 的事，那需要 Qt）。

刻意不做的事（边界）：
- 不删任何文件——盘点只读；删除动作只有一个：资源管理器里删
  整个文件夹，一步到位；
- 不碰软件文件夹之外的任何东西——steamcmd、工坊 content、备份
  区 mod_backups 是用户资产，与卸载无关（页面文案说明白）；
- 不代删注册表键——给路径给步骤，删不删用户拍板（与"备份位置
  指针不静默自愈"同一哲学：说清楚，让用户决定）。
"""
from dataclasses import dataclass, field
from pathlib import Path

# 已知文件的角色说明（页面显示与测试共用；未知文件给通用兜底）
_ROLE_MODS_DB = ("账本数据库：全部游戏档案与 mod 记录都在这里"
                 "（想带走先去【换机迁移】导出完整账本）")
_ROLE_DB_OTHER = "账本数据库的滚动备份或附属文件（自动备份与正常运行产生，随文件夹一起删除）"
_ROLE_SETTINGS = "全局设置：steamcmd 路径、备份策略、界面偏好等"
_ROLE_SETTINGS_BAK = "设置文件损坏时的自动备份（可随文件夹一起删除）"
_ROLE_SESSION_INI = "界面会话记忆：面板显隐、列宽、折叠状态等（随文件夹一起搬走或删除）"
_ROLE_GENERIC = "本软件生成的数据文件（随文件夹一起删除）"


def describe_file(name: str) -> str:
    """按文件名给一句"它是什么"。未知文件给通用兜底——盘点永远
    不留"没解释的条目"。"""
    n = name.casefold()
    if n == "mods.db":
        return _ROLE_MODS_DB
    if n.startswith("mods"):  # mods.db-wal / mods.db.bak / 滚动备份等
        return _ROLE_DB_OTHER
    if n == "globalsettings.json":
        return _ROLE_SETTINGS
    if n.endswith(".json.bak"):
        return _ROLE_SETTINGS_BAK
    if n.endswith(".ini"):
        return _ROLE_SESSION_INI
    return _ROLE_GENERIC


@dataclass(frozen=True)
class FileEntry:
    rel_path: str    # 相对软件根目录，如 data/mods.db（正斜杠显示）
    size_bytes: int
    role: str


@dataclass(frozen=True)
class FolderReport:
    entries: list = field(default_factory=list)
    total_bytes: int = 0
    data_dir_exists: bool = False
    config_dir_exists: bool = False


def scan_folder(root: Path) -> FolderReport:
    """盘点软件文件夹里 data/ 与 config/ 留下的文件（只读）。

    只看这两个目录：程序本体（exe、dll 等）不属于"留下来的数据"，
    随文件夹删除自不必说，列出来只会稀释重点。
    """
    entries: list[FileEntry] = []
    total = 0
    data_dir = Path(root) / "data"
    config_dir = Path(root) / "config"
    for base in (data_dir, config_dir):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            size = p.stat().st_size
            entries.append(FileEntry(
                rel_path=p.relative_to(root).as_posix(),
                size_bytes=size,
                role=describe_file(p.name)))
            total += size
    return FolderReport(
        entries=entries, total_bytes=total,
        data_dir_exists=data_dir.is_dir(),
        config_dir_exists=config_dir.is_dir())


def registry_key_path(org: str, app: str) -> str:
    """注册表键显示路径（QSettings Windows 原生格式的落点）。
    纯字符串格式化；组织/应用名缺哪段就跳过哪段。"""
    present = [p for p in ((org or "").strip(), (app or "").strip()) if p]
    if not present:
        return r"HKEY_CURRENT_USER\Software（组织/应用名未设置）"
    return r"HKEY_CURRENT_USER\Software" + "\\" + "\\".join(present)
