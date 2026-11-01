"""实时采集浏览器标签引擎（键盘自动化路线）"""
"""core/tabCollector.py · 旧脚本 1.5 的转正版：逐窗遍历、Ctrl+PgDn
切标签、Alt+D+Ctrl+C 偷地址栏、剪贴板哨兵——走查逻辑与 1.5 相同，
但按依赖注入重构：驱动原语（找窗/聚焦/热键/剪贴板/睡眠）全部注入，
本文件只写"走查策略"，pytest 用假驱动全速覆盖；真驱动 EdgeDriver
是 1.5 已在真机多日验证的代码搬运，不改逻辑。

为什么转正：会话文件路线（browserSessionReader）在 Edge 当前版本
（SNSS v3）上三轮真机验证 0 结果——块流不可读、zlib 假设失败、
零明文网址。用户旧 1.5 同机多日稳定，事实胜于考古。

Linux 补充（2.1.0 轮）：Firefox 的会话存档（recovery.jsonlz4）与
Edge SNSS 是两回事——官方 mozLz4 格式、运行中约每 15 秒自动保存，
直读稳定可靠；Linux/mac 的采集由 FirefoxSessionDriver 承担（只读
文件，不占鼠标键盘），Windows 仍走 EdgeDriver 键盘走查，一字未动。

代价（界面文案同步声明）：
- 采集期间占用鼠标键盘（Edge 窗口自己切换，人别动）；
- 紧急刹车四条（2026-10-31 修"死绕圈"轮）：Esc 轻点即停（粘住位，
  不必按住等轮询）；随便动一下鼠标也立即停（本引擎只发热键从不
  碰鼠标，光标位移只能来自人手）；Alt+Tab 切回本工具点
  【停止采集】；鼠标甩到屏幕左上角（pyautogui 自带 FAILSAFE，
  会抛异常终止采集）；
- 依赖 4 个包，import 惰性——没装不影响程序其他功能，点采集时
  才提示一次性安装。

相对 1.5 的改进：should_stop 随时中断（半途返回已采到的）；
剪贴板原值 finally 恢复 + 每次读取后还原；300 上限照搬；
地址栏坐标兜底不移植（Alt+D 已够稳，坐标是 1.5 最大的脆弱点）。
取舍：绕回起点按「本轮已见过的真网址再次出现」判定——空白页/首页
没有唯一指纹（两个空白页标题与网址全同），不能当绕回信号，否则
中间夹个空白页就被误判"绕回"提前收工（2026-10-31 真机踩坑）；
空白页一律跳过、只记真网址，真网址重复出现 = 真绕回一整圈；"偷网址
偶发失败被误判绕回"的旧顾虑改由空转保险丝兜底（连续 stall_limit
步零新增提前收工，经 on_note 说明）；若两个标签网址全同（用户重复
开同一页）会提前收工——建议只保留不重复、要下载的标签；拿不到
网址的标签跳过。
"""
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
# 实时采集的第三方依赖（真驱动用；psutil 项目 requirements 里已有）
REQUIRED_PACKAGES = ("pywinauto", "pyautogui", "pyperclip", "psutil")


def missing_packages() -> list[str]:
    """缺了哪些依赖。find_spec 只查存在性、不真导入——
    导入有副作用（COM 初始化等），探测阶段不该触发。"""
    import importlib.util
    return [n for n in REQUIRED_PACKAGES
            if importlib.util.find_spec(n) is None]


