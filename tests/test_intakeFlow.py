"""纳入已有 mod（客户端订阅）· 分类与落库测试"""
"""与 test_batchSpecial 同款自备夹具：临时库自动建表，不依赖 conftest。"""

import pytest

from core.localScanner import LocalItem
from core.models import Game, Mod
from core.sqliteRepository import SQLiteRepository
from workflows.intakeFlow import (ClientIntakePlan, apply_intake,
                                  classify_client_items)

CK3_APP = 1158310


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "t.db")
    yield r
    r.close()


@pytest.fixture()
def ck3(repo):
    repo.add_game(CK3_APP, "十字军之王3", rf"E:\dl\{CK3_APP}")
    return repo.get_game(CK3_APP)


def _item(mid: int, ts: int) -> LocalItem:
    return LocalItem(mod_id=mid, timeupdated=ts, size=1024, manifest=f"m{mid}")


def _existing(repo, app_id):
    return {m.mod_id: m for m in repo.list_mods(app_id)}


def test_all_new_registers_as_tracked(repo, ck3):
    items = [_item(100, 1700000000), _item(200, 1700000100)]
    plan = classify_client_items(items, _existing(repo, CK3_APP), app_id=CK3_APP)
    assert [i.mod_id for i in plan.to_register] == [100, 200]
    rep = apply_intake(repo, plan)
    assert rep.registered == 2 and rep.hints_filled == 0
    m = repo.get_mod(100)
    assert m.status == "tracked"
    assert m.time_updated == 1700000000   # 版本线索已带
    assert m.local_timeupdated is None    # 客户端那份不算"本地"（决策 21 口径）


def test_five_buckets(repo, ck3):
    repo.add_mod(Mod(mod_id=300, game_id=CK3_APP))                    # tracked 无线索
    repo.add_mod(Mod(mod_id=400, game_id=CK3_APP, time_updated=111))  # tracked 有线索
    repo.add_mod(Mod(mod_id=500, game_id=CK3_APP, status="downloaded",
                     local_timeupdated=111))
    repo.add_mod(Mod(mod_id=600, game_id=CK3_APP))
    repo.mark_deleted(600, {"title": None})
    items = [_item(300, 222), _item(400, 333), _item(500, 444),
             _item(600, 555), _item(700, 666)]
    plan = classify_client_items(items, _existing(repo, CK3_APP), app_id=CK3_APP)
    assert [i.mod_id for i in plan.to_register] == [700]
    assert [i.mod_id for i in plan.to_fill_hint] == [300]
    assert plan.already_tracked == [400]
    assert plan.already_downloaded == [500]
    assert plan.inactive == [(600, "deleted")]


def test_fill_hint_only_when_missing(repo, ck3):
    """只补缺：已有线索的 tracked 不被客户端缓存值覆盖。"""
    repo.add_mod(Mod(mod_id=400, game_id=CK3_APP, time_updated=999))
    plan = classify_client_items([_item(400, 111)], _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    assert plan.to_fill_hint == []
    assert plan.already_tracked == [400]
    apply_intake(repo, plan)                       # 空动作计划也安全
    assert repo.get_mod(400).time_updated == 999   # 未被覆盖


def test_apply_roundtrip(repo, ck3):
    """入库 + 补线索后再对一遍：全进"无事可做"桶，重复导入幂等。"""
    repo.add_mod(Mod(mod_id=300, game_id=CK3_APP))
    items = [_item(300, 222), _item(700, 666)]
    plan = classify_client_items(items, _existing(repo, CK3_APP), app_id=CK3_APP)
    rep = apply_intake(repo, plan)
    assert rep.registered == 1 and rep.hints_filled == 1
    plan2 = classify_client_items(items, _existing(repo, CK3_APP), app_id=CK3_APP)
    assert plan2.to_register == [] and plan2.to_fill_hint == []
    assert sorted(plan2.already_tracked) == [300, 700]


def test_no_resurrect(repo, ck3):
    repo.add_mod(Mod(mod_id=600, game_id=CK3_APP))
    repo.mark_deleted(600, {"title": None})
    plan = classify_client_items([_item(600, 555)], _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    assert plan.inactive == [(600, "deleted")]
    apply_intake(repo, plan)
    assert repo.get_mod(600).status == "deleted"   # 不复活


def test_empty_items(repo, ck3):
    plan = classify_client_items([], _existing(repo, CK3_APP), app_id=CK3_APP)
    rep = apply_intake(repo, plan)
    assert rep.registered == 0 and rep.hints_filled == 0
