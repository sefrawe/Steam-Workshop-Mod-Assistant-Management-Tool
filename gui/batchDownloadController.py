"""批量下载控制器
"""
"""
gui/batchDownloadController.py · 「下载批次」的大脑：把终端、步骤卡片、
备份引擎、Steam 批查、账本收尾链串成一条流水线。

与各方的分工（谁有谁的领地）：
- core/batchDownloadFlow：状态机——只管"逐条发命令、收结论、报汇总"，
  不碰数据库不碰网络；
- 本控制器：流程之外的一切——开工检查、开批前备份（V1 决策 40 保留）、
  触发值补采（D3）、收尾链（剔黑→批查→落判决→弹确认清单，D4/D11/D39）、
  僵尸批次防线、卡片行右键动作与重试；
- StepCardList：只画界面，按钮/右键动作以信号（ids_action 等）发来，
  实现全在本文件；
- MainWindow：受理"终端转批次"请求（本控制器只提供 start_batch，
  受不理裁决在主窗口）。

v2 判决制的两条铁律在本文件的落点：
- R18（写入值永不高估）：版本怎么写由 flow.resolve_written 算（D4
  分支全项目唯一实现），本控制器只准调用、不准另写判定；
- R17（确认门）：本控制器只落 pending 判决行（record_verdicts），
  绝不直接写 mods 表的版本字段——真正的入账在确认清单/入账中心
  由用户点过头之后（confirm_items）。

触发值（D3 两来源）在本控制器的处理：
- 检测页触发（trigger_versions 非空）→ 原样交给 flow；
- 手动/终端粘贴（None）→ 开批前先到 Steam 匿名接口补采一轮
  （此时下载还没开始，查到的 time_updated 与检测页看到的同性质：
  早于下载、≤ 盘上内容，满足触发值的时序保证，可作触发值用）；
  补采失败不拦批次——该条按无凭证处理，收尾写成版本未知（照发车，
  D3② 原文）。

工作线程纪律：备份阶段（robocopy 很慢）与两次批查（网络）都在
QThread 里跑；线程只调 BackupManager / SteamApiClient（前者写库沿
备份轮先例：账本连接允许跨线程、全部即时提交），零控件零 Qt 对象，
结果一律走信号回主线程。shutdown 时等不到的线程交 core.netGate.park
保活（绝不销毁运行中的线程）。

下载完成必弹日志（用户拍板）：批次汇总走 LogBus（ok/error 级别），
控制台"运行日志"页按设置自动弹出；确认清单随后由 MainWindow 弹出
（confirmation_ready 信号）。

说明：本文件按计划书 v1.3 点名的 V1 机制清单重建（决策 19/40/94/95、
僵尸批次防线、行右键与重试），非 V1 原文逐字迁移；语义出入以 V1
原文核对后为准。
"""
from types import SimpleNamespace
from core import inventoryFlow

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from core import constants, netGate
from core.urlParser import WORKSHOP_URL_TEMPLATE

from core.backupManager import BackupManager, steamcmd_running
from core.batchDownloadFlow import BatchDownloadFlow, resolve_written
from core.modRepository import ModRepository
from core.steamApiClient import SteamApiClient, SteamApiError
from gui.logBus import LogBus
from gui.modFolderOpener import open_mod_folder
# 备份引擎的保留策略参数在 _BackupPhaseWorker 构造时从设置页现读：
# backup_keep_per_mod / backup_total_quota_gb 两键（core/appSettings.DEFAULTS）。

# 批查互斥占用名：netGate 的 owner 是任意字符串，这里不冒用
# "更新检测"的名字（占用提示要说实话）。
_GATE_OWNER = constants.NET_GATE_BATCH_QUERY  # 批查互斥占用名（constants 单源）


