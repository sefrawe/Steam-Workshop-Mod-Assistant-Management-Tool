"""异常处理功能模块 · 检测引擎"""
r"""workflows/exceptionFlow.py · 异常检测的纯逻辑引擎（V2 消费版）。

把当前档案的盘面与账本异常汇聚成报告，纯逻辑零 Qt。GUI 按 D24 拆
四页消费本引擎：异常处置（桶①②③④⑤⑥）、标题检测（桶C）、远端
健康（桶A）、依赖检测（桶B）——引擎只出数据，页面各取所需。

【V2 相对 V1 的两刀（都在抛弃清单精神内）】
1. 桶① 的 acf 半边退役：V1 用 localScanner 的质量谓词从 acf 里猜
   "下载中断"——三分类与质量谓词在 V2 抛弃清单（禁复活），acf 解析
   归 acfParser（只服务盘点，D10）。判决制下下载成败以终端输出为
   准，超时/失败是判决行（判决史可查），不需要从 acf 反推。桶①
   只剩"盘上空目录（断点残留）"一半；detect_local 不再收 steamcmd
   根参数，steamcmd_missing / acf_missing 两个旗标随之退役——
   "没查成要说明原因"的口径只剩 dead_root 一个，由 modVerifier 给。
2. detect_local 签名收窄为 (repo, game)：本地快检的全部原料 = 账本
   （repo）+ modVerifier 只读对账（download_dir），与核验页同源
   同尺。未配置 steamcmd 不再缩小检测范围（下载目录是档案字段）。

【入口清单】
- detect_local(repo, game) —— 离线快检，毫秒级：
    桶① 盘上空目录（断点残留）
    桶② 账实不符（账本记已下载、盘上没有）
    桶③ 孤儿目录（盘上有纯数字目录、账本完全不认识）
    桶④ 远端失效的账本半边（failed 归档 + failed 状态）
  另携带 tracked_on_disk（「待下载」条目盘上有目录——正常排队、
  不算异常，页面据此给指路提示：到入账中心确认补版本）。
- classify_entries(entries) —— 深检分桶：桶④另一半（result=9 确认
  失效）+ 桶⑤（查询失败 / 疑似合集）。联网查询由页面工作线程自行
  驱动（分批限速、取消归线程），查完把条目字典整体交进来分类。
- classify_health(entries, local_titles) —— 桶A 远端健康（标题不符 /
  弃坑 / banned），匿名接口数据可判，无需 API key。
- classify_dependencies(entries, lib_ids, baseline, first_fetch) ——
  桶B 依赖检测（缺依赖 / 依赖变化），keyed 接口数据。
- classify_local_titles(local_titles, keywords_raw) —— 桶C 本地标题
  关键词提醒，离线零联网，词表来自设置页。
- 桶⑥ 多前端冲突无法程序化检测，静态文案 MULTIFRONTEND_NOTE。

引擎不发明新检测：桶②③来自 modVerifier 只读对账（与核验页同源），
桶④账本半边来自 repo 的 failed 归档。修复动作也不在本层（重下命令、
认领、归档与替换都是现成件），由 GUI 壳按桶引导。

调用约定（写给 GUI 壳）：
- 下载目录死路径重推导是调用方的事（先把目录核对好再进来）——
  引擎保持只读；
- 引擎零 acf 调用，没有"acf 损坏中断检测"的失败路径（V1 的
  ValueError 上抛随旗标一起退役）；
- 查询分类全部吃纯数据字典——引擎不 import 客户端类型（注入边界，
  页面侧做 WorkshopItem/KeyedItem → dict 的翻译）；
- 刻意不报的两种情况（免得满屏狼烟）：tracked 盘上有内容 → 不是
  异常，是入账中心确认补版本的管辖（tracked_on_disk 只携带不报警）；
  deleted/failed 盘上仍有文件 → 软删除保留文件属正常，不报。
"""
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from core import modVerifier
from core.models import FailedMod, Game

