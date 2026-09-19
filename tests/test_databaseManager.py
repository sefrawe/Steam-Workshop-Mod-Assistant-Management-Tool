"""
tests/test_databaseManager.py · M1 任务 2 验收
运行：python -m pytest tests/ -v
所有测试用 tmp_path 临时库——不碰你的 data/mods.db
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
    assert {"games", "mods", "mod_snapshots", "backups",
            "operations_log", "failed_mods", "special_mod_alerts"} <= names
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_reopen_idempotent(tmp_path):
    db = tmp_path / "test.db"
    r1 = SQLiteRepository(db)
    r1.add_game(1, "A", "d")
    r1.close()
    r2 = SQLiteRepository(db)          # 重复打开：不重建表、不丢数据
    assert r2.get_game(1).name == "A"
    r2.close()


# ---------- games ----------

def test_game_crud(repo):
    repo.add_game(GID, "CK3", r"D:\mods\ck3")
    g = repo.get_game(GID)
    assert g.name == "CK3" and g.created_at is not None
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_game(GID, "重复", "x")            # 主键冲突显式爆炸
    repo.update_game(GID, backup_dir=r"D:\backup\ck3")
    assert repo.get_game(GID).backup_dir == r"D:\backup\ck3"
    with pytest.raises(ValueError):
        repo.update_game(294100, name="不存在的档案")
    assert [x.app_id for x in repo.list_games()] == [GID]


def test_delete_game_restricted(repo):
    make_game(repo, 1)
    make_game(repo, 2)
    make_mod(repo, 100, game_id=1)
    with pytest.raises(sqlite3.IntegrityError):    # RESTRICT 拦下带 mod 的档案
        repo.delete_game(1)
    repo.delete_game(2)
    assert repo.get_game(2) is None


# ---------- mods 基础 ----------

def test_add_mod_roundtrip(repo):
    make_game(repo)
    m = Mod(mod_id=3403925213, game_id=GID, title="地图", tags=["Map", "全景"],
            is_special=True, note="n", time_updated=1788430663,
            local_timeupdated=1788430663, manifest="5826407289947113109",
            local_size=30934319)
    repo.add_mod(m)
    got = repo.get_mod(3403925213)
    assert got.title == "地图" and got.tags == ["Map", "全景"]
    assert got.is_special is True and got.status == "tracked"
    assert got.manifest == "5826407289947113109" and got.local_size == 30934319
    assert got.first_tracked_at is not None        # DB 的 DEFAULT 填的
    with pytest.raises(sqlite3.IntegrityError):
        make_mod(repo, 3403925213)                 # 重复 id


def test_filter_existing_ids(repo):
    make_game(repo)
    make_mod(repo, 1)
    make_mod(repo, 2)
    assert repo.filter_existing_ids([1, 2, 999, 1]) == {1, 2}


def test_list_mods_filters(repo):
    make_game(repo)
    make_mod(repo, 1, title="全景地图", note="每周更新", time_updated=300)
    make_mod(repo, 2, title="小地图", is_special=True, color_tag="red", time_updated=200)
    make_mod(repo, 3, title="装饰", time_updated=100)
    make_mod(repo, 4, title="旧", time_updated=50)
    assert len(repo.list_mods(GID)) == 4
    assert [m.mod_id for m in repo.list_mods(GID, special_only=True)] == [2]
    assert [m.mod_id for m in repo.list_mods(GID, color_tag="red")] == [2]
    assert sorted(m.mod_id for m in repo.list_mods(GID, search="地图")) == [1, 2]
    assert [m.mod_id for m in repo.list_mods(GID, search="每周")] == [1]
    assert [m.mod_id for m in repo.list_mods(GID, status="downloaded")] == []
    assert repo.list_mods(GID, order_by="time_updated ASC")[0].mod_id == 4
    assert repo.list_mods(GID)[0].mod_id == 1                   # DESC 默认
    assert len(repo.list_mods(GID, limit=2)) == 2
    with pytest.raises(ValueError):
        repo.list_mods(GID, order_by="mod_id; DROP TABLE mods")  # 白名单拦截


def test_update_api_metadata_partial(repo):
    make_game(repo)
    make_mod(repo, 1, title="旧标题", note="保留")
    repo.update_api_metadata(1, title="新标题", time_updated=42, subscriptions=7)
    m = repo.get_mod(1)
    assert m.title == "新标题" and m.time_updated == 42 and m.subscriptions == 7
    assert m.note == "保留"                                     # 没传的没被动
    repo.update_api_metadata(1, tags=[])
    assert repo.get_mod(1).tags == []                           # 空列表 = 清空
    with pytest.raises(ValueError):
        repo.update_api_metadata(999, title="x")


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
        repo.update_local_state(999, local_timeupdated=1)       # 不存在
    with pytest.raises(ValueError):
        repo.update_local_state(1, local_timeupdated=None)      # 必填项


def test_setters_and_clearing(repo):
    make_game(repo)
    make_mod(repo, 1)
    repo.set_note(1, "hello")
    repo.set_color_tag(1, "blue")
    repo.set_special(1, True)
    m = repo.get_mod(1)
    assert (m.note, m.color_tag, m.is_special) == ("hello", "blue", True)
    repo.set_color_tag(1, None)                                 # None = 清除
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

def test_snapshot_rolling_ten(repo):
    make_game(repo)
    make_mod(repo, 1)
    for i in range(1, 13):                                      # 插 12 条
        repo.add_snapshot(1, time_updated=i * 100, snapshot_at=i)
    snaps = repo.list_snapshots(1)
    assert len(snaps) == 10
    assert snaps[0].snapshot_at == 12 and snaps[-1].snapshot_at == 3  # 新→旧


# ---------- backups ----------

def test_backup_lifecycle(repo):
    make_game(repo)
    make_mod(repo, 1001)
    b = repo.add_backup(1001, r"D:\bk\1001_v5", 100, 5)
    assert b.id is not None and b.created_at is not None and b.pinned is False
    with pytest.raises(sqlite3.IntegrityError):                 # UNIQUE 路径
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
    make_mod(repo, 2, title="旧mod重传")                        # 作者重传的新 id
    repo.replace_failed_mod(1, 2)
    new = repo.get_mod(2)
    assert new.note == "备注" and new.color_tag == "red" and new.is_special is True
    assert len(repo.list_snapshots(2)) == 2                     # 快照改挂
    assert repo.list_snapshots(1) == []
    assert repo.list_failed(GID)[0].replaced_by == 2            # 证据链
    assert repo.get_mod(1).status == "failed"                   # 旧记录保留


def test_replace_merge_semantics(repo):
    make_game(repo)
    make_mod(repo, 1, note="旧备注", color_tag="red")
    repo.mark_failed(1, "r9")
    make_mod(repo, 2, note="新备注", is_special=True)
    repo.replace_failed_mod(1, 2)
    new = repo.get_mod(2)
    assert new.note == "新备注"        # 新值已有 → 不被旧值覆盖
    assert new.color_tag == "red"      # 新值为空 → 迁移旧值
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
    assert repo.filter_existing_ids({1}) == set()               # 一片都没留下


def test_transaction_nesting_forbidden(repo):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            with repo.transaction():
                pass


def test_composite_joins_outer_transaction(repo):
    make_game(repo)
    make_mod(repo, 1, title="T")
    with repo.transaction():
        repo.mark_failed(1, "r9")                               # _atomic 并入外层
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
    assert repo.get_mod(1).status == "tracked"                  # 全部回滚
    assert repo.list_failed(GID) == []
