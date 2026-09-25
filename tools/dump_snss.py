"""临时诊断：dump_snss.py v4（考古专用）"""
"""探针：头部 hexdump / 全偏移 zlib 流扫描 / ASCII 与 UTF-16 网址字样。
用法：python tools/dump_snss.py Edge ["D:\\...\\User Data"]"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    browser = sys.argv[1] if len(sys.argv) > 1 else "edge"
    override = sys.argv[2] if len(sys.argv) > 2 else None
    from core import browserSessionReader as bsr
    files = bsr.find_session_files(browser, override)
    print(f"找到 {len(files)} 个会话文件")
    for p in files:
        try:
            data = p.read_bytes()
        except OSError as exc:
            print(f"\n== {p}\n   跳过（{exc.__class__.__name__}）：被占用")
            continue
        print(f"\n== {p}  ({len(data)} 字节)")
        for i in range(0, min(96, len(data)), 16):
            chunk = data[i:i + 16]
            hexs = " ".join(f"{b:02x}" for b in chunk)
            text = "".join(chr(b) if 32 < b < 127 else "." for b in chunk)
            print(f"   {i:04x}  {hexs:<47}  {text}")
        for name, needle in (("ASCII http", b"http"),
                             ("ASCII steamcommunity", b"steamcommunity"),
                             ("UTF16 http", "http".encode("utf-16-le"))):
            print(f"   扫 {name}: {data.count(needle)} 处")
        hit = None
        for off in range(0, min(4096, len(data) - 16)):
            try:
                out = zlib.decompressobj(15).decompress(data[off:])
            except zlib.error:
                continue
            if out:
                hit = (off, out)
                break
        if hit:
            off, out = hit
            print(f"   ★ 偏移 {off} 处发现 zlib 流，解出 {len(out)} 字节；"
                  f"其中 http 出现 {out.count(b'http')} 次")
            urls = bsr._scan_first_url(out[:200000])
            print(f"   解出内容里的首个网址样本：{urls}")
        else:
            print("   0..4096 内无 zlib 流")


if __name__ == "__main__":
    main()
