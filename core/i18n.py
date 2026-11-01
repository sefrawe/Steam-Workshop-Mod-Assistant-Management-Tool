"""最低成本多语言框架（2026-11 加入）。不做 Qt Linguist / .ts / .qm；
不做热切换。语言在启动时从配置读，改设置需重启，简单可靠。

用法：
    from core.i18n import t
    label = QLabel(t("从浏览器取网址"), self)  # zh 时原样，en 时查表

翻译文件：gui/i18n_<代码>.json（如 i18n_en.json），扁平 JSON dict，
中文原文→译文。加一门新语言 = 往 gui/ 丢一个 i18n_xx.json，
设置页下拉自动多一项——无需改任何代码（机制性留位，不放空占位）。

语言设置存放：config/GlobalSettings.json 的 "language" 键
（程序内唯一配置入口，appSettings.DEFAULTS 有默认值 "zh"；
设置页「界面语言」下拉就地写入）。项目根 settings.json 是
本框架初版用过的旧入口，作为兼容兜底仍会被读一次。

翻译覆盖优先级：操作面（按钮/菜单/表头/报错/状态）> 说明块 > 日志 > 注释。
"""
import json
from pathlib import Path
from typing import Any

from core.appPaths import app_root

_lang: str = "zh"
_table: dict[str, str] = {}

# zh 是源语言（界面原文即中文），永远在场，不依赖任何翻译文件
_SOURCE_LANG = "zh"
_SOURCE_NAME = "中文（简体）"


def _gui_dir() -> Path:
    """翻译表所在目录：源码=项目根/gui；打包=随代码的数据目录。"""
    return Path(__file__).resolve().parent.parent / "gui"


def available_languages() -> list[tuple[str, str]]:
    """语言清单 [(代码, 显示名), ...]，zh 恒第一，其余按文件名排序。
    显示名取翻译文件里的 "__language_name__" 元键，缺省用大写代码。
    设置页下拉直接消费；扫 gui/ 而非硬编码——加语言零代码。"""
    out: list[tuple[str, str]] = [(_SOURCE_LANG, _SOURCE_NAME)]
    for p in sorted(_gui_dir().glob("i18n_*.json")):
        code = p.stem.removeprefix("i18n_")
        if not code or code == _SOURCE_LANG:
            continue
        name = code.upper()
        try:
            raw: Any = json.loads(p.read_text(encoding="utf-8"))
            v = raw.get("__language_name__") if isinstance(raw, dict) else None
            if isinstance(v, str) and v.strip():
                name = v.strip()
        except Exception:
            pass  # 名字读不出就用代码占位；文件本体由 _load_table 兜底
        out.append((code, name))
    return out


def init_language(lang: str | None = None) -> None:
    """应用启动时调一次：从配置读语言，加载对应翻译表。
    lang=None 时从配置文件读（GlobalSettings.json 优先，兼容项目根
    settings.json）；读不到默认 zh。"""
    global _lang, _table
    if lang is None:
        lang = _read_config_lang()
    _lang = lang or _SOURCE_LANG
    _table = _load_table(_lang)


def t(text: str) -> str:
    """翻译入口：zh 返回原文，其他语言查表（查不到也返回原文——
    缺翻译不炸 UI，只是混排）。"""
    if _lang == _SOURCE_LANG or not _table:
        return text
    return _table.get(text, text)


def current_lang() -> str:
    """当前语言代码：zh / en / ..."""
    return _lang


# ---- internal ----

def _read_config_lang() -> str:
    """读语言设置：config/GlobalSettings.json 的 "language" 键优先
    （appSettings 同一份文件，设置页写入处）；旧入口项目根
    settings.json 兼容兜底（设置页出现前教过用户手改它——
    GlobalSettings 里没有这个键时旧文件仍然生效）；都读不到→zh。"""
    for path in (app_root() / "config" / "GlobalSettings.json",
                 _gui_dir().parent / "settings.json"):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            v = cfg.get("language")
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            continue
    return _SOURCE_LANG


def _load_table(lang: str) -> dict[str, str]:
    """加载 gui/i18n_<lang>.json 翻译表。zh / 文件不存在 / 损坏
    →空表（t() 原样返回，不炸）。"""
    if lang == _SOURCE_LANG:
        return {}
    path = _gui_dir() / f"i18n_{lang}.json"
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        # 过掉 __language_name__ / __note__ / __段落__ 等元键：
        # __ 开头一律视为注释/元数据，不进查表
        return {k: v for k, v in raw.items()
                if isinstance(v, str) and not k.startswith("__")}
    except Exception:
        return {}
