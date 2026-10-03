"""SQLiteRepository 全量测试（M0）
"""
"""
tests/test_sqliteRepository.py · 契约实现的主测试。

写法沿项目惯例（test_backupRelocate 先例）：
- 每个用例开头注释写明"世界状态"——先摆好盘面再断言；
- 只碰 tmp_path 临时库，绝不碰真实数据（R10）；
- 个别用例为模拟"时间流逝"会直接拨库里的时间戳，注释里说明。
覆盖面：开库鉴别 / games / mods 查询筛选 / 状态迁移 / 彻底清账 /
快照滚动 / 翻译缓存 / 备份 / 日志与提醒 / 黑名单 / 依赖边 /
账本导入导出。确认门三正门与撤销门单列 test_confirmationGate.py。
"""
import sqlite3

import pytest

from core.models import Mod
from core.sqliteRepository import SQLiteRepository

GAME = 294100


def _add_game(repo, app_id=GAME, name="RimWorld"):
    repo.add_game(app_id, name, f"D:/steamcmd/workshop/content/{app_id}")


def _add_mod(repo, mod_id, game_id=GAME, status="tracked", **kw):
    repo.add_mod(Mod(mod_id=mod_id, game_id=game_id, status=status, **kw))


# ============ 开库鉴别（空库建表 / 旧世系拒绝 / 陌生版本拒绝） ============

def test_fresh_db_gets_schema(repo):
    # 世界状态：全新空库，刚由夹具打开
    # 断言：schema 建好（user_version=1）、可正常读写
    assert repo.list_games() == []
    ver = repo._conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 1


def test_reopen_same_db_is_idempotent(tmp_path):
    # 世界状态：第一次打开建好表并写入 1 个档案 → 关闭 → 再打开
    # 断言：不报错、数据原样（重复打开幂等）
    p = tmp_path / "mods.db"
    r1 = SQLiteRepository(p)
    _add_game(r1, 1158310, "CK3")
    r1.close()
    r2 = SQLiteRepository(p)
    assert [g.name for g in r2.list_games()] == ["CK3"]
    r2.close()


def test_old_world_db_is_refused(make_old_db):
    # 世界状态：上一代工具的旧库（有 local_timeupdated 列、没有
    # verdict_log 表、user_version=1）——两代工具的 v1 靠标记表才分得清
    path, conn = make_old_db()
    conn.close()
    with pytest.raises(ValueError, match="旧账本"):
        SQLiteRepository(path)


def test_old_world_v3_refused_as_unreadable(tmp_path):
    # 世界状态：旧世系 v3 库（版本号 3 走"读不懂"分支）
    p = tmp_path / "old3.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "PRAGMA user_version = 3;"
        "CREATE TABLE games (app_id INTEGER PRIMARY KEY);")
    conn.close()
    with pytest.raises(ValueError, match="读不懂"):
        SQLiteRepository(p)


def test_newer_version_refused(tmp_path):
    # 世界状态：user_version=99 的库（来自更未来的程序）
    p = tmp_path / "future.db"
    sqlite3.connect(p).execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="读不懂"):
        SQLiteRepository(p)


