"""批量下载编排测试
"""
"""
用假的发命令函数和手工投喂的结论/空闲信号，把状态机按时序
从头到尾走一遍。全部纯逻辑，不碰 Qt、不碰数据库。
运行：python -m pytest tests/test_batchDownloadFlow.py -v
"""
from core import batchDownloadFlow as bf
from core import outputAnalyzer as oa


class FakeSender:
    """假发命令函数：记录发过的命令；ok=False 模拟"发不出去"。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.ok = True

    def __call__(self, text: str) -> bool:
        if not self.ok:
            return False
        self.sent.append(text)
        return True


def make_flow(mod_ids=(1, 2, 3), app_id=1158310):
    sender = FakeSender()
    events: list[dict] = []
    flow = bf.BatchDownloadFlow(app_id, list(mod_ids),
                                send_command=sender, on_event=events.append)
    return flow, sender, events


def v(kind, **kw):
    return oa.Verdict(kind=kind, **kw)


def types(events):
    return [e["type"] for e in events]


# ---------- 基本节奏：发一条 → 结论 → 空闲 → 下一条 ----------

def test_basic_pacing_one_by_one():
    flow, sender, events = make_flow()
    assert flow.start() is True
    # 没给登录命令：start 直接发第一条
    assert sender.sent == ["workshop_download_item 1158310 1"]

    # 开始行只确认，不影响状态
    flow.on_verdict(v(oa.KIND_DOWNLOAD_STARTED, mod_id=1))
    # 启动初期多余的空闲信号不能触发提前发下一条
    flow.on_idle()
    assert len(sender.sent) == 1

    # 第 1 条成功 → 等空闲 → 空闲到了才发第 2 条
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1,
                      size_bytes=100, path="x"))
    assert len(sender.sent) == 1  # 结论到了但提示符没来：不发
    flow.on_idle()
    assert sender.sent[-1] == "workshop_download_item 1158310 2"

    # 第 2 条失败 → 空闲 → 第 3 条
    flow.on_verdict(v(oa.KIND_DOWNLOAD_FAILED, mod_id=2, reason="Failure"))
    flow.on_idle()
    assert sender.sent[-1] == "workshop_download_item 1158310 3"

    # 第 3 条超时 → 空闲 → 队列空 → 批次完成
    flow.on_verdict(v(oa.KIND_DOWNLOAD_TIMEOUT, mod_id=3))
    flow.on_idle()
    assert flow.state == bf.ST_DONE
    done = events[-1]
    assert done["type"] == "batch_done"
    s = done["summary"]
    assert s["total"] == 3
    assert [r.mod_id for r in s["ok"]] == [1]
    assert [r.mod_id for r in s["failed"]] == [2]
    assert [r.mod_id for r in s["timeout"]] == [3]
    assert s["stopped"] is False


def test_start_twice_rejected_and_empty_rejected():
    flow, _, _ = make_flow()
    assert flow.start() is True
    assert flow.start() is False  # 已在跑
    flow2, _, _ = make_flow(mod_ids=[])
    assert flow2.start() is False  # 空列表


# ---------- 登录阶段 ----------

def test_login_first_then_download():
    flow, sender, events = make_flow(mod_ids=(7,))
    assert flow.start(login_cmd="login bob") is True
    assert sender.sent == ["login bob"]
    flow.on_verdict(v(oa.KIND_LOGIN_OK))
    assert sender.sent[-1] == "workshop_download_item 1158310 7"
    assert "login_ok" in types(events)


def test_login_failure_goes_manual_then_resume():
    flow, sender, events = make_flow(mod_ids=(7,))
    flow.start(login_cmd="login bob")
    flow.on_verdict(v(oa.KIND_NOT_LOGGED_ON))
    assert flow.state == bf.ST_NEED_LOGIN
    flow.on_idle()  # NEED_LOGIN 下空闲不该发东西
    assert len(sender.sent) == 1
    flow.resume()  # 用户手动登录后点继续 → 没给登录命令，直接下载
    assert sender.sent[-1] == "workshop_download_item 1158310 7"
    assert flow.state == bf.ST_RUNNING


# ---------- 下载中途发现未登录 ----------

def test_not_logged_on_midway_requeues_current():
    flow, sender, events = make_flow()
    flow.start()
    flow.on_verdict(v(oa.KIND_NOT_LOGGED_ON))
    assert flow.state == bf.ST_NEED_LOGIN
    # 当前条（1）放回队首：继续后先重试它
    flow.resume()
    assert sender.sent[-1] == "workshop_download_item 1158310 1"
    # 之后剩余 2、3 顺序不变
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=1))
    flow.on_idle()
    assert sender.sent[-1] == "workshop_download_item 1158310 2"


# ---------- 断线与杂音不打断批次 ----------

def test_disconnect_and_noise_do_not_break_flow():
    flow, sender, _ = make_flow(mod_ids=(1,))
    flow.start()
    flow.on_verdict(v(oa.KIND_DISCONNECTED, code=3, note="No Connection"))
    flow.on_verdict(v(oa.KIND_COMMAND_NOT_FOUND, note="xyz"))
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=5))
    flow.on_idle()
    assert flow.state == bf.ST_DONE
    assert flow.total == 1


# ---------- 停止：温和的 ----------

def test_stop_while_running_finishes_current_item():
    flow, sender, events = make_flow()
    flow.start()          # 发了 1
    flow.stop()           # 置 STOPPING
    flow.on_idle()        # RUNNING 下的空闲不算数（等结论）
    assert len(sender.sent) == 1
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=9))
    flow.on_idle()        # 当前条跑完且空闲 → 收尾
    assert flow.state == bf.ST_DONE
    s = events[-1]["summary"]
    assert s["stopped"] is True
    assert len(sender.sent) == 1  # 2、3 没有发


def test_stop_while_waiting_idle_finishes_immediately():
    flow, sender, events = make_flow()
    flow.start()
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=9))
    flow.stop()           # 正等空闲 → 直接收尾
    assert flow.state == bf.ST_DONE
    assert events[-1]["summary"]["stopped"] is True
    flow.on_idle()        # 迟到的空闲不再发下一条
    assert len(sender.sent) == 1


def test_stop_while_need_login_finishes():
    flow, _, _ = make_flow()
    flow.start()
    flow.on_verdict(v(oa.KIND_NOT_LOGGED_ON))
    flow.stop()
    assert flow.state == bf.ST_DONE


# ---------- 发送失败 ----------

def test_send_failure_aborts_batch():
    flow, sender, events = make_flow()
    sender.ok = False
    assert flow.start() is False
    assert flow.state == bf.ST_DONE
    assert types(events)[-1] == "batch_done"
    assert events[-1]["summary"]["error"]


# ---------- batch_done 只发一次 ----------

def test_batch_done_emitted_once():
    flow, _, events = make_flow(mod_ids=(1,))
    flow.start()
    flow.on_verdict(v(oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=1))
    flow.on_idle()
    flow.on_idle()
    flow.on_idle()
    assert types(events).count("batch_done") == 1
