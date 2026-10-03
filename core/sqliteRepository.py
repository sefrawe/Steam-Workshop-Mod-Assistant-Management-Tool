"""ModRepository 契约的 SQLite 实现（v2 判决制）
"""
"""
core/sqliteRepository.py · 契约的唯一实现。

本文件做的事：把 modRepository.ModRepository 的每个方法翻译成真正的 SQL。
上层（workflows / gui）只 import 契约文件、永远不写 SQL；将来换存储引擎
只动这里，上层一个字不改。

三个贯穿全文的实现约定（沿旧版，原样保留）：
1. isolation_level=None（真自动提交）：每条语句立即落盘；需要多语句
   原子性的复合方法用显式 BEGIN/COMMIT（_atomic）。单语句方法不用管
   事务，复合方法管自己的，transaction() 管调用方的，三层互不干扰。
2. 边界转换只在读写发生：库里的 0/1 和 JSON 文本，出 repo 之前必须
   变成 bool 和 list/dict；调用方永远见不到存储格式。
3. 让错误显式爆炸：UPDATE 影响 0 行 = 目标不存在 = ValueError；
   约束冲突 = sqlite3.IntegrityError，本层绝不静默吞掉。

v2 判决制的核心变化（为什么本地版本不再由扫描自动写入）：
旧版 mods 表存 local_timeupdated，acf 扫描到什么写什么——把"盘面
推测"当成了事实。新版改为 confirmed_version（人工确认过的本地版本），
全项目只有"确认门"的几个方法能写它。acf、文件修改时间等盘面数据
从此进不了版本字段（红线 R19），防的是自动写入把猜测当事实。
"""
import json
import re
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from core import constants
from core.modRepository import (
    ALLOWED_ORDERS,
    BackupOverviewRow,
    GameDeletionSummary,
    LEDGER_TABLES,
    ModRepository,
)
from core.models import (
    Alert,
    Backup,
    FailedMod,
    Game,
    Mod,
    OperationLog,
    PurgedMod,
    Snapshot,
    Translation,
    Verdict,
)

# 合法状态值单源取自 constants（D37），本文件不再手抄一份
_TRACKED = constants.STATUS_TRACKED
_DOWNLOADED = constants.STATUS_DOWNLOADED
_DELETED = constants.STATUS_DELETED

# 判决种类合法值（写入前校验）；确认队列只摆这三种——
# 超时/失败只进判决史（详情面板看），不进待确认清单（D22）
_VALID_KINDS = frozenset(constants.VERDICT_KIND_ZH)
_QUEUE_KINDS: tuple[str, ...] = ("success", "claim", "manual")

# 确认来源合法值（写 mods.confirmed_source 前校验）
_VALID_SOURCES = frozenset(constants.CONFIRMED_SOURCE_ZH)

_DEFAULT_SNAPSHOT_KEEP = 10    # 远端观测史每 mod 默认保留条数（D36）
_DEFAULT_VERDICT_KEEP = 50     # 判决史"未确认行"每 mod 默认保留条数（D23）
_VERDICT_MAX_AGE = 90 * 86400  # 未确认判决行的 90 天保留线（秒，D23）
_DEFAULT_DB_BACKUP_KEEP = 3    # 数据库自身备份滚动保留份数（R16，沿旧版）

_CHUNK = 500  # IN (...) 分片大小：SQL 变量占位符有上限，分片永不出错

# 新世系标记表：打开 v1 库时用来鉴别"这是新工具的库还是上一代工具的库"
# （两代工具的 user_version 都从 1 起步，光看版本号分不清，见 __init__）
_NEW_WORLD_MARKER_TABLE = "verdict_log"


def _now() -> int:
    return int(time.time())


def _dumps(obj) -> str:
    """ensure_ascii=False：中文备注原样存储，可读性好且省空间"""
    return json.dumps(obj, ensure_ascii=False)


