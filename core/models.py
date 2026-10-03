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
"""数据模型（dataclass）层 """ """边界约定沿旧版（自然类型在 dataclass、存储格式转换全在 repo 读写边界）。
v2 判决制变更：Mod 去掉 manifest / local_timeupdated / local_path，
新增 confirmed_version / confirmed_at / confirmed_source——本地版本基准
唯一写入点 = 确认门（R17），版本未知 = confirmed_version IS NULL。
新增 Verdict（verdict_log 一行）与 Translation（翻译缓存一行）。 """
from dataclasses import dataclass


@dataclass
class Game:
    app_id: int
    name: str
    download_dir: str
    game_mod_dir: str | None = None
    backup_dir: str | None = None
    created_at: int | None = None


@dataclass
class Mod:
    mod_id: int
    game_id: int
    status: str = "tracked"            # constants.STATUS_VALUES 四态
    url: str | None = None
    title: str | None = None
    creator: str | None = None         # 作者 SteamID64
    time_created: int | None = None
    time_updated: int | None = None        # ★远端观测（检测写）
    last_time_updated: int | None = None   # 上次远端观测（快照分母 + 提醒去重）
    confirmed_version: int | None = None   # ★本地确认基准（确认门写；NULL=版本未知）
    confirmed_at: int | None = None
    confirmed_source: str | None = None    # verified/unverified/inherited_acf/claim/manual
    local_size: int | None = None          # 盘点回填，仅展示；R19 禁入判定
    file_size: int | None = None
    subscriptions: int | None = None
    favorited: int | None = None
    views: int | None = None
    tags: list[str] | None = None
    last_checked_at: int | None = None
    preview_url: str | None = None
    is_special: bool = False
    note: str | None = None
    color_tag: str | None = None
    deleted_at: int | None = None
    deleted_last_state: dict | None = None
    first_tracked_at: int | None = None

    @property
    def version_unknown(self) -> bool:
        """版本未知：没有已确认的本地版本（confirmed_version IS NULL）。
        单源约定沿旧版：备份守卫拒绝、更新判定分桶、库页显示三处都从这里取。
        判定公式（D2）只对 downloaded 且非版本未知条目生效；
        版本未知条目进更新对照/详情按「版本未知」态显示。"""
        return self.confirmed_version is None


@dataclass
class Snapshot:
    """远端观测史一行（v2 只记远端侧；manifest/本地列随判决制移除）"""
    mod_id: int
    time_updated: int | None = None
    snapshot_at: int | None = None
    id: int | None = None


@dataclass
class Verdict:
    """verdict_log 一行：判决史 + 确认队列双角色。
    confirmed_at=None 即 pending（待确认队列行，重启不丢）。
    mod 可能不在 mods 表（D39）——title/file_size 落 pending 时顺手存，离线可显。"""
    mod_id: int
    kind: str                          # success/timeout/fail/claim/manual
    game_id: int                       # 归属档案（冗余存档，无外键）
    version_trigger: int | None = None
    version_query: int | None = None
    version_written: int | None = None
    title: str | None = None
    file_size: int | None = None
    source: str | None = None
    confirmed_at: int | None = None
    occurred_at: int | None = None
    note: str | None = None
    id: int | None = None


@dataclass
class Translation:
    """翻译缓存一行（D35）：单 mod 单条当前译文，REPLACE 覆盖无历史"""
    mod_id: int
    source_hash: str                   # 原文 sha1；失配 = 作者改了简介 → 重译
    target_lang: str
    engine: str | None = None          # NULL = 浏览器翻译主路径未落缓存
    text_translated: str = ""
    translated_at: int | None = None


@dataclass
class Backup:
    mod_id: int
    backup_path: str
    size_bytes: int
    version_timeupdated: int           # 取确认值（D14）
    manifest: str | None = None        # 可空，备份时从 acf 盘面回填
    note: str | None = None
    pinned: bool = False
    created_at: int | None = None
    id: int | None = None


@dataclass
class OperationLog:
    command: str
    backup_id: int | None = None
    executed_at: int | None = None
    error_count: int = 0
    result: str | None = None
    id: int | None = None


@dataclass
class FailedMod:
    mod_id: int                        # 故意无外键——归档独立存活
    game_id: int
    reason: str | None = None
    last_known_state: dict | None = None
    replaced_by: int | None = None
    detected_at: int | None = None
    id: int | None = None


@dataclass
class Alert:
    mod_id: int
    remote_time_updated: int
    diff_seconds: int | None = None
    was_downloaded: bool = False
    note: str | None = None
    alert_at: int | None = None
    id: int | None = None


@dataclass
class PurgedMod:
    mod_id: int
    game_id: int
    title: str | None
    note: str | None
    purged_at: int
