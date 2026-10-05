"""exceptionFlow 检测引擎的单元测试（V2）。"""
r"""tests/test_exceptionFlow.py · 随 workflows/exceptionFlow.py V2 重写。

V1 版测的 interrupted / steamcmd_missing / acf_missing 随引擎两刀
退役（见引擎文件头），对应用例删除；modVerifier 的解析细节由它自己
的测试负责，这里只验"引擎把对账结果摆进正确的桶"。桶A/B/C 三块为
V2 新增覆盖（V1 未测）。
"""
from pathlib import Path

from core.models import FailedMod, Game, Mod
from workflows import exceptionFlow as ef

APP_ID = 1158310


# ---------- 假件与摆盘工具 ----------

class FakeRepo:
    """只实现引擎用到的两个接口。list_mods 带 **kwargs 以对齐真 repo
    的可选参数形状。"""

    def __init__(self, mods=(), failed=()):
        self._mods = list(mods)
        self._failed = list(failed)

    def list_mods(self, game_id, **kwargs):
        want = kwargs.get("status")
        return [m for m in self._mods
                if m.game_id == game_id
                and (want is None or m.status == want)]

    def list_failed(self, game_id):
        return [f for f in self._failed if f.game_id == game_id]


def make_mod(mid, status, game_id=APP_ID):
    return Mod(mod_id=mid, game_id=game_id, status=status)


def make_failed(mid, game_id=APP_ID, reason="result=9"):
    return FailedMod(id=0, mod_id=mid, game_id=game_id, reason=reason,
                     last_known_state=None, replaced_by=None, detected_at=0)


def make_game(tmp_path, *, with_content=False):
    dl = tmp_path / "content"
    if with_content:
        dl.mkdir(parents=True)
    return Game(app_id=APP_ID, name="测试游戏", download_dir=str(dl),
                game_mod_dir="", backup_dir="", created_at=0)


def put_dir(content_root, mid, *, empty=False):
    """盘上摆一个 mod 目录（默认放一个文件 = 有内容）。"""
    d = Path(content_root) / str(mid)
    d.mkdir(parents=True)
    if not empty:
        (d / "a.txt").write_text("x", encoding="utf-8")
    return d


# ---------- detect_local：旗标与对账 ----------

def test_dead_root_when_content_dir_absent(tmp_path):
    # 盘上：content 目录不存在｜账里：downloaded 111
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, make_game(tmp_path))
    assert rep.dead_root is True
    assert rep.missing == []
    # 账本半边不受影响：旗标归旗标，能判的照常给
    assert rep.failed_ids == []


def test_missing_bucket(tmp_path):
    # 盘上：content 目录空空如也｜账里：downloaded 111、tracked 222
    game = make_game(tmp_path, with_content=True)
    repo = FakeRepo([make_mod(111, "downloaded"), make_mod(222, "tracked")])
    rep = ef.detect_local(repo, game)
    assert rep.missing == [111]
    assert rep.orphans == [] and rep.empty_dirs == []
    assert rep.tracked_on_disk == []


def test_empty_dir_is_bucket1(tmp_path):
    # 盘上：111 空目录｜账里：downloaded 111
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 111, empty=True)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, game)
    assert rep.empty_dirs == [111]
    assert rep.missing == []


def test_orphan_bucket(tmp_path):
    # 盘上：纯数字目录 999｜账里：只有无关的 111
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 999)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, game)
    assert rep.orphans == [999]


def test_tracked_with_content_carried_not_reported(tmp_path):
    # 盘上：333 有内容｜账里：tracked 333（= 入账中心确认补版本管辖）
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 333)
    repo = FakeRepo([make_mod(333, "tracked")])
    rep = ef.detect_local(repo, game)
    assert rep.orphans == [] and rep.missing == []
    assert rep.tracked_on_disk == [333]        # 指路信息原样携带
    assert rep.has_anomalies is False           # 但不算异常


def test_deleted_with_content_not_reported(tmp_path):
    # 盘上：444 有内容｜账里：deleted 444（软删除保留文件属正常）
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 444)
    repo = FakeRepo([make_mod(444, "deleted")])
    rep = ef.detect_local(repo, game)
    assert rep.has_anomalies is False


def test_non_numeric_carried(tmp_path):
    # 盘上：content 里混进一个非数字目录｜账里：空
    game = make_game(tmp_path, with_content=True)
    (Path(game.download_dir) / "notes").mkdir()
    rep = ef.detect_local(FakeRepo(), game)
    assert "notes" in rep.non_numeric
    assert rep.has_anomalies is True


