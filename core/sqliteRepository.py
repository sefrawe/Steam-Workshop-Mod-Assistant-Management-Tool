"""ModRepository 契约的 SQLite 实现
"""
"""
三个贯穿全文的实现约定：
1. isolation_level=None（真自动提交）：每条语句立即落盘；需要多语句
   原子性的复合方法用显式 BEGIN/COMMIT（_atomic）。好处：单语句方法
   不用管事务，复合方法管自己的，transaction() 管调用方的，三层互不干扰
2. 边界转换只在读写发生：库里的 0/1 和 JSON 文本，出 repo 之前必须
   变成 bool 和 list/dict；调用方永远见不到存储格式
3. 让错误显式爆炸：UPDATE 影响 0 行 = 目标不存在 = ValueError，
   约束冲突 = sqlite3.IntegrityError，本层绝不静默吞掉
"""
import re

import json
import sqlite3
import time
from dataclasses import asdict

from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from core.models import (
    Alert, Backup, FailedMod, Game, Mod, OperationLog, Snapshot,
)

from core.modRepository import (ALLOWED_ORDERS, BackupOverviewRow,
                                GameDeletionSummary, LEDGER_TABLES,
                                ModRepository)


_VALID_STATUSES = frozenset({"tracked", "downloaded", "deleted", "failed"})
# 每 mod 保留的快照条数：这里只是默认值，实际值由主窗口从设置读出后
# 通过构造参数注入（设置页可改，默认 5 条）
_DEFAULT_SNAPSHOT_KEEP = 5
# 数据库自身备份的滚动保留份数（R16）：backup_to() 每次落盘后清旧，只留最新 N 份。
# 与 snapshot_keep 同理只是默认值，可由构造参数注入覆盖
_DEFAULT_DB_BACKUP_KEEP = 3

# IN (...) 分片大小；SQL 变量占位符有上限，分片永不出错
_CHUNK = 500


def _now() -> int:
    return int(time.time())


def _dumps(obj) -> str:
    """ensure_ascii=False：中文备注原样存储，可读性好且省空间"""
    return json.dumps(obj, ensure_ascii=False)


