"""异常处理功能模块 · 检测引擎"""
"""
把当前游戏盘面和账本上的六类异常汇聚成一份报告（六类桶的定义
见记事本决策 23⑥），纯逻辑零 Qt。

两个入口，对应两种检测深度：

- detect_local(repo, root, game) —— 离线快检，毫秒级：
    桶① 下载未完成（acf 里不合格条目 + 盘上空目录）
    桶② 账实不符（账本说已下载、盘上没有）
    桶③ 孤儿目录（盘上有纯数字目录、账本完全不认识）
    桶④ 远端失效的"账本半边"（failed 归档 + failed 状态）
- detect_remote(query, mod_ids) —— 深度检测，需联网：
    桶④ 另一半（对可疑 id 重查 API，result=9 = 作者已删除/失效）
    桶⑤ 查询失败与疑似合集（result 非 1 非 9 / file_size 缺失或 0）
- 桶⑥ 多前端冲突无法程序化检测，静态文案 MULTIFRONTEND_NOTE
  供界面直接展示。
   - classify_health（v2.49 桶A）—— 远端健康异常（标题不符/弃坑/
   banned），匿名接口数据即可判，无需 API key


本引擎不发明新检测：桶①来自 localScanner 的质量谓词（决策 23），
桶②③来自 modVerifier 的只读对账（与核验页同源），桶④账本半边
来自 repo 的 failed 归档——引擎只负责"跑它们 + 合并成一份报告"。

修复动作也不在本层：重下/校验重下命令（commandBuilder）、扫描
入库（决策 20）、手动确认（决策 24）、result=9 的归档与替换
（repo 的 mark_failed / replace_failed_mod）全部现成，由将来的
GUI 壳按桶引导（复用核验页修复三选的形态）。

调用约定（写给将来的 GUI 壳）：
- root 参数 = steamcmd 根目录（GUI 用 steamPaths.steamcmd_root
  从设置页路径推导，modListPage._steamcmd_root 是现成套路）；
  传 None = 未配置 steamcmd，此时只剩账本半边可判，其余桶
  全部置空并立 steamcmd_missing 旗标——"没查成"要说明原因，
  绝不静默当 0；
- 下载目录死路径重推导是调用方的事（与扫描本地/核验页同款
  套路，先把目录核对好再进来）——引擎保持只读；
- acf 结构性损坏时 scan_acf 抛 ValueError，引擎原样上抛不吞
  （项目口径：显式爆炸），调用方弹窗说明；
- 联网检测的查询函数由调用方注入（query 参数），引擎不认识
  SteamApiClient 的具体方法名，把查询结果当纯数据分桶——
  依赖注入让单测不必联网（backupManager 的 runner 注入同款思路）。

边界说明（刻意不报的两种情况，免得界面满屏狼烟）：
- 库里 tracked、盘上有内容 → 不是异常，是决策 24 的手动确认流
  管辖（核验页勾选确认正管这里）；
- deleted/failed 的条目盘上仍有文件 → 属正常保留（软删除不删
  文件），不报。
"""
from collections.abc import Callable, Iterable
import re

from dataclasses import dataclass, field
from pathlib import Path

from core import localScanner, modVerifier
from core.models import FailedMod, Game

# 决策 17：Steam API 查询分批上限 100 条/批。引擎侧再切一刀是
# 防御性的：就算调用方把客户端的查询方法原样注入（客户端自己
# 也会分批限速），切成 ≤100 的小块也不会错
_REMOTE_CHUNK = 100

# 联网查询函数的形状：收一批 mod id，交回条目字典列表。
# 条目形状 = Steam GetPublishedFileDetails 的单条返回
# （数字几乎全为字符串，result 逐条保留——决策 17）
QueryFn = Callable[[list[int]], list[dict]]

# 桶⑥ 多前端环境冲突：静态文案，界面直接展示。
# 素材 = 记事本关键事实节（RimSort 共用 steamcmd、depotcache
# 缓存复活动作、Steam 客户端订阅补回机制）
MULTIFRONTEND_NOTE = (
    "多前端环境冲突（无法自动检测，请对照自查）：\n"
    "· 同一个 steamcmd 目录可能被多个工具指挥（如 RimSort）。"
    "别的工具清单里还留着的 mod，随时可能被它指挥 steamcmd 下回来"
    "——这就是「已删 mod 复活」的常见原因；\n"
    "· 双前端铁律：谁下载谁独占——本工具跑下载批次时，RimSort 等"
    "其它前端不要同时配置同一个 steamcmd 目录；反过来用 RimSort "
    "下载时，本工具这边也别开批次。谁在指挥，另一边就放手，"
    "两边同时指挥就是互相踩脚的源头；\n"
    "· 想永久删除一个 mod：先在本工具标记为已移除，再删盘上文件夹，"
    "并到其他工具的清单里一并移除——所有指挥这台 steamcmd 的工具"
    "都移除才算删干净；\n"
    "· Steam 客户端订阅的 mod 会被客户端按云端订阅账本自动补回"
    "（本工具不感知 Steam 客户端，此条只在你混用两端时需要警惕）；\n"
    "· 排查「下载内容反复损坏」时可手动清理 steamcmd 的 depotcache"
    "（仓库缓存），日常无需理会。"
)

