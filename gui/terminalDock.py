"""steamcmd 终端面板
"""
"""
控制台 → steamcmd 终端标签页：在软件里启动/停止 steamcmd 子进程、
手动敲命令（登录验证码场景）、直接看它的原始输出。

【为什么用伪控制台（ConPTY）而不是普通管道——踩坑记录】
第一版用 QProcess（管道）启动 steamcmd，结果启动横幅之后什么都
不显示：Loading Steam API...OK、Steam> 提示符、登录应答全部消失。
原因：进程输出接到管道后，C 运行时默认"满缓冲"——攒够约 4KB 才
真正吐一次数据。steamcmd 的启动横幅是初始化时主动刷出的所以能看到，
后面的应答一行几十字节永远凑不满 4KB，全堵在缓冲区里。黑色窗口
没这个问题，因为真控制台是"写一行显示一行"。
解决：改用 pywinpty（Spyder/Jupyter 终端的同款底层）创建 Windows
伪控制台（ConPTY）——给子进程一个"看起来是真控制台"的环境，
steamcmd 的行为与手动双击完全一致：逐行吐输出、自己回显命令、
自己打 Steam> 提示符。附带两个简化：
- 命令回显由控制台自己做，本面板不再手动补回显；
- ConPTY 输出统一是 UTF-8，pywinpty 直接返回 str，
  不再需要 UTF-8/GBK 两级解码。

分工口径（"终端看原文、运行日志说人话"两层不混）：
- 本面板的输出区一字不差显示 steamcmd 的原文——你复制到黑色
  窗口里能看到的，这里也能看到，排查问题时要的就是这份原汁原味；
- 每行原文同时交给 core/outputAnalyzer 判定，关键结论（下载
  成功/失败、登录完成、断线等）走 LogBus 送到"运行日志"标签页，
  按级别着色说人话——结论归结论、原文归原文。

 为批量下载提供的接口（已由 gui/batchDownloadController.py 接线）：
- verdict_emitted 信号：每得到一条结论就发出，批量编排靠它
  判断"当前这条命令出结果了没有"；
- idle_prompt_seen 信号：单独一行 Steam> = steamcmd 空闲，
  批量编排靠它决定"现在可以发下一条命令"（真实样本里结论会
  续在下载开始那一行，所以只有看到单独的提示符行才算真的完了）；
- send_command() 方法：向 steamcmd 发一行命令的公共入口，
  批量发命令也走这里，和用户在输入框敲字同一条路——
  入口只有一条，才不会有绕过检查的后门。

三个实操细节（改这里之前先读）：
1) pywinpty 的 read() 无数据时会阻塞等待——放在后台 QThread
   里循环调用，每读到一块就发信号回主线程（Qt 跨线程信号自动
   排队，与 LogBus 同一条线程安全路数）；
2) ConPTY 的输出流里混着 ANSI 转义序列（控制光标/颜色的不可见
   字符），逐行剥掉再显示，否则输出区会偶尔冒出乱码；
3) ConPTY 的"屏幕"宽度决定 steamcmd 认为一行能写多长——太窄时
   "Success. Downloaded item …"这样的长行会被折成两行，
   outputAnalyzer 的逐行判定就断了。所以伪终端开 240 列宽，
   保长行不断行。

单实例策略（按用户拍板放宽）：整台机器同一时间跑两个 steamcmd
会争用同一个下载目录和工坊账本（acf），强烈不建议——但不再硬拦；
启动前检测到已有 steamcmd 在跑时弹窗警告，用户确认后放行。

停止逻辑：先发 quit 命令优雅退出（保住本机的登录缓存，
下次启动不用重新验证）；5 秒还没退才强杀——强杀丢登录态，
是不得已才用的手段。
"""
import re
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from core import outputAnalyzer
from core.backupManager import steamcmd_running
from gui.logBus import LogBus
from core.steamPaths import ensure_steamcmd_exe

try:
    from winpty import PtyProcess
except ImportError:
    # 没装 pywinpty：软件照常启动，点"启动 steamcmd"时给出安装指引
    PtyProcess = None

# 登录命令在设置页里的键名（与 settingsPage._FIELDS 同名，见决策 12）
_LOGIN_CMD_KEY = "steamcmd_login_cmd"

# 伪终端尺寸：(行数, 列数)。列数开大是给 outputAnalyzer 的——见文件头细节 3
_PTY_ROWS, _PTY_COLS = 30, 240