class SQLiteRepository(ModRepository):

    # ---------- 基础设施 ----------

    def __init__(self, db_path: str | Path, *,
                 snapshot_keep: int = _DEFAULT_SNAPSHOT_KEEP,
                 db_backup_keep: int = _DEFAULT_DB_BACKUP_KEEP) -> None:
        # snapshot_keep 至少为 1：0 或负数会让快照功能整个失效，直接拦在门口
        self._snapshot_keep = max(1, int(snapshot_keep))
        # 数据库备份同理：留 0 份的"滚动备份"等于没备份
        self._db_backup_keep = max(1, int(db_backup_keep))

        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None,check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 让查询结果支持按列名取值
        # foreign_keys 是连接级属性，每个连接都必须重新打开
        # （schema 文件里的那条只管建表期）
        self._conn.execute("PRAGMA foreign_keys = ON")
        # 遇到锁（比如 PyCharm 数据源正开着）先等 5 秒再报错，而不是立刻炸
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._in_transaction = False
        # user_version==0 = 空库 → 执行 schema.sql 建表（脚本自己会把
        # user_version 置 1）；已建过的库跳过——重复打开同一个库完全幂等
        if self._conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            schema = (Path(__file__).parent / "schema.sql").read_text(
                encoding="utf-8")
            self._conn.executescript(schema)

    @contextmanager
    def transaction(self):
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
        """复合方法内部用。关键设计：如果已处于外层 transaction() 中，
        直接并入外层（不 BEGIN）——这样复合方法放进 transaction() 块里
        也保持整体原子性，测试 test_composite_* 验证了这一点"""
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
        """动态 INSERT，返回 lastrowid。
        值为 None 的字段直接不写进列清单——让 schema 的 DEFAULT 生效。
        显式写 NULL 会违反 first_tracked_at 这类 NOT NULL DEFAULT 列的约束"""
        cols = {k: v for k, v in fields.items() if v is not None}
        names = ", ".join(cols)
        marks = ", ".join("?" * len(cols))
        # 拼出来的只有问号，无注入风险
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
        """自然类型 dict → 存储格式 dict（import_all 专用），与
        _row 系列互为镜像。带特殊类型的列全项目就两类（JSON 文本、
        0/1 布尔），显式列出比查表一目了然。表里没有的列名不在
        这里拦——_insert 拼出的 SQL 会被 SQLite 以 no such column
        拒绝 → 事务回滚，错误照旧显式爆炸。"""
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
                    backup_dir=row["backup_dir"], created_at=row["created_at"])

    @staticmethod
    def _mod(row) -> Mod:
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
            local_timeupdated=row["local_timeupdated"],
            manifest=row["manifest"],
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
            local_path=row["local_path"],
            deleted_at=row["deleted_at"],
            deleted_last_state=(json.loads(row["deleted_last_state"])
                                if row["deleted_last_state"] is not None
                                else None),
            first_tracked_at=row["first_tracked_at"],
        )

    @staticmethod
    def _snapshot(row) -> Snapshot:
        return Snapshot(id=row["id"], mod_id=row["mod_id"],
                        snapshot_at=row["snapshot_at"],
                        time_updated=row["time_updated"],
                        manifest=row["manifest"],
                        local_timeupdated=row["local_timeupdated"])

    @staticmethod
    def _backup(row) -> Backup:
        return Backup(id=row["id"], mod_id=row["mod_id"],
                      backup_path=row["backup_path"],
                      size_bytes=row["size_bytes"],
                      version_timeupdated=row["version_timeupdated"],
                      manifest=row["manifest"], note=row["note"],
                      pinned=bool(row["pinned"]), created_at=row["created_at"])

    @staticmethod
    def _oplog(row) -> OperationLog:
        return OperationLog(id=row["id"], command=row["command"],
                            executed_at=row["executed_at"],
                            error_count=row["error_count"],
                            result=row["result"], backup_id=row["backup_id"])

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
        # 档案下还有 mod 时，外键 RESTRICT 会让 SQLite 直接抛
        # IntegrityError——保护逻辑交给数据库本身，这里一行都不用多写
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
            "SELECT COUNT(*) FROM mods WHERE game_id = ? AND status = 'deleted'",
            (app_id,)).fetchone()[0]
        failed = self._conn.execute(
            "SELECT COUNT(*) FROM failed_mods WHERE game_id = ?",
            (app_id,)).fetchone()[0]
        # 备份登记挂在 mod 名下，要经 mods 才能找到所属档案
        bk = self._conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
               FROM backups WHERE mod_id IN
                                  (SELECT mod_id FROM mods WHERE game_id = ?)""",
            (app_id,)).fetchone()
        paths = [r[0] for r in self._conn.execute(
            """SELECT backup_path FROM backups WHERE mod_id IN
                                                     (SELECT mod_id FROM mods WHERE game_id = ?)
               ORDER BY id""", (app_id,))]
        return GameDeletionSummary(
            app_id=app_id, mod_total=mod_total, mod_deleted=mod_deleted,
            failed_count=failed, backup_count=bk[0], backup_bytes=bk[1],
            backup_paths=paths)

    def delete_game_deep(self, app_id: int) -> GameDeletionSummary:
        # 盘点放事务外（纯读）；它顺带完成存在性检查——不存在的档案
        # 在动手之前就报 ValueError，不进事务
        summary = self.game_deletion_summary(app_id)
        with self._atomic():
            # 子查询形式（IN (SELECT …)）：mod 列表为空也不会出 SQL
            # 语法问题，也不用分片，一条语句数据库自己解决
            self._conn.execute(
                """DELETE FROM special_mod_alerts WHERE mod_id IN
                                                        (SELECT mod_id FROM mods WHERE game_id = ?)""",
                (app_id,))
            self._conn.execute(
                """DELETE FROM mod_snapshots WHERE mod_id IN
                                                   (SELECT mod_id FROM mods WHERE game_id = ?)""",
                (app_id,))
            # 备份登记是 mods 的 RESTRICT 外键——必须先删干净，否则
            # 下一步删 mod 会被数据库拦下（这正是闸的工作方式）
            self._conn.execute(
                """DELETE FROM backups WHERE mod_id IN
                                             (SELECT mod_id FROM mods WHERE game_id = ?)""",
                (app_id,))
            self._conn.execute(
                "DELETE FROM mods WHERE game_id = ?", (app_id,))
            # 失效归档随档案删：mod_id 本来就没外键（证据表），归档
            # 不会自己消失，必须显式来删
            self._conn.execute(
                "DELETE FROM failed_mods WHERE game_id = ?", (app_id,))
            self._conn.execute(
                "DELETE FROM games WHERE app_id = ?", (app_id,))
            # operations_log 刻意不删（见契约注释）。其中 backup_id
            # 指向刚才删掉的备份，外键 ON DELETE SET NULL 自动置空
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
            "local_timeupdated": mod.local_timeupdated,
            "manifest": mod.manifest,
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
            "local_path": mod.local_path,
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
                chunk,
            ).fetchall()
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
                  size_min: int | None = None,
                  size_max: int | None = None,
                  updated_from: int | None = None,
                  updated_to: int | None = None,
                  tags_all: Iterable[str] | None = None) -> list[Mod]:
        """（T12 扩展）参数语义见契约层 docstring。实现分工：
        SQL 能表达的条件全部下推给 SQLite（值只走 ? 参数绑定，无注入）；
        标签是唯一例外——tags 存 JSON 文本，schema.sql 文件头约定 3
        "数据库不做 JSON 结构化查询"，取回后在 Python 里精确比对。"""
        if order_by not in ALLOWED_ORDERS:
            raise ValueError(
                f"order_by 必须取自 ALLOWED_ORDERS，收到：{order_by!r}")

        where = ["game_id = ?"]
        params: list = [game_id]

        # ---- 既有简单筛选（原样保留）----
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if special_only:
            where.append("is_special = 1")
        if color_tag is not None:
            where.append("color_tag = ?")
            params.append(color_tag)
        if search:
            # LIKE 的 % _ 是通配符，先转义用户的输入，再声明 ESCAPE 字符
            safe = (search.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            where.append("(title LIKE ? ESCAPE '\\' "
                         "OR note LIKE ? ESCAPE '\\')")
            params += [f"%{safe}%", f"%{safe}%"]

        # ---- T12 高级筛选：SQL 可表达的全部下推 ----
        # 标题/备注分开指定，同一套转义：用户输入里的 % _ \ 都当字面量
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
            # 大小口径与列表页"大小"列完全一致（模型显示 local_size or
            # file_size）：本地优先、acf 缺失或为 0 退 API。NULLIF 把 0
            # 变 NULL 交给 COALESCE 退到 file_size——SQL 一行复刻 or 语义
            where.append("COALESCE(NULLIF(local_size, 0), file_size) >= ?")
            params.append(size_min)
        if size_max is not None:
            where.append("COALESCE(NULLIF(local_size, 0), file_size) <= ?")
            params.append(size_max)
        if updated_from is not None:
            # time_updated 为 NULL（从没查过远端）→ 比较结果 NULL → 不命中。
            # "更新时间在某范围"对"不知道更新时间"的条目没有答案，
            # 排除是正确语义，不是漏网
            where.append("time_updated >= ?")
            params.append(updated_from)
        if updated_to is not None:
            where.append("time_updated <= ?")
            params.append(updated_to)

        # ---- 标签：唯一不下推的条件（原因见 docstring）----
        # 去重、去空、排序——顺序稳定，行为可预期
        want_tags = sorted({t for t in (tags_all or []) if t}) or None

        sql = (f"SELECT * FROM mods WHERE {' AND '.join(where)} "
               f"ORDER BY {order_by}")
        # LIMIT 与标签后筛的先后：SQL 先 LIMIT 再在 Python 里筛会把
        # 结果截少，所以有标签筛时 LIMIT 延后到筛完再切；没有标签筛
        # 时维持 SQL LIMIT（让数据库先截，省内存）
        sql_limit = limit is not None and want_tags is None
        if sql_limit:
            sql += " LIMIT ?"
            params.append(limit)

        mods = [self._mod(r)
                for r in self._conn.execute(sql, params).fetchall()]

        if want_tags is not None:
            wanted = set(want_tags)
            # _mod() 已把 JSON 文本转回 list——这里只做集合比较，
            # 不碰存储格式（文件头约定 2：边界转换只在读写发生）
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
        # 注意：tags=[] 会被 _dumps 成 "[]"，不是 None，所以能正常写入——
        # "空列表 = 清空标签" 与 "None = 不修改" 的语义就这样同时成立
        self._require(self._dynamic_update("mods", "mod_id", mod_id, fields),
                      f"mod {mod_id} 不存在")

    def touch_checked(self, mod_ids: Iterable[int],
                      *, checked_at: int | None = None) -> None:
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

    def update_local_state(self, mod_id: int, *, local_timeupdated: int,
                           manifest: str | None = None,
                           local_size: int | None = None,
                           status: str | None = None) -> None:
        if local_timeupdated is None:
            raise ValueError(
                "local_timeupdated 是本方法的必填项（本地版本必须有值）")
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError(f"非法 status：{status!r}")
        fields = {"local_timeupdated": local_timeupdated}
        if manifest is not None:
            fields["manifest"] = manifest
        if local_size is not None:
            fields["local_size"] = local_size
        if status is not None:
            fields["status"] = status
        self._require(self._dynamic_update("mods", "mod_id", mod_id, fields),
                      f"mod {mod_id} 不存在")

    def update_status(self, mod_id: int, status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"非法 status：{status!r}")
        self._require(self._conn.execute(
            "UPDATE mods SET status = ? WHERE mod_id = ?",
            (status, mod_id)).rowcount, f"mod {mod_id} 不存在")

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

    def mark_deleted(self, mod_id: int, last_state: dict) -> None:
        rc = self._dynamic_update("mods", "mod_id", mod_id, {
            "status": "deleted",
            "deleted_at": _now(),
            "deleted_last_state": _dumps(last_state),
        })
        self._require(rc, f"mod {mod_id} 不存在")

    def purge_mod(self, mod_id: int, *, purge_backups: bool = False) -> int:
        # 实现口径见契约 docstring。一个事务：要么全清要么原样
        with self._atomic():
            self._require_mod(mod_id)
            n_backups = self._conn.execute(
                "SELECT COUNT(*) FROM backups WHERE mod_id = ?",
                (mod_id,)).fetchone()[0]
            if n_backups and not purge_backups:
                # 闸：备份去留必须先有明确决策，不许顺手带走
                raise ValueError(
                    f"mod {mod_id} 名下有 {n_backups} 份备份登记："
                    "彻底清账须先处置备份（在清理页勾选备份选项，"
                    "或到备份总览页逐份处理）")
            if n_backups:
                self._conn.execute(
                    "DELETE FROM backups WHERE mod_id = ?", (mod_id,))
            # 快照与提醒本有 CASCADE 兜底，仍显式先删——同 delete_game_deep
            self._conn.execute(
                "DELETE FROM mod_snapshots WHERE mod_id = ?", (mod_id,))
            self._conn.execute(
                "DELETE FROM special_mod_alerts WHERE mod_id = ?", (mod_id,))
            self._conn.execute("DELETE FROM mods WHERE mod_id = ?", (mod_id,))
            # failed_mods 证据行刻意不动（无外键证据表）；operations_log 不动
            return n_backups


    # ---------- mod_snapshots ----------

    def add_snapshot(self, mod_id: int, *, time_updated: int | None = None,
                     manifest: str | None = None,
                     local_timeupdated: int | None = None,
                     snapshot_at: int | None = None) -> None:
        with self._atomic():
            self._require_mod(mod_id)
            self._insert("mod_snapshots", {
                "mod_id": mod_id,
                "time_updated": time_updated,
                "manifest": manifest,
                "local_timeupdated": local_timeupdated,
                "snapshot_at": snapshot_at,
            })
            # 滚动删除：只保留 snapshot_at 最新的 N 条
            # （N 来自构造参数，主窗口从设置注入）
            self._conn.execute(
                """DELETE FROM mod_snapshots
                   WHERE mod_id = ?
                     AND id NOT IN (
                       SELECT id FROM mod_snapshots
                       WHERE mod_id = ?
                       ORDER BY snapshot_at DESC, id DESC
                       LIMIT ?)""",
                (mod_id, mod_id, self._snapshot_keep),
            )

    def list_snapshots(self, mod_id: int) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM mod_snapshots WHERE mod_id = ? "
            "ORDER BY snapshot_at DESC, id DESC",
            (mod_id,)
        ).fetchall()
        return [self._snapshot(r) for r in rows]

    # ---------- backups ----------

    def add_backup(self, mod_id: int, backup_path: str, size_bytes: int,
                   version_timeupdated: int, *,
                   manifest: str | None = None,
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
            (int(pinned), backup_id)).rowcount, f"备份记录 {backup_id} 不存在")

    def delete_backup_record(self, backup_id: int) -> None:
        self._require(self._conn.execute(
            "DELETE FROM backups WHERE id = ?",
            (backup_id,)).rowcount, f"备份记录 {backup_id} 不存在")



    def sum_backup_bytes(self) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM backups"
        ).fetchone()[0]

    def backup_to(self, dest_dir: str | Path, *, keep: int | None = None) -> Path:
        """把整个数据库在线备份到 dest_dir，返回备份文件路径（R16）。

        - 用 SQLite 在线备份 API（conn.backup）复制数据页：无需关库，
          拿到的始终是完整一致的整库，不是"拷文件碰运气"
        - 备份文件名 <库名>_<时间戳>.db（如 mods_20260921_181216.db），
          同一秒内重复备份追加 _2、_3 防覆盖
        - 落盘后立即滚动清理：目录里"本工具命名模式"的备份只留最新
          keep 份（默认 = 构造参数 db_backup_keep），从最旧删起；
          命名模式之外的文件一律不碰——用户放在同目录的东西
          没有资格被我们删
        - user_version 随数据页一起复制：备份文件直接用
          SQLiteRepository 打开就是完整可用的库

        事务中调用 → RuntimeError：备份到未提交状态没有意义，
        与 transaction() 不支持嵌套同一哲学——显式爆炸，绝不静默。
        本方法是 SQLite 实现的特有能力，刻意不进 ModRepository 契约：
        换别的存储后端就没有"在线备份"这个概念，进契约反而逼所有
        实现假装自己会。
        """
        if self._in_transaction:
            raise RuntimeError("数据库备份不允许在事务中进行（先提交或回滚）")
        keep_total = self._db_backup_keep if keep is None else max(1, int(keep))
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        stem = self._path.stem  # 如 mods.db → "mods"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target_path, n = dest / f"{stem}_{stamp}.db", 2
        while target_path.exists():
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
        """数据库备份的滚动清理，返回被清掉的文件。
        只认本工具的命名模式 <库名>_<8位日期>_<6位时间>[_序号].db；
        时间戳命名的字典序 = 时间序，从最旧删起。"""
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
        return self._insert("operations_log",
                            {"command": command, "backup_id": backup_id})

    def finish_operation(self, op_id: int, *, error_count: int = 0,
                         result: str | None = None) -> None:
        self._require(self._dynamic_update("operations_log", "id", op_id,
                                           {"error_count": error_count,
                                            "result": result}),
                      f"操作记录 {op_id} 不存在")

    def list_operations(self, limit: int = 50) -> list[OperationLog]:
        rows = self._conn.execute(
            "SELECT * FROM operations_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._oplog(r) for r in rows]

    # ---------- failed_mods（复合方法） ----------

    def mark_failed(self, mod_id: int, reason: str) -> None:
        with self._atomic():
            mod = self.get_mod(mod_id)
            if mod is None:
                raise ValueError(f"mod {mod_id} 不存在")
            state = {"title": mod.title, "url": mod.url,
                     "time_updated": mod.time_updated,
                     "local_timeupdated": mod.local_timeupdated,
                     "manifest": mod.manifest,
                     "local_size": mod.local_size,
                     "note": mod.note, "color_tag": mod.color_tag,
                     "is_special": mod.is_special,
                     "local_path": mod.local_path}
            self._insert("failed_mods", {
                "mod_id": mod_id,
                "game_id": mod.game_id,
                "reason": reason,
                "last_known_state": _dumps(state),
            })
            self._conn.execute(
                "UPDATE mods SET status = 'failed' WHERE mod_id = ?",
                (mod_id,))

    def list_failed(self, game_id: int) -> list[FailedMod]:
        rows = self._conn.execute(
            "SELECT * FROM failed_mods WHERE game_id = ? "
            "ORDER BY detected_at DESC, id DESC",
            (game_id,),
        ).fetchall()
        return [self._failed(r) for r in rows]

    def replace_failed_mod(self, old_mod_id: int, new_mod_id: int) -> None:
        with self._atomic():
            old = self.get_mod(old_mod_id)
            new = self.get_mod(new_mod_id)
            if old is None:
                raise ValueError(f"旧 mod {old_mod_id} 不存在")
            if new is None:
                raise ValueError(f"新 mod {new_mod_id} 不存在（请先 add_mod）")
            # 1) 元数据迁移：新值为空/False 才被旧值覆盖，绝不抹掉用户
            #    在新记录上的输入
            merged: dict = {}
            if new.note is None and old.note is not None:
                merged["note"] = old.note
            if new.color_tag is None and old.color_tag is not None:
                merged["color_tag"] = old.color_tag
            if not new.is_special and old.is_special:
                merged["is_special"] = 1
            if merged:
                self._dynamic_update("mods", "mod_id", new_mod_id, merged)
            # 2) 快照改挂到新 id（迁移后可能超过保留条数，重新裁剪一次）
            self._conn.execute(
                "UPDATE mod_snapshots SET mod_id = ? WHERE mod_id = ?",
                (new_mod_id, old_mod_id))
            self._conn.execute(
                """DELETE FROM mod_snapshots
                   WHERE mod_id = ?
                     AND id NOT IN (
                       SELECT id FROM mod_snapshots
                       WHERE mod_id = ?
                       ORDER BY snapshot_at DESC, id DESC
                       LIMIT ?)""",
                (new_mod_id, new_mod_id, self._snapshot_keep))
            # 3) 归档记录指向新 id——这是"旧 id → 新 id"证据链的最后一环
            self._require(self._conn.execute(
                "UPDATE failed_mods SET replaced_by = ? WHERE mod_id = ?",
                (new_mod_id, old_mod_id)).rowcount,
                          f"failed_mods 中没有 {old_mod_id} 的归档（请先 mark_failed）")

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
            "SELECT * FROM special_mod_alerts WHERE id = ?", (aid,)).fetchone()
        return self._alert(row)

    def get_last_alert(self, mod_id: int) -> Alert | None:
        row = self._conn.execute(
            "SELECT * FROM special_mod_alerts WHERE mod_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (mod_id,)).fetchone()
        return self._alert(row) if row else None

    def list_alerts(self, mod_id: int) -> list[Alert]:
        rows = self._conn.execute(
            "SELECT * FROM special_mod_alerts WHERE mod_id = ? "
            "ORDER BY alert_at DESC, id DESC",
            (mod_id,)).fetchall()
        return [self._alert(r) for r in rows]


    # ---------- 账本导入导出（T19 dataExporter） ----------
    def export_all(self) -> dict:
        """整库倒出：{"user_version": 当前 schema 版本,
                      "tables": {表名: [行 dict, ...]}}（表序 LEDGER_TABLES）。
        行 = dataclass 自然类型（复用 _row 系列转换 + asdict）——
        存储格式（0/1、JSON 文本）绝不越过本方法的返回值（文件头
        约定 2）。行内 id 等库生成值原样保留，import_all 灌回时
        跨表引用（operations_log.backup_id、failed_mods.replaced_by）
        随之复原。dataExporter 是唯一调用方（切片、变换、编解码归它）。"""
        converters = {
            "games": self._game,
            "mods": self._mod,
            "mod_snapshots": self._snapshot,
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
            "user_version":
                self._conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": tables,
        }

    def import_all(self, exported: dict) -> None:
        """清库重灌（export_all 的逆操作，完整账本的导入语义）。

        一个事务内：先按外键安全顺序（LEDGER_TABLES 倒序）清空 7 张
        表，再按正序灌入。守门三道，全部显式爆炸：
        - exported 缺 user_version / tables → ValueError；
        - tables 键 ⊄ LEDGER_TABLES → ValueError；
        - user_version 比本库 PRAGMA user_version 新 → ValueError
          （旧程序读不懂新结构，绝不硬吃——dataExporter 文件头
          第 2 道版本闸的裁决点）。
        行只做自然类型 → 存储格式的转换（_to_storage），不做行级
        校验——那是 dataExporter 的职责；数据库约束（CHECK / 外键 /
        NOT NULL）兜底，任何一行不合法 → 整体回滚，绝不留半截账。
        自带事务（_atomic）；外层再包 transaction() 也安全（并入）。"""
        user_version = exported.get("user_version")
        tables = exported.get("tables")
        if not isinstance(user_version, int) or isinstance(user_version, bool):
            raise ValueError("载荷缺少 user_version（schema 版本）")
        if not isinstance(tables, dict):
            raise ValueError("载荷缺少 tables")
        unknown = set(tables) - set(LEDGER_TABLES)
        if unknown:
            raise ValueError(f"载荷里出现了不认识的表：{sorted(unknown)}")
        mine = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version > mine:
            raise ValueError(
                f"账本由更新的程序结构导出（schema v{user_version} > "
                f"当前 v{mine}），请先升级本工具再导入")
        with self._atomic():
            # 清空顺序 = 灌入顺序的倒序：RESTRICT 外键要求先删子表
            for table in reversed(LEDGER_TABLES):
                self._conn.execute(f"DELETE FROM {table}")
            for table in LEDGER_TABLES:
                for row in tables.get(table, []):
                    self._insert(table, self._to_storage(table, row))