def test_failed_union_of_records_and_status(tmp_path):
    # 盘上：无要求｜账里：归档 700 + 状态 failed 800（无归档）
    game = make_game(tmp_path, with_content=True)
    repo = FakeRepo([make_mod(800, "failed")], [make_failed(700)])
    rep = ef.detect_local(repo, game)
    assert rep.failed_ids == [700, 800]
    assert rep.failed_records[0].mod_id == 700


# ---------- detect_remote / classify_entries：分桶 ----------

def Q(entries):
    """把条目列表包成注入用查询函数（忽略入参批次）。"""
    return lambda batch: entries


def test_remote_invalid_result9():
    out = ef.detect_remote(Q([{"publishedfileid": "9", "result": "9"}]), [9])
    assert out.invalid == [9]
    assert out.ok == []


def test_remote_query_failed():
    out = ef.detect_remote(Q([{"publishedfileid": "8", "result": "2"}]), [8])
    assert out.query_failed == [8]
    assert out.invalid == []


def test_remote_suspected_collection_size_zero_or_missing():
    entries = [{"publishedfileid": "1", "result": "1", "file_size": "0"},
               {"publishedfileid": "2", "result": "1"}]
    out = ef.detect_remote(Q(entries), [1, 2])
    assert out.suspected_collection == [1, 2]
    assert out.ok == []


def test_remote_ok_and_sorted():
    entries = [{"publishedfileid": "30", "result": "1", "file_size": "512"},
               {"publishedfileid": "10", "result": "1", "file_size": "9"}]
    out = ef.detect_remote(Q(entries), [30, 10])
    assert out.ok == [10, 30]


def test_remote_malformed_entries():
    entries = [{"publishedfileid": "7"},        # 没有 result
               {"result": "1"},                 # 没有编号
               {"publishedfileid": "abc", "result": "1"}]  # 编号读不动
    out = ef.detect_remote(Q(entries), [7])
    assert 7 in out.malformed
    assert len(out.malformed) == 3


def test_remote_chunking_at_100():
    # 250 个 id → 3 次调用，块长 100/100/50（分批约定）
    calls = []

    def spy(batch):
        calls.append(list(batch))
        return [{"publishedfileid": str(i), "result": "1", "file_size": "1"}
                for i in batch]

    out = ef.detect_remote(spy, range(1, 251))
    assert [len(c) for c in calls] == [100, 100, 50]
    assert len(out.ok) == 250


def test_classify_entries_matches_detect_remote():
    # 同一批条目：直接分类与经 detect_remote 分批驱动，结果必须一致
    entries = [{"publishedfileid": "1", "result": "1", "file_size": "10"},
               {"publishedfileid": "2", "result": "9"},
               {"publishedfileid": "3", "result": "5"},
               {"publishedfileid": "4", "result": "1", "file_size": "0"}]
    direct = ef.classify_entries(entries)
    driven = ef.detect_remote(lambda batch: entries, [1, 2, 3, 4])
    assert direct.ok == driven.ok == [1]
    assert direct.invalid == driven.invalid == [2]
    assert direct.query_failed == driven.query_failed == [3]
    assert (direct.suspected_collection
            == driven.suspected_collection == [4])


def test_classify_entries_empty():
    out = ef.classify_entries([])
    assert out.ok == [] and out.invalid == []
    assert out.query_failed == [] and out.suspected_collection == []


def test_multifrontend_note_mentions_rimsort():
    # 桶⑥静态文案：内容底线——至少说清 RimSort 与"删干净"的口径
    assert "RimSort" in ef.MULTIFRONTEND_NOTE
    assert "一并移除" in ef.MULTIFRONTEND_NOTE


# ---------- classify_health（桶A）----------

def _ok_entry(pid, title, banned="0"):
    return {"publishedfileid": str(pid), "result": "1",
            "file_size": "10", "title": title, "banned": banned}


def test_health_title_mismatch_ignores_version_tokens():
    # 版本记号剥掉后一致 → 不算改名；剥完仍不同 → 算
    entries = [_ok_entry(1, "Better Menu v1.6"),
               _ok_entry(2, "Totally Renamed")]
    out = ef.classify_health(entries, {1: "Better Menu", 2: "Old Name"})
    assert out.title_mismatch == [(2, "Old Name", "Totally Renamed")]

