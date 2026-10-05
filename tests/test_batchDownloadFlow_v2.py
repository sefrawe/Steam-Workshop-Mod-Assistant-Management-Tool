"""batchDownloadFlow v2 增补测试
"""
"""
只覆盖 v2 新增行为（D4 判定分支 + 触发值随行）；v1 状态机的老行为
由既有测试继续把关，两份互不替代。不碰数据库、不碰 Qt、不碰网络：
流程用假发送函数驱动，终端结论用 outputAnalyzer 的 Verdict 手工喂，
空闲提示符用 on_idle() 模拟。跑法与全库一致：项目根目录下 pytest。
"""
from core import batchDownloadFlow as bdf
from core.batchDownloadFlow import BatchDownloadFlow, resolve_written
from core.outputAnalyzer import (
    KIND_DOWNLOAD_FAILED,
    KIND_DOWNLOAD_SUCCESS,
    KIND_DOWNLOAD_TIMEOUT,
    Verdict,
)

_APP_ID = 294100  # 随便一个合法 AppID（要拼进下载命令）；测试不发真请求


def _run_batch(mod_ids, verdicts, triggers=None):
    """把一批从 start() 跑到收尾，返回。"""
    sent: list[str] = []
    events: list[dict] = []

    def _send(text: str) -> bool:
        sent.append(text)   # 记下每条发出的命令（断言用）
        return True         # 假终端永远"发送成功"

    flow = BatchDownloadFlow(
        _APP_ID, list(mod_ids),
        send_command=_send,
        on_event=events.append,
        trigger_versions=triggers,
    )
    flow.start()
    for v in verdicts:
        flow.on_verdict(v)   # 模拟终端吐出一条结论
        flow.on_idle()       # 模拟随后打出的独立 "Steam>" 行
    return flow, sent, events


def _summary(events: list[dict]) -> dict:
    """取收尾汇总（顺带断言"收尾事件只发一次"这条防御还在）。"""
    done = [e for e in events if e["type"] == "batch_done"]
    assert len(done) == 1
    return done[0]["summary"]


# ---------------------------------------------------------------------------
# 触发值随行：汇总里每条 ItemResult 必须带出构造时给的触发值
# ---------------------------------------------------------------------------
def test_success_item_carries_trigger():
    flow, sent, events = _run_batch(
        [111],
        [Verdict(kind=KIND_DOWNLOAD_SUCCESS, mod_id=111, size_bytes=5)],
        triggers={111: 1000},
    )
    assert flow.state == bdf.ST_DONE
    s = _summary(events)
    assert len(s["ok"]) == 1
    assert s["ok"][0].trigger == 1000
    # 发出去的命令与 v1 同款——触发值只进账、不进命令
    assert sent == [f"workshop_download_item {_APP_ID} 111"]


def test_manual_batch_triggers_are_none():
    """手动批次（没给触发表）：全部条目 trigger=None，汇总原样带出。"""
    _flow, _sent, events = _run_batch(
        [222],
        [Verdict(kind=KIND_DOWNLOAD_SUCCESS, mod_id=222, size_bytes=7)],
        triggers=None,
    )
    assert _summary(events)["ok"][0].trigger is None


def test_failed_and_timeout_items_carry_trigger_too():
    """失败与超时也记触发值——判决史要说清"当时想下的是哪个版本"。"""
    _flow, _sent, events = _run_batch(
        [1, 2],
        [Verdict(kind=KIND_DOWNLOAD_FAILED, mod_id=1, reason="Failure"),
         Verdict(kind=KIND_DOWNLOAD_TIMEOUT, mod_id=2)],
        triggers={1: 100, 2: 200},
    )
    s = _summary(events)
    assert [r.trigger for r in s["failed"]] == [100]
    assert [r.trigger for r in s["timeout"]] == [200]
    assert s["ok"] == []


def test_trigger_table_missing_id_is_none():
    """触发表里没有的编号（重试混批等）按无凭证处理，不炸、不编。"""
    _flow, _sent, events = _run_batch(
        [333],
        [Verdict(kind=KIND_DOWNLOAD_SUCCESS, mod_id=333)],
        triggers={999: 1},   # 故意对不上
    )
    assert _summary(events)["ok"][0].trigger is None


# ---------------------------------------------------------------------------
# 判决写入分支（D4）——四种组合全覆盖 + 结构不变式
# ---------------------------------------------------------------------------
def test_resolve_written_verified_when_query_matches():
    assert resolve_written(1000, 1000) == (1000, "verified")


def test_resolve_written_downgrades_when_remote_moved():
    """远端在下载期间又动了（query > trigger）：照写触发值（R18
    不高估），背书等级如实降级——库页灰显，下轮检测再报。"""
    assert resolve_written(1000, 2000) == (1000, "unverified")


def test_resolve_written_query_failed_keeps_trigger():
    assert resolve_written(1000, None) == (1000, "unverified")


def test_resolve_written_no_trigger_is_unknown_even_if_query_ok():
    """R18 的角落：手动批次没有触发值，批查哪怕成功也不写版本——
    批查值发生在下载之后，没有"≤ 盘上内容"的时序保证。"""
    assert resolve_written(None, None) == (None, "unverified")
    assert resolve_written(None, 2000) == (None, "unverified")


def test_written_never_exceeds_trigger():
    """结构不变式（R18）：写出的值要么等于触发值、要么是 NULL。"""
    for trigger in (None, 500, 900):
        for query in (None, 500, 900):
            written, source = resolve_written(trigger, query)
            assert written is None or written == trigger
            assert source in ("verified", "unverified")
