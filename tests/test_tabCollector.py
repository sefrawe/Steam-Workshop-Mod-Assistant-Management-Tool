"""tests/test_tabCollector.py —— 实时采集引擎测试（假驱动）。"""
"""真实键鼠自动化进不了 pytest；按 DI 只测"走查策略"：切标签循环、
绕回起点判定、去重、http 过滤、上限、可中断、剪贴板恢复。
真驱动 EdgeDriver 是 1.5 验证过的搬运，不进测试。"""
import pytest

from core.tabCollector import CollectError, collect_tabs

T1 = ("Steam 工坊::A", "https://steamcommunity.com/sharedfiles/"
                       "filedetails/?id=1")
T2 = ("Steam 工坊::B", "https://steamcommunity.com/sharedfiles/"
                       "filedetails/?id=2")
T3 = ("新标签页", "edge://newtab/")


class FakeDriver:
    """模拟 OS+浏览器：热键改变内部状态，Alt+D+Ctrl+C 把当前标签
    网址放进剪贴板——collector 只认驱动原语，假即真。"""

    def __init__(self, windows):
        self._tabs = windows                 # 每窗口 [(title, url), ...]
        self._idx = [0] * len(windows)
        self.cur = None
        self._armed = False
        self.clip = "__orig__"
        self.pgdn = 0

    def find_windows(self):
        return list(range(len(self._tabs)))

    def focus(self, win):
        self.cur = win

    def window_title(self, win):
        return self._tabs[win][self._idx[win]][0]

    def hotkey(self, *keys):
        if keys == ("ctrl", "1"):
            self._idx[self.cur] = 0
        elif keys == ("ctrl", "pgdn"):
            self._idx[self.cur] = ((self._idx[self.cur] + 1)
                                   % len(self._tabs[self.cur]))
            self.pgdn += 1
        elif keys == ("alt", "d"):
            self._armed = True
        elif keys == ("ctrl", "c") and self._armed:
            self.clip = self._tabs[self.cur][self._idx[self.cur]][1]

    def clipboard_set(self, text):
        self.clip = text

    def clipboard_get(self):
        return self.clip

    def sleep(self, seconds):
        pass


def test_walks_all_tabs_in_order():
    d = FakeDriver([[T1, T2, ("B页", "https://example.com/x")]])
    tabs = collect_tabs(d)
    assert [t.url for t in tabs] == [T1[1], T2[1], "https://example.com/x"]
    assert [t.title for t in tabs] == [T1[0], T2[0], "B页"]


def test_dedup_and_non_http_skipped():
    d = FakeDriver([[T1, ("同址不同题", T1[1]), T3]])
    assert [t.url for t in collect_tabs(d)] == [T1[1]]


def test_single_tab_no_infinite_loop():
    d = FakeDriver([[T1]])
    assert [t.url for t in collect_tabs(d)] == [T1[1]]


def test_wrap_back_breaks():
    d = FakeDriver([[T1, T2, ("C", "https://e.com/3")]])
    collect_tabs(d)
    assert d.pgdn == 3  # 三个标签恰好切三轮，绕回起点即停


def test_max_tabs_caps_switches():
    d = FakeDriver([[T1, T2, ("C", "https://e.com/3")]])
    collect_tabs(d, max_tabs=2)
    assert d.pgdn == 2  # 上限只限切换次数，不 Infinite


def test_should_stop_returns_partial():
    d = FakeDriver([[T1, T2, ("C", "https://e.com/3")]])
    tabs = collect_tabs(d, should_stop=lambda: d.pgdn >= 1)
    assert [t.url for t in tabs] == [T1[1], T2[1]]  # 半途成果照常返回


def test_clipboard_restored():
    d = FakeDriver([[T1, T2]])
    collect_tabs(d)
    assert d.clip == "__orig__"


def test_no_windows_raises():
    with pytest.raises(CollectError):
        collect_tabs(FakeDriver([]))
