"""数据访问契约层（接口）
"""
"""
core/modRepository.py · 数据访问契约层（接口）

为什么先写接口再写实现：
上层（workflows / gui）只 import 本文件的类，永远不写 SQL；
sqliteRepository.py 负责把每个方法翻译成真正的 SQL。
好处：上层不关心存储细节（将来换存储引擎不动上层）；
写测试可以塞假实现；本文件本身就是"这个项目有哪些数据能力"的完整清单。

通用约定（调用方必读，共 6 条）：
1. 时间一律 Unix 时间戳（int，秒），与 Steam API / acf 原生格式一致
2. get_game / get_mod / get_backup / get_last_alert 查不到时返回 None，
   不抛异常——"找不到怎么办"由调用方决定
3. 违反唯一约束 / 外键约束时抛 sqlite3.IntegrityError，本层绝不静默吞掉
   （让错误显式爆炸，好过数据悄悄错下去）
4. 单个方法自带事务：成功自动 commit，异常自动回滚，调用方无感；
   多个方法需要打包成"要么全成要么全败"时，用 with repo.transaction(): 包住
5. 可选参数中 None = "不修改该字段"；需要"清空"的场景用专门 setter
   （如 set_color_tag(mod_id, None)）
6. 本文件不含一行 SQL，也不 import sqlite3——它只是契约
"""
from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import AbstractContextManager
from pathlib import Path

from core.models import (
    Alert, Backup, FailedMod, Game, Mod, OperationLog, Snapshot,
)

# list_mods 的排序白名单。为什么存在：ORDER BY 无法用 ? 参数绑定，只能拼进
# SQL 字符串，所以用白名单杜绝注入；要新排序就在这里加、实现层同步支持
ALLOWED_ORDERS: frozenset[str] = frozenset({
    "time_updated DESC",
    "time_updated ASC",
    "title ASC",
    "title DESC",
    "local_size DESC",
    "first_tracked_at DESC",
    "first_tracked_at ASC",
    "last_checked_at DESC",
    "mod_id ASC",
    "mod_id DESC",
})



