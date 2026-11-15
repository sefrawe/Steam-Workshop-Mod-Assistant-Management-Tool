"""i18n 键对账 v2：t('…') 字面量键 + 「存原词」裸字符串，与英文表对账。

v1 → v2 修正：v1 只认 t('字面量')，把「存原词、用时翻」的词表常量
（_GROUP_ORDER / _STATE_LOOKS / _VERSION_PRESETS / _PAGE_TITLES /
_SECTIONS / _KIND_ZH / _CLEAN_SORTS / _SOURCE_* / 各表头与引导段 …）
误判成死键——它们是裸字符串、消费点 t(变量)，静态扫不出 t。
v2 存活判定 = t 字面量 ∪ 代码里任何字符串常量：宁漏删不误删
（删错一条 = 英文界面该处回退中文）。

三段输出：
    1) 汇总
    2) 缺译（按文件分组）= t 字面量 ∪ 裸中文串（docstring 与
       f-string 静态片段除外）− en 表。裸中文进缺译 = 有显示文案
       没有英文条目——引擎层裸中文与漏登存词在这里现形
    3) 死键 = en 表有、代码里（t 字面量或任何字符串常量）都找不到
       ——真·尸体，可删
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN_JSON = ROOT / "gui" / "i18n_en.json"


def _is_cjk(s: str) -> bool:
    """含汉字 / CJK 标点 / 全角字符 → 视为中文显示文案。"""
    for ch in s:
        if ("\u4e00" <= ch <= "\u9fff"       # 汉字
                or "\u3000" <= ch <= "\u303f"    # 、「」【】等
                or "\uff00" <= ch <= "\uffef"):  # ！？（）：等全角
            return True
    return False
def _has_han(s: str) -> bool:
    """含至少一个汉字 → 中文显示文案候选（纯标点/骨架不算）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def _is_keyword_table(s: str) -> bool:
    """关键词表指纹：逗号/分号切出 ≥8 段且中英混排 → 匹配用数据，
    不是界面文案（设置页两份默认词表、exceptionFlow 词表）。
    注意：整段含词表的设置 hint 不受影响——它们已在 en 表。"""
    parts = [p.strip() for p in
             s.replace("，", ",").replace("；", ";").replace(";", ",").split(",")]
    parts = [p for p in parts if p]
    han = sum(1 for p in parts if _has_han(p))
    ascii_ = sum(1 for p in parts if p.isascii())
    return len(parts) >= 8 and han >= 3 and ascii_ >= 3


def collect(path: Path) -> tuple[set[str], set[str], set[str]]:
    """单文件 → (t 字面量键, 全部字符串常量, 裸中文显示文案)。

    裸中文排除三类（都不是待翻译的显示文案）：
    - no-op 字符串语句（ast.Expr 且值是字符串常量）——docstring、
      dsh 双 docstring 的第二份、一切注释型字符串；单独成句的
      字符串不产生任何显示，必是给人读的（v2 只豁免第一条的教训）；
    - f-string 静态片段（拼接骨架，翻译走外层 t()）；
    - 无汉字串与关键词表数据（_is_keyword_table）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        print(f"[skip] {path}: 语法解析失败（{exc}）")
        return set(), set(), set()

    skip_ids: set[int] = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)):
            skip_ids.add(id(n.value))
        if isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant):
                    skip_ids.add(id(v))

    t_keys: set[str] = set()
    bare_all: set[str] = set()
    bare_cjk: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant)
                and isinstance(node.value, str)):
            continue
        s = node.value
        bare_all.add(s)                      # 存活判定：来者不拒（保守）
        if (id(node) not in skip_ids and _has_han(s)
                and not _is_keyword_table(s)):
            bare_cjk.add(s)                  # 缺译判定：只留显示文案
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            t_keys.add(node.args[0].value)
    return t_keys, bare_all, bare_cjk


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    en_keys = {k for k in en if not k.startswith("__")}

    targets: list[Path] = []
    for d in ("gui", "core", "workflows"):
        targets.extend(sorted((ROOT / d).rglob("*.py")))
    mp = ROOT / "main.py"
    if mp.exists():
        targets.append(mp)

    data: dict[str, tuple[set[str], set[str], set[str]]] = {}
    for p in targets:
        data[str(p.relative_to(ROOT))] = collect(p)

    t_used = set().union(*(d[0] for d in data.values())) if data else set()
    alive = set().union(*(d[0] | d[1] for d in data.values())) if data else set()
    cjk_bare = set().union(*(d[2] for d in data.values())) if data else set()

    missing = (t_used | cjk_bare) - en_keys
    dead = en_keys - alive

    print(f"扫描 {len(targets)} 个文件；t 字面量键 {len(t_used)}；"
          f"裸中文串 {len(cjk_bare)}；en 表 {len(en_keys)} 条。")
    print(f"\n== 缺译 {len(missing)} 条（t 字面量 + 裸中文；英文界面回退中文） ==")
    for f in sorted(data):
        t_keys, _bare_all, cjk = data[f]
        ks = sorted((t_keys | cjk) & missing)
        if ks:
            print(f"-- {f}（{len(ks)}）")
            for k in ks:
                print(f"   {k}")
    print(f"\n== 死键 {len(dead)} 条（代码里 t 字面量与任何字符串常量都找不到） ==")
    for k in sorted(dead):
        print(f"   {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
