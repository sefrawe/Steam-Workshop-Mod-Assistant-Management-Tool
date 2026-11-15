"""审计 t() 的四类用法错误"""
"""
判定口径（2026-11-02 bbcodeParser NameError 实证同源）：
  活雷   = 同一函数里既绑定 t（赋值/for/with-as/except-as/形参）又调 t(...)
           ——本函数内调用 → TypeError；嵌套函数里调用 → NameError
  潜伏   = 绑定了 t 但全函数（含嵌套）从不调 t(...) —— 现在无害，加一句文案就炸
  导入期 = 模块级/类体里直接调 t(...)——import 时 init_language() 还没跑，
           _lang 恒 zh，词被烤死，英文界面照显中文（第 6 类，2026-11-02
           legacyMigrate/localScanner 实证）
v3：新增"导入期"分类（判法=全树 t() 调用−函数/lambda 子树内的）。
v2 事故教训照旧有效：路径锚定本文件，扫不到文件退出码 2。
退出码：有活雷或导入期 = 1；扫不到文件 = 2。用法：python tools/audit_t_shadow.py
"""
import ast
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]  # 项目根（本脚本住 tools/）
ROOTS = ("core", "gui", "workflows")
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def own_body(fn):
    """函数自身身体（不进嵌套函数/lambda——它们各有作用域）。"""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, FUNCS + (ast.Lambda,)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def has_t(node) -> bool:
    return any(isinstance(x, ast.Name) and x.id == "t"
               for x in ast.walk(node))


def _args_all(a: ast.arguments) -> list[str]:
    parts = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    if a.vararg:
        parts.append(a.vararg)
    if a.kwarg:
        parts.append(a.kwarg)
    return [x.arg for x in parts]


def binds_t(fn) -> list[int]:
    lines = []
    for n in own_body(fn):
        if isinstance(n, ast.Assign) and any(has_t(tg) for tg in n.targets):
            lines.append(n.lineno)
        elif isinstance(n, (ast.For, ast.AsyncFor)) and has_t(n.target):
            lines.append(n.lineno)
        elif isinstance(n, ast.NamedExpr) and has_t(n.target):
            lines.append(n.lineno)
        elif isinstance(n, ast.AugAssign) and has_t(n.target):
            lines.append(n.lineno)
        elif isinstance(n, ast.AnnAssign) and has_t(n.target):
            lines.append(n.lineno)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None \
                and has_t(n.optional_vars):
            lines.append(n.lineno)
        elif isinstance(n, ast.ExceptHandler) and n.name == "t":
            lines.append(n.lineno)
    return lines


def param_t(fn) -> bool:
    return "t" in _args_all(fn.args)


def calls_t(fn) -> list[int]:
    return [n.lineno for n in own_body(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "t"]


def _runtime_t_lines(tree) -> set[int]:
    """运行期 t() 调用行号 = 任何函数/lambda 子树内的调用。"""
    runtime: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, FUNCS + (ast.Lambda,)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) \
                        and isinstance(sub.func, ast.Name) \
                        and sub.func.id == "t":
                    runtime.add(sub.lineno)
    return runtime


def import_time_t(tree) -> list[int]:
    """导入期 t() 调用行号 = 全树 t() 调用 − 函数/lambda 体内的。
    （def 默认参数/装饰器严格说也在导入期求值，但 walk 会把它们算进
    函数子树——代码库内未见此形态，真遇到再补。）"""
    runtime = _runtime_t_lines(tree)
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "t" and n.lineno not in runtime]


def main() -> int:
    files: list[Path] = []
    for root in ROOTS:
        d = PROJ / root
        if not d.is_dir():
            print(f"!! 目录不存在：{d}")
            return 2
        files.extend(sorted(d.rglob("*.py")))
    if not files:
        print("!! 一个 .py 都没扫到——目录配置有问题")
        return 2

    live, latent, baked = [], [], []
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"!! 语法错误 {p}: {e}")
            continue
        # ---- 导入期：整文件级判定，与函数无关 ----
        for ln in import_time_t(tree):
            baked.append(f"{p}:{ln}")
        # ---- 遮蔽：函数级判定 ----
        for fn in (f for f in ast.walk(tree) if isinstance(f, FUNCS)):
            b, c = binds_t(fn), calls_t(fn)
            how = []
            if b:
                how.append(f"绑定行{b}")
            if param_t(fn):
                how.append("形参t")
            if not how:
                continue
            tag = "+".join(how)
            if c:
                live.append(f"{p} :: {fn.name}  {tag} 调用行{c}")
            else:
                nested = [(s.name, ln) for s in ast.walk(fn)
                          if isinstance(s, FUNCS) and s is not fn
                          for ln in calls_t(s)]
                if nested:
                    live.append(f"{p} :: {fn.name}  {tag} 嵌套调用{nested}")
                else:
                    latent.append(f"{p} :: {fn.name}  {tag}（从不调 t()）")

    print(f"（已扫描 {len(files)} 个文件：{ROOTS}）")
    print(f"—— 活雷 {len(live)} 处（运行到必炸，必须改）——")
    print("\n".join(live) if live else "（无）")
    print(f"\n—— 导入期 {len(baked)} 处（import 时词被烤死，英文界面照显中文）——")
    print("\n".join(baked) if baked else "（无）")
    print(f"\n—— 潜伏 {len(latent)} 处（现在无害，建议顺手改名）——")
    print("\n".join(latent) if latent else "（无）")
    return 1 if live or baked else 0


if __name__ == "__main__":
    sys.exit(main())