class ModRepository(ABC):
    """全部数据能力的契约。唯一实现：sqliteRepository.SQLiteRepository。"""

    # ============ 基础设施（3） ============

    @abstractmethod
    def __init__(self, db_path: str | Path, *, snapshot_keep: int = 5) -> None:
        """打开连接、设置 PRAGMA（foreign_keys=ON / WAL / busy_timeout）。
        snapshot_keep：每个 mod 保留的快照条数，由主窗口从设置注入，
        测试可以塞小数字快速验证滚动淘汰。
        若库为空（user_version==0）自动执行 core/schema.sql 建表——幂等，
        重复调用不报错，所以测试里可以随便 new 临时库。"""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """事务上下文：
        with repo.transaction():
            repo.add_mod(a)
            repo.add_snapshot(a.mod_id, ...)
        块内任何异常 → 全部回滚；正常退出 → 统一 commit。
        不要嵌套使用。迁移脚本灌 237 个 mod、导入分享包都靠它保证原子性。"""

    @abstractmethod
    def close(self) -> None:
        """关闭连接。GUI 关闭事件 / 测试 teardown 时调用。"""

    # ============ games（5） ============

    @abstractmethod
    def add_game(self, app_id: int, name: str, download_dir: str,
                 game_mod_dir: str | None = None,
                 backup_dir: str | None = None) -> None:
        """新增游戏档案（向导步骤 3）。app_id 已存在 → IntegrityError。"""

    @abstractmethod
    def get_game(self, app_id: int) -> Game | None: ...

    @abstractmethod
    def list_games(self) -> list[Game]:
        """全部档案，按创建时间旧→新。游戏切换器用。"""

    @abstractmethod
    def update_game(self, app_id: int, *, name: str | None = None,
                    download_dir: str | None = None,
                    game_mod_dir: str | None = None,
                    backup_dir: str | None = None) -> None:
        """修改档案字段，None=不修改。app_id 不存在 → ValueError。"""

    @abstractmethod
    def delete_game(self, app_id: int) -> None:
        """删除档案。档案下仍有 mod 时被外键 RESTRICT 拦下 → IntegrityError。
        这是故意的保护：防止误删带 237 个 mod 的档案。"""

    # ============ mods（12） ============

    @abstractmethod
    def add_mod(self, mod: Mod) -> None:
        """插入一条 mod。mod_id 重复 → IntegrityError。
        去重不是本方法的职责：flow 层批量导入前必须先调 filter_existing_ids，
        这里撞主键就炸——让 bug 显式暴露，好过静默跳过。"""

    @abstractmethod
    def get_mod(self, mod_id: int) -> Mod | None: ...

    @abstractmethod
    def filter_existing_ids(self, mod_ids: Iterable[int]) -> set[int]:
        """传入一批 id，返回其中已存在于 mods 表的子集。
        批量导入去重用：一条 SQL 代替 N 次 get_mod。"""

    @abstractmethod
    def list_mods(self, game_id: int, *, status: str | None = None,
                  special_only: bool = False, color_tag: str | None = None,
                  search: str | None = None,
                  order_by: str = "time_updated DESC",
                  limit: int | None = None) -> list[Mod]:
        """mod 列表页的万能查询（GUI 与导出共用）。
        status: None=全部 / 'tracked' / 'downloaded' / 'deleted' / 'failed'
        special_only: True 时只返回 is_special=1
        color_tag: 精确匹配
        search: 对 title / note 做 LIKE %xx%（SQLite LIKE 对 ASCII 不分大小写）
        order_by: 必须取自 ALLOWED_ORDERS，否则 ValueError
        limit: None=不限制"""

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
        """Steam API 查询结果回写。None=不修改该字段。
        只动远端侧数据（time_updated 在这里更新）；local_* 三件套是 acf 的
        地盘，本方法绝不触碰——两套事实源互不越界。
        last_time_updated：检测到远端版本变化时，把"当次的远端值"记下来，
        作为下一轮检测的比较基准，也是"距上次更新多少天"的计算分母。"""

    @abstractmethod
    def touch_checked(self, mod_ids: Iterable[int],
                      *, checked_at: int | None = None) -> None:
        """批量刷新 last_checked_at（默认=当前时刻）。
        API 整体查询失败时也要调用——"检测过"这个事实本身值得记录，
        否则"多久没检测"的展示会骗人。checked_at 参数供迁移脚本保留原时间。"""

    @abstractmethod
    def update_local_state(self, mod_id: int, *, local_timeupdated: int,
                           manifest: str | None = None,
                           local_size: int | None = None,
                           status: str | None = None) -> None:
        """acf 扫描回写本地版本三件套。status 可选随动：
        扫描发现 acf 里有此 id 时传 'downloaded'（tracked→downloaded 的
        状态迁移就发生在这一刻）。mod_id 不存在 → ValueError。"""

    @abstractmethod
    def update_status(self, mod_id: int, status: str) -> None:
        """通用状态迁移（如 deleted 恢复）。status 值非法 → ValueError。"""

    @abstractmethod
    def set_note(self, mod_id: int, note: str | None) -> None:
        """设置/清空备注（右键菜单）。None=清空。"""

    @abstractmethod
    def set_color_tag(self, mod_id: int, color_tag: str | None) -> None:
        """设置/清除颜色标记。None=清除。"""

    @abstractmethod
    def set_special(self, mod_id: int, is_special: bool) -> None:
        """标记/取消特别关注。"""

    @abstractmethod
    def mark_deleted(self, mod_id: int, last_state: dict) -> None:
        """软删除三连（一个事务内）：status→'deleted' + deleted_at=now +
        末态 dict 序列化进 deleted_last_state。last_state 由 flow 层组装
        （哪些字段值得留末态是业务决定，repo 只管存取）。"""

    # ============ mod_snapshots（2） ============

    @abstractmethod
    def add_snapshot(self, mod_id: int, *, time_updated: int | None = None,
                     manifest: str | None = None,
                     local_timeupdated: int | None = None,
                     snapshot_at: int | None = None) -> None:
        """★复合方法，一个事务内：插入快照 + 滚动删除该 mod 超过保留条数
        （构造时注入的 snapshot_keep）的更旧快照。业务规则在 repo 层实现，
        调用方永远不用关心保留条数。
        snapshot_at=None 表示取当前时刻；迁移脚本传原值保留历史。"""

    @abstractmethod
    def list_snapshots(self, mod_id: int) -> list[Snapshot]:
        """某 mod 的快照，新→旧。详情页版本时间线用。"""

    # ============ backups（6） ============

    @abstractmethod
    def add_backup(self, mod_id: int, backup_path: str, size_bytes: int,
                   version_timeupdated: int, *, manifest: str | None = None,
                   note: str | None = None) -> Backup:
        """备份成功落盘后登记。返回的 Backup 带 id——供 operations_log
        关联（backup_id）。backup_path 重复 → IntegrityError（UNIQUE 防
        同一目录登记两次）。"""

    @abstractmethod
    def get_backup(self, backup_id: int) -> Backup | None: ...

    @abstractmethod
    def list_backups(self, mod_id: int | None = None, *,
                     include_pinned: bool = True,
                     oldest_first: bool = False) -> list[Backup]:
        """备份列表。mod_id=None 表示全部（管理页）。
        oldest_first=True 时旧→新——保留策略扫描时从最旧开始清腾。"""

    @abstractmethod
    def set_pinned(self, backup_id: int, pinned: bool) -> None:
        """钉住/解钉。钉住的备份豁免自动清理。"""

    @abstractmethod
    def delete_backup_record(self, backup_id: int) -> None:
        """只删数据库记录。磁盘目录由 backupManager 负责——两层职责
        严格分开，本方法绝不碰文件系统。"""

    @abstractmethod
    def sum_backup_bytes(self) -> int:
        """全部备份的总字节数。全局总量配额预检（勾选对话框的
        "预计 X GB / 上限 Y GB"）用，一条聚合 SQL。"""

    # ============ operations_log（3） ============

    @abstractmethod
    def add_operation(self, command: str, *,
                      backup_id: int | None = None) -> int:
        """命令执行前先登记，返回 op_id。执行后必须配对调用 finish_operation。"""

    @abstractmethod
    def finish_operation(self, op_id: int, *, error_count: int = 0,
                         result: str | None = None) -> None:
        """outputAnalyzer 分析完终端输出后回填结果。"""

    @abstractmethod
    def list_operations(self, limit: int = 50) -> list[OperationLog]:
        """操作日志，新→旧。"""

    # ============ failed_mods（3） ============

    @abstractmethod
    def mark_failed(self, mod_id: int, reason: str) -> None:
        """★复合方法，一个事务内：mods.status→'failed' + 复制当前字段为
        last_known_state JSON 写入 failed_mods（mod_id 不设外键，归档独立
        存活）。原 mods 行保留不删。mod_id 不存在 → ValueError。"""

    @abstractmethod
    def list_failed(self, game_id: int) -> list[FailedMod]:
        """某游戏的失效归档，按 detected_at 新→旧。
        "标题相同→建议关联"的比对由 flow 层在 Python 里做（归档量小，
        不值得上 SQL 查 JSON）。"""

    @abstractmethod
    def replace_failed_mod(self, old_mod_id: int, new_mod_id: int) -> None:
        """★复合方法（result=9 关联替换，一个事务内完成 5 步）：
        1. 旧 mod 的 note / color_tag / is_special 迁移到新 mod
           （仅当新 mod 对应字段为空/False 时才覆盖，不抹掉新值）
        2. mod_snapshots 中旧 id 的快照改挂到新 id
        3. failed_mods 中 old_mod_id 的 replaced_by = new_mod_id
        old 或 new 不存在于 mods 表 → ValueError。"""

    # ============ special_mod_alerts（3） ============

    @abstractmethod
    def add_alert(self, mod_id: int, remote_time_updated: int, *,
                  diff_seconds: int | None = None,
                  was_downloaded: bool = False,
                  note: str | None = None) -> Alert:
        """特殊 mod 提醒落一行（每次提醒一条）。返回的 Alert 带 id。"""

    @abstractmethod
    def get_last_alert(self, mod_id: int) -> Alert | None:
        """某 mod 最近一次提醒。"同一远端版本只提醒一次"的比对依据：
        flow 层拿 remote_time_updated 与这条记录比对。"""

    @abstractmethod
    def list_alerts(self, mod_id: int) -> list[Alert]:
        """提醒历史，新→旧（v1.1 后的提醒历史 UI 用）。"""
