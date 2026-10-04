"""commandBuilder 校验重下（validate）命令的单元测试。"""
"""
只测纯函数，与 test_commandBuilder.py 同款口径：不碰数据库和界面。
核心断言：validate 命令 = 普通命令 + 行尾 " validate" 一个词，其余全同。
"""
from core.commandBuilder import (
    build_copy_text,
    build_plain_commands,
    build_validate_commands,
    build_validate_copy_text,
)


def test_validate_commands_exact_format():
    assert build_validate_commands(1158310, [2753176859]) == [
        "workshop_download_item 1158310 2753176859 validate",
    ]


def test_validate_commands_keeps_given_order():
    out = build_validate_commands(1158310, [3, 1, 2])
    # 每行第 3 个词是 mod id：workshop_download_item / 1158310 / <id> / validate
    assert [line.split()[2] for line in out] == ["3", "1", "2"]


def test_validate_commands_empty():
    assert build_validate_commands(1158310, []) == []


def test_validate_copy_text_joins_with_trailing_newline():
    text = build_validate_copy_text(1158310, [1, 2])
    assert text == ("workshop_download_item 1158310 1 validate\n"
                    "workshop_download_item 1158310 2 validate\n")


def test_validate_copy_text_empty_is_empty_string():
    # 空清单就该是空文本，不能是一个孤立的换行符
    assert build_validate_copy_text(1158310, []) == ""


def test_validate_line_equals_plain_line_plus_validate():
    # 与普通命令逐条对照：差别只有行尾的 validate 一个词
    ids = [7, 8]
    plain = build_plain_commands(1158310, ids)
    validate = build_validate_commands(1158310, ids)
    assert [p + " validate" for p in plain] == validate
    # 顺手钉死：普通复制文本【不含】validate（日常更新不加，Won't 条目）
    assert " validate" not in build_copy_text(1158310, ids)
