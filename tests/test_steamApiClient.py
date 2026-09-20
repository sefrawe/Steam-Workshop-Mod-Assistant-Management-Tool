"""Steam 网络客户端测试
"""
"""
全部使用假 Session（按脚本依次吐响应），一个网络包都不发，覆盖：
- 分批：100 个一批、批与批之间等待间隔、去重保序
- 解析：字符串数字转 int、缺失字段给 None、标签列表、result 原样保留
- 重试：429/503 指数退避（等待时长逐条断言）、重试耗尽抛异常、
  其他状态码立即报错、网络异常也走重试
- 合集成员：正常解析 / 非合集返回空列表

等待时间通过注入的假计时函数记录（只记录不真的等待），
整套测试秒级跑完。
"""
import pytest
import requests

from core.steamApiClient import (
    COLLECTION_URL, DETAILS_URL, SteamApiError, SteamApiClient,
)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("不是 JSON 响应")  # 模拟坏响应体
        return self._payload


class FakeSession:
    """每次 post() 按顺序弹出一个预置响应；预置的是异常就直接抛。
    预置的是裸 dict 则视为"HTTP 200 的 JSON 响应"，自动包成
    FakeResponse——客户端拿到的东西必须和真实 requests 响应同构，
    否则就是在测试一个不存在的接口形态。"""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, data: dict | None = None, timeout=None):
        self.calls.append((url, dict(data or {})))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):  # 裸 dict = 成功响应，补上状态码壳
            item = FakeResponse(200, item)
        return item


def make_client(session: FakeSession, sleeps: list[float]) -> SteamApiClient:
    return SteamApiClient(
        session, interval_ms=200, max_retries=2,
        sleeper=sleeps.append)  # 假计时：把等待秒数记进列表


def details_payload(items: list[dict]) -> dict:
    return {"response": {"result": 1, "publishedfiledetails": items}}


def normal_item(mod_id: int) -> dict:
    """一个字段齐全的条目样本——注意 Steam 实际返回里数字全是字符串。"""
    return {
        "publishedfileid": str(mod_id),
        "result": 1,
        "title": "全景地图",
        "creator": "76561198000000001",
        "time_created": "1600000000",
        "time_updated": "1788430663",
        "file_size": "30934319",
        "subscriptions": "123456",
        "favorited": "7890",
        "views": "999999",
        "tags": [{"tag": "Map"}, {"tag": "全景"}],
        "preview_url": "https://steamuserimages-a.akamaihd.net/x.jpg",
    }


# ---------- 分批与节流 ----------

