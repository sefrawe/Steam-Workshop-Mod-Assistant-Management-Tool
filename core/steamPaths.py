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
 - 本模块做三件事：路径推导、"目录是否存在"的核对（refresh_download_dir）、
   联接状态判定（junction_state）；只读路径元数据（存在性、是否链接、
   目录是否为空），不读写任何文件内容——"推导出路径"和"路径下真有东西"
   是两回事；

- steamcmd 程序路径是唯一的推导钥匙，它没填就什么都推不出来，
  此时返回 None，由调用方决定提示用户去设置页、还是降级手填。

为什么集中放在 core：设置、建档、本地扫描、备份引擎
都要按同一套公式找位置。公式写两遍，早晚改出不一致。
"""
import os
from pathlib import Path
from typing import NamedTuple


import vdf  # ValvePython：解析 libraryfolders.vdf（读 acf 的同一份依赖，零新增）

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

def ensure_steamcmd_exe(path: str) -> str:
    """T21①：把"文件夹"形态的 steamcmd 路径补全成完整 exe 路径。
    """
    """设置页允许直接填"包含 steamcmd.exe 的文件夹"（用户已拍板），
    本函数是唯一补全规则落点，三条规则：

    - 传入的是存在的文件 → 原样返回（正常情况，设置页一直这么存）
    - 传入的是存在的文件夹且里面有 steamcmd.exe → 返回 <文件夹>/steamcmd.exe
    - 其余（空串、不存在的路径、没有 exe 的文件夹）→ 原样返回——
      让上游的存在性检查报出真实情况（"文件夹里没有 steamcmd.exe"），
      好过悄悄拼出一个不存在的路径把问题藏起来

    Windows 文件名不区分大小写，steamcmd.EXE / SteamCmd.exe 同样能认出。
    """
    text = str(path or "").strip()
    if not text:
        return ""
    p = Path(text)
    if p.is_file():
        return str(p)
    if p.is_dir():
        candidate = p / "steamcmd.exe"
        if candidate.is_file():
            return str(candidate)
    return text
class JunctionReport(NamedTuple):
    """联接状态判定结果（junction_state 的返回值）。

    state 十种取值（连接指引对话框按它分派文案和命令）：
    - no_target:              实体侧（下载目录）为空，无从判定
    - missing:                游戏 mod 目录不存在（含上级不存在），且下载目录不是联接
    - file:                   游戏 mod 目录被一个普通文件占用
    - linked:                 正向已连接：游戏 mod 目录是联接，且正确指向下载目录
    - wrong_target:           正向指错：游戏 mod 目录是联接，但指向了别处（悬空也算）
    - linked_reverse:         反向已连接：下载目录本身是联接，指向游戏 mod 目录
    - linked_reverse_missing: 反向接了一半：下载目录联接指向的游戏 mod 目录不存在
    - wrong_target_reverse:   下载目录是联接，但指向别处（或游戏侧形态异常）
    - empty_dir:              两侧都不是联接；游戏 mod 目录是真实空目录——建链前需先移除
    - real_dir:               两侧都不是联接；游戏 mod 目录是真实目录且有内容——需先搬运再建链

    detail：linked / wrong_target 时为游戏侧联接的解析路径；
    linked_reverse* / wrong_target_reverse 时为下载目录联接的解析路径；其余为 ""。
    """
    state: str
    detail: str


def junction_state(link_path: str | None, target_dir: str | None) -> JunctionReport:
    """T13：判定"游戏 mod 目录 ↔ 下载目录"这对路径处于哪种联接状态。
    只读判定，不做任何修改。

    背景见连接指引对话框（gui/linkGuideDialog.py）：部分游戏只从自己的
    目录读 mod，需要用 junction 把它和 steamcmd 的工坊内容目录接通。
    本函数是对话框唯一的判定依据；命令生成、文案分派都在对话框侧。

    支持两种拓扑（v2：实测用户环境存在反向拓扑后加入）：
    - 正向：游戏 mod 目录是联接 → 指向下载目录（指引推荐的建法）
    - 反向：下载目录本身是联接 → 指向游戏 mod 目录——steamcmd 写入时
      经联接落到游戏目录，游戏直接读真实目录，同样成立

    判定顺序：
    1. 任一侧为空 → no_target / missing（防御口径，对话框不会在空输入
       时调到这里）
    2. 游戏 mod 目录是链接（os.readlink 成功）：两侧 realpath 归一化后
       casefold 比较（R14），相等 → linked，不等 → wrong_target。悬空
       链接同样能解析、能比较，不会崩
    3. 游戏 mod 目录不是链接 → 先查反向拓扑（下载目录是否是链接），
       再退回普通形态判定：
       - 下载目录是链接且解析结果 == 游戏 mod 目录 → 已按反向拓扑接通：
         游戏目录真实存在 → linked_reverse；不存在（悬空）→
         linked_reverse_missing；解析到别处 → wrong_target_reverse
       - 下载目录不是链接 → 不存在 → missing；是文件 → file；
         是目录 → 空则 empty_dir、有内容则 real_dir

    反向检查必须排在"建议正向建链"之前，两个原因：
    一是把"已接通"误报成"需搬运"（v1 恰好在用户环境犯了这个错，
    而且照做等于对着同一份数据自己搬自己）；二是游戏目录不存在、
    下载目录却是联接时，若仍建议 mklink 游戏→下载，会造出
    联接→联接 的环，NTFS 会照单全收，之后遍历就是无底洞。

    两侧都先 strip 引号/空白：从资源管理器地址栏复制的路径经常带引号
    或尾随空格，不能让格式问题污染判定。
    """
    link = str(link_path or "").strip().strip('"').strip()
    target = str(target_dir or "").strip().strip('"').strip()
    if not target:
        return JunctionReport("no_target", "")
    if not link:
        return JunctionReport("missing", "")

    # realpath 会一路穿透联接解析到最终实体，且不要求路径存在——
    # 悬空联接也照样解析出目标路径字符串
    resolved_target = os.path.realpath(target)
    p = Path(link)  # Path 会归一化尾随分隔符——readlink 对 "C:\\dir\\" 会失败

    # 第一优先：正向拓扑——游戏 mod 目录本身是链接
    try:
        os.readlink(str(p))
    except OSError:
        pass  # 不是链接，落入下面的反向 / 普通形态判定
    else:
        resolved_link = os.path.realpath(str(p))
        if resolved_link.casefold() == resolved_target.casefold():
            return JunctionReport("linked", resolved_link)
        return JunctionReport("wrong_target", resolved_link)

    # 第二优先：反向拓扑——下载目录本身是链接
    try:
        os.readlink(target)
    except OSError:
        pass
    else:
        if resolved_target.casefold() == os.path.realpath(str(p)).casefold():
            if p.is_dir():
                return JunctionReport("linked_reverse", resolved_target)
            if not p.exists():
                return JunctionReport("linked_reverse_missing", resolved_target)
            return JunctionReport("wrong_target_reverse", resolved_target)
        return JunctionReport("wrong_target_reverse", resolved_target)

    # 两侧都不是链接：按游戏 mod 目录的普通形态判
    if not p.exists():
        return JunctionReport("missing", "")
    if p.is_file():
        return JunctionReport("file", "")
    if p.is_dir():
        empty = next(p.iterdir(), None) is None
        return JunctionReport("empty_dir" if empty else "real_dir", "")
    return JunctionReport("file", "")  # 极端形态（设备等），按占用处理
def _steam_install_dir() -> str | None:
    r"""读注册表拿 Steam 客户端安装目录
    （HKCU\Software\Valve\Steam 的 SteamPath 值）。
    非 Windows / 没装 Steam / 读失败 → None（调用方降级手填）。"""
    try:
        import winreg
    except ImportError:  # 非 Windows 平台：项目不支持（Won't），空手而归
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as key:
            value, _type = winreg.QueryValueEx(key, "SteamPath")
    except OSError:
        return None
    text = str(value or "").strip()
    return text or None


def _read_library_vdf(steam_dir: Path) -> list[str]:
    r"""解析 <Steam目录>\steamapps\libraryfolders.vdf，返回登记的库路径。
    新版结构 {"libraryfolders": {"0": {"path": ...}, ...}}；老版本键的值
    直接就是路径字符串——两种都认。文件缺失/损坏/结构不认识 → 返回 []，
    调用方退回"只认安装主目录"，绝不抛错（探测是锦上添花，不能因它报错）。
    utf-8-sig：带 BOM 也能读（addMod 读文件 BOM 修复的同款先例）。"""
    vdf_path = steam_dir / "steamapps" / "libraryfolders.vdf"
    try:
        with open(vdf_path, "r", encoding="utf-8-sig", errors="replace") as fh:
            data = vdf.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    folders = data.get("libraryfolders")
    if not isinstance(folders, dict):
        return []
    out: list[str] = []
    for entry in folders.values():
        path_value = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(path_value, str) and path_value.strip():
            out.append(path_value.strip())
    return out


def client_library_roots(*, install_dir: str | None = None) -> list[str]:
    r"""Steam 客户端库根目录候选清单（首次使用向导第④步自动探测用）。
    来源两路合并：① Steam 安装主目录（注册表，或测试注入的 install_dir）；
    ② 主目录 steamapps\libraryfolders.vdf 里登记的其余库
    （用户把游戏装到 D:\ 等其他盘时登记在此）。
    边界约定：
    - 只读：注册表只读一个值、vdf 只读一个文件，绝不写、绝不动 Steam
      客户端的任何东西——与决策 21 单源架构不冲突：这里只为"读客户端
      订阅记录"这一个入口找门牌，不把客户端当数据源；
    - 去重按 normcase+normpath（R14 的 Windows 规范化形态：大小写与
      正反斜杠都不敏感），只保留盘上真实存在的目录；
    - 全部失败 → 返回 []：调用方维持手填能力（探测是预填便利，不是前提）。
    install_dir：Steam 安装目录注入点（pytest 合成样本用）；
    None = 生产路径，走注册表探测。
    """
    primary = (str(install_dir).strip() if install_dir is not None
               else _steam_install_dir())
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
        candidates.extend(_read_library_vdf(Path(primary)))
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        # 出口前归一化：注册表 SteamPath 与 vdf 里正反斜杠都可能出现
        # （注册表实测就是正斜杠），一律 normpath 成 Windows 标准形态
        norm = os.path.normpath(cand)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        if Path(norm).is_dir():
            out.append(norm)
    return out

