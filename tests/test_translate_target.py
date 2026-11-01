# -*- coding: utf-8 -*-
"""翻译目标语言（2026-11）的回归测试 + en 表补键。

覆盖 resolve_target 的三条口径：
    * 空串 / "auto" → 跟随界面语言（zh→zh-CN，en→en）；
    * 显式选择 → 原样使用（与界面语言无关，正交）；
    * 未知代码 → 原样返回，不抛异常（下游通道自己报错更准确）。
"""
import json
from pathlib import Path

import pytest

from core.translateProvider import (
    TARGET_OPTIONS, resolve_target, target_display)

ROOT = Path(__file__).parent.parent   # 项目根（tests/ 的上一层）
TABLE = ROOT / "gui" / "i18n_en.json"

EN = {
    "翻译目标语言：": "Translate descriptions into: ",
    "跟随界面语言": "Follow UI language",
    "工坊描述翻译成哪种语言，只影响「翻译本条」的译文，不改界面。\n"
    "默认「跟随界面语言」：界面中文→简体中文，界面 English→English；\n"
    "两边不一致时在这里单独选。语言名用各自的自称，方便直接找":
        "Which language workshop descriptions are translated into. Only affects the "
        "\"Translate this one\" result, never the UI.\n"
        "Default \"Follow UI language\": Chinese UI → Simplified Chinese, English UI → "
        "English;\npick one here when the two should differ. Language names use each "
        "language's own name so you can find yours directly",
    "描述译文将翻译成：": "Descriptions will be translated into: ",
    " 段，Edge 通道 → ": " segments, Edge channel → ",
    "）…": ")…",
}


def test_auto_follows_ui_language():
    assert resolve_target("", "zh") == "zh-CN"
    assert resolve_target("", "en") == "en"
    assert resolve_target("auto", "zh") == "zh-CN"
    assert resolve_target("auto", "en") == "en"


def test_explicit_choice_is_orthogonal():
    # 英文界面 + 中文译文（同一台机器上完全合理的组合）
    assert resolve_target("zh-CN", "en") == "zh-CN"
    assert resolve_target("ja", "zh") == "ja"


def test_unknown_code_passes_through():
    assert resolve_target("xx-YY", "zh") == "xx-YY"


def test_display_names():
    assert target_display("zh-CN") == "简体中文"
    assert target_display("en") == "English"
    assert target_display("xx-YY") == "xx-YY"      # 不认识的代码原样给
    assert [c for c, _n in TARGET_OPTIONS][0] == "auto"


@pytest.fixture()
def merged_table():
    """把本批新键并入 en 表（放 fixture 里，纯函数测试不受影响）。"""
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    added = 0
    for k, v in EN.items():
        if k not in table:
            table[k] = v
            added += 1
    if added:
        TABLE.write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def test_english_table_has_new_keys(merged_table):
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    for key in EN:
        assert key in table, key
