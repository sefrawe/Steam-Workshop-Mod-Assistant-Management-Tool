"""exceptionFlow 检测引擎的单元测试。"""
"""
world 状态先列表再断言（踩坑⑭⑮的规矩）：每个用例开头的注释都写明
"盘上有什么 / 账里有什么"。扫描器相关用例 monkeypatch localScanner
的函数（引擎按模块属性调用，patch 得进去）；不猜 acf 文件格式——
合成 acf 的解析已由 localScanner 自己的 32 个测试负责，这里只验
"引擎把扫描结果摆进正确的桶"。
"""
import pytest
from pathlib import Path

from core import localScanner
from core.models import FailedMod, Game, Mod
from workflows import exceptionFlow as ef

APP_ID = 1158310


# ---------- 假件与摆盘工具 ----------

class FakeRepo:
    """只实现引擎用到的三个接口。list_mods 带 **kwargs 以对齐真 repo
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
    return Mod(mod_id=mid, game_id=game_id, status=status, url=None,
               title=None, creator=None, time_created=None,
               time_updated=None, last_time_updated=None,
               local_timeupdated=None, manifest=None, local_size=None,
               file_size=None, subscriptions=None, favorited=None,
               views=None, tags=None, last_checked_at=None,
               preview_url=None, is_special=False, note=None,
               color_tag=None, local_path=None, deleted_at=None,
               deleted_last_state=None, first_tracked_at=None)


def make_failed(mid, game_id=APP_ID, reason="result=9"):
    return FailedMod(id=0, mod_id=mid, game_id=game_id, reason=reason,
                     last_known_state=None, replaced_by=None,
                     detected_at=0)


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


def make_steamcmd_root(tmp_path, *, with_acf=False):
    """摆 steamcmd 根 + workshop 层；with_acf 时摆一个空 acf 占位
    （解析由 monkeypatch 顶替，文件内容无关紧要，只要存在）。"""
    root = tmp_path / "steamcmd"
    ws = root / "steamapps" / "workshop"
    ws.mkdir(parents=True)
    if with_acf:
        (ws / f"appworkshop_{APP_ID}.acf").write_text(
            "占位", encoding="utf-8")
    return root


class FakeScanResult:
    """形状对齐 localScanner.scan_acf 的返回（引擎只碰这几个字段）。"""

    def __init__(self, items=(), interrupted=(), skipped=(), warnings=()):
        self.items = list(items)
        self.interrupted = list(interrupted)
        self.skipped = list(skipped)
        self.warnings = list(warnings)


# ---------- detect_local：旗标 ----------

def test_steamcmd_missing_only_account_buckets(tmp_path):
    # 盘上：无（root=None）｜账里：failed 归档 700 + failed 状态 800
    repo = FakeRepo([make_mod(800, "failed")],
                    [make_failed(700)])
    rep = ef.detect_local(repo, None, make_game(tmp_path))
    assert rep.steamcmd_missing is True
    assert rep.failed_ids == [700, 800]
    # 盘上桶必须全空——不是查过没有，是根本没法查
    assert rep.missing == [] and rep.orphans == []
    assert rep.interrupted == [] and rep.empty_dirs == []


def test_dead_root_when_content_dir_absent(tmp_path):
    # 盘上：content 目录不存在｜账里：downloaded 111
    root = make_steamcmd_root(tmp_path)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, root, make_game(tmp_path))
    assert rep.dead_root is True
    assert rep.missing == []
    # 账本半边不受影响：旗标归旗标，能判的照常给
    assert rep.failed_ids == []


# ---------- detect_local：三个异常桶 ----------

def test_missing_bucket(tmp_path):
    # 盘上：content 目录空空如也｜账里：downloaded 111、tracked 222
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    repo = FakeRepo([make_mod(111, "downloaded"), make_mod(222, "tracked")])
    rep = ef.detect_local(repo, root, game)
    assert rep.missing == [111]
    assert rep.orphans == [] and rep.empty_dirs == []


def test_empty_dir_is_bucket1(tmp_path):
    # 盘上：111 空目录｜账里：downloaded 111
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 111, empty=True)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, root, game)
    assert rep.empty_dirs == [111]
    assert rep.missing == []


def test_orphan_bucket(tmp_path):
    # 盘上：纯数字目录 999｜账里：只有无关的 111
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 999)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, root, game)
    assert rep.orphans == [999]


def test_tracked_with_content_is_not_an_exception(tmp_path):
    # 盘上：333 有内容｜账里：tracked 333（= 决策 24 确认流管辖）
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 333)
    repo = FakeRepo([make_mod(333, "tracked")])
    rep = ef.detect_local(repo, root, game)
    assert rep.orphans == [] and rep.missing == []
    assert rep.has_anomalies is False


def test_deleted_with_content_not_reported(tmp_path):
    # 盘上：444 有内容｜账里：deleted 444（软删除保留文件属正常）
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    put_dir(game.download_dir, 444)
    repo = FakeRepo([make_mod(444, "deleted")])
    rep = ef.detect_local(repo, root, game)
    assert rep.has_anomalies is False


def test_non_numeric_carried(tmp_path):
    # 盘上：content 里混进一个非数字目录｜账里：空
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    (Path(game.download_dir) / "notes").mkdir()

    rep = ef.detect_local(FakeRepo(), root, game)
    assert "notes" in rep.non_numeric
    assert rep.has_anomalies is True


def test_failed_union_of_records_and_status(tmp_path):
    # 盘上：无要求｜账里：归档 700 + 状态 failed 800（无归档）
    root = make_steamcmd_root(tmp_path)
    game = make_game(tmp_path, with_content=True)
    repo = FakeRepo([make_mod(800, "failed")], [make_failed(700)])
    rep = ef.detect_local(repo, root, game)
    assert rep.failed_ids == [700, 800]
    assert rep.failed_records[0].mod_id == 700


# ---------- detect_local：桶① 与 acf 旗标 ----------

def test_acf_missing_flag(tmp_path):
    # 盘上：root 有 workshop 层但没有 acf｜账里：downloaded 111 且盘上无
    root = make_steamcmd_root(tmp_path, with_acf=False)
    game = make_game(tmp_path, with_content=True)
    repo = FakeRepo([make_mod(111, "downloaded")])
    rep = ef.detect_local(repo, root, game)
    assert rep.acf_missing is True
    assert rep.interrupted == []
    assert rep.missing == [111]  # 目录对账不受 acf 缺失影响


def test_interrupted_from_scanner(tmp_path, monkeypatch):
    # 盘上：acf 占位文件｜账里：空；扫描器（顶替）报 interrupted 555 和一条读不动的脏数据
    root = make_steamcmd_root(tmp_path, with_acf=True)
    game = make_game(tmp_path, with_content=True)
    monkeypatch.setattr(
        localScanner, "scan_acf",
        lambda path: FakeScanResult(interrupted=["555", "垃圾数据"]))
    rep = ef.detect_local(FakeRepo(), root, game)
    # 脏数据被宽容转换丢掉（决策 17 口径：单字段异常不炸整批）
    assert rep.interrupted == [555]


def test_corrupt_acf_raises(tmp_path, monkeypatch):
    # 盘上：acf 占位｜扫描器（顶替）抛 ValueError → 引擎必须原样上抛
    root = make_steamcmd_root(tmp_path, with_acf=True)
    def boom(path):
        raise ValueError("acf 结构损坏")
    monkeypatch.setattr(localScanner, "scan_acf", boom)
    with pytest.raises(ValueError):
        ef.detect_local(FakeRepo(), root, make_game(tmp_path, with_content=True))


# ---------- detect_remote：分桶 ----------

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
    entries = [{"publishedfileid": "7"},            # 没有 result
               {"result": "1"},                     # 没有编号
               {"publishedfileid": "abc", "result": "1"}]  # 编号读不动
    out = ef.detect_remote(Q(entries), [7])
    assert 7 in out.malformed
    assert len(out.malformed) == 3


def test_remote_chunking_at_100():
    # 250 个 id → 3 次调用，块长 100/100/50（决策 17 分批约定）
    calls = []
    def spy(batch):
        calls.append(list(batch))
        return [{"publishedfileid": str(i), "result": "1",
                 "file_size": "1"} for i in batch]
    out = ef.detect_remote(spy, range(1, 251))
    assert [len(c) for c in calls] == [100, 100, 50]
    assert len(out.ok) == 250


def test_multifrontend_note_mentions_rimsort():
    # 桶⑥静态文案：内容底线——至少说清 RimSort 与"删干净"的口径
    assert "RimSort" in ef.MULTIFRONTEND_NOTE
    assert "一并移除" in ef.MULTIFRONTEND_NOTE

# ---------- classify_entries：detect_remote 的分类核心 ----------

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
