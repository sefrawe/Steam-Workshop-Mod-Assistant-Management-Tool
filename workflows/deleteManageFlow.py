"""清理与删除管理 · 盘点与处置引擎
"""
r"""workflows/deleteManageFlow.py · 清理与删除功能模块的引擎。
只做两类事，其余都是别人的：
- 盘点（只读）：把"账与盘的差距"摆成清单——账有盘无（账本说已下载、
  盘上没了）、盘上可清（账内 mod 的 content 目录）、孤儿目录（盘上
  有、账本完全不认识）；
- 处置（逐条执行）：按调用方定好的动作清单逐条动手——账面处置
  （软删除/彻底清账）与磁盘处置（content 目录、备份文件、孤儿目录）。

不做的（分工红线）：
- 不发明检测：账实对账用 modVerifier（只做存在性对账——版本不在
  核验范围，账本版本只有确认门一个来源）。异常页负责发现，本模块
  负责处置；
- 孤儿认领路线不分派：账外目录统一走入账中心【扫描游戏目录】只读
  盘点——账外内容一律落待认领（盘面 acf 记了版本的带版本，没记的
  认成版本未知），不再区分"acf 认不认识"（旧工具扫描只吃 acf 合格
  条目才需要分派；盘点两条路都收）。本引擎只负责把孤儿列出来和删掉；
- 磁盘删除一律走 backupManager.safe_rmtree（R4 保险丝单源），另加
  联接树预检 _find_links：safe_rmtree 只查目标自身，这里把整棵树查
  一遍——反向拓扑（content 侧是联接、真实文件在游戏目录）一旦被
  rmtree 穿透，删掉的就是游戏侧真身；
- repo 层的 purge_mod 绝不碰文件系统；磁盘动作全部在本层。

执行模型（写给 GUI 工作线程）：
- build_plan() 先把用户勾选校验、定序成动作清单（纯数据+人话备注）；
- execute_action() 一次执行一条、返回一条结果——工作线程逐条调用、
  条与条之间检查停止请求（批间停止，绝不删一半；单条动作内部不
  中断）；
- 全局顺序"账先盘后"（删档案确认同款）：账面 → 备份文件 → 磁盘
  目录。中途停下，残留方向永远是"账已清、盘还在"（无害），绝不
  出现"登记还在、文件没了"的假失联。

steamcmd 在跑（R7）：盘点照常、删除照常——用户拍板"警告不拦截"：
被误删的 content 条目重跑下载命令即可回来（steamcmd 自会重建）。
界面在执行前把警告连同回程票一起说清楚，这就是全部的拦截。

调用约定：下载目录死路径重推导是调用方的事（先把 refresh_download_dir
核对好再进来）。steamcmd 路径只用于备份根的缺省推导（档案自设备份
目录时用不上它）；没配置 steamcmd 时下载目录多半也为空 → 盘点自然
落在 dead_root 短路，与"未配置"同一观感。
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from core import modVerifier, steamPaths
from core.backupManager import safe_rmtree, steamcmd_running
from core.models import Game
from core.modRepository import ModRepository


def _fmt_size(n: int) -> str:
    """字节数 → 人话（formatters 在 GUI 层，引擎自带一份最小的）。
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
            continue   # 读不了按"没有"处理：真删的时候同样会失败并报告
        for e in entries:
            if not e.is_dir():
                continue
            if os.path.islink(e) or os.path.isjunction(e):
                found.append(e)
            else:
                stack.append(e)
    return found
def _acf_remove(acf_path: Path, ids: list[int]) -> tuple[int, str]:
    """steamcmd 账本条目移除（★2026-10 按 core/steamPaths.py 实文核过
    签名）：底层 remove_items_from_acf 返回 (实际移除的编号, 账本里
    本就没有的编号) 两个清单——不是 (条数, 文本)，本包装换算成人话。
    Installed / Details 两区块由底层一起清（只清 Installed 拦得住
    入账、拦不住整体校验装配——两块都删才算断根）；写前自动备份
    （.bak_时间戳）与只留最近 1 份由底层负责；一个条目都没删时底层
    不动文件、不产生备份。文件级问题（读不了/解析失败/没有
    AppWorkshop 根）底层抛 ValueError——execute_action 的 except
    须已按本轮刀法加宽收编，逐条失败不炸整批。steamcmd 运行中的
    拒绝已在 build_plan 决定（不发动作）。"""
    removed, absent = steamPaths.remove_items_from_acf(str(acf_path), ids)
    if not removed:  # 底层口径：一个没删 → 文件没动、备份没产生
        shown = ("、".join(str(i) for i in absent[:8])
                 + ("…" if len(absent) > 8 else "")) if absent else ""
        return 0, ("这些编号本来就不在账本里，无事可移"
                   + (f"：{shown}" if shown else ""))
    note = f"移除 {len(removed)} 条，写前备份已自动留存（只留最近 1 份）"
    if absent:
        note += ("；账本里本就没有：" + "、".join(str(i) for i in absent[:8])
                 + ("…" if len(absent) > 8 else ""))
    return len(removed), note + "——装配引信已拆"

