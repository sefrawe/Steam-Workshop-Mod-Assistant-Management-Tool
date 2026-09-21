"""localScanner 测试
"""
"""
三层覆盖：路径定位（tmp_path 假文件）→ acf 解析（仓库内合成样本 +
本机真实 fixtures）→ 计划与落库（临时 SQLite 库全链路 + 原子性）。

真实 acf 不入库（gitignore），缺失时相关用例自动 skip，合成样本兜底。
"""
from pathlib import Path

import pytest

from core import localScanner as ls
from core.models import Mod
from core.sqliteRepository import SQLiteRepository

SAMPLES = Path(__file__).parent / "samples"
FIXTURES = Path(__file__).parent / "fixtures"
MINI = SAMPLES / "appworkshop_mini.acf"
CK3 = FIXTURES / "appworkshop_1158310.acf"
OTHER = FIXTURES / "appworkshop_3117820.acf"


# ---------- locate_acf ----------
# 决策 21⑤：acf 定位绑定 steamcmd 目录布局，三种填写口径都认：
#   填 steamcmd 根   → <根>\steamapps\workshop\appworkshop_<appid>.acf
#   填 steamapps 层  → <层>\workshop\appworkshop_<appid>.acf
#   填 workshop 层   → <层>\appworkshop_<appid>.acf
# （程序内部自动推导传的总是 steamcmd 根，后两种是手工填写时的容错。）

def test_locate_from_steamcmd_root(tmp_path):
    f = tmp_path / "steamapps" / "workshop" / "appworkshop_1158310.acf"
    f.parent.mkdir(parents=True)
    f.write_text('"AppWorkshop" {}', encoding="utf-8")
    assert ls.locate_acf(str(tmp_path), 1158310) == f


def test_locate_from_steamapps_dir(tmp_path):
    f = tmp_path / "workshop" / "appworkshop_294100.acf"
    f.parent.mkdir(parents=True)
    f.write_text('"AppWorkshop" {}', encoding="utf-8")
    assert ls.locate_acf(str(tmp_path), 294100) == f


def test_locate_from_workshop_dir(tmp_path):
    f = tmp_path / "appworkshop_294100.acf"
    f.write_text('"AppWorkshop" {}', encoding="utf-8")
    assert ls.locate_acf(str(tmp_path), 294100) == f


def test_locate_missing_returns_none(tmp_path):
    assert ls.locate_acf(str(tmp_path), 1158310) is None


def test_locate_empty_or_quoted_returns_none():
    assert ls.locate_acf("", 1158310) is None
    assert ls.locate_acf(' "" ', 1158310) is None


# ---------- scan_acf（合成样本）----------

def test_scan_mini_items():
    r = ls.scan_acf(MINI)
    assert [i.mod_id for i in r.items] == [1000000001, 1000000002, 1000000004]
    a = r.items[0]
    assert (a.timeupdated, a.size, a.manifest) == (
        1700000100, 4096, "8715384612056780827")
    assert r.items[1].size is None and r.items[1].manifest is None
    assert r.items[2].manifest is None  # 空串 manifest 视为没有


def test_scan_mini_skipped():
    r = ls.scan_acf(MINI)
    # 跳过顺序与清单（id 保持 acf 出现顺序）
    assert [k for k, _ in r.skipped] == [
        "1000000003", "not_a_number", "1000000006"]
    # 原因按前缀断言：不锁全文措辞，只锁语义类别
    reasons = dict(r.skipped)
    assert reasons["1000000003"].startswith("缺")
    assert reasons["not_a_number"].startswith("编号")
    assert reasons["1000000006"].startswith("条目")


def test_scan_mini_details_section_ignored():
    r = ls.scan_acf(MINI)
    # WorkshopItemDetails 里的 latest_* 缓存不得泄漏成条目数据
    assert all(i.manifest != "9999999999999999999" for i in r.items)


# ---------- scan_acf（错误路径）----------

def test_scan_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        ls.scan_acf(tmp_path / "nope.acf")


def test_scan_truncated_file_raises(tmp_path):
    f = tmp_path / "bad.acf"
    f.write_text('"AppWorkshop" { "appid" ', encoding="utf-8")
    with pytest.raises(ValueError):
        ls.scan_acf(f)


def test_scan_wrong_root_raises(tmp_path):
    f = tmp_path / "appmanifest.acf"
    f.write_text('"AppState" { "appid" "1158310" }', encoding="utf-8")
    with pytest.raises(ValueError):
        ls.scan_acf(f)


# ---------- scan_acf（真实 fixtures，本机自测）----------

@pytest.mark.skipif(not CK3.exists(), reason="真实 acf 属本机样本（已 gitignore）")
def test_scan_real_ck3():
    r = ls.scan_acf(CK3)
    assert r.skipped == []
    assert len(r.items) > 100
    by_id = {i.mod_id: i for i in r.items}
    a = by_id[2216670956]
    assert (a.timeupdated, a.size, a.manifest) == (
        1786409687, 4091676355, "8715384612056780827")
    assert by_id[2220098919].manifest == "7530876539601303237"
    assert all(i.timeupdated > 0 for i in r.items)


