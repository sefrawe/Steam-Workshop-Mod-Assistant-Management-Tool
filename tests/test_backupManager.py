"""backupManager 测试
"""
r"""
三层覆盖：
- 纯逻辑（全平台）：退出码判定（R5 反直觉点钉死）、目录命名、
  R4 路径保险丝（真实 junction 实测）
- 真实 robocopy（skipif 非 Windows）：真拷小目录、保留策略、
  配额清腾、空间预检
- 恢复流程（skipif 非 Windows）：成功路径 + 注入假 runner 的失败回退
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import backupManager as bm
from core.backupManager import BackupManager
from core.models import Mod
from core.sqliteRepository import SQLiteRepository

WIN = sys.platform == "win32"


# ---------- fixtures ----------

@pytest.fixture()
def env(tmp_path):
    """一套最小环境：临时库 + 一个档案（显式 backup_dir，不依赖 steamcmd）
    + 一个有本地内容的 mod。返回 (repo, 游戏目录, mod 编号)。

    local_size 必须给：备份引擎的空间预检依赖它，不给会触发
    "缺大小跳过预检"警告，干扰对 warnings 的精确断言。
    backup_dir 只登记不创建：它在盘上出现是引擎第一次备份时的副产物
    （引擎先 mkdir 再做空间预检），测试里不要提前替它建。
    """
    repo = SQLiteRepository(tmp_path / "t.db")
    content = tmp_path / "content" / "294100"
    backups = tmp_path / "mod_backups" / "294100"
    repo.add_game(294100, "RimWorld",
                  download_dir=str(content), backup_dir=str(backups))
    mid = 1001
    repo.add_mod(Mod(mod_id=mid, game_id=294100, status="downloaded",
                     local_timeupdated=1700000000, manifest="m1",
                     local_size=1024))
    src = content / str(mid)
    src.mkdir(parents=True)
    (src / "a.txt").write_text("hello", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("world", encoding="utf-8")
    yield repo, content, mid
    repo.close()


def _engine(repo, **kw) -> BackupManager:
    kw.setdefault("keep_per_mod", 3)
    return BackupManager(repo, **kw)


# ---------- 纯逻辑：退出码（R5） ----------

def test_rc_boundary():
    for rc in (0, 1, 2, 7):
        assert bm.is_success_rc(rc) is True
    # ★ R5 陷阱钉死：1 = 有文件拷贝，是正常成功
    assert bm.is_success_rc(1) is True
    for rc in (8, 9, 16):
        assert bm.is_success_rc(rc) is False


# ---------- 纯逻辑：目录命名 ----------

def test_dirname_format(tmp_path):
    dst = BackupManager.__new__(BackupManager)._fresh_target(
        tmp_path, 294100, 1700000000)
    # <modid>_v<版本>_<8位日期>_<6位时间>
    assert dst.name == f"294100_v1700000000_{dst.name.split('_', 2)[2]}"


def test_dirname_collision_suffix(tmp_path):
    eng = BackupManager.__new__(BackupManager)
    first = eng._fresh_target(tmp_path, 1, 5)
    first.mkdir()
    second = eng._fresh_target(tmp_path, 1, 5)
    assert second.name.startswith(first.name + "_2")


# ---------- 纯逻辑：R4 路径保险丝 ----------

@pytest.mark.skipif(not WIN, reason="junction 仅 Windows")
def test_guard_refuses_junction(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(RuntimeError):
            bm._safe_rmtree(link, tmp_path)
        # 链接本体和实体都还在
        assert link.exists() and real.is_dir()
    finally:
        os.rmdir(link)  # 只摘链接不动实体（决策 21④ 的安全拆除法）


def test_guard_refuses_outside(tmp_path):
    inside = tmp_path / "root" / "child"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(RuntimeError):
        bm._safe_rmtree(inside, outside)  # 根不匹配 → 拒绝


# ---------- 真实 robocopy：备份主流程 ----------

@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_backup_success_and_relative_path(env):
    repo, _content, mid = env
    rep = _engine(repo).backup_mod(mid)
    assert rep.ok, rep.error
    rec = rep.backup
    # R1：存的是相对路径（纯目录名），不是绝对路径
    assert "\\" not in rec.backup_path and "/" not in rec.backup_path
    game = repo.get_game(294100)
    abs_dir = Path(game.backup_dir) / rec.backup_path
    assert abs_dir.is_dir()
    assert (abs_dir / "a.txt").read_text(encoding="utf-8") == "hello"
    assert rec.size_bytes > 0
    assert rec.version_timeupdated == 1700000000
    assert rep.warnings == []  # fixture 的 local_size 已给，预检正常走完


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_backup_missing_source(env):
    repo, content, mid = env
    shutil.rmtree(content / str(mid))
    rep = _engine(repo).backup_mod(mid)
    assert not rep.ok and "未找到本地内容" in rep.error


def test_backup_unknown_version(env):
    repo, _c, mid = env
    repo.update_local_state(mid, local_timeupdated=0)  # 0 视为未知
    rep = _engine(repo).backup_mod(mid)
    assert not rep.ok and "本地版本未知" in rep.error


def test_backup_no_root_resolvable(env):
    repo, _c, mid = env
    # 新档案从未设置 backup_dir，steamcmd 也没配 → 推导不出 → 报告失败
    repo.add_game(3117820, "Other", download_dir="X:\\nope")
    repo.add_mod(Mod(mod_id=2002, game_id=3117820,
                     local_timeupdated=111))
    rep = BackupManager(repo, steamcmd_path=None).backup_mod(2002)
    assert not rep.ok and "无法确定备份位置" in rep.error


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_backup_derives_root_and_writes_back(env, tmp_path):
    repo, _c, mid = env
    # 给档案换个没 backup_dir 的"新档案"，steamcmd 指到临时目录
    fake_steamcmd = tmp_path / "tools" / "steamcmd" / "steamcmd.exe"
    fake_steamcmd.parent.mkdir(parents=True)
    fake_steamcmd.write_text("", encoding="utf-8")
    repo.add_game(3117820, "Other", download_dir="X:\\nope")
    repo.add_mod(Mod(mod_id=2002, game_id=3117820,
                     local_timeupdated=111))
    src = tmp_path / "tools" / "steamcmd" / "steamapps" / "workshop" \
          / "content" / "3117820" / "2002"
    src.mkdir(parents=True)
    (src / "f.txt").write_text("x", encoding="utf-8")
    repo.update_game(3117820, download_dir=str(src.parent))
    rep = BackupManager(repo, steamcmd_path=str(fake_steamcmd)) \
        .backup_mod(2002)
    assert rep.ok, rep.error
    # 决策 21⑥：<steamcmd根 的上一级>\mod_backups\<appid>\
    # steamcmd 根 = …\tools\steamcmd（exe 所在目录），上一级 = …\tools
    expect = tmp_path / "tools" / "mod_backups" / "3117820"
    assert repo.get_game(3117820).backup_dir == str(expect)
    assert any("推导" in w for w in rep.warnings)


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_space_check_blocks(env, tmp_path, monkeypatch):
    repo, _c, mid = env
    # shutil.disk_usage 在 Windows 上要求路径真实存在（不存在直接
    # FileNotFoundError）。此时备份还没跑过，backup_dir 尚未创建，
    # 所以拿一定存在的 tmp_path 取样；monkeypatch 之后引擎内部
    # 无论传什么路径，拿到的都是这份伪造值（free=1 字节 = 满盘）
    usage = shutil.disk_usage(str(tmp_path))._replace(free=1)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: usage)
    rep = _engine(repo).backup_mod(mid)
    assert not rep.ok and "空间不足" in rep.error


# ---------- 保留策略 ----------

@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_keep_per_mod_prune(env):
    repo, _c, mid = env
    eng = _engine(repo, keep_per_mod=2)
    dirs: list[Path] = []  # 每份备份的磁盘目录，按备份顺序
    for ver in (111, 222, 333):
        repo.update_local_state(mid, local_timeupdated=ver)
        rep = eng.backup_mod(mid)
        assert rep.ok, rep.error
        dirs.append(Path(repo.get_game(294100).backup_dir)
                    / rep.backup.backup_path)
    rows = repo.list_backups(mid, oldest_first=True)
    # 每 mod 留 2 份：最老的 111 连记录带磁盘都被清掉
    assert [r.version_timeupdated for r in rows] == [222, 333]
    assert not dirs[0].exists()
    assert dirs[1].exists() and dirs[2].exists()


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_pinned_exempts_prune(env):
    repo, _c, mid = env
    eng = _engine(repo, keep_per_mod=1)
    repo.update_local_state(mid, local_timeupdated=111)
    first = eng.backup_mod(mid).backup
    repo.set_pinned(first.id, True)
    repo.update_local_state(mid, local_timeupdated=222)
    second = eng.backup_mod(mid).backup
    rows = repo.list_backups(mid)
    assert {r.id for r in rows} == {first.id, second.id}  # 钉住的活下来了


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_quota_warns_when_nothing_to_clean(env):
    repo, _c, mid = env
    rep = BackupManager(repo, quota_bytes=1).backup_mod(mid)
    assert rep.ok
    # 只剩本次备份（排除自身），配额压不下去 → 警告而非失败
    assert any("配额" in w for w in rep.warnings)
    assert repo.list_backups(mid)  # 本次备份不被自己清掉


# ---------- steamcmd 检测（R7） ----------

def test_steamcmd_running_returns_bool():
    assert isinstance(bm.steamcmd_running(), bool)


# ---------- 恢复流程 ----------

@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_restore_success(env):
    repo, content, mid = env
    eng = _engine(repo)
    rep = eng.backup_mod(mid)
    assert rep.ok
    backup_text = (Path(repo.get_game(294100).backup_dir)
                   / rep.backup.backup_path / "a.txt").read_text("utf-8")
    # 当前内容被改坏，模拟"作者删库后想回滚"
    (content / str(mid) / "a.txt").write_text("CORRUPTED", encoding="utf-8")
    r = eng.restore_backup(rep.backup.id)
    assert r.ok, r.error
    assert r.pre_backup is not None          # R8：恢复前自动备份存在
    assert (content / str(mid) / "a.txt"
            ).read_text("utf-8") == backup_text
    # 挪走的旧目录已删除
    leftovers = [p for p in (content).iterdir()
                 if "restore_old" in p.name]
    assert leftovers == []


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_restore_rollback_on_copy_failure(env):
    repo, content, mid = env
    real_run = BackupManager._run_robocopy
    calls: list[str] = []

    def flaky(src, dst):
        calls.append(dst)
        # 调用序：1 = 测试开头的正常备份，2 = 恢复前备份（须成功），
        # 3 = 恢复复制 → 在这里炸，验证失败回退
        if len(calls) == 3:
            return 16, "mock robocopy failure"
        return real_run(src, dst)

    eng = BackupManager(repo, keep_per_mod=5, runner=flaky)
    rep = eng.backup_mod(mid)
    assert rep.ok
    (content / str(mid) / "a.txt").write_text("CORRUPTED", encoding="utf-8")
    r = eng.restore_backup(rep.backup.id)
    assert not r.ok and "已回退" in r.error
    # 原状恢复：内容还是"改坏后"的样子（恢复前备份拿走了原样）
    assert (content / str(mid) / "a.txt"
            ).read_text("utf-8") == "CORRUPTED"
    assert [p for p in content.iterdir()
            if "restore_old" in p.name] == []


@pytest.mark.skipif(not WIN, reason="robocopy 仅 Windows")
def test_restore_missing_backup_dir(env):
    repo, _c, mid = env
    eng = _engine(repo)
    rep = eng.backup_mod(mid)
    assert rep.ok
    shutil.rmtree(Path(repo.get_game(294100).backup_dir)
                  / rep.backup.backup_path)
    r = eng.restore_backup(rep.backup.id)
    assert not r.ok and "不存在" in r.error
