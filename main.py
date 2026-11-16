"""程序入口
"""
r"""main.py —— 启动顺序：建 app + 存储坐标 → 图标 → 单实例锁 → 写路径
守卫 → 会话落点 → 主题 → 语言 → 主窗口。

v8 说明（守卫弹窗极简化：只给「以管理员身份运行」一条路）：
时间线实证收口：旧版 exe 当天运行一切正常，新版打包出来当天就被拦
——「长期不用信誉衰减」假说出局，「信任按文件指纹（哈希）记录」
成为唯一自洽解释：同一个文件夹、同一个用户、同一天，老文件有信誉、
新文件从零开始。无签名发布线下代码层能做的只剩一件事：把这个事实
直接变成产品行为——守卫失败弹窗只保留探测失败的原始信息 + 一个
「以管理员身份重启」按钮（ShellExecuteW runas → UAC 确认 → 新进程
提权启动 → 本进程先释放互斥体再退出让位）。此前版本的多步排查指引
（白名单步骤等）全部删去：程序不替用户猜病因，只给最短出路。
v5 的命名互斥体单实例锁 + 文件锁兜底链维持不变。

v5 说明（单实例锁从"文件锁"换成"命名互斥体"，根治杀软拦截）：
前两轮的教训链：打包版 exe 在系统 Temp 目录创建锁文件被杀软静默拦截
（"无签名 exe 往 Temp 写文件"正是启发式盯防的典型行为；源码版用的是
python.exe，不受影响）→ v3 加了 data/ 备用锁位，实测可用但本质仍是
"换个地方写文件"捉迷藏 → v5 换机制：Windows 上改用 Win32 命名互斥体，
一个文件都不碰，杀软无从拦起。

命名互斥体是什么：操作系统内核里的一个"有名字的计数对象"。第一个
创建它的进程独占持有；第二个进程用同一个名字去创建，系统会直接告诉
它"已经有人了"。它是 Windows 单实例的标准做法（大量正规软件都用它）。

它为什么全面优于文件锁：
· 不落盘 → 杀软的文件拦截对它无效（本轮问题的结构性消灭）；
· 进程死 → 内核自动释放 → 永远没有"残留锁"，不需要用户去删文件；
· 判定准 → "已存在"是内核给出的铁证，QLockFile 时代的误报
  （进程号被系统复用、建锁被拦误报为被占用）从机制上消失。

兜底链（互斥体只在 Windows 有意义，且要防 API 意外）：
1) Windows 首选：命名互斥体（本文件主体逻辑）；
2) 互斥体不可用（非 Windows 系统 / API 意外）→ QLockFile 文件锁：
   Windows 落在软件文件夹 data/ 内（自有地盘，杀软盯防低，v3 实测
   可用），非 Windows（Linux 移植线）落在系统临时目录（无此干扰）；
3) 文件锁也失败 → 建文件探针诊断 + 弹窗（v3/v4 的机制，保留）。

单实例锁（R12）语义不变：同一登录会话内只允许一个实例；互斥体名字
复用已冻结的 AppUserModelID 词干，V1/V2 天然各用各的锁。注意：源码
版与打包版同代 V2，共用同一把锁——两个同时开，后开的弹"程序已在
运行"，设计如此，不是故障。

仍然存在的、代码层无解的一件事：每次重新打包后，头几次运行可能弹
SmartScreen"无法验证发布者"（对 exe 文件本身的信任，按文件指纹记录，
重打包即清零）。这只影响"多点一次仍要运行"，不再影响功能。根治
唯一途径是代码签名证书（后续可选）。

T17 写路径守卫：必须赶在 AppSettings / 数据库之前，探不过弹窗即退，
不静默挪数据（数据位置可预期是便携模式的命根子）。
"""
import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QDir, QLockFile, QTimer, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from core import appPaths
from core.i18n import t

# ==================== 单实例锁（R12 / v5） ====================