# ============ 盘点 ============

@dataclass
class LedgerRow:
    """账内 mod 的一行（盘点结果，界面表格直接喂）。"""
    mod_id: int
    title: str | None
    status: str            # tracked / downloaded / deleted / failed
    dir_exists: bool       # content 目录在盘上
    dir_size: int | None   # 实测字节（目录不在 = None）
    backup_count: int      # 备份登记份数
    backup_bytes: int      # 登记合计字节
    backup_files: int      # 盘上真实存在的备份目录数


@dataclass
class OrphanRow:
    """孤儿目录一行：盘上有纯数字目录、账本完全不认识（异常页同源）。
    认领路线统一 = 入账中心【扫描游戏目录】盘点落待认领（见文件头）。"""
    mod_id: int
    dir_size: int


@dataclass
class InventoryReport:
    game_id: int
    game_name: str
    download_dir: str
    backup_root: str | None   # 备份登记路径的解析根（档案自设值优先；
    #                           未自设时按 steamcmd 位置推导，没配置 = None）
    ledger_rows: list[LedgerRow] = field(default_factory=list)
    orphans: list[OrphanRow] = field(default_factory=list)
    dead_root: bool = False       # 下载目录不存在 → 盘上全部不可判
    steamcmd_running: bool = False   # R7：只警告不拦截（用户拍板）

    @property
    def missing_rows(self) -> list[LedgerRow]:
        """账有盘无：账本说已下载、盘上没了。"""
        return [r for r in self.ledger_rows
                if r.status == "downloaded" and not r.dir_exists]

    @property
    def cleanable_rows(self) -> list[LedgerRow]:
        """盘上可清：账内 mod 且目录在盘上（含软删除/失效的残留——
        "彻底清理"场景的主角）。tracked 不在此列：手动确认流管辖，
        不是异常。"""
        return [r for r in self.ledger_rows
                if r.status in ("downloaded", "deleted", "failed")
                and r.dir_exists]


def _measure_content_dir(download_dir: str | None) -> dict[int, int]:
    """content 目录下每个纯数字目录的实测字节。
    非数字名字进不了本表——那些归核验页"非数字内容"桶，本模块不抢。"""
    out: dict[int, int] = {}
    base = Path(download_dir or "")
    if not base.is_dir():
        return out
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.isascii() and entry.name.isdigit():
            out[int(entry.name)] = _dir_size(entry)
    return out


