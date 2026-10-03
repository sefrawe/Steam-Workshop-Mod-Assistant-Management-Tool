"""确认门三正门 + 撤销门测试（M0）
"""
"""
tests/test_confirmationGate.py · 本地版本唯一写入点的专项测试。

红线背景：本地版本只能经确认门写进 mods 表（R17），门开三扇——
  ① record_verdicts → pending_confirmations → confirm_items
     （批次收尾落待确认判决，人在入账中心背书）；
  ② claim_accept（右键认领，即时生效不排队）；
  ③ set_manual_version（右键设定本地版本，人工核对）。
撤销门 revoke_confirmation 是唯一回滚。本文件逐扇验证：
写对了什么、拦住了什么、事务失败时什么都没发生。
"""
import pytest

from core.models import Mod

GAME = 294100


def _add_game(repo, app_id=GAME):
    repo.add_game(app_id, "RimWorld", f"D:/dl/{app_id}")


def _downloaded_mod(repo, mod_id=1, **kw):
    repo.add_mod(Mod(mod_id=mod_id, game_id=GAME, status="downloaded",
                     confirmed_version=100, confirmed_source="verified",
                     confirmed_at=111, **kw))


def _verdict(mod_id=1, kind="success", **kw):
    """一条判决的默认形状：成功判决、归属本档案；其余按需覆盖。"""
    base = {"mod_id": mod_id, "kind": kind, "game_id": GAME}
    base.update(kw)
    return base


# ============ ① 落判决 → 队列 → 批量确认 ============

def test_record_and_queue_basics(repo):
    # 世界状态：1 个待下载 mod；一批判决：1 成功 1 超时 1 失败
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([
        _verdict(1, "success", version_written=120, source="verified"),
        _verdict(1, "timeout"),
        _verdict(1, "fail"),
    ])
    # 队列只摆 success/claim/manual：超时与失败只进判决史（D22）
    pending = repo.pending_confirmations()
    assert len(pending) == 1 and pending[0].version_written == 120
    assert len(repo.list_verdicts(1)) == 3
    # 按档案过滤：别的档案看不到这条（D20 档案隔离）
    assert repo.pending_confirmations(game_id=1158310) == []


def test_record_verdicts_input_guards(repo):
    # kind 非法 / 缺 game_id：都拒收，且一字不落库（事务回滚）
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    with pytest.raises(ValueError):
        repo.record_verdicts([_verdict(1, "瞎写")])
    bad = _verdict(1, "success")
    del bad["game_id"]
    with pytest.raises(ValueError):
        repo.record_verdicts([bad])
    assert repo.list_verdicts(1) == []


def test_confirm_happy_path_tracked_to_downloaded(repo):
    # 世界状态：1 个待下载 mod；1 条待确认的成功判决（写值 120）
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=120,
                                   source="verified")])
    assert repo.confirm_items([1]) == 1
    m = repo.get_mod(1)
    # tracked → downloaded 在确认一刻发生（状态迁移唯一入口）；三件套落位
    assert m.status == "downloaded"
    assert (m.confirmed_version, m.confirmed_source) == (120, "verified")
    assert m.confirmed_at is not None
    # 队列清空；判决史留档（带确认戳）
    assert repo.pending_confirmations() == []
    assert repo.list_verdicts(1)[0].confirmed_at is not None


def test_confirm_creates_ledger_row_from_verdict(repo):
    # 世界状态：mod 从未入账（快速命令下载的东西，D39）；
    # 判决行上带 game_id / 标题 / 大小
    _add_game(repo)
    repo.record_verdicts([_verdict(404, "success", version_written=7,
                                   source="verified", title="账外货",
                                   file_size=123)])
    repo.confirm_items([404])
    m = repo.get_mod(404)
    assert m is not None and m.status == "downloaded"
    assert m.game_id == GAME and m.title == "账外货" and m.file_size == 123
    assert m.confirmed_version == 7


def test_confirm_refuses_blacklisted_and_deleted(repo):
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=9,
                                   source="verified")])
    # 黑名单拦截（D11：建行之前拦）
    repo.add_purged(1, GAME)
    with pytest.raises(ValueError, match="黑名单"):
        repo.confirm_items([1])
    repo.remove_purged(1)
    # 已删除：先恢复再确认
    repo.mark_deleted(1, {})
    with pytest.raises(ValueError, match="删除"):
        repo.confirm_items([1])


def test_confirm_without_pending_row(repo):
    # 没有判决就没有确认——刷新入账中心后再来的口径
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    with pytest.raises(ValueError):
        repo.confirm_items([1])


def test_confirm_batch_is_all_or_nothing(repo):
    # 世界状态：mod 1 一切就绪；mod 2 什么都没有
    # 断言：批量确认整体失败——mod 1 也没被确认（一个事务）
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.add_mod(Mod(mod_id=2, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=9,
                                   source="verified")])
    with pytest.raises(ValueError):
        repo.confirm_items([1, 2])
    assert repo.get_mod(1).status == "tracked"     # 没动
    assert len(repo.pending_confirmations()) == 1  # 还在队列里


def test_confirm_marks_all_pending_rows(repo):
    # 世界状态：同一 mod 先后两批判决都没确认
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=9,
                                   source="verified")])
    repo.record_verdicts([_verdict(1, "success", version_written=10,
                                   source="verified")])
    repo.confirm_items([1])
    # 队列里的旧批次一并销账，不留过期重复项
    assert repo.pending_confirmations() == []
    assert len(repo.list_verdicts(1)) == 2