class _BackupPhaseWorker(QThread):
    """开批前的备份阶段（V1 决策 40 保留）：逐条备份现有旧内容，
    防止"更新下载把存档配套的旧版本覆盖掉"。在 flow 发车之前跑，
    steamcmd 此时已启动但还没收到下载命令——正是备份需要的空闲窗口；
    备份读旧文件、下载随后写新文件，两边不抢。

    剔除规则（决策 40）：
    - 下载目录里没有该 mod 的旧文件夹 → 无旧可护，跳过不算失败，
      照常下载（没东西可损失）；
    - 版本未知（confirmed_version 为空）→ 引擎拒绝备份（无法命名
      版本目录），剔除出下载清单——先到库页右键「设定本地版本…」；
    - 备份真失败（空间不足/robocopy 报错）→ 剔除出下载清单——
      备份失败意味着环境有问题，覆盖旧版本风险太大，宁可不下。
    """

    item_done = Signal(int, bool, str)   # mod_id, 是否继续下载, 说明
    phase_done = Signal(int, int)        # (备份成功数, 剔除数)

    def __init__(self, repo: ModRepository, game, mod_ids: list[int],
                 settings, parent=None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._game = game
        self._ids = list(mod_ids)
        # steamcmd 路径 + 保留策略两键，构造时读一次即可：备份阶段
        # 生命周期 = 一次批次，中途改设置下一批生效
        if settings is not None:
            self._steamcmd_path = str(settings.get("steamcmd_path") or "").strip()
            # 每个 mod 保留几份（默认 3；兜底至少 1）
            self._keep = max(1, int(settings.get_int("backup_keep_per_mod", 3)))
            # 全部备份总量上限（GB → 字节；填 0 = 不限）
            gb = int(settings.get_int("backup_total_quota_gb", 10))
            self._quota = gb * 2 ** 30 if gb > 0 else None
        else:
            self._steamcmd_path = ""
            self._keep = 1
            self._quota = None

    def run(self) -> None:
        # 按次构造引擎（同备份页模式：参数每次从设置现读）
        mgr = BackupManager(self._repo, keep_per_mod=self._keep,
                            quota_bytes=self._quota,
                            steamcmd_path=self._steamcmd_path)

        from pathlib import Path
        ok_n = 0
        dropped = 0
        for mid in self._ids:
            folder = Path(self._game.download_dir or "") / str(mid)
            if not folder.is_dir():
                # 没有旧内容：没有需要保护的东西，不算失败
                self.item_done.emit(mid, True, "无旧版本，跳过备份")
                continue
            report = mgr.backup_mod(mid, note="下载前自动备份")
            if report.ok:
                ok_n += 1
                self.item_done.emit(mid, True, "已备份旧版本")
            else:
                dropped += 1
                self.item_done.emit(mid, False, report.error or "备份失败")
        self.phase_done.emit(ok_n, dropped)


class _QueryWorker(QThread):
    """Steam 匿名接口批查（开批前补采触发值 / 收尾核对现场值共用）。
    result 字典：{mod_id: (time_updated, title, file_size)}；
    查不到/失效条目（result != 1）记 (None, None, None)——是查询结果
    不是错误；整批网络失败 → result 为空字典 + error 有话。
    netGate 互斥：拿不到名额 → 全部按查询失败处理（不排队不重试——
    批查是收尾的助攻，不值得为它卡住批次）。
    """

    done = Signal(dict, str)   # (result 字典, 错误说明——无错为空串)

    def __init__(self, mod_ids: list[int], parent=None) -> None:
        super().__init__(parent)
        self._ids = list(dict.fromkeys(int(i) for i in mod_ids))

    def run(self) -> None:
        result: dict[int, tuple] = {}
        error = ""
        owner = netGate.try_acquire(_GATE_OWNER)
        if owner is not None:
            self.done.emit({}, f"联网查询名额被「{owner}」占用")
            return
        try:
            client = SteamApiClient()
            for it in client.query_details(self._ids):
                if it.mod_id is None or it.result != 1:
                    # 失效/私有/查无此条：按"查询失败"记（D4 走触发值路径）
                    key = it.mod_id if it.mod_id is not None else -1
                    result[key] = (None, None, None)
                else:
                    result[it.mod_id] = (it.time_updated, it.title,
                                         it.file_size)
        except SteamApiError as exc:
            error = str(exc)   # 整批失败：result 保持空 → 全按失败走
        finally:
            netGate.release(_GATE_OWNER)
        self.done.emit(result, error)
class _InventoryWorker(QThread):
    """⑮ 批次收尾自动盘点线程：scan_game 只读盘面 + 落候选提案 +
    回填大小。放线程防大库目录步行卡住确认弹窗；写库走 repo 事务
    （要么全成要么回滚），跨线程沿本文件备份线程先例。零控件。"""
    done = Signal(object, str)   # (ScanReport | None, 错误说明)

    def __init__(self, repo, game, steamcmd_path: str, parent=None) -> None:
        super().__init__(parent)
        self._repo, self._game, self._cmd = repo, game, steamcmd_path

    def run(self) -> None:
        try:
            report = inventoryFlow.scan_game(
                self._repo, self._game, steamcmd_path=self._cmd)
        except (OSError, ValueError) as exc:
            self.done.emit(None, str(exc))
            return
        self.done.emit(report, "")


class BatchDownloadController(QWidget):
    """持有当前批次的一切状态；一次实例服务整个程序生命周期
    （批次本身由 flow 按次建）。"""

    # 收尾链落账完毕 → 主窗口弹确认清单（带该档案的待确认行）
    confirmation_ready = Signal(list)

    def __init__(self, repo: ModRepository, settings, log: LogBus,
                 *, console, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log
        self._console = console          # ConsolePanel：自取 terminal / step_list
        self._terminal = console.terminal
        self._step_list = console.step_list
        # 卡片的【停止批次】【继续批次】→ 流程的温和停止 / 续跑。
        # 接线住控制器：ConsolePanel 只画界面不认识 flow（解耦约定）
        console.step_list.stop_requested.connect(self._on_card_stop)
        console.step_list.resume_requested.connect(self._on_card_resume)

        self._flow: BatchDownloadFlow | None = None
        self._game = None                # 开批时锁定的档案（切界面不影响）
        self._triggers: dict[int, int] = {}   # 本轮触发值表（重试也用它）
        self._backup_worker: _BackupPhaseWorker | None = None
        self._query_worker: _QueryWorker | None = None
        self._scan_worker: _InventoryWorker | None = None

        self._pending_ids: list[int] = []     # 备份阶段后的实际下载清单
        self._dropped_ids: set[int] = set()  # 备份阶段剔除的编号（批内有效）


    # ---------------- 状态查询 ----------------
    def is_busy(self) -> bool:
        """是否有批次/阶段在跑（主窗口裁决转批次、关窗确认都用）。"""
        return self._flow is not None and self._flow.state != "done"

    # ---------------- 开批入口 ----------------
    def start_batch(self, app_id: int, mod_ids: list[int],
                    backup_first: bool | None = None,
                    trigger_versions: dict[int, int] | None = None) -> bool:
        """开批唯一入口（主窗口裁决后调用；重试也走这里）。
        backup_first=True → 先备份旧版本（失败条目剔除）；None/False
        → 直接进下载阶段。trigger_versions 见文件头"触发值"。
        """
        if self.is_busy():
            self._log.warn("已有批次在跑：等它结束或先停止再开新批次")
            return False
        ids = list(dict.fromkeys(int(i) for i in mod_ids))
        if not ids:
            return False
        game = self._repo.get_game(app_id)
        if game is None:
            self._log.error(f"游戏档案 {app_id} 不存在，无法开批")
            return False
        if not self._terminal.is_busy():
            self._log.warn("steamcmd 未启动：请先到「steamcmd 终端」页"
                           "点【启动 steamcmd】，再开下载批次")
            return False
        if not str(game.download_dir or "").strip():
            self._log.error("该档案没有设置下载目录：请到「游戏 → 编辑档案」"
                            "补齐后再下载")
            return False

        # 锁定本轮上下文：之后用户切档案只影响界面，不影响批次
        self._game = game
        self._triggers = {int(k): int(v) for k, v in
                          (trigger_versions or {}).items()}
        self._step_list.reset_for_batch(
            len(ids), has_login=self._has_login_cmd())
        self._log.info(f"下载批次开始：{game.name}（{app_id}），"
                       f"共 {len(ids)} 条")

        if backup_first:
            self._pending_ids = ids
            self._dropped_ids = set()  # 新批次清空上批剔除记录，防串批
            self._start_backup_phase(ids)
            return True

        return self._begin_download_phase(ids)

    def _has_login_cmd(self) -> bool:
        raw = (str(self._settings.get("steamcmd_login_cmd") or "")
               if self._settings is not None else "")
        return bool(raw.strip())

    # ---------------- 阶段一：备份（决策 40） ----------------
    def _start_backup_phase(self, ids: list[int]) -> None:
        # 决策 40 的"steamcmd 空闲窗口"：批次未发车 = steamcmd 空闲，
        # 但若用户还在软件外手动跑 steamcmd，备份可能拿到不完整副本
        # ——只警告不拦（R7 口径：备份的对称建议）
        if steamcmd_running():
            self._log.warn("检测到软件外另有 steamcmd 在跑：备份的旧版本"
                           "可能不完整（建议等它退出再开批）")
        self._log.info(f"备份阶段：先备份 {len(ids)} 条的现有旧版本…")
        self._backup_worker = _BackupPhaseWorker(
            self._repo, self._game, ids, self._settings)
        self._backup_worker.item_done.connect(self._on_backup_item)
        self._backup_worker.phase_done.connect(self._on_backup_done)
        self._backup_worker.start()
    def _on_backup_item(self, mod_id: int, keep: bool, note: str) -> None:
        if keep:
            self._log.info(f"mod {mod_id}：{note}")
        else:
            # 边收边记进剔除集合：item_done 与 phase_done 两个信号的
            # 到达顺序没有保证，收尾时只读这个集合最稳
            self._dropped_ids.add(mod_id)
            self._log.error(f"mod {mod_id} 已从本批剔除：{note.splitlines()[0]}")

    def _on_backup_done(self, ok_n: int, dropped: int) -> None:
        self._backup_worker = None
        # 幸存者 = 备份清单 − 剔除集合（集合在 _on_backup_item 边收边记，
        # 两个信号的先后顺序不影响这里的结果）
        survivors = [m for m in self._pending_ids
                     if m not in self._dropped_ids]
        if not survivors:
            self._log.error("备份阶段后没有可下载的条目，批次结束")
            self._step_list.handle_event(
                {"type": "batch_done", "summary": {
                    "total": len(self._pending_ids), "ok": [],
                    "failed": [], "timeout": [], "stopped": False,
                    "error": "全部条目备份失败被剔除"}})
            return
        if dropped:
            self._log.warn(f"备份完成：成功 {ok_n} 条，剔除 {dropped} 条，"
                           f"继续下载 {len(survivors)} 条")
        else:
            self._log.ok(f"备份完成：{ok_n} 条旧版本已保护，开始下载")
        self._begin_download_phase(survivors)


    # ---------------- 阶段二：补采触发值（D3） ----------------
    def _begin_download_phase(self, ids: list[int]) -> bool:
        missing = [m for m in ids if m not in self._triggers]
        if missing:
            # 手动/终端粘贴批次：开批前补采一轮（此时还没下载，查到的
            # 版本满足触发值时序）。失败不拦批次——查不到的按无凭证走
            self._log.info(f"正在核对 {len(missing)} 条的远端版本"
                           "（作为本批的版本凭证）…")
            self._query_worker = _QueryWorker(missing)
            self._query_worker.done.connect(
                lambda result, err: self._on_prefetch_done(ids, result, err))
            self._query_worker.start()
            return True
        return self._launch_flow(ids)

    def _on_prefetch_done(self, ids: list[int], result: dict,
                          error: str) -> None:
        self._query_worker = None
        if error:
            # 补采失败照发车（D3②）：全部按无凭证处理，收尾写版本未知
            self._log.warn(f"远端版本核对失败（{error}）："
                           "本批将没有版本凭证，成功条目按「版本未知」入账")
        else:
            got = 0
            for mid, (tu, _t, _s) in result.items():
                if mid in ids and tu is not None:
                    self._triggers[mid] = tu   # 只补表里没有的键
                    got += 1
            self._log.info(f"版本凭证就绪：{got}/{len(ids)} 条"
                           "（查不到的条目照常下载）")
        self._launch_flow(ids)

    # ---------------- 阶段三：发车 ----------------
    def _launch_flow(self, ids: list[int]) -> bool:
        # 卡片必须先于 start 就位：start 内部可能同步发事件
        # （第一条命令发不出去时 batch_done 当场就来）
        login_cmd = None
        if self._has_login_cmd():
            raw = str(self._settings.get("steamcmd_login_cmd") or "")
            first = next((ln.strip() for ln in raw.splitlines() if ln.strip()),
                         None)
            login_cmd = first   # 决策 19：设置里的命令原样发送，不解析
        self._flow = BatchDownloadFlow(
            self._game.app_id, ids,
            send_command=self._terminal.send_command,
            on_event=self._on_flow_event,
            trigger_versions=self._triggers)
        if not self._flow.start(login_cmd=login_cmd):
            self._flow = None
            return False
        return True

    # ---------------- 流程事件 → 卡片 / 终端信号 ----------------
    def _on_flow_event(self, ev: dict) -> None:
        self._step_list.handle_event(ev)
        if ev.get("type") == "need_login":
            self._log.warn(str(ev.get("note") or "需要手动登录"))
        elif ev.get("type") == "batch_done":
            self._on_batch_done(ev.get("summary") or {})
    def _on_card_stop(self) -> None:
        """卡片【停止批次】：温和停止——不再发新命令，正在下载的
        那条让它跑完再收尾。与终端【停止 steamcmd】不同：不杀进程，
        不需要确认弹窗（卡片 tooltip 已说明）。"""
        if self._flow is not None and self._flow.state != "done":
            self._flow.stop()

    def _on_card_resume(self) -> None:
        """卡片【继续批次】：NEED_LOGIN 时手动催流程继续
        （登录成功本会自动续批，这是用户手动兜底入口）。"""
        if self._flow is not None:
            self._flow.resume()

    def on_process_exited(self, _status: int) -> None:
        """僵尸批次防线：steamcmd 进程没了，当前条目的结论永远不会来
        ——强制中止 flow（在途条目记失败），收尾链照走（已下的条目
        照常入账，绝不白下）。"""
        if self._flow is not None and self._flow.state != "done":
            self._log.warn("steamcmd 已退出：本批剩余条目中止"
                           "（已下载的条目照常进入收尾）")
            self._flow.abort("steamcmd 已退出")

    # ---------------- 收尾链（D4/D11/D39） ----------------
    def _on_batch_done(self, s: dict) -> None:

        self._flow = None
        ok = list(s.get("ok") or [])
        failed = list(s.get("failed") or [])
        timeout = list(s.get("timeout") or [])
        game = self._game
        self._game = None
        if game is None:            # 防御：理论不可达
            return

        # ① 下载完成弹日志（用户拍板）：汇总必达，控制台按设置自动弹
        err = s.get("error") or ""
        if err:
            self._log.error(f"下载批次出错：{err}")
        else:
            head = "下载批次结束：成功 {} · 失败 {} · 超时 {}（共 {}）".format(
                len(ok), len(failed), len(timeout), s.get("total", 0))
            if s.get("stopped"):
                self._log.warn(head + "——已停止")
            elif not failed and not timeout:
                self._log.ok(head)
            else:
                self._log.warn(head + "——可用卡片上的【重试失败与超时条目】再跑")

        # ② 失败/超时也落判决史（不进确认队列，repo 层按 kind 过滤；
        #    触发值带上留档——判决史要说清"当时想下的是哪个版本"）
        history = []
        for r in failed:
            history.append({"mod_id": r.mod_id, "kind": "fail",
                            "game_id": game.app_id,
                            "version_trigger": r.trigger,
                            "note": r.reason or ""})
        for r in timeout:
            history.append({"mod_id": r.mod_id, "kind": "timeout",
                            "game_id": game.app_id,
                            "version_trigger": r.trigger,
                            "note": "Timeout"})
        if history:
            self._repo.record_verdicts(history)

        # ③ 没有成功条目：没有可确认的东西，到此为止
        if not ok:
            self._log.info("本批没有成功条目：无待确认项")
            return

        # ④ 剔黑名单（D11 三拦之一）：黑名单条目已下载的不拦（下载是
        #    盘面行为），但绝不落待确认——防手滑确认入账
        purged = self._repo.filter_purged([r.mod_id for r in ok])
        net_ok = [r for r in ok if r.mod_id not in purged]
        if purged:
            self._log.warn(f"{len(purged)} 条在已清账黑名单，"
                           "不列入待确认清单（编号："
                           + "、".join(str(m) for m in sorted(purged)) + "）")
        if not net_ok:
            self._log.info("成功条目全部在黑名单：无待确认项")
            return

        # ⑤ 收尾批查（下载后的现场核对）：成功条目逐条拿现场版本，
        #    供 resolve_written 判 verified / 降级 / 版本未知
        self._log.info(f"正在核对 {len(net_ok)} 条成功条目的远端版本…")
        self._query_worker = _QueryWorker([r.mod_id for r in net_ok])
        self._query_worker.done.connect(
            lambda result, err: self._on_final_query(
                game, net_ok, result, err))

    def _on_final_query(self, game, net_ok: list, result: dict,
                        error: str) -> None:
        self._query_worker = None
        if error:
            # 批查失败不拦入账（D4c）：全部按查询失败走，触发值照写
            self._log.warn(f"远端版本核对失败（{error}）：成功条目将按"
                           "「未验证」入账，下轮更新检测会重新核对")

        rows = []
        for r in net_ok:
            q, title, size = result.get(r.mod_id, (None, None, None))
            written, source = resolve_written(r.trigger, q)
            rows.append({
                "mod_id": r.mod_id,
                "kind": "success",
                "game_id": game.app_id,
                "version_trigger": r.trigger,
                "version_query": q,          # 现场值留档，对账有据
                "version_written": written,  # D4 分支结果（R18：≤ 触发值）
                "title": title,              # D39：落 pending 顺手存
                "file_size": size or r.size_bytes,  # 批查优先，退下载实测
                "source": source,
            })
        self._repo.record_verdicts(rows)
        # 本批已给出更优判决（success 待确认）：早先扫描留下的同编号
        # 未确认 claim 提案就此作废（提案跟随更优事实）——只删未确认
        # claim 行，确认行与其他 kind 不动（repo 契约原样）
        self._repo.drop_stale_claims(game.app_id,
                                     [r["mod_id"] for r in rows])
        pending = self._repo.pending_confirmations(game.app_id)
        n_new = len(rows)
        self._log.ok(
            f"{n_new} 条已列入待确认清单（黑名单已剔除）——"
            "请核对后点【确认入账】把版本登记进账本；"
            "也可以先不管，之后在「入账中心」处理")
        # ⑮ 批次收尾自动盘点（D23/D36，默认开）：落 pending 之后、
        # 线程跑不等它——确认清单即刻弹，盘点完成进运行日志，候选在
        # 入账中心②区等（页面进页自动刷新）。刚下载的编号已由上面
        # 的去重口径跳过，这里捞的是账外内容 + 大小回填 + 失效清理
        if self._settings.get_int("auto_inventory_after_batch", 1) != 0:
            if self._scan_worker is not None:
                self._log.info("上一轮收尾盘点还在跑：本批跳过自动盘点"
                               "（需要时到入账中心手动扫）")
            else:
                cmd = str(self._settings.get("steamcmd_path") or "")
                self._scan_worker = _InventoryWorker(self._repo, game, cmd)
                self._scan_worker.done.connect(self._on_auto_inventory_done)
                self._scan_worker.start()
        if pending:
            self.confirmation_ready.emit(pending)
    def _on_auto_inventory_done(self, report, error: str) -> None:
        """⑮ 收尾自动盘点回执：只进运行日志不弹窗——批次主结果
        （确认清单）已在用户眼前，盘点是顺手的卫生工作。"""
        self._scan_worker = None
        if error:
            self._log.warn(f"收尾自动盘点失败（不影响批次结果）：{error}")
            return
        if report is not None:
            self._log.info(
                f"收尾自动盘点：盘上 {report.folders_found} 个 mod 文件夹，"
                f"新候选 {len(report.candidates_new)} 条，"
                f"清失效 {len(report.stale_removed)} 条，大小回填 "
                f"{report.sizes_backfilled} 条；{report.acf_note}")

    # ---------------- 卡片行右键 / 重试（ids_action 实现） ----------------
    def on_ids_action(self, action: str, mod_ids: list[int]) -> None:
        """StepCardList.ids_action 的落点（MainWindow 接线过来）。"""
        if action == "copy":
            QApplication.clipboard().setText(
                "\n".join(str(m) for m in mod_ids))
            self._log.info(f"已复制 {len(mod_ids)} 个编号到剪贴板")
        elif action == "open_pages":
            for mid in mod_ids:
                QDesktopServices.openUrl(QUrl(WORKSHOP_URL_TEMPLATE
                                              .format(mid)))

        elif action == "open_folders":
            game = self._game or self._current_game_fallback()
            if game is None:
                self._log.warn("没有当前游戏档案，无法定位 mod 文件夹")
                return
            for mid in mod_ids:
                # 批次条目可能不在账本（D39）：喂只带 mod_id 的兜底对象
                open_mod_folder(self.window(), game,
                                SimpleNamespace(mod_id=mid), log=self._log)
        elif action == "retry":
            if not mod_ids:
                return
            if self.is_busy():
                self._log.warn("已有批次在跑：先把当前批次停止/等它结束")
                return
            game = self._current_game_fallback()
            if game is None:
                self._log.error("重试需要游戏档案：请先在左上角选择档案")
                return
            # 重试不重复备份（首轮已备份过的旧版本还在备份区）；
            # 触发值沿用首轮记录（同一批该下的版本不变）
            triggers = {m: self._triggers[m] for m in mod_ids
                        if m in self._triggers}
            self.start_batch(game.app_id, mod_ids,
                             backup_first=False, trigger_versions=triggers)

    def _current_game_fallback(self):
        """行右键动作发生时批次多已结束（self._game 已清）——
        此时动作服务于"看看刚才下的东西"，用主窗口的当前档案。
                读取方式：self.window() 即 MainWindow，直接取它的 _current_game。"""

        win = self.window()
        return getattr(win, "_current_game", None) if win else None

    # ---------------- 停止 / 退出守卫 / 收尾 ----------------
    def confirm_stop(self) -> bool:
        """terminal.set_stop_guard 注入的守卫：批次在跑时停止 steamcmd
        = 中止批次，先问过用户。返回 True 放行停止。"""
        if not self.is_busy():
            return True
        ret = QMessageBox.question(
            self.window(), "下载批次进行中",
            "steamcmd 一旦停止，本批剩余条目将不再执行（已下载的条目"
            "照常进入收尾确认）。\n\n确定要停止 steamcmd 吗？")
        return ret == QMessageBox.StandardButton.Yes

    def confirm_exit(self) -> bool:
        """关窗前的批次确认（closeEvent 调用）。文案与 confirm_stop
        分开——关窗的后果是整个程序退出。"""
        if not self.is_busy():
            return True
        ret = QMessageBox.question(
            self.window(), "下载批次进行中",
            "退出会中止当前下载批次（已下载的条目照常进入收尾确认，"
            "steamcmd 登录缓存会保留）。\n\n确定要退出吗？")
        return ret == QMessageBox.StandardButton.Yes

    def shutdown(self) -> None:
        """程序退出收尾：批查线程等 1.5 秒，等不到 park 保活
        （线程只发网络请求不写库，强杀零数据损失——netGate 同款口径）。
        备份线程同口径（robocopy 是独立子进程，进程退场自然带走）。"""
        for worker in (self._query_worker, self._backup_worker,
                       self._scan_worker):

            if worker is not None and worker.isRunning():
                worker.wait(1500)
                netGate.park(worker)