# Steam API 查询分批上限 100 条/批（官方上限；引擎侧保留此常量供
# detect_remote 的防御性再切刀——页面工作线程自己也会按 100 分批）
_REMOTE_CHUNK = 100

# 联网查询函数的形状：收一批 mod id，交回条目字典列表。条目形状 =
# Steam GetPublishedFileDetails 的单条返回（数字几乎全为字符串，
# result 逐条保留）。引擎只认纯数据，不认识客户端的具体方法名。
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
    """Steam 返回的数字几乎全是字符串：单字段转不动就交 None，
    绝不为一个字段炸掉整批。刻意本地实现而不 import 客户端的同名
    工具——引擎只认注入查询交回的纯数据，不依赖客户端的实现细节。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class LocalReport:
    """离线快检报告：桶①②③④的本地可判部分 + dead_root 旗标。
    V2 相对 V1：interrupted（acf 质量谓词产物）与 steamcmd_missing /
    acf_missing 两旗标退役（见文件头）；新增 tracked_on_disk
    （非异常的指路信息）。"""
    game_id: int
    # —— 桶① 下载未完成：只剩空目录一半（断点残留）——
    empty_dirs: list[int] = field(default_factory=list)
    # —— 桶② 账实不符：账本记已下载、盘上目录没了 ——
    missing: list[int] = field(default_factory=list)
    # —— 桶③ 孤儿目录：盘上有纯数字目录、账本完全不认识 ——
    orphans: list[int] = field(default_factory=list)
    # —— 桶④ 远端失效的账本半边（离线可判的一半）——
    failed_records: list[FailedMod] = field(default_factory=list)  # 归档详情
    failed_ids: list[int] = field(default_factory=list)  # 归档 ∪ failed 状态
    # —— 非异常的指路信息：「待下载」条目盘上有目录（到入账中心
    # 确认补版本）；刻意不进 has_anomalies ——
    tracked_on_disk: list[int] = field(default_factory=list)
    # —— 六桶之外的附加发现（核验页同源，只报不动）——
    non_numeric: list[str] = field(default_factory=list)
    # —— 唯一的"没查成"旗标：下载目录不存在 → 盘面桶全不可判 ——
    dead_root: bool = False

    @property
    def has_anomalies(self) -> bool:
        """有没有任何需要用户看一眼的东西（含非数字内容）。
        界面用它决定"干净"还是"展开报告"。"""
        return bool(
            self.empty_dirs
            or self.missing
            or self.orphans
            or self.failed_ids
            or self.non_numeric)


def detect_local(repo, game: Game) -> LocalReport:
    """离线快检：桶①（空目录）②③ + 桶④账本半边。只读，不写库
    不动文件。V2 起不再收 steamcmd 根——账本半边与目录对账都不需要
    它（下载目录死路径由 modVerifier 立旗标）。"""
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
    # 桶①②③ + 非数字 + tracked_on_disk：modVerifier 只读对账
    # （与核验页同源同尺）
    vr = modVerifier.verify(game.download_dir, status_map)
    if vr.dead_root:
        # 下载目录不存在：逐条对账全是误报，立旗标收工
        # （账本半边 failed_ids 已在上面交付，不受影响）
        rep.dead_root = True
        return rep
    rep.missing = sorted(vr.missing)
    rep.empty_dirs = sorted(vr.empty)
    # 孤儿 = 盘上有目录且账本完全不认识这个编号（状态是 None）。
    # tracked（待下载）与 deleted/failed（正常保留文件）都不算异常：
    # tracked 由 tracked_on_disk 携带（指路入账中心），见模块头边界说明
    rep.orphans = sorted(
        mid for mid, st in vr.untracked_content if st is None)
    rep.tracked_on_disk = sorted(
        mid for mid, st in vr.untracked_content if st == "tracked")
    rep.non_numeric = list(vr.non_numeric)
    return rep


@dataclass
class RemoteFindings:
    """深度检测报告：联网重查一批 id 后的分桶结果。"""
    ok: list[int] = field(default_factory=list)  # 远端正常
    # —— 桶④ 另一半 ——
    invalid: list[int] = field(default_factory=list)  # result=9 失效
    # —— 桶⑤ 查询失败与疑似合集 ——
    query_failed: list[int] = field(default_factory=list)  # result 非 1 非 9
    # result=1 但 file_size 缺失或 0（疑似合集/异常，不替用户下结论，
    # 交页面引导"展开合集"——更新检测页有对应按钮）
    suspected_collection: list[int] = field(default_factory=list)
    # —— 连编号或结果码都读不出来的条目，原样陈列 ——
    malformed: list = field(default_factory=list)


def detect_remote(query: QueryFn, mod_ids: Iterable[int],
                  *, chunk: int = _REMOTE_CHUNK) -> RemoteFindings:
    """联网深度检测：对给定 id 逐批重查远端，按结果码分桶。
    query 由调用方注入——真机时传包装过的 SteamApiClient 查询
    （客户端自己管限流与重试），测试时传假函数。本函数只负责分批
    驱动；分类统一委托 classify_entries（页面工作线程自行分批查询
    后，直接调它做纯分类——批间节流归客户端，分类归引擎，两不耽误）。
    每个条目的判定口径：result=9 → 桶④ 失效；result=1 → 正常
    （file_size 缺失或 0 → 疑似合集）；其他/缺失 → 读不出结果码进
    malformed，读得出非 1 非 9 进桶⑤。"""
    step = max(1, int(chunk))
    ids = sorted({int(i) for i in mod_ids})
    entries: list[dict] = []
    for start in range(0, len(ids), step):
        entries.extend(query(ids[start:start + step]) or [])
    return classify_entries(entries)


def classify_entries(entries: Iterable[dict]) -> RemoteFindings:
    """把查询返回的条目字典列表按结果码分桶（detect_remote 的分类
    核心，独立成函数供复用）。判定口径与 detect_remote 相同。"""
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
# 桶A：远端健康异常 —— 匿名接口可判，无需 API key
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
    missing_banned_field：整批响应都没带 banned 字段时置位——
    "没查成要说明原因"口径：页面必须把这件事说出口，绝不静默当
    "没人被封禁"。（V2 客户端已解析 banned；此旗标只剩"Steam 响应
    本身没带"一种触发可能，保留作防御。）"""
    title_mismatch: list[tuple[int, str, str]] = field(default_factory=list)
    abandoned: list[int] = field(default_factory=list)
    banned: list[int] = field(default_factory=list)
    missing_banned_field: bool = False

    @property
    def total(self) -> int:
        """三类合计（汇总行与日志用）。"""
        return (len(self.title_mismatch)
                + len(self.abandoned) + len(self.banned))


