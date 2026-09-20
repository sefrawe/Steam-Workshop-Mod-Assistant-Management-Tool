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
import json
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

from core.models import (
    Alert, Backup, FailedMod, Game, Mod, OperationLog, Snapshot,
)
from core.modRepository import ALLOWED_ORDERS, ModRepository

_VALID_STATUSES = frozenset({"tracked", "downloaded", "deleted", "failed"})
# 每 mod 保留的快照条数：这里只是默认值，实际值由主窗口从设置读出后
# 通过构造参数注入（设置页可改，默认 5 条）
_DEFAULT_SNAPSHOT_KEEP = 5
# IN (...) 分片大小；SQL 变量占位符有上限，分片永不出错
_CHUNK = 500


def _now() -> int:
    return int(time.time())


def _dumps(obj) -> str:
    """ensure_ascii=False：中文备注原样存储，可读性好且省空间"""
    return json.dumps(obj, ensure_ascii=False)


class SQLiteRepository(ModRepository):

    # ---------- 基础设施 ----------

    def __init__(self, db_path: str | Path,
                 *, snapshot_keep: int = _DEFAULT_SNAPSHOT_KEEP) -> None:
        # snapshot_keep 至少为 1：0 或负数会让快照功能整个失效，直接拦在门口
        self._snapshot_keep = max(1, int(snapshot_keep))
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
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
                  limit: int | None = None) -> list[Mod]:
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
            # LIKE 的 % _ 是通配符，先转义用户的输入，再声明 ESCAPE 字符
            safe = (search.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            where.append("(title LIKE ? ESCAPE '\\' "
                         "OR note LIKE ? ESCAPE '\\')")
            params += [f"%{safe}%", f"%{safe}%"]
        sql = (f"SELECT * FROM mods WHERE {' AND '.join(where)} "
               f"ORDER BY {order_by}")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._mod(r)
                for r in self._conn.execute(sql, params).fetchall()]

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
