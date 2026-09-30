"""Steam 网络客户端
"""
"""
封装 Steam 官方的三个查询接口（都不需要 API Key，与旧脚本一致）：

- GetPublishedFileDetails：一次最多查 100 个工坊条目的远端信息
  （标题、作者、更新时间、大小、订阅数、收藏数、浏览数、标签、预览图）
- GetCollectionDetails：查某个"合集"里包含哪些条目
- appdetails（商店公开接口）：查游戏名（添加档案对话框用）


本文件只负责发请求和解析响应，不碰数据库，也不碰界面。

三个决定解析代码写法的关键事实：
1. Steam 返回的数字经常是字符串（如 "294100"），偶尔缺失——所以数值
   一律走 _to_int()：能转就转，转不动/缺失给 None，绝不让一个奇怪
   的字段炸掉整批结果
2. 响应里每个条目自带 result 字段：1=查询成功，非 1=该条目有问题
   （被删除/设为私有/查无此条）——这是"逐条判定"的依据，不是整体错误，
   所以原样保留在 WorkshopItem 里，分类交给上层做
3. 服务器可能限流（HTTP 429/503）——遇到就重试，每次等待时间翻倍
   （指数退避），重试用完仍失败才抛异常。错误显式爆炸，绝不静默
   返回空结果——静默的空结果会让"全部 mod 都是最新"变成一个谎言

关于取消（v2.45）：客户端支持协作式取消——构造时注入 cancel_event，
调用方 set() 后：等待中（批间隔/重试退避）立即中断、重试不再发起、
整批查询尽快抛 SteamApiCancelled。已在途的单个 HTTP 请求无法被打断
（requests 的阻塞调用），它受超时约束（连接 10 秒/读取 30 秒）
——所以"点停止到真停"的上限 = 一次在途请求的超时，而不是
"重试次数 × 超时"的数分钟。


关于合集：远端响应没有一个官方写明的"这是合集"标记字段，所以本客户端
不做合集判定，只提供查询手段：
- 上层把 file_size 缺失/为 0 的条目归入"疑似合集/异常"，由用户裁决
- 用户确认要展开时，调 query_collection_children() 拿成员编号列表

关于证书校验：requests 默认用它自带的 certifi 证书名单，而浏览器和
Python 标准库用的是 Windows 系统证书库。部分网络环境（代理/安全软件）
会换发服务器证书——系统库认识、certifi 不认识，导致误报
CERTIFICATE_VERIFY_FAILED。装了 truststore 就让 requests 也走系统
证书库（行为与浏览器一致，校验本身没有被关闭）；没装则维持原样。
"""
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import threading

import requests

# 可选增强：注入失败不应影响程序其余部分，所以吞掉一切异常并注明原因
try:
    import truststore
    truststore.inject_into_ssl()  # 全进程生效：此后所有 HTTPS 都走系统证书库
except Exception:  # 没安装 / 环境不支持 → 退回 certifi 默认行为
    pass

DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
COLLECTION_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
STORE_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# 官方接口的单次查询上限：超过 100 个必须自己分批
_BATCH = 100
# 单次请求超时：连接 10 秒 / 读取 30 秒（元组给 requests）。
# 连接阶段单独收紧——卡死场景（代理半死，如 watt toolkit 挂着）
# 多半吊在 TCP 连接上，10 秒连不上就该放弃本轮尝试
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30

# 遇到这两个状态码值得重试（典型限流）；其他非 200 直接报错，不多耗时间
_RETRY_STATUSES = frozenset({429, 503})


class SteamApiError(RuntimeError):
    """Steam 接口请求层面的失败（连不上/限流耗尽/响应格式不对）。
    注意与"单个条目 result 非 1"区分：后者是查询结果，不是请求失败。"""

class SteamApiCancelled(SteamApiError):
    """调用方请求取消（点了停止）——不是网络失败。
    继承 SteamApiError 让既有"except SteamApiError"兜底仍接得住
    （万一漏接也不会变成无声崩溃）；工作线程先接本类、再接父类，
    取消走 stopped 路径、不弹"检测失败"。"""

