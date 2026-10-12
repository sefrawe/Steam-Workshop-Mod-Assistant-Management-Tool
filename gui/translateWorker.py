"""gui/translateWorker.py · 描述翻译工作线程（刀3，2026-10 批量化）。

职责边界（与 RemoteQueryWorker 同家族、同纪律）：
    - 按 provider.plan_batches 分组发请求：支持批量的通道（Edge）
      一组段一次请求——快；不支持的（Bing）自动退化为一段一请求；
    - 整批失败 → 退回逐段重试（单段容错不丢）；全部段失败才报 failed；
    - 不写库不碰 UI：译文分段回执交主线程（succeeded 信号），落库
      由接收 translation_ready 的对象完成（写口 A）；
    - 停止语义：停止 = 什么都没发生（已翻的段不落，收 stopped）；
      点停到真停的上限 = 一次在途请求。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Signal

from core.translateProvider import TranslateProviderError, make_provider


class TranslateWorker(QThread):
    """分段批量翻译线程。信号：progress(已完成, 总数) / succeeded(list，
    与输入段同序，None=该段失败) / failed(原因) / stopped()。"""

    progress = Signal(int, int)
    succeeded = Signal(list)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, segments: list[str], *, provider_name: str,
                 interval_ms: int, max_retries: int,
                 target_lang: str = "zh-CN",
                 parent=None) -> None:
        super().__init__(parent)
        self._segments = list(segments)
        self._provider_name = provider_name
        self._interval_ms = max(0, int(interval_ms))
        self._max_retries = max(0, int(max_retries))
        self._target = target_lang
        self._stop_requested = False
        self._cancel = threading.Event()

    def stop(self) -> None:
        """请求停止：等待与重试立即中断，只剩在途的那一个请求。"""
        self._stop_requested = True
        self._cancel.set()

    # ---- 重试壳：返回结果或最后一个错误（调用方分流）----

    def _attempt(self, fn, retries: int | None = None):
        n = self._max_retries if retries is None else retries
        for attempt in range(n + 1):
            try:
                return fn()
            except TranslateProviderError as exc:
                if self._stop_requested or attempt >= n:
                    return exc
                if self._cancel.wait(1.5 * (attempt + 1)):
                    return exc

    def run(self) -> None:
        total = len(self._segments)
        out: list[str | None] = [None] * total
        done = 0
        last_err = ""
        try:
            provider = make_provider(self._provider_name,
                                     cancel_event=self._cancel)
            for idxs in provider.plan_batches(self._segments):
                if self._stop_requested:
                    self.stopped.emit()
                    return
                batch = [self._segments[i] for i in idxs]
                got = self._attempt(
                    lambda b=batch: provider.translate_batch(b, self._target))
                if isinstance(got, TranslateProviderError):
                    last_err = str(got)
                    # 整批失败 → 逐段救（一次重试，保单段容错）：
                    # 通道真死时每段都会快速失败，不会干等
                    for i in idxs:
                        if self._stop_requested:
                            self.stopped.emit()
                            return
                        one = self._attempt(
                            lambda t=self._segments[i]:
                            provider.translate(t, self._target),
                            retries=1)
                        if not isinstance(one, TranslateProviderError):
                            out[i] = one
                else:
                    for i, r in zip(idxs, got):
                        out[i] = r
                done += len(idxs)
                self.progress.emit(done, total)
                if done < total and self._interval_ms > 0:
                    if self._cancel.wait(self._interval_ms / 1000):
                        self.stopped.emit()
                        return
        except Exception as exc:  # 兜底：任何意外按失败收场，不炸主线程
            self.failed.emit(f"翻译线程异常：{exc}")
            return
        if self._stop_requested:
            self.stopped.emit()
            return
        if out and all(s is None for s in out):
            self.failed.emit(f"{total} 段全部翻译失败：{last_err}")
            return
        self.succeeded.emit(out)
