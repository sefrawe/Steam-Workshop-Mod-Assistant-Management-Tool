"""浏览器会话标签读取器"""
import zlib

"""core/browserSessionReader.py · 从 Edge / Chrome 的本地会话文件读出
"当前与最近打开的标签页"，替代旧 1.5 脚本的键鼠自动化采集
（1.5 = 逐个切标签页偷地址栏，运行期占用鼠标键盘、依赖坐标点击、
有 300 上限；本文件只读硬盘上的普通文件，零干扰、毫秒级、无上限）。

原理（为什么可行）：Chromium 系浏览器本来就把打开的标签页写在
本地会话文件（SNSS 格式）里。读它 = 读普通文件：浏览器开着关着
都能读、不联网、不装驱动、不需要重启浏览器。Edge 与 Chrome 同一
格式（目录不同）；Firefox 是另一种格式（jsonlz4），第二批支持。

解析策略（对版本漂移的防御；第 4 轮真机翻车后的改版）：
- 文件框架稳定：SNSS 魔数 + 若干"块"（4 字节类型 + 4 字节长度 +
  内容）。块类型不作白名单——真机文件证明导航块的 ID 会与资料
  记载不符，白名单一旦开错，连兜底扫描都没机会出场（整文件零
  标签的实录）。改为逐块尝试解析，认不出网址的块自然落空。
- 块内字段随版本可能增删，所以网址读取走双保险：先按标准布局
  直读（标签 id、序号、网址、标题）；任何一步对不上就退回锚点
  扫描——网址以 UTF-8 存放而标题是 UTF-16，ASCII 锚点不会误中
  标题，最坏情况只丢标题不丢网址。


取舍（如实声明）：
- 同一标签的多条导航记录只取序号最大的一条 = 标签当前所在页；
- 手滑关掉的标签记录也在会话文件里——这是特性（找回误关），
  页面文案按"当前与最近打开"措辞，用户按需勾选；
- 跨文件（当前会话与上次会话）按网址去重，先出现的优先；
- 只保留 http/https 网址，浏览器内部页（chrome:// 等）按规则
  略过——这是明示的过滤规则，不是静默丢数据。

只读纪律：不写任何文件、不碰浏览器进程、不删东西；单个文件
被占用或损坏 → 跳过继续读其余，绝不中断整批。
"""
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from core.urlParser import extract_mod_id

_SNSS_MAGIC = b"SNSS"
# 导航块不再按类型白名单（第 4 轮真机教训，见文件头"解析策略"）


# 浏览器名 → 用户数据根在 %LOCALAPPDATA% 下的固定布局
_BROWSER_ROOTS = {
    "edge": ("Microsoft", "Edge", "User Data"),
    "chrome": ("Google", "Chrome", "User Data"),
}


@dataclass
class BrowserTab:
    """一个标签页：网址 + 标题 + 来源（纯展示/排查用）。"""
    url: str
    title: str
    source: str   # 例 "Edge·Default"（浏览器·配置目录），猜不出就用文件名
    file: str     # 来源文件名

    @property
    def is_workshop(self) -> bool:
        """是不是工坊条目页——判定调 urlParser 单源，与本工具其他
        页面对"什么算工坊网址"的理解永远一致。"""
        return extract_mod_id(self.url) is not None


def find_session_files(browser: str,
                       user_data_override: str | None = None) -> list[Path]:
    """定位某浏览器的全部会话文件：每个配置目录的 Sessions\\ 下所有
    文件 + 配置目录下的 Last Session / Last Tabs。
    返回按修改时间新→旧排序（当前会话排前面，去重时优先）。
    目录不存在返回空表（调用方显式报告，不静默）。"""
    key = browser.strip().lower()
    if key == "firefox":
        raise ValueError("Firefox 的会话是另一种格式（jsonlz4），"
                         "第二批支持，暂不可读")
    if key not in _BROWSER_ROOTS:
        raise ValueError(f"不认识的浏览器：{browser}（可选 edge / chrome）")
    if user_data_override:
        root = Path(user_data_override)
    else:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return []
        root = Path(local).joinpath(*_BROWSER_ROOTS[key])
    if not root.is_dir():
        return []
    files: list[Path] = []
    for profile in root.iterdir():          # Default / Profile 1 / ...
        if not profile.is_dir():
            continue
        sessions = profile / "Sessions"
        if sessions.is_dir():
            files.extend(p for p in sessions.iterdir() if p.is_file())
        for name in ("Last Session", "Last Tabs"):
            p = profile / name
            if p.is_file():
                files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files
