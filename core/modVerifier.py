"""账实核验器
"""
"""
core/modVerifier.py · 两份只读对账：
verify()           数据库账本 ↔ content 磁盘实况（"文件真的还在吗"）
verify_junctions() 数据库账本 ↔ 游戏侧链接（"游戏能看到 mod 吗"）

与其他方向的分工（三份对账互补不重复）：
- 只读盘点（core/inventoryFlow）：盘面 → 待认领队列。它回答"盘上有
  哪些内容、盘面记的版本是多少（读 acf）"，把账外内容摆成认领候选
  等人背书入账；
- verify()（本模块）：我们的库 ↔ 磁盘目录。它回答"文件真的还在吗"。
  典型场景：用户在资源管理器里手滑删了某个 mod 的目录——账本照旧
  是 downloaded，盘点会把它当正常内容读过去，只有对盘核验能发现；
- verify_junctions()：已下载的每个 mod，游戏侧 mods 目录里是否正确
  建了指向 content 的链接。链接缺失/指错时，游戏里根本看不到这个
  mod，而以上两个对账全都测不出来。

版本不在本模块职责内：账本版本只有一个来源 = 确认门（人背书的
判决），"版本可不可信"由判决史与更新检测承载。本模块只做存在性
对账，绝不拿盘面 mtime 或 acf 反推版本（R19：mtime 不参与任何
版本判定）。

定位：只读不改。两个函数都不写库、不删目录、不动任何文件——产出
是"发现清单"，修复动作由用户在界面上选。刻意不做自动修复：把原因
不明的差异自动改成状态变更/删文件，违背 repo 错误显式哲学与 R4
的精神。

verify() 分桶口径（VerifyResult）：
- missing：账本 downloaded，盘上目录不存在 → 建议重下（修复动作
  全项目统一 = 重新下载）
- empty：目录存在但一个条目都没有 → 疑似中断残留，建议重下或删
  空目录。判断只做一层 scandir：目录里有任何子目录就算非空（mod
  目录里只有空子目录的情况罕见，不值得为它做递归统计）
- untracked_content：盘上有数字目录，但账本不是 downloaded——
  状态 None = 本工具之外下载的内容（认领候选由盘点兜住）；
  "tracked" = 已收录待下载、盘上却有内容 → 走入账中心盘点落待认领；
  "deleted"/"failed" = 软删除/失败保留文件，属正常
- non_numeric：content/<appid>/ 下名字不是纯数字的目录或散文件。
  正常这里只该有数字目录，非数字条目多是残留。本工具不代删（不
  认识的路径绝不动手，R4 精神），原样报名字
- dead_root：download_dir 本身不存在。逐条对账只会满屏误报 missing，
  短路返回；修复 = 按 steamcmd 现位置重推导（GUI 复用
  steamPaths.refresh_download_dir 写回）

verify_junctions() 分桶口径（JunctionResult）：
- 前提：档案配置了 game_mod_dir（游戏侧 mods 目录）。未配置（None
  或空串）返回 None，GUI 显示"未配置"——不是错误，有些游戏不玩
  链接这套
- link_missing：已下载但游戏侧没有对应条目 → 游戏里看不到此 mod。
  修复 = 重建链接（重下无效！重下只恢复 content 侧，游戏侧链接
  不会自己长出来）；本工具此轮只报不代建
- link_wrong_target：游戏侧有条目且是链接，但指向不是本档案的
  content 目录 → 指错地方，人工确认后删除重建
- link_real_dir：游戏侧是真实目录/普通文件，不是任何形式的链接
  → R4：只报不动（那是实体，删不删只有用户有资格决定）
- extra：游戏侧其余条目（非数字名的目录/文件、账本非 downloaded
  的数字名条目）→ 人工确认
- 刻意不设"悬空链接"桶：链接指向正确但 content 实体没了，和
  verify() 的 missing 是同一个问题——同一页报两遍只会混乱。本函数
  只管"链接对不对"，"实体在不在"归 verify()；重下完成后悬空链接
  自然复活
- 方向合法性：链接在游戏侧、实体在 content 侧，是 R4 允许的唯一
  方向（反向 = 实体在游戏侧、链接混进 content，绝对禁止）

Windows 细节（三个已知坑，比对前必须处理）：
- "链接"不只 junction 一种：符号链接对游戏完全等价，只认 junction
  会把符号链接误报成"实为目录"。islink 对 junction 返回 False、
  isjunction 对符号链接也返回 False，两者必须都查
- os.readlink 对 junction/symlink 可能返回带 \\\\?\\ 扩展前缀的
  路径，剥掉再比；UNC 的扩展形式顺带还原成普通共享路径
- 路径比对一律 casefold（Windows 路径大小写不敏感）

同步执行：全程只做目录列表、存在性检查和 readlink，不读文件内容，
百条级毫秒完成，不需要线程。
"""
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================
# 第一份对账：账本 ↔ content 磁盘实况
# ============================================================
@dataclass
class VerifyResult:
    """一次核验的产出。所有清单按编号/名称排序，展示顺序稳定。"""
    download_dir: Path
    dead_root: bool = False
    missing: list[int] = field(default_factory=list)
    empty: list[int] = field(default_factory=list)
    untracked_content: list[tuple[int, str | None]] = field(default_factory=list)
    non_numeric: list[str] = field(default_factory=list)
    healthy: int = 0