def classify_health(entries: Iterable[dict],
                    local_titles: dict[int, str]) -> HealthFindings:
    """桶A 分类核心：对深检拿回的条目字典做健康判定。
    与 classify_entries 同一注入边界（纯数据进出）；local_titles =
    账本 编号 → 本地标题（页面侧一次取齐传入）。
    判定口径：只对 result=1 的条目判——查询失败/失效的归桶⑤桶④，
    不重复报；标题比较双方剥版本记号、压空白、casefold 后比较——
    剥完还不一致才算"标题不符"；弃坑关键词只扫远端标题（扫描述
    误报率不可控，刻意不扫）；banned 字段缺席 → missing_banned_field
    置位，绝不静默当 0。"""
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


# ============================================================
# 桶B：依赖检测（keyed 接口数据）
# ============================================================
@dataclass
class DepFindings:
    """桶B 判定结果。纯数据，页面渲染用。"""
    missing: dict[int, list[int]]           # mod 编号 → 缺失的必需编号
    changed: list[tuple[int, list[int], list[int]]]  # (mod, 旧边, 新边)
    undetermined: list[int]                 # children 键缺席（未判定）
    childless: list[int]                    # 真无依赖（正常）
    skipped: int                            # 远端失效/查询失败条数（桶④⑤管）
    fetched: int                            # 本次拿到清单的条目数