@pytest.mark.skipif(not OTHER.exists(), reason="真实 acf 属本机样本（已 gitignore）")
def test_scan_real_second_game():
    r = ls.scan_acf(OTHER)
    assert r.skipped == []
    a = {i.mod_id: i for i in r.items}[3485191385]
    assert (a.timeupdated, a.size, a.manifest) == (
        1775339505, 491232, "7844953786737163906")


# ---------- diff_plan ----------

def _item(mid, tu=1700000000):
    return ls.LocalItem(mod_id=mid, timeupdated=tu, size=1, manifest="1")


def test_plan_new_id_goes_to_inserts():
    plan = ls.diff_plan([_item(1)], {}, game_id=99)
    assert [p.item.mod_id for p in plan.inserts] == [1]
    assert plan.updates == [] and plan.game_id == 99


def test_plan_tracked_transitions():
    plan = ls.diff_plan([_item(1)], {1: "tracked"}, game_id=99)
    assert plan.updates[0].to_downloaded is True and plan.inserts == []


def test_plan_downloaded_no_transition():
    plan = ls.diff_plan([_item(1)], {1: "downloaded"}, game_id=99)
    assert plan.updates[0].to_downloaded is False


def test_plan_deleted_failed_backfill_only():
    plan = ls.diff_plan([_item(1), _item(2)],
                        {1: "deleted", 2: "failed"}, game_id=99)
    assert all(not u.to_downloaded for u in plan.updates)
    assert plan.inserts == []


def test_plan_sorted_regardless_of_input_order():
    plan = ls.diff_plan([_item(3), _item(1), _item(2)], {}, game_id=99)
    assert [p.item.mod_id for p in plan.inserts] == [1, 2, 3]


def test_plan_empty():
    plan = ls.diff_plan([], {}, game_id=99)
    assert plan.updates == [] and plan.inserts == []


# ---------- apply ----------

@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "t.db")
    r.add_game(1158310, "CK3", str(tmp_path / "dl"))
    yield r
    r.close()


def _mkitem(mid, tu, size=10, manifest="m1"):
    return ls.LocalItem(mod_id=mid, timeupdated=tu, size=size, manifest=manifest)


def test_apply_full_flow(repo):
    repo.add_mod(Mod(mod_id=100, game_id=1158310, status="tracked"))
    repo.add_mod(Mod(mod_id=200, game_id=1158310, status="deleted"))
    items = [_mkitem(100, 111), _mkitem(200, 222), _mkitem(300, 333)]
    status_map = {m.mod_id: m.status for m in repo.list_mods(1158310)}
    rep = ls.apply(repo, ls.diff_plan(items, status_map, game_id=1158310))
    assert (rep.updated, rep.transitioned, rep.inserted) == (2, 1, 1)
    m100 = repo.get_mod(100)
    assert m100.status == "downloaded"
    assert (m100.local_timeupdated, m100.local_size, m100.manifest) == (111, 10, "m1")
    m200 = repo.get_mod(200)
    assert m200.status == "deleted"  # 只补证据，不复活
    assert m200.local_timeupdated == 222
    m300 = repo.get_mod(300)
    assert m300.status == "downloaded"
    assert m300.url == ("https://steamcommunity.com/sharedfiles/"
                        "filedetails/?id=300")
    assert m300.title is None
    assert m300.first_tracked_at is not None  # schema DEFAULT 生效


def test_apply_atomic_rollback(repo):
    repo.add_mod(Mod(mod_id=100, game_id=1158310, status="tracked"))
    plan = ls.ScanPlan(
        game_id=1158310,
        updates=[
            ls.PlannedUpdate(item=_mkitem(100, 111), to_downloaded=True),
            ls.PlannedUpdate(item=_mkitem(999, 222), to_downloaded=False),  # 不存在
        ],
        inserts=[ls.PlannedInsert(item=_mkitem(300, 333))],
    )
    with pytest.raises(ValueError):
        ls.apply(repo, plan)
    m100 = repo.get_mod(100)
    # 前一条成功的回填也被整体回滚
    assert m100.status == "tracked" and m100.local_timeupdated is None
    assert repo.get_mod(300) is None


def test_apply_idempotent(repo):
    repo.add_mod(Mod(mod_id=100, game_id=1158310, status="tracked"))
    item = _mkitem(100, 111, 10, "m1")
    first_map = {m.mod_id: m.status for m in repo.list_mods(1158310)}
    plan = ls.diff_plan([item], first_map, game_id=1158310)
    ls.apply(repo, plan)
    first = repo.get_mod(100)
    ls.apply(repo, plan)  # 同一计划重放，库中数据必须纹丝不动
    again = repo.get_mod(100)
    assert (again.status, again.local_timeupdated, again.local_size,
            again.manifest) == (
        first.status, first.local_timeupdated, first.local_size, first.manifest)
    # 重新对表：已 downloaded，不再出现跃迁
    plan2 = ls.diff_plan([item],
                         {m.mod_id: m.status for m in repo.list_mods(1158310)},
                         game_id=1158310)
    assert plan2.updates[0].to_downloaded is False


def test_apply_empty_plan(repo):
    rep = ls.apply(repo, ls.ScanPlan(game_id=1158310, updates=[], inserts=[]))
    assert (rep.updated, rep.transitioned, rep.inserted) == (0, 0, 0)
