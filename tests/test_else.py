"""acfParser 测试"""
"""appworkshop acf 解析：正常提取 / 结构缺失容忍 / 坏文本显式炸。"""
import pytest

from core.acfParser import parse_acf_text, read_workshop_versions, workshop_versions

_ACF = '"AppWorkshop"\n{\n\t"AppID"\t\t"1158310"\n\t"apps"\n\t{\n' \
       '\t\t"1158310"\n\t\t{\n' \
       '\t\t\t"WorkshopItemsInstalled"\n\t\t\t{\n' \
       '\t\t\t\t"111"\t\t"1700000000"\n' \
       '\t\t\t\t"222"\t\t"1700000001"\n\t\t\t}\n' \
       '\t\t\t"WorkshopItemsMetadata"\n\t\t\t{\n' \
       '\t\t\t\t"111"\n\t\t\t\t{\n' \
       '\t\t\t\t\t"timeupdated"\t\t"1600000001"\n\t\t\t\t}\n' \
       '\t\t\t\t"222"\n\t\t\t\t{\n' \
       '\t\t\t\t\t"timeupdated"\t\t"1600000002"\n\t\t\t\t}\n' \
       '\t\t\t}\n\t\t}\n\t}\n}'


def test_parses_metadata_versions():
    assert workshop_versions(parse_acf_text(_ACF)) == {
        111: 1600000001, 222: 1600000002}


def test_missing_structure_returns_empty():
    assert workshop_versions(parse_acf_text('"AppWorkshop"\n{\n}')) == {}


def test_broken_text_raises(tmp_path):
    p = tmp_path / "appworkshop_1.acf"
    p.write_text('"AppWorkshop" { "apps" ', encoding="utf-8")
    with pytest.raises(ValueError):
        read_workshop_versions(p)

"""inventoryFlow 测试"""
"""盘点引擎：候选幂等 / 分类跳过 / 失效清除 / 大小回填——
真 repo（tmp 库）+ tmp 造目录，不 mock。"""
import shutil

from core.inventoryFlow import scan_game
from core.models import Mod
from core.sqliteRepository import SQLiteRepository

_APP = 100


def _repo(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db", snapshot_keep=3)
    repo.add_game(_APP, "测试游戏", str(tmp_path / "dl"),
                  game_mod_dir=str(tmp_path / "mods"))
    return repo


def _make_dir(base, mod_id):
    d = base / str(mod_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "file.bin").write_bytes(b"x" * 10)
    return d


def test_candidates_recorded_once_and_deduped(tmp_path):
    base = tmp_path / "mods"
    _make_dir(base, 111)
    _make_dir(base, 222)
    repo = _repo(tmp_path)
    game = repo.get_game(_APP)

    r1 = scan_game(repo, game)
    assert r1.candidates_new == [111, 222]
    assert r1.versions_from_acf == 0        # 没有 acf → 版本未知候选
    rows = repo.pending_confirmations(_APP)
    assert {v.mod_id for v in rows} == {111, 222}
    assert all(v.kind == "claim" and v.version_written is None
               for v in rows)

    r2 = scan_game(repo, game)              # 再扫：幂等，不重复落
    assert r2.candidates_new == []
    assert r2.candidates_dup == 2
    assert len(repo.pending_confirmations(_APP)) == 2


def test_skips_accounted_deleted_and_purged(tmp_path):
    base = tmp_path / "mods"
    _make_dir(base, 333)   # 已彻底入账 → 只回填大小
    _make_dir(base, 444)   # 黑名单 → 跳过
    _make_dir(base, 555)   # 已删除 → 跳过
    _make_dir(base, 666)   # tracked 未确认 → 候选
    repo = _repo(tmp_path)
    repo.add_mod(Mod(mod_id=333, game_id=_APP, status="downloaded",
                     confirmed_version=7, confirmed_source="verified"))
    repo.add_purged(444, _APP)
    repo.add_mod(Mod(mod_id=555, game_id=_APP, status="deleted"))
    repo.add_mod(Mod(mod_id=666, game_id=_APP, status="tracked"))
    game = repo.get_game(_APP)

    r = scan_game(repo, game)
    assert r.candidates_new == [666]
    assert r.skipped_accounted == 1
    assert r.skipped_other == 2
    sizes = {m.mod_id: m.local_size for m in repo.list_mods(_APP)}
    assert sizes[333] == 10                 # 大小回填到展示列


def test_stale_claims_removed_when_folder_gone(tmp_path):
    base = tmp_path / "mods"
    _make_dir(base, 111)
    repo = _repo(tmp_path)
    game = repo.get_game(_APP)
    scan_game(repo, game)
    assert len(repo.pending_confirmations(_APP)) == 1

    shutil.rmtree(base / "111")             # 用户删了文件夹
    r = scan_game(repo, game)
    assert r.stale_removed == [111]
    assert repo.pending_confirmations(_APP) == []

"""drop_stale_claims 契约测试"""
"""只删未确认 claim 提案；确认行 / 其他 kind / 其他档案不连坐。"""
from core.sqliteRepository import SQLiteRepository


def test_drops_only_pending_claims(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db", snapshot_keep=3)
    repo.add_game(100, "g", "D:/dl")
    repo.record_verdicts([
        {"mod_id": 1, "kind": "claim", "game_id": 100, "version_written": 5},
        {"mod_id": 2, "kind": "claim", "game_id": 100},
        {"mod_id": 3, "kind": "success", "game_id": 100, "version_written": 7},
        {"mod_id": 4, "kind": "claim", "game_id": 100},
    ])
    repo.confirm_items([4])                 # claim 确认后 = 事实，不可清
    assert repo.drop_stale_claims(100, [1, 2, 3, 4]) == 2
    assert {v.mod_id for v in repo.pending_confirmations(100)} == {3}
    assert {r.mod_id for r in repo.list_game_verdicts(100)} == {3, 4}


def test_scoped_to_game(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db", snapshot_keep=3)
    repo.add_game(100, "a", "D:/a")
    repo.add_game(200, "b", "D:/b")
    repo.record_verdicts([{"mod_id": 1, "kind": "claim", "game_id": 200}])
    assert repo.drop_stale_claims(100, [1]) == 0
    assert len(repo.pending_confirmations(200)) == 1
def test_scan_skips_mods_with_pending_success(tmp_path):
    """⑮ 配套去重：批次收尾刚落 success 待确认行的编号，扫描不再
    落第二份 claim 候选（同一编号只允许一份待办）；大小回填照常。"""
    base = tmp_path / "mods"
    _make_dir(base, 777)
    repo = _repo(tmp_path)
    repo.add_mod(Mod(mod_id=777, game_id=_APP, status="tracked"))
    repo.record_verdicts([
        {"mod_id": 777, "kind": "success", "game_id": _APP,
         "version_trigger": 100, "version_written": 100,
         "source": "verified"},
    ])
    r = scan_game(repo, repo.get_game(_APP))
    assert r.candidates_new == []
    assert r.candidates_dup == 1
    assert {v.kind for v in repo.pending_confirmations(_APP)} == {"success"}
    assert repo.get_mod(777).local_size == 10   # 大小回填照常干活
