"""批量下载控制器
"""
"""
GUI 接线层：把 TerminalDock 的信号、StepCardList 的按钮信号和
core.BatchDownloadFlow 缝在一起。自己不画界面（界面在
stepCardList）、不写状态机（状态机在 batchDownloadFlow）——
这里只有胶水。

为什么单独一层而不是写在 MainWindow 里：
1) "接信号—断信号"有生命周期要管：批次结束后必须拆掉
   verdict/idle 接线，否则下一批会收到上一批的余波——
   这份收尾逻辑收在小类里，MainWindow 只剩三行；
2) MainWindow 已经很大，能不碰就不碰（贴错文件的教训）。

单批次约定：steamcmd 单实例（先跑的警告不拦截，但两个批次
同时发命令必然互相踩），所以控制器整软件只建一个实例；
旧批次未结束时 start_batch 直接拒绝。

GUI 侧规矩（上一块测试时定下的，这里落实）：
start_batch 返回 False 时只说明"没启动起来"（steamcmd 没跑、
没勾选、已在批次里），错误详情一律以 batch_done 事件为准——
事件是唯一事实来源，调用方不要在返回 False 时再补一条日志。
"""
from PySide6.QtCore import QObject

from core import batchDownloadFlow

# 登录命令在设置里的键名（与 settingsPage._FIELDS 同名，决策 12）
_LOGIN_CMD_KEY = "steamcmd_login_cmd"


class BatchDownloadController(QObject):
    """一个软件实例一个：管理当前批次的接线与生命周期。"""

    def __init__(self, terminal, step_list, log, settings,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._terminal = terminal    # TerminalDock：发命令、收结论
        self._step_list = step_list  # StepCardList：画事件、发按钮信号
        self._log = log              # LogBus
        self._settings = settings    # AppSettings：读登录命令
        self._flow = None            # 当前批次（None = 空闲）
        step_list.stop_requested.connect(self._on_stop)
        step_list.resume_requested.connect(self._on_resume)

    # ---------------- 对外 ----------------

    def is_active(self) -> bool:
        """是否有批次在跑（MainWindow 关窗确认用）。"""
        return self._flow is not None

    def start_batch(self, app_id: int, mod_ids: list[int]) -> bool:
        """开一批。返回是否启动成功（False 的原因以事件/日志为准）。"""
        if self._flow is not None:
            self._log.warn("已有批次在进行中：等它结束或先点【停止批次】")
            return False
        if not mod_ids:
            self._log.warn("没有勾选任何 mod：批次没有启动")
            return False
        if not self._terminal.is_busy():
            self._log.error("steamcmd 未在运行：请先到 控制台 → steamcmd 终端 "
                            "启动并登录，再批量下载")
            return False
        flow = batchDownloadFlow.BatchDownloadFlow(
            app_id, list(mod_ids),
            send_command=self._terminal.send_command,
            on_event=self._on_event)
        self._flow = flow
        # 接线必须先于 start：start 内部可能同步发出事件
        self._terminal.verdict_emitted.connect(flow.on_verdict)
        self._terminal.idle_prompt_seen.connect(flow.on_idle)
        login_cmd = self._login_cmd()
        self._step_list.reset_for_batch(
            total=len(mod_ids), has_login=bool(login_cmd))
        if not flow.start(login_cmd=login_cmd or None):
            # 没启动起来：batch_done（带 error）已在 start 内部
            # 同步发过并走了 _on_event → 汇总卡/日志都有了，
            # 这里只拆线即可
            self._detach(flow)
            return False
        self._log.info(f"批量下载开始：共 {len(mod_ids)} 个 mod")
        return True

    # ---------------- 内部：接线管理 ----------------

    def _login_cmd(self) -> str:
        if self._settings is None:
            return ""
        return str(self._settings.get(_LOGIN_CMD_KEY) or "").strip()

    def _detach(self, flow) -> None:
        """拆掉本批次的信号接线（批次结束 / 启动失败都要调）。

        不拆的话：下一批开始前终端里的任何结论都会灌进旧 flow，
        状态机被陈旧信号打扰——接线与拆线必须成对。
        """
        try:
            self._terminal.verdict_emitted.disconnect(flow.on_verdict)
        except (RuntimeError, TypeError):
            pass  # 本来就没连上 / 已断开（启动失败路径会二次调用）
        try:
            self._terminal.idle_prompt_seen.disconnect(flow.on_idle)
        except (RuntimeError, TypeError):
            pass
        if self._flow is flow:
            self._flow = None

    # ---------------- 内部：flow 事件出口 ----------------

    def _on_event(self, ev: dict) -> None:
        """流程事件：一律先给卡片，再按需写批次级日志。

        逐条成败不在这里写日志——终端已把每条 verdict 送到
        运行日志（说人话那层），这里再写就是重复播报。
        """
        self._step_list.handle_event(ev)
        t = ev.get("type")
        if t == "need_login":
            self._log.warn(ev.get("note") or "批次暂停：需要手动登录")
        elif t == "batch_done":
            s = ev.get("summary") or {}
            ok_n = len(s.get("ok") or [])
            fail_n = len(s.get("failed") or [])
            to_n = len(s.get("timeout") or [])
            if s.get("error"):
                self._log.error(f"批次出错收尾：{s['error']}")
            elif s.get("stopped"):
                self._log.info(f"批次已停止：成功 {ok_n}，失败 {fail_n}，"
                               f"超时 {to_n}")
            else:
                self._log.info(f"批次完成：成功 {ok_n}，失败 {fail_n}，"
                               f"超时 {to_n}")
            flow = self._flow
            if flow is not None:
                self._detach(flow)

    # ---------------- 内部：卡片按钮 ----------------

    def _on_stop(self) -> None:
        if self._flow is not None:
            self._flow.stop()
            self._log.info("已请求停止批次：不再发新命令，当前条跑完即收尾")

    def _on_resume(self) -> None:
        if self._flow is not None:
            self._flow.resume()
