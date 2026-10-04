"""账本导出 / 分享包测试（v2 判决制口径）。"""
"""按模块头承诺逐条钉死：
- 往返一致性：8 张表全覆盖的小世界 → 导出 → 落盘再读回 → 灌进
  新库 → 两库整库快照相等（同一抽取原语下的最强等价断言）
- 分享包三变换：状态重映射 / 剥确认三件套与机器字段 / 排除已删·
  失效·别家
- 增量并入：全新库全收；已有档案复用不改；已有 mod 跳过不动
- 两道版本闸各一例 + 畸形载荷指名道姓 + 失败导入原账无损
verdict_log 的行级往返由确认门（M2）测试覆盖——本文件验通道：
表随账走、空表不缺席、计数如实。
"""
import sqlite3

import pytest

from core.dataExporter import (FORMAT_LEDGER, FORMAT_SHAREPACK,
                               FORMAT_VERSION, build_ledger, build_sharepack,
                               default_filename, import_ledger,
                               import_sharepack, load, save)
from core.models import Mod
from core.sqliteRepository import SQLiteRepository


def make_repo(tmp_path, name="mods.db") -> SQLiteRepository:
    return SQLiteRepository(tmp_path / name, snapshot_keep=5)


def seed(repo: SQLiteRepository) -> None:
    """覆盖 8 张表的最小世界；断言一律按 id 现查，不靠返回值。"""
    repo.add_game(1158310, "Crusader Kings III", r"C:\dl\content\1158310",
                  game_mod_dir=r"C:\Games\CK3\mod")
    repo.add_game(294100, "RimWorld", r"C:\dl\content\294100")
    repo.add_mod(Mod(mod_id=1001, game_id=1158310, status="downloaded",
                     title="奇观mod",
                     url="https://steamcommunity.com/sharedfiles/filedetails/?id=1001",
                     time_created=1680000000, time_updated=1700000000,
                     last_time_updated=1700000000,
                     confirmed_version=1700000000, confirmed_at=1700000100,
                     confirmed_source="verified",
                     local_size=123456, tags=["中文", "平衡"], note="好东西",
                     is_special=True, first_tracked_at=1690000000,
                     last_checked_at=1699000000))
    repo.add_mod(Mod(mod_id=1002, game_id=1158310, status="tracked",
                     title="还没下", time_updated=1700001000))
    repo.add_mod(Mod(mod_id=1003, game_id=294100, status="downloaded",
                     title="边缘世界mod", confirmed_version=1700002000))
    repo.add_mod(Mod(mod_id=1004, game_id=1158310, status="downloaded",
                     title="已删的", confirmed_version=1695000000))
    repo.mark_deleted(1004, {"title": "已删的"})
    repo.add_mod(Mod(mod_id=1005, game_id=1158310, status="tracked",
                     title="已失效的"))
    repo.mark_failed(1005, "测试失效")
    repo.add_snapshot(1001, time_updated=1690000000)
    repo.add_backup(1001, r"C:\bk\1001_v1700000000", 123456, 1700000000,
                    note="保险")
    op_id = repo.add_operation("steamcmd +workshop_download_item 1158310 1001")
    repo.finish_operation(op_id, error_count=0, result="success")
    repo.add_alert(1001, 1700000000, diff_seconds=86400, was_downloaded=True)


# ---------- 往返一致性 ----------

def test_ledger_round_trip_preserves_everything(tmp_path):
    a = make_repo(tmp_path, "a.db")
    seed(a)
    path = save(build_ledger(a), tmp_path / "ledger.json")
    payload = load(path)   # 落盘往返后仍能过全量校验
    b = make_repo(tmp_path, "b.db")
    counts = import_ledger(payload, b)
    assert counts["games"] == 2
    assert counts["mods"] == 5
    assert counts["mod_snapshots"] == 1
    assert counts["backups"] == 1
    assert counts["operations_log"] == 1
    assert counts["failed_mods"] == 1
    assert counts["special_mod_alerts"] == 1
    assert counts["verdict_log"] == 0   # 空表不缺席：通道随账走
    # 最强断言：两库用同一抽取原语倒出来逐表相等（含 user_version、
    # 各表 id 与跨表引用、中文 note、tags 数组、bool 字段）
    assert a.export_all() == b.export_all()


# ---------- 分享包：构建侧三变换 ----------

def test_sharepack_strips_machine_side_keeps_organization(tmp_path):
    repo = make_repo(tmp_path)
    seed(repo)
    payload = build_sharepack(repo, 1158310)
    assert payload["format"] == FORMAT_SHAREPACK
    assert set(payload["tables"]) == {"games", "mods"}
    mods = payload["tables"]["mods"]
    # 1004 已删、1005 已失效、1003 别家：都不外带
    assert sorted(m["mod_id"] for m in mods) == [1001, 1002]
    assert all(m["status"] == "tracked" for m in mods)   # downloaded→tracked
    big = next(m for m in mods if m["mod_id"] == 1001)
    for field in ("confirmed_version", "confirmed_at", "confirmed_source",
                  "local_size", "first_tracked_at", "last_checked_at"):
        assert big[field] is None, field
    assert big["note"] == "好东西"        # 整理成果原样
    assert big["tags"] == ["中文", "平衡"]   # 自然类型
    assert big["is_special"] is True
    assert big["time_updated"] == 1700000000   # 远端事实保留
    game = payload["tables"]["games"][0]
    assert game["name"] == "Crusader Kings III"
    assert game["game_mod_dir"] is None
    assert game["backup_dir"] is None
    assert game["download_dir"] == r"C:\dl\content\1158310"


