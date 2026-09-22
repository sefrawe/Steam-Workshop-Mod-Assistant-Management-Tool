"""本地扫描器
"""
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
locate_acf(steamcmd 布局基路径, app_id) → 定位 acf；找不到返回 None
  （不是错误，可能 steamcmd 路径没配或该游戏没下载过，由调用方提示）
scan_acf(path) → 解析出条目清单 + 跳过清单
diff_plan(items, 库中状态) → 生成回填 / 新入库计划，不碰任何 IO
apply(repo, plan) → 整个计划包进一个事务，要么全成要么全败

状态规则：
- 对库中已有 id：三件套全量回填；状态只允许 tracked → downloaded 这一个
  跃迁方向；deleted / failed 只补本地证据，状态不动
- 库中没有的 id（steamcmd 之外途径出现的条目）：acf 有记录 = 文件在盘上，
  直接以 downloaded 入库，url 现拼（不联网），标题等远端字段留 NULL，
  由下一轮更新检测的 API 查询顺带补全

质量谓词（决策 23：账本只记"确信下载成功"的条目）：
- timeupdated 缺失或 ≤ 0 → 疑似下载中断，跳过不入账
- manifest 缺失 / 空串 / "-1"（steamcmd 下载中断的标志值）→ 同上
- size 缺失或 0 → 不拦（只影响大小展示与备份空间预检），聚合成一条警告
- 被谓词拦下的原因统一以"疑似下载中断"开头，与格式类跳过（不是键值块 /
  编号不纯数字）区分开；ScanResult.interrupted 专门收集这类编号，
  GUI 据此单独提醒重下（异常修复动作全项目统一 = 重新下载）

错误边界：
- 文件级（读不了 / 不是 VDF / 没有 AppWorkshop 根）→ ValueError 显式爆炸，
  页面层 catch 后提示；语义统一，GUI 只需接一种异常
- 条目级（疑似下载中断 / 编号不是数字 / 不是键值块）→ 跳过并记录原因，
  绝不因一个坏条目炸掉其余上百条事实

对账假设：publishedfileid 在 Steam 全工坊全局唯一，一个条目只会出现在
一个游戏的 acf 里，所以 diff_plan 只按当前游戏对表；万一真出现跨游戏
撞主键，IntegrityError 会显式爆炸并整体回滚，不会写脏数据。

依赖 vdf 库（Valve KV 格式解析）；同步执行，百条级毫秒完成，不需要线程。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import vdf

from core.models import Mod
from core.modRepository import ModRepository

_WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"

# 质量谓词拦下的跳过原因统一用这个前缀（决策 23）：
# ScanResult.interrupted 靠它把"疑似下载中断"和"格式不完整"分开算数
_INTERRUPTED_PREFIX = "疑似下载中断"


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
    # size 缺失/为 0 的条目照常入账（决策 23：size 仅警告），但聚合一条
    # 人话警告留在这里，GUI 原样转述给用户
    warnings: list[str] = field(default_factory=list)

    @property
    def interrupted(self) -> list[str]:
        """被质量谓词判为"疑似下载中断"的原始编号（保持 acf 出现顺序）。

        与格式类跳过分开的意义：两者修法不同——格式类说明 acf 或工具
        出了问题要排查，中断类的修复动作就是重新下载。GUI 据此单独提醒。
        """
        return [k for k, reason in self.skipped
                if reason.startswith(_INTERRUPTED_PREFIX)]


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

    updated: int      # 回填条数（含跃迁）
    transitioned: int # 其中 tracked → downloaded
    inserted: int     # 新入库条数


def locate_acf(base_path: str | Path | None, app_id: int) -> Path | None:
    """按 steamcmd 目录布局定位工坊账本文件（决策 21⑤ 的口径）。

    base_path 认三种填写口径（程序内部自动推导时传的总是 steamcmd 根，
    后两种是兼容手工填写的容错）：
      填 steamcmd 根   → <根>\\steamapps\\workshop\\appworkshop_<appid>.acf
      填 steamapps 层  → <层>\\workshop\\appworkshop_<appid>.acf
      填 workshop 层   → <层>\\appworkshop_<appid>.acf

    找不到返回 None——不是错误，由调用方决定怎么提示。
    """
    raw = str(base_path or "").strip().strip('"').strip()
    if not raw:
        return None
    base = Path(raw).expanduser()
    name = f"appworkshop_{app_id}.acf"
    for candidate in (
            base / "steamapps" / "workshop" / name,  # 填了 steamcmd 根
            base / "workshop" / name,                # 填了 steamapps 层
            base / name,                             # 填了 workshop 层
    ):
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
    """解析 acf → 条目清单。

    文件级问题抛 ValueError；条目级问题跳过记原因——其中疑似下载
    中断的按质量谓词拦下（口径见模块头），绝不混进账本。
    """
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
    no_size: list[str] = []  # size 缺失/为 0 但照常入账的条目（只警告不拦）
    for raw_id, block in installed.items():
        if not isinstance(block, dict):
            skipped.append((str(raw_id), "条目不是键值块"))
            continue
        mod_id = _to_int(raw_id)
        if mod_id is None:
            skipped.append((str(raw_id), "编号不是纯数字"))
            continue

        # ---- 质量谓词（决策 23）：两道硬门，拦下的统一按"疑似下载中断"记 ----
        timeupdated = _to_int(block.get("timeupdated"))
        if timeupdated is None or timeupdated <= 0:
            # 没有有效的本地时间 = 这次下载没有正常完成（steamcmd 中断时
            # 账本里会留下 0），也过不去 repo 的必填项 → 不入账
            skipped.append(
                (str(raw_id),
                 f"{_INTERRUPTED_PREFIX}：timeupdated 缺失或为 0"))
            continue
        # 空串/缺失一律视为没有
        manifest = str(block["manifest"]) if block.get("manifest") else None
        if manifest is None or manifest == "-1":
            # "-1" 是 steamcmd 下载中断留在账本里的标志值；缺失/空串同样
            # 说明内容没有完整落地。没有 manifest 就无法定位具体版本 → 不入账
            skipped.append(
                (str(raw_id),
                 f"{_INTERRUPTED_PREFIX}：manifest 缺失或为 -1"))
            continue

        # size 只影响展示与备份空间预检（备份引擎对缺大小有自己的兜底），
        # 缺失/为 0 不拦入账，留到扫描结束后聚合一条警告
        size = _to_int(block.get("size"))
        if not size:  # None 或 0 都算"没有有效大小"
            no_size.append(str(raw_id))

        items.append(LocalItem(
            mod_id=mod_id,
            timeupdated=timeupdated,
            size=size,
            manifest=manifest,
        ))

    items.sort(key=lambda it: it.mod_id)

    warnings: list[str] = []
    if no_size:
        # 编号列表太长就截断，日志里给人看个意思即可，全量以 acf 为准
        preview = "、".join(no_size[:10]) + ("…" if len(no_size) > 10 else "")
        warnings.append(
            f"{len(no_size)} 个条目 size 缺失或为 0（不影响入账，仅影响大小"
            f"展示与备份空间预检）：{preview}")

    return ScanResult(acf_path=p, items=items, skipped=skipped,
                      warnings=warnings)


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
    None = acf 没这字段，保持库中现值）。新入库走 add_mod，status 直接
    是 downloaded，url 现拼不联网，标题等远端字段留 NULL 待更新检测补全。
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
