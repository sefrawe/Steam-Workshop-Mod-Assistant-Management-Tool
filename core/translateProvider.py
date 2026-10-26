"""翻译引擎适配层
"""
from __future__ import annotations

"""
为什么有这个文件：
    翻译通道会换（Edge → Bing → Azure F0 → 将来 LLM），调用方
    （TranslateWorker）只认一个动词。通道差异——会话、鉴权、批量、
    落点域——全部住在本文件的 provider 实现里，换通道零改 worker。

默认通道 = Edge（免鉴权端点）：
    POST https://edge.microsoft.com/translate/translatetext
        ?from=&to=zh-Hans&isEnterpriseClient=false
    Body = JSON 字符串数组；回执与输入同序，translations[].text。
    零 key、零 token、原生批量——接口形状校准自 calibre 的
    Ebook Translator 插件（engines/microsoft.py，长期维护的同端点
    实现）。Bing 网页通道（抠 IG/token 那条）降为备选：2026-10
    实测其页面令牌在部分环境下整批 400，正是"随时改版"的应验。

纪律：
    - provider 只发请求、只翻译，不碰库不碰 UI；
    - 一切失败抛 TranslateProviderError（文案直接给用户看）；
    - 重试/退避/段间限流住 worker，本文件只管单次翻译。
"""

import http.cookiejar
import json
import re
import threading
import urllib.parse
import urllib.request


class TranslateProviderError(Exception):
    """翻译通道错误。文案直接给用户看。"""


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")


class BaseProvider:
    """通道基类：单段 translate + 批量 plan_batches/translate_batch。
    worker 的批量循环只认这三个动词；不支持批量的通道用默认实现
    （每段一组、循环单段）自动退化，零特殊分支。"""

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        self._cancel = cancel_event

    def translate(self, text: str, target: str) -> str:
        raise NotImplementedError

    def plan_batches(self, texts: list[str]) -> list[list[int]]:
        """把段下标分组（每组 = 一次请求）。默认：一段一组。"""
        return [[i] for i in range(len(texts))]

    def translate_batch(self, texts: list[str], target: str) -> list[str]:
        """一组段 → 一组译文（同序，失败抛错——单段容错在 worker）。"""
        return [self.translate(t, target) for t in texts]


