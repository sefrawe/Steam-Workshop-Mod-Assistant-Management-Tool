"""异常处理功能模块
"""
"""
六类异常的汇聚诊断台：一键把当前档案的异常全查一遍，按类分桶
展示，每个桶给"发生了什么 + 该去哪修"的引导（决策 23⑥ 的六类
分类学落到界面上）。与账实核验页的分工：核验页管"账↔盘逐条
对账"，本页管"全档案异常总览与处置引导"。

检测分两档：
- 本地快检（【开始检测】按钮）：纯离线毫秒级，同步执行——与
  扫描本地/账实核验同理，不值得开线程。覆盖桶①②③与桶④的
  账本半边（workflows/exceptionFlow.detect_local）。
- 联网深度检测（【联网深度检测】按钮）：后台线程重查库内全部
  未删除 mod 的远端实况（failed 照查——就是它失效才要盯），
  补桶④另一半（result=9 确认真失效）与桶⑤（查询失败/疑似
  合集）。**纯读诊断：不写库、不改元数据、不拍快照**——与
  更新检测（维护动作）的分工就在这：想诊断不想动数据时用它。
  疑似合集的"展开入库"也不在此做，引导去更新检测页。

修法引导（本页只做引导与跳转，具体修法归属既有页面）：
- 桶①/桶② → 重新下载：一键跳【下载命令生成】页并勾选这些编号
  （与 mod 库页右键"获取下载命令"、核验页双击行同一份跳转契约）
- 桶③ → 到【mod 库】页点【扫描本地】尝试入账。诚实边界：扫描
  只认账本文件（acf）里的完好条目——损坏/中断的下载和纯手动
  放进去的文件扫不进来，只能人工判断去留
- 桶④ → 本页直接提供「关联替换」（repo.replace_failed_mod）：
  把失效 mod 的备注/颜色/特别关注/快照迁到替代 mod 上，归档记
  "旧→新"证据链；旧记录保留失效状态作历史
- 桶⑤ → 看明细；疑似合集去更新检测页展开，查询失败的先观察
- 桶⑥ → 静态自查文案（多前端环境，无法程序化检测），文案单源
  在引擎（MULTIFRONTEND_NOTE），本页只负责展示

只读红线：本地快检只读不动文件；联网深检只读不写库；写库仅
桶④「关联替换」一处，且逐条确认后才发生。检测前的下载目录
死路径重推导与扫描本地/核验页同款三步（引擎约定：调用方先把
目录核对好再进来）。

线程约定（照更新检测页同款，含两个关键防闪退细节）：
- 只有联网深检进后台线程；分批驱动查询、批间礼貌间隔、批边界
  生效的停止协议，全部照 _CheckWorker 先例
- 线程收尾必须在 finished 信号里先取引用置 None 再 wait()——
  在 succeeded/failed/stopped 的处理函数里直接销毁引用会踩中
  "销毁仍在运行的线程"（进程闪退 0xC0000409）
- 结果归属：本地快检同步执行无归属问题；深检进行中允许切档案，
  结果区不动（仍显示开始检测时那份），渲染一律按 rep.game_id
  取数据，不用当前档案——切了也不串
"""
import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import steamPaths
from core.appSettings import AppSettings
from core.models import Game
from core.steamApiClient import SteamApiClient, SteamApiError, WorkshopItem
from gui.consolePanel import LogBus
from workflows import exceptionFlow

# 与批量下载步骤卡片一致的配色，按用途命名
_C_OK = "#46a758"     # 无异常
_C_FAIL = "#e5484d"   # 失效类（桶④）
_C_WARN = "#f5a623"   # 需要人管
_C_MUTED = "#8a8a8f"  # 说明文字/未检测

_ID_LIMIT = 20  # 卡片里编号列表最多原样列出多少个，超出折成"…"

# 每批查询的条目数：Steam 官方接口单次上限就是 100（与更新检测页同款）
_BATCH = 100


def _item_to_dict(item: WorkshopItem) -> dict:
    """WorkshopItem → 引擎 QueryFn 契约的纯数据条目。
    只转分类要用的三个字段；引擎保持不 import 客户端类型
    （决策 28④ 的注入边界），转换这层"翻译"放在页面侧。"""
    return {"publishedfileid": item.mod_id,
            "result": item.result,
            "file_size": item.file_size}