def classify_dependencies(entries: list[dict], lib_ids: set[int],
                          baseline: dict[int, list[int]],
                          *, first_fetch: bool) -> DepFindings:
    """桶B 判定：账实对照 + 依赖变化。纯函数：不碰库不碰网，数据由
    页面注入。entries 条目形状 {"mod_id","result","title","children"}；
    children 三态：list / None=键缺席——键缺席时按 num_children 仲裁（0=无依赖；>0 或缺席=未判定）。。
    baseline = 拉取前账本旧边；first_fetch = 依赖表还空着（首拉只建
    基线，一切差异都不算"变化"）。
    判定口径：missing = result=1 且必需编号不在 lib_ids（依赖指向的
    编号即使在账本但 failed/deleted 也不报——桶③④已管，不重复报）；
    changed = 非首拉且 set(旧边)≠set(新清单)；undetermined = 有依赖证据却拿不到清单；result≠1 的条目不判依赖，只计数。"""
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
            # children 键缺席的两层判读（2026-10-06 探针实测改判）：
            # Steam 对"必需物品数为 0"的条目省略空清单不回键，但条目
            # 自带的 num_children 元数据照发——它不随 includechildren
            # 参数伸缩，等于 Steam 官方盖章"我有没有依赖"。
            #   =0 → 无依赖，按空清单走正常判定与入账（下次拉取有
            #        基线可比，不会永远停在"未判定"）；
            #   >0（说有却没给清单）或字段也缺席 → 真异常，保守记
            #        未判定，绝不冒充无依赖写库覆盖真实依赖边
            if _to_int(e.get("num_children")) == 0:
                children = []
            else:
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


# ============================================================
# 桶C：本地标题关键词提醒 —— 离线、毫秒级、零联网、不入闸（D24）
# ============================================================
# 词表默认值：只收高置信度词（与桶A 远端弃坑词表同一哲学：宁可漏报
# 不误报）。设置页「本地标题提醒关键词」可自行增删，留空 = 停用。
# 中文词不进默认：本地标题含中文弃坑词的少且误报难判，需要再自己加。
DEFAULT_TITLE_KEYWORDS = "abandoned,deprecated,discontinued,unmaintained,outdated"


@dataclass
class LocalTitleFindings:
    """桶C 判定结果：本地标题含提醒关键词的条目。纯数据，页面渲染用。"""
    hits: dict[int, list[str]] = field(default_factory=dict)

    # 编号 → 命中的词

    @property
    def total(self) -> int:
        return len(self.hits)


def classify_local_titles(local_titles: dict[int, str],
                          keywords_raw: str | None) -> LocalTitleFindings:
    """桶C 分类核心（纯函数）。keywords_raw = 设置页原文：逗号/
    中文逗号/分号/空白分隔均可（宽容解析——设置页保存侧已把中文
    逗号归一半角，这里是消费端兜底，两道保险），大小写不敏感子串
    匹配；解析后为空 = 功能停用（返回空）。
    误报要明示：调用方必须在行内显示命中的词——"Abandoned Mines"
    这类地图名同样会命中，只提醒不处置，判断权在用户。只扫本地
    账本标题；远端侧的弃坑检测在桶A，两桶词表独立。"""
    out = LocalTitleFindings()
    raw = str(keywords_raw or "").strip()
    if not raw:
        return out
    kws = sorted({k.casefold() for k in re.split(r"[,，;；\s]+", raw)
                  if k.strip()})
    if not kws:
        return out
    for mid, title in local_titles.items():
        t = str(title or "").casefold()
        if not t:
            continue
        hit = [k for k in kws if k in t]
        if hit:
            out.hits[mid] = hit
    return out
