"""deleteManageFlow 引擎与 purge_mod 的测试（决策 69）。
盘上操作全部走 pytest tmp_path 真目录——safe_rmtree 的 R4 保险丝
必须对真文件系统生效才值得测。junction 用例仅在 Windows + Python 3.12+ 跑。"""
import os
import sys

import pytest

from core.models import Game, Mod
from core.sqliteRepository import SQLiteRepository
from workflows import deleteManageFlow as dmf
from pathlib import Path
from core import steamPaths
APP = 1158310


def _mod(mid, status="downloaded"):
    return Mod(
        mod_id=mid, game_id=APP, status=status,
        url=f"u{mid}", title=f"标题{mid}", creator=None,
        time_created=None, time_updated=None, last_time_updated=None,
        local_timeupdated=111 if status == "downloaded" else None,
        manifest="m", local_size=10, file_size=None,
        subscriptions=None, favorited=None, views=None, tags=None,
        last_checked_at=None, preview_url=None, is_special=False,
        note=None, color_tag=None, local_path=None,
        deleted_at=None, deleted_last_state=None, first_tracked_at=None)


@pytest.fixture()
def repo(tmp_path):
    return SQLiteRepository(tmp_path / "t.db")


@pytest.fixture()
def env(tmp_path, repo):
    """搭一个假 steamcmd 布局：content/1158310 下 100 在盘、200 缺失、
    300 软删残留、999 孤儿。返回 (repo, game, content_dir)。"""
    content = tmp_path / "steamcmd" / "steamapps" / "workshop" / "content" / str(APP)
    content.mkdir(parents=True)
    (content / "100").mkdir()
    (content / "100" / "f.bin").write_bytes(b"x" * 10)
    (content / "300").mkdir()
    (content / "999").mkdir()
    (content / "999" / "g.bin").write_bytes(b"y" * 5)
    (content / "说明.txt").write_text("非数字，归核验页管")
    repo.add_game(APP, "假游戏", str(content))
    for mid, st in ((100, "downloaded"), (200, "downloaded"),
                    (300, "deleted"), (400, "tracked")):
        repo.add_mod(_mod(mid, st))
    game = repo.get_game(APP)
    return repo, game, content


def test_inventory_sections(env):
    repo, game, content = env
    rep = dmf.inventory(repo, game, str(content.parents[2] / "steamcmd.exe"))
    assert rep.steamcmd_missing is False and rep.dead_root is False
    assert {r.mod_id for r in rep.missing_rows} == {200}
    assert {r.mod_id for r in rep.cleanable_rows} == {100, 300}
    assert [o.mod_id for o in rep.orphans] == [999]
    assert rep.orphans[0].in_acf is False and rep.acf_missing is True
    assert rep.ledger_rows[0].dir_size == 10  # mod 100 实测 10 字节


def test_purge_gate_blocks_with_backups(repo, tmp_path):
    # mods 表外键指向 games 表：先有游戏才能有 mod（env 夹具同款前提）
    repo.add_game(APP, "假游戏", str(tmp_path / "content"))
    repo.add_mod(_mod(500))
    repo.add_backup(500, "500_v1_x", 7, 111)
    with pytest.raises(ValueError):
        repo.purge_mod(500)  # 默认拒绝：备份去留必须先有决策
    assert repo.get_mod(500) is not None


def test_purge_clears_records_keeps_evidence(repo, tmp_path):
    repo.add_game(APP, "假游戏", str(tmp_path / "content"))
    repo.add_mod(_mod(500))
    repo.add_snapshot(500, time_updated=1)
    repo.add_alert(500, 1)
    repo.add_backup(500, "500_v1_x", 7, 111)
    repo.mark_failed(500, "result=9")
    n = repo.purge_mod(500, purge_backups=True)
    assert n == 1
    assert repo.get_mod(500) is None
    assert repo.list_backups(500) == []
    assert repo.list_snapshots(500) == []
    assert repo.list_alerts(500) == []
    # 证据表刻意存活（无外键证据表）；operations_log 不在本测（无引用）
    assert len(repo.list_failed(APP)) == 1


