"""打开 mod 文件夹
"""
"""
gui/modFolderOpener.py · 「打开 mod 文件夹」动作的唯一实现。
mod 库右键、详情面板、将来批次卡片的行右键都调这一个函数——
打开行为只有一份，提示口径才不会漂移。

V2 口径：mod 内容文件夹的位置恒为 下载目录\\<编号>（steamcmd 布局，
账本不存本地路径——steamcmd 挪了位置也不会留下失效旧路径）。
目录不在盘上 = 还没下载、已被清理或已删除，如实提示，绝不猜路径。
纯读操作：只打开资源管理器，不写任何数据。
"""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from gui.logBus import LogBus


def open_mod_folder(parent: QWidget, game, mod, *,
                    log: LogBus | None = None) -> None:
    """打开某 mod 的下载内容文件夹（资源管理器）。

    game：Game（要用它的 download_dir）；mod：Mod，或只带 mod_id
    属性的兜底对象（批次条目可能不在账本，调用方会喂两字段假对象，
    本函数只摸 mod_id——契约就这么宽，别多用）。
    log 不传也能工作：弹窗照出，只是少一条运行日志。
    """
    download_dir = str(getattr(game, "download_dir", "") or "").strip()
    if not download_dir:
        _say(parent, log, "warn",
             "这个档案还没有设置下载目录：请到「游戏 → 编辑档案」检查。")
        return
    folder = Path(download_dir) / str(mod.mod_id)
    if not folder.is_dir():
        _say(parent, log, "info",
             f"下载目录里没有这个 mod 的文件夹：\n{folder}\n\n"
             "可能还没下载、已被清理，或该条目处于已删除状态。")
        return
    if QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
        if log is not None:
            log.info(f"已打开 mod 文件夹：{folder}")
    else:
        # openUrl 失败是静默的（v2.18 教训）：手动兜底提示
        _say(parent, log, "warn",
             f"文件管理器没有响应，请手动打开：\n{folder}")


def _say(parent: QWidget, log: LogBus | None,
         level: str, text: str) -> None:
    """提示双通道：弹窗给人看 + 运行日志留痕（log 未注入就只弹窗）。"""
    if log is not None:
        getattr(log, level)(text.splitlines()[0])  # 日志只留第一行摘要
    QMessageBox.information(parent, "打开 mod 文件夹", text)
