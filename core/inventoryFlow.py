"""core/inventoryFlow.py · 只读盘点引擎（待认领区轮新写件）。

一轮 scan_game 全做完，全程只读磁盘；写入只走两扇既有正门
（record_verdicts 落候选提案 / backfill_local_sizes 回填展示列），
本引擎不发明任何写库路径（R17）。

1. 找候选：mod 目录（game_mod_dir 优先，没设退 download_dir）下
   整数命名的子目录 = 盘上有的 mod。对账本分类：
   - 已彻底入账（confirmed_version 非空）→ 非候选，只回填大小；
   - 已删除 → 跳过（文件回来了先去库页「恢复」，不在这里暗翻状态）；
   - 黑名单 → 跳过（D11 同款拦截）；
   - 已有未确认 claim 行 → 跳过（幂等：提案不重复落）；
   - 其余（账上没有 / tracked / failed / 版本未知）→ 落 claim 判决行，
     version_written = acf 盘面版本（读不到 = NULL，认领成版本未知）。
   候选走 pending 队列（M0 追记⑥）：待认领区读 verdict_log 里未确认
   的 claim 行，确认走 confirm_items 唯一正门。
2. 清失效：上轮扫描留下的未确认 claim 行，这轮盘上文件夹没了 →
   drop_stale_claims 清掉（提案跟随盘面：文件夹没了提案作废；
   确认行绝不动）。
3. 回填大小：每个整数目录实测占用 → backfill_local_sizes（展示列，
   R19 禁入判定；账上没有的编号 repo 自会忽略）。

没有 acf 也继续：手动拷进来的 mod 没有 acf 记录，恰恰是最需要
认领的一批——照落候选，版本留 NULL。

线程与 repo：算目录大小可能很慢，调用方放 QThread 跑（BackupManager
先例：账本连接允许跨线程、全部即时提交）。本文件零 Qt。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from core import acfParser

_INT_DIR = re.compile(r"^\d+$")


@dataclass
class ScanReport:
    """一轮盘点的产出口径（日志与人读）。"""
    mod_dir: str = ""                  # 实际扫描的目录
    folders_found: int = 0             # 盘上整数目录总数
    candidates_new: list[int] = field(default_factory=list)  # 新落候选
    candidates_dup: int = 0            # 已有候选，未重复落
    stale_removed: list[int] = field(default_factory=list)   # 失效候选清除
    skipped_accounted: int = 0         # 已入账（只回填大小）
    skipped_other: int = 0             # 已删除 / 黑名单
    versions_from_acf: int = 0         # acf 带出盘面版本的候选数
    sizes_backfilled: int = 0
    acf_note: str = ""                 # acf 读取情况（找不到/读坏/正常）


def scan_game(repo, game, *, steamcmd_path: str | None = None) -> ScanReport:
    """盘点一个档案。目录不存在等环境问题记进报告返回，不抛——
    抛只抛真异常（权限等 OSError 由调用方兜）。"""
    base = _pick_mod_dir(game)
    if base is None:
        return ScanReport(acf_note="档案没设 mod 目录/下载目录，无从盘点")
    if not base.is_dir():
        # 不清提案：目录看不见 ≠ 盘面没了（移动盘没挂载、网络盘掉线
        # 都长这样）——提案跟随"看清楚的盘面"，看不清就不动
        return ScanReport(mod_dir=str(base),
                          acf_note="目录不存在（先到档案设置里确认路径）")

    report = ScanReport(mod_dir=str(base))

    # ---- 盘面事实：整数命名的子目录 = mod ----
    folders: dict[int, Path] = {}
    for child in sorted(base.iterdir()):
        if child.is_dir() and _INT_DIR.match(child.name):
            folders[int(child.name)] = child
    report.folders_found = len(folders)
    if not folders:
        # 目录在、只是空：盘面确定没有这些 mod 了——未确认候选全部
        # 作废（提案跟随盘面；"删光文件夹再扫自动清"的兑现点）。
        # 已确认行与其他 kind 本就不在队列，天然不受影响
        pending = {v.mod_id
                   for v in repo.pending_confirmations(game.app_id)
                   if v.kind == "claim"}
        if pending:
            report.stale_removed = sorted(pending)
            repo.drop_stale_claims(game.app_id, pending)
        report.acf_note = "目录里没有整数命名的 mod 文件夹"
        return report

    versions = _read_versions(game.app_id, base, steamcmd_path, report)

    # ---- 现状一次拿齐（读侧）----
    pending_claims = {v.mod_id for v in repo.pending_confirmations(game.app_id)
                      if v.kind == "claim"}
    purged = repo.filter_purged(folders.keys())

    sizes: dict[int, int] = {}
    new_rows: list[dict] = []
    for mid, folder in folders.items():
        sizes[mid] = _dir_size(folder)
        mod = repo.get_mod(mid)
        if mod is not None and mod.confirmed_version is not None:
            report.skipped_accounted += 1        # 已彻底入账
            continue
        if mod is not None and mod.status == "deleted":
            report.skipped_other += 1            # 删除态：走恢复，不暗翻
            continue
        if mid in purged:
            report.skipped_other += 1            # 黑名单（D11 同款）
            continue
        if mid in pending_claims:
            report.candidates_dup += 1           # 幂等：提案已在队列
            continue
        ver = versions.get(mid)
        new_rows.append({
            "mod_id": mid, "kind": "claim", "game_id": game.app_id,
            "version_written": ver, "source": "claim",
            "title": None, "file_size": sizes[mid],
        })
        if ver is not None:
            report.versions_from_acf += 1
    if new_rows:
        repo.record_verdicts(new_rows)
        report.candidates_new = [r["mod_id"] for r in new_rows]

    # ---- 失效候选：提案跟随盘面 ----
    stale = sorted(pending_claims - folders.keys())
    if stale:
        report.stale_removed = stale
        repo.drop_stale_claims(game.app_id, stale)

    # ---- 大小回填（展示列；账上没有的编号 repo 自会忽略）----
    report.sizes_backfilled = repo.backfill_local_sizes(sizes)
    return report


def _pick_mod_dir(game) -> Path | None:
    """扫描落点：mod 目录优先（游戏实际读 mod 的地方），没设退
    下载目录（steamcmd 放内容的地方）。两个都没有 = 没得盘。"""
    for raw in (game.game_mod_dir, game.download_dir):
        s = str(raw or "").strip()
        if s:
            return Path(s)
    return None


def _read_versions(app_id: int, mod_dir: Path,
                   steamcmd_path: str | None, report: ScanReport
                   ) -> dict[int, int]:
    """找 appworkshop_<appid>.acf 并提取盘面版本表。找的顺序：
    下载目录上两级（…/steamapps/workshop/content/<appid> 标准布局
    → workshop 就在上两级）；steamcmd 根下的 steamapps/workshop
    （force_install_dir 没指向预期位置时的兜底）。都找不到/读坏
    → 空表 + 说明（盘点照做，候选版本留 NULL）。"""
    paths: list[Path] = [mod_dir.parent.parent
                         / f"appworkshop_{app_id}.acf"]
    s = str(steamcmd_path or "").strip()
    if s:
        paths.append(Path(s) / "steamapps" / "workshop"
                     / f"appworkshop_{app_id}.acf")
    for p in paths:
        if not p.is_file():
            continue
        try:
            table = acfParser.read_workshop_versions(p)
        except (OSError, ValueError) as exc:
            report.acf_note = f"acf 读取失败（{p}）：{exc}"
            return {}
        report.acf_note = f"acf 正常：{len(table)} 条盘面版本"
        return table
    report.acf_note = ("没找到 appworkshop acf——手动拷入的 mod 没有"
                       "acf 记录，照常盘点，候选按版本未知落")
    return {}


def _dir_size(folder: Path) -> int:
    total = 0
    for f in folder.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue          # 个别文件读不动不挡整盘
    return total