def _to_int(value) -> int | None:
    """决策 17 口径的宽容转换：Steam 返回的数字几乎全是字符串，
    单字段转不动就交 None，绝不为一个字段炸掉整批。
    刻意本地实现而不 import 客户端的同名工具——引擎只认注入查询
    交回的纯数据，不依赖客户端的实现细节。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class LocalReport:
    """离线快检报告：六桶里的本地可判部分 + 三个"没查成"旗标。"""

    game_id: int
    # —— 桶① 下载未完成（两种长相，修法同为重新下载）——
    interrupted: list[int] = field(default_factory=list)  # acf 条目不合格
    empty_dirs: list[int] = field(default_factory=list)   # 盘上空目录
    # —— 桶② 账实不符：账本记已下载、盘上目录没了 ——
    missing: list[int] = field(default_factory=list)
    # —— 桶③ 孤儿目录：盘上有纯数字目录、账本完全不认识 ——
    orphans: list[int] = field(default_factory=list)
    # —— 桶④ 远端失效的账本半边（离线可判的一半）——
    failed_records: list[FailedMod] = field(default_factory=list)  # 归档详情
    failed_ids: list[int] = field(default_factory=list)  # 归档 ∪ failed 状态
    # —— 六桶之外的附加发现（核验页同源，只报不动）——
    non_numeric: list[str] = field(default_factory=list)

    # —— 三个"没查成"旗标：各自说清原因，调用方据此提示，不许静默 ——
    steamcmd_missing: bool = False  # 未配置 steamcmd → 盘上桶全不可判
    dead_root: bool = False         # 下载目录不存在 → 盘上桶全不可判
    acf_missing: bool = False       # 找不到 acf → 桶① 的 acf 半边不可判

    @property
    def has_anomalies(self) -> bool:
        """有没有任何需要用户看一眼的东西（含非数字内容）。
        界面用它决定"干净"还是"展开报告"。"""
        return bool(
            self.interrupted or self.empty_dirs or self.missing
            or self.orphans or self.failed_ids or self.non_numeric)


def detect_local(repo, root: Path | str | None, game: Game) -> LocalReport:
    """离线快检：桶①②③ + 桶④账本半边。只读，不写库不动文件。"""
    rep = LocalReport(game_id=game.app_id)

    # 库内状态一览：对账与分桶都要用它
    status_map = {m.mod_id: m.status for m in repo.list_mods(game.app_id)}

    # 桶④ 账本半边：failed 归档表（带原因等详情）与 status=failed 的
    # 条目取并集——正常情况两者一一对应，取并集是防"归档在、状态
    # 被别处改动"之类的半残状态漏报
    records = list(repo.list_failed(game.app_id))
    rep.failed_records = records
    failed = {r.mod_id for r in records}
    failed |= {mid for mid, st in status_map.items() if st == "failed"}
    rep.failed_ids = sorted(failed)

    # 未配置 steamcmd：盘上一切不可判，账本半边照常交付
    if root is None:
        rep.steamcmd_missing = True
        return rep
    root = Path(root)

    # 桶②③ + 非数字内容：modVerifier 只读对账（与核验页同源）
    vr = modVerifier.verify(game.download_dir, status_map)
    if vr.dead_root:
        # 下载目录不存在：逐条对账全是误报，立旗标收工
        rep.dead_root = True
        return rep
    rep.missing = sorted(vr.missing)
    rep.empty_dirs = sorted(vr.empty)
    # 孤儿 = 盘上有目录且账本完全不认识这个编号（status 是 None）。
    # tracked（待确认）与 deleted/failed（正常保留文件）都不算异常，
    # 见模块头"边界说明"
    rep.orphans = sorted(mid for mid, st in vr.untracked_content
                         if st is None)
    rep.non_numeric = list(vr.non_numeric)

    # 桶① 的 acf 半边：质量谓词（决策 23）拦下的疑似中断条目
    acf = localScanner.locate_acf(root, game.app_id)
    if acf is None:
        rep.acf_missing = True
        return rep
    scan = localScanner.scan_acf(acf)  # 结构损坏 → ValueError 上抛
    interrupted: list[int] = []
    for raw in scan.interrupted:
        v = _to_int(raw)
        if v is not None:
            interrupted.append(v)
    rep.interrupted = sorted(interrupted)
    return rep


@dataclass
class RemoteFindings:
    """深度检测报告：联网重查一批 id 后的分桶结果。"""

    ok: list[int] = field(default_factory=list)            # 远端正常
    # —— 桶④ 另一半 ——
    invalid: list[int] = field(default_factory=list)       # result=9 失效
    # —— 桶⑤ 查询失败与疑似合集 ——
    query_failed: list[int] = field(default_factory=list)  # result 非 1 非 9
    suspected_collection: list[int] = field(default_factory=list)
    # result=1 但 file_size 缺失或 0（决策 17：疑似合集/异常，
    # 不替用户下结论，交界面引导"展开合集"）
    # —— 连编号或结果码都读不出来的条目，原样陈列 ——
    malformed: list = field(default_factory=list)

def detect_remote(query: QueryFn, mod_ids: Iterable[int],
                  *, chunk: int = _REMOTE_CHUNK) -> RemoteFindings:
    """联网深度检测：对给定 id 逐批重查远端，按结果码分桶。

    query 由调用方注入（形状见 QueryFn）——真机时传包装过的
    SteamApiClient 查询（客户端自己管限流与重试），测试时传假函数。
    本函数只负责分批驱动查询；分类统一委托 classify_entries
    （GUI 工作线程自行驱动查询后，可直接调它做纯分类——批间
    节流由客户端管，分类归引擎，两不耽误）。
    每个条目的判定（决策 17 口径）：
      result=9  → 桶④ 失效（作者删除/被下架）
      result=1  → 正常；但 file_size 缺失或 0 → 疑似合集
      其他/缺失 → 读不出结果码进 malformed，读得出非 1 非 9 进桶⑤
    """
    step = max(1, int(chunk))
    ids = sorted({int(i) for i in mod_ids})
    entries: list[dict] = []
    for start in range(0, len(ids), step):
        entries.extend(query(ids[start:start + step]) or [])
    return classify_entries(entries)


def classify_entries(entries: Iterable[dict]) -> RemoteFindings:
    """把查询返回的条目字典列表按结果码分桶（detect_remote 的
    分类核心，独立成函数供复用：GUI 工作线程自行分批查询后，
    把全部条目一次交进来分类）。判定口径与 detect_remote 相同：
    引擎只认纯数据条目字典，不 import 客户端类型（决策 28④）。"""
    out = RemoteFindings()
    for entry in entries:
        pid = _to_int(entry.get("publishedfileid"))
        if pid is None:
            # 连编号都读不出：整条原样收进 malformed，人工看
            out.malformed.append(entry.get("publishedfileid"))
            continue
        result = _to_int(entry.get("result"))
        if result is None:
            out.malformed.append(pid)
        elif result == 9:
            out.invalid.append(pid)
        elif result == 1:
            size = _to_int(entry.get("file_size"))
            # size 缺失(None)或 0 都算疑似合集；负数不会出现，
            # 万一出现按异常条目处理也归这边，绝不写库
            if size:
                out.ok.append(pid)
            else:
                out.suspected_collection.append(pid)
        else:
            out.query_failed.append(pid)
    out.ok.sort()
    out.invalid.sort()
    out.query_failed.sort()
    out.suspected_collection.sort()
    return out
# ============================================================
# 桶A（v2.49）：远端健康异常 —— 匿名接口可判，无需 API key
# ============================================================

# 弃坑关键词（对远端标题 casefold 后子串匹配）。宁可漏报不误报：
# 只收高置信度词；"最终版""别更新了"之类赌气命名不收，免得满屏狼烟
_ABANDONED_KEYWORDS = (
    "abandoned", "deprecated", "discontinued", "unmaintained",
    "no longer", "不再维护", "不再更新", "停止更新", "停更", "弃坑",
)

# 标题比较前剥掉的版本记号：作者常把版本号写进标题（v1.6 / [1.6]），
# 每更新一次标题就变一次——不剥掉"标题不符"会误报成灾。
# 剥完再比，剩下的差异才算真改名
_VERSION_TOKEN = re.compile(r"v?\d+\.\d+(\.\d+)?", re.IGNORECASE)


def _normalize_title(text: str) -> str:
    """标题规范化：剥版本记号 → 压空白 → casefold。只用于比较。"""
    t = _VERSION_TOKEN.sub(" ", text)
    return " ".join(t.split()).casefold()


@dataclass
class HealthFindings:
    """桶A 分桶结果：远端实况与账本记录对不上的三类信号。

    - title_mismatch：规范化后标题不一致（作者改名/上传内容被顶替
      的信号）——元素 = (编号, 本地标题, 远端标题)，黄字提醒；
    - abandoned：远端标题含弃坑关键词——作者明示不再维护，红字；
    - banned：远端 banned=1——被 Steam 封禁的条目，红字。
    missing_banned_field：整批响应都没带 banned 字段（= 客户端封装
    没解析它；Steam 原始响应恒有）时置位——"没查成要说明原因"
    口径：调用方必须把这件事说出口，绝不静默当"没人被封禁"。
    """
    title_mismatch: list[tuple[int, str, str]] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)
    banned: list[int] = field(default_factory=list)
    missing_banned_field: bool = False

    @property
    def total(self) -> int:
        """三类合计（汇总行与日志用）。"""
        return (len(self.title_mismatch) + len(self.abandoned)
                + len(self.banned))


def classify_health(entries: Iterable[dict],
                    local_titles: dict[int, str]) -> HealthFindings:
    """桶A 分类核心：对深检拿回的条目字典做健康判定。
    与 classify_entries 同一注入边界（纯数据进出，不 import 客户端
    类型）；local_titles = 账本 编号 → 本地标题（页面侧一次取齐传入）。

    判定口径：
    - 只对 result=1 的条目判——查询失败/失效的归桶⑤桶④，不重复报；
    - 标题比较：双方剥版本记号、压空白、casefold 后比较——剥完还
      不一致才算"标题不符"；
    - 弃坑关键词只扫远端标题（扫描述误报率不可控，刻意不扫）；
    - banned 字段缺席 → missing_banned_field 置位，绝不静默当 0。
    """
    out = HealthFindings()
    saw_result1 = False
    banned_known = False
    for entry in entries:
        pid = _to_int(entry.get("publishedfileid"))
        if pid is None or _to_int(entry.get("result")) != 1:
            continue
        saw_result1 = True
        remote_title = str(entry.get("title") or "").strip()
        raw_banned = entry.get("banned")
        if raw_banned is not None:
            banned_known = True
            if _to_int(raw_banned) == 1:
                out.banned.append(pid)
        if not remote_title:
            continue
        low = remote_title.casefold()
        if any(k in low for k in _ABANDONED_KEYWORDS):
            out.abandoned.append(pid)
        local = str(local_titles.get(pid) or "").strip()
        if local and _normalize_title(local) != _normalize_title(remote_title):
            out.title_mismatch.append((pid, local, remote_title))
    out.title_mismatch.sort(key=lambda t: t[0])
    out.abandoned.sort()
    out.banned.sort()
    out.missing_banned_field = saw_result1 and not banned_known
    return out
@dataclass
class DepFindings:
    """桶B（依赖检测）判定结果（v2.49）。纯数据，页面渲染用。"""
    missing: dict[int, list[int]]   # mod 编号 → 缺失的必需编号
    changed: list[tuple[int, list[int], list[int]]]  # (mod, 旧边, 新边)
    undetermined: list[int]         # children 键缺席（未判定）
    childless: list[int]            # 真无依赖（正常）
    skipped: int                    # 远端失效/查询失败条数（桶④⑤管）
    fetched: int                    # 本次拿到清单的条目数


def classify_dependencies(entries: list[dict], lib_ids: set[int],
                          baseline: dict[int, list[int]], *,
                          first_fetch: bool) -> DepFindings:
    """桶B 判定：账实对照 + 依赖变化。纯函数：不碰库不碰网，数据
    由页面注入（与 classify_entries / classify_health 同一纪律）。
    entries 条目形状 {"mod_id","result","title","children"}；children
    三态：list / None=键缺席。baseline = 拉取前账本旧边；first_fetch
    = 依赖表还空着（首拉只建基线，一切差异都不算"变化"）。
    判定口径：
    - missing：result=1 且必需编号不在 lib_ids。依赖指向的编号即使
      在账本但 failed/deleted 也不报——桶③④已管，不重复报；
    - changed：非首拉且 set(旧边)≠set(新清单)。刚导入的新 mod 首次
      被拉取也会以"新增依赖"出现在这里——信息无害，照实展示；
    - undetermined：children 键缺席——绝不冒充"无依赖"入账；
    - result≠1 的条目不判依赖（失效/私有无从谈起），只计数。"""
    missing: dict[int, list[int]] = {}
    changed: list[tuple[int, list[int], list[int]]] = []
    undetermined: list[int] = []
    childless: list[int] = []
    fetched = skipped = 0
    for e in entries:
        mid = e.get("mod_id")
        if mid is None:
            continue
        if e.get("result") != 1:
            skipped += 1
            continue
        children = e.get("children")
        if children is None:
            undetermined.append(mid)
            continue
        fetched += 1
        new_set = set(children)
        lack = sorted(i for i in new_set if i not in lib_ids and i != mid)
        if lack:
            missing[mid] = lack
        old = baseline.get(mid) or []
        if not first_fetch and set(old) != new_set:
            changed.append((mid, sorted(set(old)), sorted(new_set)))
        if not new_set:
            childless.append(mid)
    return DepFindings(missing=missing, changed=changed,
                       undetermined=undetermined, childless=childless,
                       skipped=skipped, fetched=fetched)
