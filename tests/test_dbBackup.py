"""数据库备份（R16 backup_to）测试
"""
r"""
backup_to 的关键性质逐条钉死：
- 在线备份 API 产出的是完整可用库（数据 + user_version 都在，
  备份文件直接用 SQLiteRepository 打开不触发重建）
- 每份备份是时间点快照：备份后再写的数据不出现在旧备份里
- 滚动清理只留最新 N 份、只删本工具命名模式的文件
- 事务中禁止备份
不依赖 Windows 特有设施，全平台跑。
"""
import pytest

from core.models import Mod
from core.sqliteRepository import SQLiteRepository


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "t.db")
    r.add_game(294100, "RimWorld", download_dir=str(tmp_path / "dl"))
    yield r
    r.close()


def test_backup_creates_valid_database(repo, tmp_path):
    repo.add_mod(Mod(mod_id=1001, game_id=294100, status="downloaded",
                     local_timeupdated=1700000000))
    out = repo.backup_to(tmp_path / "db_bak")
    assert out.is_file() and out.suffix == ".db"
    # 备份文件本身就是完整可用的库：直接当 repo 打开（user_version
    # 随数据页复制，不会触发重建表），数据原样能查
    restored = SQLiteRepository(out)
    try:
        assert restored.get_game(294100).name == "RimWorld"
        assert restored.get_mod(1001).local_timeupdated == 1700000000
    finally:
        restored.close()


def test_backup_is_point_in_time_snapshot(repo, tmp_path):
    bdir = tmp_path / "db_bak"
    repo.add_mod(Mod(mod_id=1, game_id=294100))
    first = repo.backup_to(bdir)
    repo.add_mod(Mod(mod_id=2, game_id=294100))
    second = repo.backup_to(bdir)
    assert first != second and first.is_file() and second.is_file()
    r1 = SQLiteRepository(first)
    try:
        assert r1.get_mod(1) is not None
        assert r1.get_mod(2) is None       # 备份之后写的数据不在旧份里
    finally:
        r1.close()
    r2 = SQLiteRepository(second)
    try:
        assert r2.get_mod(2) is not None
    finally:
        r2.close()


def test_rolling_keep(repo, tmp_path):
    bdir = tmp_path / "db_bak"
    outs = [repo.backup_to(bdir, keep=2) for _ in range(3)]
    # 同一秒内连做 3 次：靠 _2/_3 后缀防覆盖，三份都曾真实落盘
    assert len({o.name for o in outs}) == 3
    assert not outs[0].exists()            # 最旧的被清掉
    assert outs[1].exists() and outs[2].exists()


def test_prune_leaves_foreign_files_alone(repo, tmp_path):
    bdir = tmp_path / "db_bak"
    bdir.mkdir()
    stranger = bdir / "我的重要文件.txt"
    stranger.write_text("别删我", encoding="utf-8")
    # 库名不匹配本工具命名模式（库叫 t.db，模式要求 t_ 开头）→ 不碰
    other_db = bdir / "other_20200101_000000.db"
    other_db.write_text("", encoding="utf-8")
    for _ in range(4):
        repo.backup_to(bdir, keep=1)
    assert stranger.exists() and other_db.exists()
    # 本工具自己的份只留 1 份
    mine = [p for p in bdir.iterdir() if p.name.startswith("t_")]
    assert len(mine) == 1


def test_backup_forbidden_in_transaction(repo, tmp_path):
    with pytest.raises(RuntimeError):
        with repo.transaction():
            repo.backup_to(tmp_path / "db_bak")
