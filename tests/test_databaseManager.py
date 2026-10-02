"""数据库仓库层测试
"""
"""
覆盖 SQLiteRepository 的全部数据能力：建库幂等、games/mods 增删改查、
快照滚动淘汰（条数可配）、备份、操作日志、失效归档复合方法、
特殊 mod 提醒、事务语义（提交 / 回滚 / 禁止嵌套 / 复合方法并入外层）。

运行：python -m pytest tests/ -v
所有测试用 tmp_path 临时库——绝不碰你的 data/mods.db。
"""
import sqlite3
import time

import pytest

from core.models import Mod
from core.sqliteRepository import SQLiteRepository

GID = 1158310  # CK3


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "test.db")
    yield r
    r.close()


def make_game(r: SQLiteRepository, app_id: int = GID) -> int:
    r.add_game(app_id, "Crusader Kings III", r"D:\mods\ck3")
    return app_id


def make_mod(r: SQLiteRepository, mod_id: int, game_id: int = GID, **kw) -> None:
    r.add_mod(Mod(mod_id=mod_id, game_id=game_id, **kw))


# ---------- 建库与幂等 ----------

def test_schema_auto_created(tmp_path):
    db = tmp_path / "test.db"
    r = SQLiteRepository(db)
    r.close()
    con = sqlite3.connect(db)
    names = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"games", "mods", "mod_snapshots", "backups", "operations_log",
            "failed_mods", "special_mod_alerts",
            "purged_mods", "mod_dependencies"} <= names
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3

    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_reopen_idempotent(tmp_path):
    db = tmp_path / "test.db"
    r1 = SQLiteRepository(db)
    r1.add_game(1, "A", "d")
    r1.close()
    r2 = SQLiteRepository(db)
    # 重复打开：不重建表、不丢数据
    assert r2.get_game(1).name == "A"
    r2.close()


# ---------- games ----------

def test_game_crud(repo):
    repo.add_game(GID, "CK3", r"D:\mods\ck3")
    g = repo.get_game(GID)
    assert g.name == "CK3" and g.created_at is not None
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_game(GID, "重复", "x")  # 主键冲突显式爆炸
    repo.update_game(GID, backup_dir=r"D:\backup\ck3")
    assert repo.get_game(GID).backup_dir == r"D:\backup\ck3"
    with pytest.raises(ValueError):
        repo.update_game(294100, name="不存在的档案")
    assert [x.app_id for x in repo.list_games()] == [GID]


