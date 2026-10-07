"""acf_installed_ids 只读检测两用例（v1.13 F 节欠账清偿）。"""
from core.steamPaths import acf_installed_ids


def test_acf_installed_ids_normal(tmp_path):
    acf = tmp_path / "appworkshop_1158310.acf"
    acf.write_text(
        '"AppWorkshop"\n{\n'
        '\t"appid"\t\t"1158310"\n'
        '\t"WorkshopItemsInstalled"\n\t{\n'
        '\t\t"3563882422"\t\t"timeupdated"\t\t"1700000000"\n'
        '\t}\n'
        '\t"WorkshopItemDetails"\n\t{\n'
        '\t\t"3489112041"\t\t"timeupdated"\t\t"1700000001"\n'
        '\t}\n'
        '}\n', encoding="utf-8")
    # 两块合并；数字形态键转 int
    assert acf_installed_ids(acf) == {3563882422, 3489112041}


def test_acf_installed_ids_unreadable_returns_none(tmp_path):
    # 文件不存在 → None（绝不抛）
    assert acf_installed_ids(tmp_path / "没有.acf") is None
    # 坏文本（括号不齐）→ None（绝不抛）
    bad = tmp_path / "bad.acf"
    bad.write_text('"AppWorkshop" { 括号不齐', encoding="utf-8")
    assert acf_installed_ids(bad) is None
