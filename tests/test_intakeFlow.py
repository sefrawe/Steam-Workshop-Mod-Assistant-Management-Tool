"""纳入已有 mod（客户端订阅）· 分类与落库测试
"""
"""
V2 判决制四桶版：V1 的「待补版本线索」桶已砍（拍板 3）——客户端
acf 的时间没资格自动写进任何版本字段（版本只认确认门背书），
tracked 一律跳过、入库一个版本字段都不碰。与 test_batchSpecial
同款自备夹具：临时库自动建表，不依赖 conftest。
"""
import pytest

from core.localScanner import LocalItem
from core.models import Game, Mod
from core.sqliteRepository import SQLiteRepository
from workflows.intakeFlow import apply_intake, classify_client_items

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
    plan = classify_client_items(items, _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    assert [i.mod_id for i in plan.to_register] == [100, 200]
    rep = apply_intake(repo, plan)
    assert rep.registered == 2
    m = repo.get_mod(100)
    assert m.status == "tracked"
    assert m.game_id == CK3_APP
    assert m.time_updated is None  # 版本字段一个不碰（拍板 3：只等确认门）


def test_four_buckets(repo, ck3):
    # 注意：inactive 断言假设软删除落的状态码是 "deleted"——
    # 若与 V2 库实际码不符 pytest 会红，届时以实际码单行改
    repo.add_mod(Mod(mod_id=300, game_id=CK3_APP))            # tracked
    repo.add_mod(Mod(mod_id=400, game_id=CK3_APP))            # tracked
    repo.add_mod(Mod(mod_id=500, game_id=CK3_APP, status="downloaded"))
    repo.add_mod(Mod(mod_id=600, game_id=CK3_APP))
    repo.mark_deleted(600, {"title": None})
    items = [_item(300, 222), _item(400, 333), _item(500, 444),
             _item(600, 555), _item(700, 666)]
    plan = classify_client_items(items, _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    assert [i.mod_id for i in plan.to_register] == [700]
    assert plan.already_tracked == [300, 400]      # tracked 一律跳过（不分有无版本）
    assert plan.already_downloaded == [500]
    assert plan.inactive == [(600, "deleted")]


def test_apply_roundtrip(repo, ck3):
    """入库后再对一遍：全进跳过桶，重复纳入幂等。"""
    repo.add_mod(Mod(mod_id=300, game_id=CK3_APP))
    items = [_item(300, 222), _item(700, 666)]
    plan = classify_client_items(items, _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    rep = apply_intake(repo, plan)
    assert rep.registered == 1
    plan2 = classify_client_items(items, _existing(repo, CK3_APP),
                                  app_id=CK3_APP)
    assert plan2.to_register == []
    assert sorted(plan2.already_tracked) == [300, 700]


def test_no_resurrect(repo, ck3):
    repo.add_mod(Mod(mod_id=600, game_id=CK3_APP))
    repo.mark_deleted(600, {"title": None})
    plan = classify_client_items([_item(600, 555)], _existing(repo, CK3_APP),
                                 app_id=CK3_APP)
    assert plan.inactive == [(600, "deleted")]
    apply_intake(repo, plan)
    assert repo.get_mod(600).status == "deleted"  # 不复活


def test_empty_items(repo, ck3):
    plan = classify_client_items([], _existing(repo, CK3_APP), app_id=CK3_APP)
    rep = apply_intake(repo, plan)
    assert rep.registered == 0