# ANSI 转义序列（ConPTY 输出里控制光标/颜色的不可见字符），逐行剥掉。
# 注意：这是模块级常量，用的时候直接写 _ANSI_RE，不要写成 self._ANSI_RE
# （self 只找实例/类属性，找不到模块级变量——之前在这里栽过一跤）
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# 结论 → 日志级别。不认识的种类一律 info（宁可低调也不乱报警）
_VERDICT_LEVELS = {
    outputAnalyzer.KIND_DOWNLOAD_STARTED: "info",
    outputAnalyzer.KIND_DOWNLOAD_SUCCESS: "ok",
    outputAnalyzer.KIND_DOWNLOAD_FAILED: "error",
    outputAnalyzer.KIND_DOWNLOAD_TIMEOUT: "error",
    outputAnalyzer.KIND_NOT_LOGGED_ON: "warn",
    outputAnalyzer.KIND_DISCONNECTED: "warn",
    outputAnalyzer.KIND_COMMAND_NOT_FOUND: "warn",
    outputAnalyzer.KIND_LOGIN_OK: "ok",
    outputAnalyzer.KIND_READY: "info",
}


class _PtyReader(QThread):
    """后台读线程：循环读伪终端输出，每读到一块就发回主线程。

    read() 无数据时阻塞（细节 1），所以必须放线程里；子进程退出时
    read 抛 EOFError 或 isalive 变 False，循环自然结束，最后把
    退出码发回主线程收尾。
    """

    chunk_received = Signal(str)  # 一块输出原文（可能不是完整的行）
    exited = Signal(int)          # 子进程退出，携带退出码（未知 = -1）

    def __init__(self, pty, parent=None) -> None:
        super().__init__(parent)
        self._pty = pty

    def run(self) -> None:
        while self._pty.isalive():
            try:
                chunk = self._pty.read(4096)
            except (EOFError, OSError):
                break
            if not chunk:
                continue  # pywinpty 偶发返回空串（内部哨兵值），跳过即可
            self.chunk_received.emit(chunk)
        try:
            status = int(self._pty.exitstatus)
        except Exception:
            status = -1
        self.exited.emit(status)


