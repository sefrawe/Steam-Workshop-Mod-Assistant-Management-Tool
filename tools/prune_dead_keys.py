"""按 check_i18n_keys 的同一存活口径批删死键。自动备份先行；
只删「代码里 t 字面量与任何字符串常量都找不到」的键，
"__" 开头的元数据键不动。删错可从备份捞回。"""
import json
import shutil
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from check_i18n_keys import collect, EN_JSON

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

alive: set[str] = set()
for p in targets:
    t_keys, bare_all, _cjk = collect(p)
    alive |= t_keys | bare_all

dead = sorted(en_keys - alive)
bak = EN_JSON.parent / (EN_JSON.name + ".bak_prune")
shutil.copy2(EN_JSON, bak)
print(f"备份 → {bak.name}；死键 {len(dead)} 条。")
if dead:
    for k in dead:
        del en[k]
    EN_JSON.write_text(json.dumps(en, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
kept = len([k for k in en if not k.startswith("__")])
print(f"已删 {len(dead)} 条；en 表余 {kept} 条。")
