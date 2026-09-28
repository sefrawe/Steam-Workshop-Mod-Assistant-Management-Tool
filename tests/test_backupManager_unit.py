"""backupManager 测试
"""
r"""
覆盖红线：R5（rc 1=成功、8=失败——最易踩的反直觉点）、R4 保险丝
（删除拒绝 junction）、R8（恢复前强制备份/无内容跳过）、保留策略
（keep / 配额 / 钉住豁免）、恢复失败回退（runner 计数器制造第 N 次失败）、
delete_backup 先盘后账。robocopy 全部注入假 runner，不依赖真 robocopy。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.backupManager import BackupManager, is_success_rc
from core.models import Mod
from core.sqliteRepository import SQLiteRepository


# ---------- 环境与假 runner ----------

def _ok_runner(src, dst):
    """假 robocopy：建出目标并放一个文件。rc=1（有文件拷贝，R5 常态成功）。"""
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "data.txt"), "w", encoding="utf-8") as f:
        f.write("copied")
    return 1, ""


def _dirty_runner(src, dst):
    """失败版：先把目标建出来（制造半成品），再报 rc=8。"""
    os.makedirs(dst, exist_ok=True)
    return 8, "boom"


@pytest.fixture()
def env(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    dl = tmp_path / "dl"
    (dl / "100").mkdir(parents=True)
    (dl / "100" / "mod.txt").write_text("v1", encoding="utf-8")
    repo.add_game(294100, "RimWorld", download_dir=str(dl))
    repo.add_mod(Mod(mod_id=100, game_id=294100, status="downloaded",
                     local_timeupdated=1700000000, local_size=100))
    repo.update_game(294100, backup_dir=str(tmp_path / "bak"))
    yield repo, tmp_path
    repo.close()


def _mgr(repo, tmp_path, **kw):
    return BackupManager(repo, runner=_ok_runner,
                         steamcmd_path=str(tmp_path / "sc"), **kw)


# ---------- R5：退出码判定 ----------

def test_is_success_rc():
    # R5：0-7 全部成功——尤其 1（有文件被拷贝）是最常见的正常成功
    assert all(is_success_rc(rc) for rc in (0, 1, 3, 7))
    assert not any(is_success_rc(rc) for rc in (8, 9, 16))


# ---------- 备份 ----------

def test_backup_happy(env):
    repo, tmp = env
    rep = _mgr(repo, tmp).backup_mod(100, note="手动")
    assert rep.ok and rep.backup is not None
    target = tmp / "bak" / rep.backup.backup_path
    assert target.is_dir() and (target / "data.txt").is_file()
    # R1：backup_path 只存目录名，不带任何路径分隔符
    assert "/" not in rep.backup.backup_path and "\\" not in rep.backup.backup_path
    assert rep.backup.version_timeupdated == 1700000000
    assert rep.backup.size_bytes and rep.backup.size_bytes > 0
    assert rep.backup.note == "手动"


def test_backup_rc8_cleans_halfproduct_and_no_record(env):
    repo, tmp = env
    m = BackupManager(repo, runner=_dirty_runner, steamcmd_path=str(tmp / "sc"))
    rep = m.backup_mod(100)
    assert not rep.ok and rep.backup is None and "8" in rep.error
    # 半成品被清掉，账上没有记录
    assert not any((tmp / "bak").iterdir())
    assert repo.list_backups(oldest_first=True) == []


def test_backup_requires_local_content(env):
    repo, tmp = env
    shutil.rmtree(tmp / "dl" / "100")
    rep = _mgr(repo, tmp).backup_mod(100)
    assert not rep.ok and "本地内容" in rep.error


def test_backup_requires_known_version(env):
    repo, tmp = env
    repo.add_mod(Mod(mod_id=200, game_id=294100, status="downloaded"))
    rep = _mgr(repo, tmp).backup_mod(200)
    assert not rep.ok and "本地版本未知" in rep.error


# ---------- 保留策略 ----------

def test_prune_keeps_latest_n(env):
    repo, tmp = env
    m = _mgr(repo, tmp, keep_per_mod=2)
    reps = [m.backup_mod(100) for _ in range(3)]
    assert all(r.ok for r in reps)
    kept = {b.id for b in repo.list_backups(oldest_first=True)}
    assert kept == {reps[1].backup.id, reps[2].backup.id}
    assert reps[2].cleaned == [reps[0].backup.backup_path]
    assert not (tmp / "bak" / reps[0].backup.backup_path).exists()


def test_prune_pinned_exempt(env):
    repo, tmp = env
    m = _mgr(repo, tmp, keep_per_mod=1)
    first = m.backup_mod(100)
    repo.set_pinned(first.backup.id, True)
    m.backup_mod(100)
    kept = repo.list_backups(oldest_first=True)
    # keep=1 但钉住的第一份豁免清理 → 两份都还在
    assert len(kept) == 2 and kept[0].pinned is True


def test_quota_cleans_oldest_then_warns(env):
    repo, tmp = env
    m = _mgr(repo, tmp, quota_bytes=1)  # 配额 1 字节：永远超标
    r1 = m.backup_mod(100)
    r2 = m.backup_mod(100)
    assert r2.ok
    kept = [b.id for b in repo.list_backups(oldest_first=True)]
    assert kept == [r2.backup.id]           # 只剩本次（exclude 保护）
    assert r1.backup.backup_path in r2.cleaned
    assert any("配额" in w for w in r2.warnings)  # 清不动了 → 提示手动处理


# ---------- 恢复 ----------

def test_restore_happy(env):
    repo, tmp = env
    m = _mgr(repo, tmp)
    bak = m.backup_mod(100).backup
    # 当前内容演进（模拟 mod 更新后想回滚）
    (tmp / "dl" / "100" / "new.txt").write_text("v2 新增", encoding="utf-8")
    rep = m.restore_backup(bak.id)
    assert rep.ok and rep.pre_backup is not None
    assert rep.pre_backup.note == "恢复前自动备份"  # R8 落实
    dest = tmp / "dl" / "100"
    assert (dest / "data.txt").is_file()      # 备份内容回来了
    assert not (dest / "new.txt").is_file()   # 当前版本被替换
    # 挪走的旧目录已清理，无 _restore_old_ 残留
    assert not [p for p in (tmp / "dl").iterdir() if "_restore_old_" in p.name]


def test_restore_failure_rolls_back(env):
    repo, tmp = env
    calls: list[str] = []

    def runner(src, dst):
        calls.append(dst)
        os.makedirs(dst, exist_ok=True)
        if len(calls) == 3:  # 第3次 = 恢复制复制（1=备份 2=恢复前备份）
            return 8, "boom"
        with open(os.path.join(dst, "data.txt"), "w", encoding="utf-8") as f:
            f.write("copied")
        return 1, ""

    m = BackupManager(repo, runner=runner, steamcmd_path=str(tmp / "sc"))
    bak = m.backup_mod(100).backup
    (tmp / "dl" / "100" / "marker.txt").write_text("current", encoding="utf-8")
    rep = m.restore_backup(bak.id)
    assert not rep.ok and "回退" in rep.error
    dest = tmp / "dl" / "100"
    # 回退成功：当前版本原样（marker 还在），无半成品、无改名残留
    assert (dest / "marker.txt").is_file()
    assert not (dest / "data.txt").is_file()
    assert not [p for p in (tmp / "dl").iterdir() if "_restore_old_" in p.name]


def test_restore_without_current_content(env):
    repo, tmp = env
    m = _mgr(repo, tmp)
    bak = m.backup_mod(100).backup
    shutil.rmtree(tmp / "dl" / "100")
    rep = m.restore_backup(bak.id)
    assert rep.ok and rep.pre_backup is None
    assert any("跳过恢复前备份" in w for w in rep.warnings)
    assert (tmp / "dl" / "100" / "data.txt").is_file()


# ---------- 删除 ----------

def test_delete_removes_disk_and_record(env):
    repo, tmp = env
    m = _mgr(repo, tmp)
    bak = m.backup_mod(100).backup
    target = tmp / "bak" / bak.backup_path
    ok, msg = m.delete_backup(bak.id)
    assert ok and msg is None
    assert not target.exists()
    assert repo.get_backup(bak.id) is None


def test_delete_record_only_when_disk_gone(env):
    repo, tmp = env
    m = _mgr(repo, tmp)
    bak = m.backup_mod(100).backup
    shutil.rmtree(tmp / "bak" / bak.backup_path)
    ok, msg = m.delete_backup(bak.id)
    assert ok and msg and "仅删除了记录" in msg
    assert repo.get_backup(bak.id) is None


def test_delete_unknown_id_raises(env):
    repo, tmp = env
    with pytest.raises(ValueError):
        _mgr(repo, tmp).delete_backup(999)


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_delete_refuses_junction(env):
    # R4：备份位置本身是 junction → 拒绝动手，账目不动
    repo, tmp = env
    m = _mgr(repo, tmp)
    bak = m.backup_mod(100).backup
    target = tmp / "bak" / bak.backup_path
    shutil.rmtree(target)
    decoy = tmp / "decoy"
    decoy.mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(decoy)],
                       capture_output=True)
    assert r.returncode == 0
    try:
        ok, msg = m.delete_backup(bak.id)
        assert not ok and "拒绝操作链接/junction" in msg

        assert repo.get_backup(bak.id) is not None
    finally:
        os.rmdir(target)  # 只摘 junction 本体（rmtree 会穿进实体，踩坑 ⑩ 邻居）
# ---------- T18 回归钉子：版本未知必须被拒 ----------
@pytest.mark.parametrize("no_version", [None, 0])
def test_backup_rejects_version_unknown_downloaded(env, no_version):
    """T18/决策 24：手动确认入账的 mod（downloaded + 无本地版本）
    必须被备份引擎拒绝，且绝不碰 robocopy、绝不入库。

    世界状态：账里 100 号有版本（fixture 建的）；另建一个 downloaded
    但无版本的 mod（None = 手动确认的实况，0 = 同样不合格）。
    盘上没有它的内容目录也无所谓——版本守卫在内容守卫之前
    （test_backup_requires_known_version 已实证此顺序）。
    """
    repo, tmp = env
    repo.add_mod(Mod(mod_id=300, game_id=294100,
                     status="downloaded", local_timeupdated=no_version))
    calls: list[tuple[str, str]] = []

    def counting_runner(src, dst):  # 记账版假 robocopy
        calls.append((str(src), str(dst)))
        return _ok_runner(src, dst)

    rep = BackupManager(repo, runner=counting_runner,
                        steamcmd_path=str(tmp / "sc")).backup_mod(300)
    assert not rep.ok and rep.backup is None
    assert "版本未知" in rep.error
    # 文案必须指路"重下回填"，不许指"去扫描"死路（T18：它不在 acf 里）
    assert "重新下载" in rep.error
    assert calls == []  # 引擎层面直接拒绝，robocopy 一次都没被调
    assert repo.list_backups(oldest_first=True) == []  # 账上无记录