def test_details_batches_of_100():
    session = FakeSession([
        details_payload([]), details_payload([]), details_payload([])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details(range(1, 251))  # 250 个 → 100+100+50 三批

    assert len(session.calls) == 3
    assert [len(c[1]) - 1 for c in session.calls] == [100, 100, 50]  # 减去 itemcount
    assert session.calls[0][1]["itemcount"] == "100"
    assert session.calls[2][1]["itemcount"] == "50"
    assert session.calls[0][1]["publishedfileids[0]"] == "1"
    assert session.calls[2][1]["publishedfileids[49]"] == "250"
    assert items == []


def test_details_waits_interval_between_batches():
    session = FakeSession([
        details_payload([]), details_payload([]), details_payload([])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    client.query_details(range(1, 251))

    # 三批之间等两次，最后一批之后不等；每次等一个间隔（0.2 秒）
    assert sleeps == [0.2, 0.2]


def test_details_dedups_preserving_order():
    session = FakeSession([details_payload([])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    client.query_details([7, 3, 7, 5, 3])

    form = session.calls[0][1]
    n = int(form["itemcount"])
    sent = [form[f"publishedfileids[{i}]"] for i in range(n)]
    assert sent == ["7", "3", "5"]  # 重复剔除，顺序不变


# ---------- 解析 ----------

def test_details_parses_string_numbers():
    session = FakeSession([details_payload([normal_item(3403925213)])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([3403925213])

    assert len(items) == 1
    m = items[0]
    assert m.mod_id == 3403925213
    assert m.result == 1
    assert m.title == "全景地图"
    assert m.time_updated == 1788430663  # 字符串成功转 int
    assert m.file_size == 30934319
    assert m.subscriptions == 123456
    assert m.tags == ["Map", "全景"]
    assert m.preview_url.startswith("https://")


def test_details_missing_fields_become_none():
    session = FakeSession([details_payload([{"publishedfileid": "1", "result": 1}])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([1])

    m = items[0]
    assert m.title is None
    assert m.time_updated is None
    assert m.file_size is None
    assert m.tags == []  # 标签缺失 = 空列表，不是 None


def test_details_keeps_result_flag_untouched():
    # result 非 1 的条目（已删除/私有）不是请求失败，是查询结果——
    # 原样交给上层分类，本层绝不丢弃也绝不改写
    session = FakeSession([details_payload([
        {"publishedfileid": "42", "result": 15}])])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([42])

    assert items[0].result == 15
    assert items[0].title is None


# ---------- 重试与报错 ----------

def test_retry_on_429_then_success():
    session = FakeSession([
        FakeResponse(status_code=429),                      # 第一次被限流
        details_payload([normal_item(1)]),                  # 重试成功
    ])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([1])

    assert len(session.calls) == 2
    assert items[0].title == "全景地图"
    assert sleeps == [0.2]  # 第一次重试前等一个间隔


def test_retry_exhausted_raises():
    session = FakeSession([
        FakeResponse(status_code=429),
        FakeResponse(status_code=429),
        FakeResponse(status_code=429),
    ])  # max_retries=2 → 共请求 3 次
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    with pytest.raises(SteamApiError):
        client.query_details([1])

    assert len(session.calls) == 3
    # 指数退避：0.2、0.4（第三次失败后不再等待）
    assert sleeps == [0.2, 0.4]


def test_other_http_status_raises_immediately():
    # 404 这类错误重试没有意义：一次请求就报错，不浪费时间
    session = FakeSession([FakeResponse(status_code=404)])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    with pytest.raises(SteamApiError):
        client.query_details([1])

    assert len(session.calls) == 1
    assert sleeps == []


def test_network_error_is_retried_too():
    session = FakeSession([
        requests.ConnectionError("断网"),
        details_payload([normal_item(2)]),
    ])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([2])

    assert len(session.calls) == 2
    assert items[0].mod_id == 2


def test_bad_json_is_retried():
    # 偶发的空响应/坏 JSON 也值得重试一次
    session = FakeSession([
        FakeResponse(status_code=200, payload=None),
        details_payload([normal_item(3)]),
    ])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    items = client.query_details([3])

    assert items[0].mod_id == 3


# ---------- 合集成员 ----------

def test_collection_children_parsed():
    payload = {"response": {"result": 1, "collectiondetails": [{
        "publishedfileid": "999",
        "result": 1,
        "children": [
            {"publishedfileid": "11", "sortorder": 1},
            {"publishedfileid": "22", "sortorder": 2},
            {"publishedfileid": "abc"},  # 转不了 int 的脏数据直接跳过
        ],
    }]}}
    session = FakeSession([payload])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    ids = client.query_collection_children(999)

    assert ids == [11, 22]
    url, form = session.calls[0]
    assert url == COLLECTION_URL
    assert form == {"collectioncount": "1", "publishedfileids[0]": "999"}


def test_collection_non_collection_returns_empty():
    # 非合集编号（普通 mod）查成员：响应里没有 children → 空列表。
    # 这是正常的查询结果，不是错误——调用方据此提示用户即可
    session = FakeSession([{"response": {"result": 1, "collectiondetails": [
        {"publishedfileid": "5", "result": 1}]}}])
    sleeps: list[float] = []
    client = make_client(session, sleeps)

    assert client.query_collection_children(5) == []
