"""备份引擎 v2 适配的测试
"""
"""
只测本轮改动相关的行为，不追求覆盖引擎全部（robocopy 本体由真机冒烟覆盖）。
两个测试技巧说明：
- robocopy 用假 runner 注入（构造参数 runner），退出码 1 = 成功（R5：
  1 是"有文件被拷贝"的正常成功码）、8 = 失败；假 runner 不真拷文件，
  所以断言对象是"账本记了什么、报告说了什么"，不是文件系统拷贝结果；
- steamcmd_running 一律 monkeypatch 成 False：万一开发机上恰好在跑
  steamcmd，恢复会被引擎拒绝，测试就会无故翻车——测的是引擎逻辑，
  不该看环境脸色。
keep_per_mod 一律给 3：引擎默认是 1，而"恢复前备份"会新增一条记录，
1 份保留会把刚要恢复的那份当作"最旧"清掉——那是保留策略的正确行为，
但会搅了恢复测试的局（V1 页面传的就是设置页的 3，这里与真值对齐）。
"""
import shutil

import pytest

import core.backupManager as bm
from core.backupManager import BackupManager
from core.models import Mod
from core.sqliteRepository import SQLiteRepository


# ---------- 公共小件 ----------

@pytest.fixture(autouse=True)
def _no_steamcmd(monkeypatch):
    """所有测试里 steamcmd 一律视为没在跑（理由见文件头）。"""
    monkeypatch.setattr(bm, "steamcmd_running", lambda: False)


def _ok_runner(src, dst):
    """假 robocopy：返回正常成功码 1，并且真的把目录拷过去。

    为什么要真拷：v2 恢复流程第一步就核对"备份目录在盘上存在"——
    只返回成功码不落盘的话，backup_mod 造出来的记录是孤儿记录，
    恢复当场被守卫拦下（第一版测试失败就是这个原因：不是引擎的
    问题，是替身学得不像真 robocopy）。dirs_exist_ok=True 对应
    robocopy 的合并语义：目标已存在时往里添，不报错。
    """
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return 1, "ok"


def _fail_runner(src, dst):
    """假 robocopy：返回失败码 8。"""
    return 8, "boom"


def _make_repo(tmp_path):
    """临时账本：空库开箱自动建表（schema 自带 user_version=1）。"""
    return SQLiteRepository(tmp_path / "t.db")


def _add_game(repo, tmp_path):
    """建档：下载目录、备份目录都指到临时目录里，不碰真机。"""
    repo.add_game(42, "测试游戏", str(tmp_path / "content"),
                  backup_dir=str(tmp_path / "backups"))


def _add_mod(repo, *, mod_id=1001, version=111, status="downloaded"):
    """入一个 mod。version=None = 版本未知（confirmed_version 为空）。"""
    repo.add_mod(Mod(mod_id=mod_id, game_id=42, status=status,
                     confirmed_version=version, title="测试mod"))


def _mk_content(tmp_path, mod_id=1001, text="旧内容"):
    """造当前下载内容（一个带文件的文件夹）。"""
    d = tmp_path / "content" / str(mod_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "save.txt").write_text(text, encoding="utf-8")
    return d


def _mk_backup_record(repo, tmp_path, *, mod_id=1001, name, version=5):
    """手工登记一条备份记录 + 在备份区造出对应目录（恢复的"来源"）。"""
    rec = repo.add_backup(mod_id, name, 1, version)
    d = tmp_path / "backups" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.txt").write_text("备份内容", encoding="utf-8")
    return rec


# ---------- 主动备份的版本未知守卫 ----------