def missing_packages_message() -> str | None:
    """点采集前的统一体检：缺依赖时返回可直接弹窗的完整文案，None=放行。
    只看 Windows 走查路线的四个包——Linux/mac 走 FirefoxSessionDriver
    会话直读，不依赖它们（lz4 只在读压缩会话时才要，缺失由驱动给指引）。"""
    if sys.platform != "win32":
        return None
    missing = missing_packages()
    if not missing:
        return None
    return ("实时采集缺 " + "、".join(missing) + "。\n\n"
            "安装命令（复制到 cmd 执行）：\n"
            "pip install pywinauto pyautogui pyperclip psutil\n\n"
            "装完后无需重启本工具，再点一次本按钮即可继续。")


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
                 on_note: Callable[[str], None] | None = None,
                 stall_limit: int = 12,
                 ) -> list[CollectedTab]:
    """走查策略（与 1.5 同骨架 + 2026-10-31 修）：每个窗口先跳最左
    标签（可选），循环 Ctrl+PgDn 直到绕回起点或达上限。网址去重；
    非 http/https 跳过；should_stop 每步询问（偷网址的 0.05 秒
    轮询里也问——Esc 轻点、动鼠标都被接住）。finally 恢复剪贴板。

    修（2026-10-31，两轮）：绕回判定按「本轮已见过的真网址再次
    出现」——空白页/首页没唯一指纹（两个空白页标题与网址全同），
    不能当绕回信号，否则中间夹个空白页就被误判"绕回"提前收工
    （真机踩坑：第一与中间各一个空网址，碰到第二个就停、漏掉后面
    标签）；空白页一律跳过、只记真网址，真网址重复出现 = 真绕回
    一整圈。旧版 (标题,网址) 直比在"起点空白 + 中间又空白"场景
    下必错。"偷网址偶发失败被误判绕回"的顾虑由 stall_limit 保险丝
    兜底：连续 stall_limit 步零新增就提前收工，经 on_note 说明。

    鼠标刹车：驱动若提供 arm_mouse_brake/mouse_moved（EdgeDriver
    有，测试假驱动可以没有），开跑前布点、跑动中查位移——本引擎
    从不动鼠标，宽限期后位移超阈值只能是人在动 = 立即收工。"""
    windows = driver.find_windows()
    if not windows:
        raise CollectError("未发现正在运行的 Edge 窗口——请先打开 Edge"
                           "（窗口可以最小化，但不能全关）")
    arm = getattr(driver, "arm_mouse_brake", None)
    if callable(arm):
        arm()  # 假驱动没有这两个原语，getattr 惰性跳过
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
                if _sleep_polling(driver, 0.35, should_stop):
                    return out  # 中断：半途成果照常返回
            start_title = driver.window_title(win)
            start_url = _current_url(driver, should_stop)
            if start_url is None and on_note is not None:
                on_note("起点标签是空白页/首页，没有网址指纹——绕回判定"
                        "改按「本轮已见过的真网址再次出现」识别，空白页"
                        "一律跳过，不影响采集")
            # 本轮（本窗口遍历）已见的真网址 = 绕回信号。空白页没
            # 唯一指纹，不能当绕回信号：两个空白页标题、网址全同，
            # 会把"绕回起点"误判成中间那个空白页，提前收工漏掉后面
            # 的标签（2026-10-31 修"中间夹空网址就停"）。所以只记
            # 真网址——真网址重复出现 = 真绕回一整圈。
            cycle_seen: set[str] = set()
            if start_url:
                cycle_seen.add(start_url)
            _push(out, seen, start_url, start_title, on_progress)
            steps = 0
            stall = 0  # 连续多少步没采到新网址（保险丝计数）
            while steps < max_tabs:
                if should_stop is not None and should_stop():
                    return out  # 中断：半途成果照常返回
                driver.hotkey("ctrl", "pgdn")
                if _sleep_polling(driver, 0.5, should_stop):
                    return out
                steps += 1
                title = driver.window_title(win)
                url = _current_url(driver, should_stop)
                if url and url in cycle_seen:
                    break  # 绕回：这个真网址本轮已经见过
                if url:
                    cycle_seen.add(url)
                grew = len(out)
                _push(out, seen, url, title, on_progress)
                stall = 0 if len(out) > grew else stall + 1
                if stall >= stall_limit:
                    if on_note is not None:
                        on_note(f"连续 {stall_limit} 个标签没采到新网址"
                                "——提前收工（防死绕圈保险丝；已采到的"
                                "照常在清单里）")
                    break
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


def _sleep_polling(driver, seconds: float,
                   should_stop: Callable[[], bool] | None) -> bool:
    """把一段等待切成 0.05 秒小片，片间问 should_stop——原来整段
    time.sleep 是"无感盲区"，几十毫秒的快击 Esc 落在里面就漏了
    （2026-10-31 修"Esc 失灵"第二刀：不是焦点问题，是盲区太宽 +
    旧版只认"此刻正按着"）。返回 True = 等待期间收到停止请求，
    调用方该提前收工。"""
    deadline = time.time() + seconds
    while True:
        if should_stop is not None and should_stop():
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        driver.sleep(min(0.05, remaining))