def _to_int(value) -> int | None:
    """Steam 的数字字段经常以字符串形式出现，偶尔缺失。
    能转就转，转不动/缺失返回 None——解析层绝不因此抛异常。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WorkshopItem:
    """一条工坊条目的远端信息（GetPublishedFileDetails 单条解析结果）。

    任何字段都可能为 None——本类只忠实转存，不做任何"这像不像合集"
    的判断。"正常条目 / 疑似合集 / 查询失败"的三桶分类在上层做，
    依据：result 是否为 1、file_size 是否缺失或为 0。
    """
    mod_id: int | None
    result: int | None        # 1=成功；非 1=该条目失效/私有/查无此条
    title: str | None
    creator: str | None
    time_created: int | None
    time_updated: int | None
    file_size: int | None
    subscriptions: int | None
    favorited: int | None
    views: int | None
    tags: list[str]           # 无标签时为空列表（与库里"空列表=清空"语义对齐）
    preview_url: str | None

    @classmethod
    def from_api(cls, d: dict) -> "WorkshopItem":
        raw_tags = d.get("tags")
        tags = [t["tag"] for t in raw_tags
                if isinstance(t, dict) and t.get("tag")] \
            if isinstance(raw_tags, list) else []
        return cls(
            mod_id=_to_int(d.get("publishedfileid")),
            result=_to_int(d.get("result")),
            title=str(d["title"]) if d.get("title") is not None else None,
            creator=str(d["creator"]) if d.get("creator") is not None else None,
            time_created=_to_int(d.get("time_created")),
            time_updated=_to_int(d.get("time_updated")),
            file_size=_to_int(d.get("file_size")),
            subscriptions=_to_int(d.get("subscriptions")),
            favorited=_to_int(d.get("favorited")),
            views=_to_int(d.get("views")),
            tags=tags,
            preview_url=(str(d["preview_url"])
                         if d.get("preview_url") is not None else None),
        )


class SteamApiClient:
    """两个接口的统一入口。

    请求间隔和重试次数由主窗口从设置读出后注入，本类不碰配置文件；
    session 参数供测试注入假对象（不发真实网络请求），平时用默认值。
    """

    def __init__(self, session: requests.Session | None = None, *,
                 interval_ms: int = 200, max_retries: int = 3,
                 sleeper: Callable[[float], None] = time.sleep,
                 cancel_event: threading.Event | None = None) -> None:
        self._session = session if session is not None else requests.Session()
        self._interval_ms = max(0, int(interval_ms))
        self._max_retries = max(0, int(max_retries))
        # 计时函数注入：生产环境是 time.sleep，测试换成"只记录不等待"
        self._sleep = sleeper
        # 协作式取消（v2.45）：不传 = 自备永不置位的事件（行为与
        # 旧版完全一致）；工作线程把 stop 用的同一个事件传进来
        self._cancel = (cancel_event if cancel_event is not None
                        else threading.Event())

    # ---------- 取消（协作式）----------
    def cancel(self) -> None:
        """请求取消：等待中（批间隔/重试退避）立即中断，重试不再发起。
        已在途的单个 HTTP 请求无法被打断（requests 的阻塞调用没有
        中途取消的口子），它受超时约束——点停止到真停的上限 =
        一次在途请求的超时（连接 10 秒/读取 30 秒）。"""
        self._cancel.set()

    def _check_cancel(self) -> None:
        """取消检查点：置位即抛 SteamApiCancelled。已取到的数据由
        调用方按"停止 = 什么都没发生"处置（工作线程丢弃不落库）。"""
        if self._cancel.is_set():
            raise SteamApiCancelled()

    def _sleep_cancellable(self, seconds: float) -> None:
        """可被取消打断的等待：生产环境用 Event.wait（取消立即醒）；
        测试注入了 sleeper 时退化为原样调用——假计时器没有时钟，
        Event.wait 会真睡，注入语义保持纯净。"""
        if self._sleep is not time.sleep:
            self._sleep(seconds)
            return
        self._cancel.wait(seconds)


    # ---------- 对外：批量查条目详情 ----------

    def query_details(self, mod_ids: Iterable[int]) -> list[WorkshopItem]:
        """批量查询，自动去重（保持传入顺序）、自动按 100 个一批分批。
        批与批之间等待请求间隔，礼貌节流，降低被限流的概率。
        取消检查在每个批边界与批间隔里（v2.45）。"""
        ids = list(dict.fromkeys(int(i) for i in mod_ids))
        # 去重且保序
        items: list[WorkshopItem] = []
        for start in range(0, len(ids), _BATCH):
            self._check_cancel()
            chunk = ids[start:start + _BATCH]
            items.extend(self._query_details_chunk(chunk))
            if start + _BATCH < len(ids):
                # 还有下一批才等，最后一批不等；等待可被取消打断
                self._sleep_cancellable(self._interval_ms / 1000)
        return items

    def _query_details_chunk(self, ids: list[int]) -> list[WorkshopItem]:
        # 表单格式是官方要求的数组写法：publishedfileids[0]=xx&publishedfileids[1]=xx
        form: dict[str, str] = {"itemcount": str(len(ids))}
        for i, mid in enumerate(ids):
            form[f"publishedfileids[{i}]"] = str(mid)
        data = self._post_json(DETAILS_URL, form)
        response = data.get("response") if isinstance(data, dict) else None
        details = response.get("publishedfiledetails") \
            if isinstance(response, dict) else None
        if not isinstance(details, list):
            raise SteamApiError(
                "GetPublishedFileDetails 响应缺少 publishedfiledetails 列表")
        return [WorkshopItem.from_api(d) for d in details
                if isinstance(d, dict)]

    # ---------- 对外：查游戏名 ----------

    def query_app_name(self, app_id: int) -> str | None:
        """查游戏名（商店公开接口 appdetails，无需 key，GET 请求）。

        返回值三档（与"逐条判定"同一哲学：查询结果不是错误）：
        - 游戏名 str：success=true 且带非空 name
        - None：查无此 AppID / 没带 data / name 为空——这是商店的正常
          回答，调用方（添加档案对话框）据此让用户手输名字，不拦建档
        - SteamApiError：429/503/断网/坏 JSON 重试耗尽，或 404 等其他
          状态码立即抛——网络层的失败要让人看见，不该冒充"没这个名字"
        """
        data = self._get_json(STORE_APPDETAILS_URL,
                              {"appids": str(app_id), "filters": "basic"})
        # 响应形状名义上是 { "<appid>": {"success": bool, "data": {...}} }，
        # 但外层键不可信——实测查 294100（RimWorld），返回的键是它某个
        # DLC 的编号，载荷却正是 294100 本尊（data.steam_appid 才是权威，
        # 浏览器实测为证）。所以先按键取，取不到就按值取（一次只查一个
        # appid，响应至多一条），最后核验载荷归属，绝不张冠李戴
        entry = None
        if isinstance(data, dict) and data:
            entry = data.get(str(app_id))
            if not isinstance(entry, dict):
                entry = next(iter(data.values()), None)
        if not isinstance(entry, dict) or not entry.get("success"):
            return None
        info = entry.get("data")
        if not isinstance(info, dict):
            return None
        # 归属核验：载荷声明属于别的 appid → 宁可"查无此名"，也不报错名字
        payload_id = info.get("steam_appid")
        if payload_id is not None and str(payload_id) != str(app_id):
            return None
        name = info.get("name")
        return str(name) if name else None

    # ---------- 对外：查合集成员 ----------

    def query_collection_children(self, collection_id: int) -> list[int]:
        """查询合集包含的条目编号列表。

        返回空列表 = 该编号不是合集（或远端数据异常）。这不算错误——
        是一次正常的查询结果，调用方据此向用户提示即可，不要抛异常。
        """
        form = {"collectioncount": "1",
                "publishedfileids[0]": str(collection_id)}
        data = self._post_json(COLLECTION_URL, form)
        response = data.get("response") if isinstance(data, dict) else None
        collections = response.get("collectiondetails") \
            if isinstance(response, dict) else None
        if not isinstance(collections, list) or not collections:
            return []
        children = collections[0].get("children") \
            if isinstance(collections[0], dict) else None
        if not isinstance(children, list):
            return []
        ids: list[int] = []
        for child in children:
            cid = _to_int(child.get("publishedfileid")) \
                if isinstance(child, dict) else None
            if cid is not None:
                ids.append(cid)
        return ids

    # ---------- 内部：带重试的请求 ----------

    def _request_json(self, url: str, send: Callable[[], requests.Response]) -> dict:
        """发一次请求 → 解析 JSON，重试三档（POST/GET 共用这一份逻辑）：
        - 429/503、网络异常、空响应体：值得重试，指数退避（等待翻倍）
        - 其他非 200（如 404）：立刻报错，重试没有意义
        - 重试用完仍失败：抛 SteamApiError，把最后一次的失败原因带出去
        - 取消置位（v2.45）：任何检查点/退避等待里立即抛
          SteamApiCancelled——重试与退避都不再发生
        send 是"怎么发这个请求"的无参函数（POST 还是 GET 由调用方决定），
        本方法只管"发了之后怎么算失败、失败了怎么办"——重试策略只有
        这一份，改判定/改退避只动这里，绝不出现两处走样。
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):  # 首次 + N 次重试
            self._check_cancel()
            try:
                resp = send()
            except requests.RequestException as exc:
                last_exc = exc
                # 连不上/DNS 失败/超时/证书校验失败等，值得重试
            else:
                if resp.status_code in _RETRY_STATUSES:
                    last_exc = SteamApiError(f"HTTP {resp.status_code}（疑似限流）")
                elif resp.status_code != 200:
                    raise SteamApiError(
                        f"HTTP {resp.status_code}，接口：{url}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_exc = exc  # 偶发空响应/坏 JSON，值得重试
            if attempt < self._max_retries:
                # 指数退避：第 1 次重试等 1 个间隔，第 2 次等 2 个，
                # 第 3 次等 4 个……等待可被取消立即打断
                self._sleep_cancellable(
                    self._interval_ms / 1000 * (2 ** attempt))
        self._check_cancel()  # 重试耗尽时若恰好点过停，取消优先于报错
        raise SteamApiError(
            f"请求多次失败（共 {self._max_retries + 1} 次）：{last_exc}")

    def _post_json(self, url: str, form: dict) -> dict:
        """POST 表单查询（两个官方接口的发送方式），重试逻辑见 _request_json。"""
        return self._request_json(
            url, lambda: self._session.post(
                url, data=form, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)))

    def _get_json(self, url: str, params: dict) -> dict:
        """GET 查询（商店接口的发送方式），重试逻辑见 _request_json。"""
        return self._request_json(
            url, lambda: self._session.get(
                url, params=params, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)))
