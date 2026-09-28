"""清理与删除管理 · 盘点与处置引擎
"""
r"""workflows/deleteManageFlow.py · 清理与删除功能模块的引擎（决策 69）。

只做两类事，其余都是别人的：
- 盘点（只读）：把"账与盘的差距"摆成清单——账有盘无（账本说已下载、
  盘上没了）、盘上可清（账内 mod 的 content 目录）、孤儿目录（盘上有、
  账本完全不认识）；
- 处置（逐条执行）：按调用方定好的动作清单逐条动手——账面处置
  （软删除/彻底清账）与磁盘处置（content 目录、备份文件、孤儿目录）。

不做的（分工红线）：
- 不发明检测：账实对账用 modVerifier（与异常页桶②③同源），acf 质量
  谓词用 localScanner（决策 23）——异常页负责发现，本模块负责处置；
- 磁盘删除一律走 backupManager.safe_rmtree（R4 保险丝单源），另加
  联接树预检 _find_links：safe_rmtree 只查目标自身，这里把整棵树查
  一遍——反向拓扑（content 侧是联接、真实文件在游戏目录）一旦被
  rmtree 穿透，删掉的就是游戏侧真身（决策 21④ 方向铁律）；
- repo 层的 purge_mod 绝不碰文件系统；磁盘动作全部在本层。

执行模型（写给 GUI 工作线程）：
- build_plan() 先把用户勾选校验、定序成动作清单（纯数据+人话备注）；
- execute_action() 一次执行一条、返回一条结果——工作线程逐条调用、
  条与条之间检查停止请求（决策 62 纪律：批间停止，绝不删一半；
  单条动作内部不中断）；
- 全局顺序"账先盘后"（gameDeleteDialog 同款）：账面 → 备份文件 →
  磁盘目录。中途停下，残留方向永远是"账已清、盘还在"（无害），
  绝不出现"登记还在、文件没了"的假失联。

steamcmd 在跑（R7）：盘点照常、删除照常——用户拍板"警告不拦截"：
被误删的 content 条目重跑下载命令即可回来（steamcmd 自会重建）。
界面在执行前把警告连同回程票一起说清楚，这就是全部的拦截。

调用约定：下载目录死路径重推导是调用方的事（与扫描本地同款：
先把 refresh_download_dir 核对好再进来）；acf 结构损坏不炸盘点，
按"acf 不认识"保守分派认领路线（宁可指保守路，不给半截账）。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from core import localScanner, modVerifier, steamPaths
from core.backupManager import safe_rmtree, steamcmd_running
from core.models import Game
from core.modRepository import ModRepository


def _fmt_size(n: int) -> str:
    """字节数 → 人话（gui.formatters 在 GUI 层，引擎自带一份最小的）。
    只用于备注文案，不参与任何判定。"""
    if n >= 2 ** 30:
        return f"{n / 2 ** 30:.2f} GiB"
    if n >= 2 ** 20:
        return f"{n / 2 ** 20:.1f} MiB"
    if n >= 2 ** 10:
        return f"{n / 2 ** 10:.1f} KiB"
    return f"{n} B"


def _dir_size(path: Path) -> int:
    """目录实测字节（量不到的文件按 0 计，不炸整体）。
    os.walk 默认不穿透链接/junction——量尺寸本身也是安全的。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _find_links(target: Path) -> list[Path]:
    """目录树里的全部链接/junction（含根自身）。不穿透任何链接。
    R4 的加强版：发现任何一个就整体拒绝删除——树里有链接说明拓扑
    不是"纯内容目录"，先去「连接」指引处理拓扑再回来清理。"""
    if os.path.islink(target) or os.path.isjunction(target):
        return [target]
    found: list[Path] = []
    stack = [target]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue  # 读不了按"没有"处理：真删的时候同样会失败并报告
        for e in entries:
            if not e.is_dir():
                continue
            if os.path.islink(e) or os.path.isjunction(e):
                found.append(e)
            else:
                stack.append(e)
    return found


# ============ 盘点 ============

