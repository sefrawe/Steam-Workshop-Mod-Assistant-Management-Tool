"""联网查询工作线程
"""
r"""gui/remoteQueryWorker.py · 联网查询工作线程（三页共用件）。

异常处置【联网深度检测】、远端健康页、依赖检测页三处的批循环完全
同构（100/批、批间礼貌间隔、批边界生效的停止协议、取消事件中断
等待/重试）——V1 抄了三份，V2 收编成一件。

只发网络请求；分类与写库都在主线程回调（repo 不过线程）。查询
函数由调用方以工厂注入：make_query(cancel_event) -> query_fn。
客户端在线程内构建、cancel_event 由线程持有（等待与重试的中断
靠它）——匿名 GetDetails 与 keyed GetDetails 的差异全部住在
页面闭包里，线程本体不认识客户端类型。

停止语义（照 V1 _DeepWorker 口径）：stop() 置停止位 + 触发取消
事件——客户端的等待与重试立即中断，最多多等一次在途请求的超时；
已查到的数据一律丢弃、走 stopped 信号——"停止 = 什么都没发生"。
"""
import time
import threading
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

from core.steamApiClient import SteamApiCancelled, SteamApiError

_BATCH = 100   # Steam 官方单批上限（constants.BATCH_SIZE 同值；线程私有副本防 gui→core 常量误用出环）

# 查询工厂形状：收取消事件，交回"一批编号 → 条目列表"的查询函数
QueryFactory = Callable[[threading.Event], Callable[[list[int]], list]]


def workshop_item_to_entry(item) -> dict:
    """WorkshopItem → 匿名接口引擎契约的纯数据条目。
    只转分类要用的字段；引擎不 import 客户端类型（注入边界）。
    banned 用 getattr 宽容提取：响应没带时为 None，由引擎的
    missing_banned_field 口径显式降级——绝不静默当 0。"""
    return {"publishedfileid": item.mod_id, "result": item.result,
            "file_size": item.file_size,
            "title": getattr(item, "title", None),
            "banned": getattr(item, "banned", None)}


def keyed_item_to_entry(item) -> dict:
    """KeyedItem → keyed 接口引擎契约的纯数据条目。children 三态
    （list / None=键缺席）原样保留。"""
    return {"mod_id": item.mod_id, "result": item.result,
            "title": item.title, "children": item.children}


class RemoteQueryWorker(QThread):
    """批查询线程。四信号：batch_done(已查数) / succeeded(全部条目
    dict) / failed(请求层失败原因) / stopped(用户停止=什么都没发生)。"""
    batch_done = Signal(int)
    succeeded = Signal(list)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, mod_ids, *, make_query: QueryFactory, to_entry,
                 interval_ms: int, max_retries: int,
                 parent=None) -> None:
        super().__init__(parent)
        self._ids = list(dict.fromkeys(int(i) for i in mod_ids))  # 去重保序
        self._make_query = make_query
        self._to_entry = to_entry
        self._interval_ms = max(0, int(interval_ms))
        self._max_retries = int(max_retries)
        self._stop_requested = False
        self._cancel = threading.Event()

    def stop(self) -> None:
        """请求停止：等待与重试立即中断，只剩在途的那一个请求要等。"""
        self._stop_requested = True
        self._cancel.set()

    def run(self) -> None:
        query = self._make_query(self._cancel)
        done = 0
        entries: list[dict] = []
        try:
            for start in range(0, len(self._ids), _BATCH):
                if self._stop_requested:
                    self.stopped.emit()
                    return
                chunk = self._ids[start:start + _BATCH]
                items = query(chunk)
                entries.extend(self._to_entry(i) for i in items)
                done += len(chunk)
                self.batch_done.emit(done)
                if start + _BATCH < len(self._ids):
                    # 批间礼貌间隔；等待期间无法响应停止（取消事件
                    # 已让客户端侧的等待可中断，这里是批与批之间的
                    # 纯 sleep），最多多等一个间隔——与 V1 同口径
                    time.sleep(self._interval_ms / 1000)
        except SteamApiCancelled:
            # 取消事件在请求/等待中途触发：与批边界停止同一收场
            self.stopped.emit()
            return
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(entries)
