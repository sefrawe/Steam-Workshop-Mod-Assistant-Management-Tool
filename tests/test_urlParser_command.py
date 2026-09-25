"""tests/test_urlParser_command.py —— 下载命令行形态的解析测试。"""
"""旧脚本 1.7 生成的命令文件、终端里复制的整行 steamcmd 命令，
粘贴进来都应能解析出 mod 编号；命令里的游戏 AppID 必须被单独
带回来（防错档比对用），且绝不能混进 mod 编号清单。常规输入
（纯数字 / 网址）的行为必须与升级前完全一致（回归钉）。
"""
from core.urlParser import parse_lines


def test_command_line_yields_mod_id_not_app_id():
    # 294100 是游戏 AppID：绝不能出现在 mod 编号清单里
    report = parse_lines(["workshop_download_item 294100 2452585382"])
    assert report.mod_ids == [2452585382]
    assert report.invalid == []
    assert report.command_app_ids == [294100]


def test_command_lines_keep_order_and_dedup():
    report = parse_lines([
        "workshop_download_item 294100 111",
        "workshop_download_item 294100 222",
        "workshop_download_item 294100 111",   # 重复 → 只认一次
    ])
    assert report.mod_ids == [111, 222]
    assert report.command_app_ids == [294100]


def test_full_terminal_line_is_honest_about_leftovers():
    # 整行从终端复制：命令本体认出来；登录名、退出等修饰词
    # 如实进"无法识别"，不静默吞掉
    report = parse_lines(
        ["+login my_name +workshop_download_item 294100 333 +quit"])
    assert report.mod_ids == [333]
    assert report.invalid == ["+login", "my_name", "+quit"]


def test_mixed_line_command_plus_url():
    # 一行里命令和网址混排：两边都认
    report = parse_lines([
        "workshop_download_item 294100 444 "
        "https://steamcommunity.com/sharedfiles/filedetails/?id=555",
    ])
    assert report.mod_ids == [444, 555]


def test_validate_suffix_still_extracts():
    # 核验页"校验重下"同款命令（带 validate 后缀）也认
    report = parse_lines(["workshop_download_item 294100 666 validate"])
    assert report.mod_ids == [666]


def test_plain_inputs_unchanged():
    # 回归钉：常规输入（网址 + 纯数字，含同行空格分隔重复）零行为变化
    report = parse_lines([
        "https://steamcommunity.com/sharedfiles/filedetails/?id=3403925213",
        "2216850785 2216850785",
    ])
    assert report.mod_ids == [3403925213, 2216850785]
    assert report.invalid == []
    assert report.command_app_ids == []

"""临时诊断：dump_snss.py"""
"""会话文件块结构体检：占用/跳过、压缩壳、块直方图、锚点网址样本，
最后跑引擎本身显示实读数。
用法：python tools/dump_snss.py Edge ["D:\\...\\User Data"]"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browserSessionReader as bsr


def _ascii_guess(tag: int) -> str:
    b = struct.pack("<I", tag)
    return "".join(chr(c) if 32 < c < 127 else "?" for c in b)


def main() -> None:
    browser = sys.argv[1] if len(sys.argv) > 1 else "edge"
    override = sys.argv[2] if len(sys.argv) > 2 else None
    files = bsr.find_session_files(browser, override)
    print(f"找到 {len(files)} 个会话文件")
    skipped = 0
    for p in files:
        try:
            data = p.read_bytes()
        except OSError as exc:
            skipped += 1
            print(f"\n== {p}\n   跳过（{exc.__class__.__name__}）：被占用"
                  "——引擎也会同样跳过")
            continue
        print(f"\n== {p}  ({len(data)} 字节)  头 {data[:8]!r}")
        body = bsr._block_stream(data)
        if body is None:
            print("   不是 SNSS，跳过")
            continue
        print(f"   块流 {len(body)} 字节（原 {len(data) - 8}）"
              "——两数差很多 = 压缩壳已剥掉")
        hist: dict[int, int] = {}
        urls: list[str] = []
        for tag, payload in bsr._iter_blocks(body):
            hist[tag] = hist.get(tag, 0) + 1
            u = bsr._scan_first_url(payload)
            if u and u not in urls:
                urls.append(u)
        for tag, n in sorted(hist.items(), key=lambda kv: -kv[1]):
            print(f"   块 {tag:#010x} ({_ascii_guess(tag)}) × {n}")
        print(f"   锚点扫出 {len(urls)} 个网址：")
        for u in urls[:5]:
            print(f"     {u}")
    print(f"\n被占用跳过 {skipped} 个文件")
    tabs = bsr.read_open_tabs(files)
    print(f"引擎 read_open_tabs 结果：{len(tabs)} 个标签")
    for t in tabs[:10]:
        print(f"   [{t.source}] {t.title or '（无标题）'} — {t.url}")


if __name__ == "__main__":
    main()
