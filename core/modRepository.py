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
from dataclasses import dataclass

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
    "local_timeupdated DESC",   # 本地版本（T19⑰ 扩列）
    "local_timeupdated ASC",
    "status ASC",               # 状态：按库内值字母序分组聚拢（T19⑰）
    "status DESC",
    "is_special DESC",          # 特别关注：关注的在前（单向，反向无场景）
    "first_tracked_at DESC",
    "first_tracked_at ASC",
    "last_checked_at DESC",
    "mod_id ASC",
    "mod_id DESC",
})


# 整库导出/导入的表清单与灌库顺序（T19 dataExporter）。顺序 = 外键
# 依赖序：父表在前（games → mods → 各子表）。灌库按此序、清库按
# 倒序，RESTRICT 外键两头都不卡；导出的 JSON 各表也按此序，人肉
# 打开先档案后 mod，可读。
LEDGER_TABLES: tuple[str, ...] = (
    "games", "mods", "mod_snapshots", "backups",
    "operations_log", "failed_mods", "special_mod_alerts",
)
# ---------- 查询结果结构（不是账本行，所以不放 models.py） ----------
# models.py 存的是"账本里一行"的形状（Game/Mod/Backup…）；下面两个是
# 查询服务的返回形状（多表聚合 / 联表），只被本契约与其实现使用，
# 就近定义在契约层（modVerifier 的 VerifyResult 是同样的就近先例）。

@dataclass
class GameDeletionSummary:
    """删除档案前的盘点结果。三个用途：
    1. 删除确认弹窗把数字摆给用户看（"将删除 N 条 mod、M 条备份登记…"）
    2. backup_paths 是登记过的备份目录完整清单——账删掉之后，磁盘上
       要不要带走这些文件、带走哪些，由 GUI 层按用户勾选另行处理；
       repo 只管账本，绝不碰文件系统（与 delete_backup_record 同一分工）
    3. delete_game_deep 的返回值——删完写日志时报告删了什么
    """
    app_id: int
    mod_total: int           # 档案下 mod 记录总数（含所有状态）
    mod_deleted: int         # 其中软删除（status='deleted'）条数
    failed_count: int        # 失效归档（failed_mods）条数
    backup_count: int        # 备份登记条数
    backup_bytes: int        # 登记的备份总字节数
    backup_paths: list[str]  # 备份目录名清单（R1：账本存相对 backup_dir 的
    # 目录名；拼回完整路径 + 过保险丝是 GUI 层的事）