class _DeepWorker(QThread):
    """联网深检后台线程：分批查询全部条目，每批报告一次进度。
    只发网络请求；分类不在这里做（交回主线程调引擎的
    classify_entries），更不碰数据库。停止协议照 _CheckWorker：
    批边界生效，已查到的数据一律丢弃。"""
    batch_done = Signal(int)   # 已完成查询的条目数（驱动进度条）
    succeeded = Signal(list)   # 全部查完，携带纯数据条目字典列表
    failed = Signal(str)       # 请求层面失败，携带给用户看的原因
    stopped = Signal()         # 用户点了"停止"：正常收场，不算失败

    def __init__(self, mod_ids: list[int], *, interval_ms: int,
                 max_retries: int) -> None:
        super().__init__()
        self._mod_ids = mod_ids
        self._interval_ms = interval_ms
        self._max_retries = max_retries
        self._stop_requested = False

    def stop(self) -> None:
        """请求停止：批边界生效，最多多等一批的时间（同更新检测）。"""
        self._stop_requested = True

    def run(self) -> None:
        client = SteamApiClient(interval_ms=self._interval_ms,
                                max_retries=self._max_retries)
        ids = list(dict.fromkeys(self._mod_ids))  # 去重且保序
        done = 0
        entries: list[dict] = []
        try:
            for start in range(0, len(ids), _BATCH):
                if self._stop_requested:
                    self.stopped.emit()
                    return
                chunk = ids[start:start + _BATCH]
                items = client.query_details(chunk)
                entries.extend(_item_to_dict(i) for i in items)
                done += len(chunk)
                self.batch_done.emit(done)
                if start + _BATCH < len(ids):
                    # 批间礼貌间隔；等待期间无法响应停止，
                    # 最多多等一个间隔（同更新检测页口径）
                    time.sleep(self._interval_ms / 1000)
        except SteamApiError as exc:
            self.failed.emit(str(exc))
            return
        # 兜底：最后一批查询途中点的停，同样按"什么都没发生"处理
        if self._stop_requested:
            self.stopped.emit()
            return
        self.succeeded.emit(entries)


