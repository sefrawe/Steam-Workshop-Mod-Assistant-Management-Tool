"""数据访问契约层（接口）
"""
"""数据访问契约层（接口） """ """v2 判决制契约。与旧版的三大差异：
1. 判定公式换 confirmed_version（D2）；acf 三件套退场——update_local_state 删除，
   R19：任何 acf/mtime 产物禁止进入本契约的版本写入路径
2. 新增确认门三正门（R17：本地版本唯一写入点）：
   record_verdicts → pending_confirmations → confirm_items / claim_accept；
   撤销 = revoke_confirmation（置 NULL，status 不动，D8）
3. verdict_log（判决史+确认队列）与 translations（翻译缓存）两个新表的读写
通用约定 6 条沿旧版（Unix 秒 / 查不到返 None / IntegrityError 上抛 /
单方法自带事务 / None=不修改 / 本文件零 SQL 零 sqlite3）。 """
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from core.models import (
    Alert, Backup, FailedMod, Game, Mod, OperationLog,
    PurgedMod, Snapshot, Translation, Verdict,
)

# 排序白名单（防注入，沿旧决策 6）。v2 变更：local_timeupdated 两项
# 换 confirmed_version——库页"本地版本"列新数据源（D21 同尺）。
ALLOWED_ORDERS: frozenset[str] = frozenset({
    "time_updated DESC", "time_updated ASC",
    "title ASC", "title DESC",
    "local_size DESC",

    "local_size ASC",

    "confirmed_version DESC", "confirmed_version ASC",
    "status ASC", "status DESC",
    "is_special DESC",
    "first_tracked_at DESC", "first_tracked_at ASC",
    "last_checked_at DESC",
    "mod_id ASC", "mod_id DESC",
})

# ledger 导出表（D14 重定：+verdict_log，8 张；translations 是机器生成
# 缓存不入账本；purged_mods / mod_dependencies 沿旧口径不随 ledger 走）
LEDGER_TABLES: tuple[str, ...] = (
    "games", "mods", "mod_snapshots", "verdict_log",
    "backups", "operations_log", "failed_mods", "special_mod_alerts",
)


@dataclass
class GameDeletionSummary:
    """沿旧版原样（删档前盘点）"""
    app_id: int
    mod_total: int
    mod_deleted: int
    failed_count: int
    backup_count: int
    backup_bytes: int
    backup_paths: list[str]


@dataclass
class BackupOverviewRow:
    """沿旧版原样（备份总览联表行）"""
    backup_id: int
    mod_id: int
    mod_title: str | None
    mod_status: str
    game_id: int
    game_name: str
    backup_path: str
    size_bytes: int
    version_timeupdated: int
    manifest: str | None
    created_at: int
    pinned: bool


