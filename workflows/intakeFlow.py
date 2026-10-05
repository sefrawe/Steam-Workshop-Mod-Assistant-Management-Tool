"""客户端订阅纳入引擎
"""
"""
workflows/intakeFlow.py —— 首次使用向导第④步「纳入客户端已有的 mod」
的逻辑半件。零界面、零 Qt，pytest 直接覆盖（tests/test_intakeFlow.py）。

场景：已经在玩、在 Steam 客户端里订阅了 mod 的用户——他们的 mod
内容躺在客户端自己的库目录里（appworkshop_<appid>.acf 记账），
steamcmd 与本工具对此一无所知。本引擎把客户端账本与工具账本对表，
把"工具账本里还没有"的编号登记进来（「待下载」），管理权就此交接
给本工具；之后用 steamcmd 补下载、收尾确认清单背书入账，与「加入
新 mod」同一条链。

V2 判决制四桶（V1 是五桶，本版砍掉"待补版本线索"桶——拍板 3）：
- to_register         账本里还没有 → 登记为「待下载」；
- already_tracked     已在账且是「待下载」→ 跳过。版本信息一个字
                      不碰：客户端 acf 里的 timeupdated 既不是终端
                      判决也不是盘面事实，没资格自动写进任何版本
                      字段——真实版本等下载后经确认门背书，或跑一次
                      更新检测自然补全；
- already_downloaded  已是「已下载」→ 跳过（steamcmd 那份已经管着）；
- inactive            已删除 / 已失效等其他状态 → 不复活（软删除是
                      人的决定，机器不翻案）。原因 = 账本里的状态码
                      原样带回，界面上如实展示。

纪律：只补缺不覆盖、不复活、重复纳入幂等（再跑一遍全进跳过桶）；
整批一个事务，绝不写一半。
"""
import time
from dataclasses import dataclass, field

from core.models import Mod
from core.urlParser import WORKSHOP_URL_TEMPLATE


@dataclass
class ClientIntakePlan:
    """第④步预览的产出：客户端条目与账本对表后的分堆结果。

    to_register        待纳入条目的列表（入库候选；元素由
                       core.localScanner 产出，引擎只读 .mod_id，
                       鸭子类型不 import——解析器归解析器，分堆归
                       分堆，谁也不绑谁）；
    already_tracked / already_downloaded
                       跳过桶：编号列表（报数与明细展示用）；
    inactive           (mod_id, 状态码) 元组列表——已删除/失效等，
                       原因 = 账本里的状态码原样带回；
    app_id             归属档案（classify 存进来，apply 从这里取——
                       两个函数之间不用再传第二遍）。
    """
    to_register: list = field(default_factory=list)
    already_tracked: list[int] = field(default_factory=list)
    already_downloaded: list[int] = field(default_factory=list)
    inactive: list[tuple[int, str]] = field(default_factory=list)
    app_id: int | None = None

    @property
    def work_count(self) -> int:
        """会写账的条数——界面据此点亮【纳入账本】按钮。"""
        return len(self.to_register)


def classify_client_items(items, existing: dict[int, Mod],
                          app_id: int) -> ClientIntakePlan:
    """把客户端条目与账本现状对表分堆（只读，不写库）。

    items     客户端账本解析出的条目列表（core.localScanner.scan_acf
              产出）；
    existing  账本现状：{mod_id: Mod}——调用方用 list_mods(app_id)
              现取，保证对的是"这一刻"的账；
    app_id    归属档案的 AppID（存进计划，apply_intake 用）。

    分堆口径：账里没有 → 待纳入；是「已下载」→ 已下载跳过；是
    「待下载」→ 已在册跳过（版本字段一个不碰）；其余状态（已删除、
    已失效等）→ 不复活，原因原样带回。
    """
    plan = ClientIntakePlan(app_id=app_id)
    for item in items:
        mod = existing.get(item.mod_id)
        if mod is None:
            plan.to_register.append(item)      # 账里没有 → 纳入
        elif mod.status == "downloaded":
            plan.already_downloaded.append(item.mod_id)
        elif mod.status == "tracked":
            plan.already_tracked.append(item.mod_id)
        else:
            # 已删除 / 已失效 / 其他状态：如实带原因，绝不复活
            plan.inactive.append((item.mod_id, mod.status))
    return plan


@dataclass
class IntakeReport:
    """纳入执行的结果报数（界面日志与卡片展示用）。"""
    registered: int = 0


def apply_intake(repo, plan: ClientIntakePlan) -> IntakeReport:
    """把预览计划写进账本：只登记 to_register，一个版本字段都不碰。

    - 整批一个事务：任何一条失败全部回滚，绝不写一半；
    - 只写编号 / 归属 / 链接 / 状态（「待下载」，内部码 tracked，与
      入账中心、registerFlow 同码）/ 收录时刻——标题等远端信息留给
      更新检测补全，版本只等确认门背书（R17 红线）；
    - 跳过桶与 inactive 桶本函数碰都不碰（幂等的另一半：重复纳入
      安全，再对表一遍全进跳过桶）。

    预览过期（对表之后别的页面恰好登记了同一编号）会撞主键抛
    sqlite3.IntegrityError 并整批回滚——界面接住提示重新预览。
    空计划直接返回 0，不碰数据库。
    """
    report = IntakeReport()
    if not plan.to_register or plan.app_id is None:
        return report
    now = int(time.time())
    with repo.transaction():
        for item in plan.to_register:
            repo.add_mod(Mod(
                mod_id=item.mod_id,
                game_id=plan.app_id,
                url=WORKSHOP_URL_TEMPLATE.format(item.mod_id),
                status="tracked",
                first_tracked_at=now,
            ))
    report.registered = len(plan.to_register)
    return report