def read_open_tabs(files: Iterable[Path]) -> list[BrowserTab]:
    """把会话文件读成标签清单。每个标签取当前页面（同标签最大序号），
    跨文件按网址去重（先出现优先——files 应按新→旧传入）。
    单个文件被占用/不是 SNSS/块损坏 → 跳过，绝不中断整批。"""
    tabs: list[BrowserTab] = []
    seen: set[str] = set()
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            continue  # 被占用（浏览器开着）：跳过该文件，读其余
        body = _block_stream(data)
        if body is None:
            continue  # 不是会话文件：跳过
        label = _profile_label(path)
        # 同一文件内：标签 id → (序号, 网址, 标题)，序号大者胜
        per_tab: dict[int, tuple[int, str, str]] = {}
        order: list[int] = []
        # 逐块尝试解析（块类型不作白名单）。全文件只许出现这一个
        # 循环——补丁 4c 教训：嵌套双循环 286 全绿照样漏网
        for _, payload in _iter_blocks(body):
            entry = _parse_navigation(payload)
            if entry is None:
                continue  # 认不出网址的块（窗口信息等）：略过
            tab_id, index, url, title = entry
            if not url.startswith(("http://", "https://")):
                continue  # 内部页按明示规则略过（见文件头"取舍"）
            if tab_id is None:
                # 兜底路径拿不到标签 id：每条自成一项，交给跨文件去重兜底
                if url not in seen:
                    seen.add(url)
                    tabs.append(BrowserTab(url, title, label, path.name))
                continue
            prev = per_tab.get(tab_id)
            if prev is None:
                order.append(tab_id)
                per_tab[tab_id] = (index, url, title)
            elif index > prev[0]:
                per_tab[tab_id] = (index, url, title)
        for tab_id in order:
            _, url, title = per_tab[tab_id]
            if url in seen:
                continue
            seen.add(url)
            tabs.append(BrowserTab(url, title, label, path.name))
    return tabs

# ---------- SNSS 框架 ----------
def _block_stream(data: bytes) -> bytes | None:
    """文件字节 → 纯块流字节；非 SNSS 返回 None。v3 起整个块流
    用 zlib 压在 8 字节明文头之后（dump 实锤：v3 头 + 零块 +
    零明文网址），这里剥掉压缩壳；解不开就按未压缩老格式处理。
    返回的流从头就是块，文件头与压缩已在这一层处理完。"""
    if len(data) < 8 or data[:4] != _SNSS_MAGIC:
        return None
    body = data[8:]
    for wbits in (15, -15, 31):  # zlib / 裸 deflate / gzip，挨个试
        try:
            out = zlib.decompressobj(wbits).decompress(body)
            if out:
                return out
        except zlib.error:
            continue
    return body  # 老格式：未压缩


def _iter_blocks(data: bytes) -> Iterator[tuple[int, bytes]]:
    """按 SNSS 框架逐块产出 (块类型, 块内容)。输入必须是"纯块流"
    （_block_stream 的产物，起点 0、不再预留文件头）。
    框架损坏（块长越界、非整块结尾）即停——只损失坏点之后的内容，
    之前的有效块照常产出。"""
    offset, total = 0, len(data)
    while offset + 8 <= total:
        tag = struct.unpack_from("<I", data, offset)[0]
        length = struct.unpack_from("<I", data, offset + 4)[0]
        payload = data[offset + 8: offset + 8 + length]
        if len(payload) < length:
            return  # 块被截断（写一半）：到此为止
        yield tag, payload
        offset += 8 + length

