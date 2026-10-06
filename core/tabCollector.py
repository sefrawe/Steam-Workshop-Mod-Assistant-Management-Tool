"""实时采集浏览器标签引擎（键盘自动化路线）"""
"""core/tabCollector.py · 旧脚本 1.5 的转正版：逐窗遍历、Ctrl+PgDn
切标签、Alt+D+Ctrl+C 偷地址栏、剪贴板哨兵——走查逻辑与 1.5 相同，
但按依赖注入重构：驱动原语（找窗/聚焦/热键/剪贴板/睡眠）全部注入，
本文件只写"走查策略"，pytest 用假驱动全速覆盖；真驱动 EdgeDriver
是 1.5 已在真机多日验证的代码搬运，不改逻辑。

为什么转正：会话文件路线（browserSessionReader）在 Edge 当前版本
（SNSS v3）上三轮真机验证 0 结果——块流不可读、zlib 假设失败、
零明文网址。用户旧 1.5 同机多日稳定，事实胜于考古。

代价（界面文案同步声明）：
- 采集期间占用鼠标键盘（Edge 窗口自己切换，人别动）；
- 紧急刹车两条：Alt+Tab 切回本工具点【停止采集】；或鼠标甩到
  屏幕左上角（pyautogui 自带 FAILSAFE，会抛异常终止采集）；
- 依赖 4 个包，import 惰性——没装不影响程序其他功能，点采集时
  才提示一次性安装。

相对 1.5 的改进：should_stop 随时中断（半途返回已采到的）；
剪贴板原值 finally 恢复 + 每次读取后还原；300 上限照搬；
地址栏坐标兜底不移植（Alt+D 已够稳，坐标是 1.5 最大的脆弱点）。
取舍（与 1.5 相同）：绕回起点按（标题,网址）对判定——若两个相邻
标签标题与网址全同会提前收工；拿不到网址的标签跳过。
"""
import time
from dataclasses import dataclass
from typing import Callable
# 实时采集的第三方依赖（真驱动用；psutil 项目 requirements 里已有）
REQUIRED_PACKAGES = ("pywinauto", "pyautogui", "pyperclip", "psutil")


def missing_packages() -> list[str]:
    """缺了哪些依赖。find_spec 只查存在性、不真导入——
    导入有副作用（COM 初始化等），探测阶段不该触发。"""
    import importlib.util
    return [n for n in REQUIRED_PACKAGES
            if importlib.util.find_spec(n) is None]


@dataclass
class CollectedTab:
    """一个实时采集到的标签：网址 + 页面标题。"""
    url: str
    title: str


class CollectError(Exception):
    """显式失败（依赖未装 / 浏览器没开 / 找不到窗口），绝不静默。"""


def collect_tabs(driver, *, max_tabs: int = 300,
                 start_from_first: bool = True,
                 should_stop: Callable[[], bool] | None = None,
                 on_progress: Callable[[int], None] | None = None,
                 ) -> list[CollectedTab]:
    """走查策略（与 1.5 相同的骨架）：每个窗口先跳最左标签（可选），
    记下起点（标题,网址），循环 Ctrl+PgDn 直到绕回起点或达上限。
    网址去重；非 http/https 跳过；should_stop 每步询问。
    finally 恢复剪贴板原值。"""
    windows = driver.find_windows()
    if not windows:
        raise CollectError("未发现正在运行的 Edge 窗口——请先打开 Edge"
                           "（窗口可以最小化，但不能全关）")
    out: list[CollectedTab] = []
    seen: set[str] = set()
    saved_clip = driver.clipboard_get()
    try:
        for win in windows:
            if should_stop is not None and should_stop():
                break
            driver.focus(win)
            if start_from_first:
                driver.hotkey("ctrl", "1")
                driver.sleep(0.35)
            start_title = driver.window_title(win)
            start_url = _current_url(driver)
            _push(out, seen, start_url, start_title, on_progress)
            steps = 0
            while steps < max_tabs:
                if should_stop is not None and should_stop():
                    return out  # 中断：半途成果照常返回
                driver.hotkey("ctrl", "pgdn")
                driver.sleep(0.5)
                steps += 1
                title = driver.window_title(win)
                url = _current_url(driver)
                if start_url is not None and (title, url) == (start_title,
                                                              start_url):
                    break  # 绕回起点 = 本窗口遍历完
                _push(out, seen, url, title, on_progress)
    finally:
        driver.clipboard_set(saved_clip)
    return out


