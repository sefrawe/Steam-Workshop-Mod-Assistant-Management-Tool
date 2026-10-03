"""旧账迁移测试（M0）
"""
"""
tests/test_legacyMigrate.py · peek 鉴别 / 两道闸 / 字段翻译 / 事务完整性。

世界状态先列表再断言（项目惯例）。旧库夹具见 conftest.py 的
make_old_db——表结构是旧版 schema 的精简版，刻意不打开外键，
好造出孤儿行、坏 JSON 这类真实旧库里会出现的脏数据。
翻译函数是纯函数，直接喂数据行做单元断言。
"""
import sqlite3

import pytest

from core import legacyMigrate
from core.legacyMigrate import migrate_old_ledger, peek_old_ledger
from core.sqliteRepository import SQLiteRepository

GAME = 294100


@pytest.fixture
def new_repo(tmp_path):
    """迁移目标：空的新仓库。"""
    r = SQLiteRepository(tmp_path / "new.db")
    yield r
    r.close()


def _populate_old(conn):
    """造一个典型旧库：1 档案 3 mod（1 继承 / 1 版本未知 / 1 待下载），
    全套附属表各 1 行，外加 1 个孤儿 mod、1 段坏 JSON、1 条不在账的
    依赖目标（缺依赖常态）。"""
    conn.execute(
        "INSERT INTO games (app_id, name, download_dir) VALUES (?, ?, ?)",
        (GAME, "RimWorld", "D:/dl"))
    # m1：已下载带本地版本 100（→ 继承 ⤵）
    conn.execute(
        """INSERT INTO mods (mod_id, game_id, status, title,
           local_timeupdated, manifest, local_size, tags, time_updated)
           VALUES (1, ?, 'downloaded', '旧模甲', 100, '999', 2048,
           '["中文"]', 500)""", (GAME,))
    # m2：已下载但本地版本 NULL（→ 版本未知）
    conn.execute(
        "INSERT INTO mods (mod_id, game_id, status, title) "
        "VALUES (2, ?, 'downloaded', '旧模乙')", (GAME,))
    # m3：待下载，tags 是坏 JSON
    conn.execute(
        "INSERT INTO mods (mod_id, game_id, status, tags) "
        "VALUES (3, ?, 'tracked', '不是JSON')", (GAME,))
    # m99：孤儿（档案 888 不存在——旧库外键没开拦不住它）
    conn.execute(
        "INSERT INTO mods (mod_id, game_id, status) "
        "VALUES (99, 888, 'tracked')")
    conn.execute(
        "INSERT INTO mod_snapshots (mod_id, snapshot_at, time_updated, "
        "manifest, local_timeupdated) VALUES (1, 1000, 90, '888', 88)")
    conn.execute(
        "INSERT INTO backups (mod_id, backup_path, size_bytes, "
        "version_timeupdated, pinned) VALUES (1, 'bk1', 10, 100, 1)")
    conn.execute(
        "INSERT INTO operations_log (command, backup_id) "
        "VALUES ('旧命令', 1)")
    conn.execute(
        """INSERT INTO failed_mods (mod_id, game_id, reason,
           last_known_state) VALUES (2, ?, 'reason',
           '{"title":"旧模乙","local_timeupdated":55,"manifest":"7"}')""",
        (GAME,))
    conn.execute(
        "INSERT INTO special_mod_alerts (mod_id, remote_time_updated, "
        "was_downloaded) VALUES (1, 500, 1)")
    conn.execute(
        "INSERT INTO purged_mods (mod_id, game_id, title) "
        "VALUES (777, ?, '清过的')", (GAME,))
    conn.execute(
        "INSERT INTO mod_dependencies (mod_id, required_mod_id, fetched_at) "
        "VALUES (1, 2, 1234), (1, 999, 1234)")
    conn.commit()


# ============ peek：先看清楚 ============

def test_peek_kinds(tmp_path, make_old_db):
    # 六种身份各验一遍：缺失 / 不是库 / 空库 / 新世系 / 旧世系
    assert peek_old_ledger(tmp_path / "无").kind == "missing"
    fake = tmp_path / "fake.db"
    fake.write_text("文本", encoding="utf-8")
    assert peek_old_ledger(fake).kind == "not_sqlite"
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert peek_old_ledger(empty).kind == "empty"
    fresh = SQLiteRepository(tmp_path / "new.db")
    fresh.close()
    assert peek_old_ledger(tmp_path / "new.db").kind == "new"
    path, conn = make_old_db()
    _populate_old(conn)
    conn.close()
    info = peek_old_ledger(path)
    assert info.kind == "old" and info.is_migratable
    assert info.counts["games"] == 1 and info.counts["mods"] == 4


def test_peek_alien(tmp_path):
    # 表结构对不上（有 mods 表但既无旧列也无新表）→ 拒收不硬吃
    p = tmp_path / "alien.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE mods (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert peek_old_ledger(p).kind == "alien"


# ============ migrate：两道前置闸 ============

def test_migrate_refuses_non_old_source(new_repo, tmp_path):
    # 闸 1：源头不是旧库（拿新工具自己的库充数）→ 拒绝
    other = SQLiteRepository(tmp_path / "another.db")
    other.close()
    with pytest.raises(ValueError, match="旧账本"):
        migrate_old_ledger(tmp_path / "another.db", new_repo)


def test_migrate_refuses_nonempty_target(new_repo, make_old_db):
    # 闸 2：目标库已有档案 → 拒绝（迁移只进空库）
    path, conn = make_old_db()
    _populate_old(conn)
    conn.close()
    new_repo.add_game(1, "已有", "D:/x")
    with pytest.raises(ValueError, match="空库"):
        migrate_old_ledger(path, new_repo)


