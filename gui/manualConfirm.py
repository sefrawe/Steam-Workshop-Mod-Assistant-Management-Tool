"""手动确认入账"""
""" 「确认已下载（手动）」的共享实现：mod 库页右键与核验页"账未记"
桶共用，单条/批量一份文案，防止两个页面的话说不一致。

背景：一部分 mod 从不经 steamcmd 下载（早期手动下载的旧收藏），
acf 文件里永远不会有它们的记录，「扫描本地」无论如何扫不到，
只能由用户看过核验页展示的磁盘实况后，亲口确认"它在盘上"。

诚实边界（改这段代码时必须守住）：
- 只写 status 一个字段；版本三件套一律留空——版本事实只来自 acf，
  手动确认不产生版本事实，也绝不编造（不写 0、不写假 manifest）
- 动作前重读数据库核对当前状态（不信任调用方递来的旧数据）；
  仅 tracked → downloaded 单向（决策 20），deleted / failed 不开放
- 弹窗必须讲清确认的后果（备份将拒 / 检测列「版本未知」/
  重下可恢复）——决策 22：变更不许静默
- operations_log 表不记录（没有命令语义，硬塞会污染日志表）；
  反馈走 LogBus 当场告知
"""
from PySide6.QtWidgets import QMessageBox, QWidget

from gui.consolePanel import LogBus
from gui.formatters import status_zh


def _consequences(count: int) -> str:
    """确认后果说明，单条/批量共用同一份文字。"""
    whom = "该 mod" if count == 1 else f"这 {count} 个 mod"
    return (
        f"确认后（{whom}的版本三件套留空）：\n"
        "· 备份功能将拒绝——本地版本未知，无法确定备份版本；\n"
        "· 更新检测无法判定新旧，结果里单独列为「版本未知」；\n"
        "· 以后若用 steamcmd 重新下载并点【扫描本地】，"
        "版本自动回填，以上功能随之恢复。")


def confirm_one(parent: QWidget, repo, log: LogBus, mod_id: int) -> bool:
    """确认单个 mod 已下载（手动）。返回是否确认成功，
    供调用方决定要不要刷新界面。"""
    fresh = repo.get_mod(mod_id)
    if fresh is None:
        return False
    if fresh.status != "tracked":
        QMessageBox.information(
            parent, "确认已下载",
            f"mod {mod_id} 当前状态是「{status_zh(fresh.status)}」，"
            "只有「已收录」的 mod 才需要手动确认。")
        return False

    title = fresh.title or "（无标题）"
    ret = QMessageBox.question(
        parent, "确认已下载（手动）",
        f"「{title}」（mod {mod_id}）将直接标记为「已下载」。\n\n"
        "适用场景：早期手动下载、不经 steamcmd——acf 永远没有它的记录，"
        "「扫描本地」无法自动确认。\n\n" + _consequences(1))
    if ret != QMessageBox.StandardButton.Yes:
        log.info(f"已取消手动确认：mod {mod_id}")
        return False

    repo.update_status(mod_id, "downloaded")
    log.ok(f"mod {mod_id} 已手动确认下载（版本未知：备份将拒、"
           "更新检测列「版本未知」；重下并扫描本地可恢复）")
    return True


def confirm_batch(parent: QWidget, repo, log: LogBus,
                  mod_ids: list[int]) -> int:
    """批量确认（核验页"全部确认"按钮）。返回确认成功的个数。

    非 tracked 的编号跳过（弹窗里说明跳了几个）——批量场景里
    混着几种状态是常态，不拦不炸。整批包在一个事务里：
    中途任何一步失败，全部回滚，不会确认了一半。
    """
    eligible = []
    for mid in mod_ids:
        m = repo.get_mod(mid)
        if m is not None and m.status == "tracked":
            eligible.append(m)
    if not eligible:
        QMessageBox.information(
            parent, "全部确认已下载",
            "没有可确认的条目（所选编号都不在「已收录」状态）。")
        return 0

    preview = "\n".join(
        f"{m.mod_id}  {m.title or '（无标题）'}" for m in eligible[:8])
    if len(eligible) > 8:
        preview += f"\n… 等共 {len(eligible)} 个"
    skipped = len(mod_ids) - len(eligible)
    skip_note = (f"\n（其余 {skipped} 个不是「已收录」，已跳过）"
                 if skipped > 0 else "")

    ret = QMessageBox.question(
        parent, "全部确认已下载（手动）",
        f"将把 {len(eligible)} 个「已收录」的 mod 标记为「已下载」：\n"
        f"{preview}{skip_note}\n\n"
        "适用场景：早期手动下载、不经 steamcmd——acf 永远没有它们的记录，"
        "「扫描本地」无法自动确认。\n\n" + _consequences(len(eligible)))
    if ret != QMessageBox.StandardButton.Yes:
        log.info(f"已取消批量手动确认（{len(eligible)} 个）")
        return 0

    with repo.transaction():
        for m in eligible:
            repo.update_status(m.mod_id, "downloaded")
    log.ok(f"批量手动确认完成：{len(eligible)} 个 mod 已记为「已下载」"
           "（版本未知；重下并扫描本地可恢复）")
    return len(eligible)
