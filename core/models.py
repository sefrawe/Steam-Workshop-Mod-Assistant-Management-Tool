"""
数据模型（dataclass）层
"""
"""
为什么有这个文件：
    sqlite3 查询返回 tuple，字段靠位置访问，取错列运行时才炸；
    用 dict 则要手打 "local_timeupdated" 这种长键名，拼错同样难查。
    dataclass 让 IDE 全程补全 + 拼错立刻标红。

与 schema 的边界约定（★ 重要）：
    dataclass 里用 Python 的自然类型（bool / list / dict），
    存储格式（INTEGER 0/1、JSON 文本）与自然类型的互相转换
    一律由 sqliteRepository 在读写边界完成，GUI 层永远不碰 0/1 和 JSON 字符串。

    - 时间一律 Unix 时间戳（int），与 Steam API / acf 原生格式一致
    - manifest 用 str（schema 约定：无符号 64 位超出 INTEGER 安全范围）
    - id / created_at / detected_at 等允许 None = 插入时不用填，
      由 SQLite 的自增 / DEFAULT 负责填充，读出来时才有值
"""
from dataclasses import dataclass


@dataclass
class Game:
    app_id: int                      # Steam AppID（CK3=1158310 / RimWorld=294100）
    name: str
    download_dir: str                # steamcmd force_install_dir 指向的目录
    game_mod_dir: str | None = None  # 游戏本体 mod 目录，可空
    backup_dir: str | None = None    # None = 取默认值（下载目录同级 mod_backups/<appid>/）
    created_at: int | None = None


@dataclass
class Mod:
    mod_id: int                      # Steam publishedfileid
    game_id: int
    status: str = "tracked"          # tracked / downloaded / deleted / failed
    url: str | None = None
    title: str | None = None
    creator: str | None = None       # 作者 SteamID64
    time_created: int | None = None
    time_updated: int | None = None       # ★远端最新版本（API）
    last_time_updated: int | None = None  # 上次快照时的远端值（快照生成 + 提醒去重）
    local_timeupdated: int | None = None  # ★本地版本（acf）；None = 未下载
    manifest: str | None = None           # acf 内容版本号
    local_size: int | None = None         # acf 本地占用（字节）
    file_size: int | None = None          # API file_size（acf 缺失时兜底显示）
    subscriptions: int | None = None
    favorited: int | None = None
    views: int | None = None
    tags: list[str] | None = None         # API 标签；库中存 JSON 文本
    last_checked_at: int | None = None    # 仅展示"多久没检测"，不参与判定
    preview_url: str | None = None
    is_special: bool = False              # 假中文等待处理 mod；库中存 0/1
    note: str | None = None
    color_tag: str | None = None
    local_path: str | None = None
    deleted_at: int | None = None
    deleted_last_state: dict | None = None  # 软删除前末态；库中存 JSON 文本
    first_tracked_at: int | None = None


@dataclass
class Snapshot:
    mod_id: int
    time_updated: int | None = None       # 快照时刻的远端版本
    manifest: str | None = None
    local_timeupdated: int | None = None  # 快照时刻的本地版本
    snapshot_at: int | None = None
    id: int | None = None


@dataclass
class Backup:
    mod_id: int
    backup_path: str                # 磁盘目录，UNIQUE
    size_bytes: int
    version_timeupdated: int        # 备份时的本地版本
    manifest: str | None = None
    note: str | None = None
    pinned: bool = False            # 豁免自动清理；库中存 0/1
    created_at: int | None = None
    id: int | None = None


@dataclass
class OperationLog:
    command: str
    backup_id: int | None = None
    executed_at: int | None = None
    error_count: int = 0
    result: str | None = None       # success / error / timeout / ...
    id: int | None = None


@dataclass
class FailedMod:
    mod_id: int                     # 原 id（故意无外键——归档表独立存活）
    game_id: int
    reason: str | None = None
    last_known_state: dict | None = None  # 库中存 JSON 文本
    replaced_by: int | None = None        # 关联替换后指向新 mod_id
    detected_at: int | None = None
    id: int | None = None


@dataclass
class Alert:
    mod_id: int
    remote_time_updated: int        # 触发提醒时的远端版本（同版本只提醒一次的比对依据）
    diff_seconds: int | None = None
    was_downloaded: bool = False    # 库中存 0/1
    note: str | None = None
    alert_at: int | None = None
    id: int | None = None
