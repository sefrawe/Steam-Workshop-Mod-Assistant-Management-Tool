"""core/acfParser.py · Steam workshop ACF 解析（盘点轮新写件）。

ACF 是 Valve 的类 VDF 文本格式。steamcmd 下载的 mod 在
…/steamapps/workshop/content/<appid>/ 下的同时，workshop 目录
（上两级）有一份 appworkshop_<appid>.acf，记着每个已下载 mod 的
盘面事实。认领候选的"盘面版本"（timeupdated）就从这里读——
R19 的合法读法：盘面产物只读不写，认领值要过确认门才算账。

为什么独立成件：格式解析自成一体，pytest 直接喂文本就测，不碰
磁盘不碰账本（V1 把解析埋在 localScanner 肚子里没法单测，教训）。
只认 WorkshopItemsMetadata.timeupdated（工坊侧最后更新时间 = 版本）；
WorkshopItemsInstalled 的安装时间不是版本，不混用。
"""
import re
from pathlib import Path

_INT_TOKEN = re.compile(r"^-?\d+$")


def _tokenize(text: str) -> list[str]:
    """引号串（含转义）与花括号切成 token 流。ACF 无注释语法。"""
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif text[j] == '"':
                    break
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append("".join(buf))
            i = j + 1
        elif c in "{}":
            tokens.append(c)
            i += 1
        else:                       # 无引号裸词（少见，容忍）
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def parse_acf_text(text: str) -> dict:
    """ACF 文本 → 嵌套 dict。空文本 / 键后缺值 / 括号不齐 →
    ValueError（显式爆炸，调用方记日志后自行决定是否继续）。"""
    tokens = _tokenize(text)
    if not tokens:
        raise ValueError("ACF 文本为空")
    pos = 0

    def block() -> dict:
        nonlocal pos
        out: dict = {}
        while pos < len(tokens):
            if tokens[pos] == "}":
                pos += 1
                return out
            key = tokens[pos]
            pos += 1
            if pos >= len(tokens):
                raise ValueError(f"ACF 文本不完整：键 {key!r} 后缺值")
            val = tokens[pos]
            if val == "{":
                pos += 1
                out[key] = block()
            else:
                out[key] = val
                pos += 1
        return out

    return block()


def workshop_versions(tree: dict) -> dict[int, int]:
    """提取 {mod_id: timeupdated}。路径：AppWorkshop → apps →
    <appid> → WorkshopItemsMetadata → <mod_id> → timeupdated。
    结构对不上（键缺失/形状变化）按"没有元数据"处理返回空表——
    盘点照做，候选版本留 NULL（认成版本未知，检测补全）。"""
    result: dict[int, int] = {}
    apps = tree.get("AppWorkshop", {}).get("apps", {})
    if not isinstance(apps, dict):
        return result
    for app in apps.values():
        if not isinstance(app, dict):
            continue
        meta = app.get("WorkshopItemsMetadata", {})
        if not isinstance(meta, dict):
            continue
        for mid, node in meta.items():
            if isinstance(node, dict) and "timeupdated" in node:
                try:
                    result[int(mid)] = int(node["timeupdated"])
                except (TypeError, ValueError):
                    continue
    return result


def read_workshop_versions(acf_path) -> dict[int, int]:
    """读一份 appworkshop acf 并提取版本表。文件不存在 →
    FileNotFoundError 照抛；格式坏 → ValueError——都由调用方
    决定算不算事（盘点引擎：记进报告继续扫）。"""
    text = Path(acf_path).read_text(encoding="utf-8", errors="replace")
    return workshop_versions(parse_acf_text(text))