def test_health_abandoned_and_banned():
    # 本用例只管弃坑/banned 两路：本地标题给成与远端一致，
    # 不触发标题不符（那一路由 test_health_title_mismatch_* 负责）
    entries = [_ok_entry(1, "Abandoned Things", banned="1"),
               _ok_entry(2, "Deprecated Stuff")]
    out = ef.classify_health(entries, {1: "Abandoned Things",
                                       2: "Deprecated Stuff"})
    assert out.title_mismatch == []  # 标题一致：零不符
    assert out.abandoned == [1, 2]   # 两个标题都命中弃坑词
    assert out.banned == [1]         # 只有 1 号被封禁
    assert out.total == 3

def test_health_missing_banned_field_flag():
    # 整批都没带 banned 键 → 置位；带了（哪怕值是 0）→ 不置位；
    # 没有 result=1 条目 → 无从谈起，不置位
    no_field = [{"publishedfileid": "1", "result": "1",
                 "file_size": "5", "title": "A"}]
    assert ef.classify_health(no_field, {}).missing_banned_field is True
    assert ef.classify_health([_ok_entry(1, "A")], {},
                              ).missing_banned_field is False
    dead = [{"publishedfileid": "1", "result": "9"}]
    assert ef.classify_health(dead, {}).missing_banned_field is False


def test_health_skips_non_ok_entries():
    # result≠1 的条目不判健康（归桶④⑤，不重复报）
    entries = [{"publishedfileid": "1", "result": "9",
                "title": "abandoned", "banned": "1"}]
    out = ef.classify_health(entries, {1: "abandoned"})
    assert out.abandoned == [] and out.banned == []
    assert out.title_mismatch == []


# ---------- classify_dependencies（桶B）----------

def test_dep_missing_and_childless():
    entries = [{"mod_id": 1, "result": 1, "children": [111, 999]},
               {"mod_id": 2, "result": 1, "children": []}]
    out = ef.classify_dependencies(entries, {111}, {}, first_fetch=True)
    assert out.missing == {1: [999]}     # 999 不在账本 = 缺依赖
    assert out.childless == [2]          # 真无依赖
    assert out.fetched == 2 and out.skipped == 0


def test_dep_changed_only_on_second_fetch():
    baseline = {1: [111]}
    entries = [{"mod_id": 1, "result": 1, "children": [111, 222]}]
    first = ef.classify_dependencies(entries, {111, 222}, baseline,
                                     first_fetch=True)
    assert first.changed == []           # 首拉只建基线
    second = ef.classify_dependencies(entries, {111, 222}, baseline,
                                      first_fetch=False)
    assert second.changed == [(1, [111], [111, 222])]


def test_dep_undetermined_children_key_absent():
    # children 键缺席 = 未判定：绝不冒充"无依赖"，也绝不入账
    entries = [{"mod_id": 1, "result": 1, "children": None}]
    out = ef.classify_dependencies(entries, {111}, {}, first_fetch=False)
    assert out.undetermined == [1]
    assert out.childless == [] and out.fetched == 0


def test_dep_skips_failed_entries():
    entries = [{"mod_id": 1, "result": 9, "children": [999]}]
    out = ef.classify_dependencies(entries, set(), {}, first_fetch=False)
    assert out.skipped == 1 and out.missing == {}


# ---------- classify_local_titles（桶C）----------

def test_title_hits_casefold():
    # 大小写不敏感子串匹配（设置页 tooltip 承诺的用户契约）
    out = ef.classify_local_titles(
        {1: "Abandoned Mines", 2: "Nice Mod"}, "abandoned")
    assert out.hits == {1: ["abandoned"]}
    assert out.total == 1


def test_title_keywords_tolerance():
    # 消费端兜底：中文逗号/分号/空白混排都能切开（保存侧归一之外的第二道）
    out = ef.classify_local_titles({1: "Deprecated Thing"},
                                   "Abandoned，Deprecated;  outdated")
    assert out.hits == {1: ["deprecated"]}


def test_title_empty_disables():
    # 留空 = 停用（None 与 "" 同语义）；命中词来自词表、按序返回
    assert ef.classify_local_titles({1: "Abandoned"}, "").hits == {}
    assert ef.classify_local_titles({1: "Abandoned"}, None).hits == {}
    multi = ef.classify_local_titles(
        {1: "Old Abandoned Deprecated"}, "deprecated,abandoned")
    assert multi.hits == {1: ["abandoned", "deprecated"]}  # 词表排序