def inventory(repo: ModRepository, game: Game,
              steamcmd_path: str | None) -> InventoryReport:
    """只读盘点。只查不删不写——写库只发生在处置动作里。"""
    rep = InventoryReport(
        game_id=game.app_id, game_name=game.name,
        download_dir=game.download_dir or "", backup_root=None)
    mods = repo.list_mods(game.app_id)
    status_map = {m.mod_id: m.status for m in mods}

    # 账实对账（存在性，与核验页同源）：missing / 孤儿都从这来
    vr = modVerifier.verify(game.download_dir, status_map)
    if vr.dead_root:
        rep.dead_root = True
        return rep

    sizes = _measure_content_dir(game.download_dir)
    # 备份登记路径的解析根（与 backupManager._resolve_root 同一口径：
    # 档案自设值优先，缺省按 steamcmd 位置推导；没配置 = None）
    stored = str(game.backup_dir or "").strip()
    rep.backup_root = stored or steamPaths.backup_root_default(
        steamcmd_path, game.app_id)

    for m in mods:
        backs = repo.list_backups(m.mod_id)
        files = sum(1 for b in backs
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
            continue   # 账本认识的（如 tracked）不算孤儿
        rep.orphans.append(OrphanRow(
            mod_id=int(mid), dir_size=sizes.get(int(mid), 0)))
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
    # | "wipe_purge"（删文件+彻底清账）
    purge_backup_files: bool = False   # 彻底清账条目的磁盘备份文件是否带走
    orphan_ids: list[int] = field(default_factory=list)   # 勾选删除的孤儿
    orphan_defense: bool = False
    # 孤儿防复活复合开关（2026-10）：勾选删除的孤儿在删文件之外，
    # 同时 ① 列入已清账黑名单 ② 移除 steamcmd 账本条目（断根）；
    # 收尾清缓存由页面侧 T4 触发条件覆盖
    steamcmd_path: str | None = None
    # 页面传入：仅 defense 时定位 appworkshop acf（下载目录非标准
    # 布局时的兜底，盘点 _read_versions 同款顺序）


@dataclass
class Action:
    """一条处置动作（纯数据）。工作线程逐条执行，条间可停止。"""
    kind: str   # ledger_soft / ledger_purge / backup_file / content / orphan
    mod_id: int
    path: str = ""            # 磁盘类动作的目标路径
    expected_root: str = ""   # R4 保险丝参数：路径必须落在这个根内
    backup_id: int | None = None
    game_id: int = 0  # 拉黑类动作需要（add_purged 必传；其余动作不填）
    ids: list[int] = field(default_factory=list)  # 批次级动作（orphan_acf）
    # 的编号清单——一次动作搬整批账本条目，不逐条重写 acf


@dataclass
class ActionResult:
    kind: str
    mod_id: int
    ok: bool
    detail: str = ""   # 失败原因 / 附带说明（写日志、进报告）


@dataclass
class CleanupPlan:
    actions: list[Action]
    notes: list[str]   # 确认弹窗逐条展示：做什么 / 动多大 / 回程票


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
    # ---- 孤儿防复活复合动作（2026-10）----
    # 复活条目 = 盘上有目录 ∧ 账本无记录 ∧ 黑名单在案。对勾选的孤儿
    # 一次做三件事：拉黑 → 移除 steamcmd 账本条目（断根）→ 删文件。
    # 拉黑只登记不在名单的（已在 = 复活条目，跳过以保住原 note）；
    # 断根对全部勾选生效（引信不分新旧）。steamcmd 运行中：账本
    # 条目移除整体跳过（它退出时会覆盖账本，写了白写；清除残留
    # 同边界），拉黑与删文件照常（R7 警告不拦截口径）。
    black_actions: list[Action] = []
    acf_actions: list[Action] = []
    resurfaced_n = 0
    acf_note = ""
    if req.orphan_defense and req.orphan_ids:
        orphan_set = set(req.orphan_ids)
        already = repo.filter_purged(orphan_set)
        resurfaced_n = len(orphan_set & already)
        for mid in sorted(orphan_set - already):
            black_actions.append(Action(
                kind="orphan_blacklist", mod_id=mid,
                game_id=report.game_id))
        acf_path = _find_acf(report, req.steamcmd_path)
        if acf_path is None:
            acf_note = ("没找到 appworkshop 账本文件——这些目录多半"
                        "本就没有 steamcmd 账本记录，无事可移。")
        elif report.steamcmd_running:
            acf_note = ("steamcmd 正在运行：本次跳过账本条目移除"
                        "（它退出时会覆盖账本）——断根可事后到"
                        "【已清账管理·清除残留】补做。")
        else:
            acf_actions.append(Action(
                kind="orphan_acf", mod_id=0, path=str(acf_path),
                ids=sorted(orphan_set)))
    # ---- 定序：拉黑 → 账本条目移除 → 账面 → 备份文件 → 账内目录 →
    # 孤儿文件。决定与断根放最前、重活最后：任何一处停下，残留方向
    # 永远是"拦路闸已上、文件还在"（无害可续行）；反过来中途停下会
    # 留下"文件没了、引信还在"的复活窗口 ----
    actions: list[Action] = []
    actions.extend(black_actions)
    actions.extend(acf_actions)
    for mid in soft_ids:

        actions.append(Action(kind="ledger_soft", mod_id=mid))
    for mid in purge_ids:
        actions.append(Action(kind="ledger_purge", mod_id=mid))
    actions.extend(backup_file_actions)
    actions.extend(content_actions)
    actions.extend(orphan_actions)

    # ---- 人话备注（确认弹窗逐条展示：做什么/动多大/回程票/可逆性） ----
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
    if req.orphan_defense and req.orphan_ids:
        lines = [f"孤儿·防复活 {len(req.orphan_ids)} 个：删除文件之外——"]
        lines.append(f"· {len(black_actions)} 个列入已清账黑名单"
                     "（本工具从此不自动入账；想反悔：【已清账管理】"
                     "→ 允许录入）")
        if resurfaced_n:
            lines.append(f"· {resurfaced_n} 个已在名单（复活条目）："
                         "跳过重复登记，其余处置照常")
        if acf_actions:
            lines.append(f"· steamcmd 账本条目移除 "
                         f"{len(acf_actions[0].ids)} 个（写前自动备份）"
                         "——条目不除，steamcmd 会反复把它们装配回来")
        elif acf_note:
            lines.append(f"· 账本条目移除跳过：{acf_note}")
        lines.append("· 若这些编号有待认领提案，随拉黑一并撤销；"
                     "执行收尾自动清一次 steamcmd 缓存（复活燃料）")
        notes.append("\n".join(lines))

    if report.steamcmd_running and (content_actions or orphan_actions):
        notes.append("注意：steamcmd 正在运行——此刻删除可能与下载争用。"
                     "被误删的条目重跑下载命令即可回来（steamcmd 会自建条目）。")
    # 动作口径预告（2026-10 增补）：执行层把「连账面一起处置」的条目
    # 拆成账面+文件两条动作（wipe_soft / wipe_purge，"账先盘后"定序），
    # 勾了带走备份文件的每份各一条——动作总条数可能大于 mod 个数。
    # 确认弹窗先说清，执行汇总按同口径计数，"完成 2 条"不会被误读
    # 成 2 个 mod
    involved = ({mid for mid, _a in req.ledger_actions.items()}
                | set(req.clean_actions) | set(req.orphan_ids))
    if len(actions) > len(involved):
        notes.append(
            f"执行口径：本次共 {len(actions)} 条动作、涉及 "
            f"{len(involved)} 个 mod——「连账面一起处置」的条目各拆"
            "「账面 + 文件」两步，带走备份文件的每份各一条，孤儿防复活每条各拆「拉黑 + 账本 + 文件」（账本移除整批一条）；进度与"
            "完成数按动作条计，逐条日志写明是哪一步。")
    return CleanupPlan(actions=actions, notes=notes)
def _find_acf(report: InventoryReport, steamcmd_path: str | None) -> Path | None:
    """定位 appworkshop_<appid>.acf——单源走 steamPaths.locate_acf。
    先按下载目录上两级（= workshop 层，第三口径直接命中），再按
    steamcmd 根（第一口径）兜底。都找不到 = 这些孤儿多半本就没有
    steamcmd 账本记录，无事可移。"""
    dd = str(report.download_dir or "").strip()
    if dd:
        hit = steamPaths.locate_acf(Path(dd).parent, report.game_id)
        if hit is not None:
            return hit
    return steamPaths.locate_acf(steamcmd_path, report.game_id)





def execute_action(repo: ModRepository, action: Action) -> ActionResult:
    """执行一条动作。一切"操作层面不成立"都返回 ok=False 并说明，
    不抛异常——需要向用户解释的情况不是 bug（backupManager 同一口径）。"""
    try:
        if action.kind == "ledger_soft":
            mod = repo.get_mod(action.mod_id)
            if mod is None:
                return ActionResult(action.kind, action.mod_id, ok=False,
                                    detail="记录已不在（可能刚被别处处理）")
            # 删前快照：恢复时需要的东西。v2 的版本三件套一并入快照——
            # 恢复按 confirmed_version 有无回推 downloaded/tracked
            last_state = {"title": mod.title, "url": mod.url,
                          "time_updated": mod.time_updated,
                          "confirmed_version": mod.confirmed_version,
                          "confirmed_at": mod.confirmed_at,
                          "confirmed_source": mod.confirmed_source,
                          "local_size": mod.local_size,
                          "note": mod.note, "color_tag": mod.color_tag,
                          "is_special": mod.is_special}
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
        if action.kind == "orphan_blacklist":
            repo.add_purged(action.mod_id, action.game_id,
                            note="孤儿处置拉黑")
            dropped = repo.drop_stale_claims(action.game_id,
                                             [action.mod_id])
            extra = (f"；撤销未确认认领提案 {dropped} 条"
                     if dropped else "")
            return ActionResult(
                action.kind, action.mod_id, ok=True,
                detail="已列入已清账黑名单（本工具永不自动入账；"
                       "反悔走已清账管理·允许录入）" + extra)
        if action.kind == "orphan_acf":
            if not action.ids:
                return ActionResult(action.kind, 0, ok=True,
                                    detail="无编号可处理")
            _, note = _acf_remove(Path(action.path), action.ids)
            return ActionResult(action.kind, 0, ok=True, detail=note)

        return ActionResult(action.kind, action.mod_id, ok=False,
                            detail=f"未知动作类型：{action.kind}")
    except (RuntimeError, OSError, ValueError) as exc:
        # R4 保险丝拒绝 / 权限 / 文件被占用 / repo 与 acf 层的
        # 「操作不成立」（显式爆炸约定）——逐条报告，不炸整批
        return ActionResult(action.kind, action.mod_id, ok=False, detail=str(exc))

