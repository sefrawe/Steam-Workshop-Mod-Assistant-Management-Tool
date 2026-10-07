"""游戏退场 · 盘点与处置引擎
"""
r"""workflows/gameExitFlow.py · 功能模块「游戏退场」的引擎（决策 96）。

只做两类事，其余都是别人的：
- 盘点（只读）：survey() 把"这个游戏退场要处理什么"一次查清——
  下载内容目录的存在性与联接形态、备份登记与盘上实况、steamcmd
  是否在跑。纯只读、毫秒级（几次路径 stat + 几条聚合 SQL），
  GUI 切档案时可以在主线程直接调；
- 处置（单一动作）：delete_content_dir() 删除档案的下载内容目录
  （标准形态 …\steamapps\workshop\content\<AppID>），四道红线见
  函数 docstring。

不做的（分工红线）：
- 备份处置：备份总览页的正门原样可用（逐份钉住/删登记/删文件），
  本引擎不重造；
- 账本清账：repo.delete_game_deep 与 gameDeleteDialog 现成（先盘点
  再删、数据库快照兜底），界面第⑤步原样转调；
- 游戏侧文件：指引不代劳（决策 69⒊ 纪律）——游戏 mod 目录、配置、
  存档由用户在资源管理器自行处置，本引擎只给路径与状态事实。

复用（决策 61④ 单源同思路）：目录测尺寸、树内联接扫描、字节
格式化取自 deleteManageFlow 的既有实现（跨模块 import 它的下划线
内部件是刻意的——复制一份反而制造两个会各自漂移的"真源"）；
R4 保险丝 safe_rmtree 与 steamcmd 在跑探测 steamcmd_running 来自
backupManager；联接判定与目录推导来自 steamPaths。这里只有编排
与"退场特有"的判定，没有新发明的检测。

执行模型（写给 GUI 工作线程）：delete_content_dir 是同步函数、
内部没有停止点（删除没有安全的停止，gameDeleteDialog 同一口径），
调用方放后台线程跑、跑动期间锁界面入口。

steamcmd 在跑（R7）：警告不拦截（用户拍板口径同 deleteManageFlow）
——被误删的 content 条目重跑下载命令即可回来；界面在确认弹窗里
把警告连同回程票一起说清楚，这就是全部的拦截。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from core import steamPaths
from core.backupManager import safe_rmtree, steamcmd_running
from core.models import Game
from core.modRepository import ModRepository
# 单源复用（见文件头说明）：这三个是 deleteManageFlow 已有且被
# 既有页面使用着的实现
from workflows.deleteManageFlow import _dir_size, _find_links, _fmt_size


def _is_link(path: str) -> bool:
    """路径本身是不是联接/junction/符号链接（只看它自己，不下钻）。
    与 deleteManageFlow._find_links 同一把尺。"""
    return os.path.islink(path) or os.path.isjunction(path)


@dataclass
class ExitSurvey:
    """退场盘点结果（只读快照）。界面卡片按它分派文案与可用性。"""
    app_id: int
    game_name: str
    download_dir: str            # 生效下载目录（核对/重推导之后）
    dir_rederived: bool          # 本次盘点是否发生了重推导（仅提示，不写回）
    content_exists: bool         # 下载目录在盘上存在
    content_is_link: bool        # 下载目录本身是联接（反向拓扑）
    junction_state: str          # （游戏mod目录, 下载目录）的十态判定
    junction_detail: str
    game_mod_dir: str            # 档案登记的游戏 mod 目录（可为空串）
    game_mod_dir_is_link: bool   # 游戏 mod 目录本身是联接（正向拓扑）
    mod_total: int               # 档案名下 mod 记录数（含各状态）
    backup_count: int            # 备份登记份数
    backup_bytes: int            # 登记合计字节
    backup_found: int            # 盘上真实找到的备份目录数
    backup_root: str | None      # 备份登记路径的解析根
    steamcmd_missing: bool       # 未配置 steamcmd（推导钥匙不在）
    steamcmd_running: bool       # R7：只警告不拦截


def survey(repo: ModRepository, game: Game,
           steamcmd_path: str | None) -> ExitSurvey:
    """只读盘点。毫秒级，主线程直调。
    档案不存在 → ValueError（game_deletion_summary 的既有口径）。"""
    summary = repo.game_deletion_summary(game.app_id)
    # 备份根解析（与 deleteManageFlow.inventory 同一口径：档案值优先，
    # 缺省按 steamcmd 位置推导）
    stored_root = str(game.backup_dir or "").strip()
    backup_root = stored_root or steamPaths.backup_root_default(
        steamcmd_path, game.app_id)
    backup_found = sum(
        1 for p in summary.backup_paths
        if backup_root and (Path(backup_root) / p).is_dir())
    # 下载目录核对（与扫描本地同款口径：存在即健康，失效且推得出
    # 新值才重推导）。退场场景的重推导结果只用于本次处置，不写回
    # 档案——档案马上就要删了，写回没有意义
    eff_dir, rederived = steamPaths.refresh_download_dir(
        game.download_dir, steamcmd_path, game.app_id)
    jr = steamPaths.junction_state(game.game_mod_dir, eff_dir)
    gm = str(game.game_mod_dir or "").strip()
    return ExitSurvey(
        app_id=game.app_id,
        game_name=game.name,
        download_dir=eff_dir or "",
        dir_rederived=rederived,
        content_exists=bool(eff_dir) and Path(eff_dir).exists(),
        content_is_link=bool(eff_dir) and _is_link(eff_dir),
        junction_state=jr.state,
        junction_detail=jr.detail,
        game_mod_dir=gm,
        game_mod_dir_is_link=bool(gm) and _is_link(gm),
        mod_total=summary.mod_total,
        backup_count=summary.backup_count,
        backup_bytes=summary.backup_bytes,
        backup_found=backup_found,
        backup_root=backup_root,
        steamcmd_missing=steamPaths.steamcmd_root(steamcmd_path) is None,
        steamcmd_running=steamcmd_running(),
    )


def delete_content_dir(download_dir: str, *, app_id: int) -> tuple[bool, str]:
    """删除档案的下载内容目录（退场第③步唯一的破坏性动作）。

    四道红线，任何一道不过就拒绝，并且什么都不会被删：
    ① 空路径/未配置 → 拒绝（先到编辑档案把下载目录改回推导值）；
    ② 目录名不是本游戏 AppID → 拒绝——标准形态的下载目录最后一段
       恰是 AppID；不是，说明档案指向的不是标准 content 目录，
       先人工核对再动手。宁可多问一句，不可错删一个；
    ③ 目录本身是联接（反向拓扑）→ 绝不递归删：真实内容在游戏侧，
       递归删除会穿透联接删掉游戏目录的真身（决策 21④ 方向铁律）。
       正确做法是 rmdir 摘链（界面第③④步给出命令）；
    ④ 目录树里发现任何联接/链接 → 拒绝整删（拓扑不是纯内容，先到
       「游戏 → 连接指引」理清）；R4 保险丝 safe_rmtree 再兜最后一道
       （目标必须严格落在其父目录之内）。

    steamcmd 在跑不拦（R7 用户拍板）：误删可重下。
    全部检查通过才先量尺寸（进返回详情），再删。
    返回 (是否完成, 人话详情)；操作层面失败返回 ok=False 不抛异常
    （deleteManageFlow.execute_action 同一口径）。
    """
    text = str(download_dir or "").strip().strip('"').strip()
    if not text:
        return False, ("档案没有有效的下载目录——先到「游戏 → 编辑档案」"
                       "点【改回推导值】核对，再回来删除")
    p = Path(text)
    if not p.exists():
        return True, "盘上本已不存在（本步无需处理）"
    if p.name != str(app_id):
        return False, ("下载目录最后一段不是本游戏的 AppID（标准形态是 "
                       "…\\content\\294100 这样的纯数字目录名）——请先到"
                       "「游戏 → 编辑档案」用【改回推导值】核对，再回来删除")
    if _is_link(text):
        return False, ("下载目录本身是目录联接（反向拓扑）——真实内容在"
                       "游戏 mod 目录侧，这里绝不能递归删除；请用第③步"
                       "给出的 rmdir 命令把联接摘掉（只摘链，不碰真实文件）")
    links = _find_links(p)
    if links:
        shown = "、".join(str(x) for x in links[:3])
        return False, ("目录树中发现联接/链接，拒绝整目录删除：" + shown +
                       "——先到「游戏 → 连接指引」理清拓扑，再回来删除")
    size = _dir_size(p)
    try:
        safe_rmtree(p, p.parent)
    except (RuntimeError, OSError) as exc:
        return False, f"删除失败（内容原样保留）：{exc}"
    return True, (f"已删除（约 {_fmt_size(size)}）。想反悔：重新下载即可"
                  "把内容找回来")