@dataclass
class LedgerRow:
    """账内 mod 的一行（盘点结果，界面表格直接喂）。"""
    mod_id: int
    title: str | None
    status: str                  # tracked / downloaded / deleted / failed
    dir_exists: bool             # content 目录在盘上
    dir_size: int | None         # 实测字节（目录不在 = None）
    backup_count: int            # 备份登记份数
    backup_bytes: int            # 登记合计字节
    backup_files: int            # 盘上真实存在的备份目录数


@dataclass
class OrphanRow:
    """孤儿目录一行：盘上有纯数字目录、账本完全不认识（异常页桶③同源）。"""
    mod_id: int
    dir_size: int
    in_acf: bool                 # steamcmd 的 acf 还认不认识它（认领路线分派）


@dataclass
class InventoryReport:
    game_id: int
    game_name: str
    download_dir: str
    backup_root: str | None      # 备份登记路径的解析根（档案值优先，缺省推导）
    ledger_rows: list[LedgerRow] = field(default_factory=list)
    orphans: list[OrphanRow] = field(default_factory=list)
    steamcmd_missing: bool = False   # 未配置 steamcmd → 盘上全部不可判
    dead_root: bool = False          # 下载目录不存在 → 盘上全部不可判
    acf_missing: bool = False        # acf 不在 → 孤儿全部按"不认识"分派
    steamcmd_running: bool = False   # R7：只警告不拦截（用户拍板）

    @property
    def missing_rows(self) -> list[LedgerRow]:
        """账有盘无：账本说已下载、盘上没了（异常页桶②同口径）。"""
        return [r for r in self.ledger_rows
                if r.status == "downloaded" and not r.dir_exists]

    @property
    def cleanable_rows(self) -> list[LedgerRow]:
        """盘上可清：账内 mod 且目录在盘上（含软删除/失效的残留——
        "彻底清理"场景的主角）。tracked 不在此列：那是手动确认流
        （决策 24）管辖，不是异常。"""
        return [r for r in self.ledger_rows
                if r.status in ("downloaded", "deleted", "failed")
                and r.dir_exists]


def _acf_known_ids(root: Path, app_id: int) -> tuple[set[int], bool]:
    """steamcmd 的 acf 还认识哪些编号（含"疑似下载中断"——acf 记着它，
    只是没下完，同样算认识）。返回 (认识集合, acf 是否缺失)。
    acf 缺失/结构损坏 → (空集, True)：孤儿全部落到"手动确认"认领路线，
    界面说明原因——不给半截账，也绝不炸盘点。"""
    acf = localScanner.locate_acf(root, app_id)
    if acf is None:
        return set(), True
    try:
        scan = localScanner.scan_acf(acf)
    except ValueError:
        return set(), True
    known: set[int] = set()
    for raw in getattr(scan, "interrupted", None) or []:
        try:
            known.add(int(str(raw)))
        except (TypeError, ValueError):
            continue
    for it in getattr(scan, "items", None) or []:
        # items 的字段名以 localScanner 实现为准（dict 或 dataclass 都容）：
        # 万一都没取到，孤儿只是全部走保守认领路线，不出错
        for attr in ("mod_id", "publishedfileid", "id"):
            v = it.get(attr) if isinstance(it, dict) else getattr(it, attr, None)
            if v is not None:
                try:
                    known.add(int(str(v)))
                except (TypeError, ValueError):
                    pass
                break
    return known, False


def _measure_content_dir(download_dir: str | None) -> dict[int, int]:
    """content 目录下每个纯数字目录的实测字节。
    非数字名字进不了本表——那些归核验页"非数字内容"桶，本模块不抢。"""
    out: dict[int, int] = {}
    base = Path(download_dir or "")
    if not base.is_dir():
        return out
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.isdigit():
            out[int(entry.name)] = _dir_size(entry)
    return out


