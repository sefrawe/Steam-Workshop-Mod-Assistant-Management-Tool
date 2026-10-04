"""list_game_verdicts 契约测试"""
"""按档案跨 mod 判决史：过滤 / 排序 / limit / 含已确认行。
文件名必须 test_ 开头（pytest 收集规则）——首版误名
list_game_verdicts.py 导致静默零收集，78≠80 的差额就是它。"""
from core.sqliteRepository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(tmp_path / "t.db", snapshot_keep=3)


def test_filters_by_game_and_orders_desc(tmp_path):
    repo = _repo(tmp_path)
    # record_verdicts 统一盖 now 时间戳、不收 occurred_at——
    # 倒序断言靠 id 平票（后插的行 id 大排前），与实现一致
    repo.record_verdicts([
        {"mod_id": 1, "kind": "success", "game_id": 100},
        {"mod_id": 2, "kind": "timeout", "game_id": 100},
        {"mod_id": 3, "kind": "success", "game_id": 200},
    ])
    rows = repo.list_game_verdicts(100)
    assert [r.mod_id for r in rows] == [2, 1]     # 新→旧
    assert all(r.game_id == 100 for r in rows)


def test_includes_confirmed_and_respects_limit(tmp_path):
    repo = _repo(tmp_path)
    # confirm_items 按 D39 从判决行建 mods 行——mods.game_id 有外键
    # 指向 games，游戏行必须先在。真实流程里判决必来自已存在的档案，
    # 生产路径撞不到这个 FK；测试造数要补齐这个前提
    repo.add_game(100, "测试游戏", "D:/dl")

    repo.record_verdicts([
        {"mod_id": 1, "kind": "success", "game_id": 100,
         "version_written": 555, "source": "verified"},
        {"mod_id": 1, "kind": "success", "game_id": 100,
         "version_written": 777, "source": "verified"},
    ])
    assert repo.confirm_items([1]) == 1
    rows = repo.list_game_verdicts(100)
    assert len(rows) == 2                          # 史含已确认行
    assert len(repo.pending_confirmations(100)) == 0   # 队列已清空
    assert repo.list_game_verdicts(100, limit=1)[0].version_written == 777