# ---- 首选锁位（仅 Windows）：命名互斥体 ----
# 名字复用已冻结的 AppUserModelID 词干（改 = 换应用，与任务栏身份
# 同生共死）。「Local\」前缀 = 按登录会话隔离：同一 Windows 账户内
# 单实例；不同账户各自一个实例（与原 Temp 文件锁同语义，且不需要
# 任何特权）。注意 Python 字符串里反斜杠要写成 \\。
_MUTEX_NAME = "Local\\sefrawe.SteamWorkshopModAssistantV2.SingleInstance"

# ---- 备用锁位一（Windows / 互斥体意外时）：软件文件夹 data/ 内 ----
# v3 引入、v4 实测可用。互斥体是首选，这条只是兜底的兜底。
try:
    _APP_DIR = Path(sys.argv[0]).resolve().parent
except Exception:
    _APP_DIR = Path.cwd()
_FALLBACK_LOCK_PATH = str(_APP_DIR / "data" / "single-instance.fallback.lock")

# ---- 备用锁位二（非 Windows）：系统临时目录 ----
# Linux 等系统没有杀软拦 Temp 的生态问题，沿用原 Temp 方案。
_TEMP_LOCK_PATH = QDir.temp().absoluteFilePath(
    "SteamWorkshopModAssistantV2.single-instance.lock")

# 文件锁对象的"保活"名单：QLockFile 对象被回收 = 锁被释放，
# 所有抢到的锁都要塞进这里活到进程退出
_LOCK_KEEPALIVE: list = []

# kernel32 只在 Windows 上准备；use_last_error=True 让 ctypes 替我们
# 抓住 API 调用后的系统错误码（直接调 kernel32.GetLastError 会被
# ctypes 内部调用冲掉，是常见的坑）
# kernel32/advapi32 只在 Windows 上准备；use_last_error=True 让 ctypes
# 替我们抓住 API 调用后的系统错误码（直接调 kernel32.GetLastError 会被
# ctypes 内部调用冲掉，是常见的坑）
_kernel32 = None
_advapi32 = None
if sys.platform == "win32":
    from ctypes import wintypes

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE          # 返回句柄
    _kernel32.CreateMutexW.argtypes = [
        ctypes.POINTER(_SECURITY_ATTRIBUTES),  # 安全属性（v10 带标签）
        wintypes.BOOL,      # 是否立刻要求所有权（要，True）
        wintypes.LPCWSTR,   # 互斥体名字（Unicode 版本）
    ]
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype \
        = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,                  # SDDL 串
        wintypes.DWORD,                    # revision（恒为 1）
        ctypes.POINTER(ctypes.c_void_p),   # 收安全描述符指针
        ctypes.POINTER(wintypes.ULONG),    # 收长度（不要）
    ]
# 互斥体的安全描述符（SDDL，v10.2 拆成两档）：DACL 相同（Everyone 完全
# 访问），差别在完整性标签——
# · 带标签版：给管理员实例用。其令牌默认标签是 High，不显式压到 Medium，
#   普通权限的第二实例打不开它（完整性检查在 DACL 之前）。贴标签需要
#   SeCreateIntegrityLabelPrivilege 特权，普通用户没有（实测 1314）；
# · 无标签版：普通实例用。令牌默认标签就是 Medium，创建出的对象天然
#   就是"Medium + 禁止向上写"，绕开特权要求。
# Local\ 前缀仍按登录会话隔离，跨账户互不可见，作用域没有放大。
_SDDL_MUTEX_LABELED = "D:(A;;GA;;;WD)S:(ML;;NW;;;ME)"
_SDDL_MUTEX_PLAIN = "D:(A;;GA;;;WD)"

# 特权判定结果缓存：None = 还没查过（提不提权出生即定，进程内不变）
_LABEL_PRIV: bool | None = None


# 互斥体句柄：在 _try_windows_mutex 里赋值。模块级先给默认 None，
# 保证 _relaunch_as_admin 在任何路径下引用都不会 NameError
_MUTEX_HANDLE = None

# 同软件进程名的「指纹」：打包版 exe 叫
# "Steam Workshop Mod Assistant Management Tool V2.exe"，
# 按小写子串匹配。只在弹窗里用来补充显示对方 PID，锦上添花。
_SIBLING_NAME_NEEDLE = "workshop mod assistant"

# ---- 任务⑩：Qt 几何警告的人话转译 ----
_GEOMETRY_TOLD = False  # 提示只来一次