def inventory(repo: ModRepository, game: Game,
              steamcmd_path: str | None) -> InventoryReport:
    """只读盘点。只查不删不写——写库只发生在处置动作里。"""
    rep = InventoryReport(
        game_id=game.app_id, game_name=game.name,
        download_dir=game.download_dir or "", backup_root=None)
    root = steamPaths.steamcmd_root(steamcmd_path)
    if root is None:
        # 与 exceptionFlow.detect_local 同一口径：没钥匙不硬猜，
        # 立旗标让界面说清"为什么没查成"（绝不静默当 0）
        rep.steamcmd_missing = True
        return rep

    mods = repo.list_mods(game.app_id)
    status_map = {m.mod_id: m.status for m in mods}

    # 账实对账（与异常页同源）：missing / 孤儿 / 非数字内容都从这来
    vr = modVerifier.verify(game.download_dir, status_map)
    if vr.dead_root:
        rep.dead_root = True
        return rep

    sizes = _measure_content_dir(game.download_dir)
    acf_known, acf_missing = _acf_known_ids(root, game.app_id)
    rep.acf_missing = acf_missing

    # 备份登记路径的解析根（与 backupManager._resolve_root 同一口径：
    # 档案值优先，缺省按 steamcmd 位置推导）
    stored = str(game.backup_dir or "").strip()
    rep.backup_root = stored or steamPaths.backup_root_default(
        steamcmd_path, game.app_id)

    for m in mods:
        backs = repo.list_backups(m.mod_id)
        files = sum(
            1 for b in backs
            if rep.backup_root
            and (Path(rep.backup_root) / b.backup_path).is_dir())
        size = sizes.get(m.mod_id)
        rep.ledger_rows.append(LedgerRow(
            mod_id=m.mod_id, title=m.title, status=m.status,
            dir_exists=size is not None, dir_size=size,
            backup_count=len(backs),
            backup_bytes=sum(b.size_bytes for b in backs),
            backup_files=files))

    for mid, st in vr.untracked_content:
        if st is not None:
            continue  # 账本认识的（如 tracked）不算孤儿——异常页同口径
        rep.orphans.append(OrphanRow(
            mod_id=int(mid), dir_size=sizes.get(int(mid), 0),
            in_acf=int(mid) in acf_known))
    rep.orphans.sort(key=lambda o: o.mod_id)
    rep.steamcmd_running = steamcmd_running()
    return rep


# ============ 处置计划 ============

@dataclass
class PlanRequest:
    """用户的勾选（页面收集后整体递交）。字典 = mod_id → 动作。"""
    ledger_actions: dict[int, str] = field(default_factory=dict)
    # 账有盘无行："soft"（软删除）| "purge"（彻底清账）
    clean_actions: dict[int, str] = field(default_factory=dict)
    # 盘上可清行："wipe"（仅删文件）| "wipe_soft"（删文件+软删除）
    #            | "wipe_purge"（删文件+彻底清账）
    purge_backup_files: bool = False   # 彻底清账条目的磁盘备份文件是否带走
    orphan_ids: list[int] = field(default_factory=list)  # 勾选删除的孤儿


@dataclass
class Action:
    """一条处置动作（纯数据）。工作线程逐条执行，条间可停止。"""
    kind: str            # ledger_soft / ledger_purge / backup_file / content / orphan
    mod_id: int
    path: str = ""       # 磁盘类动作的目标路径
    expected_root: str = ""  # R4 保险丝参数：路径必须落在这个根内
    backup_id: int | None = None


@dataclass
class ActionResult:
    kind: str
    mod_id: int
    ok: bool
    detail: str = ""     # 失败原因 / 附带说明（写日志、进报告）


@dataclass
class CleanupPlan:
    actions: list[Action]
    notes: list[str]     # 确认弹窗逐条展示：做什么 / 动多大 / 回程票


