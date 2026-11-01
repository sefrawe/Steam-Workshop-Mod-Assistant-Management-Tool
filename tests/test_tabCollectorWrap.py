"""collect_tabs 绕回判定的回归测试（2026-10-31 修"中间夹空网址提前
收工"轮）：用脚本化假驱动全速走查，不碰真 Edge。只测纯策略——绕回
该不该停、半途成果照常返回；中断/鼠标刹车/依赖体检不在此列。

背景：绕回判定经历三轮踩坑——
1. 起点空白页 → 旧 guard `start_url is not None` 令判定永久失效，死绕 300 步；
2. 改 (标题,网址) 直比 → 起点空白 + 中间又夹空白页时，第二个空白页
   与起点标题/网址全同，被误判"绕回"提前收工，漏掉后面标签；
3. 本轮改"本轮已见过的真网址再次出现"——空白页没唯一指纹一律跳过、
   只记真网址，真网址重复出现 = 真绕回一圈。
本文件把这三种场景 + 跨窗口重叠钉死，防第四轮。
"""
import pytest

from core import tabCollector


class _FakeDriver:
    """脚本化假驱动：windows = [[(url_or_None, title), ...], ...]。
    剪贴板舞步照 real 走：ctrl+c 把当前标签网址塞进剪贴板（空白页
    给 "about:blank" 这个非 http 串），_current_url 据此返回网址或
    None——空白页走"非 http"分支快速返回，测试不打空转。"""

    def __init__(self, windows):
        self._windows = windows
        self._win = 0
        self._tab = 0
        self._clip = ""

    def find_windows(self):
        return list(range(len(self._windows)))

    def focus(self, win):
        self._win = win
        self._tab = 0

    def _cur(self):
        return self._windows[self._win][self._tab]

    def hotkey(self, *keys):
        k = tuple(keys)
        if k == ("ctrl", "1"):
            self._tab = 0
        elif k == ("ctrl", "pgdn"):
            self._tab = (self._tab + 1) % len(self._windows[self._win])
        elif k == ("ctrl", "c"):
            url = self._cur()[0]
            self._clip = url if url else "about:blank"

    def window_title(self, win):
        return self._cur()[1]

    def clipboard_get(self):
        return self._clip

    def clipboard_set(self, x):
        self._clip = x

    def sleep(self, seconds):
        pass


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    # 本文件只关心绕回策略，不关心 0.05 秒轮询睡眠——把等待打平成全速
    monkeypatch.setattr(tabCollector, "_sleep_polling",
                        lambda driver, seconds, should_stop: False)


def _collect(windows):
    d = _FakeDriver(windows)
    return [t.url for t in tabCollector.collect_tabs(d, should_stop=lambda: False)]


def test_blank_start_single():
    # 起点一个空白页 + 三个真网址：空白起点不能当绕回信号，但真网址
    # 绕一圈回来要能停（第一轮修的 bug）
    assert _collect([
        [(None, "新标签页"), ("https://a.com/1", "A1"),
         ("https://a.com/2", "A2"), ("https://a.com/3", "A3")],
    ]) == ["https://a.com/1", "https://a.com/2", "https://a.com/3"]


def test_blank_start_and_blank_middle():
    # 起点空白 + 中间再夹一个空白：碰到中间空白页绝不能提前收工，
    # 后面两个真网址也要采全（本轮修的 bug）
    assert _collect([
        [(None, "新标签页"), ("https://b.com/1", "B1"),
         ("https://b.com/2", "B2"), (None, "新标签页"),
         ("https://b.com/3", "B3"), ("https://b.com/4", "B4")],
    ]) == ["https://b.com/1", "https://b.com/2",
           "https://b.com/3", "https://b.com/4"]


def test_real_start_wraps():
    # 起点就是真网址：绕回即停，不多跑
    assert _collect([
        [("https://c.com/1", "C1"), ("https://c.com/2", "C2"),
         ("https://c.com/3", "C3")],
    ]) == ["https://c.com/1", "https://c.com/2", "https://c.com/3"]


def test_cross_window_overlap():
    # 两个窗口网址有交集：绕回判定按"本轮"（窗口内）算，不能把
    # 上一窗口采过的网址误当本窗口的绕回信号
    assert _collect([
        [("https://d.com/1", "D1"), ("https://d.com/2", "D2")],
        [("https://d.com/2", "D2"), ("https://d.com/3", "D3")],
    ]) == ["https://d.com/1", "https://d.com/2", "https://d.com/3"]