def _tell_geometry_hint() -> None:
    """几何警告 → 一次性人话弹窗（任务⑩）。"""
    global _GEOMETRY_TOLD
    if _GEOMETRY_TOLD:
        return
    _GEOMETRY_TOLD = True
    QMessageBox.information(
        None, t("窗口尺寸超出屏幕（不是故障）"),
        t("检测到上次记住的窗口尺寸在当前屏幕放不下（Qt: Unable to "
          "set geometry）。\n\n"
          "常见原因：那个尺寸是在另一块屏幕/另一个缩放比例下存下的"
          "（如 Surface Go 2 的 1920x1280、副屏的 125%/150% 缩放），"
          "Parsec 远程会话中的分辨率变化同理——跟现在的屏幕大小无关。\n"
          "程序已自动把窗口收进当前屏幕，功能与数据不受任何影响，"
          "这不是程序故障。本提示每次运行最多出现一次；同屏重启后"
          "不会再弹。"))


def _qt_message_handler(_mode, _context, message) -> None:
    """Qt 消息钩子（任务⑩）：几何警告弹人话（singleShot 出事件循环
    再弹，防在几何调整中途重入），原文照打 stderr 留档；其余消息
    一个不吞，原样走 stderr。"""
    if "Unable to set geometry" in message:
        print(f"[Qt] {message}", file=sys.stderr)
        QTimer.singleShot(0, _tell_geometry_hint)
        return
    print(f"[Qt] {message}", file=sys.stderr)


