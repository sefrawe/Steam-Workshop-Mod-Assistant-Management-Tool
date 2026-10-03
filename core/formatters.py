"""显示层格式化
"""
"""
core/formatters.py · 列表与详情共用的"给人看"格式化函数。

边界约定：数据库永远存 Unix 时间戳等原始值，格式化只发生在这里；
任何页面要"3 天前 / 2024-05-01 12:00"都调这几个函数，不要自己再写
一份（一处算清，别处只读）。详情面板会同时展示原始值和格式化值——
调试时要能直接核对库里的原始数据。

状态显示词不自持：STATUS_ZH 单源在 core/constants（D37 一级单源；
tracked 显示「待下载」，见 D30——在账本里、还没下载，一词写实）。
要改显示词去 constants 改，全项目自动跟。
"""

import time
from datetime import datetime

from core.constants import STATUS_ZH


def relative_time(ts: int | None, now: int | None = None) -> str:
    """Unix 秒 → "3 天前"；None/0 → "从未"。
    now 参数留给测试注入"当前时间"用，正常调用不传。
    时钟偏差 / 未来时间一律按"刚刚"处理（diff 兜底为 0），
    不显示负数也不会崩。
    """
    if not ts:
        return "从未"
    now = int(time.time()) if now is None else now
    diff = max(now - ts, 0)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    days = diff // 86400
    if days < 365:
        return f"{days} 天前"
    return f"{days // 365} 年前"


def abs_time(ts: int | None) -> str:
    """Unix 秒 → "2024-05-01 12:00"（本地时区）；空值 → "—"。
    只用于给人看。任何大小比较、更新判定都不允许用它，
    一律拿原始时间戳比。
    """
    return datetime.fromtimestamp(ts).strftime(
        "%Y-%m-%d %H:%M") if ts else "—"


def fmt_size(n: int | None) -> str:
    """字节数 → "1.5 MiB" 这样的可读大小；None / 负数 → "—"。
    1024 进制（KiB / MiB / GiB）。负数不当 0 处理：
    大小出负数本身就是异常数据，显示"—"提醒人去看，而不是糊弄成 0。
    """
    if n is None or n < 0:
        return "—"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def status_zh(status: str | None) -> str:
    """库内状态值 → 中文显示词（下拉框、表格、详情三处共用）。
    词表单源在 core/constants.STATUS_ZH；空 → "—"；
    未知值原样返回（不吞新状态）。
    """
    if not status:
        return "—"
    return STATUS_ZH.get(status, status)
