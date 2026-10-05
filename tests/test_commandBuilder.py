"""commandBuilder 的单元测试：分组规则 + 命令拼装格式。"""
"""
只测纯函数，不碰数据库和界面。
用 SimpleNamespace 伪造 mod 对象，字段名和数据库 Mod 对象保持一致。
"""

from types import SimpleNamespace

from core.commandBuilder import build_copy_text, build_plain_commands, group_mods


def make_mod(mid, *, status="downloaded", remote=100, local=100, title="某mod"):
    """造一个测试用 mod：只带 commandBuilder 用到的 5 个属性。"""
    return SimpleNamespace(
        mod_id=mid, title=title, status=status,
        time_updated=remote, confirmed_version=local,

    )


# ---------------- group_mods：分组规则 ----------------

def test_deleted_and_failed_are_excluded():
    mods = [make_mod(1, status="deleted"), make_mod(2, status="failed")]
    g = group_mods(mods)
    assert all(len(v) == 0 for v in g.values())


def test_no_local_record_goes_to_not_downloaded():
    g = group_mods([make_mod(1, local=0, remote=500)])
    assert len(g["not_downloaded"]) == 1 and len(g["needs_update"]) == 0


def test_local_zero_and_remote_zero_also_not_downloaded():
    # 两个都为 0：先按"本地没有"处理，归未下载
    g = group_mods([make_mod(1, local=0, remote=0)])
    assert len(g["not_downloaded"]) == 1


def test_remote_newer_goes_to_needs_update():
    g = group_mods([make_mod(1, local=100, remote=200)])
    assert len(g["needs_update"]) == 1


def test_remote_unknown_but_has_local_goes_to_needs_update():
    # 本地有文件、从没查过远端：宁可多下一遍也不漏
    g = group_mods([make_mod(1, local=100, remote=0)])
    assert len(g["needs_update"]) == 1


def test_equal_timestamps_goes_to_up_to_date():
    g = group_mods([make_mod(1, local=100, remote=100)])
    assert len(g["up_to_date"]) == 1


def test_remote_older_goes_to_up_to_date():
    g = group_mods([make_mod(1, local=200, remote=100)])
    assert len(g["up_to_date"]) == 1


def test_mixed_input_lands_in_right_buckets():
    mods = [
        make_mod(1, local=0, remote=5),      # 未下载
        make_mod(2, local=10, remote=20),    # 需要更新
        make_mod(3, local=20, remote=20),    # 已最新
        make_mod(4, status="deleted"),       # 不出现
    ]
    g = group_mods(mods)
    assert [m.mod_id for m in g["not_downloaded"]] == [1]
    assert [m.mod_id for m in g["needs_update"]] == [2]
    assert [m.mod_id for m in g["up_to_date"]] == [3]


def test_within_group_sorted_numerically():
    # 字符串排序会得到 10, 100, 9；数字排序必须是 9, 10, 100
    mods = [make_mod(10, local=0), make_mod(9, local=0), make_mod(100, local=0)]
    g = group_mods(mods)
    assert [m.mod_id for m in g["not_downloaded"]] == [9, 10, 100]


def test_empty_input_gives_three_empty_groups():
    g = group_mods([])
    assert g == {"needs_update": [], "not_downloaded": [], "up_to_date": []}


# ---------------- 命令拼装格式 ----------------

def test_plain_commands_exact_format():
    out = build_plain_commands(1158310, [2753176859, 3090564070])
    assert out == [
        "workshop_download_item 1158310 2753176859",
        "workshop_download_item 1158310 3090564070",
    ]


def test_plain_commands_keeps_given_order():
    # 排序是 group_mods 的事，拼装函数只负责按给定顺序出
    out = build_plain_commands(1158310, [3, 1, 2])
    assert [line.split()[-1] for line in out] == ["3", "1", "2"]


def test_plain_commands_empty():
    assert build_plain_commands(1158310, []) == []


def test_copy_text_joins_with_trailing_newline():
    text = build_copy_text(1158310, [1, 2])
    assert text == "workshop_download_item 1158310 1\nworkshop_download_item 1158310 2\n"


def test_copy_text_lines_match_plain_commands():
    lines = build_copy_text(1158310, [7, 8]).splitlines()
    assert lines == build_plain_commands(1158310, [7, 8])
