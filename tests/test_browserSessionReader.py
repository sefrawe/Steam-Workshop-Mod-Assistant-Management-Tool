"""tests/test_browserSessionReader.py —— 会话文件解析引擎测试。"""
import zlib

"""不依赖真实浏览器环境：测试自造 SNSS 字节样本（合成样本机制，
与 appworkshop_mini.acf 同款）。重点钉死：标准布局直读（含对齐）、
同标签取最新序号、跨文件去重、兜底扫描、损坏文件不炸。"""
import struct

import pytest

from core import browserSessionReader as bsr
from core.browserSessionReader import find_session_files, read_open_tabs

U1 = "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213"
U2 = "https://steamcommunity.com/sharedfiles/filedetails/?id=2216850785"


def _snss(blocks: list[tuple[int, bytes]]) -> bytes:
    out = b"SNSS" + struct.pack("<I", 1)          # 头部：魔数 + 版本
    for tag, payload in blocks:
        out += struct.pack("<II", tag, len(payload)) + payload
    return out


def _nav(tab_id: int, index: int, url: str,
         referrer: str = "", title: str = "") -> bytes:
    """按引擎期待的标准布局造一条 DSNF 块内容（含 pickle 头与对齐）。"""
    url_b = url.encode()
    ref_b = referrer.encode()
    title_b = title.encode("utf-16-le")
    body = struct.pack("<II", tab_id, index)
    body += struct.pack("<I", len(url_b)) + url_b
    body += b"\x00" * (-(16 + len(url_b)) % 4)          # 网址后对齐
    body += struct.pack("<I", len(ref_b)) + ref_b
    after_ref = 16 + len(url_b) + (-(16 + len(url_b)) % 4) + 4 + len(ref_b)
    body += b"\x00" * (-after_ref % 4)                   # referrer 后对齐
    body += struct.pack("<I", len(title)) + title_b
    return struct.pack("<I", len(body)) + body


DSNF = 0x464E5344


def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_single_tab_url_and_title(tmp_path):
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(7, 0, U1, referrer="https://steamcommunity.com/",
                                 title="Steam 工坊::某个 mod"))]))
    tabs = read_open_tabs([f])
    assert len(tabs) == 1
    assert tabs[0].url == U1
    assert tabs[0].title == "Steam 工坊::某个 mod"
    assert tabs[0].is_workshop is True          # urlParser 单源判定


def test_latest_index_wins(tmp_path):
    # 同一标签先逛了旧页再停在目标页：只该报目标页
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(7, 0, U2)),
                     (DSNF, _nav(7, 1, U1))]))
    tabs = read_open_tabs([f])
    assert [t.url for t in tabs] == [U1]


def test_dedupe_across_files_first_file_wins(tmp_path):
    a, b = tmp_path / "Tabs_1", tmp_path / "Last Tabs"
    _write(a, _snss([(DSNF, _nav(1, 0, U1, title="新会话"))]))
    _write(b, _snss([(DSNF, _nav(2, 0, U1, title="旧会话"))]))
    tabs = read_open_tabs([a, b])               # 新→旧传入
    assert len(tabs) == 1 and tabs[0].title == "新会话"


def test_internal_pages_dropped(tmp_path):
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(1, 0, "chrome://newtab/")),
                     (DSNF, _nav(2, 0, U1))]))
    assert [t.url for t in read_open_tabs([f])] == [U1]


def test_fallback_scan_when_strict_fails(tmp_path):
    # 布局对不上（长度前缀离谱）：直读失败，锚点扫描仍救回网址
    f = tmp_path / "Tabs_1"
    payload = (struct.pack("<I", 4) + struct.pack("<I", 0xFFFFFFF0)
               + U1.encode() + b"\x00\x00\x00\x00")
    _write(f, _snss([(DSNF, payload)]))
    tabs = read_open_tabs([f])
    assert len(tabs) == 1
    assert tabs[0].url == U1
    assert tabs[0].title == ""                  # 兜底路径如实放弃标题


def test_truncated_block_keeps_earlier_tabs(tmp_path):
    good = _nav(1, 0, U1)
    data = (_snss([(DSNF, good)])
            + struct.pack("<II", DSNF, 500) + b"short")   # 块长越界
    f = tmp_path / "Tabs_1"
    _write(f, data)
    assert [t.url for t in read_open_tabs([f])] == [U1]


def test_bad_magic_skipped(tmp_path):
    f = tmp_path / "not_snss"
    _write(f, b"XXXX" + b"\x00" * 64)
    assert read_open_tabs([f]) == []


def test_locked_file_skipped(tmp_path, monkeypatch):
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(1, 0, U1))]))
    def boom(self):
        raise PermissionError("被浏览器占用")
    monkeypatch.setattr(type(f), "read_bytes", boom)
    assert read_open_tabs([f]) == []            # 跳过不中断，不炸


def test_find_session_files_layout(tmp_path):
    _write(tmp_path / "Default" / "Sessions" / "Tabs_1", b"SNSS" + b"\x00" * 4)
    _write(tmp_path / "Default" / "Last Tabs", b"SNSS" + b"\x00" * 4)
    _write(tmp_path / "Profile 1" / "Sessions" / "Session_9", b"SNSS" + b"\x00" * 4)
    files = find_session_files("edge", user_data_override=str(tmp_path))
    assert {p.name for p in files} == {"Tabs_1", "Last Tabs", "Session_9"}


def test_firefox_and_unknown_rejected():
    with pytest.raises(ValueError):
        find_session_files("firefox")
    with pytest.raises(ValueError):
        find_session_files("opera")


def test_no_localappdata_returns_empty(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert find_session_files("edge") == []

def test_unknown_block_ids_still_read(tmp_path):
    # 第 4 轮真机教训：块类型不能当白名单——真实文件的块 ID 与
    # 资料不符时，锚点兜底必须仍然救得回来
    f = tmp_path / "Session_123"
    _write(f, _snss([(0xDEADBEEF, _nav(9, 0, U1, title="真机形态"))]))
    tabs = read_open_tabs([f])
    assert [t.url for t in tabs] == [U1]
    assert tabs[0].title == "真机形态"

def test_two_tabs_both_kept(tmp_path):
    # 补丁 4c 教训：点改残留曾把循环嵌套成"全文件只留最后一条"
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(1, 0, U1)),
                     (DSNF, _nav(2, 0, U2))]))
    assert [t.url for t in read_open_tabs([f])] == [U1, U2]


def test_unparseable_blocks_skipped(tmp_path):
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(0x01, b"\x00\x00\x00\x00"),
                     (DSNF, _nav(1, 0, U1)),
                     (0x02, b"junk")]))
    assert [t.url for t in read_open_tabs([f])] == [U1]


def test_compressed_session_read(tmp_path):
    # v3 压缩壳场景（引擎侧逻辑保留：万一 Edge 老格式/其他浏览器命中）
    inner = _snss([(DSNF, _nav(1, 0, U1, title="压缩壳里的标签")),
                   (DSNF, _nav(2, 0, U2))])[8:]
    f = tmp_path / "Session_1"
    _write(f, b"SNSS" + struct.pack("<I", 3) + zlib.compress(inner))
    tabs = read_open_tabs([f])
    assert [t.url for t in tabs] == [U1, U2]
    assert tabs[0].title == "压缩壳里的标签"


def test_uncompressed_legacy_still_read(tmp_path):
    f = tmp_path / "Tabs_1"
    _write(f, _snss([(DSNF, _nav(1, 0, U1))]))
    assert [t.url for t in read_open_tabs([f])] == [U1]
