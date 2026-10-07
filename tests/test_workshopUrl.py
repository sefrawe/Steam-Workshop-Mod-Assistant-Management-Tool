"""tests/test_workshopUrl.py · workshop_url 唯一入口三用例。"""
from core.urlParser import workshop_url


def test_stored_url_wins():
    """账本里登记过网址 → 原样使用。"""
    stored = "https://steamcommunity.com/sharedfiles/filedetails/?id=999"
    assert workshop_url(123, stored) == stored


def test_blank_stored_falls_back():
    """登记值为空白 = 等于没登记 → 现拼。"""
    assert workshop_url(123, "   ") == workshop_url(123, None)


def test_built_from_id():
    assert workshop_url(123) == \
        "https://steamcommunity.com/sharedfiles/filedetails/?id=123"
