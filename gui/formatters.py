"""显示层格式化（G2 起，列表与详情共用）
"""
"""
约定：库里永远是 Unix 时间戳原始值；这里只做"给人看"的转换。
详情面板会同时展示原始值和格式化值——调试需要原始数据。
"""
import time
from datetime import datetime


def relative_time(ts: int | None, now: int | None = None) -> str:
    """Unix 秒 → '3 天前'；None/0 → '从未'。"""
    if not ts:
        return "从未"
    now = int(time.time()) if now is None else now
    diff = max(now - ts, 0)  # 时钟偏差/未来时间一律按"刚刚"处理
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
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


def fmt_size(n: int | None) -> str:
    if n is None or n < 0:
        return "—"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

# 数据库状态值 → 中文显示（下拉框、表格、详情三处共用，别各写各的）
STATUS_ZH: dict[str, str] = {
    "tracked": "已收录",
    "downloaded": "已下载",
    "deleted": "已删除",
    "failed": "已失败",
}


def status_zh(status: str | None) -> str:
    if not status:
        return "—"
    return STATUS_ZH.get(status, status)