def _dir_is_empty(p: Path) -> bool:
    """目录里是否一个条目都没有（只看一层，理由见模块头）。"""
    with os.scandir(p) as it:
        return next(it, None) is None


def verify(
        download_dir: str | Path | None,
        status_by_id: Mapping[int, str],
) -> VerifyResult:
    """对账主入口。

    download_dir：档案的 steamcmd 工坊内容目录（…/workshop/content/<appid>）
    status_by_id：{mod_id: 状态}，调用方用 list_mods 取全后传字典
    （要含 deleted/failed——它们决定"盘上有目录"算不算异常）
    """
    if not download_dir:
        return VerifyResult(download_dir=Path(""), dead_root=True)
    root = Path(str(download_dir)).expanduser()
    if not root.is_dir():
        # 死根短路：不逐条报 missing（全是噪音），让 GUI 去走重推导流程
        return VerifyResult(download_dir=root, dead_root=True)

    numeric: dict[int, str] = {}   # 编号 → 目录名（保留原名，防前导零错位）
    non_numeric: list[str] = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            numeric[int(entry.name)] = entry.name
        else:
            # 非数字目录，以及 content 下不该出现的散文件——都进非数字桶
            non_numeric.append(entry.name)

    missing: list[int] = []
    empty: list[int] = []
    healthy = 0
    for mod_id, status in status_by_id.items():
        if status != "downloaded":
            continue
        name = numeric.get(mod_id)
        if name is None:
            missing.append(mod_id)
        elif _dir_is_empty(root / name):
            empty.append(mod_id)
        else:
            healthy += 1

    # 盘上有数字目录、账本却不是 downloaded：全部收进 untracked_content，
    # 状态原样携带（None = 不在库中），怎么解读交给 GUI
    untracked = [
        (mod_id, status_by_id.get(mod_id))
        for mod_id in numeric
        if status_by_id.get(mod_id) != "downloaded"
    ]
    return VerifyResult(
        download_dir=root,
        missing=sorted(missing),
        empty=sorted(empty),
        untracked_content=sorted(untracked),
        non_numeric=sorted(non_numeric),
        healthy=healthy,
    )


# ============================================================
# 第二份对账：账本 ↔ 游戏侧链接
# ============================================================
@dataclass
class JunctionResult:
    """一次 junction 巡检的产出。清单均排序，展示顺序稳定。"""
    game_mod_dir: Path
    dead_root: bool = False
    ok: int = 0  # 链接方向正确的已下载条目数
    link_missing: list[int] = field(default_factory=list)
    link_wrong_target: list[tuple[int, str]] = field(default_factory=list)
    link_real_dir: list[int] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


