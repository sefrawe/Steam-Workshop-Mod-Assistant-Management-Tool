"""PTY 后端抽象：伪终端子进程的跨平台入口。

平台分工（2026-10 Linux 移植里程碑 2）：
- Windows：pywinpty（ConPTY）——steamcmd 需要"看起来是真控制台"
  的环境，普通管道会满缓冲不吐输出（踩坑记录见 terminalDock.py
  文件头）；
- Linux / macOS：pexpect（底层 ptyprocess）——POSIX 原生的
  伪终端，同样给子进程一个真 TTY。

终端面板只认本模块统一的 Pty 形状（read / write / isalive /
terminate / exitstatus），不直接碰任何一家的 API；换引擎只改这里。

依赖全部惰性导入：没装时软件照常启动，面板点"启动"时才提示安装。
"""

import sys

_WIN = sys.platform == "win32"

# pexpect 每次 read 的等待上限（秒）：无数据时返回空串，面板读线程
# continue 即可。pywinpty 的 read 无超时阻塞，两边行为不一致但不
# 影响读线程契约（见 terminalDock._PtyReader）。
_PEXPECT_READ_TIMEOUT = 0.5


class PtyError(Exception):
    """后端不可用 / 子进程启动失败。message 可直接展示给用户。"""


class Pty:
    """统一的伪终端子进程接口（面板只依赖这个形状）。"""

    def read(self, size: int) -> str:
        """读最多 size 字节输出；无数据时阻塞等待（面板在后台线程调）。"""
        raise NotImplementedError

    def write(self, data: str) -> None:
        """向子进程 stdin 写文本。"""
        raise NotImplementedError

    def isalive(self) -> bool:
        """子进程是否还在运行。"""
        raise NotImplementedError

    def terminate(self, force: bool = True) -> None:
        """结束子进程：force=True 强杀，False 温和退出。"""
        raise NotImplementedError

    @property
    def exitstatus(self) -> int | None:
        """退出码；尚未退出 = None。"""
        raise NotImplementedError


def pty_available() -> bool:
    """当前平台所需的 PTY 依赖是否已安装。"""
    try:
        if _WIN:
            import winpty  # noqa: F401
        else:
            import pexpect  # noqa: F401
    except ImportError:
        return False
    return True


def pty_install_hint() -> str:
    """缺依赖时给用户的安装指引（按平台各说各话）。"""
    return "pip install pywinpty" if _WIN else "pip install pexpect"


# ---- Windows 适配：pywinpty（ConPTY） ----


class _WinptyPty(Pty):
    def __init__(self, proc):
        self._proc = proc

    def read(self, size: int) -> str:
        return self._proc.read(size)

    def write(self, data: str) -> None:
        self._proc.write(data)

    def isalive(self) -> bool:
        return self._proc.isalive()

    def terminate(self, force: bool = True) -> None:
        self._proc.terminate(force=force)

    @property
    def exitstatus(self) -> int | None:
        return self._proc.exitstatus


# ---- POSIX 适配：pexpect ----


class _PexpectPty(Pty):
    def __init__(self, proc):
        self._proc = proc

    def read(self, size: int) -> str:
        return self._proc.read(size)

    def write(self, data: str) -> None:
        # POSIX 行纪律（ICRNL）会把输入的 CR 转成 NL：发 "\r\n"
        # 子进程收到的是 "\n\n"（多一个空行）。与真终端一致的做法
        # 是只发 \n。
        self._proc.write(data.replace("\r\n", "\n"))

    def isalive(self) -> bool:
        return self._proc.isalive()

    def terminate(self, force: bool = True) -> None:
        self._proc.terminate(force=force)

    @property
    def exitstatus(self) -> int | None:
        return self._proc.exitstatus


def spawn_pty(argv: list[str], *, cwd: str, rows: int, cols: int) -> Pty:
    """spawn 一个伪终端子进程。依赖缺失 / 启动失败 → PtyError。"""
    if _WIN:
        try:
            from winpty import PtyProcess
        except ImportError as exc:
            raise PtyError(
                f"缺少终端依赖 pywinpty：{exc}\n"
                f"请执行 {pty_install_hint()} 后重启本软件。") from exc
        try:
            proc = PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols))
        except Exception as exc:
            raise PtyError(f"伪终端启动失败：{exc}") from exc
        return _WinptyPty(proc)

    try:
        import pexpect
    except ImportError as exc:
        raise PtyError(
            f"缺少终端依赖 pexpect：{exc}\n"
            f"请执行 {pty_install_hint()} 后重启本软件。") from exc
    try:
        proc = pexpect.spawn(
            argv, cwd=cwd, dimensions=(rows, cols),
            timeout=_PEXPECT_READ_TIMEOUT,
            encoding="utf-8", codec_errors="replace")
    except Exception as exc:
        raise PtyError(f"伪终端启动失败：{exc}") from exc
    return _PexpectPty(proc)