def build_plan(repo: ModRepository, report: InventoryReport,
               req: PlanRequest) -> CleanupPlan:
    """把用户勾选校验、定序成动作清单。校验不过 → ValueError
    （消息可直接给用户看）。纯逻辑，可单测。"""
    missing_ids = {r.mod_id for r in report.missing_rows}
    clean_by_id = {r.mod_id: r for r in report.cleanable_rows}
    orphan_ids = {o.mod_id for o in report.orphans}

    # ---- 校验：每个动作都必须落在盘点给过的行上（不许凭空发明） ----
    for mid, act in req.ledger_actions.items():
        if mid not in missing_ids:
            raise ValueError(f"mod {mid} 不在「账有盘无」清单里，不能处置")
        if act not in ("soft", "purge"):
            raise ValueError(f"未知的账面动作：{act}")
    for mid, act in req.clean_actions.items():
        row = clean_by_id.get(mid)
        if row is None:
            raise ValueError(f"mod {mid} 不在「盘上可清」清单里，不能处置")
        if act == "wipe_soft" and row.status != "downloaded":
            raise ValueError(
                f"mod {mid} 状态为{row.status}，没有「软删除」可做"
                "（已经是删除/失效状态）")
        if act == "wipe_purge" and row.status == "failed":
            raise ValueError(
                f"mod {mid} 是失效条目：处置链归异常页，本页只清文件")
        if act not in ("wipe", "wipe_soft", "wipe_purge"):
            raise ValueError(f"未知的清理动作：{act}")
    for mid in req.orphan_ids:
        if mid not in orphan_ids:
            raise ValueError(f"{mid} 不在孤儿清单里（可能已被处置或认领）")

    # ---- 收集"彻底清账"对象（账有盘无的 purge + 盘上可清的 wipe_purge） ----
    purge_ids = sorted(
        [mid for mid, a in req.ledger_actions.items() if a == "purge"]
        + [mid for mid, a in req.clean_actions.items() if a == "wipe_purge"])
    soft_ids = sorted(
        [mid for mid, a in req.ledger_actions.items() if a == "soft"]
        + [mid for mid, a in req.clean_actions.items() if a == "wipe_soft"])

    # ---- 备份文件动作：路径必须在账还在的时候取好（purge 之后记录就没了） ----
    backup_file_actions: list[Action] = []
    purge_backup_records = 0
    purge_backup_bytes = 0
    for mid in purge_ids:
        for b in repo.list_backups(mid):
            purge_backup_records += 1
            purge_backup_bytes += b.size_bytes
            if req.purge_backup_files and report.backup_root:
                backup_file_actions.append(Action(
                    kind="backup_file", mod_id=mid,
                    path=str(Path(report.backup_root) / b.backup_path),
                    expected_root=report.backup_root, backup_id=b.id))

    # ---- 磁盘目录动作（content / 孤儿） ----
    content_actions: list[Action] = []
    content_bytes = 0
    for mid, act in req.clean_actions.items():
        content_actions.append(Action(
            kind="content", mod_id=mid,
            path=str(Path(report.download_dir) / str(mid)),
            expected_root=report.download_dir))
        content_bytes += clean_by_id[mid].dir_size or 0
    orphan_actions = [
        Action(kind="orphan", mod_id=mid,
               path=str(Path(report.download_dir) / str(mid)),
               expected_root=report.download_dir)
        for mid in sorted(req.orphan_ids)]
    orphan_bytes = sum(o.dir_size for o in report.orphans
                       if o.mod_id in set(req.orphan_ids))

    # ---- 定序（账先盘后）：账面 → 备份文件 → 账内目录 → 孤儿 ----
    actions: list[Action] = []
    for mid in soft_ids:
        actions.append(Action(kind="ledger_soft", mod_id=mid))
    for mid in purge_ids:
        actions.append(Action(kind="ledger_purge", mod_id=mid))
    actions.extend(backup_file_actions)
    actions.extend(content_actions)
    actions.extend(orphan_actions)

    # ---- 人话备注（确认弹窗逐条展示，总纲②⑤：三问+可逆性） ----
    notes: list[str] = []
    if soft_ids:
        notes.append(f"账面·软删除 {len(soft_ids)} 个：记录与快照保留，"
                     "随时可恢复（回程票：mod 库页右键即可恢复状态）。")
    if purge_ids:
        notes.append(
            f"账面·彻底清账 {len(purge_ids)} 个：记录物理删除，不可恢复；"
            f"随账清除备份登记 {purge_backup_records} 份"
            f"（约 {_fmt_size(purge_backup_bytes)}）。")
        if not req.purge_backup_files:
            notes.append("备份磁盘文件保留在原地，成为未登记的普通文件夹"
                         "（可日后手动删；本工具不再跟踪它们）。")
    if backup_file_actions:
        notes.append(f"备份文件：删除 {len(backup_file_actions)} 份"
                     f"（约 {_fmt_size(purge_backup_bytes)}）——不可恢复。")
    if content_actions:
        notes.append(f"content 目录：删除 {len(content_actions)} 个"
                     f"（约 {_fmt_size(content_bytes)}）——"
                     "可重得：重新下载即可回来。")
    if orphan_actions:
        notes.append(
            f"孤儿目录：删除 {len(orphan_actions)} 个"
            f"（约 {_fmt_size(orphan_bytes)}）——账本无记录，"
            "删了没有账面可回，这是删前的最后一道确认。")
    if report.steamcmd_running and (content_actions or orphan_actions):
        notes.append("注意：steamcmd 正在运行——此刻删除可能与下载争用。"
                     "被误删的条目重跑下载命令即可回来（steamcmd 会自建条目）。")
    return CleanupPlan(actions=actions, notes=notes)