class SQLiteRepository(ModRepository):
    # ---------- 基础设施 ----------

    def __init__(self, db_path: str | Path, *,
                 snapshot_keep: int = _DEFAULT_SNAPSHOT_KEEP,
                 verdict_keep: int = _DEFAULT_VERDICT_KEEP,
                 db_backup_keep: int = _DEFAULT_DB_BACKUP_KEEP) -> None:
        # 保留条数至少为 1：0 或负数会让对应功能整个失效，直接拦在门口
        self._snapshot_keep = max(1, int(snapshot_keep))
        self._verdict_keep = max(1, int(verdict_keep))
        self._db_backup_keep = max(1, int(db_backup_keep))
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 查询结果支持按列名取值
        # foreign_keys 是连接级属性，每个连接都必须重新打开
        # （schema.sql 里的那条只管建表期）
        self._conn.execute("PRAGMA foreign_keys = ON")
        # 遇到锁（比如别的程序正开着这个库）先等 5 秒再报错，而不是立刻炸
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._in_transaction = False

        # ---- 开库三分支：空库建表 / 新世系 v1 直接开 / 其余拒绝 ----
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # 空库 → 跑 schema.sql 建表（脚本自己会把 user_version 置 1）。
            # 幂等：重复打开同一个空库不报错，测试里可以随便造临时库
            schema = (Path(__file__).parent / "schema.sql").read_text(
                encoding="utf-8")
            self._conn.executescript(schema)
        elif version == 1:
            # v1 有两种可能：本工具的新世系库，或上一代工具的旧库
            # （旧版 user_version 同样从 1 起步）。用标记表鉴别：
            # 新世系必有 verdict_log，旧库没有。拿错库要大声说清，
            # 绝不开着错的库继续跑（否则查询会以更难懂的方式炸）。
            marker = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (_NEW_WORLD_MARKER_TABLE,)).fetchone()
            if marker is None:
                raise ValueError(
                    f"{self._path} 是上一代工具的旧账本：请到"
                    "「换机迁移」页先做一次旧账迁移，把旧数据搬进新库；"
                    "不要让新旧两代工具指向同一个数据库文件")
        else:
            # 版本号比本工具认识的新（或旧世系 v2/v3）——都读不懂，拒绝。
            # 显式爆炸好过静默错读（约定 3）
            raise ValueError(
                f"{self._path} 的数据库结构版本（v{version}）本工具读不懂："
                "旧世系库请走「换机迁移」的旧账迁移；更新版本的库请先"
                "升级本工具再打开")

    @contextmanager
    def transaction(self):
        """调用方的事务：块内任何异常全部回滚，正常退出统一提交。
        不支持嵌套（嵌套 = 调用方没想清楚边界，直接炸出来）。"""
        if self._in_transaction:
            raise RuntimeError("transaction() 不支持嵌套")
        self._conn.execute("BEGIN")
        self._in_transaction = True
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._in_transaction = False

    @contextmanager
    def _atomic(self):
        """复合方法内部用。已处于外层 transaction() 中时直接并入外层
        （不重复 BEGIN）——复合方法放进 transaction() 块里也保持整体原子。"""
        if self._in_transaction:
            yield
            return
        self._conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    # ---------- 内部工具 ----------

    def _insert(self, table: str, fields: dict) -> int:
        """动态 INSERT，返回 lastrowid。值为 None 的字段不写进列清单——
        让 schema 的 DEFAULT 生效（显式写 NULL 会撞 NOT NULL DEFAULT 列）"""
        cols = {k: v for k, v in fields.items() if v is not None}
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        cur = self._conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})",
            list(cols.values()),
        )
        return cur.lastrowid

    def _dynamic_update(self, table: str, pk_col: str, pk_val,
                        fields: dict) -> int:
        """动态 UPDATE，返回 rowcount（0 = 目标行不存在）。
        表名/列名全部来自代码字面量，用户数据只走 ? 参数——无注入"""
        sets = ", ".join(f"{c} = ?" for c in fields)
        cur = self._conn.execute(
            f"UPDATE {table} SET {sets} WHERE {pk_col} = ?",
            [*fields.values(), pk_val],
        )
        return cur.rowcount

    @staticmethod
    def _require(rowcount: int, message: str) -> None:
        if rowcount == 0:
            raise ValueError(message)

    def _require_mod(self, mod_id: int) -> None:
        if self.get_mod(mod_id) is None:
            raise ValueError(f"mod {mod_id} 不存在")

    @staticmethod
    def _in_chunks(ids: list[int]) -> Iterable[list[int]]:
        for i in range(0, len(ids), _CHUNK):
            yield ids[i:i + _CHUNK]

    @staticmethod
    def _to_storage(table: str, row: dict) -> dict:
        """自然类型 dict → 存储格式 dict（import_all 专用），与 _row 系列
        互为镜像。带特殊类型的列全项目就两类（JSON 文本、0/1 布尔）。
        verdict_log / translations 的字段全是原始类型，无需转换。"""
        data = dict(row)
        if table == "mods":
            if data.get("tags") is not None:
                data["tags"] = _dumps(data["tags"])
            if data.get("deleted_last_state") is not None:
                data["deleted_last_state"] = _dumps(data["deleted_last_state"])
            if data.get("is_special") is not None:
                data["is_special"] = int(data["is_special"])
        elif table == "backups":
            if data.get("pinned") is not None:
                data["pinned"] = int(data["pinned"])
        elif table == "failed_mods":
            if data.get("last_known_state") is not None:
                data["last_known_state"] = _dumps(data["last_known_state"])
        elif table == "special_mod_alerts":
            if data.get("was_downloaded") is not None:
                data["was_downloaded"] = int(data["was_downloaded"])
        return data

    # ---------- 行 → dataclass 转换（边界转换集中在这里） ----------

    @staticmethod
    def _game(row) -> Game:
        return Game(app_id=row["app_id"], name=row["name"],
                    download_dir=row["download_dir"],
                    game_mod_dir=row["game_mod_dir"],
                    backup_dir=row["backup_dir"],
                    created_at=row["created_at"])

    @staticmethod
    def _mod(row) -> Mod:
        # v2：本地版本读 confirmed_* 三件套；旧版的
        # local_timeupdated / manifest / local_path 已随判决制移除
        return Mod(
            mod_id=row["mod_id"],
            game_id=row["game_id"],
            status=row["status"],
            url=row["url"],
            title=row["title"],
            creator=row["creator"],
            time_created=row["time_created"],
            time_updated=row["time_updated"],
            last_time_updated=row["last_time_updated"],
            confirmed_version=row["confirmed_version"],
            confirmed_at=row["confirmed_at"],
            confirmed_source=row["confirmed_source"],
            local_size=row["local_size"],
            file_size=row["file_size"],
            subscriptions=row["subscriptions"],
            favorited=row["favorited"],
            views=row["views"],
            tags=json.loads(row["tags"]) if row["tags"] is not None else None,
            last_checked_at=row["last_checked_at"],
            preview_url=row["preview_url"],
            is_special=bool(row["is_special"]),
            note=row["note"],
            color_tag=row["color_tag"],
            deleted_at=row["deleted_at"],
            deleted_last_state=(json.loads(row["deleted_last_state"])
                                if row["deleted_last_state"] is not None
                                else None),
            first_tracked_at=row["first_tracked_at"],
        )

    @staticmethod
    def _snapshot(row) -> Snapshot:
        # v2 快照只记远端侧（本地版本进了判决制，不再进快照）
        return Snapshot(id=row["id"], mod_id=row["mod_id"],
                        snapshot_at=row["snapshot_at"],
                        time_updated=row["time_updated"])

    @staticmethod
    def _verdict(row) -> Verdict:
        return Verdict(
            id=row["id"],
            mod_id=row["mod_id"],
            kind=row["kind"],
            game_id=row["game_id"],
            version_trigger=row["version_trigger"],
            version_query=row["version_query"],
            version_written=row["version_written"],
            title=row["title"],
            file_size=row["file_size"],
            source=row["source"],
            confirmed_at=row["confirmed_at"],
            occurred_at=row["occurred_at"],
            note=row["note"],
        )

    @staticmethod
    def _translation(row) -> Translation:
        return Translation(
            mod_id=row["mod_id"],
            source_hash=row["source_hash"],
            target_lang=row["target_lang"],
            engine=row["engine"],
            text_translated=row["text_translated"],
            translated_at=row["translated_at"],
        )

    @staticmethod
    def _backup(row) -> Backup:
        return Backup(id=row["id"], mod_id=row["mod_id"],
                      backup_path=row["backup_path"],
                      size_bytes=row["size_bytes"],
                      version_timeupdated=row["version_timeupdated"],
                      manifest=row["manifest"], note=row["note"],
                      pinned=bool(row["pinned"]),
                      created_at=row["created_at"])

    @staticmethod
    def _oplog(row) -> OperationLog:
        return OperationLog(id=row["id"], command=row["command"],
                            executed_at=row["executed_at"],
                            error_count=row["error_count"],
                            result=row["result"],
                            backup_id=row["backup_id"])

    @staticmethod
    def _failed(row) -> FailedMod:
        return FailedMod(id=row["id"], mod_id=row["mod_id"],
                         game_id=row["game_id"], reason=row["reason"],
                         last_known_state=(json.loads(row["last_known_state"])
                                           if row["last_known_state"]
                                              is not None else None),
                         replaced_by=row["replaced_by"],
                         detected_at=row["detected_at"])

    @staticmethod
    def _alert(row) -> Alert:
        return Alert(id=row["id"], mod_id=row["mod_id"],
                     alert_at=row["alert_at"],
                     remote_time_updated=row["remote_time_updated"],
                     diff_seconds=row["diff_seconds"],
                     was_downloaded=bool(row["was_downloaded"]),
                     note=row["note"])

    # ---------- games ----------

    def add_game(self, app_id: int, name: str, download_dir: str,
                 game_mod_dir: str | None = None,
                 backup_dir: str | None = None) -> None:
        self._insert("games", {"app_id": app_id, "name": name,
                               "download_dir": download_dir,
                               "game_mod_dir": game_mod_dir,
                               "backup_dir": backup_dir})

    def get_game(self, app_id: int) -> Game | None:
        row = self._conn.execute(
            "SELECT * FROM games WHERE app_id = ?", (app_id,)).fetchone()
        return self._game(row) if row else None

    def list_games(self) -> list[Game]:
        rows = self._conn.execute(
            "SELECT * FROM games ORDER BY created_at, app_id").fetchall()
        return [self._game(r) for r in rows]

    def update_game(self, app_id: int, *, name: str | None = None,
                    download_dir: str | None = None,
                    game_mod_dir: str | None = None,
                    backup_dir: str | None = None) -> None:
        raw = {"name": name, "download_dir": download_dir,
               "game_mod_dir": game_mod_dir, "backup_dir": backup_dir}
        fields = {k: v for k, v in raw.items() if v is not None}
        if not fields:  # 全 None = 无事可做
            return
        self._require(self._dynamic_update("games", "app_id", app_id, fields),
                      f"游戏档案 {app_id} 不存在")

    def delete_game(self, app_id: int) -> None:
        # 档案下还有 mod 时外键 RESTRICT 会让 SQLite 直接抛
        # IntegrityError——保护逻辑交给数据库，这里一行都不用多写
        self._conn.execute("DELETE FROM games WHERE app_id = ?", (app_id,))

    # ---------- 档案删除与备份总览 ----------

    def game_deletion_summary(self, app_id: int) -> GameDeletionSummary:
        # get_game 查不到返回 None → 这里转成 ValueError（口径同 update_game）
        if self.get_game(app_id) is None:
            raise ValueError(f"游戏档案 {app_id} 不存在")
        mod_total = self._conn.execute(
            "SELECT COUNT(*) FROM mods WHERE game_id = ?",
            (app_id,)).fetchone()[0]
        mod_deleted = self._conn.execute(
            "SELECT COUNT(*) FROM mods WHERE game_id = ? AND status = ?",
            (app_id, _DELETED)).fetchone()[0]
        failed = self._conn.execute(
            "SELECT COUNT(*) FROM failed_mods WHERE game_id = ?",
            (app_id,)).fetchone()[0]
        # 备份登记挂在 mod 名下，要经 mods 才能找到所属档案
        bk = self._conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM backups
               WHERE mod_id IN (SELECT mod_id FROM mods WHERE game_id = ?)""",
            (app_id,)).fetchone()
        paths = [r[0] for r in self._conn.execute(
            """SELECT backup_path FROM backups
               WHERE mod_id IN (SELECT mod_id FROM mods WHERE game_id = ?)
               ORDER BY id""", (app_id,))]
        return GameDeletionSummary(
            app_id=app_id, mod_total=mod_total, mod_deleted=mod_deleted,
            failed_count=failed, backup_count=bk[0], backup_bytes=bk[1],
            backup_paths=paths)

    def delete_game_deep(self, app_id: int) -> GameDeletionSummary:
        """删档案连同名下全部从属记录（一个事务，要么全清要么原样）。
        v2 增量（C 级提案③）：判决史一并清——verdict_log 自带 game_id
        列（C 级提案①），一条直删，不用绕子查询。translations 挂在
        mods 行的外键 CASCADE 上，mod 删了缓存自动跟着走，不用管。"""
        # 盘点放事务外（纯读）；顺带完成存在性检查——不存在的档案
        # 在动手之前就报 ValueError，不进事务
        summary = self.game_deletion_summary(app_id)
        with self._atomic():
            # 子查询形式：mod 列表为空也不会出 SQL 语法问题
            self._conn.execute(
                """DELETE FROM special_mod_alerts WHERE mod_id IN
                                                        (SELECT mod_id FROM mods WHERE game_id = ?)""", (app_id,))
            self._conn.execute(
                """DELETE FROM mod_snapshots WHERE mod_id IN
                                                   (SELECT mod_id FROM mods WHERE game_id = ?)""", (app_id,))
            # 判决史按 game_id 直删（自带档案列的好处）
            self._conn.execute(
                "DELETE FROM verdict_log WHERE game_id = ?", (app_id,))
            # 备份登记是 mods 的 RESTRICT 外键——必须先删干净，
            # 否则下一步删 mod 会被数据库拦下（这正是闸的工作方式）
            self._conn.execute(
                """DELETE FROM backups WHERE mod_id IN
                                             (SELECT mod_id FROM mods WHERE game_id = ?)""", (app_id,))
            self._conn.execute(
                "DELETE FROM mods WHERE game_id = ?", (app_id,))
            # 失效归档随档案删：mod_id 本来就没外键（证据表），
            # 不会自己消失，必须显式来删
            self._conn.execute(
                "DELETE FROM failed_mods WHERE game_id = ?", (app_id,))
            self._conn.execute(
                "DELETE FROM games WHERE app_id = ?", (app_id,))
            # operations_log 刻意不删（全局历史，活得比备份久）；
            # 其中 backup_id 的外键是 SET NULL，自动置空不悬空
        return summary

    def list_backups_overview(self, game_id: int | None = None
                              ) -> list[BackupOverviewRow]:
        sql = """SELECT b.id AS backup_id, b.mod_id, m.title AS mod_title,
                        m.status AS mod_status, m.game_id,
                        g.name AS game_name, b.backup_path, b.size_bytes,
                        b.version_timeupdated, b.manifest, b.created_at,
                        b.pinned
                 FROM backups b
                          JOIN mods m ON b.mod_id = m.mod_id
                          JOIN games g ON m.game_id = g.app_id"""
        params: list = []
        if game_id is not None:
            sql += " WHERE m.game_id = ?"
            params.append(game_id)
        sql += " ORDER BY b.created_at DESC, b.id DESC"
        return [BackupOverviewRow(
            backup_id=r["backup_id"], mod_id=r["mod_id"],
            mod_title=r["mod_title"], mod_status=r["mod_status"],
            game_id=r["game_id"], game_name=r["game_name"],
            backup_path=r["backup_path"], size_bytes=r["size_bytes"],
            version_timeupdated=r["version_timeupdated"],
            manifest=r["manifest"], created_at=r["created_at"],
            pinned=bool(r["pinned"]),
        ) for r in self._conn.execute(sql, params).fetchall()]

    # ---------- mods ----------

    def add_mod(self, mod: Mod) -> None:
        # 确认三件套：正常入账时全是 None（等确认门来写）；
        # 旧账迁移（legacyMigrate）会带着继承值进来
        self._insert("mods", {
            "mod_id": mod.mod_id,
            "game_id": mod.game_id,
            "status": mod.status,
            "url": mod.url,
            "title": mod.title,
            "creator": mod.creator,
            "time_created": mod.time_created,
            "time_updated": mod.time_updated,
            "last_time_updated": mod.last_time_updated,
            "confirmed_version": mod.confirmed_version,
            "confirmed_at": mod.confirmed_at,
            "confirmed_source": mod.confirmed_source,
            "local_size": mod.local_size,
            "file_size": mod.file_size,
            "subscriptions": mod.subscriptions,
            "favorited": mod.favorited,
            "views": mod.views,
            "tags": None if mod.tags is None else _dumps(mod.tags),
            "last_checked_at": mod.last_checked_at,
            "preview_url": mod.preview_url,
            "is_special": int(mod.is_special),
            "note": mod.note,
            "color_tag": mod.color_tag,
            "deleted_at": mod.deleted_at,
            "deleted_last_state": (None if mod.deleted_last_state is None
                                   else _dumps(mod.deleted_last_state)),
            "first_tracked_at": mod.first_tracked_at,
        })

    def get_mod(self, mod_id: int) -> Mod | None:
        row = self._conn.execute(
            "SELECT * FROM mods WHERE mod_id = ?", (mod_id,)).fetchone()
        return self._mod(row) if row else None

    def filter_existing_ids(self, mod_ids: Iterable[int]) -> set[int]:
        ids = sorted(set(mod_ids))
        found: set[int] = set()
        for chunk in self._in_chunks(ids):
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT mod_id FROM mods WHERE mod_id IN ({marks})",
                chunk).fetchall()
            found.update(r[0] for r in rows)
        return found

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
        """mod 列表页的万能查询。SQL 能表达的条件全部下推给 SQLite
        （值只走 ? 参数绑定，无注入）；标签是唯一例外——tags 存 JSON
        文本，数据库不做结构化查询，取回后在 Python 里精确比对。"""
        if order_by not in ALLOWED_ORDERS:
            raise ValueError(
                f"order_by 必须取自 ALLOWED_ORDERS，收到：{order_by!r}")
        where = ["game_id = ?"]
        params: list = [game_id]
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if special_only:
            where.append("is_special = 1")
        if color_tag is not None:
            where.append("color_tag = ?")
            params.append(color_tag)
        if search:
            # LIKE 的 % _ 是通配符，先转义用户输入，再声明 ESCAPE 字符
            safe = (search.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            where.append("(title LIKE ? ESCAPE '\\' "
                         "OR note LIKE ? ESCAPE '\\')")
            params += [f"%{safe}%", f"%{safe}%"]
        if title_contains:
            safe = (title_contains.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            where.append("title LIKE ? ESCAPE '\\'")
            params.append(f"%{safe}%")
        if note_contains:
            safe = (note_contains.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            where.append("note LIKE ? ESCAPE '\\'")
            params.append(f"%{safe}%")
        if mod_id is not None:
            where.append("mod_id = ?")
            params.append(mod_id)
        if size_min is not None:
            # 大小口径与列表页"大小"列一致：本地优先，缺失或为 0 退 API
            # file_size。NULLIF 把 0 变 NULL 交给 COALESCE 退——一行复刻 or
            where.append("COALESCE(NULLIF(local_size, 0), file_size) >= ?")
            params.append(size_min)
        if size_max is not None:
            where.append("COALESCE(NULLIF(local_size, 0), file_size) <= ?")
            params.append(size_max)
        if updated_from is not None:
            # time_updated 为 NULL（从没查过远端）→ 比较结果 NULL →
            # 不命中。"更新时间在某范围"对"不知道更新时间"的条目
            # 没有答案，排除是正确语义
            where.append("time_updated >= ?")
            params.append(updated_from)
        if updated_to is not None:
            where.append("time_updated <= ?")
            params.append(updated_to)
        # 标签：去重、去空、排序——顺序稳定，行为可预期
        want_tags = sorted({t for t in (tags_all or []) if t}) or None
        sql = (f"SELECT * FROM mods WHERE {' AND '.join(where)} "
               f"ORDER BY {order_by}")
        # LIMIT 与标签后筛的先后：SQL 先 LIMIT 再在 Python 里筛会把
        # 结果截少，所以有标签筛时 LIMIT 延后到筛完再切
        sql_limit = limit is not None and want_tags is None
        if sql_limit:
            sql += " LIMIT ?"
            params.append(limit)
        mods = [self._mod(r)
                for r in self._conn.execute(sql, params).fetchall()]
        if want_tags is not None:
            wanted = set(want_tags)
            mods = [m for m in mods if m.tags and wanted <= set(m.tags)]
            if limit is not None:
                mods = mods[:limit]
        return mods

    def update_api_metadata(self, mod_id: int, *, title: str | None = None,
                            creator: str | None = None,
                            url: str | None = None,
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
        """Steam API 查询结果回写（检测三路写库之一）。只动远端侧数据；
        确认三件套（confirmed_*）本方法绝不触碰——那是确认门的领地（R17）。
        tags=[] 会写成 "[]"（不是 None），所以"空列表=清空标签"与
        "None=不修改"的语义同时成立。"""
        raw = {"title": title, "creator": creator, "url": url,
               "time_created": time_created, "time_updated": time_updated,
               "last_time_updated": last_time_updated,
               "file_size": file_size, "subscriptions": subscriptions,
               "favorited": favorited, "views": views,
               "tags": None if tags is None else _dumps(tags),
               "preview_url": preview_url,
               "last_checked_at": last_checked_at}
        fields = {k: v for k, v in raw.items() if v is not None}
        if not fields:
            return
        self._require(self._dynamic_update("mods", "mod_id", mod_id, fields),
                      f"mod {mod_id} 不存在")

    def touch_checked(self, mod_ids: Iterable[int], *,
                      checked_at: int | None = None) -> None:
        ids = sorted(set(mod_ids))
        if not ids:
            return
        ts = checked_at if checked_at is not None else _now()
        for chunk in self._in_chunks(ids):
            marks = ",".join("?" * len(chunk))
            self._conn.execute(
                f"UPDATE mods SET last_checked_at = ? "
                f"WHERE mod_id IN ({marks})",
                [ts, *chunk],
            )

    # ---------- 状态迁移 ----------

    def mark_deleted(self, mod_id: int, last_state: dict) -> None:
        """软删除三连（一个事务内）：status→deleted + deleted_at=now +
        末态 dict 序列化进 deleted_last_state。last_state 由 flow 层组装。"""
        rc = self._dynamic_update("mods", "mod_id", mod_id, {
            "status": _DELETED,
            "deleted_at": _now(),
            "deleted_last_state": _dumps(last_state),
        })
        self._require(rc, f"mod {mod_id} 不存在")

    def mark_restored(self, mod_id: int) -> None:
        """软删除恢复：status 按"有没有确认版本"回推——
        有 → downloaded（东西确认过，回来就是已下载）；
        没有 → tracked（版本还说不清，回到待下载）。
        同时清掉软删除标记（它不再是"等恢复"的存放态）。
        只动账面，磁盘处置归清理页。仅 deleted 条目可恢复。"""
        mod = self.get_mod(mod_id)
        if mod is None:
            raise ValueError(f"mod {mod_id} 不存在")
        if mod.status != _DELETED:
            raise ValueError(
                f"mod {mod_id} 不是已删除状态（现为 {mod.status}），无需恢复")
        new_status = _DOWNLOADED if mod.confirmed_version is not None \
            else _TRACKED
        self._conn.execute(
            """UPDATE mods SET status = ?, deleted_at = NULL,
                               deleted_last_state = NULL WHERE mod_id = ?""",
            (new_status, mod_id))

    def mark_failed(self, mod_id: int, reason: str) -> None:
        """★复合：mods.status→failed + 末态快照写入 failed_mods
        （归档表故意无外键，证据独立存活）。原 mods 行保留不删。"""
        with self._atomic():
            mod = self.get_mod(mod_id)
            if mod is None:
                raise ValueError(f"mod {mod_id} 不存在")
            # 末态快照字段随判决制换血：确认版本与来源顶替了
            # 旧版的 acf 本地版本与 manifest
            state = {"title": mod.title, "url": mod.url,
                     "time_updated": mod.time_updated,
                     "confirmed_version": mod.confirmed_version,
                     "confirmed_source": mod.confirmed_source,
                     "local_size": mod.local_size,
                     "note": mod.note, "color_tag": mod.color_tag,
                     "is_special": mod.is_special}
            self._insert("failed_mods", {
                "mod_id": mod_id,
                "game_id": mod.game_id,
                "reason": reason,
                "last_known_state": _dumps(state),
            })
            self._conn.execute(
                "UPDATE mods SET status = 'failed' WHERE mod_id = ?",
                (mod_id,))

    def replace_failed_mod(self, old_mod_id: int, new_mod_id: int) -> None:
        """★复合（关联替换，一个事务五步）：整理成果迁移（新值为空才
        覆盖，不抹掉用户在新记录上的输入）→ 快照改挂 → 归档指向新 id。"""
        with self._atomic():
            old = self.get_mod(old_mod_id)
            new = self.get_mod(new_mod_id)
            if old is None:
                raise ValueError(f"旧 mod {old_mod_id} 不存在")
            if new is None:
                raise ValueError(
                    f"新 mod {new_mod_id} 不存在（请先 add_mod）")
            merged: dict = {}
            if new.note is None and old.note is not None:
                merged["note"] = old.note
            if new.color_tag is None and old.color_tag is not None:
                merged["color_tag"] = old.color_tag
            if not new.is_special and old.is_special:
                merged["is_special"] = 1
            if merged:
                self._dynamic_update("mods", "mod_id", new_mod_id, merged)
            # 快照改挂到新 id（迁移后可能超过保留条数，重新裁剪一次）
            self._conn.execute(
                "UPDATE mod_snapshots SET mod_id = ? WHERE mod_id = ?",
                (new_mod_id, old_mod_id))
            self._conn.execute(
                """DELETE FROM mod_snapshots WHERE mod_id = ? AND id NOT IN
                                                                  (SELECT id FROM mod_snapshots WHERE mod_id = ?
                                                                   ORDER BY snapshot_at DESC, id DESC LIMIT ?)""",
                (new_mod_id, new_mod_id, self._snapshot_keep))
            self._require(self._conn.execute(
                "UPDATE failed_mods SET replaced_by = ? WHERE mod_id = ?",
                (new_mod_id, old_mod_id)).rowcount,
                          f"failed_mods 中没有 {old_mod_id} 的归档（请先 mark_failed）")

    def purge_mod(self, mod_id: int, *, purge_backups: bool = False) -> int:
        """彻底清账。规则沿旧版：备份去留必须先有明确决策（RESTRICT 闸
        的显式版）；快照与提醒显式先删（读代码的人不用背外键图）；
        自动登记黑名单（防扫描复活）；failed_mods 证据行与操作日志
        刻意存活。v2 增量（C 级提案③）：判决史一并清——账清史清，
        历史证据由 failed_mods / purged_mods / operations_log 承载；
        translations 挂外键 CASCADE，随 mods 行自动走。"""
        with self._atomic():
            # 一次查询两个用途：存在性检查 + 末态快照（登记黑名单要用）
            mod = self.get_mod(mod_id)
            if mod is None:
                raise ValueError(f"mod {mod_id} 不存在")
            n_backups = self._conn.execute(
                "SELECT COUNT(*) FROM backups WHERE mod_id = ?",
                (mod_id,)).fetchone()[0]
            if n_backups and not purge_backups:
                raise ValueError(
                    f"mod {mod_id} 名下有 {n_backups} 份备份登记：彻底清账"
                    "须先处置备份（清理页勾选，或到备份总览页逐份处理）")
            if n_backups:
                self._conn.execute(
                    "DELETE FROM backups WHERE mod_id = ?", (mod_id,))
            self._conn.execute(
                "DELETE FROM mod_snapshots WHERE mod_id = ?", (mod_id,))
            self._conn.execute(
                "DELETE FROM special_mod_alerts WHERE mod_id = ?", (mod_id,))
            self._conn.execute(
                "DELETE FROM verdict_log WHERE mod_id = ?", (mod_id,))
            self._conn.execute(
                "DELETE FROM mods WHERE mod_id = ?", (mod_id,))
            # 黑名单登记放最后：mods 行删掉后 mod 对象已在手里，不怕丢
            self._conn.execute(
                """INSERT OR REPLACE INTO purged_mods (mod_id, game_id, title)
                   VALUES (?, ?, ?)""", (mod_id, mod.game_id, mod.title))
        return n_backups

    # ---------- 整理 setter ----------

    def set_note(self, mod_id: int, note: str | None) -> None:
        self._require(self._conn.execute(
            "UPDATE mods SET note = ? WHERE mod_id = ?",
            (note, mod_id)).rowcount, f"mod {mod_id} 不存在")

    def set_color_tag(self, mod_id: int, color_tag: str | None) -> None:
        self._require(self._conn.execute(
            "UPDATE mods SET color_tag = ? WHERE mod_id = ?",
            (color_tag, mod_id)).rowcount, f"mod {mod_id} 不存在")

    def set_special(self, mod_id: int, is_special: bool) -> None:
        self._require(self._conn.execute(
            "UPDATE mods SET is_special = ? WHERE mod_id = ?",
            (int(is_special), mod_id)).rowcount, f"mod {mod_id} 不存在")

    # ============ ★ 确认门（红线 R17：本地版本的唯一写入点） ============
    # 门开三扇，对应三种确认来路：
    #   record_verdicts → pending_confirmations → confirm_items
    #       下载批次收尾的批量确认：程序先落判决行（待确认），人在
    #       入账中心看过清单、点确认，版本号才落进 mods 表；
    #   claim_accept —— 右键"认领"：用户看着盘上的文件直接背书，即时生效；
    #   set_manual_version —— 右键"设定本地版本"：用户人工核对后填数。
    # 三扇门最终写的都是同三个字段：confirmed_version / confirmed_at /
    # confirmed_source。除此之外本文件任何方法不碰这三个字段
    # （撤销门 revoke_confirmation 除外——它清空这三个字段）。

    def record_verdicts(self, items: list[dict]) -> None:
        """批次收尾批量落判决行（待确认），一个事务。
        item 必须带：mod_id / kind / game_id；可选：version_trigger、
        version_query、version_written、title、file_size、source、note。
        分工铁律：
        - "该写哪个版本号"（version_written / source 取值）由 flow 层按
          D4 分支算好传入，本方法只管存——写入值永不高估（R18）的守卫
          在 flow，repo 不做二次推断；
        - 黑名单拦截在 flow 落行之前完成（黑名单三拦之一，D11），
          本方法不重复查；
        - mod 可能还没入账（快速命令下载的东西，D39）——本方法不校验
          mods 存在性，建行的活留给 confirm_items。
        落行后按 verdict_keep 滚动修剪（见 _prune_verdicts）。"""
        if not items:
            return
        now = _now()
        touched: set[int] = set()
        with self._atomic():
            for item in items:
                mod_id = item["mod_id"]
                kind = item["kind"]
                if kind not in _VALID_KINDS:
                    raise ValueError(f"非法判决种类：{kind!r}")
                if item.get("game_id") is None:
                    raise ValueError(
                        f"mod {mod_id} 的判决行缺 game_id"
                        "（没有它入账中心没法按档案过滤，flow 层必须给）")
                self._insert("verdict_log", {
                    "mod_id": mod_id,
                    "kind": kind,
                    "game_id": item["game_id"],
                    "version_trigger": item.get("version_trigger"),
                    "version_query": item.get("version_query"),
                    "version_written": item.get("version_written"),
                    "title": item.get("title"),
                    "file_size": item.get("file_size"),
                    "source": item.get("source"),
                    "occurred_at": now,
                    "note": item.get("note"),
                })
                touched.add(mod_id)
            # 修剪放同一事务：写入和清理要么都成、要么都不算数
            for mod_id in touched:
                self._prune_verdicts(mod_id, now)

    def _prune_verdicts(self, mod_id: int, now: int) -> None:
        """判决史修剪（D23）。确认行 = confirmed_version 的"票据"，
        永不删。未确认行（超时/失败的历史、没确认的旧批次）只留两类，
        两个条件是"或"的关系：最近 verdict_keep 条以内的留；
        90 天以内的留（哪怕排在 N 条之外——近期的还有确认价值）。
        又老又排在 N 条之外的才清。"""
        cutoff = now - _VERDICT_MAX_AGE
        self._conn.execute(
            """DELETE FROM verdict_log
               WHERE mod_id = ? AND confirmed_at IS NULL
                 AND occurred_at < ?
                 AND id NOT IN (
                   SELECT id FROM verdict_log
                   WHERE mod_id = ? AND confirmed_at IS NULL
                   ORDER BY occurred_at DESC, id DESC
                   LIMIT ?)""",
            (mod_id, cutoff, mod_id, self._verdict_keep))

    def pending_confirmations(self, game_id: int | None = None
                              ) -> list[Verdict]:
        """确认队列（入账中心的数据源）：还没确认的 success/claim/manual
        判决行。game_id=None 返回全部档案；传则只看该档案——
        "其他档案还有 N 条待确认"的顶部提示，N 就从带参调用拿（D20）。"""
        marks = ",".join("?" * len(_QUEUE_KINDS))
        sql = (f"SELECT * FROM verdict_log WHERE confirmed_at IS NULL "
               f"AND kind IN ({marks})")
        params: list = list(_QUEUE_KINDS)
        if game_id is not None:
            sql += " AND game_id = ?"
            params.append(game_id)
        sql += " ORDER BY occurred_at DESC, id DESC"
        return [self._verdict(r)
                for r in self._conn.execute(sql, params).fetchall()]

    def confirm_items(self, mod_ids: list[int]) -> int:
        """★确认门主入口：把入账中心勾选的条目批量确认，返回确认条数。
        一个事务，逐条做四件事：
        1. 找该 mod 最新一条未确认的 success/claim/manual 判决行——
           没有 = 没东西可确认（已确认过 / 从没跑过批次）→ 报错；
        2. 黑名单拦截（D11）：名单内的编号绝不自动建行；
        3. mods 有行 → 更新确认三件套，tracked/failed → downloaded
           （deleted 拒绝，请先恢复）；没行 → 用判决行上顺手存的
           game_id/title/file_size 建行（D39：快速命令下载的东西
           账上本来没有）；
        4. 该 mod 全部未确认的 success/claim/manual 行盖"已确认"戳
           （排队里的旧批次一并销账，队列不留过期重复项）。
        版本号与来源直接取判决行上已写好的值——GUI 只做"背书"，没有
        第二次填数字的机会，写入值永不高估（R18）由结构保证。
        幂等口径：同一批重复确认 → 第二次报错（队列里已没有它的行），
        账面状态不变；部分失败整体回滚，绝不留半截确认。"""
        ids = list(dict.fromkeys(int(i) for i in mod_ids))  # 去重保序
        if not ids:
            return 0
        confirmed = 0
        marks = ",".join("?" * len(_QUEUE_KINDS))
        with self._atomic():
            for mod_id in ids:
                row = self._conn.execute(
                    f"""SELECT * FROM verdict_log
                        WHERE mod_id = ? AND confirmed_at IS NULL
                          AND kind IN ({marks})
                        ORDER BY occurred_at DESC, id DESC LIMIT 1""",
                    (mod_id, *_QUEUE_KINDS)).fetchone()
                if row is None:
                    raise ValueError(
                        f"mod {mod_id} 没有待确认的判决行（可能已确认过，"
                        "或从未跑过批次）——刷新入账中心后重试")
                if self.is_purged(mod_id):
                    raise ValueError(
                        f"mod {mod_id} 在已清账黑名单里（D11）：要重新收录"
                        "请先到「已清账管理」页移出名单")
                version = row["version_written"]
                source = row["source"]
                now = _now()
                existing = self._conn.execute(
                    "SELECT status FROM mods WHERE mod_id = ?",
                    (mod_id,)).fetchone()
                if existing is None:
                    # 不在账本：拿判决行上存的身份信息建行（D39）。
                    # 其余字段留 NULL，下轮检测自动补全（旧决策 20 同款）
                    self._insert("mods", {
                        "mod_id": mod_id,
                        "game_id": row["game_id"],
                        "status": _DOWNLOADED,
                        "title": row["title"],
                        "file_size": row["file_size"],
                        "confirmed_version": version,
                        "confirmed_source": source,
                        "confirmed_at": now,
                    })
                else:
                    status = existing["status"]
                    if status == _DELETED:
                        raise ValueError(
                            f"mod {mod_id} 是已删除状态："
                            "请先在 mod 库页恢复再确认")
                    if status in (_TRACKED, "failed"):
                        # tracked→downloaded 的状态迁移发生在这一刻
                        # （D6 唯一入口）；failed 复活同理（重试成功）
                        self._conn.execute(
                            "UPDATE mods SET status = ? WHERE mod_id = ?",
                            (_DOWNLOADED, mod_id))
                    self._conn.execute(
                        """UPDATE mods SET confirmed_version = ?,
                                           confirmed_source = ?, confirmed_at = ?
                           WHERE mod_id = ?""",
                        (version, source, now, mod_id))
                self._conn.execute(
                    f"""UPDATE verdict_log SET confirmed_at = ?
                        WHERE mod_id = ? AND confirmed_at IS NULL
                          AND kind IN ({marks})""",
                    (now, mod_id, *_QUEUE_KINDS))
                confirmed += 1
        return confirmed

    def claim_accept(self, mod_id: int, version: int | None, *,
                     source: str = "claim",
                     local_size: int | None = None) -> None:
        """★认领：用户对"盘上有这个 mod 的文件"直接背书，即时生效
        不进队列（D7）。只服务已在账本的条目（右键认领的天然场景）；
        盘上陌生目录（账上没有的）不走这里——由扫描落成待确认的
        claim 判决行，到入账中心确认（D39 队列）。
        一个事务两件事：
        1. 落 claim 判决行并当场盖"已确认"戳（认领即确认，判决留史）；
        2. mods 更新确认三件套；tracked/failed → downloaded
           （人背书了盘面事实）；deleted 拒绝（先恢复）；downloaded
           保持（用认领值修正版本）。
        local_size 可选回填——纯展示列，大小永远不参与版本判定（R19）。
        version=None 合法：文件在、版本读不出来 → 确认成"版本未知"
        （备份守卫会照旧拒绝给版本未知的东西做备份）。"""
        if source not in _VALID_SOURCES:
            raise ValueError(f"非法确认来源：{source!r}")
        with self._atomic():
            mod = self.get_mod(mod_id)
            if mod is None:
                raise ValueError(
                    f"mod {mod_id} 不在账本里：右键认领只服务已收录条目；"
                    "盘上陌生目录请走扫描 → 入账中心确认")
            if mod.status == _DELETED:
                raise ValueError(
                    f"mod {mod_id} 是已删除状态：请先恢复再认领")
            now = _now()
            self._insert("verdict_log", {
                "mod_id": mod_id,
                "kind": "claim",
                "game_id": mod.game_id,
                "version_written": version,
                "source": source,
                "confirmed_at": now,
                "occurred_at": now,
            })
            new_status = (_DOWNLOADED
                          if mod.status in (_TRACKED, "failed")
                          else mod.status)
            self._conn.execute(
                """UPDATE mods SET confirmed_version = ?,
                                   confirmed_source = ?, confirmed_at = ?, status = ?,
                                   local_size = COALESCE(?, local_size) WHERE mod_id = ?""",
                (version, source, now, new_status, local_size, mod_id))

    def set_manual_version(self, mod_id: int, version: int | None, *,
                           note: str | None = None) -> None:
        """★手动设定本地版本（mod 库页右键「设定本地版本…」，D5③）：
        用户人工核对后输入版本号——最重的人工背书。mod 必须已在账本
        （右键的前提）；deleted 拒绝（先恢复）。
        一个事务两件事：落 manual 判决行当场盖戳 + 写确认三件套；
        tracked/failed → downloaded（同认领的迁移规则）。
        version=None 合法：用户确认"文件在但版本号说不清" = 版本未知。"""
        with self._atomic():
            mod = self.get_mod(mod_id)
            if mod is None:
                raise ValueError(f"mod {mod_id} 不存在")
            if mod.status == _DELETED:
                raise ValueError(
                    f"mod {mod_id} 是已删除状态：请先恢复再设定版本")
            now = _now()
            self._insert("verdict_log", {
                "mod_id": mod_id,
                "kind": "manual",
                "game_id": mod.game_id,
                "version_written": version,
                "source": "manual",
                "confirmed_at": now,
                "occurred_at": now,
                "note": note,
            })
            new_status = (_DOWNLOADED
                          if mod.status in (_TRACKED, "failed")
                          else mod.status)
            self._conn.execute(
                """UPDATE mods SET confirmed_version = ?,
                                   confirmed_source = 'manual', confirmed_at = ?,
                                   status = ? WHERE mod_id = ?""",
                (version, now, new_status, mod_id))

    def revoke_confirmation(self, mod_id: int) -> None:
        """撤销确认（D8）：确认三件套清空，status 不动——状态轴与
        版本轴正交，撤销只退版本轴。判决史不动：撤的是"当前有效值"，
        不是"发生过"。下轮检测会把它当版本未知重新分桶，走确认门重来。"""
        rc = self._conn.execute(
            """UPDATE mods SET confirmed_version = NULL,
                               confirmed_at = NULL, confirmed_source = NULL
               WHERE mod_id = ?""", (mod_id,)).rowcount
        self._require(rc, f"mod {mod_id} 不存在")

    def list_verdicts(self, mod_id: int, limit: int = 10) -> list[Verdict]:
        """某 mod 的最近判决（详情面板"判决史"小节），新→旧。
        含已确认与未确认——超时/失败的历史在这里一眼可见（D22）。"""
        rows = self._conn.execute(
            "SELECT * FROM verdict_log WHERE mod_id = ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (mod_id, limit)).fetchall()
        return [self._verdict(r) for r in rows]

    def backfill_local_sizes(self, sizes: dict[int, int]) -> int:
        """盘点回填本地占用（D23）：磁盘/acf 盘点拿到各 mod 的目录
        大小后批量写入展示列。只动 local_size，绝不碰版本字段（R19）；
        账上没有的编号直接忽略（回填不建行）。返回实际更新条数。"""
        if not sizes:
            return 0
        updated = 0
        with self._atomic():
            for mod_id, size in sizes.items():
                rc = self._conn.execute(
                    "UPDATE mods SET local_size = ? WHERE mod_id = ?",
                    (int(size), int(mod_id))).rowcount
                updated += rc
        return updated

    # ---------- mod_snapshots ----------

    def add_snapshot(self, mod_id: int, *, time_updated: int | None = None,
                     snapshot_at: int | None = None) -> None:
        """★复合：插一条远端观测 + 滚动淘汰该 mod 超过 snapshot_keep 的
        更旧观测——业务规则在 repo 层，调用方不用关心保留条数。
        （v2 快照只记远端侧：本地版本进了判决制，不再进快照。）"""
        with self._atomic():
            self._insert("mod_snapshots", {
                "mod_id": mod_id,
                "time_updated": time_updated,
                "snapshot_at": snapshot_at if snapshot_at is not None
                else _now(),
            })
            # 滚动淘汰：只留最新的 snapshot_keep 条（与 replace_failed_mod
            # 里的裁剪是同一句 SQL——单一写法不漂移）
            self._conn.execute(
                """DELETE FROM mod_snapshots WHERE mod_id = ? AND id NOT IN
                                                                  (SELECT id FROM mod_snapshots WHERE mod_id = ?
                                                                   ORDER BY snapshot_at DESC, id DESC LIMIT ?)""",
                (mod_id, mod_id, self._snapshot_keep))

    def list_snapshots(self, mod_id: int) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM mod_snapshots WHERE mod_id = ? "
            "ORDER BY snapshot_at DESC, id DESC",
            (mod_id,)).fetchall()
        return [self._snapshot(r) for r in rows]

    # ---------- translations（简介翻译缓存，D35） ----------

    def get_translation(self, mod_id: int,
                        target_lang: str) -> Translation | None:
        """读缓存。命中与否由调用方对比 source_hash——原文 sha1 一致
        才算真命中，失配 = 作者改了简介，调用方重译后 REPLACE 覆盖。"""
        row = self._conn.execute(
            "SELECT * FROM translations WHERE mod_id = ? AND target_lang = ?",
            (mod_id, target_lang)).fetchone()
        return self._translation(row) if row else None

    def save_translation(self, mod_id: int, *, source_hash: str,
                         target_lang: str, engine: str | None,
                         text_translated: str) -> None:
        """单 mod 单条当前译文：OR REPLACE 覆盖旧的（无历史，D35）。
        mod 不在账本 → 外键 IntegrityError 上抛（简介只服务在账 mod）。"""
        self._conn.execute(
            """INSERT OR REPLACE INTO translations
               (mod_id, source_hash, target_lang, engine, text_translated)
               VALUES (?, ?, ?, ?, ?)""",
            (mod_id, source_hash, target_lang, engine, text_translated))

    # ---------- backups ----------

    def add_backup(self, mod_id: int, backup_path: str, size_bytes: int,
                   version_timeupdated: int, *, manifest: str | None = None,
                   note: str | None = None) -> Backup:
        self._require_mod(mod_id)
        bid = self._insert("backups", {
            "mod_id": mod_id,
            "backup_path": backup_path,
            "size_bytes": size_bytes,
            "version_timeupdated": version_timeupdated,
            "manifest": manifest,
            "note": note,
        })
        return self.get_backup(bid)  # 读回来拿 DB 填好的 created_at

    def get_backup(self, backup_id: int) -> Backup | None:
        row = self._conn.execute(
            "SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
        return self._backup(row) if row else None

    def list_backups(self, mod_id: int | None = None, *,
                     include_pinned: bool = True,
                     oldest_first: bool = False) -> list[Backup]:
        where, params = [], []
        if mod_id is not None:
            where.append("mod_id = ?")
            params.append(mod_id)
        if not include_pinned:
            where.append("pinned = 0")
        sql = "SELECT * FROM backups"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at " + ("ASC" if oldest_first else "DESC")
        return [self._backup(r)
                for r in self._conn.execute(sql, params).fetchall()]

    def set_pinned(self, backup_id: int, pinned: bool) -> None:
        self._require(self._conn.execute(
            "UPDATE backups SET pinned = ? WHERE id = ?",
            (int(pinned), backup_id)).rowcount,
                      f"备份记录 {backup_id} 不存在")

    def delete_backup_record(self, backup_id: int) -> None:
        """只删数据库记录。磁盘目录归 backupManager——两层职责严格
        分开，本方法绝不碰文件系统。"""
        self._require(self._conn.execute(
            "DELETE FROM backups WHERE id = ?", (backup_id,)).rowcount,
                      f"备份记录 {backup_id} 不存在")

    def sum_backup_bytes(self) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM backups"
        ).fetchone()[0]

    def backup_to(self, dest_dir: str | Path, *,
                  keep: int | None = None) -> Path:
        """把整个数据库在线备份到 dest_dir，返回备份文件路径（R16）。
        用 SQLite 在线备份 API（conn.backup）复制数据页：无需关库，
        拿到的始终是完整一致的整库。落盘后滚动清理：目录里"本工具
        命名模式"的备份只留最新 keep 份，命名模式之外的文件一律不碰。
        事务中调用 → RuntimeError：备份到未提交状态没有意义。
        本方法是 SQLite 实现的特有能力，刻意不进 ModRepository 契约。"""
        if self._in_transaction:
            raise RuntimeError("数据库备份不允许在事务中进行（先提交或回滚）")
        keep_total = self._db_backup_keep if keep is None else max(1, int(keep))
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        stem = self._path.stem
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target_path, n = dest / f"{stem}_{stamp}.db", 2
        while target_path.exists():  # 同一秒内重复备份追加序号防覆盖
            target_path = dest / f"{stem}_{stamp}_{n}.db"
            n += 1
        target = sqlite3.connect(target_path)
        try:
            self._conn.backup(target)
        except BaseException:
            # 失败不留半成品（Windows 下删打开中的文件会失败，先关再删）
            target.close()
            target_path.unlink(missing_ok=True)
            raise
        target.close()
        self._prune_db_backups(dest, keep_total)
        return target_path

    def _prune_db_backups(self, dest: Path, keep: int) -> list[Path]:
        """数据库备份的滚动清理，返回被清掉的文件。只认本工具的命名
        模式；时间戳命名的字典序 = 时间序，从最旧删起。"""
        pattern = re.compile(
            rf"^{re.escape(self._path.stem)}_\d{{8}}_\d{{6}}(?:_\d+)?\.db$")
        mine = sorted(p for p in dest.iterdir()
                      if p.is_file() and pattern.match(p.name))
        victims = mine[:-keep] if len(mine) > keep else []
        for victim in victims:
            victim.unlink()
        return victims

    # ---------- operations_log ----------

    def add_operation(self, command: str, *,
                      backup_id: int | None = None) -> int:
        return self._insert("operations_log", {"command": command,
                                               "backup_id": backup_id})

    def finish_operation(self, op_id: int, *, error_count: int = 0,
                         result: str | None = None) -> None:
        self._require(self._dynamic_update(
            "operations_log", "id", op_id,
            {"error_count": error_count, "result": result}),
            f"操作记录 {op_id} 不存在")

    def list_operations(self, limit: int = 50) -> list[OperationLog]:
        rows = self._conn.execute(
            "SELECT * FROM operations_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._oplog(r) for r in rows]

    # ---------- failed_mods ----------

    def list_failed(self, game_id: int) -> list[FailedMod]:
        rows = self._conn.execute(
            "SELECT * FROM failed_mods WHERE game_id = ? "
            "ORDER BY detected_at DESC, id DESC",
            (game_id,)).fetchall()
        return [self._failed(r) for r in rows]

    # ---------- special_mod_alerts ----------

    def add_alert(self, mod_id: int, remote_time_updated: int, *,
                  diff_seconds: int | None = None,
                  was_downloaded: bool = False,
                  note: str | None = None) -> Alert:
        self._require_mod(mod_id)
        aid = self._insert("special_mod_alerts", {
            "mod_id": mod_id,
            "remote_time_updated": remote_time_updated,
            "diff_seconds": diff_seconds,
            "was_downloaded": int(was_downloaded),
            "note": note,
        })
        row = self._conn.execute(
            "SELECT * FROM special_mod_alerts WHERE id = ?",
            (aid,)).fetchone()
        return self._alert(row)

    def get_last_alert(self, mod_id: int) -> Alert | None:
        """某 mod 最近一次提醒——"同一远端版本只提醒一次"的比对依据。"""
        row = self._conn.execute(
            "SELECT * FROM special_mod_alerts WHERE mod_id = ? "
            "ORDER BY id DESC LIMIT 1", (mod_id,)).fetchone()
        return self._alert(row) if row else None

    def list_alerts(self, mod_id: int) -> list[Alert]:
        rows = self._conn.execute(
            "SELECT * FROM special_mod_alerts WHERE mod_id = ? "
            "ORDER BY alert_at DESC", (mod_id,)).fetchall()
        return [self._alert(r) for r in rows]

    # ---------- purged_mods（已清账黑名单） ----------

    def list_purged(self) -> list[PurgedMod]:
        rows = self._conn.execute(
            "SELECT * FROM purged_mods ORDER BY purged_at DESC").fetchall()
        return [PurgedMod(mod_id=r["mod_id"], game_id=r["game_id"],
                          title=r["title"], note=r["note"],
                          purged_at=r["purged_at"]) for r in rows]

    def filter_purged(self, mod_ids: Iterable[int]) -> set[int]:
        """批量查：传入的编号里哪些在黑名单里（扫描入库前的拦截检查）。
        与 filter_existing_ids 同款分片写法，编号再多也不撞变量上限。"""
        ids = sorted(set(mod_ids))
        found: set[int] = set()
        for chunk in self._in_chunks(ids):
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT mod_id FROM purged_mods WHERE mod_id IN ({marks})",
                chunk).fetchall()
            found.update(r[0] for r in rows)
        return found

    def is_purged(self, mod_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM purged_mods WHERE mod_id = ?",
            (mod_id,)).fetchone() is not None

    def remove_purged(self, mod_id: int) -> None:
        """允许录入：移出黑名单。不在名单 → ValueError（白点了要说清）。"""
        rc = self._conn.execute(
            "DELETE FROM purged_mods WHERE mod_id = ?",
            (mod_id,)).rowcount
        if rc == 0:
            raise ValueError(f"mod {mod_id} 不在已清账名单中")

    def add_purged(self, mod_id: int, game_id: int, *,
                   title: str | None = None,
                   note: str | None = None) -> None:
        """手动加入黑名单（档案被整体删除不走 purge_mod、不会自动
        登记，或想提前防住某个编号）。重复添加 = 覆盖刷新。"""
        self._conn.execute(
            """INSERT OR REPLACE INTO purged_mods
               (mod_id, game_id, title, note) VALUES (?, ?, ?, ?)""",
            (mod_id, game_id, title, note))

    # ---------- mod_dependencies（依赖边表） ----------

    def replace_dependencies(self, mod_id: int, required_ids: list[int],
                             fetched_at: int | None = None) -> None:
        """清旧边 + 写新边，一个事务——依赖清单以最近一次拉取为准。
        无外键版本：存在性由本方法显式校验。required_ids 去重保序。"""
        if self.get_mod(mod_id) is None:
            raise ValueError(f"mod {mod_id} 不在账本中，无法记录依赖")
        ts = fetched_at if fetched_at is not None else _now()
        ids = list(dict.fromkeys(int(i) for i in required_ids))
        with self._atomic():
            self._conn.execute(
                "DELETE FROM mod_dependencies WHERE mod_id = ?", (mod_id,))
            self._conn.executemany(
                "INSERT INTO mod_dependencies "
                "(mod_id, required_mod_id, fetched_at) VALUES (?, ?, ?)",
                [(mod_id, rid, ts) for rid in ids])

    def list_dependencies(self, mod_id: int) -> list[int]:
        """它依赖谁（工坊编号升序）。从未拉取 = 空列表。"""
        rows = self._conn.execute(
            "SELECT required_mod_id FROM mod_dependencies "
            "WHERE mod_id = ? ORDER BY required_mod_id",
            (mod_id,)).fetchall()
        return [r[0] for r in rows]

    def list_dependents(self, mod_id: int) -> list[int]:
        """谁依赖它（反向查：处置前看谁被连坐）。"""
        rows = self._conn.execute(
            "SELECT mod_id FROM mod_dependencies "
            "WHERE required_mod_id = ? ORDER BY mod_id",
            (mod_id,)).fetchall()
        return [r[0] for r in rows]

    def latest_dependency_fetch(self) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(fetched_at) FROM mod_dependencies").fetchone()
        return row[0] if row is not None and row[0] is not None else None

    # ---------- 账本导入导出 ----------

    def export_all(self) -> dict:
        """整库导出：{"user_version": 当前 schema 版本,
        "tables": {表名: [行 dict, ...]}}，表序见 LEDGER_TABLES。
        行 = dataclass 自然类型（复用 _row 系列转换 + asdict）——存储格式
        绝不越过本方法的返回值。行内 id 原样保留，导入时据此复原跨表引用。"""
        converters = {
            "games": self._game,
            "mods": self._mod,
            "mod_snapshots": self._snapshot,
            "verdict_log": self._verdict,
            "backups": self._backup,
            "operations_log": self._oplog,
            "failed_mods": self._failed,
            "special_mod_alerts": self._alert,
        }
        tables: dict[str, list[dict]] = {}
        for table in LEDGER_TABLES:
            rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = [asdict(converters[table](r)) for r in rows]
        return {
            "user_version": self._conn.execute(
                "PRAGMA user_version").fetchone()[0],
            "tables": tables,
        }

    def import_all(self, exported: dict) -> None:
        """清库重灌（export_all 的逆操作）。守门三道，全部显式爆炸：
        缺键 / 不认识的表 / 版本号更新。版本闸里特别说明旧世系：
        旧版工具导出的 JSON 版本号（v3）比新世系（v1）大，同样会被拦——
        旧账本必须走「换机迁移」的旧账迁移，不走 JSON 导入这条路。"""
        user_version = exported.get("user_version")
        tables = exported.get("tables")
        if not isinstance(user_version, int) or isinstance(user_version, bool):
            raise ValueError("载荷缺少 user_version（schema 版本）")
        if not isinstance(tables, dict):
            raise ValueError("载荷缺少 tables")
        unknown = set(tables) - set(LEDGER_TABLES)
        if unknown:
            raise ValueError(
                f"载荷里出现了不认识的表：{sorted(unknown)}")
        mine = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version > mine:
            raise ValueError(
                f"账本结构版本（v{user_version}）比本工具（v{mine}）新或属"
                "旧世系：旧版工具的账本请走「换机迁移」的旧账迁移；"
                "更新版本的账本请先升级本工具")
        with self._atomic():
            # 清空顺序 = 灌入顺序的倒序：RESTRICT 外键要求先删子表
            for table in reversed(LEDGER_TABLES):
                self._conn.execute(f"DELETE FROM {table}")
            for table in LEDGER_TABLES:
                for row in tables.get(table, []):
                    self._insert(table, self._to_storage(table, row))
