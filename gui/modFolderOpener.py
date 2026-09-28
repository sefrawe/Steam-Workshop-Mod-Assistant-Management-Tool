"""打开 mod 下载文件夹（mod 库页 / 更新对照页共用单源）"""
r"""gui/modFolderOpener.py · 「打开 mod 文件夹」的唯一实现。
此前 modListPage 与 updateComparePage 各有一份镜像实现，本模块
合并为一份——路径推导、失败兜底、提示文案从此只有一处口径。

路径两路候选：local_path（历史预留字段，现无任何链路写入、恒为
空，保留兼容）→ 档案 download_dir + 编号（实际唯一机制：steamcmd
下载位置恒定，acf 本身不记录每条路径）。

软删除条目照样可开——软删除不删文件，盘上多半还在。
log 可传 gui.consolePanel.LogBus（mod 库页传，成功/失败各记一行）；
不传则只弹窗不记日志。
"""
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox


def open_mod_folder(parent, game, mod, log=None) -> bool:
    """在文件管理器打开该 mod 的下载内容文件夹。返回是否打开成功。
    失败时已就地弹窗（与写日志），调用方无需重复提示。"""
    candidates: list[str] = []
    if mod.local_path:
        candidates.append(mod.local_path)
    if game is not None and game.download_dir:
        candidates.append(str(Path(game.download_dir) / str(mod.mod_id)))
    for path in candidates:
        if Path(path).is_dir():
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                # openUrl 失败是静默的（v2.18 教训）：手动兜底提示
                QMessageBox.warning(
                    parent, "打开文件夹",
                    f"文件管理器没有响应，请手动打开：\n{path}")
                return False
            if log is not None:
                log.info(f"已打开 mod {mod.mod_id} 的文件夹：{path}")
            return True
    where = candidates[0] if candidates else "没有可推算的本地路径"
    if log is not None:
        log.warn(f"mod {mod.mod_id} 的文件夹不存在：{where}")
    QMessageBox.information(
        parent, "打开 mod 文件夹",
        f"盘上没有找到 mod {mod.mod_id} 的文件夹：\n{where}\n\n"
        "可能原因：还没下载过（已收录）、已被清理，或下载目录变动过"
        "（后者可在设置页核对 steamcmd 路径后点【扫描本地】回填）。")
    return False