def test_not_a_database_file(tmp_path):
    # 世界状态：纯文本文件冒充数据库
    p = tmp_path / "fake.db"
    p.write_text("这不是数据库", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteRepository(p)


# ============ games：增改查删 + 删除保护 + 盘点与深删 ============

def test_game_crud(repo):
    # 世界状态：无档案 → 建档 → 改名 → 查无 → 删
    _add_game(repo, 1158310, "CK3")
    assert repo.get_game(1158310).name == "CK3"
    repo.update_game(1158310, name="十字军之王3")
    repo.update_game(1158310)  # 全 None = 无事可做，不报错
    assert repo.get_game(1158310).name == "十字军之王3"
    assert repo.get_game(1) is None          # 查不到返回 None
    with pytest.raises(ValueError):          # 改不存在的档案
        repo.update_game(1, name="x")
    repo.delete_game(1158310)                # 空档案可以删
    assert repo.list_games() == []


def test_delete_game_blocked_by_mods(repo):
    # 世界状态：1 档案 + 名下 1 个 mod
    _add_game(repo)
    _add_mod(repo, 101)
    # 断言：RESTRICT 闸拦下（防误删带 237 个 mod 的档案）
    with pytest.raises(sqlite3.IntegrityError):
        repo.delete_game(GAME)


def test_deletion_summary_and_deep_delete(repo):
    # 世界状态：1 档案；名下 1 个已下载 mod（1 份备份登记 1024 字节）
    # + 1 个待下载 mod；另有一条挂在"账外编号 999"上的待确认判决
    # （game_id 指向本档案——快速命令下载、还没入账的东西）
    _add_game(repo)
    _add_mod(repo, 101, status="downloaded")
    repo.add_backup(101, "bk_101", 1024, 100)
    _add_mod(repo, 102)
    repo.record_verdicts([
        {"mod_id": 999, "kind": "success", "game_id": GAME,
         "version_written": 5, "source": "verified", "title": "账外"},
    ])
    s = repo.game_deletion_summary(GAME)
    assert (s.mod_total, s.backup_count, s.backup_bytes) == (2, 1, 1024)
    # 深删：账内账外的判决行一并清（verdict_log 自带 game_id 列）
    repo.delete_game_deep(GAME)
    assert repo.list_games() == []
    assert repo.get_mod(101) is None
    assert repo.list_backups_overview() == []
    assert repo.pending_confirmations() == []


# ============ mods：万能查询的筛选矩阵 ============

def _seed_list_world(repo):
    """世界状态：3 个 mod——
    m1：已下载、确认版本 100、local_size=0（退 API 500）、
        标签[中文,UI]、特别关注、远端 200、红标记、备注"喜欢"
    m2：已下载、无确认版本（版本未知）、file_size=800、标签[中文]
    m3：待下载、远端 NULL、标签[UI]"""
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", title="帧率增强",
             confirmed_version=100, confirmed_source="verified",
             confirmed_at=111, local_size=0, file_size=500,
             tags=["中文", "UI"], is_special=True, time_updated=200,
             color_tag="#ff0000", note="喜欢")
    _add_mod(repo, 2, status="downloaded", title="城市扩建",
             file_size=800, tags=["中文"])
    _add_mod(repo, 3, title="UI 补丁", tags=["UI"])


def test_list_filters_basic(repo):
    _seed_list_world(repo)
    got = lambda **kw: [m.mod_id for m in repo.list_mods(GAME, **kw)]
    assert got(status="downloaded") == [1, 2]
    assert got(special_only=True) == [1]
    assert got(color_tag="#ff0000") == [1]
    assert got(search="帧率") == [1]


def test_list_search_escapes_like_wildcards(repo):
    # 世界状态：标题 "ab" 的 1 个 mod
    # 断言：搜 "a%" 不命中——% 被当字面量；不转义就会命中
    _add_game(repo)
    _add_mod(repo, 1, title="ab")
    assert repo.list_mods(GAME, search="a%") == []


def test_list_size_semantics(repo):
    _seed_list_world(repo)
    got = lambda **kw: [m.mod_id for m in repo.list_mods(GAME, **kw)]
    # m1 本地大小 0 → 退 API file_size=500：与 m2(800) 一起命中下限 100
    assert got(size_min=100) == [1, 2]
    assert got(size_max=600) == [1]
    # 大小未知（两处都空）的 m3 不落在任何区间——未知 ≠ 0
    assert got(size_min=1, size_max=99999) == [1, 2]


def test_list_updated_range_excludes_unknown(repo):
    _seed_list_world(repo)
    # 远端时间 NULL（从没查过）的条目不命中任何时间范围
    got = [m.mod_id for m in repo.list_mods(
        GAME, updated_from=100, updated_to=300)]
    assert got == [1]


def test_list_tags_all_limit_after_filter(repo):
    _seed_list_world(repo)
    # 标签筛在 SQL 之外做，LIMIT 必须筛完再切——
    # 标签[中文] 命中 2 条，limit=1 → 恰好 1 条而不是 0 条
    assert len(repo.list_mods(GAME, tags_all=["中文"], limit=1)) == 1


def test_list_title_note_contains(repo):
    _seed_list_world(repo)
    got = lambda **kw: [m.mod_id for m in repo.list_mods(GAME, **kw)]
    assert got(title_contains="扩建") == [2]
    assert got(note_contains="喜欢") == [1]


def test_list_order_whitelist_blocks_injection(repo):
    _seed_list_world(repo)
    # ORDER BY 无法参数绑定，靠白名单防注入：白名单外的一律拒绝，
    # 表也毫发无损
    with pytest.raises(ValueError):
        repo.list_mods(GAME, order_by="mod_id; DROP TABLE mods")
    rows = repo.list_mods(GAME, order_by="confirmed_version DESC")
    assert rows[0].mod_id == 1   # 有确认版本的排最前


