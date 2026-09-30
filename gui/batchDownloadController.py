"""批量下载控制器
"""
"""
GUI 接线层：把 TerminalDock 的信号、StepCardList 的按钮信号和
core.BatchDownloadFlow 缝在一起。自己不画界面（界面在 stepCardList）、
不写状态机（状态机在 batchDownloadFlow）——这里只有胶水。

为什么单独一层而不是写在 MainWindow 里：
1) "接信号—断信号"有生命周期要管：批次结束后必须拆掉 verdict/idle
   接线，否则下一批会收到上一批的余波——这份收尾逻辑收在小类里，
   MainWindow 只剩三行；
2) MainWindow 已经很大，能不碰就不碰（贴错文件的教训）。

单批次约定：steamcmd 单实例（先跑的警告不拦截，但两个批次同时发
命令必然互相踩），所以控制器整软件只建一个实例；旧批次未结束时
start_batch 直接拒绝。

批次前备份阶段（决策 40）：调用方传入 backup_first（要先备份的
条目清单）时，先在后台线程逐个调备份引擎，全部备完自动接着开下载
批次。时机是安全的：此刻批次还没开始，steamcmd 停在提示符空闲，
没有任何下载在写盘，robocopy 读到的是稳定文件——备份引擎里
"steamcmd 正在运行"的警告是针对下载进行中场景的（R7），在本阶段
必然出现且必然失真，由控制器统一过滤、不逐条转述。备份失败的条目
不参与下载（保护原内容），计入 batch_done 汇总的 backup_failed。

GUI 侧规矩（上一块测试时定下的，这里落实）：
start_batch 返回 False 时只说明"没受理"（steamcmd 没跑、没勾选、
已在批次里），错误详情一律以 batch_done 事件/运行日志为准——事件
是唯一事实来源，调用方不要在返回 False 时再补一条日志。
"""
from PySide6.QtCore import QObject, QThread, Signal

from core import batchDownloadFlow
from core.backupManager import BackupManager

# 登录命令在设置里的键名（与 settingsPage._FIELDS 同名，决策 12）
_LOGIN_CMD_KEY = "steamcmd_login_cmd"
# 备份保留策略的两个键名（与 backupPage 同名同源，决策 12）
_KEY_KEEP_PER_MOD = "backup_keep_per_mod"
_KEY_QUOTA_GB = "backup_total_quota_gb"


class _BackupPhaseWorker(QThread):
    """批次前的备份阶段（决策 40）：逐个 mod 调备份引擎，为
    "备份+更新"条目在下载前留一份可回滚的旧版本。
    停止协议与备份管理页一致：批间生效（当前这个备完就停），
    进行中的 robocopy 不打断。"""
    one_done = Signal(int, int, bool, str)  # 已完成数, 总数, 成功?, 一行摘要
    phase_done = Signal(list, bool)         # (失败 [(编号, 摘要)], 是否手动停止)
    crashed = Signal(str)                   # 预期外异常（bug 性质）

    def __init__(self, manager: BackupManager, mod_ids: list[int]) -> None:
        super().__init__()
        self._manager = manager
        self._ids = list(mod_ids)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        failed: list[tuple[int, str]] = []
        total = len(self._ids)
        for i, mid in enumerate(self._ids, 1):
            if self._stop:
                self.phase_done.emit(failed, True)
                return
            try:
                rep = self._manager.backup_mod(mid)
            except Exception as exc:  # 引擎约定操作层失败走报告；到这里=bug
                self.crashed.emit(f"备份 mod {mid} 时出现预期外错误：{exc}")
                return
            if rep.ok:
                line = f"mod {mid} 备份成功"
            else:
                # 摘要只取第一行：进度日志要能读，完整原因在报告里
                first = (rep.error or "未知原因").splitlines()[0]
                line = f"mod {mid} 备份失败：{first}"
                failed.append((mid, line))
            self.one_done.emit(i, total, rep.ok, line)
        self.phase_done.emit(failed, False)