def execute_action(repo: ModRepository, action: Action) -> ActionResult:
    """执行一条动作。一切"操作层面不成立"都返回 ok=False 并说明，
    不抛异常——需要向用户解释的情况不是 bug（backupManager 同一口径）。"""
    try:
        if action.kind == "ledger_soft":
            mod = repo.get_mod(action.mod_id)
            if mod is None:
                return ActionResult(action.kind, action.mod_id, ok=False,
                                    detail="记录已不在（可能刚被别处处理）")
            last_state = {"title": mod.title, "url": mod.url,
                          "time_updated": mod.time_updated,
                          "local_timeupdated": mod.local_timeupdated,
                          "manifest": mod.manifest,
                          "local_size": mod.local_size, "note": mod.note,
                          "color_tag": mod.color_tag,
                          "is_special": mod.is_special,
                          "local_path": mod.local_path}
            repo.mark_deleted(action.mod_id, last_state)
            return ActionResult(action.kind, action.mod_id, ok=True,
                                detail="账面已标记已删除（记录与快照保留，可恢复）")

        if action.kind == "ledger_purge":
            n = repo.purge_mod(action.mod_id, purge_backups=True)
            return ActionResult(action.kind, action.mod_id, ok=True,
                                detail=f"记录已彻底清除（随账清除备份登记 {n} 份）")

        if action.kind == "backup_file":
            target = Path(action.path)
            if not target.is_dir():
                return ActionResult(action.kind, action.mod_id, ok=True,
                                    detail="盘上本已不存在（仅剩的记录已随账清除）")
            safe_rmtree(target, Path(action.expected_root))
            return ActionResult(action.kind, action.mod_id, ok=True,
                                detail="备份文件已删除")

        if action.kind in ("content", "orphan"):
            target = Path(action.path)
            links = _find_links(target)
            if links:
                return ActionResult(
                    action.kind, action.mod_id, ok=False,
                    detail="目录树中发现链接/junction，拒绝删除："
                           + "、".join(str(p) for p in links[:3])
                           + "——请先到「连接」指引处理拓扑，再回来清理")
            if not target.is_dir():
                return ActionResult(action.kind, action.mod_id, ok=True,
                                    detail="盘上本已不存在")
            safe_rmtree(target, Path(action.expected_root))
            tip = ("已删除；想回来重新下载即可（命令生成页勾选该编号）"
                   if action.kind == "content"
                   else "已删除（账本无记录，不可恢复）")
            return ActionResult(action.kind, action.mod_id, ok=True, detail=tip)

        return ActionResult(action.kind, action.mod_id, ok=False,
                            detail=f"未知动作类型：{action.kind}")
    except (RuntimeError, OSError) as exc:
        # R4 保险丝拒绝 / 权限 / 文件被占用等——逐条报告，不炸整批
        return ActionResult(action.kind, action.mod_id, ok=False,
                            detail=str(exc))
