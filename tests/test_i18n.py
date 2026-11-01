"""core/i18n 最小回归（2026-11）：zh 直通、en 查表与缺词回退、
语言清单自动发现、未知语言优雅降级。

不读真实配置文件——init_language 显式传语言，绕开 _read_config_lang；
autouse fixture 收尾恢复 zh，避免污染同进程的其他测试。
"""
import pytest

from core import i18n


@pytest.fixture(autouse=True)
def _restore_zh():
    yield
    i18n.init_language("zh")


def test_zh_passthrough():
    i18n.init_language("zh")
    assert i18n.t("从浏览器取网址") == "从浏览器取网址"
    assert i18n.current_lang() == "zh"


def test_en_lookup_and_miss_fallback():
    i18n.init_language("en")
    assert i18n.current_lang() == "en"
    # 表内键：翻出英文（内容随翻译文件演进，不锁具体措辞，
    # 只要求"确实翻了"且非空）
    translated = i18n.t("从浏览器取网址")
    assert translated and translated != "从浏览器取网址"
    # 稳定短键锁一个具体值，防翻译表整体失联还悄悄通过
    assert i18n.t("关于") == "About"
    # 缺词回退原文——混排不炸
    assert i18n.t("不存在的翻译键xyz") == "不存在的翻译键xyz"


def test_available_languages_discovers_en():
    langs = dict(i18n.available_languages())
    assert list(langs)[0] == "zh"           # 源语言恒第一
    assert "en" in langs                    # i18n_en.json 被扫到
    assert langs["en"] == "English"         # 显示名取 __language_name__ 元键
    assert all(isinstance(v, str) and v for v in langs.values())


def test_unknown_lang_degrades_to_passthrough():
    i18n.init_language("xx")                # 没有对应翻译文件
    assert i18n.current_lang() == "xx"
    assert i18n.t("任意文本") == "任意文本"   # 空表→原样返回，不炸
