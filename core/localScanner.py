"""客户端 acf 已安装清单解析器（纳入链专用）
"""
"""
core/localScanner.py · 解析 appworkshop_<appid>.acf 的
WorkshopItemsInstalled 区块，产出"客户端已安装条目"清单。

本件在 V2 的职责范围（与记事本 D10 修订的分工备案）：
- 只服务一条链：首次使用第④步「纳入客户端已有的 mod」——把 Steam
  客户端库里的已安装订阅条目解析出来，交 workflows/intakeFlow 与
  账本对表、登记为「待下载」；
- 本件零写库：没有 diff_plan、没有 apply、没有状态跃迁——V1 版的
  "扫描灌库"管线在判决制下是抛弃清单成员（扫描无写入权），已删，
  不迁移、不保留空壳。唯一的产出就是只读的条目清单；
- 盘点/认领链的 acf 解析（WorkshopItemsMetadata 版本口径）住
  core/acfParser（D10 口径），与本件不重复：那个读版本，本件读
  安装清单，各取所需；
- acf 条目移除（断根毒 mod）住 core/steamPaths 的
  remove_items_from_acf，本件不管。

保留自 V1 的两样东西（原样，未改口径）：
- locate_acf 的三种填写层容错（库根 / steamapps / workshop）——
  客户端库与 steamcmd 布局同构，同一份定位逻辑两处通用；
- 质量谓词（timeupdated 缺失或 ≤0、manifest 缺失或 "-1" → 跳过）：
  下载中断的残件不进纳入候选（登记进来也是要重下的空号），原因
  照旧以"疑似下载中断"前缀记录，界面按此分开报数提醒重下。
  注意它只是"排除出候选清单并说明原因"，不是任何写库闸门。

错误边界（与 V1 同）：文件级问题（读不了 / 不是 VDF / 没有
AppWorkshop 根）→ ValueError 显式爆炸，页面层接住提示；条目级
问题跳过并记录原因，绝不因一个坏条目炸掉其余上百条事实。

依赖 vdf 库（Valve KV 格式解析）；同步执行，百条级毫秒完成，
不需要线程。零 Qt、零 repo——纯函数直接 pytest。
"""
from dataclasses import dataclass, field
from pathlib import Path

import vdf
from core.steamPaths import locate_acf  # noqa: F401  # acf 定位单源在 steamPaths（v1.3 既定归位）；
# 本模块 re-export 保旧 import 路径——firstUsePage 与测试零改动

# 质量谓词拦下的跳过原因统一用这个前缀：
# ScanResult.interrupted 靠它把"疑似下载中断"和"格式不完整"分开算数
_INTERRUPTED_PREFIX = "疑似下载中断"


@dataclass
class LocalItem:
    """acf 中一个已安装条目的本地三件套。manifest 保持 str
    （无符号 64 位逼近 SQLite INTEGER 上限，schema 约定）。"""
    mod_id: int
    timeupdated: int
    size: int | None
    manifest: str | None


@dataclass
class ScanResult:
    """一次解析的产出。skipped 记录被跳过的条目及原因（原始键, 原因）。"""
    acf_path: Path
    items: list[LocalItem]
    skipped: list[tuple[str, str]]
    # size 缺失/为 0 的条目照常进 items（size 仅影响展示，不拦），
    # 但聚合一条人话警告留在这里，GUI 原样转述给用户
    warnings: list[str] = field(default_factory=list)

    @property
    def interrupted(self) -> list[str]:
        """被质量谓词判为"疑似下载中断"的原始编号（保持 acf 出现
        顺序）。与格式类跳过分开的意义：修法不同——格式类说明 acf
        或工具出了问题要排查，中断类的修复动作就是重新下载。GUI
        据此单独提醒。"""
        return [k for k, reason in self.skipped
                if reason.startswith(_INTERRUPTED_PREFIX)]

# locate_acf 已上收单源：core/steamPaths（"路径布局知识只住这一份"纪律
# 兑现）。收编前两版逐行 diff 行为零差异，无漂移损失；
# from core.localScanner import locate_acf 旧路径经顶部 re-export 有效

def _to_int(value) -> int | None:
    """acf 的数字全是字符串；转不动/缺失返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def scan_acf(path: str | Path) -> ScanResult:
    """解析 acf → 已安装条目清单。

    文件级问题抛 ValueError；条目级问题跳过记原因——其中疑似下载
    中断的按质量谓词拦下（口径见模块头），绝不混进候选清单。
    """
    p = Path(path)
    try:
        # utf-8-sig：顺手吃掉 Windows 文件可能带的 BOM，否则 vdf
        # 第一个键就解析坏
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"acf 读取失败：{p}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"acf 不是 UTF-8 文本：{p}") from exc
    try:
        data = vdf.loads(text)
    except Exception as exc:
        # vdf 对半截文件抛自己的异常类型；统一转写为 ValueError 并
        # 保留原始异常链——是转写不是吞掉，页面层只需接一种异常
        raise ValueError(
            f"acf 解析失败（文件损坏或不是 VDF 格式）：{p}") from exc
    if not isinstance(data, dict) or not isinstance(
            data.get("AppWorkshop"), dict):
        raise ValueError(f"该文件没有 AppWorkshop 根区块（可能选错了文件）：{p}")

    installed = data["AppWorkshop"].get("WorkshopItemsInstalled")
    if not isinstance(installed, dict):
        installed = {}  # 合法空态：游戏装了但一个工坊条目都没有

    items: list[LocalItem] = []
    skipped: list[tuple[str, str]] = []
    no_size: list[str] = []  # size 缺失/为 0 但照常收录的条目（只警告不拦）

    for raw_id, block in installed.items():
        if not isinstance(block, dict):
            skipped.append((str(raw_id), "条目不是键值块"))
            continue
        mod_id = _to_int(raw_id)
        if mod_id is None:
            skipped.append((str(raw_id), "编号不是纯数字"))
            continue

        # ---- 质量谓词：两道硬门，拦下的统一按"疑似下载中断"记 ----
        timeupdated = _to_int(block.get("timeupdated"))
        if timeupdated is None or timeupdated <= 0:
            # 没有有效的本地时间 = 这次下载没有正常完成
            # （steamcmd/客户端中断时账本里会留下 0）
            skipped.append(
                (str(raw_id),
                 f"{_INTERRUPTED_PREFIX}：timeupdated 缺失或为 0"))
            continue
        # 空串/缺失一律视为没有 manifest
        manifest = str(block["manifest"]) if block.get("manifest") else None
        if manifest is None or manifest == "-1":
            # "-1" 是下载中断留在账本里的标志值；缺失/空串同样说明
            # 内容没有完整落地。没有 manifest 就无法定位具体版本
            skipped.append(
                (str(raw_id),
                 f"{_INTERRUPTED_PREFIX}：manifest 缺失或为 -1"))
            continue

        # size 只影响展示（备份空间预检另有兜底），缺失/为 0 不拦，
        # 留到解析结束后聚合一条警告
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
            f"{len(no_size)} 个条目 size 缺失或为 0（不影响纳入，仅影响"
            f"大小展示）：{preview}")

    return ScanResult(acf_path=p, items=items,
                      skipped=skipped, warnings=warnings)
