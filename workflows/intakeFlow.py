"""纳入已有 mod 流程（首次使用向导引擎）"""
r"""workflows/intakeFlow.py —— 功能模块「首次使用」第④步的逻辑半件。
界面半件是 gui/firstUsePage.py（下一轮交付）。本文件零界面、零 Qt，
pytest 直接覆盖（tests/test_intakeFlow.py）。

场景：用户在用本工具之前，早就用 Steam 客户端订阅过一堆 mod（客户端
把它们下载进了客户端自己的库）。本模块把"客户端订阅记录"纳入账本：
读客户端库里的 appworkshop_<appid>.acf，解析出订阅条目，与账本对表
后入库——让更新检测、命令生成从此接管这批 mod。

为什么入库为「已收录」(tracked) 而不是「已下载」(downloaded)：
本工具的"本地"专指 steamcmd 下载目录（决策 21 的账实口径），扫描
本地读的是 steamcmd 的 acf，永远看不到客户端那份拷贝。所以客户端
条目入库后是"已收录"，真正的"已下载"要等用户用 steamcmd 把内容
下进受管目录、扫描确认之后才算——链路与「加入新 mod」完全一致。

版本线索的语义（time_updated = 客户端 acf 的 timeupdated）：
客户端下载的是当时的最新版，这个值 ≈ 当时的远端版本。之后跑更新
检测：工坊没更新 → API 值相等，不误报；工坊更新过 → 如实报"有新
版本"。语义正好正确，检测页会用 API 实际值接管这个字段。

单源纪律（本文件刻意不重写的东西）：
- acf 定位与解析直接用 core.localScanner.locate_acf / scan_acf——
  客户端库与 steamcmd 的 acf 格式完全相同（同一个工坊系统写的，
  连目录布局都同构：<库根>\steamapps\workshop\...），解析器不需要
  也不应该有第二份；
- 入库只走 ModRepository.add_mod / update_api_metadata，整批一个
  事务，要么全成要么全败；
- 只补缺不覆盖：账里已有版本线索的绝不覆盖（API 值比客户端缓存
  更权威）；已删除 / 已失效的条目绝不复活（软删除是用户的决定）。
"""
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from core.localScanner import LocalItem
from core.models import Mod
from core.modRepository import ModRepository
from core.urlParser import WORKSHOP_URL_TEMPLATE  # 模板唯一定义点 = core/urlParser



@dataclass
class ClientIntakePlan:
    """一次"客户端订阅对表"的全部结论（classify 的产出，apply 的输入）。

    五桶语义：
    to_register        账里没有 → 入库为「已收录」，带版本线索
    to_fill_hint       已是「已收录」但没有任何版本线索 → 只补 time_updated
    already_tracked    已是「已收录」且版本线索已有 → 无事可做
    already_downloaded 已下载（steamcmd 已管辖）→ 无事可做，也不动
    inactive           已删除 / 已失效 → 跳过，绝不复活
    """
    app_id: int
    to_register: list[LocalItem] = field(default_factory=list)
    to_fill_hint: list[LocalItem] = field(default_factory=list)
    already_tracked: list[int] = field(default_factory=list)
    already_downloaded: list[int] = field(default_factory=list)
    inactive: list[tuple[int, str]] = field(default_factory=list)  # (编号, 状态码)


def classify_client_items(items: Iterable[LocalItem],
                          existing: Mapping[int, Mod], *,
                          app_id: int) -> ClientIntakePlan:
    """把客户端 acf 条目与账本对表分类。纯函数、只读、零 IO。

    existing：{mod_id: Mod}，调用方用 repo.list_mods(app_id) 现取——
    与扫描链做差同一口径，只按当前游戏对表（publishedfileid 全工坊
    唯一，一个条目只属于一个游戏）。
    """
    plan = ClientIntakePlan(app_id=app_id)
    for item in sorted(items, key=lambda it: it.mod_id):
        mod = existing.get(item.mod_id)
        if mod is None:
            plan.to_register.append(item)
        elif mod.status == "tracked":
            # 只补缺：已有版本线索的（多半来自 API 检测）比客户端缓存
            # 权威，不动
            if mod.time_updated is None:
                plan.to_fill_hint.append(item)
            else:
                plan.already_tracked.append(item.mod_id)
        elif mod.status == "downloaded":
            plan.already_downloaded.append(item.mod_id)
        else:
            # deleted / failed：跳过且绝不复活。软删除是用户的决定；
            # 失效归档有自己的处置链（T16），不在这里抢
            plan.inactive.append((item.mod_id, mod.status))
    return plan


@dataclass
class IntakeReport:
    """落库计数（只描述"计划执行了什么"，与扫描的 ScanApplyReport 同思路）。"""
    registered: int = 0    # 新入库（已收录）
    hints_filled: int = 0  # 补了版本线索的条数


def apply_intake(repo: ModRepository, plan: ClientIntakePlan) -> IntakeReport:
    """把计划落库：整批一个事务，要么全成要么全败。

    撞车说明：对表的是"当前档案"，若某编号恰好登记在别的档案名下
    （异常情况，正常到不了这里），add_mod 会撞主键抛 IntegrityError
    并整批回滚——调用方接住后提示核对，与「加入新 mod」的同款兜底一致。
    """
    report = IntakeReport()
    now = int(time.time())
    with repo.transaction():
        for item in plan.to_register:
            repo.add_mod(Mod(
                mod_id=item.mod_id,
                game_id=plan.app_id,
                status="tracked",
                url=WORKSHOP_URL_TEMPLATE.format(item.mod_id),
                # 版本线索：客户端下载时的版本 ≈ 当时的远端版本
                #（语义见文件头）；之后更新检测会用 API 实际值接管
                time_updated=item.timeupdated,
                first_tracked_at=now,
                # 刻意全空：manifest / local_* 三件套是 steamcmd 那份
                # 拷贝的事实，现在还没有；扫描确认后才回填
            ))
            report.registered += 1
        for item in plan.to_fill_hint:
            # 借 update_api_metadata 的门补 time_updated（该字段的唯一
            # 合法写入口）；这里写入的是客户端缓存值，界面上会说明来源
            repo.update_api_metadata(item.mod_id, time_updated=item.timeupdated)
            report.hints_filled += 1
    return report