def _current_url(driver, should_stop: Callable[[], bool] | None = None
                 ) -> str | None:
    """偷当前标签地址栏（1.5 的哨兵舞步）：置哨兵 → Alt+D → Ctrl+C
    → 轮询等剪贴板变化 → 还原调用前剪贴板。1.2 秒拿不到就放弃
    （首页/空白页地址栏本来就空，None 是常态不是故障）。轮询每
    0.05 秒顺手问一次 should_stop——中断请求不必等整步走完。"""
    orig = driver.clipboard_get()
    sentinel = f"__collect_{time.time()}__"
    driver.clipboard_set(sentinel)
    driver.hotkey("alt", "d")
    if _sleep_polling(driver, 0.12, should_stop):
        driver.clipboard_set(orig)  # 被中断也要还原哨兵
        return None
    driver.hotkey("ctrl", "c")
    url: str | None = None
    deadline = time.time() + 1.2
    while time.time() < deadline:
        if should_stop is not None and should_stop():
            break
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
        # 刹车状态（每轮采集新建驱动 → 天然归零，不跨轮误停）：
        # Esc 粘住位 + 鼠标刹车基准点/起跑时刻
        self._esc_latch = False
        self._brake_origin: tuple[int | None, int | None] | None = None
        self._brake_time = 0.0

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

    # ---- 刹车原语（collect_tabs 按 getattr 惰性取用；测试假驱动
    # 不实现这些也能跑） ----

    # 布点后的宽限秒数：点【采集】后手从鼠标挪开是正常动作，不计
    _BRAKE_GRACE_S = 2.0
    # 光标位移超过这个像素数才算"人动了鼠标"（手抖/误触远小于此）
    _BRAKE_PX = 15

    def arm_mouse_brake(self) -> None:
        """记录光标基准位置与起跑时刻，collect_tabs 开跑前调用。本
        引擎全程只发热键、从不移动光标——宽限期后任何像样位移都
        只能来自人手，mouse_moved() 据此报警（用户提案：开始两秒后
        动鼠标即停）。顺手清掉 Esc 的"自上次查询以来被按过"标志：
        不清理的话，采集前为别的事按过的 Esc 会残留，第一次 probe
        就误停（esc_pressed 认 0x0001 位）。"""
        self._brake_origin = self._cursor_pos()
        self._brake_time = time.time()
        if sys.platform == "win32":
            try:
                import ctypes
                # 只读不判：这一调把 0x0001 残留位清零
                ctypes.windll.user32.GetAsyncKeyState(0x1B)
            except Exception:
                pass

    def mouse_moved(self) -> bool:
        """宽限期后光标位移超阈值 = 人动了鼠标 = 该停。拿不到光标
        位置按"没动"处理（与 esc_pressed 同款保守）。"""
        origin = self._brake_origin
        if origin is None or origin[0] is None:
            return False
        if time.time() - self._brake_time < self._BRAKE_GRACE_S:
            return False
        x, y = self._cursor_pos()
        if x is None:
            return False
        return (abs(x - origin[0]) > self._BRAKE_PX
                or abs(y - origin[1]) > self._BRAKE_PX)

    @staticmethod
    def _cursor_pos() -> tuple[int | None, int | None]:
        """Win32 GetCursorPos，零新依赖；非 Windows / 调用失败给
        (None, None)，调用方按"没动"处理。"""
        if sys.platform != "win32":
            return None, None
        try:
            import ctypes
            import ctypes.wintypes
            pt = ctypes.wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        return None, None

    def esc_pressed(self) -> bool:
        """Esc 按下或刚按过（粘住位）。Win32 GetAsyncKeyState：0x8000
        位 = 此刻正按着（按住）；0x0001 位 = 自上次查询以来被按过
        （快击落进两次轮询之间也逃不掉）——两路都认并粘住，见过一次
        就永久返回 True。旧版只认 0x8000 且轮询是步级（约 0.5 秒盲区），
        轻点多半撞不上采样点，用户体感"按了不停"（2026-10-31 真机
        踩坑：与焦点无关，纯粹是盲区太宽 + 只认按住）。驱动每轮采集
        新建，粘住位随驱动生命周期，不会跨轮误停。零新依赖；拿不到
        键盘状态就按"没按"处理。"""
        if self._esc_latch:
            return True
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            if ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8001:
                self._esc_latch = True
                return True
        except Exception:
            return False
        return False


