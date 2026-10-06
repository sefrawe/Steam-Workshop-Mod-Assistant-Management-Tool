"""网址解析器"""
"""从工坊网址、纯数字片段或 steamcmd 下载命令提取 mod 编号。
接受三种形式：
- 纯数字（如 3403925213）
- 含 steamcommunity.com 且带 id 参数的网址（参数位置、顺序不限）
- steamcmd 下载命令行：workshop_download_item <游戏AppID> <mod编号>
  （大小写不限；允许 +login / +quit 等前后缀与命令同行，旧脚本
  1.7 生成的命令文件、从终端历史里整行复制下来的命令都认）

命令行必须在按空白切分之前整段识别——如果切成散词，命令里的
游戏 AppID 会被误当成 mod 编号混进清单。识别出的游戏 AppID 单独
记进 command_app_ids 带回给调用方：拿它和当前游戏档案比对，
能拦住"把 A 游戏的命令粘进 B 档案"的错档事故（与决策 15 的
防呆同一思路：解析结果只属于当前档案）。

按空白切分逐段解析，兼容"每行一个"与"整段空格分隔"两种导出格式；
同批自动去重；无法识别的片段（包括命令行里的登录名、退出等
修饰词）原样回报给调用方展示——不猜、不纠正、不静默丢弃。
"""
import re
from dataclasses import dataclass, field
from typing import Iterable
# 工坊条目页地址模板——全项目唯一定义点。
# 网址格式的知识属于解析器。占位符用位置形式 {}，但调用方不要自己
# .format——一律调下面的 workshop_url()。历史上 constants.py 与本文件
# 各存了一份模板、占位符风格还不一致（{mod_id} vs {}），按位置填参的
# 调用当场 KeyError——"右键打开工坊页面"闪报错的根因，已归一。
WORKSHOP_URL_TEMPLATE = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


def workshop_url(mod_id: int, stored_url: str | None = None) -> str:
    """mod 编号 → 工坊页面网址（全项目唯一入口）。

    stored_url = 账本里登记过的网址：有且非空白就用它；
    没登记 / 空白 → 按编号现拼（编号→网址是确定函数，认领或手动
    入账的条目没有存过网址也能打开页面）。
    """
    s = (stored_url or "").strip()
    if s:
        return s
    return WORKSHOP_URL_TEMPLATE.format(mod_id)


_ID_PARAM = re.compile(r"[?&]id=(\d+)")
# 下载命令片段：workshop_download_item <游戏AppID> <mod编号>
# 两个编号都要：mod 编号入清单，AppID 带回去和当前档案比对（防错档）
_CMD_ITEM = re.compile(
    r"(?:\+)?workshop_download_item\s+(\d+)\s+(\d+)", re.IGNORECASE)


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
    # 命令行里出现的游戏 AppID（去重、按出现顺序）；没有命令行则为空。
    # 只报告不裁决——和当前档案比不比、怎么拦，由调用方决定
    command_app_ids: list[int] = field(default_factory=list)


def parse_lines(lines: Iterable[str]) -> ParseReport:
    seen: set[int] = set()
    ids: list[int] = []
    invalid: list[str] = []
    cmd_apps: list[int] = []
    for raw in lines:
        # 第一步：整行先认一遍下载命令。命中就收下命令里的 mod 编号，
        # 并把命令片段从行里挖掉，剩下的部分照常按空白切分解析。
        # 挖除是关键：不挖的话 AppID 会被下一步当成纯数字编号收进清单。
        if _CMD_ITEM.search(raw):
            for app_text, mid_text in _CMD_ITEM.findall(raw):
                mid = int(mid_text)
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
                app = int(app_text)
                if app not in cmd_apps:
                    cmd_apps.append(app)
            raw = _CMD_ITEM.sub(" ", raw)
        # 第二步：常规解析（纯数字 / 工坊网址），与升级前行为完全一致
        for token in raw.split():
            mid = extract_mod_id(token)
            if mid is None:
                invalid.append(token)
            elif mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ParseReport(mod_ids=ids, invalid=invalid,
                       command_app_ids=cmd_apps)
