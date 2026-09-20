"""本地扫描器"""
"""
core/localScanner.py · 解析 appworkshop_<appid>.acf，把本地版本三件套灌进数据库。

事实源分工（本模块一切行为的出发点）：
- WorkshopItemsInstalled 是本地事实源：size / timeupdated / manifest 三件套
  原样采信，全量回填、幂等重写，刻意不与库中旧值做差异比较
- acf 顶层字段（SizeOnDisk / TimeLastUpdated / NeedsUpdate ...）和
  WorkshopItemDetails 区块（里面那对 latest_* 是 steamcmd 上次联网时的
  缓存）都不是远端事实源——一律忽略；远端唯一事实源是 Steam Web API
- manifest 保持字符串：无符号 64 位逼近 SQLite INTEGER 上限（schema 约定）

管线（前三个纯函数可直接单测，apply 是唯一写库点）：
  locate_acf(库路径, app_id)  → 定位 acf；找不到返回 None（不是错误，
                               可能路径没配或游戏没装，由调用方提示）
  scan_acf(path)             → 解析出条目清单 + 跳过清单
  diff_plan(items, 库中状态)  → 生成回填 / 新入库计划，不碰任何 IO
  apply(repo, plan)          → 整个计划包进一个事务，要么全成要么全败

状态规则：
- 对库中已有 id：三件套全量回填；状态只允许 tracked → downloaded 这一个
  跃迁方向；deleted / failed 只补本地证据，状态不动
- 库中没有的 id（Steam 客户端自己下载的）：acf 有记录 = 文件在盘上，
  直接以 downloaded 入库，url 现拼（不联网），标题等远端字段留 NULL，
  由下一轮更新检测的 API 查询顺带补全

错误边界：
- 文件级（读不了 / 不是 VDF / 没有 AppWorkshop 根）→ ValueError 显式爆炸，
  页面层 catch 后提示；语义统一，GUI 只需接一种异常
- 条目级（缺 timeupdated / 编号不是数字 / 不是键值块）→ 跳过并记录原因，
  绝不因一个坏条目炸掉其余上百条事实

对账假设：publishedfileid 在 Steam 全工坊全局唯一，一个条目只会出现在
一个游戏的 acf 里，所以 diff_plan 只按当前游戏对表；万一真出现跨游戏
撞主键，IntegrityError 会显式爆炸并整体回滚，不会写脏数据。

依赖 vdf 库（Valve KV 格式解析）；同步执行，百条级毫秒完成，不需要线程。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import vdf

from core.models import Mod
from core.modRepository import ModRepository

_WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


@dataclass
class LocalItem:
    """acf 中一个已安装条目的本地三件套。manifest 保持 str。"""
    mod_id: int
    timeupdated: int
    size: int | None
    manifest: str | None


@dataclass
class ScanResult:
    """一次扫描的产出。skipped 记录被跳过的条目及原因（原始键, 原因）。"""
    acf_path: Path
    items: list[LocalItem]
    skipped: list[tuple[str, str]]


@dataclass
class PlannedUpdate:
    """库中已有 id 的回填动作。to_downloaded 仅在原状态为 tracked 时 True。"""
    item: LocalItem
    to_downloaded: bool


@dataclass
class PlannedInsert:
    """库中没有 id 的补录动作。入库即 downloaded（acf 有记录 = 文件在盘上）。"""
    item: LocalItem


@dataclass
class ScanPlan:
    """diff_plan 的产出，apply 的输入。字段全是纯数据，可序列化可检查。"""
    game_id: int
    updates: list[PlannedUpdate]
    inserts: list[PlannedInsert]


@dataclass
class ScanApplyReport:
    """落库计数。只描述"计划执行了什么"，不代表"数据有没有变化"。"""
    updated: int       # 回填条数（含跃迁）
    transitioned: int  # 其中 tracked → downloaded
    inserted: int      # 新入库条数


def locate_acf(steam_library_path: str | Path | None, app_id: int) -> Path | None:
    """按两种填写口径定位 acf：库根（…\\SteamLibrary）或 steamapps 层都认。
    找不到返回 None——不是错误，由调用方决定怎么提示。"""
    raw = str(steam_library_path or "").strip().strip('"').strip()
    if not raw:
        return None
    base = Path(raw).expanduser()
    name = f"appworkshop_{app_id}.acf"
    for candidate in (base / "steamapps" / name, base / name):
        if candidate.is_file():
            return candidate
    return None


def _to_int(value) -> int | None:
    """acf 的数字全是字符串；转不动/缺失返回 None（与 steamApiClient 同策略）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scan_acf(path: str | Path) -> ScanResult:
    """解析 acf → 条目清单。文件级问题抛 ValueError；条目级问题跳过记原因。"""
    p = Path(path)
    try:
        # utf-8-sig：顺手吃掉 Windows 文件可能带的 BOM，否则 vdf 第一个键就解析坏
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"acf 读取失败：{p}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"acf 不是 UTF-8 文本：{p}") from exc
    try:
        data = vdf.loads(text)
    except Exception as exc:
        # vdf 对半截文件抛自己的异常类型；统一转写为 ValueError 并保留
        # 原始异常链——是转写不是吞掉，页面层只需接一种异常
        raise ValueError(f"acf 解析失败（文件损坏或不是 VDF 格式）：{p}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("AppWorkshop"), dict):
        raise ValueError(f"该文件没有 AppWorkshop 根区块（可能选错了文件）：{p}")

    installed = data["AppWorkshop"].get("WorkshopItemsInstalled")
    if not isinstance(installed, dict):
        installed = {}  # 合法空态：游戏装了但一个工坊条目都没有

    items: list[LocalItem] = []
    skipped: list[tuple[str, str]] = []
    for raw_id, block in installed.items():
        if not isinstance(block, dict):
            skipped.append((str(raw_id), "条目不是键值块"))
            continue
        mod_id = _to_int(raw_id)
        if mod_id is None:
            skipped.append((str(raw_id), "编号不是纯数字"))
            continue
        timeupdated = _to_int(block.get("timeupdated"))
        if timeupdated is None:
            # 没有本地时间 = 无法参与更新判定，repo 的必填项也过不去 → 跳过
            skipped.append((str(raw_id), "缺 timeupdated"))
            continue
        items.append(LocalItem(
            mod_id=mod_id,
            timeupdated=timeupdated,
            size=_to_int(block.get("size")),
            # 空串/缺失一律视为没有，交给 repo 的"None = 保持现值"语义
            manifest=(str(block["manifest"]) if block.get("manifest") else None),
        ))
    items.sort(key=lambda it: it.mod_id)
    return ScanResult(acf_path=p, items=items, skipped=skipped)


def diff_plan(
    items: Iterable[LocalItem],
    existing_status: Mapping[int, str],
    *,
    game_id: int,
) -> ScanPlan:
    """扫描结果与库中现状对表，产出写库计划（纯函数，不碰任何 IO）。

    existing_status：{mod_id: status}，调用方用 list_mods 取全后传字典。
    库中没有的 id 进 inserts，其余进 updates——三件套全量回填（幂等），
    其中原状态为 tracked 的才附带唯一允许的跃迁。输出按 mod_id 排序，
    与传入顺序无关。
    """
    updates: list[PlannedUpdate] = []
    inserts: list[PlannedInsert] = []
    for item in sorted(items, key=lambda it: it.mod_id):
        status = existing_status.get(item.mod_id)
        if status is None:
            inserts.append(PlannedInsert(item=item))
        else:
            updates.append(PlannedUpdate(
                item=item,
                to_downloaded=(status == "tracked"),
            ))
    return ScanPlan(game_id=game_id, updates=updates, inserts=inserts)


def apply(repo: ModRepository, plan: ScanPlan) -> ScanApplyReport:
    """把计划落库：整个计划包在一个事务里，要么全成要么全败。

    回填走 update_local_state（status=None = 状态不动；manifest/size 传
    None = acf 没这字段，保持库中现值）。新入库走 add_mod，status 直接是
    downloaded，url 现拼不联网，标题等远端字段留 NULL 待更新检测补全。
    """
    report = ScanApplyReport(updated=0, transitioned=0, inserted=0)
    with repo.transaction():
        for u in plan.updates:
            repo.update_local_state(
                u.item.mod_id,
                local_timeupdated=u.item.timeupdated,
                manifest=u.item.manifest,
                local_size=u.item.size,
                status="downloaded" if u.to_downloaded else None,
            )
            report.updated += 1
            if u.to_downloaded:
                report.transitioned += 1
        for ins in plan.inserts:
            repo.add_mod(Mod(
                mod_id=ins.item.mod_id,
                game_id=plan.game_id,
                status="downloaded",
                url=_WORKSHOP_URL.format(ins.item.mod_id),
                local_timeupdated=ins.item.timeupdated,
                manifest=ins.item.manifest,
                local_size=ins.item.size,
            ))
            report.inserted += 1
    return report