class EdgeProvider(BaseProvider):
    """微软 Edge 内置翻译通道（默认）。零 key 零 token：直 POST
    字符串数组即译，原生批量——一条描述通常一两次请求完成。
    非公开端点，改版风险与所有免费通道同级——失败文案给足指引。
    单段超长（>单请求字符上限）时该段单独成批，仍超限就让它失败
    （该段显示「⚠ 该段翻译失败」+原文）——工坊描述到不了这个量级。"""

    _URL = "https://edge.microsoft.com/translate/translatetext"
    _MAX_ELEMS = 50      # 单请求段数上限（保守值，后端实际更宽）
    _MAX_CHARS = 10000   # 单请求总字符上限（保守值）

    # worker 说 zh-CN（Bing 口径）；Azure 系端点要 zh-Hans——别名归一
    _TARGET_ALIAS = {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant"}

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        super().__init__(cancel_event)
        self._opener = urllib.request.build_opener()  # 无 cookie 需求

    @classmethod
    def _norm_target(cls, target: str) -> str:
        return cls._TARGET_ALIAS.get(target, target)

    def _post(self, texts: list[str], target: str) -> list[str]:
        to = urllib.parse.quote(self._norm_target(target))
        url = f"{self._URL}?from=&to={to}&isEnterpriseClient=false"
        body = json.dumps(list(texts), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Origin": "https://www.microsoft.com",
            "Referer": "https://www.microsoft.com/",
        })
        try:
            with self._opener.open(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            raise TranslateProviderError(
                f"Edge 翻译请求失败：{exc}"
                "（网络不通/端点改版——可到设置把间隔调大再试，"
                "或等 Azure key 通道启用）") from exc
        try:
            return [item["translations"][0]["text"] for item in data]
        except (TypeError, KeyError, IndexError) as exc:
            raise TranslateProviderError(
                f"Edge 翻译回执异常：{str(data)[:120]}") from exc

    def translate(self, text: str, target: str) -> str:
        if not text.strip():
            return text
        return self._post([text], target)[0]

    def plan_batches(self, texts: list[str]) -> list[list[int]]:
        batches: list[list[int]] = []
        cur: list[int] = []
        chars = 0
        for i, t in enumerate(texts):
            n = max(len(t), 1)
            if cur and (len(cur) >= self._MAX_ELEMS
                        or chars + n > self._MAX_CHARS):
                batches.append(cur)
                cur, chars = [], 0
            cur.append(i)
            chars += n
        if cur:
            batches.append(cur)
        return batches

    def translate_batch(self, texts: list[str], target: str) -> list[str]:
        if not texts:
            return []
        return self._post(list(texts), target)


class BingWebProvider(BaseProvider):
    """Bing 网页翻译通道（备选）。GET /translator 拿 cookie + IG +
    key/token（params_AbusePreventionHelper），POST /ttranslatev3
    单段翻译。令牌每次会话现取、失败即清空下轮重取。
    ⚠ 2026-10 实测：部分环境下页面令牌整批 400（statusCode 400 /
    errorMessage 空）——该路依赖网页内部结构，属"随时改版"级风险，
    仅作 Edge 通道的对照备选保留。"""

    _PAGE = "https://www.bing.com/translator"

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        super().__init__(cancel_event)
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self._ig: str | None = None
        self._key: str | None = None
        self._token: str | None = None
        self._api: str | None = None
        self._referer: str | None = None

    def _get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with self._opener.open(req, timeout=15) as resp:
            final = resp.geturl()
            body = resp.read().decode("utf-8", "replace")
        host = urllib.parse.urlsplit(final)
        self._api = f"{host.scheme}://{host.netloc}/ttranslatev3"
        self._referer = final
        return body

    def _post(self, url: str, data: dict) -> object:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": _UA,
            "Referer": self._referer or self._PAGE,
        })
        with self._opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise TranslateProviderError(
                f"Bing 回执不是 JSON（HTTP 层可能被拦）：{raw[:120]}") from exc

    def _ensure_token(self) -> None:
        if self._key is not None and self._token is not None and self._api:
            return
        page = self._get(self._PAGE)
        m_ig = re.search(r'IG:"([^"]+)"', page)
        m_key = re.search(
            r"params_AbusePreventionHelper\s*=\s*\[\s*(\d+)\s*,"
            r"\s*\"([^\"]+)\"", page)
        if m_ig is None or m_key is None:
            raise TranslateProviderError(
                "Bing 翻译通道失效（页面取不到 token，可能已改版）——"
                "默认通道已换 Edge，本通道仅备选。")
        self._ig = m_ig.group(1)
        self._key = m_key.group(1)
        self._token = m_key.group(2)

    def translate(self, text: str, target: str = "zh-CN") -> str:
        if not text.strip():
            return text
        self._ensure_token()
        url = self._api + "?=&IID=translator.5023"
        if self._ig:
            url += f"&IG={self._ig}"
        payload = {"fromLang": "auto", "text": text, "to": target,
                   "key": self._key, "token": self._token}
        try:
            data = self._post(url, payload)
        except Exception as exc:
            self._key = self._token = None
            raise TranslateProviderError(f"Bing 请求失败：{exc}") from exc
        try:
            item = data[0]
            return item["translations"][0]["text"]
        except (TypeError, IndexError, KeyError):
            self._key = self._token = None
            raise TranslateProviderError(
                f"Bing 回执异常（{str(data)[:120]}）——若反复出现，"
                "请使用默认的 Edge 通道。")


def make_provider(name: str, *, cancel_event: threading.Event | None = None):
    """provider 工厂（适配层唯一入口）。edge=免鉴权默认通道；
    bing=网页备选；azure=官方 F0 预留位（settings 配 key 后启用）。"""
    if name == "edge":
        return EdgeProvider(cancel_event)
    if name == "bing":
        return BingWebProvider(cancel_event)
    if name == "azure":
        raise TranslateProviderError(
            "Azure 通道是预留位：待设置页加 Ocp-Apim key 配置后启用。")
    raise TranslateProviderError(f"未知翻译通道：{name}")