def test_build_plan_validations(env):
    repo, game, content = env
    rep = dmf.inventory(repo, game, str(content.parents[2] / "steamcmd.exe"))
    with pytest.raises(ValueError):
        dmf.build_plan(repo, rep, dmf.PlanRequest(
            ledger_actions={100: "soft"}))       # 100 不在缺失清单
    with pytest.raises(ValueError):
        dmf.build_plan(repo, rep, dmf.PlanRequest(
            clean_actions={300: "wipe_soft"}))   # 已删除条目没有软删除可做
    with pytest.raises(ValueError):
        dmf.build_plan(repo, rep, dmf.PlanRequest(
            orphan_ids=[100]))                   # 100 不是孤儿


def test_execute_content_and_outside_root(env):
    repo, game, content = env
    act = dmf.Action(kind="content", mod_id=100,
                     path=str(content / "100"), expected_root=str(content))
    res = dmf.execute_action(repo, act)
    assert res.ok and not (content / "100").exists()
    # 根外路径 → R4 保险丝拒绝（ok=False 且目标还在）
    outside = content.parent.parent / "别处"
    outside.mkdir()
    act2 = dmf.Action(kind="content", mod_id=100, path=str(outside),
                      expected_root=str(content))
    res2 = dmf.execute_action(repo, act2)
    assert not res2.ok and outside.exists()

def test_execute_full_mixed_run(env, tmp_path):
    repo, game, content = env
    steamcmd_exe = str(content.parents[2] / "steamcmd.exe")
    # 备份根与引擎走同一单源（steamPaths.backup_root_default）——
    # 不自设路径：引擎推导在哪，假备份就建在哪
    bdir = Path(steamPaths.backup_root_default(steamcmd_exe, APP))
    (bdir / "500_v1_x").mkdir(parents=True, exist_ok=True)
    (bdir / "500_v1_x" / "b.bin").write_bytes(b"z" * 3)
    repo.add_mod(_mod(500))  # downloaded 且无目录 → missing
    repo.add_backup(500, "500_v1_x", 3, 111)
    rep = dmf.inventory(repo, game, steamcmd_exe)
    req = dmf.PlanRequest(
        ledger_actions={200: "soft", 500: "purge"},
        clean_actions={100: "wipe_soft", 300: "wipe"},
        purge_backup_files=True, orphan_ids=[999])
    plan = dmf.build_plan(repo, rep, req)
    assert plan.actions[0].kind == "ledger_soft"   # 账先
    assert plan.actions[-1].kind == "orphan"       # 盘后
    results = [dmf.execute_action(repo, a) for a in plan.actions]
    assert all(r.ok for r in results), [r.detail for r in results if not r.ok]
    assert repo.get_mod(500) is None            # 彻底清账
    assert repo.get_mod(200).status == "deleted"  # 软删除
    assert repo.get_mod(100).status == "deleted"  # 删文件+软删除
    assert not (content / "100").exists() and not (content / "300").exists()
    assert not (content / "999").exists()       # 孤儿删除
    assert not (bdir / "500_v1_x").exists()     # 备份文件随勾选删除
    assert repo.list_backups(500) == []         # 登记随账清除
    # 不勾备份文件时：文件保留（成为未登记文件夹）
    (bdir / "500_v1_x").mkdir(parents=True, exist_ok=True)
    repo.add_mod(_mod(500))
    repo.add_backup(500, "500_v1_x", 3, 111)
    rep2 = dmf.inventory(repo, game, steamcmd_exe)
    plan2 = dmf.build_plan(repo, rep2, dmf.PlanRequest(
        ledger_actions={500: "purge"}, purge_backup_files=False))
    for a in plan2.actions:
        dmf.execute_action(repo, a)
    assert (bdir / "500_v1_x").exists()


@pytest.mark.skipif(sys.platform != "win32"
                    or not hasattr(os.path, "isjunction"),
                    reason="junction 预检仅 Windows + Python 3.12+ 有意义")
def test_junction_tree_refused(env, tmp_path):
    repo, game, content = env
    import _winapi
    real = tmp_path / "游戏侧真身"
    real.mkdir()
    (real / "data.bin").write_bytes(b"!")
    link = content / "888"
    _winapi.CreateJunction(str(real), str(link))
    act = dmf.Action(kind="content", mod_id=888, path=str(link),
                     expected_root=str(content))
    res = dmf.execute_action(repo, act)
    assert not res.ok and real.exists() and (real / "data.bin").exists()
