"""批量特别关注·对表分类测试"""
"""classify_special_targets 五桶分类 + 应用后闭环。
夹具自备：SQLiteRepository 对空库自动建表（repo 契约），tmp_path 隔离，
不依赖 conftest。GUI 对话框本体不进 pytest（冒烟覆盖）。"""

import pytest

from core.models import Game, Mod
from core.sqliteRepository import SQLiteRepository
from gui.batchSpecialDialog import classify_special_targets

CK3_APP = 1158310
RIM_APP = 294100


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "t.db")
    yield r
    r.close()


@pytest.fixture()
def ck3(repo):
    repo.add_game(CK3_APP, "十字军之王3", rf"E:\dl\{CK3_APP}")
    return repo.get_game(CK3_APP)


def _add(repo, mid, game_id=CK3_APP, special=False):
    repo.add_mod(Mod(mod_id=mid, game_id=game_id))
    if special:
        repo.set_special(mid, True)


def test_five_buckets(repo, ck3):
    _add(repo, 100)                 # 在库、未关注 → to_set
    _add(repo, 200, special=True)   # 在库、已关注 → already_special
    text = "\n".join([
        "https://steamcommunity.com/sharedfiles/filedetails/?id=100",
        "200",
        "300",                          # 不在库 → missing
        "https://example.com/?id=1",    # 非工坊域名 → invalid
    ])
    r = classify_special_targets(repo, ck3, text)
    assert r.parsed_ids == [100, 200, 300]
    assert r.to_set == [100]
    assert r.already_special == [200]
    assert r.missing == [300]
    assert r.invalid_lines == ["https://example.com/?id=1"]
    assert r.in_other_games == []


def test_other_game_skipped(repo, ck3):
    repo.add_game(RIM_APP, "RimWorld", rf"E:\dl\{RIM_APP}")
    _add(repo, 500, game_id=RIM_APP)
    r = classify_special_targets(repo, ck3, "500")
    assert r.in_other_games == [(500, "RimWorld")]
    assert r.to_set == []


def test_dedup_keeps_order(repo, ck3):
    _add(repo, 100)
    r = classify_special_targets(
        repo, ck3,
        "100\n100\nhttps://steamcommunity.com/sharedfiles/filedetails/?id=100")
    assert r.parsed_ids == [100]
    assert r.to_set == [100]


def test_apply_roundtrip(repo, ck3):
    """应用动作的闭环：分类 → 整批设 → 再分类应全进"已关注"桶。"""
    _add(repo, 100)
    _add(repo, 200)
    r = classify_special_targets(repo, ck3, "100\n200")
    with repo.transaction():
        for mid in r.to_set:
            repo.set_special(mid, True)
    r2 = classify_special_targets(repo, ck3, "100\n200")
    assert r2.to_set == []
    assert sorted(r2.already_special) == [100, 200]


def test_blank_text(repo, ck3):
    r = classify_special_targets(repo, ck3, "   \n  ")
    assert r.parsed_ids == []
    assert r.to_set == []