def _strip_extended_prefix(raw: str) -> str:
    """剥掉 Windows 扩展长度前缀：os.readlink 对 junction/symlink 常
    返回 \\\\?\\E:\\... 形式，不剥掉没法和普通 Path 比对。UNC 的扩展
    形式 \\\\?\\UNC\\server\\share 顺带还原成 \\\\server\\share。"""
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def _same_path(a: Path, b: Path) -> bool:
    """Windows 路径相等判断：解析 + casefold（建链接时写的大小写和
    库里的不一定一致，但指向同一个地方）。"""
    try:
        sa = a.resolve().as_posix()
    except OSError:
        sa = a.as_posix()
    try:
        sb = b.resolve().as_posix()
    except OSError:
        sb = b.as_posix()
    return sa.casefold() == sb.casefold()


def verify_junctions(
        download_dir: str | Path | None,
        game_mod_dir: str | Path | None,
        status_by_id: Mapping[int, str],
) -> JunctionResult | None:
    """junction 巡检主入口。

    game_mod_dir 未配置（None / 空串 / 纯引号空白）→ 返回 None：
    该游戏不巡检，调用方显示"未配置"即可，不是错误。
    """
    raw = str(game_mod_dir or "").strip().strip('"').strip()
    if not raw:
        return None
    if not download_dir:
        # 没有内容目录就没有比对基准。建档必填理论上到不了这里，防御一行
        return JunctionResult(game_mod_dir=Path(raw).expanduser(), dead_root=True)
    root = Path(raw).expanduser()
    content_root = Path(str(download_dir)).expanduser()

    # 单目录布局：游戏侧与下载目录指向同一处（斜杠方向、大小写差异
    # 不算不同）。合法布局——mod 实体就在游戏读取的目录里，不存在
    # 也不需要链接，逐条巡检只会把全部实体目录误报成"实为目录"。
    # 按未启用处理返回 None。放在存在性检查之前：目录真不存在时
    # 主对账（verify）已报死根，这里不必重复报
    if _same_path(root, content_root):
        return None
    if not root.is_dir():
        return JunctionResult(game_mod_dir=root, dead_root=True)

    # ---- 第一步：盘点游戏侧条目 ----
    present: dict[int, Path] = {}   # 数字名条目 → 路径（保留原名防前导零）
    non_numeric: list[str] = []
    for entry in root.iterdir():
        if entry.name.isdigit():
            present[int(entry.name)] = entry
        else:
            non_numeric.append(entry.name)
    downloaded = {mid for mid, st in status_by_id.items() if st == "downloaded"}

    # ---- 第二步：逐个"已下载"mod 检查游戏侧链接 ----
    ok = 0
    missing: list[int] = []
    wrong: list[tuple[int, str]] = []
    real_dir: list[int] = []
    for mid in sorted(downloaded):
        p = present.get(mid)
        if p is None:
            missing.append(mid)   # 游戏侧压根没有 → 游戏里看不到
            continue
        # junction 和符号链接都算"链接"：对游戏完全等价。
        # islink 对 junction 返回 False，isjunction 对 symlink 也返回
        # False——只查一个就会把另一种误报成"实为目录"
        if not (os.path.isjunction(p) or os.path.islink(p)):
            real_dir.append(mid)   # 真实目录/普通文件——R4 只报不动
            continue
        try:
            # readlink 拿到链接目标；剥扩展前缀后与期望位置比对
            target = Path(_strip_extended_prefix(os.readlink(p)))
        except OSError:
            wrong.append((mid, "（无法读取链接目标）"))
            continue
        if _same_path(target, content_root / str(mid)):
            # 链接方向正确。content 实体在不在归 verify() 的 missing 桶管，
            # 这里不重复报（见模块头"刻意不设悬空链接桶"）
            ok += 1
        else:
            wrong.append((mid, str(target)))

    # ---- 第三步：游戏侧对不上账的条目 ----
    # 非数字名的 + 数字名但不在"已下载"集合里的（tracked 却有链接 /
    # 账外链接 / 软删除留下的链接等），一律交人工确认
    extra = sorted(non_numeric
                   + [p.name for mid, p in present.items() if mid not in downloaded])

    return JunctionResult(
        game_mod_dir=root,
        ok=ok,
        link_missing=sorted(missing),
        link_wrong_target=sorted(wrong),
        link_real_dir=sorted(real_dir),
        extra=extra,
    )
