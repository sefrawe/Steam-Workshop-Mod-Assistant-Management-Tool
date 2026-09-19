"""网址解析器测试"""
"""解析规则用例 + fixtures 真实 Edge 导出样本的端到端断言。"""
from pathlib import Path

from core.urlParser import extract_mod_id, parse_lines

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_standard_url():
    assert extract_mod_id(
        "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213") == 3403925213


def test_url_with_extra_params():
    assert extract_mod_id(
        "https://steamcommunity.com/sharedfiles/filedetails/?l=english&id=2216850785&searchtext=x") == 2216850785


def test_plain_number():
    assert extract_mod_id("  3403925213 ") == 3403925213


def test_garbage_and_foreign_domain():
    assert extract_mod_id("") is None
    assert extract_mod_id("随便一行文字") is None
    assert extract_mod_id("https://example.com/?id=1") is None   # 非工坊域名不放行


def test_parse_dedup_and_invalid():
    r = parse_lines([
        "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213",
        "3403925213",            # 重复 → 去重
        "https://steamcommunity.com/sharedfiles/filedetails/?id=2216850785",
        "",
        "随便一行文字",
    ])
    assert r.mod_ids == [3403925213, 2216850785]
    assert r.invalid == ["随便一行文字"]


def test_space_separated_single_line():
    r = parse_lines(["https://steamcommunity.com/sharedfiles/filedetails/?id=1 "
                     "https://steamcommunity.com/sharedfiles/filedetails/?id=2"])
    assert r.mod_ids == [1, 2]


def test_edge_export_sample():
    text = (FIXTURES / "Edge浏览器网址_20260907_225539.txt").read_text(
        encoding="utf-8-sig", errors="replace")
    r = parse_lines(text.splitlines())
    assert r.mod_ids == [3403925213, 3684849062, 3732584001, 3769843893, 2216850785]
    assert r.invalid == []
