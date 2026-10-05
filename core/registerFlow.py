"""登记流程（入账中心·登记区引擎）
"""
"""
core/registerFlow.py —— 入账中心登记区的逻辑半件。界面半件
gui/accountCenterPage.py；本文件零 Qt，pytest 直测。

登记区收"意图"：用户把工坊网址 / 纯编号 / steamcmd 下载命令贴进来，
解析出的编号与当前账本对表分类，新编号入库为 tracked（"待下载"）。
登记不是判决（事实）也不是认领（盘面事实）——它只是"想下载"的
跨会话存档；真正的版本记账仍只走确认门（R17 不变）。

五桶口径（预览表逐行着色）：
- to_register       账里没有 → 登记 tracked（批查元数据顺手带上）
- already_in_ledger 账里已有（任意状态）→ 跳过，只显示状态
- invalid           无法识别的原始片段 → 原样回报，不猜不纠正
- wrong_game        命令行 AppID ≠ 当前档案 → 整批红灯
- blacklisted       已清账黑名单 → 照登记 + 警示（确认/认领被拦）

错档为什么整批拦（而不是只拦命令里那几条）：urlParser 把编号与
命令行 AppID 分开汇报、不逐条挂钩——混合粘贴时无法断言哪个编号
来自哪条命令。"解析结果只属于当前档案"的防呆同一思路：归属存疑
时宁可全拦，让用户分开粘贴；误登记的代价（命令生成错 AppID、
下载 No match）比多贴一次高。

apply 的唯一写库点：整批一个事务（首次使用向导的纳入流程同款），
全成或全败。元数据来自批查（联网归页面层）；查不到就留 NULL——
标题/大小由下一轮更新检测补全（登记区口径：失败留 NULL 标注）。
"""
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.models import Mod
from core.urlParser import WORKSHOP_URL_TEMPLATE, ParseReport


@dataclass
class RegisterPlan:
    """classify_register 的产出（预览与 apply 的输入）。"""
    app_id: int
    to_register: list[int] = field(default_factory=list)
    already_in_ledger: list[tuple[int, str]] = field(default_factory=list)
    blacklisted: list[int] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    wrong_game_apps: list[int] = field(default_factory=list)
    mismatch_ids: list[int] = field(default_factory=list)

    @property
    def blocked_by_mismatch(self) -> bool:
        """错档红灯：True = 本批全部拦下、一个不登。"""
        return bool(self.wrong_game_apps)


def classify_register(parsed: ParseReport,
                      existing: Mapping[int, Any],
                      purged: Iterable[int], *,
                      app_id: int) -> RegisterPlan:
    """解析结果与账本对表分类。纯函数、只读、零 IO。

    parsed：urlParser.parse_lines 的产出；
    existing：{mod_id: 任意有 .status 的对象}，调用方用
        repo.list_mods(app_id) 现取成字典（纳入流程同口径）；
    purged：黑名单命中集合（repo.filter_purged 的返回值）。
    """
    plan = RegisterPlan(app_id=app_id)
    plan.invalid = list(parsed.invalid)

    # 错档红灯：命令行里出现非当前档案的 AppID → 整批拦下（理由见
    # 文件头）；无效行照常回报——那是格式反馈，与归属无关
    wrong = sorted({a for a in parsed.command_app_ids if a != app_id})
    if wrong:
        plan.wrong_game_apps = wrong
        plan.mismatch_ids = list(parsed.mod_ids)
        return plan

    purged_set = set(purged)
    for mid in parsed.mod_ids:            # 保持解析顺序（解析器已去重）
        mod = existing.get(mid)
        if mod is not None:
            plan.already_in_ledger.append((mid, mod.status))
        elif mid in purged_set:
            # 黑名单：登记照办（意图自由），确认/认领的正门会拦——
            # 拦截语义在确认门，这里只预警
            plan.to_register.append(mid)
            plan.blacklisted.append(mid)
        else:
            plan.to_register.append(mid)
    return plan


@dataclass
class RegisterMeta:
    """一条待登记编号的批查元数据（查不到的字段为 None = 留 NULL）。"""
    mod_id: int
    title: str | None = None
    creator: str | None = None
    file_size: int | None = None
    time_updated: int | None = None


@dataclass
class RegisterReport:
    """落库计数。with/without 分开报——登记当轮"有没有标题"是
    用户可感知的质量差（建行当轮就有元数据，失败留 NULL 标注）。"""
    registered: int = 0
    with_metadata: int = 0
    without_metadata: int = 0


def apply_register(repo, plan: RegisterPlan,
                   metadata: Mapping[int, RegisterMeta]) -> RegisterReport:
    """把计划落库：整批一个事务，要么全成要么全败。

    入库 = tracked（"待下载"）；url 现拼不联网（登记不强制联网，
    元数据有则带、无则 NULL 等检测兜底）。time_updated 是登记时刻
    的远端观测值——"检测只写远端侧"的合法写入，不碰确认基准
    （确认基准只能走确认门，R17）。

    repo 只要求一个成员：transaction() 上下文 + add_mod(mod)——
    与纳入流程的 apply 同款最小接口，pytest 用假 repo 直测，
    不碰真库（真库行为由账本层自己的测试把关）。
    """
    report = RegisterReport()
    now = int(time.time())
    with repo.transaction():
        for mid in plan.to_register:
            meta = metadata.get(mid)
            has_meta = bool(meta and (meta.title or meta.time_updated
                                      or meta.file_size))
            repo.add_mod(Mod(
                mod_id=mid,
                game_id=plan.app_id,
                status="tracked",
                url=WORKSHOP_URL_TEMPLATE.format(mid),
                title=meta.title if meta else None,
                creator=meta.creator if meta else None,
                file_size=meta.file_size if meta else None,
                time_updated=meta.time_updated if meta else None,
                first_tracked_at=now,
            ))
            report.registered += 1
            if has_meta:
                report.with_metadata += 1
            else:
                report.without_metadata += 1
    return report