def test_confirm_failed_mod_revives(repo):
    # 世界状态：失效归档的 mod 重新下载成功
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME, status="failed"))
    repo.record_verdicts([_verdict(1, "success", version_written=5,
                                   source="verified")])
    repo.confirm_items([1])
    assert repo.get_mod(1).status == "downloaded"


def test_confirm_allows_version_unknown(repo):
    # 批查失败照常确认（D4c）：写值 None = 版本未知，状态照样已下载
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=None,
                                   source="unverified")])
    repo.confirm_items([1])
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.confirmed_version is None
    assert m.version_unknown is True


# ============ ② 认领（即时生效） ============

def test_claim_accept_instant(repo):
    # 世界状态：1 个待下载 mod；用户右键认领盘上文件
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.claim_accept(1, 200, local_size=4096)
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.confirmed_version == 200
    assert m.confirmed_source == "claim" and m.local_size == 4096
    # 认领即背书：判决行当场带确认戳，不进队列
    assert repo.pending_confirmations() == []
    v = repo.list_verdicts(1)[0]
    assert v.kind == "claim" and v.confirmed_at is not None


def test_claim_on_downloaded_updates_version_only(repo):
    # 已下载条目被认领：状态保持，确认值照录（下轮检测自愈兜底）
    _add_game(repo)
    _downloaded_mod(repo)
    repo.claim_accept(1, 300)
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.confirmed_version == 300


def test_claim_refuses_missing_and_deleted(repo):
    _add_game(repo)
    # 账上没有：指路"扫描 → 入账中心"，不走这里（D39）
    with pytest.raises(ValueError, match="扫描"):
        repo.claim_accept(999, 1)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.mark_deleted(1, {})
    with pytest.raises(ValueError, match="删除"):
        repo.claim_accept(1, 1)


# ============ ③ 手动设定 ============

def test_manual_version(repo):
    # 世界状态：1 个待下载 mod；用户右键人工核对后填版本号
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.set_manual_version(1, 321, note="对照游戏内版本号核对")
    m = repo.get_mod(1)
    assert m.status == "downloaded" and m.confirmed_version == 321
    assert m.confirmed_source == "manual"
    v = repo.list_verdicts(1)[0]
    assert v.kind == "manual" and v.note == "对照游戏内版本号核对"


# ============ 撤销门与判决史修剪 ============

def test_revoke_clears_version_keeps_status_and_history(repo):
    # 世界状态：已确认的 mod；执行撤销后——
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts([_verdict(1, "success", version_written=9,
                                   source="verified")])
    repo.confirm_items([1])
    repo.revoke_confirmation(1)
    m = repo.get_mod(1)
    # 版本轴清空，状态轴不动（两轴正交）；判决史还在（撤的是当前值）
    assert m.confirmed_version is None and m.confirmed_source is None
    assert m.status == "downloaded"
    assert len(repo.list_verdicts(1)) == 1
    with pytest.raises(ValueError):
        repo.revoke_confirmation(999)


def test_verdict_pruning_keeps_recent_or_confirmed(repo):
    # 夹具 verdict_keep=3。世界状态：1 个 mod、6 条未确认判决；
    # 把最旧 3 条的时间拨回 100 天前（模拟时间流逝）；
    # 再落 1 条触发修剪——修剪规则：确认行豁免；未确认行
    # "最近 3 条以内"或"90 天以内"二者占一即留，双出局的清
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    for i in range(6):
        repo.record_verdicts(
            [_verdict(1, "success", version_written=i, source="verified")])
    repo._conn.execute(
        "UPDATE verdict_log SET occurred_at = occurred_at - 100*86400 "
        "WHERE id <= 3")
    repo.record_verdicts(
        [_verdict(1, "success", version_written=99, source="verified")])
    rows = repo.list_verdicts(1, limit=50)
    # 100 天前的 3 条里只有落在"最近 3 条"窗口内的 1 条幸存
    assert len(rows) == 4


def test_verdict_pruning_spares_confirmed(repo):
    # 确认行是"当前版本的票据"：再老、再多未确认判决也不该把它挤掉
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    repo.record_verdicts(
        [_verdict(1, "success", version_written=1, source="verified")])
    repo.confirm_items([1])
    repo._conn.execute(   # 把确认行也拨到 100 天前
        "UPDATE verdict_log SET occurred_at = occurred_at - 100*86400")
    for i in range(3):    # 再灌 3 条新的未确认判决，触发修剪
        repo.record_verdicts(
            [_verdict(1, "success", version_written=i, source="verified")])
    rows = repo.list_verdicts(1, limit=50)
    confirmed = [r for r in rows if r.confirmed_at is not None]
    assert len(confirmed) == 1 and confirmed[0].version_written == 1


def test_backfill_local_sizes(repo):
    # 盘点回填展示列：账上有的更新，账上没有的忽略不建行
    _add_game(repo)
    repo.add_mod(Mod(mod_id=1, game_id=GAME))
    n = repo.backfill_local_sizes({1: 4096, 999: 8192})
    assert n == 1
    assert repo.get_mod(1).local_size == 4096
    assert repo.get_mod(999) is None
