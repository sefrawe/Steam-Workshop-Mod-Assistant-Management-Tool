"""网址解析器"""
"""
从工坊网址或纯数字片段提取 mod 编号。接受两种形式：
- 纯数字（如 3403925213）
- 含 steamcommunity.com 且带 id 参数的网址（参数位置、顺序不限）
按空白切分逐段解析，兼容"每行一个"与"整段空格分隔"两种导出格式；
同批自动去重，无法识别的片段原样回报给调用方展示。
"""
import re
from dataclasses import dataclass, field
from typing import Iterable

_ID_PARAM = re.compile(r"[?&]id=(\d+)")


def extract_mod_id(text: str) -> int | None:
    """单个片段 → mod 编号；无法识别返回 None。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "steamcommunity.com" in text:
        m = _ID_PARAM.search(text)
        if m:
            return int(m.group(1))
    return None


@dataclass
class ParseReport:
    mod_ids: list[int] = field(default_factory=list)   # 去重后按出现顺序
    invalid: list[str] = field(default_factory=list)   # 无法识别的原始片段


def parse_lines(lines: Iterable[str]) -> ParseReport:
    seen: set[int] = set()
    ids: list[int] = []
    invalid: list[str] = []
    for raw in lines:
        for token in raw.split():      # 行内多网址空格分隔也能解析
            mid = extract_mod_id(token)
            if mid is None:
                invalid.append(token)
            elif mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ParseReport(mod_ids=ids, invalid=invalid)