# ============ 检测回写与检测时间 ============

def test_api_metadata_none_keeps_and_empty_clears(repo):
    # 世界状态：1 个 mod，标题"旧名"、标签[旧]
    _add_game(repo)
    _add_mod(repo, 1, title="旧名", tags=["旧"])
    repo.update_api_metadata(1, title="新名", tags=[])
    m = repo.get_mod(1)
    assert m.title == "新名"
    assert m.tags == []            # 空列表 = 清空；None 才是不修改
    assert m.time_updated is None  # 没传的字段一字不动
    repo.update_api_metadata(1, time_updated=555)
    assert repo.get_mod(1).time_updated == 555
    with pytest.raises(ValueError):
        repo.update_api_metadata(999, title="x")


def test_touch_checked_batch(repo):
    _add_game(repo)
    _add_mod(repo, 1)
    _add_mod(repo, 2)
    repo.touch_checked([1, 2], checked_at=777)
    assert repo.get_mod(1).last_checked_at == 777
    assert repo.get_mod(2).last_checked_at == 777


# ============ 状态迁移：软删除 / 恢复 / 失效归档 / 关联替换 ============

def test_mark_deleted_and_restore(repo):
    # 世界状态：1 个已下载、确认版本 100 的 mod
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111, title="t")
    repo.mark_deleted(1, {"title": "t", "confirmed_version": 100})
    m = repo.get_mod(1)
    assert m.status == "deleted" and m.deleted_at is not None
    assert m.deleted_last_state == {"title": "t", "confirmed_version": 100}
    # 恢复：有确认版本 → 回"已下载"，软删标记清掉
    repo.mark_restored(1)
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.deleted_at is None
    assert m.deleted_last_state is None


def test_restore_without_version_goes_tracked(repo):
    # 世界状态：已删除、没有确认版本（版本未知的东西被软删过）
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded")
    repo.mark_deleted(1, {})
    repo.mark_restored(1)
    # 恢复口径：版本说不清 → 回"待下载"，等确认门重新给版本
    assert repo.get_mod(1).status == "tracked"


def test_restore_rejects_non_deleted(repo):
    _add_game(repo)
    _add_mod(repo, 1)
    with pytest.raises(ValueError):
        repo.mark_restored(1)


def test_mark_failed_snapshots_last_state(repo):
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111, title="t")
    repo.mark_failed(1, "文件夹缺失")
    assert repo.get_mod(1).status == "failed"
    rows = repo.list_failed(GAME)
    assert len(rows) == 1 and rows[0].reason == "文件夹缺失"
    # 末态快照按新口径：记确认版本，不记 acf 产物
    assert rows[0].last_known_state["confirmed_version"] == 100


def test_replace_failed_mod_transfers_and_relinks(repo):
    # 世界状态：旧 mod（备注"老备注"、1 条快照）已失效归档；
    # 新 mod（无备注）已入账
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111, note="老备注")
    repo.add_snapshot(1, time_updated=90)
    repo.mark_failed(1, "reason")
    _add_mod(repo, 2)
    repo.replace_failed_mod(1, 2)
    assert repo.get_mod(2).note == "老备注"          # 新值为空才覆盖
    assert repo.list_failed(GAME)[0].replaced_by == 2  # 证据链接上
    assert [s.time_updated for s in repo.list_snapshots(2)] == [90]
    assert repo.list_snapshots(1) == []


# ============ 彻底清账 ============

def test_purge_requires_backup_decision(repo):
    # 世界状态：1 个已下载 mod 带 1 份备份登记
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111)
    repo.add_backup(1, "bk", 10, 100)
    # 断言：没交代备份去留 → 拒绝（RESTRICT 闸的显式版）
    with pytest.raises(ValueError, match="备份"):
        repo.purge_mod(1)