# ---------- 单条导航记录 ----------

def _parse_navigation(payload: bytes) -> tuple[int | None, int, str, str] | None:
    """一条导航记录 → (标签 id, 序号, 网址, 标题)；拿不到网址返回 None。
    先走标准布局直读；对不上退回锚点扫描（id/序号未知 → None）。"""
    # —— 直读：pickle 头(4) + 标签id(4) + 序号(4) + 网址长度(4) + 网址 ——
    try:
        tab_id = struct.unpack_from("<I", payload, 4)[0]
        index = struct.unpack_from("<I", payload, 8)[0]
        url_len = struct.unpack_from("<I", payload, 12)[0]
        if 0 < url_len <= 2048 and 16 + url_len <= len(payload):
            url = payload[16:16 + url_len].decode("utf-8")
            if _looks_like_url(url):
                title = _read_title_after(payload, 16 + url_len)
                return tab_id, index, url, title
    except (struct.error, UnicodeDecodeError):
        pass
    # —— 锚点扫描兜底（版本漂移时） ——
    url = _scan_first_url(payload)
    if url is None:
        return None
    return None, 0, url, ""


def _looks_like_url(text: str) -> bool:
    """网址合理性：http/https 开头、全 ASCII 可打印、无空白/控制字符。"""
    if not text.startswith(("http://", "https://")):
        return False
    return all(0x20 < ord(c) < 0x7F for c in text)


def _read_title_after(payload: bytes, url_end: int) -> str:
    """从网址结束处按标准布局继续读标题：referrer 字符串（UTF-8、
    4 字节字节长度前缀）→ 标题（UTF-16、4 字节字符数前缀），字段间
    按 4 字节对齐。任何一步不合理就地放弃——标题缺失不影响网址。"""
    def align(n: int) -> int:
        return n + (-(n) % 4)  # 对齐到 4 字节边界（pickle 的排布规矩）

    try:
        p = align(url_end)                     # referrer 长度前缀
        ref_len = struct.unpack_from("<I", payload, p)[0]
        p = align(p + 4 + ref_len)             # 跳过 referrer 内容 + 对齐
        title_chars = struct.unpack_from("<I", payload, p)[0]
        p += 4
        if not 0 < title_chars <= 1024:
            return ""
        title = payload[p: p + title_chars * 2].decode("utf-16-le")
        title = title.replace("\x00", "").strip()
        return title
    except (struct.error, UnicodeDecodeError):
        return ""


def _scan_first_url(payload: bytes) -> str | None:
    """兜底：找块内容里第一个像网址的 ASCII 串。标题是 UTF-16 存储
    （每个字符后跟 \\x00），ASCII 锚点不会误中标题——这是本兜底
    敢于"盲扫"的根据。"""
    for anchor in (b"https://", b"http://"):
        pos = payload.find(anchor)
        while pos != -1:
            url = _extract_url_at(payload, pos)
            if url:
                return url
            pos = payload.find(anchor, pos + 1)
    return None


def _extract_url_at(payload: bytes, start: int) -> str | None:
    """锚点处取网址：前 4 字节若恰好是合理长度前缀就按前缀取整串，
    否则向后扫到第一个 URL 非法字符。"""
    if start >= 4:
        url_len = struct.unpack_from("<I", payload, start - 4)[0]
        if 8 <= url_len <= 2048 and start + url_len <= len(payload):
            try:
                text = payload[start: start + url_len].decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text and _looks_like_url(text):
                return text
    end = start
    while end < len(payload) and 0x20 < payload[end] < 0x7F:
        end += 1
    try:
        text = payload[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if _looks_like_url(text) else None


def _profile_label(path: Path) -> str:
    """从路径猜展示名：<…>/User Data/<配置名>/Sessions/<文件>。
    猜不出就用文件名——纯展示用途，猜错无副作用。"""
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "User Data" and i + 1 < len(parts) - 1:
            browser = parts[i - 1] if i else ""
            return f"{browser}·{parts[i + 1]}"
    return path.name