def _push(out: list, seen: set, url: str | None, title: str,
          on_progress) -> None:
    """入库门槛：http/https 且未见过。on_progress 报已采集数。"""
    if url and url.startswith(("http://", "https://")) and url not in seen:
        seen.add(url)
        out.append(CollectedTab(url, title))
        if on_progress is not None:
            on_progress(len(out))


def _current_url(driver) -> str | None:
    """偷当前标签地址栏（1.5 的哨兵舞步）：置哨兵 → Alt+D → Ctrl+C
    → 轮询等剪贴板变化 → 还原调用前剪贴板。1.2 秒拿不到就放弃。"""
    orig = driver.clipboard_get()
    sentinel = f"__collect_{time.time()}__"
    driver.clipboard_set(sentinel)
    driver.hotkey("alt", "d")
    driver.sleep(0.12)
    driver.hotkey("ctrl", "c")
    url: str | None = None
    deadline = time.time() + 1.2
    while time.time() < deadline:
        data = driver.clipboard_get()
        if data and data != sentinel:
            url = data.strip()
            break
        driver.sleep(0.05)
    driver.clipboard_set(orig)  # 哨兵不外漏
    return url if url and url.startswith(("http://", "https://")) else None


class EdgeDriver:
    """真机驱动：窗口枚举照搬 1.5 的 psutil 进程白名单（msedge.exe
    严格过滤）+ Desktop(uia) 列窗，带标题正则兜底；热键 pyautogui、
    剪贴板 pyperclip。依赖惰性导入，缺包给一次性安装指引。"""

    def __init__(self) -> None:
        try:
            import warnings
            with warnings.catch_warnings():
                # pywinauto 导入时的 "Revert to STA COM threading mode"
                # 是正常现象不是错误——静音，别吓小白
                warnings.simplefilter("ignore", UserWarning)
                from pywinauto.application import Application
                from pywinauto import Desktop, findwindows
                import pyautogui
                import pyperclip
                import psutil
        except ImportError as exc:
            raise CollectError(
                "实时采集需要一次性安装四个包：\n"
                "pip install pywinauto pyautogui pyperclip psutil\n"
                "（装完无需重启本工具，再点一次采集即可）") from exc
        self._Application = Application
        self._Desktop = Desktop
        self._findwindows = findwindows
        self._pyautogui = pyautogui
        self._pyperclip = pyperclip
        self._psutil = psutil
        self._cache: dict = {}  # handle → 窗口控制器，避免每次重连

    # ---- 驱动原语（与 collect_tabs 的约定见文件头） ----

    def find_windows(self) -> list:
        pids = []
        for p in self._psutil.process_iter(attrs=["name", "pid"]):
            try:
                if (p.info.get("name") or "").lower() == "msedge.exe":
                    pids.append(p.info["pid"])
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue
        wins: list = []
        for pid in pids:  # 方案A：按进程列可见顶级窗口（1.5 验证最稳）
            try:
                wins.extend(w.element_info for w in self._Desktop(
                    backend="uia").windows(process=pid, visible_only=True,
                                           control_type="Window"))
            except Exception:
                continue
        if not wins:  # 兜底：标题正则，仍限 Edge 进程
            for pid in pids:
                try:
                    wins.extend(self._findwindows.find_elements(
                        title_re=r".*Edge.*", process=pid, backend="uia"))
                except Exception:
                    continue
        return [w for w in wins if self._process_name(w) == "msedge.exe"]

    def _process_name(self, info) -> str:
        try:
            return self._psutil.Process(info.process_id).name().lower()
        except Exception:
            return ""

    def _win(self, info):
        key = info.handle
        if key not in self._cache:
            app = self._Application(backend="uia").connect(
                process=info.process_id)
            self._cache[key] = app.window(handle=info.handle)
        return self._cache[key]

    def focus(self, win) -> None:
        self._win(win).set_focus()

    def window_title(self, win) -> str:
        return self._win(win).window_text()

    def hotkey(self, *keys: str) -> None:
        self._pyautogui.hotkey(*keys)

    def clipboard_set(self, text: str) -> None:
        self._pyperclip.copy(text)

    def clipboard_get(self) -> str:
        return self._pyperclip.paste()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


    def esc_pressed(self) -> bool:
        """Esc 键是否正被按下（Win32 GetAsyncKeyState，零新依赖）。
        仅作中断探测：拿不到键盘状态就按"没按"处理。"""
        try:
            import ctypes
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B)
                        & 0x8000)
        except Exception:
            return False