def test_purge_clears_verdicts_and_registers_blacklist(repo):
    # 世界状态：1 个已下载 mod：1 条已确认判决 + 1 条待确认判决
    # + 1 条简介翻译缓存；无备份
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111)
    repo.record_verdicts([{"mod_id": 1, "kind": "success",
                           "game_id": GAME, "version_written": 100,
                           "source": "verified"}])
    repo.confirm_items([1])
    repo.record_verdicts([{"mod_id": 1, "kind": "success",
                           "game_id": GAME, "version_written": 120,
                           "source": "verified"}])
    repo.save_translation(1, source_hash="h", target_lang="zh",
                          engine=None, text_translated="译")
    n = repo.purge_mod(1)
    assert n == 0
    # 账、判决史（含待确认）、翻译缓存全清；黑名单自动登记
    assert repo.get_mod(1) is None
    assert repo.list_verdicts(1) == []
    assert repo.pending_confirmations() == []
    assert repo.get_translation(1, "zh") is None   # 翻译缓存 CASCADE
    assert repo.is_purged(1)


# ============ 快照滚动 / 翻译缓存 ============

def test_snapshot_rolling_keep(repo):
    # 夹具 snapshot_keep=3：连加 5 条远端观测只留最新 3 条
    _add_game(repo)
    _add_mod(repo, 1)
    for i in range(5):
        repo.add_snapshot(1, time_updated=i, snapshot_at=1000 + i)
    rows = repo.list_snapshots(1)
    assert [r.time_updated for r in rows] == [4, 3, 2]  # 新→旧


def test_translation_cache_replace_semantics(repo):
    _add_game(repo)
    _add_mod(repo, 1)
    repo.save_translation(1, source_hash="h1", target_lang="zh",
                          engine="deepl", text_translated="第一版")
    repo.save_translation(1, source_hash="h2", target_lang="zh",
                          engine=None, text_translated="第二版")
    t = repo.get_translation(1, "zh")
    # 单 mod 单条当前译文：新的覆盖旧的，无历史
    assert t.text_translated == "第二版" and t.source_hash == "h2"
    assert repo.get_translation(1, "en") is None


def test_translation_requires_existing_mod(repo):
    _add_game(repo)
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_translation(999, source_hash="h", target_lang="zh",
                              engine=None, text_translated="x")


# ============ 备份登记 ============

def test_backup_basics(repo):
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", confirmed_version=100,
             confirmed_source="verified", confirmed_at=111)
    b = repo.add_backup(1, "D:/bk/1_v100", 2048, 100, note="手动")
    assert b.id is not None and b.created_at is not None
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE：防重复登记
        repo.add_backup(1, "D:/bk/1_v100", 2048, 100)
    repo.set_pinned(b.id, True)
    assert repo.list_backups(1, include_pinned=False) == []
    assert repo.list_backups(1)[0].pinned is True
    assert repo.sum_backup_bytes() == 2048
    repo.delete_backup_record(b.id)
    assert repo.list_backups(1) == []
    with pytest.raises(ValueError):              # 删不存在的记录
        repo.delete_backup_record(b.id)
    with pytest.raises(ValueError):              # 只许挂在账内 mod 名下
        repo.add_backup(999, "D:/bk/999", 1, 1)


def test_db_backup_keeps_latest_three(repo, tmp_path):
    # 数据库自身备份的滚动保留：连备 4 次只留最新 3 份
    dest = tmp_path / "dbbk"
    for _ in range(4):
        repo.backup_to(dest)
    assert len(list(dest.glob("*.db"))) == 3


# ============ 操作日志 / 特殊提醒 / 黑名单 / 依赖边 ============

def test_operations_log_roundtrip(repo):
    op = repo.add_operation("steamcmd +download ...")
    repo.finish_operation(op, error_count=1, result="error")
    rows = repo.list_operations()
    assert rows[0].id == op and rows[0].result == "error"
    with pytest.raises(ValueError):
        repo.finish_operation(999)


def test_alerts_last_and_order(repo):
    _add_game(repo)
    _add_mod(repo, 1)
    a1 = repo.add_alert(1, 100)
    a2 = repo.add_alert(1, 200, diff_seconds=100, was_downloaded=True)
    assert repo.get_last_alert(1).id == a2.id
    assert [x.id for x in repo.list_alerts(1)] == [a2.id, a1.id]
    with pytest.raises(ValueError):
        repo.add_alert(999, 1)


def test_purged_filters_in_chunks(repo):
    # 世界状态：黑名单里有 2 个编号；查询传入 600 个编号（超分片 500）
    _add_game(repo)
    repo.add_purged(1, GAME)
    repo.add_purged(501, GAME)
    assert repo.filter_purged(range(1, 601)) == {1, 501}
    repo.remove_purged(1)
    assert repo.is_purged(1) is False
    with pytest.raises(ValueError):   # 白点了要说清
        repo.remove_purged(1)