class FirefoxSessionDriver:
    """Linux/macOS 的真驱动：直读 Firefox 会话存档拿"当前打开的标签页"。

    V1 否掉会话路线针对的是 Edge 的 SNSS v3（三轮真机 0 结果）；Firefox
    的 recovery.jsonlz4 是另一回事——官方格式（mozLz4 头 + lz4 块压缩
    的 JSON），运行中约每 15 秒自动保存，含全部窗口/标签的网址与标题，
    多年稳定。只读文件：不占鼠标键盘、不动浏览器窗口，也不要求 Firefox
    正开着（正常退出后的存档照样在）。隐私窗口读不到（Firefox 本就不
    把私有会话写盘）；存档最多落后真实状态一个保存周期（~15 秒）。
    """

    _MAGIC = b"mozLz40\0"  # 魔数 8B + 未压缩大小 uint32 LE + lz4 块

    def __init__(self, profile_root: Path | None = None) -> None:
        # profile_root 是测试注入点；None = 按平台找真实 Firefox 档案目录
        self._profile_root = profile_root

    def _candidate_roots(self) -> list[Path]:
        """Firefox 档案目录的三种安装形态：deb/官方包、snap、flatpak。"""
        if self._profile_root is not None:
            return [self._profile_root]
        home = Path.home()
        return [home / ".mozilla" / "firefox",
                home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
                home / ".var" / "app" / "org.mozilla.firefox"
                / ".mozilla" / "firefox"]

    def _session_files(self) -> list[Path]:
        """全部候选档案的会话文件，按修改时间新→旧。recovery.jsonlz4 =
        运行中的实时存档；没有（Firefox 已正常关闭）就退 previous.jsonlz4
        = 上次正常退出时的存档。"""
        out: list[Path] = []
        for root in self._candidate_roots():
            if not root.is_dir():
                continue
            for prof in root.iterdir():
                ss = prof / "sessionstore-backups"
                for name in ("recovery.jsonlz4", "previous.jsonlz4"):
                    f = ss / name
                    if f.is_file():
                        out.append(f)
        out.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return out

    @classmethod
    def _decode(cls, data: bytes) -> dict:
        """mozLz4 → dict；也收未压缩 JSON（测试直塞明文用）。"""
        if data[:8] == cls._MAGIC:
            try:
                import lz4.block
            except ImportError as exc:
                raise CollectError(
                    "读到 Firefox 压缩会话，但缺 lz4 库：\n"
                    "pip install lz4\n装完再点一次即可。") from exc
            size = int.from_bytes(data[8:12], "little")
            raw = lz4.block.decompress(data[12:], uncompressed_size=size)
        else:
            raw = data
        return json.loads(raw.decode("utf-8"))

    def collect(self, *, max_tabs: int = 300) -> list[CollectedTab]:
        """全部档案的全部窗口的全部标签（每标签只取当前页，不走浏览
        历史），网址去重、http/https 门槛、300 上限——产物口径与
        Windows 走查路线一致。"""
        files = self._session_files()
        if not files:
            raise CollectError(
                "未发现 Firefox 会话文件——请确认装了 Firefox 且打开过"
                "标签页（正在运行或正常关闭过都行）。用的若是 Edge/Chrome，"
                "本功能暂不支持。")
        out: list[CollectedTab] = []
        seen: set[str] = set()
        for f in files:
            try:
                data = self._decode(f.read_bytes())
            except CollectError:
                raise  # 缺 lz4 这类"要人动手"的指引必须冒出来
            except Exception:
                continue  # 单个档案损坏换下一个，绝不因一个坏档全灭
            for win in data.get("windows", []):
                for tab in win.get("tabs", []):
                    entries = tab.get("entries") or []
                    idx = tab.get("index") or len(entries)
                    if not 1 <= idx <= len(entries):
                        continue
                    cur = entries[idx - 1]
                    url = cur.get("url") or ""
                    if (url.startswith(("http://", "https://"))
                            and url not in seen):
                        seen.add(url)
                        out.append(CollectedTab(
                            url, cur.get("title") or ""))
                        if len(out) >= max_tabs:
                            return out
        if not out:
            raise CollectError(
                "Firefox 会话里没有可用的 http(s) 标签页"
                "（可能全停在空白页或浏览器内部页）。")
        return out