class ModRepository(ABC):
    """全部数据能力的契约。唯一实现：sqliteRepository.SQLiteRepository。"""

    # ============ 基础设施（3） ============
    @abstractmethod
    def __init__(self, db_path: str | Path, *,
                 snapshot_keep: int = 10, verdict_keep: int = 50) -> None:
        """snapshot_keep：远端观测史滚动保留条数（D36 默认 10）。
        verdict_keep：verdict_log 非确认行按 mod 滚动保留条数（D23，默认 50；
        确认行不受此限——它是 confirmed_version 的票据）。"""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]: ...
    @abstractmethod
    def close(self) -> None: ...

    # ============ games（5）+ 删除与总览（3） ============
    # 签名与语义与旧契约逐字一致（add_game / get_game / list_games /
    # update_game / delete_game / game_deletion_summary /
    # delete_game_deep / list_backups_overview），docstring 见旧版。
    # v2 差异：delete_game_deep 的清理范围 += verdict_log 与 translations
    # C 级提案③：彻底清档 = 账全清；translations 随 mods CASCADE 自动）。
    @abstractmethod
    def add_game(self, app_id: int, name: str, download_dir: str,
                 game_mod_dir: str | None = None,
                 backup_dir: str | None = None) -> None: ...
    @abstractmethod
    def get_game(self, app_id: int) -> Game | None: ...
    @abstractmethod
    def list_games(self) -> list[Game]: ...
    @abstractmethod
    def update_game(self, app_id: int, *, name: str | None = None,
                    download_dir: str | None = None,
                    game_mod_dir: str | None = None,
                    backup_dir: str | None = None) -> None: ...
    @abstractmethod
    def delete_game(self, app_id: int) -> None: ...
    @abstractmethod
    def game_deletion_summary(self, app_id: int) -> GameDeletionSummary: ...
    @abstractmethod
    def delete_game_deep(self, app_id: int) -> GameDeletionSummary: ...
    @abstractmethod
    def list_backups_overview(self, game_id: int | None = None) -> list[BackupOverviewRow]: ...

    # ============ mods 基础（沿旧口径，判定字段换名） ============
    @abstractmethod
    def add_mod(self, mod: Mod) -> None: ...
    @abstractmethod
    def get_mod(self, mod_id: int) -> Mod | None: ...
    @abstractmethod
    def filter_existing_ids(self, mod_ids: Iterable[int]) -> set[int]: ...
    @abstractmethod
    def list_mods(self, game_id: int, *, status: str | None = None,
                  special_only: bool = False, color_tag: str | None = None,
                  search: str | None = None,
                  order_by: str = "time_updated DESC",
                  limit: int | None = None,
                  title_contains: str | None = None,
                  note_contains: str | None = None,
                  mod_id: int | None = None,
                  size_min: int | None = None, size_max: int | None = None,
                  updated_from: int | None = None,
                  updated_to: int | None = None,
                  tags_all: Iterable[str] | None = None) -> list[Mod]:
        """参数语义沿旧契约（T12 全套保留）。两处口径换新尺：
        - 大小口径 = COALESCE(NULLIF(local_size,0), file_size) 不变
          （local_size 现在来自只读盘点回填，D23）
        - 排序白名单见本文件顶部（confirmed_version 换防 local_timeupdated）"""

    @abstractmethod
    def update_api_metadata(self, mod_id: int, *, title: str | None = None,
                            creator: str | None = None, url: str | None = None,
                            time_created: int | None = None,
                            time_updated: int | None = None,
                            last_time_updated: int | None = None,
                            file_size: int | None = None,
                            subscriptions: int | None = None,
                            favorited: int | None = None,
                            views: int | None = None,
                            tags: list[str] | None = None,
                            preview_url: str | None = None,
                            last_checked_at: int | None = None) -> None:
        """检测三路写库之一（D2：沿用旧口径，判定字段换 confirmed_version）。
        只动远端侧；confirmed_* 三件套本方法绝不触碰（R17）。"""

    @abstractmethod
    def touch_checked(self, mod_ids: Iterable[int], *,
                      checked_at: int | None = None) -> None: ...

    # ============ 状态迁移 ============
    @abstractmethod
    def mark_deleted(self, mod_id: int, last_state: dict) -> None:
        """软删除三连（沿旧版）。last_state 由 flow 层组装。"""

    @abstractmethod
    def mark_restored(self, mod_id: int) -> None:
        """软删除恢复（旧决策 69⑦ 延续，判定字段换名）：
        status 按 confirmed_version 回推——非空 → downloaded、
        NULL → tracked。仅对 deleted 条目开放，其余状态 → ValueError。
        只动账面；磁盘处置归清理页（决策 69 分工不变）。"""

    @abstractmethod
    def mark_failed(self, mod_id: int, reason: str) -> None:
        """★复合（沿旧版）：mods.status→failed + 末态快照（含
        confirmed_version）写入 failed_mods。result=9 建档专用。"""

    @abstractmethod
    def replace_failed_mod(self, old_mod_id: int, new_mod_id: int) -> None:
        """★复合（沿旧版五步；快照改挂后按 snapshot_keep 重新裁剪）"""

    @abstractmethod
    def purge_mod(self, mod_id: int, *, purge_backups: bool = False) -> int:
        """彻底清账（沿旧版：RESTRICT 闸显式版 / 自动登记黑名单 /
        failed_mods 与 operations_log 刻意存活）。
        v2 差异（C 级提案③）：一并清 verdict_log 该 mod 全部行——
        判决史属账本，账清史清；历史证据由 failed_mods / purged_mods /
        operations_log 承载。translations 随 mods 行 CASCADE 自动清。"""

    # ============ 整理 setter（沿旧版：None=清空消歧义，决策 8） ============
    @abstractmethod
    def set_note(self, mod_id: int, note: str | None) -> None: ...
    @abstractmethod
    def set_color_tag(self, mod_id: int, color_tag: str | None) -> None: ...
    @abstractmethod
    def set_special(self, mod_id: int, is_special: bool) -> None: ...

    # ============ ★ 确认门（R17：本地版本唯一写入点，全项目仅此三处） ============
    @abstractmethod
    def record_verdicts(self, items: list[dict]) -> None:
        """批次收尾批量落 verdict_log（pending），一个事务。
        item 形状（dict，键全可选除 mod_id/kind/game_id）：
          mod_id, kind, game_id, version_trigger, version_query,
          version_written, title, file_size, source, note
        分工铁律：
        - D4 分支（version_written / source 取值）由 flow 层算好传入，
          repo 纯存取不推断——总禁令 R18（写入值永不高估）的守卫点在 flow；
        - 剔黑名单（D11）在 flow 落行之前完成（黑名单三拦之一）；
        - D39：mod 可能不在 mods 表，本方法不校验 mods 存在性。
        落行后按 verdict_keep 滚动修剪该 mod 的非确认行（确认行豁免，
        同 add_snapshot 滚动先例）；90 天线一并执行（D23）。"""

    @abstractmethod
    def pending_confirmations(self, game_id: int | None = None) -> list[Verdict]:
        """确认队列：confirmed_at IS NULL 且 kind IN ('success','claim','manual')
        的行。game_id=None 返回全部（D20 档案隔离的"其他档案 N 条"计数
        也从带参调用拿）。按 occurred_at 新→旧。"""
    @abstractmethod
    def list_game_verdicts(self, game_id: int, limit: int = 200) -> list:
        """按档案查最近判决史（跨 mod、含已确认行；入账中心时间线）。
        行形状与 pending_confirmations 一致；occurred_at 倒序。"""
    @abstractmethod
    def drop_stale_claims(self, game_id: int, mod_ids: Iterable[int]) -> int:
        """删除指定档案的未确认 claim 判决行（盘点发现文件夹已消失，
        候选提案作废——提案跟随盘面）。返回删除行数。已确认行与其他
        kind 一律不动（确认是账本事实，R17 只进不出；未确认行本就走
        verdict_keep/90 天的既有修剪口径，删除与其同性质）。"""

    @abstractmethod
    def confirm_items(self, mod_ids: list[int]) -> int:
        """★确认门唯一写入点（D5 三入口同正门），一个事务，返回确认条数。
        mod_ids = 入账中心勾选的 mod 编号列表——GUI 只做"背书"，version
        与 source 由实现从该 mod 最新 pending 判决行直取（D4 预算好的值
        原样落账），GUI 没有第二次填数字的机会，写入值永不高估（R18）
        由结构保证。version_written=None 的行照确认 = 版本未知态合法写入
        （守卫照旧拒绝备份）。
        逐条（D39 upsert 语义）：
        1. 无 pending 行 → ValueError（先经 record_verdicts 落判决）；
        2. 黑名单 → ValueError（D11：upsert 建行之前拦截）；
        3. mods 无行 → 取判决行 game_id/title/file_size 建行
           （status=downloaded）；有行 → tracked→downloaded 迁移在此
           发生（D6 唯一入口；deleted 拒绝，先恢复）；
        4. 该 mod 全部 pending success/claim/manual 行填 confirmed_at。
        幂等：重复确认同一批 → 第二次报错（队列已无其行），账面不变。"""

    @abstractmethod
    def claim_accept(self, mod_id: int, version: int | None, *,
                     source: str = "claim",
                     local_size: int | None = None) -> None:
        """★认领入账（D7），一个事务：
        1. 落 verdict 行 kind='claim'、version_written=version、
           confirmed_at=now（认领即背书，不进待确认队列）
        2. mods upsert（同 confirm_items 语义）：建行（元数据缺 NULL
           下轮检测补全，旧决策 20 同款）或 tracked→downloaded；
           已 downloaded 行照录 acf 值（D7 两情形，下轮复检自愈）
        3. local_size 可选回填（盘点顺手的展示列，R19 禁入判定）
        黑名单 → ValueError。mtimes 与盘面其余产物禁止出现在参数里（R19）。"""

    @abstractmethod
    def revoke_confirmation(self, mod_id: int) -> None:
        """撤销确认 = 唯一回滚（D8）：confirmed_version / confirmed_at /
        confirmed_source 全部置 NULL，status 不动（两轴正交）。
        verdict_log 不动（判决有史可查）。下轮检测自然重报。
        mod 不存在 → ValueError。"""

    @abstractmethod
    def list_verdicts(self, mod_id: int, limit: int = 10) -> list[Verdict]:
        """某 mod 最近判决（D22 详情面板"最近判决"小节），新→旧。
        含 confirmed 与 pending——超时次数多一眼可见。"""

    # ============ mod_snapshots（2，签名瘦身） ============
    @abstractmethod
    def add_snapshot(self, mod_id: int, *, time_updated: int | None = None,
                     snapshot_at: int | None = None) -> None:
        """★复合（沿旧版滚动淘汰）。v2：manifest/local_timeupdated 参数移除。"""
    @abstractmethod
    def list_snapshots(self, mod_id: int) -> list[Snapshot]: ...

    # ============ translations（D35，2） ============
    @abstractmethod
    def get_translation(self, mod_id: int, target_lang: str) -> Translation | None:
        """读缓存。命中与否由调用方对比 source_hash（失配 → 重译 REPLACE）。"""
    @abstractmethod
    def save_translation(self, mod_id: int, *, source_hash: str,
                         target_lang: str, engine: str | None,
                         text_translated: str) -> None:
        """INSERT OR REPLACE（mod_id 主键，单条当前译文）。mod 不在账本 →
        FK IntegrityError 上抛（简介只服务在账 mod）。"""

    # ============ backups（6）/ operations_log（3）/ failed_mods 查询 /
    #            special_mod_alerts（3）/ purged_mods（5）/ mod_dependencies（4）============
    # 签名与语义与旧契约逐字一致，docstring 见旧版（本批不重复粘贴）。
    @abstractmethod
    def add_backup(self, mod_id: int, backup_path: str, size_bytes: int,
                   version_timeupdated: int, *, manifest: str | None = None,
                   note: str | None = None) -> Backup: ...
    @abstractmethod
    def get_backup(self, backup_id: int) -> Backup | None: ...
    @abstractmethod
    def list_backups(self, mod_id: int | None = None, *,
                     include_pinned: bool = True,
                     oldest_first: bool = False) -> list[Backup]: ...
    @abstractmethod
    def set_pinned(self, backup_id: int, pinned: bool) -> None: ...
    @abstractmethod
    def delete_backup_record(self, backup_id: int) -> None: ...
    @abstractmethod
    def sum_backup_bytes(self) -> int: ...

    @abstractmethod
    def add_operation(self, command: str, *,
                      backup_id: int | None = None) -> int: ...
    @abstractmethod
    def finish_operation(self, op_id: int, *, error_count: int = 0,
                         result: str | None = None) -> None: ...
    @abstractmethod
    def list_operations(self, limit: int = 50) -> list[OperationLog]: ...

    @abstractmethod
    def list_failed(self, game_id: int) -> list[FailedMod]: ...

    @abstractmethod
    def add_alert(self, mod_id: int, remote_time_updated: int, *,
                  diff_seconds: int | None = None,
                  was_downloaded: bool = False,
                  note: str | None = None) -> Alert: ...
    @abstractmethod
    def get_last_alert(self, mod_id: int) -> Alert | None: ...
    @abstractmethod
    def list_alerts(self, mod_id: int) -> list[Alert]: ...

    @abstractmethod
    def list_purged(self) -> list[PurgedMod]: ...
    @abstractmethod
    def filter_purged(self, mod_ids: Iterable[int]) -> set[int]: ...
    @abstractmethod
    def is_purged(self, mod_id: int) -> bool: ...
    @abstractmethod
    def remove_purged(self, mod_id: int) -> None: ...
    @abstractmethod
    def add_purged(self, mod_id: int, game_id: int, *,
                   title: str | None = None,
                   note: str | None = None) -> None: ...

    @abstractmethod
    def replace_dependencies(self, mod_id: int, required_ids: list[int],
                             fetched_at: int | None = None) -> None: ...
    @abstractmethod
    def list_dependencies(self, mod_id: int) -> list[int]: ...
    @abstractmethod
    def list_dependents(self, mod_id: int) -> list[int]: ...
    @abstractmethod
    def latest_dependency_fetch(self) -> int | None: ...

    # ============ 账本导入导出（沿旧口径，表集换新） ============
    @abstractmethod
    def export_all(self) -> dict: ...
    @abstractmethod
    def import_all(self, exported: dict) -> None: ...
    @abstractmethod
    def set_manual_version(self, mod_id: int, version: int | None, *,
                           note: str | None = None) -> None:
        """★手动设定本地版本（mod 库页右键「设定本地版本…」，D5③第三扇门）：
        用户人工核对后输入版本号——最重的人工背书。落 manual 判决行当场
        盖戳 + 写确认三件套（R17 唯一写入点之一）；tracked/failed →
        downloaded；deleted 拒绝（先恢复）。version=None 合法 = 版本未知态。"""

    @abstractmethod
    def backfill_local_sizes(self, sizes: dict[int, int]) -> int:
        """盘点回填本地占用（D23 批次收尾只读盘点）：磁盘/acf 盘点得到的
        各 mod 目录大小批量写入展示列。只动 local_size，绝不碰版本字段
        （R19）；账上没有的编号直接忽略（回填不建行）。返回实际更新条数。"""