# ============ migrate：正常搬运 ============

def test_migrate_happy_path(new_repo, make_old_db):
    # 世界状态：典型旧库（见 _populate_old 注释）
    path, conn = make_old_db()
    _populate_old(conn)
    conn.close()
    report = migrate_old_ledger(path, new_repo)
    # 报数：孤儿 mod 跳过不计入 mods（3 不是 4）
    assert (report.games, report.mods) == (1, 3)
    assert report.mods_inherited == 1 and report.mods_version_unknown == 1
    assert (report.snapshots, report.backups, report.operations) == (1, 1, 1)
    assert (report.failed, report.alerts, report.purged) == (1, 1, 1)
    assert report.dependency_edges == 2
    assert len(report.warnings) >= 2   # 孤儿 mod + 坏 JSON，各有提醒
    # 继承口径：版本原样带过来、来源=⤵继承旧账、确认时刻=迁移时刻
    m1 = new_repo.get_mod(1)
    assert (m1.status, m1.confirmed_version) == ("downloaded", 100)
    assert m1.confirmed_source == "inherited_acf"
    assert m1.confirmed_at is not None
    # 版本未知口径：状态保留、确认三件套全空
    m2 = new_repo.get_mod(2)
    assert m2.status == "downloaded" and m2.version_unknown
    # 待下载照旧；坏 JSON 的标签放弃（记提醒），其余字段照搬
    m3 = new_repo.get_mod(3)
    assert m3.status == "tracked" and m3.tags is None
    # 快照只带远端侧；备份钉住状态保真
    snaps = new_repo.list_snapshots(1)
    assert len(snaps) == 1 and snaps[0].time_updated == 90
    assert new_repo.list_backups(1)[0].pinned is True
    # 失效归档的末态翻译成新口径：版本改名继承、manifest 丢弃
    f = new_repo.list_failed(GAME)[0]
    assert f.last_known_state["confirmed_version"] == 55
    assert "manifest" not in f.last_known_state
    # 黑名单生效；依赖边保留（999 不在账正是"缺依赖"的常态）
    assert new_repo.is_purged(777)
    assert new_repo.list_dependencies(1) == [2, 999]
    # 旧世界没有判决制：不从旧账编造判决
    assert new_repo.pending_confirmations() == []
    assert new_repo.list_verdicts(1) == []


def test_migrate_old_v1_without_tail_tables(new_repo, make_old_db):
    # 更老的旧库：没有黑名单和依赖边两张表 → 按空表处理，不炸
    path, conn = make_old_db()
    conn.execute("DROP TABLE purged_mods")
    conn.execute("DROP TABLE mod_dependencies")
    conn.execute("INSERT INTO games (app_id, name, download_dir) "
                 "VALUES (1, 'G', 'D:/g')")
    conn.execute("INSERT INTO mods (mod_id, game_id, status) "
                 "VALUES (1, 1, 'tracked')")
    conn.commit()
    conn.close()
    report = migrate_old_ledger(path, new_repo)
    assert report.purged == 0 and report.dependency_edges == 0


def test_migrate_rolls_back_on_failure(new_repo, make_old_db, monkeypatch):
    # 世界状态：正常旧库；但把目标库的 import_all 换成必炸的假货
    # 断言：外层事务整体回滚——目标库一个字节都没写进去
    path, conn = make_old_db()
    _populate_old(conn)
    conn.close()

    def boom(_payload):
        raise RuntimeError("炸在半路")
    monkeypatch.setattr(new_repo, "import_all", boom)
    with pytest.raises(RuntimeError):
        migrate_old_ledger(path, new_repo)
    assert new_repo.list_games() == []
    assert new_repo.is_purged(777) is False


# ============ 翻译纯函数的单元断言 ============

def test_remap_state_json_translation():
    # 旧末态 → 新末态：版本改名并标注继承；manifest / local_path 丢弃
    old = ('{"title":"t","local_timeupdated":42,"manifest":"9",'
           '"local_path":"D:/x","note":"n"}')
    out = legacyMigrate._remap_state_json(old, [], "测试")
    assert out["confirmed_version"] == 42
    assert out["confirmed_source"] == "inherited_acf"
    assert "manifest" not in out and "local_path" not in out
    assert out["title"] == "t" and out["note"] == "n"


def test_remap_state_json_broken_returns_none():
    # 坏 JSON 不硬吃：返回 None（该段放弃），警告留给调用方记
    assert legacyMigrate._remap_state_json("垃圾", [], "测试") is None


def test_remap_mod_drops_dead_columns():
    # manifest / local_path 不进新账（新世界没有这两个概念）
    old = {"mod_id": 1, "game_id": GAME, "status": "downloaded",
           "local_timeupdated": 100, "manifest": "9", "local_path": "p",
           "tags": '["a"]'}
    out = legacyMigrate._remap_mod(old, 12345, [])
    assert "manifest" not in out and "local_path" not in out
    assert out["confirmed_version"] == 100
    assert out["confirmed_source"] == "inherited_acf"
    assert out["confirmed_at"] == 12345
    assert out["tags"] == ["a"]      # JSON 文本已还原成 list


def test_remap_mod_version_unknown_stays_unknown():
    # 旧账"已下载但没有本地版本" → 新口径版本未知，不编造
    old = {"mod_id": 2, "game_id": GAME, "status": "downloaded",
           "local_timeupdated": None}
    out = legacyMigrate._remap_mod(old, 1, [])
    assert out["confirmed_version"] is None
    assert out["confirmed_source"] is None