class ExceptionPage(QWidget):
    # 一键跳命令生成页：参数 = 要勾选的 mod id 列表。
    # 与 mod 库页 / 核验页同一份跳转契约，MainWindow 接线到
    # _on_command_gen_requested（切页 + 重新载入 + 只勾这些）
    command_gen_requested = Signal(list)

    def __init__(self, repo, settings: AppSettings,
                 parent: QWidget | None = None, *,
                 log: LogBus | None = None) -> None:
        super().__init__(parent)
        self._repo = repo
        self._settings = settings
        self._log = log or LogBus()  # 没人接也照发（发进空气=无操作）
        self._game: Game | None = None
        self._cards_box: QVBoxLayout | None = None
        # 最近一次本地快检报告：深检结果回来时与它同屏重画
        self._last_local: exceptionFlow.LocalReport | None = None
        self._deep_worker: _DeepWorker | None = None
        self._build_ui()

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("异常处理", self)
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        root.addWidget(title)

        self._game_label = QLabel(self)
        root.addWidget(self._game_label)

        tip = QLabel(
            "一键检查当前档案的六类异常（① 下载未完成 / ② 账实不符 / "
            "③ 孤儿目录 / ④ 远端失效 / ⑤ 查询失败与疑似合集 / "
            "⑥ 多前端环境冲突），按桶给出修法引导。\n"
            "【开始检测】纯离线、毫秒级、只读不动文件；【联网深度"
            "检测】后台重查远端实况（只读不写库），补全桶④⑤。写库"
            "仅桶④的「关联替换」，且逐条确认后才发生。")
        tip.setWordWrap(True)  # 踩坑 ⑨：可能变长的标签一律开换行
        tip.setStyleSheet("color: gray;")
        root.addWidget(tip)

        btn_row = QWidget(self)
        h = QHBoxLayout(btn_row)
        h.setContentsMargins(0, 0, 0, 0)
        self._detect_btn = QPushButton("开始检测", btn_row)
        self._detect_btn.setToolTip(
            "检查当前档案的六类异常（本地部分）。离线毫秒级；"
            "未配置 steamcmd 路径时只查账本半边，并在结果里说明")
        self._detect_btn.clicked.connect(self._start_detect)
        self._deep_btn = QPushButton("联网深度检测", btn_row)
        self._deep_btn.setToolTip(
            "重查库内全部未删除 mod 的远端实况（只读，不写库）：\n"
            "确认哪些真失效（result=9）、列出查询失败与疑似合集。\n"
            "需要网络；每 100 个一批、礼貌限速，耗时取决于 mod 数量。\n"
            "本地快检没跑过时会先自动跑一遍本地部分")
        self._deep_btn.clicked.connect(self._start_deep)
        self._deep_stop_btn = QPushButton("停止", btn_row)
        self._deep_stop_btn.setEnabled(False)
        self._deep_stop_btn.clicked.connect(self._stop_deep)
        self._progress = QProgressBar(btn_row)
        self._progress.setVisible(False)
        h.addWidget(self._detect_btn)
        h.addWidget(self._deep_btn)
        h.addWidget(self._deep_stop_btn)
        h.addWidget(self._progress, 1)
        root.addWidget(btn_row)

        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        # 卡片容器：每次检测整体重建（与批量下载步骤卡片同款做法）
        host = QWidget(self)
        self._cards_box = QVBoxLayout(host)
        self._cards_box.setContentsMargins(0, 4, 0, 4)
        self._cards_box.setSpacing(6)
        root.addWidget(host)
        root.addStretch(1)

    # ---------- 对外（MainWindow 调用） ----------
    def set_game(self, game: Game | None) -> None:
        self._game = game
        if self._deep_worker is not None:
            # 深检进行中：只更新标签，不动结果区——结果仍归属开始
            # 深检时的档案（渲染按 rep.game_id 取数，不会串）。
            # 与更新检测页同款约定
            if game is None:
                self._game_label.setText(
                    "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            else:
                self._game_label.setText(
                    f"当前游戏：{game.name}（{game.app_id}）")
            return
        self._last_local = None
        self._clear_cards()
        self._summary.setText("")
        if game is None:
            self._game_label.setText(
                "当前游戏：（未选择）—— 请先在左上角添加或选择档案")
            self._detect_btn.setEnabled(False)
            self._deep_btn.setEnabled(False)
        else:
            self._game_label.setText(
                f"当前游戏：{game.name}（{game.app_id}）")
            self._detect_btn.setEnabled(True)
            self._deep_btn.setEnabled(True)

    def shutdown(self) -> None:
        """程序退出前的收尾（MainWindow 的页面循环自动发现并调用，
        本页新增此方法不需要改 MainWindow）：停掉可能还在跑的深检
        线程并等它退出。停止协议 = 批边界生效，wait() 最多几秒。"""
        if self._deep_worker is not None:
            self._deep_worker.stop()
            self._deep_worker.wait()

    # ---------- 本地快检 ----------
    def _start_detect(self) -> None:
        if self._game is None or self._deep_worker is not None:
            return
        game = self._latest_game()  # 先核对下载目录（引擎只读）
        if game is None:
            return
        self._run_local(game)

    def _run_local(self, game: Game
                   ) -> exceptionFlow.LocalReport | None:
        """跑本地快检并渲染；失败（acf 损坏）弹窗说明并返回 None。
        本地入口与深检入口共用：深检前没跑过本地时也走这里。"""
        root = self._steamcmd_root()
        if root is None:
            self._log.warn(
                "未设置 steamcmd 程序路径：本次只查账本半边"
                "（盘面部分无法检查，结果页会说明）")
        try:
            report = exceptionFlow.detect_local(self._repo, root, game)
        except ValueError as exc:
            # acf 结构性损坏：引擎口径 = 原样上抛（显式爆炸），这里
            # 接住说人话。修复 = 到设置页核对 steamcmd 程序路径
            self._log.error(f"检测中断：工坊账本文件（acf）损坏：{exc}")
            QMessageBox.critical(
                self, "检测中断",
                f"工坊账本文件（acf）损坏，无法完成检测：\n{exc}\n\n"
                "请到设置页核对 steamcmd 程序路径是否指向正确的"
                " steamcmd 目录。")
            return None
        self._last_local = report
        self._render(report)
        return report

    def _latest_game(self) -> Game | None:
        """检测前核对下载目录（决策 21：它永远可由 steamcmd 位置推导）。
        steamcmd 挪过位置的话旧目录成死路径——按当前位置重新推导并写回。
        与扫描本地/核验页同款三步：推导 → 写库 → 内存同步。"""
        assert self._game is not None
        effective, changed = steamPaths.refresh_download_dir(
            self._game.download_dir,
            self._settings.get("steamcmd_path"),
            self._game.app_id)
        if changed:
            self._repo.update_game(self._game.app_id,
                                   download_dir=effective)
            self._game = self._repo.get_game(self._game.app_id)
            self._log.info(
                f"下载目录已按当前 steamcmd 位置重新推导：{effective}")
        return self._game

    def _steamcmd_root(self):
        """设置页的 steamcmd 程序路径 → steamcmd 根目录（可能为 None，
        引擎会立旗标而不是报错——"没查成"要说明原因）。"""
        return steamPaths.steamcmd_root(self._settings.get("steamcmd_path"))

    # ---------- 联网深度检测 ----------
    def _start_deep(self) -> None:
        if self._game is None or self._deep_worker is not None:
            return
        game = self._latest_game()
        if game is None:
            return
        # 本地快检没跑过就先跑一遍（同步毫秒级）：深检结果要与本地
        # 结果同屏重画，且桶④的"账本半边"来自它
        if self._run_local(game) is None:
            return
        # 深检范围：未删除的全部（failed 照查——就是它失效才要盯，
        # 与更新检测页同一口径）
        mods = self._repo.list_mods(game.app_id)
        ids = [m.mod_id for m in mods if m.status != "deleted"]
        if not ids:
            self._log.warn("当前档案没有可深检的 mod（已删除的除外）")
            return
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._set_deep_running(True)
        self._deep_worker = _DeepWorker(
            ids,
            interval_ms=self._settings.get_int(
                "api_request_interval_ms", 200),
            max_retries=self._settings.get_int("api_max_retries", 3))
        self._deep_worker.batch_done.connect(self._progress.setValue)
        self._deep_worker.succeeded.connect(self._on_deep_succeeded)
        self._deep_worker.failed.connect(self._on_deep_failed)
        self._deep_worker.stopped.connect(self._on_deep_stopped)
        # 线程跑完（无论哪种结局）统一由 finished 收尾销毁。
        # 不能在 succeeded/failed/stopped 的处理函数里把
        # self._deep_worker 置 None——那时线程可能还没完全退出，
        # Python 侧提前销毁会让 Qt 直接终止进程（闪退 0xC0000409）
        self._deep_worker.finished.connect(self._on_deep_finished)
        self._deep_worker.start()
        self._log.info(
            f"联网深度检测开始：{len(ids)} 个 mod（只读不写库）…")

    def _stop_deep(self) -> None:
        if self._deep_worker is not None:
            self._deep_worker.stop()
            self._deep_stop_btn.setEnabled(False)

    def _set_deep_running(self, running: bool) -> None:
        self._detect_btn.setEnabled(not running and self._game is not None)
        self._deep_btn.setEnabled(not running and self._game is not None)
        self._deep_stop_btn.setEnabled(running)
        self._progress.setVisible(running)

    def _on_deep_succeeded(self, entries: list) -> None:
        self._set_deep_running(False)
        if self._last_local is None:
            return  # 理论到不了：深检前必先本地快检；防御一行
        remote = exceptionFlow.classify_entries(entries)
        self._render(self._last_local, remote=remote)
        self._log.ok(
            f"联网深度检测完成：确认失效 {len(remote.invalid)}，"
            f"查询失败 {len(remote.query_failed)}，"
            f"疑似合集 {len(remote.suspected_collection)}")

    def _on_deep_failed(self, message: str) -> None:
        self._set_deep_running(False)
        # 不弹窗（quiet 口径）：网络失败常见，红字汇总 + 日志足够，
        # 本地快检的卡片结果仍然有效、不受影响
        self._summary.setText(
            f"联网深度检测失败：{message}\n（本地快检结果仍有效，"
            "见下方卡片；稍后可重试）")
        self._summary.setStyleSheet(f"color: {_C_FAIL};")
        self._log.error(f"联网深度检测失败：{message}")

    def _on_deep_stopped(self) -> None:
        """用户手动停止：正常收场，不弹窗不标红（与更新检测页同款）。"""
        self._set_deep_running(False)
        self._summary.setText(
            "已停止联网深度检测，本次结果未使用（本地快检结果仍有效）。")
        self._summary.setStyleSheet("color: gray;")
        self._log.info("联网深度检测已手动停止")

    def _on_deep_finished(self) -> None:
        """深检线程跑完的统一收尾（理由见 _start_deep 里的注释，
        照更新检测页 _on_worker_finished 同款）。"""
        w = self._deep_worker
        self._deep_worker = None
        if w is not None:
            w.wait()

    # ---------- 渲染 ----------
    def _render(self, rep: exceptionFlow.LocalReport,
                remote: exceptionFlow.RemoteFindings | None = None
                ) -> None:
        """把检测报告画成桶卡片。

        remote=None：只渲染本地快检结果（桶④仅账本半边、桶⑤待深检）。
        remote 给定：桶④合并本地归档与联网确认（含"恢复正常"提示），
        桶⑤实装，汇总行加深检数字。

        三个"没查成"旗标横幅置顶；盘面桶（①②③）在旗标存在时显示
        "未能检查"而不是绿色"无异常"——"没查"和"查了没有"必须
        一眼可分（决策 28③）。
        渲染一律按 rep.game_id 取数（不是当前档案）——深检进行中
        切过档案也不会串。
        """
        self._clear_cards()
        owner = self._repo.get_game(rep.game_id)
        owner_name = owner.name if owner is not None \
            else f"档案 {rep.game_id}"

        # 库内编号一览：判断桶①哪些编号能直接跳命令页用
        lib_ids = {m.mod_id for m in self._repo.list_mods(rep.game_id)}

        # 各桶问题清单先算好（汇总行要用总数）
        b1_ids = sorted(set(rep.interrupted) | set(rep.empty_dirs))
        disk_ok = not (rep.steamcmd_missing or rep.dead_root)

        # ---- 旗标横幅 ----
        flags: list[str] = []
        if rep.steamcmd_missing:
            flags.append("未配置 steamcmd 程序路径——盘面三桶（①②③）"
                         "无法检测；到设置页填写后重测")
        if rep.dead_root:
            flags.append("下载目录不存在——盘面三桶（①②③）无法检测；"
                         "请检查设置页 steamcmd 路径与该档案是否匹配")
        if rep.acf_missing:
            flags.append("找不到工坊账本文件（acf）——桶①的「下载中断」"
                         "半边无法检测（该游戏可能从未用本机 steamcmd "
                         "下载过）")
        if flags:
            frame, status, box = self._add_card("flags",
                                                "本次未能检查的部分")
            self._set_status(status, f"{len(flags)} 项前提不满足",
                             _C_WARN)
            for f in flags:
                self._add_text(box, frame, "· " + f, _C_WARN)

        # ---- 桶① 下载未完成 ----
        frame, status, box = self._add_card("b1", "桶① 下载未完成")
        if not disk_ok:
            self._set_status(status, "未能检查（原因见横幅）", _C_MUTED)
        elif not b1_ids:
            self._set_status(status, "无异常", _C_OK)
            if rep.acf_missing:
                self._add_text(box, frame,
                               "注：下载中断半边未检查（无 acf，见横幅）")
        else:
            self._set_status(
                status,
                f"{len(b1_ids)} 处（下载中断 {len(rep.interrupted)}"
                f" · 空目录 {len(rep.empty_dirs)}）", _C_WARN)
            self._add_text(box, frame, "编号：" + _ids_text(b1_ids))
            self._add_text(
                box, frame,
                "修法 = 重新下载：中断条目未入账（不影响账本），空目录"
                "是断点残留；下载完成后到【mod 库】页点【扫描本地】入账。")
            b1_in = [i for i in b1_ids if i in lib_ids]
            b1_out = [i for i in b1_ids if i not in lib_ids]
            if b1_in:
                self._add_button(
                    box, frame,
                    f"生成重下命令（{len(b1_in)} 条，可在命令页增减）…",
                    "跳到【下载命令生成】页并勾选这些编号；"
                    "不想下的取消勾选即可，复制前还能再核对预览",
                    lambda _=False, ids=list(b1_in):
                    self._emit_command_gen(ids))
            if b1_out:
                self._add_text(
                    box, frame,
                    f"另有 {len(b1_out)} 个编号不在库中（新下载中途被打断"
                    "的）：想下载它们先到【网址批量导入】添加，再到命令"
                    "生成页勾选。", _C_WARN)

        # ---- 桶② 账实不符 ----
        frame, status, box = self._add_card(
            "b2", "桶② 账实不符（账本记已下载、盘上没有）")
        if not disk_ok:
            self._set_status(status, "未能检查（原因见横幅）", _C_MUTED)
        elif not rep.missing:
            self._set_status(status, "无异常", _C_OK)
        else:
            self._set_status(status, f"{len(rep.missing)} 个", _C_WARN)
            self._add_text(box, frame, "编号：" + _ids_text(rep.missing))
            self._add_text(
                box, frame,
                "目录整个没了（可能被手动删除/杀毒清理）。快捷修法 = "
                "重新下载；「校验重下 validate」「标记为已移除」等完整"
                "三选在【账实核验】页对应行里。")
            self._add_button(
                box, frame,
                f"生成重下命令（{len(rep.missing)} 条）…",
                "跳到【下载命令生成】页并勾选这些编号",
                lambda _=False, ids=list(rep.missing):
                self._emit_command_gen(ids))

        # ---- 桶③ 孤儿目录 ----
        frame, status, box = self._add_card(
            "b3", "桶③ 孤儿目录（盘上有、账本不认识）")
        if not disk_ok:
            self._set_status(status, "未能检查（原因见横幅）", _C_MUTED)
        elif not rep.orphans:
            self._set_status(status, "无异常", _C_OK)
        else:
            self._set_status(status, f"{len(rep.orphans)} 个", _C_WARN)
            self._add_text(box, frame, "编号：" + _ids_text(rep.orphans))
            self._add_text(
                box, frame,
                "到【mod 库】页点【扫描本地】尝试入账。注意：扫描只认"
                "账本文件里的完好条目——损坏/中断的下载和纯手动放进去"
                "的文件扫不进来，确认无用再手动删除。")

        # ---- 桶④ 远端失效 ----
        # 本地账本半边 ∪ 联网确认的失效；联网查过才谈"恢复正常"
        remote_invalid = list(remote.invalid) if remote else []
        all_invalid = sorted(set(rep.failed_ids) | set(remote_invalid))
        newly = [i for i in remote_invalid
                 if i not in set(rep.failed_ids)]
        recovered = sorted(
            set(rep.failed_ids)
            & (set(remote.ok) if remote else set()))
        frame, status, box = self._add_card(
            "b4", "桶④ 远端失效（result=9：作者删除/下架）")
        if not all_invalid:
            if remote is None:
                self._set_status(status, "无（按账本记录）", _C_OK)
                self._add_text(
                    box, frame,
                    "远端实况重查（区分真失效/暂时查不到）点上方"
                    "【联网深度检测】。")
            else:
                self._set_status(
                    status, f"无异常（联网重查 {len(remote.ok) + len(remote.invalid) + len(remote.query_failed)} 个条目）",
                    _C_OK)
        else:
            extra = f"（含联网新确认 {len(newly)} 个）" if newly else ""
            self._set_status(status,
                             f"{len(all_invalid)} 个{extra}", _C_FAIL)
            rec_map = {r.mod_id: r for r in rep.failed_records}
            for mid in all_invalid:
                rec = rec_map.get(mid)
                if rec and rec.reason:
                    reason = rec.reason
                elif mid in remote_invalid:
                    reason = "本次联网确认 result=9"
                else:
                    reason = "（无归档：仅有 failed 状态）"
                row = QWidget(frame)
                rh = QHBoxLayout(row)
                rh.setContentsMargins(0, 0, 0, 0)
                replaced = rec.replaced_by if rec else None
                if replaced:
                    lbl = QLabel(
                        f"mod {mid} · {reason} · 已替换 → mod {replaced}",
                        row)
                    lbl.setWordWrap(True)
                    rh.addWidget(lbl, 1)
                else:
                    lbl = QLabel(f"mod {mid} · {reason}", row)
                    lbl.setWordWrap(True)
                    rh.addWidget(lbl, 1)
                    btn = QPushButton("替换…", row)
                    btn.setToolTip(
                        "关联替换：把这条失效记录的备注/颜色标记/特别"
                        "关注/版本快照迁到替代 mod 上，归档记「旧→新」"
                        "证据链。需要替代 mod 已在库中")
                    btn.clicked.connect(
                        lambda _=False, m=mid: self._replace_failed(m))
                    rh.addWidget(btn)
                box.addWidget(row)
            if recovered:
                self._add_text(
                    box, frame,
                    "本地记为失效、本次远端查询却正常："
                    + _ids_text(recovered)
                    + "（可能是作者恢复了条目——要不要继续用自行判断；"
                      "更新检测会照常盯它们）", _C_WARN)
            self._add_text(
                box, frame,
                "库内筛选「已失败」可看全部失效 mod；执行替换后点"
                "【开始检测】复检。")

        # ---- 桶⑤ 查询失败 / 疑似合集 ----
        frame, status, box = self._add_card(
            "b5", "桶⑤ 查询失败 / 疑似合集")
        if remote is None:
            self._set_status(status, "待联网深度检测", _C_MUTED)
            self._add_text(
                box, frame,
                "本桶需要重新查询远端才能判定，点上方【联网深度检测】。")
        else:
            n5 = len(remote.query_failed) + len(remote.suspected_collection)
            if n5 == 0 and not remote.malformed:
                self._set_status(status, "无异常（本次联网重查全部正常）",
                                 _C_OK)
            else:
                self._set_status(
                    status,
                    f"查询失败 {len(remote.query_failed)}"
                    f" · 疑似合集 {len(remote.suspected_collection)}",
                    _C_WARN)
                if remote.query_failed:
                    self._add_text(
                        box, frame,
                        "查询失败（接口返回异常结果码）："
                        + _ids_text(remote.query_failed))
                    self._add_text(
                        box, frame,
                        "可能是条目被设为私有/地区限制/远端波动——"
                        "稍后重测；确认真失效的会归入桶④。")
                if remote.suspected_collection:
                    self._add_text(
                        box, frame,
                        "疑似合集（远端缺少文件大小）："
                        + _ids_text(remote.suspected_collection))
                    self._add_text(
                        box, frame,
                        "确认与展开入库到【更新检测】页进行"
                        "（对应行有【展开合集…】按钮）——本页是纯读"
                        "诊断，不做登记。")
            if remote.malformed:
                self._add_text(
                    box, frame,
                    f"另有 {len(remote.malformed)} 条响应数据读不动，"
                    "已忽略（极少见，多为接口异常波动）。", _C_WARN)

        # ---- 附加发现：非数字内容 ----
        if rep.non_numeric:
            frame, status, box = self._add_card(
                "nn", "附加发现：非数字内容（编号之外的目录/文件）")
            self._set_status(status,
                             f"{len(rep.non_numeric)} 项", _C_WARN)
            for name in rep.non_numeric[:_ID_LIMIT]:
                self._add_text(box, frame, "· " + name)
            if len(rep.non_numeric) > _ID_LIMIT:
                self._add_text(box, frame,
                               f"…以及另外 "
                               f"{len(rep.non_numeric) - _ID_LIMIT}"
                               " 项（控制台日志不重复列，见上方汇总）")
            self._add_text(
                box, frame,
                "本工具不代删：人工确认后自行处理（下载目录树里只应"
                "存在以编号命名的文件夹）。")

        # ---- 桶⑥ 多前端环境冲突（静态自查清单，文案单源在引擎） ----
        frame, status, box = self._add_card(
            "b6", "桶⑥ 多前端环境冲突（无法自动检测，自查）")
        self._set_status(status, "自查清单", _C_MUTED)
        self._add_text(box, frame, exceptionFlow.MULTIFRONTEND_NOTE)
        # ---- 汇总行 ----
        local_total = (len(b1_ids) + len(rep.missing) + len(rep.orphans)
                       + len(rep.failed_ids) + len(rep.non_numeric))
        if remote is not None:
            remote_total = (len(remote.invalid)
                            + len(remote.query_failed)
                            + len(remote.suspected_collection))
        else:
            remote_total = None

        # v2.10.1 修正：仅本地检测时 remote_total 是 None，原写法
        # `remote_total == 0` 恒不成立 → 全净绿分支永不触发，跌进
        # "部分未能检查"分支、凭空声称有横幅（实测干净库误报）。
        # `not (remote_total or 0)` 把"深检没跑(None)"和"跑了零发现"
        # 一视同仁
        if local_total == 0 and not flags and not (remote_total or 0):
            tail = "（含联网深度检测）" if remote is not None else ""
            self._summary.setStyleSheet(f"color: {_C_OK};")
            self._summary.setText(f"六桶检查完毕{tail}：未发现异常。")
            self._log.ok(f"异常检测（{owner_name}）：未发现异常")
            return
        # 本身零发现、只有未能检查的旗标：专用的友好汇总
        if not local_total and not (remote_total or 0) and flags:
            self._summary.setStyleSheet(f"color: {_C_WARN};")
            self._summary.setText(
                "可判范围内未发现异常；有未能检查的部分，见上方横幅。")
            self._log.warn(
                f"异常检测（{owner_name}）：部分范围未能检查")
            return
        parts: list[str] = []
        if local_total:
            parts.append(f"本地发现 {local_total} 处")
        if remote_total:
            parts.append(f"联网深检确认 {remote_total} 处"
                         "（失效/查询失败/疑似合集）")
        if flags:
            parts.append(f"{len(flags)} 项未能检查（见横幅）")
        self._summary.setStyleSheet(f"color: {_C_WARN};")
        self._summary.setText(
            f"「{owner_name}」：" + "；".join(parts)
            + "。各桶卡片内有明细与修法引导，修完复检。")
        self._log.warn(f"异常检测（{owner_name}）："
                       + "；".join(parts))


    # ---------- 桶④处置：关联替换 ----------
    def _replace_failed(self, old_id: int) -> None:
        """桶④处置（决策 28⑤：处置 UI 随壳）：关联替换。
        交互四步：输入新编号 → 校验 → 确认 → 执行并复检。
        全部异常显式报告，不静默吞。

        注意编号输入不用 QInputDialog.getInt——它封顶 2³¹-1，
        而工坊编号可到约 43 亿，超上限；用 getText 手工解析。"""
        text, ok = QInputDialog.getText(
            self, "关联替换",
            f"mod {old_id} 已失效。\n输入替代 mod 的工坊编号（纯数字）：")
        if not ok:
            return
        try:
            new_id = int(text.strip())
        except ValueError:
            QMessageBox.warning(self, "关联替换", "编号必须是纯数字。")
            return
        if new_id <= 0 or new_id == old_id:
            QMessageBox.warning(
                self, "关联替换",
                "编号无效（需为正整数，且不能与失效编号相同）。")
            return
        mod = self._repo.get_mod(new_id)
        if mod is None:
            QMessageBox.warning(
                self, "关联替换",
                f"编号 {new_id} 不在账本中。\n请先【网址批量导入】或"
                "扫描入账，再回来替换。")
            return
        ret = QMessageBox.question(
            self, "关联替换",
            f"把 mod {old_id} 的失效记录替换为 mod {new_id}"
            f"「{mod.title or ''}」？\n\n"
            "将迁移：备注、颜色标记、特别关注、版本快照；\n"
            "归档中记下「旧 → 新」证据链；\n"
            "旧 mod 保留失效状态，作为历史记录。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            self._repo.replace_failed_mod(old_id, new_id)
        except ValueError as exc:
            # 常见情形：旧条目只有 failed 状态没有归档（归档由更新检测
            # 建立），或替代条目状态异常——原文报给用户，不猜
            self._log.error(
                f"关联替换失败（mod {old_id} → {new_id}）：{exc}")
            QMessageBox.critical(self, "关联替换失败", str(exc))
            return
        self._log.ok(
            f"已关联替换：mod {old_id} → mod {new_id}"
            "（旧记录保留为失效归档；替换后请点【开始检测】复检）")
        self._start_detect()  # 复检刷新：该行应变为"已替换 → 新编号"

    # ---------- 跳转命令生成页 ----------
    def _emit_command_gen(self, ids: list[int]) -> None:
        self._log.info(
            f"跳转命令生成页并勾选 {len(ids)} 个编号（异常修复重下）")
        self.command_gen_requested.emit(list(ids))

    # ---------- 卡片基建（样式与批量下载步骤卡片同款） ----------
    def _clear_cards(self) -> None:
        if self._cards_box is None:
            return
        while self._cards_box.count():
            item = self._cards_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _add_card(self, name: str, title: str
                  ) -> tuple[QWidget, QLabel, QVBoxLayout]:
        """造一张桶卡片，返回（卡框, 状态行, 内容布局）。
        样式选择器限定到本框（QFrame#card_xxx）：QLabel 也是 QFrame
        子类，不限定的边框会画到卡里每行字上——批量下载步骤卡片
        踩过的同款坑，照抄其处理。"""
        frame = QFrame(self)
        frame.setObjectName(f"card_{name}")
        frame.setStyleSheet(
            f"QFrame#card_{name} {{ border: 1px solid #3a3a3a;"
            f" border-radius: 6px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("border:none; font-weight:600;")
        status = QLabel("", frame)
        status.setWordWrap(True)
        status.setStyleSheet("border:none;")
        box.addWidget(title_lbl)
        box.addWidget(status)
        self._cards_box.addWidget(frame)
        return frame, status, box

    def _set_status(self, status: QLabel, text: str, color: str) -> None:
        status.setText(text)
        status.setStyleSheet(f"border:none; color:{color};")

    def _add_text(self, box: QVBoxLayout, frame: QWidget, text: str,
                  color: str = _C_MUTED) -> None:
        lbl = QLabel(text, frame)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"border:none; color:{color}; font-size:12px;")
        box.addWidget(lbl)

    def _add_button(self, box: QVBoxLayout, frame: QWidget, text: str,
                    tooltip: str, on_click) -> None:
        btn = QPushButton(text, frame)
        btn.setToolTip(tooltip)
        btn.clicked.connect(on_click)
        row = QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)  # 按钮保持自然宽度，不横向撑满
        box.addLayout(row)


def _ids_text(ids: list[int], limit: int = _ID_LIMIT) -> str:
    """编号清单 → 展示文本：最多列 limit 个，超出折成"…"。"""
    head = "、".join(str(i) for i in ids[:limit])
    if len(ids) > limit:
        head += f" …（共 {len(ids)} 个）"
    return head