class BatchDownloadController(QObject):
    """一个软件实例一个：管理当前批次（含备份阶段）的接线与生命周期。"""

    batch_done = Signal(dict)  # 批次收尾（含汇总 dict）；主窗口接它做自动复扫

    def __init__(self, terminal, step_list, log, settings, repo,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._terminal = terminal   # TerminalDock：发命令、收结论
        self._step_list = step_list # StepCardList：画事件、发按钮信号
        self._log = log             # LogBus
        self._settings = settings   # AppSettings：读登录命令与备份策略
        self._repo = repo           # 仓库：备份阶段读写备份账（决策 40）
        self._flow = None           # 当前下载批次（None = 空闲）
        self._phase_worker: _BackupPhaseWorker | None = None  # 备份阶段线程
        self._pending_batch: tuple[int, list[int]] | None = None  # 阶段后备次
        self._phase_backup_failed: list[int] = []  # 并进 batch_done 汇总
        step_list.stop_requested.connect(self._on_stop)
        step_list.resume_requested.connect(self._on_resume)
        # steamcmd 进程退出 → 立即中止在跑批次（v2.45 僵尸批次防线）
        terminal.process_exited.connect(self._on_process_exited)
        self._shutting_down = False  # closeEvent 置位后不再响应退出事件


    # ---------------- 对外 ----------------

    def is_active(self) -> bool:
        """是否有批次在跑（MainWindow 关窗确认 / 导入拒绝等用）。
        备份阶段也算——它是批次的前半段，此时开新批次或清库会打架。"""
        return self._flow is not None or self._phase_worker is not None

    def start_batch(self, app_id: int, mod_ids: list[int],
                    backup_first: list[int] | None = None) -> bool:
        """开一批。backup_first = 其中要先备份旧版本再下载的条目
        （决策 40"备份+更新"；None/空 = 全部直接下载——mod 库页的
        手动批次不备份，维持现状，那边有右键手动备份兜底）。
        返回是否受理成功（False 的原因以事件/日志为准）。
        有备份条目时先跑备份阶段（后台线程，进度进运行日志），备份
        完自动接着开下载批次。"""
        if self._flow is not None or self._phase_worker is not None:
            self._log.warn("已有批次在进行中：等它结束或先点【停止批次】")
            return False
        if not mod_ids:
            self._log.warn("没有勾选任何 mod：批次没有启动")
            return False
        if not self._terminal.is_busy():
            self._log.error("steamcmd 未在运行：请先到 控制台 → steamcmd 终端 "
                            "启动并登录，再批量下载")
            return False
        if backup_first:
            self._start_backup_phase(app_id, list(mod_ids), list(backup_first))
            return True
        self._launch(app_id, list(mod_ids))
        return True

    def shutdown(self) -> None:
        """主窗口退出前调用（closeEvent 统一调）：备份阶段若在跑，
        批间停止并等它收尾，避免退出时销毁活线程。下载批次本身由
        terminal.shutdown 收尾（steamcmd 退出后流程自然结束）。
        同时置关机旗：reader 线程的退出事件在 closeEvent 走完后才
        投递，此后不再中止批次——程序正在退场，收尾汇总与自动复扫
        都该免了（库马上就要关闭，不能在关闭后再触发复扫）。"""
        self._shutting_down = True
        if self._phase_worker is not None:
            self._phase_worker.stop()
            self._phase_worker.wait()
    def _on_process_exited(self, _status: int) -> None:
        """steamcmd 进程退出（quit / 强杀 / 崩溃都走这）：批次还在跑
        就立即强制中止。流程等的是"当前条的结论"与"空闲提示符"，
        进程没了这两样永远不会来——不中止就是僵尸批次：is_active()
        永真，退出守卫、检测清单、新批次、账本导入全被挡住（v2.45
        实证）。备份阶段不受影响：备份不依赖 steamcmd，照常收尾；
        随后的下载批次会因命令发送失败自然出错收尾（_try_send 现成
        路径，不会挂死）。"""
        if self._shutting_down:
            return  # 程序退出中：批次随程序结束，不再收尾复扫
        flow = self._flow
        if flow is None:
            return
        pending = flow.pending_count
        msg = "steamcmd 已退出：批次立即中止"
        if pending:
            msg += f"，还有 {pending} 条未开始"
        self._log.warn(msg)
        flow.abort(reason="steamcmd 已退出")
        # abort → batch_done 事件 → _on_event → 汇总卡 + batch_done
        # 广播（主窗口自动复扫）→ _detach，全部同步完成，这里无事可做

    # ---------------- 下载批次启动（原 start_batch 本体） ----------------

    def _launch(self, app_id: int, mod_ids: list[int]) -> bool:
        """真正开下载批次：接线 → 复位卡片 → 启动流程状态机。"""
        flow = batchDownloadFlow.BatchDownloadFlow(
            app_id, list(mod_ids), send_command=self._terminal.send_command,
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

    # ---------------- 批次前备份阶段（决策 40） ----------------

    def _start_backup_phase(self, app_id: int, mod_ids: list[int],
                            backup_ids: list[int]) -> None:
        """批次前半段：把"备份+更新"条目的当前版本先备份一遍。
        备份引擎按次构造（与备份管理页同一组装：保留份数 / 总量配额
        / steamcmd 路径都从设置现读）。卡片先复位成批次形状，备份
        进度写运行日志；备完在 _on_phase_done 里接着开下载批次。"""
        keep = self._settings.get_int(_KEY_KEEP_PER_MOD, 1) \
            if self._settings else 1
        quota_gb = self._settings.get_int(_KEY_QUOTA_GB, 100) \
            if self._settings else 100
        manager = BackupManager(
            self._repo,
            keep_per_mod=keep,
            # GB → 字节只在边界换算一次；0 = 不限
            quota_bytes=(quota_gb * 1024 ** 3) if quota_gb > 0 else None,
            steamcmd_path=(self._settings.get("steamcmd_path")
                           if self._settings else ""))
        self._pending_batch = (app_id, list(mod_ids))
        self._phase_backup_failed = []  # 新批次开始，清掉上一批的残留
        login_cmd = self._login_cmd()
        self._step_list.reset_for_batch(
            total=len(mod_ids), has_login=bool(login_cmd))
        self._phase_worker = _BackupPhaseWorker(manager, backup_ids)
        self._phase_worker.one_done.connect(self._on_phase_one_done)
        self._phase_worker.phase_done.connect(self._on_phase_done)
        self._phase_worker.crashed.connect(self._on_phase_crashed)
        self._phase_worker.finished.connect(self._on_phase_thread_finished)
        self._phase_worker.start()
        self._log.info(
            f"批次前先备份 {len(backup_ids)} 个 mod 的当前版本"
            "（更新前留一份，可到备份管理页恢复）……")

    def _on_phase_one_done(self, done: int, total: int, ok: bool,
                           line: str) -> None:
        (self._log.ok if ok else self._log.warn)(
            f"备份进度 {done}/{total}：{line}")

    def _on_phase_done(self, failed: list, stopped: bool) -> None:
        """备份阶段收尾 → 剔除备份失败的条目，接着开下载批次。
        手动停止 / 全部备份失败时不开批次：发一个收尾汇总让卡片和
        模块页如实记录（批次没有开始，没有任何内容被改动）。"""
        if self._pending_batch is None:
            return  # 双保险：正常流程到不了这里
        app_id, mod_ids = self._pending_batch
        self._pending_batch = None
        failed_ids = [mid for mid, _line in failed]
        self._phase_backup_failed = list(failed_ids)
        for mid, line in failed:
            self._log.warn(f"备份失败，跳过下载（原内容未动）：{line}")
        if stopped:
            self._log.info("备份阶段已手动停止：批次没有开始")
            self.batch_done.emit({
                "total": len(mod_ids), "ok": [], "failed": [],
                "timeout": [], "stopped": True, "error": None,
                "backup_failed": failed_ids})
            return
        if not failed_ids:
            self._launch(app_id, mod_ids)
            return
        remaining = [i for i in mod_ids if i not in set(failed_ids)]
        if not remaining:
            self._log.error("所有条目备份失败，批次没有开始（原内容都未动）")
            self.batch_done.emit({
                "total": len(mod_ids), "ok": [], "failed": [],
                "timeout": [], "stopped": False,
                "error": "所有条目备份失败，批次没有开始（原内容都未动）",
                "backup_failed": failed_ids})
            return
        self._log.info(
            f"备份阶段完成：{len(remaining)}/{len(mod_ids)} 个继续下载，"
            f"{len(failed_ids)} 个因备份失败跳过")
        self._launch(app_id, remaining)

    def _on_phase_crashed(self, message: str) -> None:
        """备份阶段的预期外异常：如实上报并收尾（不开下载批次），
        不让模块页的卡片停在"进行中"。"""
        self._pending_batch = None
        self._log.error(message)
        self.batch_done.emit({
            "total": 0, "ok": [], "failed": [], "timeout": [],
            "stopped": False, "error": f"备份阶段异常中断：{message}",
            "backup_failed": []})

    def _on_phase_thread_finished(self) -> None:
        w = self._phase_worker
        self._phase_worker = None
        if w is not None:
            w.wait()

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
            # 备份阶段的失败清单并进汇总，一次交清（决策 40）
            if self._phase_backup_failed:
                s = dict(s)
                s["backup_failed"] = list(self._phase_backup_failed)
                self._phase_backup_failed = []
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
                # 拆线完成后再广播：订阅方（主窗口自动复扫）同步执行，
                # 不能让它在旧批次还挂着接线时看到事件
                self.batch_done.emit(s)

    # ---------------- 内部：卡片按钮 ----------------

    def _on_stop(self) -> None:
        if self._phase_worker is not None:
            self._phase_worker.stop()
            self._log.info("已请求停止：当前这个 mod 备完就停，"
                           "下载批次不会开始")
            return
        if self._flow is not None:
            self._flow.stop()
            self._log.info("已请求停止批次：不再发新命令，当前条跑完即收尾")

    def _on_resume(self) -> None:
        if self._flow is not None:
            self._flow.resume()