@dataclass
class BackupOverviewRow:
    """备份总览页的一行：登记信息 + 归属信息，联表一次取齐。
    磁盘上文件还在不在，不在本结构里——repo 只管账；GUI 拿到行后
    逐行 Path.exists() 判定"盘上"状态（与备份页「盘上」列同一分工）。
    """
    backup_id: int
    mod_id: int
    mod_title: str | None    # mod 记录可能没标题（acf 冷启动、API 未补）
    mod_status: str          # 所属 mod 的账面状态（软删/失效的备份要能看出来）
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

    # ============ 档案删除与备份总览（3） ============

    @abstractmethod
    def game_deletion_summary(self, app_id: int) -> GameDeletionSummary:
        """删除前的只读盘点：数一数这个档案名下都有什么。
        给删除确认弹窗用——先看清楚，再决定删不删（数据先可见再动手）。
        app_id 不存在 → ValueError（口径同 update_game）。"""

    @abstractmethod
    def delete_game_deep(self, app_id: int) -> GameDeletionSummary:
        """删档案连同名下全部从属记录（一个事务，要么全清要么原样）。

        与 delete_game 的分工：delete_game 是带 RESTRICT 闸的低层原语
        （名下有 mod 就拒绝，防误删）；本方法是"用户看清楚之后明确
        要走"的完整通道，按外键依赖从子到父依次清空：
          特殊提醒 → 快照 → 备份登记 → mod → 失效归档 → 档案
        （提醒和快照本有 CASCADE 兜底，仍显式先删——读代码的人不用
        背外键图也知道发生了什么。）

        范围拍板：
        - 备份登记删账；磁盘文件是否带走由 GUI 层按用户勾选另行处理
          （repo 绝不碰文件系统）；删账前 GUI 应先做数据库备份兜底
        - 失效归档随档案删（证据属于游戏，游戏没了证据无从谈起）
        - 软删除的 mod 一并物理清除（档案没了没有"等恢复"可言）
        - operations_log 不删：全局历史，设计上活得比备份久；其中
          backup_id 指向本批备份的外键是 SET NULL，自动置空不悬空

        返回删除前的盘点（= 删掉了什么，供日志报告）。
        档案不存在 → ValueError（不进事务，动手之前就拦下）。"""

    @abstractmethod
    def list_backups_overview(self, game_id: int | None = None) -> list[BackupOverviewRow]:
        """备份总览页的数据源：全部备份登记联表取齐归属信息。
        game_id=None 返回全部档案的；传则只看该档案。
        新→旧排序（同一时刻按 id 倒序，顺序稳定）。只读，不碰磁盘。"""


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
                  limit: int | None = None,
                  title_contains: str | None = None,
                  note_contains: str | None = None,
                  mod_id: int | None = None,
                  size_min: int | None = None,
                  size_max: int | None = None,
                  updated_from: int | None = None,
                  updated_to: int | None = None,
                  tags_all: Iterable[str] | None = None) -> list[Mod]:
        """mod 列表页的万能查询（GUI 与导出共用）。
        status: None=全部 / 'tracked' / 'downloaded' / 'deleted' / 'failed'
        special_only: True 时只返回 is_special=1
        color_tag: 精确匹配
        search: 对 title / note 做 LIKE %xx%（SQLite LIKE 对 ASCII 不分大小写）
        order_by: 必须取自 ALLOWED_ORDERS，否则 ValueError
        limit: None=不限制（有标签筛时改为筛完再切，见下）
        —— 以下为 T12 高级筛选新增，条件之间全部 AND 叠加，
           None / 空 = 不限；老调用方一个字都不用改 ——
        title_contains / note_contains: 只对 title / note 各自做 LIKE %xx%。
          与 search 的分工：search 是两处合查的快筛，这两个分开指定
        mod_id: 精确编号
        size_min / size_max: 字节数。口径与列表页"大小"列一致——
          本地 local_size 优先，acf 缺失退 API file_size；两者都缺的
          条目不落在任何区间里（大小未知 ≠ 大小为 0）
        updated_from / updated_to: 远端版本时间范围（epoch 秒，闭区间）；
          time_updated 为 NULL（从没查过远端）的条目不命中任何范围
        tags_all: 标签精确匹配、须同时全有。标签存 JSON 文本（schema
          约定 3：数据库不做 JSON 结构化查询），实现层取回后在 Python
          里比对——limit 因此在筛完之后生效"""

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

    # ============ 账本导入导出（2，T19 dataExporter） ============
    @abstractmethod
    def export_all(self) -> dict:
        """整库倒出（T19 dataExporter 的唯一读取原语）：
        {"user_version": 导出时的 schema 版本,
         "tables": {表名: [行 dict, ...]}}，表序见 LEDGER_TABLES。
        行 = dataclass 自然类型（tags 是 list、is_special 是 bool、
        JSON 字段是 dict）——存储格式（0/1、JSON 文本）绝不越过本
        方法（models.py 边界约定）。行内 id 等库生成值原样保留，
        导入时据此复原跨表引用（operations_log.backup_id 等）。"""

    @abstractmethod
    def import_all(self, exported: dict) -> None:
        """清库重灌（export_all 的逆操作，完整账本的导入语义）。
        exported 形状同 export_all 返回值。user_version 比本库
        PRAGMA user_version 新 → ValueError（旧程序读不懂新结构，
        绝不硬吃）；tables 的键 ⊄ LEDGER_TABLES → ValueError；
        任何一行不合法 → 整体回滚，绝不留半截账。
        本方法只做自然类型 → 存储格式的转换，不做行级校验——那是
        dataExporter 的职责。自带事务（_atomic）；外层再包
        transaction() 也安全（自动并入）。"""