def test_delete_game_restricted(repo):
    make_game(repo, 1)
    make_game(repo, 2)
    make_mod(repo, 100, game_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        # RESTRICT 拦下带 mod 的档案
        repo.delete_game(1)
    repo.delete_game(2)
    assert repo.get_game(2) is None


# ---------- mods 基础 ----------

def test_add_mod_roundtrip(repo):
    make_game(repo)
    m = Mod(mod_id=3403925213, game_id=GID, title="地图",
            tags=["Map", "全景"], is_special=True, note="n",
            time_updated=1788430663, local_timeupdated=1788430663,
            manifest="5826407289947113109", local_size=30934319)
    repo.add_mod(m)
    got = repo.get_mod(3403925213)
    assert got.title == "地图" and got.tags == ["Map", "全景"]
    assert got.is_special is True and got.status == "tracked"
    assert got.manifest == "5826407289947113109" and got.local_size == 30934319
    assert got.first_tracked_at is not None  # DB 的 DEFAULT 填的
    with pytest.raises(sqlite3.IntegrityError):
        make_mod(repo, 3403925213)  # 重复 id


def test_filter_existing_ids(repo):
    make_game(repo)
    make_mod(repo, 1)
    make_mod(repo, 2)
    assert repo.filter_existing_ids([1, 2, 999, 1]) == {1, 2}


def test_list_mods_filters(repo):
    make_game(repo)
    make_mod(repo, 1, title="全景地图", note="每周更新", time_updated=300)
    make_mod(repo, 2, title="小地图", is_special=True, color_tag="red",
             time_updated=200)
    make_mod(repo, 3, title="装饰", time_updated=100)
    make_mod(repo, 4, title="旧", time_updated=50)
    assert len(repo.list_mods(GID)) == 4
    assert [m.mod_id for m in repo.list_mods(GID, special_only=True)] == [2]
    assert [m.mod_id for m in repo.list_mods(GID, color_tag="red")] == [2]
    assert sorted(m.mod_id for m in repo.list_mods(GID, search="地图")) == [1, 2]
    assert [m.mod_id for m in repo.list_mods(GID, search="每周")] == [1]
    assert [m.mod_id for m in repo.list_mods(GID, status="downloaded")] == []
    assert repo.list_mods(GID, order_by="time_updated ASC")[0].mod_id == 4
    assert repo.list_mods(GID)[0].mod_id == 1  # DESC 默认
    assert len(repo.list_mods(GID, limit=2)) == 2
    with pytest.raises(ValueError):
        repo.list_mods(GID, order_by="mod_id; DROP TABLE mods")  # 白名单拦截


def test_update_api_metadata_partial(repo):
    make_game(repo)
    make_mod(repo, 1, title="旧标题", note="保留")
    repo.update_api_metadata(1, title="新标题", time_updated=42, subscriptions=7)
    m = repo.get_mod(1)
    assert m.title == "新标题" and m.time_updated == 42 and m.subscriptions == 7
    assert m.note == "保留"  # 没传的没被动
    repo.update_api_metadata(1, tags=[])
    assert repo.get_mod(1).tags == []  # 空列表 = 清空
    with pytest.raises(ValueError):
        repo.update_api_metadata(999, title="x")


def test_update_api_metadata_writes_last_time_updated(repo):
    # 检测到远端版本变化时，把当次的远端值记进 last_time_updated，
    # 作为下一轮检测的比较基准
    make_game(repo)
    make_mod(repo, 1)
    repo.update_api_metadata(1, time_updated=1000, last_time_updated=1000)
    m = repo.get_mod(1)
    assert m.time_updated == 1000
    assert m.last_time_updated == 1000

    # None = 不修改：之后只补标题时，上一轮记下的远端值不能被抹掉
    repo.update_api_metadata(1, title="示例标题")
    assert repo.get_mod(1).last_time_updated == 1000


def test_touch_checked(repo):
    make_game(repo)
    make_mod(repo, 1)
    make_mod(repo, 2)
    before = int(time.time())
    repo.touch_checked([1, 2])
    for m in (repo.get_mod(1), repo.get_mod(2)):
        assert abs(m.last_checked_at - before) < 60


def test_update_local_state(repo):
    make_game(repo)
    make_mod(repo, 1)
    repo.update_local_state(1, local_timeupdated=1779841231,
                            manifest="1601231248691770967",
                            local_size=1536419, status="downloaded")
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.local_timeupdated == 1779841231
    assert m.manifest == "1601231248691770967"
    with pytest.raises(ValueError):
        repo.update_local_state(999, local_timeupdated=1)  # 不存在
    with pytest.raises(ValueError):
        repo.update_local_state(1, local_timeupdated=None)  # 必填项


def test_setters_and_clearing(repo):
    make_game(repo)
    make_mod(repo, 1)
    repo.set_note(1, "hello")
    repo.set_color_tag(1, "blue")
    repo.set_special(1, True)
    m = repo.get_mod(1)
    assert (m.note, m.color_tag, m.is_special) == ("hello", "blue", True)
    repo.set_color_tag(1, None)  # None = 清除
    assert repo.get_mod(1).color_tag is None
    with pytest.raises(ValueError):
        repo.set_note(999, "x")


def test_mark_deleted(repo):
    make_game(repo)
    make_mod(repo, 1, title="x", local_size=123)
    repo.mark_deleted(1, {"local_size": 123})
    m = repo.get_mod(1)
    assert m.status == "deleted" and m.deleted_at is not None
    assert m.deleted_last_state == {"local_size": 123}


# ---------- 快照 ----------

def test_snapshot_rolling_default_five(repo):
    # 默认保留 5 条：插 7 条后只剩最新的 5 条，且顺序为新→旧
    make_game(repo)
    make_mod(repo, 1)
    for i in range(1, 8):  # 插 7 条
        repo.add_snapshot(1, time_updated=i * 100, snapshot_at=i)
    snaps = repo.list_snapshots(1)
    assert len(snaps) == 5
    assert snaps[0].snapshot_at == 7 and snaps[-1].snapshot_at == 3  # 新→旧


def test_snapshot_keep_is_configurable(tmp_path):
    # 构造参数注入 2 条：验证滚动淘汰跟随配置走，而不是写死的数字
    r = SQLiteRepository(tmp_path / "test.db", snapshot_keep=2)
    make_game(r)
    make_mod(r, 1)
    for i in range(1, 6):  # 插 5 条
        r.add_snapshot(1, time_updated=i * 100, snapshot_at=i)
    snaps = r.list_snapshots(1)
    assert len(snaps) == 2
    assert snaps[0].snapshot_at == 5 and snaps[-1].snapshot_at == 4
    r.close()


# ---------- backups ----------

def test_backup_lifecycle(repo):
    make_game(repo)
    make_mod(repo, 1001)
    b = repo.add_backup(1001, r"D:\bk\1001_v5", 100, 5)
    assert b.id is not None and b.created_at is not None and b.pinned is False
    with pytest.raises(sqlite3.IntegrityError):
        # UNIQUE 路径
        repo.add_backup(1001, r"D:\bk\1001_v5", 100, 5)
    repo.set_pinned(b.id, True)
    assert repo.get_backup(b.id).pinned is True
    assert repo.list_backups(include_pinned=False) == []
    assert repo.list_backups(oldest_first=True)[0].id == b.id
    assert repo.sum_backup_bytes() == 100
    repo.delete_backup_record(b.id)
    assert repo.get_backup(b.id) is None


# ---------- operations_log ----------

def test_operations_flow(repo):
    op = repo.add_operation("steamcmd +workshop_download_item ...")
    repo.finish_operation(op, error_count=2, result="error")
    logs = repo.list_operations()
    assert logs[0].id == op and logs[0].error_count == 2 and logs[0].result == "error"
    with pytest.raises(ValueError):
        repo.finish_operation(999, result="x")


# ---------- failed_mods 复合方法 ----------

def test_mark_failed(repo):
    make_game(repo)
    make_mod(repo, 1, title="消失的mod", time_updated=555)
    repo.mark_failed(1, "result=9")
    assert repo.get_mod(1).status == "failed"
    failed = repo.list_failed(GID)
    assert failed[0].mod_id == 1 and failed[0].replaced_by is None
    assert failed[0].last_known_state["title"] == "消失的mod"
    assert failed[0].last_known_state["time_updated"] == 555
    with pytest.raises(ValueError):
        repo.mark_failed(999, "x")


def test_replace_failed_mod(repo):
    make_game(repo)
    make_mod(repo, 1, title="旧mod", note="备注", color_tag="red", is_special=True)
    repo.add_snapshot(1, time_updated=100, snapshot_at=1)
    repo.add_snapshot(1, time_updated=200, snapshot_at=2)
    repo.mark_failed(1, "result=9")
    make_mod(repo, 2, title="旧mod重传")  # 作者重传的新 id
    repo.replace_failed_mod(1, 2)
    new = repo.get_mod(2)
    assert new.note == "备注" and new.color_tag == "red" and new.is_special is True
    assert len(repo.list_snapshots(2)) == 2  # 快照改挂
    assert repo.list_snapshots(1) == []
    assert repo.list_failed(GID)[0].replaced_by == 2  # 证据链
    assert repo.get_mod(1).status == "failed"  # 旧记录保留


def test_replace_merge_semantics(repo):
    make_game(repo)
    make_mod(repo, 1, note="旧备注", color_tag="red")
    repo.mark_failed(1, "r9")
    make_mod(repo, 2, note="新备注", is_special=True)
    repo.replace_failed_mod(1, 2)
    new = repo.get_mod(2)
    assert new.note == "新备注"  # 新值已有 → 不被旧值覆盖
    assert new.color_tag == "red"  # 新值为空 → 迁移旧值
    assert new.is_special is True


# ---------- special_mod_alerts ----------

def test_alerts(repo):
    make_game(repo)
    make_mod(repo, 1)
    repo.add_alert(1, remote_time_updated=100)
    repo.add_alert(1, remote_time_updated=200, was_downloaded=True)
    last = repo.get_last_alert(1)
    assert last.remote_time_updated == 200 and last.was_downloaded is True
    assert len(repo.list_alerts(1)) == 2


# ---------- 事务 ----------

def test_transaction_commit(repo):
    make_game(repo)
    with repo.transaction():
        make_mod(repo, 1)
        make_mod(repo, 2)
    assert repo.filter_existing_ids({1, 2}) == {1, 2}


def test_transaction_rollback(repo):
    make_game(repo)
    with pytest.raises(RuntimeError):
        with repo.transaction():
            make_mod(repo, 1)
            raise RuntimeError("boom")
    assert repo.filter_existing_ids({1}) == set()  # 一片都没留下


def test_transaction_nesting_forbidden(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            with repo.transaction():
                pass


def test_composite_joins_outer_transaction(repo):
    make_game(repo)
    make_mod(repo, 1, title="T")
    with repo.transaction():
        repo.mark_failed(1, "r9")  # _atomic 并入外层
        make_mod(repo, 2)
    assert repo.get_mod(1).status == "failed"
    assert repo.filter_existing_ids({2}) == {2}


def test_composite_rolled_back_with_outer(repo):
    make_game(repo)
    make_mod(repo, 1, title="T")
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.mark_failed(1, "r9")
            raise RuntimeError("boom")
    assert repo.get_mod(1).status == "tracked"  # 全部回滚
    assert repo.list_failed(GID) == []

# ---------- 档案删除与备份总览 ----------

def make_full_scene(r: SQLiteRepository) -> int:
    """一个"满员"档案（id=1）+ 一个无辜档案（id=2）。
    返回满员档案的备份登记 id——级联测试和总览测试共用这套场景。"""
    make_game(r, 1)
    make_mod(r, 101, game_id=1, title="常规")
    make_mod(r, 102, game_id=1, title="已软删")
    r.mark_deleted(102, {"local_size": 1})
    make_mod(r, 103, game_id=1, title="有备份的")
    r.add_snapshot(103, time_updated=100, snapshot_at=1)
    r.add_alert(103, remote_time_updated=100)
    b = r.add_backup(103, r"D:\bk\103_v100", 500, 100)
    make_mod(r, 104, game_id=1, title="已失效")
    r.mark_failed(104, "result=9")
    # 无辜档案：同样带 mod 和备份，用来证明深删不殃及邻里
    make_game(r, 2)
    make_mod(r, 201, game_id=2, title="邻居的mod")
    r.add_backup(201, r"D:\bk\201_v1", 60, 1)
    return b.id


def test_game_deletion_summary(repo):
    make_full_scene(repo)
    s = repo.game_deletion_summary(1)
    assert (s.app_id, s.mod_total, s.mod_deleted) == (1, 4, 1)
    assert (s.failed_count, s.backup_count, s.backup_bytes) == (1, 1, 500)
    assert s.backup_paths == [r"D:\bk\103_v100"]
    with pytest.raises(ValueError):
        repo.game_deletion_summary(999)


def test_delete_game_deep_cleans_all(repo):
    bid = make_full_scene(repo)
    op = repo.add_operation("steamcmd +workshop_download_item …", backup_id=bid)
    summary = repo.delete_game_deep(1)
    # 返回的盘点 = 删掉了的东西
    assert summary.mod_total == 4 and summary.backup_count == 1
    # 满员档案账面全清：档案、mod、快照、提醒、备份登记、失效归档
    assert repo.get_game(1) is None
    assert repo.list_mods(1) == []
    assert repo.list_snapshots(103) == []
    assert repo.list_alerts(103) == []
    assert repo.get_backup(bid) is None
    assert repo.list_failed(1) == []
    # 无辜档案毫发无损
    assert repo.get_game(2) is not None
    assert repo.get_mod(201).title == "邻居的mod"
    assert [r.backup_path for r in repo.list_backups_overview(2)] == [r"D:\bk\201_v1"]
    # 操作日志保留（全局历史），backup_id 被外键自动置空、不悬空
    logs = repo.list_operations()
    assert len(logs) == 1 and logs[0].id == op and logs[0].backup_id is None


def test_delete_game_deep_empty_game(repo):
    # 空档案（名下什么都没有）也能走完整通道，不报错
    make_game(repo, 3)
    s = repo.delete_game_deep(3)
    assert s.mod_total == 0 and s.backup_count == 0
    assert repo.get_game(3) is None


def test_delete_game_deep_nonexistent(repo):
    with pytest.raises(ValueError):
        repo.delete_game_deep(999)


def test_list_backups_overview(repo):
    bid = make_full_scene(repo)
    repo.set_pinned(bid, True)
    rows = repo.list_backups_overview()
    assert len(rows) == 2
    assert rows[0].backup_path == r"D:\bk\201_v1"  # 新→旧（同刻按 id 倒序）
    by_path = {r.backup_path: r for r in rows}
    row103 = by_path[r"D:\bk\103_v100"]
    assert row103.mod_title == "有备份的" and row103.mod_status == "tracked"
    assert row103.game_name == "Crusader Kings III" and row103.pinned is True
    assert by_path[r"D:\bk\201_v1"].game_id == 2
    # 按档案筛
    assert [r.backup_path for r in repo.list_backups_overview(2)] == [r"D:\bk\201_v1"]
    assert repo.list_backups_overview(999) == []  # 不存在的档案 → 空清单，不报错
def test_migration_v2_to_v3(tmp_path):
    """v2 老库打开 → 静默迁 v3：补建 mod_dependencies，purged_mods 不动。"""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
                      CREATE TABLE games (
                                             app_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                                             created_at INTEGER NOT NULL DEFAULT (
                                                 CAST(strftime('%s','now') AS INTEGER)),
                                             download_dir TEXT NOT NULL, game_mod_dir TEXT, backup_dir TEXT);
                      CREATE TABLE purged_mods (
                                                   mod_id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL,
                                                   title TEXT,
                                                   purged_at INTEGER NOT NULL DEFAULT (
                                                       CAST(strftime('%s','now') AS INTEGER)),
                                                   note TEXT);
                      PRAGMA user_version = 2;
                      """)
    con.commit()
    con.close()

    r = SQLiteRepository(db)  # 打开即迁移
    r.close()

    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {row[1] for row in con.execute(
            "PRAGMA table_info(mod_dependencies)")}
        assert {"mod_id", "required_mod_id", "fetched_at"} <= cols
        # 幂等：再开一次不炸不回退
        r2 = SQLiteRepository(db)
        r2.close()
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        con.close()