def test_backup_rejects_version_unknown(tmp_path):
    """版本未知（确认值为空）的 mod 主动备份必须拒绝，且不入账。
    账本只存有版本锚的备份——这是 D14 的守卫面。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=None)
    _mk_content(tmp_path)  # 盘上有内容，但版本说不清

    mgr = BackupManager(repo, keep_per_mod=3, runner=_ok_runner)
    rep = mgr.backup_mod(1001)

    assert not rep.ok
    assert "本地版本未知" in (rep.error or "")
    assert repo.list_backups() == []  # 账上什么都没记


def test_backup_records_confirmed_version(tmp_path):
    """正常备份：登记的版本 = 确认值；manifest 列 v2 无来源，记 NULL。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=111)
    _mk_content(tmp_path)

    mgr = BackupManager(repo, keep_per_mod=3, runner=_ok_runner)
    rep = mgr.backup_mod(1001)

    assert rep.ok
    assert rep.backup.version_timeupdated == 111
    assert rep.backup.manifest is None
    assert len(repo.list_backups()) == 1
    # 两层结构：登记的是 "<mod编号>/<目录名>" 两段式相对路径
    assert rep.backup.backup_path.startswith("1001/")
    assert "/" in rep.backup.backup_path



# ---------- 恢复前备份的两分 ----------

def test_restore_prebackup_registered_when_version_known(tmp_path):
    """版本已知：恢复前先正经备份当前内容（入账），恢复照常成功。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=111)
    _mk_content(tmp_path, text="第一版")

    mgr = BackupManager(repo, keep_per_mod=3, runner=_ok_runner)
    first = mgr.backup_mod(1001)          # 先造一份可恢复的备份
    assert first.ok

    _mk_content(tmp_path, text="第二版")  # 当前内容变了
    n_before = len(repo.list_backups())   # = 1

    rep = mgr.restore_backup(first.backup.id)

    assert rep.ok
    assert rep.pre_backup is not None      # 恢复前备份走了正路、入了账
    assert rep.pre_backup.version_timeupdated == 111
    assert rep.pre_copy_dir is None        # 没走副本那条路
    assert len(repo.list_backups()) == n_before + 1


def test_restore_prebackup_unregistered_when_version_unknown(tmp_path):
    """版本未知：恢复不拦（保命副本只落盘不入账），账面零变化。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=None)           # 版本未知，但盘上有内容
    _mk_content(tmp_path)
    rec = _mk_backup_record(repo, tmp_path, name="1001_v5_20240101_000000")

    n_before = len(repo.list_backups())    # = 1（只有那份手工登记的）
    mgr = BackupManager(repo, keep_per_mod=3, runner=_ok_runner)
    rep = mgr.restore_backup(rec.id)

    assert rep.ok
    assert rep.pre_backup is None          # 没有入账的恢复前备份
    assert rep.pre_copy_dir is not None    # 副本位置带回报告
    assert "v未知" in str(rep.pre_copy_dir)
    assert any("保命副本" in w for w in rep.warnings)
    assert len(repo.list_backups()) == n_before  # 账面一份没多


def test_restore_aborts_when_prebackup_fails(tmp_path):
    """版本未知 + 副本复制失败 → 中止恢复，盘面分毫未动。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=None)
    content = _mk_content(tmp_path, text="不能丢的内容")
    rec = _mk_backup_record(repo, tmp_path, name="1001_v5_20240101_000000")

    mgr = BackupManager(repo, keep_per_mod=3, runner=_fail_runner)
    rep = mgr.restore_backup(rec.id)

    assert not rep.ok
    assert "已中止恢复" in (rep.error or "")
    assert rep.pre_copy_dir is None
    assert (content / "save.txt").read_text(encoding="utf-8") == "不能丢的内容"
    assert len(repo.list_backups()) == 1        # 账面也没动
    # 没有改名挪走的旧目录残留
    leftovers = [p.name for p in content.parent.iterdir()
                 if p.name.startswith("1001_restore_old")]
    assert leftovers == []


def test_restore_skips_prebackup_without_local_content(tmp_path):
    """盘上没有当前内容：跳过恢复前备份（没东西可保），恢复照走。"""
    repo = _make_repo(tmp_path)
    _add_game(repo, tmp_path)
    _add_mod(repo, version=111)            # 有版本、没内容
    rec = _mk_backup_record(repo, tmp_path, name="1001_v5_20240101_000000")

    mgr = BackupManager(repo, keep_per_mod=3, runner=_ok_runner)
    rep = mgr.restore_backup(rec.id)

    assert rep.ok
    assert rep.pre_backup is None
    assert rep.pre_copy_dir is None
    assert any("跳过恢复前备份" in w for w in rep.warnings)
