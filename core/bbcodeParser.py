"""core/bbcodeParser.py · 工坊描述 BBCode → 块序列 → HTML。

为什么有这个文件：
    Steam 工坊描述是一整段 BBCode（图片/视频/链接以标签内嵌正文，
    不是独立字段）。详情页要三种看法（原文/译文/对照），翻译要把
    文本一段段发引擎而标签骨架不动——三件事共用同一个前提：先把
    BBCode 拆成"文本块 + 链接块"的有序序列。本文件就是那次拆分
    与三种渲染的唯一实现（面板与刀3 的 worker 都从这里吃，绝不
    各拆各的）。

容错铁律（剥壳留肉）：
    - 认识的标准标签转换：[img]URL[/img]、[youtube]/[video]、
      [url=…]文字[/url] → 链接行（零下载，点开浏览器看）；
    - 其余一切标签剥壳留肉：[b]粗[/b] → 粗、[size=5]大[/size] → 大、
      [list][*]甲[*]乙[/list] → • 甲 • 乙——内容一个字不丢；
    - 纯文本里偶然的方括号（"[哈哈]"、"[EOS R6]"）不是标签，不动；
    - 作者贴进来的 HTML/尖括号在渲染边界全过 html.escape，只当字看。

块序即段序：
    译文分段数组（刀3 落库的 description_trans JSON）与 text 块按
    出现顺序一一对齐——对照态"第 N 段原文贴第 N 段译文"靠这个序，
    谁也不许私自重排。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass


# ---------- 块模型 ----------

@dataclass
class DescBlock:
    """描述块：kind="text" = 可翻译文本段（text = 原文段）；
    kind="link" = 非文本内容的链接行（url = 目标，text = 显示文案，
    不参与翻译）。"""

    kind: str
    text: str
    url: str | None = None


# ---------- 正则（模块级编译一次） ----------

_RE_IMG = re.compile(r"\[img\]\s*(.*?)\s*\[/img\]", re.I | re.S)
_RE_URL = re.compile(r"\[url(?:=([^\[\]]*))?\]\s*(.*?)\s*\[/url\]",
                     re.I | re.S)
_RE_YT = re.compile(r"\[youtube\]\s*(.*?)\s*\[/youtube\]", re.I | re.S)
_RE_VID = re.compile(r"\[video\]\s*(.*?)\s*\[/video\]", re.I | re.S)
_RE_LISTITEM = re.compile(r"\[\*\]")
# 剥壳留肉的通用壳：字母开头的 [tag] / [/tag] / [tag=参数]（标签名
# 与参数不含空格/方括号）——纯文本里偶然的方括号短语不受影响
_RE_TAG = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9_\-]*(?:=[^\[\]\n]*)?\]")
_RE_SENTINEL = re.compile("\x00(\\d+)\x00")
_RE_BLANK3 = re.compile(r"\n{3,}")
_RE_TAIL_WS = re.compile(r"[ \t]+\n")
_YT_ID = re.compile(r"^[A-Za-z0-9_\-]{11}$")
# 裸链接（作者没包 [url] 的纯文本 URL）——渲染层 linkify 用。
# 刻意不在 parse_description 里转链接块：段结构一动，已落库的
# description_trans 按旧段序的对齐就全错了（2026-10 设计裁决）
_RE_BARE_URL = re.compile(r"https?://[^\s<>\"']+")


# ---------- 解析 ----------

def _youtube_url(content: str) -> str | None:
    """[youtube]/[video] 内容 → 可打开的 URL；解析不动返回 None
    （调用方退回正文，绝不造假链接）。裸 11 位 = 视频 id。"""
    c = content.strip()
    if _YT_ID.match(c):
        return "https://www.youtube.com/watch?v=" + c
    low = c.lower()
    if low.startswith(("http://", "https://")):
        return c
    if low.startswith("www."):
        return "https://" + c
    return None


def parse_description(raw: str | None) -> list[DescBlock]:
    """BBCode 原文 → 块序列。空描述/剥完是空的 → 空列表。

    两段策略：先 stash 再归位——认识的内嵌标签换成 \\x00序号\\x00 哨兵
    混进正文流，等文本清洗（[*]→圆点、剥壳留肉、压空行）按空行切段
    后，再按哨兵原位插回链接块。顺序全程不乱。
    """
    if not raw:
        return []

    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    links: list[DescBlock] = []

    def stash(block: DescBlock) -> str:
        links.append(block)
        return f"\x00{len(links) - 1}\x00"

    def on_yt(m: re.Match) -> str:
        url = _youtube_url(m.group(1))
        return (stash(DescBlock("link", "▶ 视频 · 点击打开", url))
                if url else m.group(1))

    img_n = 0

    def on_img(m: re.Match) -> str:
        nonlocal img_n
        u = m.group(1).strip()
        if u.lower().startswith(("http://", "https://")):
            img_n += 1
            return stash(DescBlock("link", f"🖼 图片 {img_n} · 点击打开",
                                   u))
        return u  # 不是网址的 [img] 内容当正文留下

    def on_url(m: re.Match) -> str:
        target = (m.group(1) or "").strip()
        label = (m.group(2) or "").strip()
        if not target:
            target = label
        url = None
        if target.lower().startswith(("http://", "https://")):
            url = target
        elif target.lower().startswith("www."):
            url = "https://" + target
        if url is None:
            return label or target  # 不是网址：留正文
        if not label:
            label = url
        label = _RE_TAG.sub("", label).strip() or url
        return stash(DescBlock("link", label, url))

    s = _RE_YT.sub(on_yt, s)
    s = _RE_VID.sub(on_yt, s)
    s = _RE_IMG.sub(on_img, s)
    s = _RE_URL.sub(on_url, s)

    # 文本清洗：列表项给圆点；其余标签剥壳留肉；压空行、去行尾空白
    s = _RE_LISTITEM.sub("\n• ", s)
    s = _RE_TAG.sub("", s)
    s = _RE_BLANK3.sub("\n\n", s)
    s = _RE_TAIL_WS.sub("\n", s).strip()
    if not s:
        return []

    blocks: list[DescBlock] = []
    for para in s.split("\n\n"):
        pieces = _RE_SENTINEL.split(para)  # [文本, 序号, 文本, 序号, …]
        for i, piece in enumerate(pieces):
            if i % 2 == 1:
                idx = int(piece)
                if 0 <= idx < len(links):
                    blk = links[idx]
                    if blk.url is not None:
                        blocks.append(blk)
                continue
            text = piece.strip()
            if text:
                blocks.append(DescBlock("text", text))
    return blocks


# ---------- 渲染 ----------

def text_segments(blocks: list[DescBlock]) -> list[str]:
    """text 块原文段按序取出——刀3 翻译 worker 的分段输入单源。"""
    return [b.text for b in blocks if b.kind == "text"]
def _linkify_html(escaped: str) -> str:
    """裸 http(s) 链接 → 可点 <a>（在已转义文本上跑）。经典坑：
    句尾标点会被正则粘进 URL——尾部中英句读剥回正文（剥到非标点
    为止；括号不剥，防误伤含括号的合法链接）。"""
    def repl(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?。，；：！？":
            trail = url[-1] + trail
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trail}'
    return _RE_BARE_URL.sub(repl, escaped)

def _html_text(s: str) -> str:
    """渲染边界统一出口：HTML 转义 → 裸链接转可点 → 换行转 <br>
    （Qt rich text 子集）。转义之后才跑 linkify：文本里已无裸
    <>"'，正则不会闯进任何标签；URL 里若带 &，此刻是 &amp; 形态，
    直接进 href 属性即合法 HTML（浏览器自动解回 &）。"""
    out = html.escape(s, quote=True)
    return _linkify_html(out).replace("\n", "<br>")

def blocks_to_html(blocks: list[DescBlock], *,
                   translations: list[str | None] | None = None,
                   compare: bool = False,
                   dim: str = "#8a8a8f") -> str:
    """块序列 → HTML（配 QTextBrowser.setHtml）。三态一函三吃：

    - translations=None 且 compare=False → 原文态；
    - translations 给定 → 译文态（text 块换成译文段；None 段 =
      「⚠ 该段翻译失败」附原文；段数不齐缺的按失败回退——宁保真
      不装懂）；
    - compare=True → 对照态：text 块 = 原文段 + 紧随的译文行（dim
      色），link 块原位穿插。

    link 块三种态都是同一行链接（<a href> 走 setOpenExternalLinks）。
    """
    out: list[str] = []
    ti = 0  # text 块序号 = 译文段下标
    for blk in blocks:
        if blk.kind == "link":
            out.append('<p><a href="'
                       + html.escape(blk.url or "", quote=True)
                       + '">' + _html_text(blk.text) + "</a></p>")
            continue
        seg = None
        if translations is not None:
            seg = translations[ti] if ti < len(translations) else None
            ti += 1
        orig = _html_text(blk.text)
        if not compare:
            if translations is None:
                out.append(f"<p>{orig}</p>")
            elif seg:
                out.append(f"<p>{_html_text(seg)}</p>")
            elif seg is None:
                out.append(f'<p>{orig}<br><span style="color:{dim}">'
                           "⚠ 该段翻译失败</span></p>")
            else:
                out.append(f'<p><span style="color:{dim}">（译文为空）'
                           "</span></p>")
        else:
            body = orig
            if translations is not None:
                if seg:
                    body += (f'<br><span style="color:{dim}">'
                             f"{_html_text(seg)}</span>")
                elif seg is None:
                    body += (f'<br><span style="color:{dim}">'
                             "⚠ 该段翻译失败</span>")
                else:
                    body += (f'<br><span style="color:{dim}">'
                             "（译文为空）</span>")
            out.append(f"<p>{body}</p>")
    return "\n".join(out)
