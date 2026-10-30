"""FirefoxSessionDriver 的会话解析测试（不依赖真 Firefox）。

会话文件用合成 JSON / 合成 mozLz4 直塞临时目录（profile_root 注入），
覆盖：当前页提取、去重、scheme 门槛、无会话报错、previous 兜底、
新档优先、损坏档跳过、300 上限、真实压缩格式、平台门卫分派。
"""
import json
import sys
import time

import pytest

from core import tabCollector
from core.tabCollector import CollectError, FirefoxSessionDriver


def _entry(url: str, title: str = "T") -> dict:
    return {"url": url, "title": title}


def _make_session(root, prof: str, tabs, name="recovery.jsonlz4") -> None:
    """tabs = [(entries, index), ...]；写未压缩 JSON（驱动兼容口径）。"""
    ss = root / prof / "sessionstore-backups"
    ss.mkdir(parents=True, exist_ok=True)
    data = {"windows": [
        {"tabs": [{"entries": e, "index": i} for (e, i) in tabs]}]}
    (ss / name).write_bytes(json.dumps(data).encode("utf-8"))


def test_current_page_only_not_history(tmp_path):
    _make_session(tmp_path, "p.default", [
        ([_entry("https://steamcommunity.com/app/294100"),
          _entry("https://steamcommunity.com/sharedfiles/filedetails/?id=111",
                 "H2")], 2),
        ([_entry("https://example.com/")], 1),
    ])
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == [
        "https://steamcommunity.com/sharedfiles/filedetails/?id=111",
        "https://example.com/"]
    assert tabs[0].title == "H2"


def test_dedupe_and_scheme_gate(tmp_path):
    _make_session(tmp_path, "p.default", [
        ([_entry("https://a.com/1")], 1),
        ([_entry("https://a.com/1")], 1),      # 重复网址
        ([_entry("about:blank")], 1),          # 浏览器内部页
        ([_entry("file:///C:/x")], 1),         # 非http(s)
        ([_entry("http://b.com/2")], 1),
    ])
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == ["https://a.com/1", "http://b.com/2"]


def test_no_session_raises(tmp_path):
    with pytest.raises(CollectError):
        FirefoxSessionDriver(profile_root=tmp_path).collect()


def test_falls_back_to_previous(tmp_path):
    _make_session(tmp_path, "p.default",
                  [([_entry("https://a.com/")], 1)],
                  name="previous.jsonlz4")
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == ["https://a.com/"]


def test_newest_profile_first(tmp_path):
    _make_session(tmp_path, "old.default",
                  [([_entry("https://old.com/")], 1)])
    time.sleep(0.02)
    _make_session(tmp_path, "new.default",
                  [([_entry("https://new.com/")], 1)])
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == ["https://new.com/", "https://old.com/"]


def test_corrupt_file_skipped(tmp_path):
    bad = tmp_path / "bad.default" / "sessionstore-backups"
    bad.mkdir(parents=True)
    (bad / "recovery.jsonlz4").write_bytes(b"\x00garbage\x00")
    _make_session(tmp_path, "good.default",
                  [([_entry("https://ok.com/")], 1)])
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == ["https://ok.com/"]


def test_max_tabs_cap(tmp_path):
    _make_session(tmp_path, "p.default",
                  [([_entry(f"https://x.com/{i}")], 1) for i in range(10)])
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect(max_tabs=3)
    assert len(tabs) == 3


def test_mozlz4_compressed(tmp_path):
    pytest.importorskip("lz4")
    import lz4.block
    payload = json.dumps({"windows": [{"tabs": [{
        "entries": [_entry("https://z.com/", "Z")], "index": 1}]}]}
    ).encode("utf-8")
    comp = lz4.block.compress(payload, store_size=False)
    f = tmp_path / "p.default" / "sessionstore-backups" / "recovery.jsonlz4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"mozLz40\0" + len(payload).to_bytes(4, "little") + comp)
    tabs = FirefoxSessionDriver(profile_root=tmp_path).collect()
    assert [t.url for t in tabs] == ["https://z.com/"]
    assert tabs[0].title == "Z"


def test_preflight_platform_dispatch():
    """门卫分派：非 Windows 一律放行（会话直读不依赖那四包）；
    Windows 视安装情况返回 None 或文案——两端都不许抛异常。"""
    msg = tabCollector.missing_packages_message()
    if sys.platform != "win32":
        assert msg is None