def _find_sibling_process() -> int | None:
    """找「同软件的另一个进程」真的在跑没有。返回它的 PID；没有返回 None。

    只在弹窗环节使用，用来在文案里补一句对方的 PID，方便用户去任务
    管理器核对。psutil 没装 / 任何意外 → 返回 None，绝不影响主流程。
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "name"]):
            info = proc.info
            if info["pid"] == me:
                continue  # 跳过自己
            name = (info["name"] or "").lower()
            if _SIBLING_NAME_NEEDLE in name:
                return info["pid"]
    except Exception:
        return None
    return None
def _probe_file_creation(target_dir: Path) -> str:
    """在指定目录做一次「建文件-删文件」探针，返回一行诊断结果。

    只在「互斥体和文件锁全都失败」的罕见路径上使用，把『哪个目录
    不让写』变成事实。探针文件跑完即删；任何失败都不上抛。
    结论词过 i18n；errno 后的原始系统报错保持原样——那是报障用的
    原始数据，刻意不翻译。
    """
    probe_path = target_dir / f"smma_v2_probe_{os.getpid()}.tmp"
    try:
        with open(probe_path, "x", encoding="utf-8") as f:
            f.write("probe")
    except OSError as exc:
        return t("失败：") + f"{type(exc).__name__}（errno={exc.errno}）：{exc}"
    try:
        probe_path.unlink()
    except OSError:
        pass  # 删除失败不影响结论（创建已成功）
    return t("OK（能正常创建文件）")

def _relaunch_as_admin() -> bool:
    """以管理员身份重启自己（写路径守卫失败时的快车道，v8）。

    实现顺序很关键：先释放单实例锁（CloseHandle），再 ShellExecuteW
    触发 UAC——顺序不能反。ShellExecuteW 是异步的：它返回成功时新
    进程可能已经在启动路上，若旧句柄还没放，新进程会误判"已有实例"
    而弹"程序已在运行"。

    返回 True = 新进程已在提权启动，调用方应立即退出本进程；
    返回 False = UAC 被取消或启动失败（调用方直接退出，用户重新
    双击再走一遍正常流程，或手动右键以管理员身份运行）。
    """
    if sys.platform != "win32":
        return False
    # 1) 先放锁，给新进程让路（放锁失败也无路可退：本进程本来就要退）
    try:
        if _MUTEX_HANDLE:
            _kernel32.CloseHandle(_MUTEX_HANDLE)
    except Exception:
        pass
    # 2) 以提权方式启动新进程（runas verb = 触发 UAC 弹窗）
    try:
        if getattr(sys, "frozen", False):
            # 打包版：重启 exe 自身
            target = sys.executable
            params = ""
        else:
            # 源码版：python.exe main.py
            target = sys.executable
            params = f'"{Path(__file__).resolve()}"'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, params, str(_APP_DIR), 1)  # SW_SHOWNORMAL
    except Exception:
        return False
    # MSDN：成功返回 > 32；但用户在 UAC 点"否"返回 ERROR_CANCELLED
    # (1223)，这个值同样大于 32，必须特判，否则会把"用户取消"误判
    # 成"已启动"
    ERROR_CANCELLED = 1223
    return ret > 32 and ret != ERROR_CANCELLED

def _show_already_running_dialog(sibling_pid: int | None,
                                 lock_path: str = "",
                                 lock_exists: bool = False,
                                 probe_lines: str = "",
                                 offer_admin: bool = False) -> None:
    """单实例锁抢锁失败时的弹窗。

    offer_admin=True（文件锁兜底也失败的罕见路径）时给「以管理员身份
    重启」按钮——连锁机制都确认不了状态时，提权重跑是最实际的出路。
    互斥体明确判定"已有实例"时不提供：已经在跑了，再开一个只会
    看到"程序已在运行"，没意义。
    """
    if sibling_pid is not None:
        text = (
                t("本工具（V2）已有另一个窗口在运行（可能最小化在任务栏）。\n\n"
                  "请切换到那个窗口使用——同时开两个会互相干扰"
                  "（两边同时指挥 steamcmd、同时写同一本账）。")
                + f"\n\nPID: {sibling_pid}"
        )
    elif lock_path and lock_exists:
        text = (
                t("没有检测到正在运行的本工具进程，但锁文件存在——可能是"
                  "上一次异常退出（崩溃 / 被杀毒软件拦截 / 被任务管理器"
                  "结束）留下的残留锁。\n\n"
                  "确认任务栏和任务管理器里没有本工具窗口后，把下面这个"
                  "文件删掉，再重新打开本软件即可：\n\n")
                + lock_path
        )
    else:
        text = (
            t("单实例锁检查没有正常完成（互斥体与文件锁都没能确认状态）。\n\n"
              "最常见的原因：本软件已经在运行——特别是以管理员身份启动的"
              "那个窗口还在（普通权限再开一个，检查可能被系统完整性保护"
              "挡住，看不到它）。\n\n"
              "请先看任务栏 / 任务管理器：如果本工具已在运行，直接去用"
              "那个窗口即可，本实例已自动退出。\n"
              "确认没有在运行、仍然弹本提示的，才是杀毒软件对刚打包的"
              "无签名 exe 的观察期拦截（每次重新打包都会触发新一轮）：\n"
              "① 把本软件所在的整个文件夹加入杀毒软件 / Windows 安全中心"
              "的白名单；\n"
              "② 右键 exe → 以管理员身份运行，再试一次；或点下方按钮。\n")
        )
        if probe_lines:
            text += "\n" + t("诊断探针（报障时请把控制台对应行一并发来）：\n") + probe_lines
        if lock_path:
            text += "\n" + t("文件锁的预期位置：\n") + lock_path
    if offer_admin and sys.platform == "win32":
        box = QMessageBox(QMessageBox.Information,
                          t("程序已在运行"), text)
        relaunch_btn = box.addButton(t("以管理员身份重启"),
                                     QMessageBox.AcceptRole)
        box.addButton(t("退出"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is relaunch_btn:
            _relaunch_as_admin()  # 无论启动成功与否，本进程都退出
    else:
        QMessageBox.information(None, t("程序已在运行"), text)
def _can_assign_integrity_label() -> bool:
    """本进程能否给对象贴完整性标签（SDDL 的 ML 子句）。

    判定顺序：① 试着启用 SeCreateIntegrityLabelPrivilege（管理员令牌
    通常已授予但默认禁用；AdjustTokenPrivileges 后 GetLastError=1300
    = 特权未授予，普通用户走到这）；② 特权路线没走通时用
    IsUserAnAdmin 兜底猜一把（管理员令牌若默认已启用则不启用也能贴）；
    ③ 都不行 = False，无标签创建。管理员走到 False 的代价只是退回
    旧行为（跨提权第二实例看不到它，弹窗文案已兜底）。
    """
    global _LABEL_PRIV
    if _LABEL_PRIV is not None:
        return _LABEL_PRIV
    _LABEL_PRIV = False
    if sys.platform != "win32" or _advapi32 is None or _kernel32 is None:
        return False
    try:
        from ctypes import wintypes
        adv = _advapi32
        adv.LookupPrivilegeValueW.restype = wintypes.BOOL
        adv.LookupPrivilegeValueW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LUID)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE)]
        adv.AdjustTokenPrivileges.restype = wintypes.BOOL
        adv.AdjustTokenPrivileges.argtypes = [
            wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p,
            wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD)]

        class _TP(ctypes.Structure):
            _fields_ = [("Count", wintypes.DWORD),
                        ("Luid", wintypes.LUID),
                        ("Attr", wintypes.DWORD)]

        luid = wintypes.LUID()
        token = wintypes.HANDLE()
        if adv.LookupPrivilegeValueW(
                None, "SeCreateIntegrityLabelPrivilege", ctypes.byref(luid)):
            if adv.OpenProcessToken(
                    _kernel32.GetCurrentProcess(), 0x0028,  # 调权|查询
                    ctypes.byref(token)):
                tp = _TP(1, luid, 0x0002)  # SE_PRIVILEGE_ENABLED
                ok = adv.AdjustTokenPrivileges(
                    token, False, ctypes.byref(tp), 0, None, None)
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(token)
                if ok and err == 0:  # 1300 = 未授予（普通用户）
                    _LABEL_PRIV = True
                    return True
        try:
            _LABEL_PRIV = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            _LABEL_PRIV = False
        return _LABEL_PRIV
    except Exception:
        _LABEL_PRIV = False
        return False
def _try_windows_mutex() -> int:
    """尝试用 Win32 命名互斥体抢单实例锁。

    返回：0 = 抢到；1 = 已有实例（内核确认）；
          -1 = 机制不可用 → 调用方走文件锁兜底。

    v10.2：描述符按特权分档（普通→无标签，管理员→带 Medium 标签）；
    带标签创建若仍失败，自动退回无标签再试——宁可退回旧行为，也不能
    让互斥体机制整个瘫掉（v10.1 实测教训：1314 把普通首启打废）。
    """
    global _MUTEX_HANDLE
    if _kernel32 is None:
        return -1  # 非 Windows：直接走文件锁兜底
    labeled = _can_assign_integrity_label()
    candidates = ([_SDDL_MUTEX_LABELED, _SDDL_MUTEX_PLAIN] if labeled
                  else [_SDDL_MUTEX_PLAIN])
    for idx, sddl in enumerate(candidates):
        try:
            sd = ctypes.c_void_p()
            if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(sd), None) or not sd:
                print(f"[main] SDDL 构造失败：{sddl!r}", file=sys.stderr)
                continue
            sa = _SECURITY_ATTRIBUTES()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = sd.value
            sa.bInheritHandle = False
            _MUTEX_HANDLE = _kernel32.CreateMutexW(
                ctypes.byref(sa), True, _MUTEX_NAME)
            ctypes.windll.kernel32.LocalFree(sd)
        except Exception as exc:
            print(f"[main] 互斥体创建异常：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        err = ctypes.get_last_error()
        _INVALID = ctypes.c_void_p(-1).value
        if _MUTEX_HANDLE and _MUTEX_HANDLE != _INVALID:
            if err == 183:  # ERROR_ALREADY_EXISTS
                print("[main] 单实例锁：互斥体已存在（内核判定已有实例）",
                      file=sys.stderr)
                return 1
            tag = "带 Medium 标签" if (idx == 0 and labeled) else "无标签"
            print(f"[main] 单实例锁：互斥体抢锁成功（{tag}）",
                  file=sys.stderr)
            return 0
        print(f"[main] 互斥体创建失败（{'带标签' if idx == 0 and labeled else '无标签'}）："
              f"GetLastError={err}", file=sys.stderr)
        if err == 5:
            # ACCESS_DENIED：互斥体已存在但打不开——典型：旧版打包的
            # 管理员实例（High 标签）在跑。重试也是撞同一对象，放弃
            break
        # 1314 等：换下一种描述符再试（带标签失败退无标签）
    # 兜底的兜底：默认安全属性（v5 历史 behavior）
    try:
        _MUTEX_HANDLE = _kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    except Exception as exc2:
        print(f"[main] 互斥体默认创建也失败：{type(exc2).__name__}: {exc2}",
              file=sys.stderr)
        return -1
    err = ctypes.get_last_error()
    _INVALID = ctypes.c_void_p(-1).value
    if not _MUTEX_HANDLE or _MUTEX_HANDLE == _INVALID:
        print(f"[main] 互斥体默认创建失败：GetLastError={err}",
              file=sys.stderr)
        return -1
    if err == 183:
        return 1
    print("[main] 单实例锁：互斥体抢锁成功（默认安全属性）",
          file=sys.stderr)
    return 0

def _acquire_single_instance() -> bool:
    """单实例锁总入口。返回 True = 可以继续启动（锁已持有）；
    返回 False = 不能启动（弹窗已弹过）。"""
    # ---- 首选：命名互斥体（Windows）----
    status = _try_windows_mutex()
    if status == 0:
        print("[main] 单实例锁：互斥体抢锁成功", file=sys.stderr)
        return True
    if status == 1:
        # 互斥体已明确判定"有实例在跑"。此时 file_lock_path / exists /
        # probe_lines 还没定义（它们属于兜底路径），只传 PID 分诊；
        # 也不给管理员重启按钮——已经在跑了，再提权开一个毫无意义。
        print("[main] 单实例锁：互斥体报告已有实例", file=sys.stderr)
        _show_already_running_dialog(_find_sibling_process())
        return False

    # ---- 兜底：QLockFile 文件锁（互斥体不可用才走到这）----
    if sys.platform == "win32":
        file_lock_path = _FALLBACK_LOCK_PATH  # 自有 data/，杀软盯防低
        try:
            Path(file_lock_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[main] 备用锁目录建不出来：{type(exc).__name__}：{exc}",
                  file=sys.stderr)
    else:
        file_lock_path = _TEMP_LOCK_PATH  # 非 Windows：沿用 Temp 方案
    print(f"[main] 互斥体不可用，回退文件锁：{file_lock_path}",
          file=sys.stderr)

    lock = QLockFile(file_lock_path)
    if lock.tryLock(0):
        _LOCK_KEEPALIVE.append(lock)  # 保活：对象被回收 = 锁被释放
        print("[main] 文件锁抢锁成功，继续启动", file=sys.stderr)
        return True

    # ---- 文件锁也失败：分诊 + 探针 + 弹窗（罕见路径）----
    # 走到这里的场景：互斥体机制不可用 + 文件锁也抢不到/建不出来。
    # 只有这条路径才配得上管理员重启按钮和全套探针诊断。
    exists = os.path.exists(file_lock_path)
    pid = _find_sibling_process()
    if pid is not None:
        _show_already_running_dialog(pid)
        return False
    probe_lines = ""
    if sys.platform == "win32":
        # 三个探针位各司其职：Temp（原锁位）、软件根（守卫同一结论）、
        # data/（文件锁的父目录——上一轮实测根 OK 但锁失败，嫌疑就在它）
        probe_temp = _probe_file_creation(Path(_TEMP_LOCK_PATH).parent)
        probe_app = _probe_file_creation(_APP_DIR)
        probe_data = _probe_file_creation(Path(file_lock_path).parent)
        print(f"[main] 探针·Temp 建文件：{probe_temp}", file=sys.stderr)
        print(f"[main] 探针·软件文件夹建文件：{probe_app}", file=sys.stderr)
        print(f"[main] 探针·data 目录建文件：{probe_data}", file=sys.stderr)
        probe_lines = (
                t("· 临时目录（Temp）建文件测试：") + f"{probe_temp}\n"
                + t("· 软件文件夹建文件测试：") + f"{probe_app}\n"
                + t("· data 目录（文件锁所在）建文件测试：") + f"{probe_data}")
    _show_already_running_dialog(None, file_lock_path, exists, probe_lines,
                                 offer_admin=True)
    return False

# ==================== 主流程 ====================

def main() -> int:
    app = QApplication(sys.argv)
    qInstallMessageHandler(_qt_message_handler)  # 任务⑩：见模块级钩子

    # —— 存储坐标（不是显示名！禁止改成标题/仓库 URL 等好看的形式，
    # 改了会话记忆静默搬家/丢失——踩坑㊽族。V2 取值 = 断代拍板）——
    app.setApplicationName("Steam Workshop Mod Assistant V2")
    app.setOrganizationName("sefrawe")
    app.setApplicationVersion("2.2.0")  # 与 version.txt、发行 tag 三处对齐

    # ---- 图标（运行时两件）：任务栏归属 + 窗口图标 ----
    # AppUserModelID：向 Windows 声明独立应用身份；自 V2 首次发布起
    # 冻结，永不改。互斥体名字复用同一词干，两者同进退。
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sefrawe.SteamWorkshopModAssistantV2.1")

    # 窗口图标：标题栏/任务栏/所有弹窗的共同来源。缺失时 stderr 留
    # 一行再跳过——QIcon 加载失败不报错、静默回退默认图标，缺图要留痕
    _icon_path = appPaths.resource_path("icon/icon.png")
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))
    else:
        print(f"[main] 图标缺失，跳过窗口图标：{_icon_path}", file=sys.stderr)
    # ---- i18n：语言必须在锁/守卫弹窗之前定下来（v10）----
    # 否则单实例锁、写路径守卫的启动期弹窗永远显示中文——即使用户
    # 已切英文。原位置在主题之后：v8 之前启动期没有任何弹窗，晚定
    # 语言无害；现在弹窗都在主题之前，位置就必须前移。
    from core.i18n import init_language
    init_language()

    # ---- R12 单实例锁：赶在守卫与主窗口之前 ----
    if not _acquire_single_instance():
        return 0

    # ---- T17 写路径守卫：必须赶在 AppSettings / 数据库之前 ----
    # v8：弹窗只给一条出路——以管理员身份重启（快车道）。
    # 实证背景：同一天、同文件夹，旧 exe 正常、新打包 exe 写 data/
    # 被拒（普通权限），提权即通——安全软件按「进程令牌身份」拦
    # 陌生 exe 的写操作。程序不猜病因，把最短出路直接递给用户。
    problem = appPaths.writability_problem()
    if problem is not None:
        print(f"[main] 写路径守卫未通过：{problem}", file=sys.stderr)
        box = QMessageBox(
            QMessageBox.Critical,
            t("无法启动：数据目录不可写"),
            problem)

        relaunch_btn = box.addButton(t("以管理员身份重启"),
                                     QMessageBox.AcceptRole)
        box.addButton(t("退出"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is relaunch_btn and _relaunch_as_admin():
            return 0  # 新进程已提权启动，本进程让位退出
        return 1

    # ---- 决策 70：会话记忆落点 = 软件文件夹内 config/ 下的 ini ----
    from gui.sessionStore import startup as session_startup
    session_startup()

    from gui.theme import apply_theme, apply_font_scale
    apply_theme(app)
    apply_font_scale(app)  # D25 界面字号缩放：必须在主窗口构造之前

    from gui.MainWindow import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    # ---- 启动期异常兜底（v4 引入，保留）----
    # 启动任何一步崩溃：完整堆栈同时打控制台 + 弹窗，告别"闪退失明"。
    # 只捕 Exception：sys.exit 的 SystemExit 不属于 Exception，不受影响。
    _exit_code = 0
    try:
        _exit_code = main()
    except Exception:
        import traceback
        _tb = traceback.format_exc()
        print("[main] 启动期异常，完整堆栈如下：", file=sys.stderr)
        print(_tb, file=sys.stderr)
        try:
            QMessageBox.critical(
                None, t("启动失败（诊断信息）"),
                t("程序在启动阶段遇到错误，已退出。下面是完整错误堆栈，"
                  "报障时请整段复制发来：\n\n")
                + _tb[-2000:])
        except Exception:
            pass  # 崩得太早连弹窗都弹不出时，stderr 里已有完整留档
        _exit_code = 1
    sys.exit(_exit_code)
