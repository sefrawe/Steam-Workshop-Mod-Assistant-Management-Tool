"""批量打开工坊页面 · 分类逻辑测试（classify_open_targets 全程零写入）"""

import pytest

from core.models import Game, Mod
from core.sqliteRepository import SQLiteRepository
from gui.batchOpenDialog import classify_open_targets

CK3_APP = 1158310
_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "t.db")
    yield r
    r.close()


@pytest.fixture()
def ck3(repo):
    repo.add_game(CK3_APP, "十字军之王3", rf"E:\dl\{CK3_APP}")
    return repo.get_game(CK3_APP)


def test_mixed_buckets_order_dedupe(repo, ck3):
    repo.add_mod(Mod(mod_id=100, game_id=CK3_APP))
    text = ("https://steamcommunity.com/sharedfiles/filedetails/?id=100\n"
            "200\n"
            "垃圾行\n"
            "300 100")  # 重复编号：parse_lines 自动去重
    rep = classify_open_targets(repo, text)
    assert rep.parsed_ids == [100, 200, 300]
    assert rep.tracked_ids == [100]
    assert rep.untracked_ids == [200, 300]
    assert rep.invalid_lines == ["垃圾行"]
    assert rep.urls() == [_URL.format(100), _URL.format(200), _URL.format(300)]


def test_tracked_in_other_archive_counts_as_tracked(repo, ck3):
    """全库对表不分档案：条目在其他档案名下也算"已在账本"。"""
    repo.add_game(72850, "其他游戏", r"E:\dl\72850")
    repo.add_mod(Mod(mod_id=100, game_id=72850))
    rep = classify_open_targets(repo, "100")
    assert rep.tracked_ids == [100] and rep.untracked_ids == []


def test_steamcmd_command_line_accepted(repo, ck3):
    """urlParser 单源红利：整行 steamcmd 命令照认（AppID 不会混进清单）。"""
    repo.add_mod(Mod(mod_id=200, game_id=CK3_APP))
    rep = classify_open_targets(repo, "+workshop_download_item 1158310 200")
    assert rep.parsed_ids == [200] and rep.tracked_ids == [200]
    assert rep.invalid_lines == []


def test_empty_text(repo):
    rep = classify_open_targets(repo, "   \n  ")
    assert rep.parsed_ids == [] and rep.urls() == []
