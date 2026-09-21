"""steamcmd 目录布局推导
"""
r"""
从 steamcmd 程序路径推导 steamcmd 的标准目录布局
（决策 21 的代码落点：路径口径只在这一处定义）。

steamcmd 装好后，它自己管理的目录树长这样（实测结构）：

    <steamcmd根>\
      steamcmd.exe                     ← 设置页里填的"steamcmd 程序"
      steamapps\
        workshop\
          appworkshop_<appid>.acf      ← 工坊账本文件（localScanner 读它）
          content\
            <appid>\                   ← 工坊内容：每个游戏一个目录
              <mod编号>\               ← 里面只放编号命名的 mod 文件夹

mod 备份的位置（决策 21⑥）也在本模块推导：

    <steamcmd根 的上一级>\mod_backups\<appid>\
      与 content 同盘（robocopy 多线程快）；在 steamcmd 自管树之外
      （自更新不碰、重装不误删）；远离 Documents/OneDrive。

边界约定：
- 本模块只做路径推导和"目录是否存在"的核对（refresh_download_dir），
  不读写任何文件内容——"推导出路径"和"路径下真有东西"是两回事；
- steamcmd 程序路径是唯一的推导钥匙，它没填就什么都推不出来，
  此时返回 None，由调用方决定提示用户去设置页、还是降级手填。

为什么集中放在 core：设置、建档、本地扫描、备份引擎
都要按同一套公式找位置。公式写两遍，早晚改出不一致。
"""
from pathlib import Path


def steamcmd_root(steamcmd_path: str | None) -> Path | None:
    """steamcmd.exe 完整路径 → 其所在目录（steamcmd 根）。

    填了路径但文件暂不存在也照常返回目录：布局推导不依赖文件存在，
    用户可能正准备去下载 steamcmd，先把目录结构定下来。
    路径为空 / None → 返回 None（"没钥匙"，调用方自行提示）。
    """
    raw = str(steamcmd_path or "").strip().strip('"').strip()
    if not raw:
        return None
    return Path(raw).expanduser().parent


def workshop_content_dir(steamcmd_path: str | None, app_id: int) -> str | None:
    """游戏档案的下载目录：steamcmd 存放该游戏工坊内容的位置。

    公式（决策 21）： <steamcmd根>\\steamapps\\workshop\\content\\<appid>
    这棵树有一条铁律：只放以 mod 编号命名的文件夹——别的文件放进来，
    将来核验页做账实对比时会被当成异常内容报出来。

    steamcmd 程序路径没填 → 推导不出，返回 None。
    """
    root = steamcmd_root(steamcmd_path)
    if root is None:
        return None
    return str(root / "steamapps" / "workshop" / "content" / str(app_id))


def backup_root_default(steamcmd_path: str | None, app_id: int) -> str | None:
    """游戏档案的备份根目录默认值（决策 21⑥）。

    公式： <steamcmd根 的上一级>\\mod_backups\\<appid>\\
    上一级而不是 steamcmd 树内部：备份要放在 steamcmd 自更新、
    重装都不会碰到的地方；又不至于跑到另一个盘（同盘拷贝快）。

    steamcmd 程序路径没填 → 推导不出，返回 None。
    """
    root = steamcmd_root(steamcmd_path)
    if root is None:
        return None
    return str(root.parent / "mod_backups" / str(app_id))


def refresh_download_dir(
        stored_dir: str | None,
        steamcmd_path: str | None,
        app_id: int,
) -> tuple[str, bool]:
    """核对档案的下载目录是否还有效，失效则按当前 steamcmd 位置重新推导。

    背景（决策 21）：下载目录永远等于
    <steamcmd根>\\steamapps\\workshop\\content\\<appid>，
    但档案里存的是建档那一刻的推导值——steamcmd 之后挪了位置，
    存的值就变成指向旧位置的死路径。

    规则：
      - 存的值在盘上真实存在 → 原样保留（存在即健康，不二次猜疑；
        手填的自定义路径也因此天然豁免）
      - 存的值不存在/为空，且 steamcmd 程序路径可推导 → 用新推导值，
        changed=True 提示调用方写回档案
      - steamcmd 未配置、推不出新值 → 维持原值不动（changed=False），
        该提示的由调用方自己处理

    返回 (生效目录, 是否发生重推导)。
    """
    stored = str(stored_dir or "").strip().strip('"').strip()
    if stored and Path(stored).is_dir():
        return stored, False
    fresh = workshop_content_dir(steamcmd_path, app_id)
    if fresh is None:
        return stored, False
    return fresh, fresh != stored