def test_dependencies_edges(repo):
    _add_game(repo)
    _add_mod(repo, 1)
    # 去重保序；999 不在账本是常态（那正是"缺依赖"要报的）
    repo.replace_dependencies(1, [2, 3, 2, 999])
    assert repo.list_dependencies(1) == [2, 3, 999]
    assert repo.list_dependents(2) == [1]
    assert repo.latest_dependency_fetch() is not None
    repo.replace_dependencies(1, [])   # 空列表 = 清空（真无依赖）
    assert repo.list_dependencies(1) == []
    assert repo.latest_dependency_fetch() is None
    with pytest.raises(ValueError):    # 幽灵 mod 不许记依赖
        repo.replace_dependencies(999, [1])


# ============ 账本导入导出 ============
def _build_full_world(repo):
    """搭一个 8 张 ledger 表都有货的世界，返回 export_all 载荷。"""
    _add_game(repo)
    _add_mod(repo, 1, status="downloaded", title="t",
             confirmed_version=100, confirmed_source="verified",
             confirmed_at=111, tags=["中文"], is_special=True)
    _add_mod(repo, 2)
    repo.add_snapshot(1, time_updated=90, snapshot_at=1000)
    b = repo.add_backup(1, "bk1", 10, 100)
    op = repo.add_operation("cmd", backup_id=b.id)
    repo.finish_operation(op, result="success")
    repo.record_verdicts([{"mod_id": 1, "kind": "success",
                           "game_id": GAME, "version_written": 100,
                           "source": "verified"}])
    repo.confirm_items([1])   # 判决行当场确认（本轮补）：导入端
    # "待确认队列已清空"的断言才成立
    repo.mark_failed(2, "归档原因")
    repo.add_alert(1, 100)
    return repo.export_all()



def test_export_import_roundtrip(tmp_path):
    src = SQLiteRepository(tmp_path / "a.db")
    payload = _build_full_world(src)
    src.close()
    dst = SQLiteRepository(tmp_path / "b.db")
    dst.import_all(payload)
    # 档案、mod（含确认三件套与自然类型还原）、判决、归档、提醒全到位
    assert dst.get_game(GAME).name == "RimWorld"
    m = dst.get_mod(1)
    assert (m.confirmed_version, m.confirmed_source) == (100, "verified")
    assert m.tags == ["中文"] and m.is_special is True
    assert dst.pending_confirmations() == []   # 判决行已带确认戳
    assert len(dst.list_verdicts(1)) == 1
    assert dst.list_failed(GAME)[0].reason == "归档原因"
    assert dst.get_last_alert(1) is not None
    assert dst.sum_backup_bytes() == 10
    dst.close()


def test_import_guards(tmp_path):
    # 守门三道：未来版本 / 缺版本号 / 不认识的表——全部显式爆炸
    src = SQLiteRepository(tmp_path / "a.db")
    _add_game(src)
    payload = src.export_all()
    src.close()
    dst = SQLiteRepository(tmp_path / "b.db")
    with pytest.raises(ValueError):
        dst.import_all({**payload, "user_version": 99})
    with pytest.raises(ValueError):
        dst.import_all({"tables": {}})
    with pytest.raises(ValueError):
        dst.import_all({"user_version": 1, "tables": {"神秘表": []}})
    dst.close()

def test_import_rolls_back_on_bad_row(tmp_path):
    # 世界状态：目标库已有一份完整账（1 档案 + 1 mod）；新载荷里
    # 混进一条 game_id 不存在的 mod 行
    src = SQLiteRepository(tmp_path / "a.db")
    _add_game(src)
    _add_mod(src, 1)   # 回滚断言的对象得先存在（本轮补）
    payload = src.export_all()
    src.close()
    dst = SQLiteRepository(tmp_path / "b.db")
    dst.import_all(payload)
    bad = {"user_version": 1,
           "tables": {**payload["tables"],
                      "mods": [{"mod_id": 777, "game_id": 424242,
                                "status": "tracked"}]}}
    with pytest.raises(sqlite3.IntegrityError):
        dst.import_all(bad)
    # 整体回滚：原有账目原样，绝不留半截账
    assert dst.get_mod(1) is not None
    assert dst.get_mod(777) is None
    dst.close()