class TerminalDock(QWidget):
    """steamcmd 终端：启动/停止子进程 + 原文输出 + 手动命令。"""

    verdict_emitted = Signal(object)  # outputAnalyzer.Verdict
    idle_prompt_seen = Signal()

    def __init__(self, log_bus: LogBus | None = None,
                 settings=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log = log_bus if log_bus is not None else LogBus()
        self._settings = settings  # AppSettings：读 steamcmd 路径和登录命令
        self._pty = None       # pywinpty 的 PtyProcess（未启动 = None）
        self._reader = None    # 配套的读线程（未启动 = None）
        self._buf = ""         # 输出缓冲：攒够整行才解析
        self._build_ui()

    # ---------------- 界面 ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # 顶部：状态 + 三个控制按钮
        top = QHBoxLayout()
        self._lbl_state = QLabel("未启动", self)
        self._btn_start = QPushButton("启动 steamcmd", self)
        self._btn_start.setToolTip(
            "按设置页里的 steamcmd 程序路径启动子进程；"
            "本机已有 steamcmd 在跑时会先提醒（同一时间跑两个不建议）")
        self._btn_stop = QPushButton("停止", self)
        self._btn_stop.setToolTip(
            "先发 quit 优雅退出（保住登录缓存）；5 秒没退才强杀")
        self._btn_login = QPushButton("发送登录命令", self)
        self._btn_login.setToolTip(
            "把设置页里填的登录命令原样发给 steamcmd；"
            "还没填的话去设置页 → steamcmd 登录命令")
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_stop.setEnabled(False)
        self._btn_login.clicked.connect(self._on_send_login)
        self._btn_login.setEnabled(False)
        top.addWidget(self._lbl_state)
        top.addWidget(self._btn_start)
        top.addWidget(self._btn_stop)
        top.addWidget(self._btn_login)
        top.addStretch(1)  # T19⑥：弹簧挪到按钮后面——按钮靠左，与 mod 库页顶栏同风格
        root.addLayout(top)

        # 中部：输出区（等宽字体更像终端；上限 5000 行防内存膨胀，
        # 超出自动丢最旧行——与运行日志的 2000 行同理，实现都是一行设置）
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(5000)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._view.setFont(font)
        root.addWidget(self._view, 1)

        # 底部：命令输入框 + 发送按钮
        bottom = QHBoxLayout()
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(
            "输入 steamcmd 命令，回车发送（例：login 你的用户名）")
        self._input.setEnabled(False)
        self._btn_send = QPushButton("发送", self)
        self._btn_send.setEnabled(False)
        self._input.returnPressed.connect(self._on_send_input)
        self._btn_send.clicked.connect(self._on_send_input)
        bottom.addWidget(self._input, 1)
        bottom.addWidget(self._btn_send)
        root.addLayout(bottom)

    # ---------------- 对外（MainWindow / 批量编排调用） ----------------

    def is_busy(self) -> bool:
        """steamcmd 子进程是否还在跑（关窗确认、批量编排的开工检查用）。"""
        return self._is_running()

    def send_command(self, text: str) -> bool:
        """向 steamcmd 发一行命令。返回是否已发出。

        用户输入框和将来的批量编排都从这一条路走。ConPTY 模式下
        命令回显和 Steam> 提示符由控制台自己完成（和黑色窗口一样），
        本方法不再手动补回显。
        """
        if not self._is_running():
            self._log.warn("steamcmd 未在运行：命令没有发出")
            return False
        text = text.strip()
        if not text:
            return False
        try:
            self._pty.write(text + "\r\n")
        except (EOFError, OSError):
            self._log.error("steamcmd 已退出：命令没有发出")
            return False
        return True

    def shutdown(self) -> None:
        """程序退出前的收尾（MainWindow.closeEvent 统一调用）。

        退出路径上没有第二次机会，这里同步等待：先给 quit 3 秒，
        还不退就强杀——与备份页 shutdown() 的"收尾阶段允许短暂
        阻塞"是同一个理由。
        """
        if self._is_running():
            try:
                self._pty.write("quit\r\n")
            except (EOFError, OSError):
                pass
            deadline = time.time() + 3.0
            while self._is_running() and time.time() < deadline:
                time.sleep(0.1)
            if self._is_running():
                try:
                    self._pty.terminate(force=True)
                except Exception:
                    pass
        if self._reader is not None:
            self._reader.wait(3000)

    # ---------------- 启动 / 停止 ----------------

    def _is_running(self) -> bool:
        return self._pty is not None and self._pty.isalive()

    def _on_start(self) -> None:
        if self._is_running():
            return
        if PtyProcess is None:
            QMessageBox.warning(
                self, "缺少组件",
                "终端功能需要 pywinpty 库（Windows 伪控制台），当前环境未安装。\n"
                "请先执行：\n    pip install pywinpty\n装好后重启本软件。")
            return
        exe = (str(self._settings.get("steamcmd_path") or "").strip()
               if self._settings is not None else "")
        # T21①：设置页保存时已把文件夹补全成完整 exe 路径；这里再兜一层，
        # 手改 JSON 直接填文件夹也能启动
        exe = ensure_steamcmd_exe(exe)

        if not exe:
            QMessageBox.information(
                self, "无法启动",
                "还没有设置 steamcmd 程序路径。\n"
                "请到 设置页 → steamcmd 程序，填入 steamcmd.exe 的完整路径。")
            return
        if not Path(exe).is_file():
            QMessageBox.warning(
                self, "无法启动",
                f"设置页里的路径在磁盘上找不到文件：\n{exe}\n"
                "请到 设置页 → steamcmd 程序 重新选择。")
            return
        # 单实例提醒（不拦截）：两个 steamcmd 同时跑会争用同一个
        # 下载目录和工坊账本（acf），强烈不建议；确有需要时确认放行
        if steamcmd_running():
            ret = QMessageBox.question(
                self, "检测到 steamcmd 正在运行",
                "本机已有一个 steamcmd 在运行。同一时间跑两个 steamcmd "
                "会争用同一个下载目录和工坊账本（acf），不建议这样做。\n\n"
                "仍要再启动一个吗？")
            if ret != QMessageBox.StandardButton.Yes:
                self._log.info("已取消启动：等已有 steamcmd 结束后再来")
                return
            self._log.warn("在已有 steamcmd 的情况下仍启动了一个（不建议）")
        try:
            # argv 用列表形式：路径带空格时字符串形式会被 shlex 拆坏。
            # dimensions 见文件头细节 3；cwd 设为 steamcmd 所在目录，
            # 让它自己的日志/缓存写在熟悉的位置
            pty = PtyProcess.spawn(
                [exe], cwd=str(Path(exe).parent),
                dimensions=(_PTY_ROWS, _PTY_COLS))
        except Exception as exc:
            self._log.error(f"steamcmd 启动失败：{exc}")
            QMessageBox.warning(self, "无法启动", f"steamcmd 启动失败：{exc}")
            return
        self._pty = pty
        self._buf = ""
        reader = _PtyReader(pty, self)
        reader.chunk_received.connect(self._on_chunk)
        reader.exited.connect(self._on_exited)
        self._reader = reader
        reader.start()
        self._set_running_ui(True)
        self._log.info(f"steamcmd 启动中：{exe}")

    def _on_stop(self) -> None:
        if not self._is_running():
            return
        # 先礼后兵：quit 让它正常收尾（写完日志、保存登录缓存），
        # 5 秒还没退（比如卡在下载写盘）才强杀
        try:
            self._pty.write("quit\r\n")
        except (EOFError, OSError):
            pass
        self._log.info("已向 steamcmd 发送 quit，等待退出…")
        QTimer.singleShot(5000, self._force_kill_if_alive)

    def _force_kill_if_alive(self) -> None:
        if self._is_running():
            self._log.warn("steamcmd 5 秒内没有退出，强制结束（登录缓存可能丢失）")
            try:
                self._pty.terminate(force=True)
            except Exception:
                pass

    def _set_running_ui(self, running: bool) -> None:
        self._lbl_state.setText("运行中" if running else "未启动")
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_login.setEnabled(running)
        self._input.setEnabled(running)
        self._btn_send.setEnabled(running)

    # ---------------- 子进程输出与生命周期 ----------------

    def _on_chunk(self, chunk: str) -> None:
        """收到一块原文：按行切开、剥 ANSI 转义，逐行处理。"""
        # \r\n 先成对变 \n；剩下的孤 \r（进度条重绘）也当换行处理，
        # 避免两行内容粘在一行里
        text = chunk.replace("\r\n", "\n").replace("\r", "\n")
        self._buf += text
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break  # 剩的是半行，留在缓冲里等下一块
            line = _ANSI_RE.sub("", self._buf[:nl])  # 模块级常量，不带 self.
            self._buf = self._buf[nl + 1:]
            self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        """一行原文：显示 → 判定 → 结论广播（信号 + 人话日志）。"""
        self._view.appendPlainText(line)
        verdict = outputAnalyzer.classify_line(line)
        if verdict is not None:
            # 结论发给批量编排（controller 已接线）；没有订阅者时发进空气，无副作用
            self.verdict_emitted.emit(verdict)
            level = _VERDICT_LEVELS.get(verdict.kind, "info")
            getattr(self._log, level)(outputAnalyzer.describe(verdict))
        if outputAnalyzer.is_idle_prompt(line):
            self.idle_prompt_seen.emit()

    def _on_exited(self, status: int) -> None:
        """子进程退出（无论 quit、强杀还是自己崩）统一走到这里收尾。"""
        self._log.info(f"steamcmd 已退出（退出码 {status}）")
        self._buf = ""
        self._pty = None
        self._set_running_ui(False)
        # 读线程发完 exited 就结束了：等它彻底退出再放手，
        # 防止"销毁仍在运行的线程"（与更新检测页同一套路）
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.wait(2000)

    # ---------------- 输入框 ----------------

    def _on_send_input(self) -> None:
        """用户在输入框敲的命令：与批量下载同走 send_command 一条路。"""
        text = self._input.text()
        if self.send_command(text):
            self._input.clear()

    def _on_send_login(self) -> None:
        """把设置页里的登录命令原样发给 steamcmd。

        登录命令可能有验证码等后续交互——那部分用户直接在下面的
        输入框里敲（这正是保留手动输入框的原因）。
        """
        cmd = (str(self._settings.get(_LOGIN_CMD_KEY) or "").strip()
               if self._settings is not None else "")
        if not cmd:
            self._log.warn("设置里还没填登录命令：设置页 → steamcmd 登录命令")
            return
        if self.send_command(cmd):
            self._log.info("登录命令已发送，等 steamcmd 应答；"
                           "若提示验证码，直接在下方输入框输入即可")