# ---------- 分享包：导入侧增量并入 ----------

def test_sharepack_import_fresh_then_merge_dedups(tmp_path):
    src = make_repo(tmp_path, "src.db")
    seed(src)
    payload = build_sharepack(src, 1158310)
    fresh = make_repo(tmp_path, "fresh.db")
    report = import_sharepack(payload, fresh)
    assert report == {"app_id": 1158310, "added": 2, "skipped": 0}
    assert fresh.get_game(1158310).name == "Crusader Kings III"
    m = fresh.get_mod(1001)
    assert m.status == "tracked"           # downloaded→tracked
    assert m.confirmed_version is None     # 确认三件套全空：
    assert m.confirmed_at is None          # 接收者的版本等他自己的
    assert m.confirmed_source is None      # 第一批下载走确认门
    assert m.note == "好东西"
    assert m.tags == ["中文", "平衡"]
    # 同包再并一次 + 目标库已有自己的整理：全跳过、一字不改
    fresh.set_note(1002, "朋友自己的备注")
    report2 = import_sharepack(payload, fresh)
    assert report2 == {"app_id": 1158310, "added": 0, "skipped": 2}
    assert fresh.get_mod(1002).note == "朋友自己的备注"


def test_sharepack_import_reuses_existing_game_row(tmp_path):
    src = make_repo(tmp_path, "src.db")
    seed(src)
    payload = build_sharepack(src, 1158310)
    target = make_repo(tmp_path, "target.db")
    target.add_game(1158310, "我的三国志", r"D:\steamcmd\content\1158310")
    target.add_mod(Mod(mod_id=1001, game_id=1158310, status="downloaded",
                       title="我早就有了"))
    report = import_sharepack(payload, target)
    assert report == {"app_id": 1158310, "added": 1, "skipped": 1}
    g = target.get_game(1158310)
    assert g.name == "我的三国志"   # 档案行一字未改
    assert g.download_dir == r"D:\steamcmd\content\1158310"
    assert target.get_mod(1001).title == "我早就有了"   # 已有 mod 不动
    assert target.get_mod(1002).status == "tracked"     # 新 mod 正常入账


# ---------- 两道版本闸 ----------

def test_load_rejects_foreign_format_and_newer_format(tmp_path):
    foreign = {"format": "not-our-tool", "format_version": 1,
               "schema_version": 1, "tables": {}}
    with pytest.raises(ValueError, match="不是本工具"):
        load(save(foreign, tmp_path / "foreign.json"))
    future = {"format": FORMAT_LEDGER, "format_version": FORMAT_VERSION + 1,
              "schema_version": 1, "tables": {}}
    with pytest.raises(ValueError, match="升级"):
        load(save(future, tmp_path / "future.json"))


def test_import_rejects_newer_schema_version(tmp_path):
    repo = make_repo(tmp_path)
    seed(repo)
    before = repo.export_all()
    payload = build_ledger(repo)
    payload["schema_version"] = before["user_version"] + 1
    with pytest.raises(ValueError, match="升级"):
        import_ledger(payload, repo)
    assert repo.export_all() == before   # 拒收发生在动手之前，原账无损


# ---------- 畸形载荷与回滚 ----------

def test_load_names_the_broken_row(tmp_path):
    def payload_with(mods_rows):
        return {"format": FORMAT_LEDGER, "format_version": FORMAT_VERSION,
                "schema_version": 1, "exported_at": 1735689600,
                "tables": {"games": [], "mods": mods_rows,
                           "mod_snapshots": [], "backups": [],
                           "operations_log": [], "failed_mods": [],
                           "special_mod_alerts": [], "verdict_log": []}}

    missing = payload_with([{"game_id": 1, "status": "tracked"}])
    with pytest.raises(ValueError, match=r"mods\[0\] 缺少必要字段 mod_id"):
        load(save(missing, tmp_path / "a.json"))
    wrong_type = payload_with(
        [{"mod_id": "1001", "game_id": 1, "status": "tracked"}])
    with pytest.raises(ValueError, match=r"mods\[0\]\.mod_id 应为整数"):
        load(save(wrong_type, tmp_path / "b.json"))


def test_failed_import_rolls_back_completely(tmp_path):
    repo = make_repo(tmp_path)
    seed(repo)
    before = repo.export_all()
    payload = build_ledger(repo)
    payload["tables"]["mods"][0]["game_id"] = 424242   # 指向不存在的档案
    with pytest.raises(sqlite3.IntegrityError):
        import_ledger(payload, repo)
    assert repo.export_all() == before   # 整体回滚：原账一字未损


# ---------- 便利函数 ----------

def test_default_filename(tmp_path):
    repo = make_repo(tmp_path)
    seed(repo)
    assert default_filename(build_ledger(repo)).startswith("账本备份_")
    name = default_filename(build_sharepack(repo, 1158310))
    assert "Crusader Kings III" in name and "1158310" in name
    repo.add_game(42, "坏:名字/含*非法", r"C:\x")
    name2 = default_filename(build_sharepack(repo, 42))
    for ch in '<>:"/\\|?*':
        assert ch not in name2
