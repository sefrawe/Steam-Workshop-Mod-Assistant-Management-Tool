"""批量下载编排
"""
"""
把"一条条粘贴 workshop_download_item、眼睛盯着输出、看到提示符
再粘下一条"的手工流程自动化：登录（可选）→ 逐条发下载命令 →
收结论 → 全部完成后给汇总。

v2 判决制增补（相对 v1 的全部差异就这三条，其余逐字保留）：
- 构造参数 trigger_versions：{mod_id: 触发值}。触发值 = 开批前
  检测页看到的远端版本号——"用户当时点的、想下的就是哪个版本"
  的凭证，判决写入分支（计划书 D4）靠它定案。手动批次（命令
  生成页 / 终端粘贴）没有检测环节，传 None，全部条目按无凭证
  处理。
- ItemResult 增加 trigger 字段：每条的触发值随收尾汇总带出，
  收尾链（控制器）据此写判决行。状态机本身不碰数据库、不碰
  批查——它只管把批次跑完，把"谁、成没成、凭证是什么"如实
  报出来。
- 模块级纯函数 resolve_written：判决写入分支（D4）的唯一实现，
  给定触发值与批查现场值，算出 (写入版本, 来源)。放 core 做
  纯函数：同样输入必得同样输出，pytest 直测；控制器只准调用、
  不准另写一份——判定公式全项目只此一处，不漂移。

纯逻辑、零 Qt（记事本架构约定：core 不 import gui）。它与终端
的所有来往都通过"注入的回调 + 被接线的入口"完成，本模块自己
不认识任何 Qt 类：

  GUI 侧（控制台/MainWindow）              本模块
  --------------------------------------  --------------------
  terminalDock.send_command     ──注入──▶  send_command 回调
  terminalDock.verdict_emitted  ─接线─▶   on_verdict()
  terminalDock.idle_prompt_seen ─接线─▶   on_idle()
  on_event 回调（更新卡片/写日志）◀──调用──  每个状态变化

为什么"等空闲提示符才发下一条"（本模块最核心的一条设计）：
真实样本里结论会续在"开始下载"同一行（Downloading item …ERROR!
Timeout …），而且大 mod 下载几个小时都正常——用时间判断"这条
完事了"必然误判。唯一可靠的信号是 steamcmd 打出单独一行 Steam>
（空闲提示符），等价于"人眼看到提示符再粘贴"。所以每条的节奏是：
发命令 → 等结论（记账）→ 等空闲提示符 → 发下一条。

状态机（改这里之前先照着画一遍）：

  IDLE ──start()──▶ LOGGING_IN ──登录成功──▶ RUNNING ──队列空──▶ DONE
  │                    │                        ▲
  └─没给登录命令───────┘                        │
              每条：发命令→收结论→等空闲提示符──┘
  RUNNING 中收到"未登录" ──▶ NEED_LOGIN ──resume()/登录成功──▶ RUNNING
  用户 stop()：RUNNING/LOGGING_IN ──▶ STOPPING（当前条收尾后结束）
  WAITING_IDLE/NEED_LOGIN 收到 stop ──▶ 直接 DONE

已知限制（都讨论过，写在这里备忘）：
- 不做超时判死：大 mod 下载几小时正常，强行判死会误杀；真卡死
  由用户手动【停止 steamcmd】兜底，本模块靠"已退出"事件收尾；
- 断线不暂停队列：steamcmd 自己会重试，连上后命令该失败就失败，
  批次结束统一看失败清单重跑；
- 停止是温和的：不再发新命令，已发出的那条让它跑完——强杀
  steamcmd 会丢登录缓存，不划算。
"""
from core import outputAnalyzer

# ---------- 批次状态（模块常量：比较用这里的名，别写裸字符串） ----------
ST_IDLE = "idle"                    # 未开始 / 已结束，可以 start()
ST_LOGGING_IN = "logging_in"        # 已发登录命令，等登录结论
ST_RUNNING = "running"              # 已发某条的下载命令，等结论
ST_WAITING_IDLE = "waiting_idle"    # 当前条已有结论，等空闲提示符发下一条
ST_NEED_LOGIN = "need_login"        # 下载时发现未登录，暂停等用户手动登录
ST_STOPPING = "stopping"            # 用户点了停止：不发新命令，当前条收尾后结束
ST_DONE = "done"                    # 批次结束（正常跑完 / 停止 / 出错）


# ---------- 条目结果（进 summary：GUI 展示与收尾链落账都用它） ----------
class ItemResult:
    """一条 mod 的下载结果。失败时 reason 是 steamcmd 括号里的原文。

    trigger = 该条开跑前的触发值（检测页看到的远端版本号）。
    没有凭证（手动批次 / 触发表里没这个编号）记 None——诚实记录
    "不知道该写哪个版本"，收尾链据此把它写成版本未知态，绝不现
    编一个版本号出来（红线 R18：写入值永不高估）。
    """

    def __init__(self, mod_id: int, ok: bool, reason: str = "",
                 size_bytes: int | None = None,
                 trigger: int | None = None) -> None:
        self.mod_id = mod_id
        self.ok = ok
        self.reason = reason
        self.size_bytes = size_bytes
        self.trigger = trigger


# ---------------------------------------------------------------------------
# 判决写入分支（计划书 D4）——判定公式全项目唯一一份；控制器只准调用，
# 不准另写第二份（两处口径迟早走样）。
#
# 名词先说清：
#   触发值（trigger）— 开批前检测页看到的远端版本号，"用户点的是
#                       哪个版本"的凭证；手动批次没有，为 None。
#   批查现场值（query）— 下载完成后，收尾链到 Steam 匿名接口核对该
#                       条目拿到的 time_updated；查询失败/被拒/没查
#                       为 None。
#
# 分支总表（四种组合全覆盖）：
#   trigger 有 + query == trigger → (trigger, "verified")
#       批查现场与触发值吻合：触发值获得远端背书（D4a）
#   trigger 有 + query != trigger → (trigger, "unverified")
#       远端在"检测→下载完成"之间又更新过：写触发值仍是最保守的
#       选择（见下），但它没被现场吻合背书，来源如实降级——库页
#       灰显，下轮检测自然会再报
#   trigger 有 + query 失败       → (trigger, "unverified")（D4c）
#   trigger 无（无论 query 成败） → (None, "unverified")
#       没有凭证就不写版本（R18）。批查值哪怕拿到了也绝不直接入账：
#       批查发生在下载之后，若远端恰在"下载完成→批查"之间又更新，
#       批查值会比盘上内容新——写它就是高估。触发值之所以可信，
#       正因为它早于下载、只会 ≤ 盘上内容；批查值没有这个时序保证。
#
# 结构保证（R18）：本函数写出的 version_written 要么等于 trigger，
# 要么是 None——"写入值永不高估"由这里一处保证，全项目受益。
# 批查的现场值无论成败都不丢弃：收尾链把它存进判决行 version_query
# 列留档，哪天要对账，原始证据都在。
# ---------------------------------------------------------------------------
def resolve_written(trigger: int | None,
                    query: int | None) -> tuple[int | None, str]:
    """D4 判定分支：→ (写入版本, 来源)。规则见上方总表。"""
    if trigger is None:
        # 无触发值：手动批次没有"点的哪个版本"的凭证，写 NULL =
        # 版本未知态。批查成功与否都不改变这一点（理由见上）。
        return None, "unverified"
    if query is not None and query == trigger:
        # 现场与触发值吻合：背书成立
        return trigger, "verified"
    # 有触发值但批查失败 / 现场不吻合：照写触发值（保守），来源如实
    return trigger, "unverified"


class BatchDownloadFlow:
    """一次批量下载的生命周期。一次实例只跑一批，跑完重新建实例。"""

    def __init__(self, app_id: int, mod_ids: list[int], send_command,
                 on_event=None,
                 trigger_versions: dict[int, int] | None = None) -> None:
        """app_id：当前游戏的 Steam AppId（命令要写在下载命令里）。
        mod_ids：要下载的 mod 编号列表（按此顺序逐条发）。
        send_command：发命令的函数，签名 -> bool——GUI 把
            terminalDock.send_command 注入进来；返回 False 表示
            没发出去（steamcmd 没在跑等）。
        on_event：状态变化回调，签名 -> None——GUI 用它
            更新步骤卡片和写日志。事件字典的 type 见各 _emit 调用处。
        trigger_versions：触发值表 {mod_id: 版本号}（v2 判决制新增）。
            检测页"检测→开批"时传入：每条的值 = 检测时看到的远端
            time_updated。手动批次传 None。本模块只保管、不解释——
            解释权在 resolve_written。
        """
        self._app_id = app_id
        self._send = send_command
        self._on_event = on_event
        # 触发值表（v2）：表里没有的编号按无凭证处理（_trigger_for），
        # 不炸、不编
        self._triggers: dict[int, int] = dict(trigger_versions or {})
        self._pending = list(mod_ids)   # 还没发的编号（队首 = 下一条）
        self._total = len(self._pending)
        self._current: int | None = None  # 已发命令、还没出结论的编号
        self._state = ST_IDLE
        self._ok: list[ItemResult] = []
        self._failed: list[ItemResult] = []
        self._timeout: list[ItemResult] = []
        self._login_cmd: str | None = None
        self._finished_emitted = False  # batch_done 事件只发一次（防御）

    # ---------------- 对外：开始 / 停止 / 继续 ----------------
    def start(self, login_cmd: str | None = None) -> bool:
        """开始批次。给了 login_cmd 就先登录，等登录成功再逐条下载。
        返回是否成功启动（已在跑 / 列表为空 / 第一条命令就发不出去 →
        False；最后这种情况批次会以出错收尾，batch_done 事件照发，
        GUI 靠事件显示失败原因，返回值只说明"没启动起来"）。
        """
        if self._state != ST_IDLE or not self._pending:
            return False
        self._login_cmd = (login_cmd or "").strip() or None
        if self._login_cmd:
            # 登录决策 19：设置里的命令原样发送，不解析、不拼装。
            # R2：登录命令含密码，失败文案必须脱敏
            self._state = ST_LOGGING_IN
            if not self._try_send(
                    self._login_cmd,
                    fail_note="登录命令发送失败（steamcmd 没在运行？）"):
                return False
            self._emit("login_started", note="登录命令已按设置原样发送")
            return True
        return self._advance()

    def stop(self) -> None:
        """用户点停止：温和停止——不再发新命令，当前那条让它跑完。
        已在等空闲提示符 / 等登录 / 等手动登录 → 立即收尾；
        正在下载某条 → 置 STOPPING，那条的结论到了再收尾。
        """
        if self._state in (ST_WAITING_IDLE, ST_NEED_LOGIN, ST_LOGGING_IN):
            self._finish(stopped=True)
        elif self._state in (ST_RUNNING,):
            self._state = ST_STOPPING
            self._emit("stop_requested", mod_id=self._current)

    def resume(self) -> None:
        """NEED_LOGIN 状态下手动催批次继续（决策 94 之后的兜底入口：
        登录成功会自动续批，正常流程用不到它——留着，万一自动续批
        被终端杂音盖住时用户还能手动点）。
        不程序化验证登录态（steamcmd 没有可靠的查询命令）——
        信任用户；真没登录好，下一条会再报未登录，无害回到 NEED_LOGIN。
        """
        if self._state != ST_NEED_LOGIN:
            return
        self._advance()

    def abort(self, reason: str = "") -> None:
        """强制中止（steamcmd 进程已退出时由控制器调用）：与 stop()
        的温和停止本质不同——进程没了，当前条的结论永远不会来，
        等下去只会变成僵尸批次（退出守卫、检测清单、新批次、账本
        导入全被 is_active() 挡住——v2.45 实证 bug）。
        RUNNING/STOPPING 的在途条目记失败（reason 如实入账）；
        WAITING_IDLE 的当前条已有结论、不重复记账；其余状态直接
        收尾。已结束（IDLE/DONE）为无害空操作。"""
        if self._state in (ST_IDLE, ST_DONE):
            return
        if self._state in (ST_RUNNING, ST_STOPPING) \
                and self._current is not None:
            self._failed.append(ItemResult(
                self._current, ok=False,
                reason=reason or "steamcmd 已退出",
                trigger=self._trigger_for(self._current)))
            self._emit("item_done", mod_id=self._current, ok=False,
                       reason=reason or "steamcmd 已退出",
                       done=self._done_count())
        self._finish(stopped=True)

    # ---------------- 对外：终端信号接线入口 ----------------
    def on_verdict(self, verdict) -> None:
        """terminalDock.verdict_emitted 接这里：一条输出结论。"""
        # 各状态只关心自己的结论，其余一律忽略（防御式：启动初期
        # 多余的 Steam> 行、断线重试刷屏等都不会打扰状态机）
        if self._state == ST_LOGGING_IN:
            self._on_login_verdict(verdict)
        elif self._state == ST_NEED_LOGIN:
            self._on_need_login_verdict(verdict)   # 决策 94
        elif self._state == ST_RUNNING:
            self._on_download_verdict(verdict)
        elif self._state == ST_STOPPING:
            self._on_download_verdict(verdict)

    def on_idle(self) -> None:
        """terminalDock.idle_prompt_seen 接这里：steamcmd 空闲了。"""
        if self._state == ST_WAITING_IDLE:
            self._advance()                # 正常节奏：发下一条
        elif self._state == ST_STOPPING:
            self._finish(stopped=True)     # 停止后当前条也跑完了 → 收尾
        # 其他状态收到空闲（启动初期、登录等待中）一律忽略

    # ---------------- 内部：状态机 ----------------
    def _trigger_for(self, mod_id: int) -> int | None:
        """查某条的触发值；表里没有的编号（重试混批等）按无凭证处理。
        返回 None 是合法且有意义的——收尾链会把该条写成版本未知，
        而不是编造版本号（R18）。"""
        return self._triggers.get(mod_id)

    def _on_login_verdict(self, verdict) -> None:
        """登录阶段的结论。"""
        if verdict.kind == outputAnalyzer.KIND_LOGIN_OK:
            self._emit("login_ok")
            self._advance()
            return
        if verdict.kind == outputAnalyzer.KIND_NOT_LOGGED_ON:
            # 登录没成（比如密码错）——转等手动登录；登录成功后
            # 批次自动继续（决策 94），「继续批次」保留当手动兜底
            self._state = ST_NEED_LOGIN
            self._emit("need_login", note="登录命令没有成功，"
                                          "请在终端里完成登录，登录成功后批次自动继续"
                                          "（也可点「继续批次」）")
            return
        # 其余结论（断线等）不影响等待登录，忽略

    def _on_need_login_verdict(self, verdict) -> None:
        """NEED_LOGIN（等手动登录）状态的结论（决策 94）。
        只关心一件事：登录成功 → 自动续批，走 resume() 同一条路。
        其余结论一律忽略——等登录期间终端里会有密码回显、验证码
        提示等各类杂音，都不该惊动状态机；真没登录好，续发的那条
        下载命令会再报未登录，无害回到本状态（与手动「继续批次」
        同一套兜底）。"""
        if verdict.kind == outputAnalyzer.KIND_LOGIN_OK:
            # 卡片沿用 login_ok 事件画"登录完成"（与自动登录路径
            # 同款），控制器借此写"批次自动继续"日志
            self._emit("login_ok")
            self._advance()
    def _on_download_verdict(self, verdict) -> None:
        """下载条目的结论（RUNNING / STOPPING 状态）。"""
        kind = verdict.kind
        if kind == outputAnalyzer.KIND_DOWNLOAD_STARTED:
            return  # 开始行只是确认，卡片在发命令时已标"进行中"
        if kind == outputAnalyzer.KIND_DISCONNECTED:
            # 断线：steamcmd 自动重试，当前条大概率随后报失败/超时，
            # 这里只广播提醒，不改状态（已知限制见文件头）
            self._emit("disconnected", code=verdict.code, note=verdict.note)
            return
        if kind == outputAnalyzer.KIND_COMMAND_NOT_FOUND:
            # 我们自己发的命令不该走到这；真出现说明 steamcmd 行为
            # 异常，广播提醒但不打断批次
            self._emit("warn", note=verdict.note)
            return
        if kind == outputAnalyzer.KIND_NOT_LOGGED_ON:
            if self._state == ST_STOPPING:
                # 修9：停止中收到"未登录"绝不转 NEED_LOGIN——否则停止
                # 意图被"未登录"劫持，批次卡在等登录、is_active 恒真
                # = 僵尸批次（v2.45 实证 bug 的变体入口）。该条按失败
                # 记账不悄悄消失，留在 STOPPING 等 on_idle 收尾
                # （steamcmd 报错后必回提示符；进程真退了由 abort() 兜底）。
                self._failed.append(ItemResult(
                    self._current, ok=False, reason="未登录（批次停止中）",
                    trigger=self._trigger_for(self._current)))
                self._item_done(self._current, ok=False,
                                reason="未登录（批次停止中）")
                self._current = None
                return
            # 未登录：当前条放回队首，暂停等登录（决策 94：终端里
            # 登录成功后批次自动继续，不需要再点「继续批次」）
            self._pending.insert(0, self._current)
            self._current = None
            self._state = ST_NEED_LOGIN
            self._emit("need_login", note="steamcmd 未登录——请在终端"
                                          "里完成登录，登录成功后批次自动继续"
                                          "（也可点「继续批次」手动催一下）")
            return
        if kind == outputAnalyzer.KIND_DOWNLOAD_SUCCESS:
            self._ok.append(ItemResult(
                verdict.mod_id, ok=True,
                size_bytes=verdict.size_bytes,
                trigger=self._trigger_for(verdict.mod_id)))
            self._item_done(verdict.mod_id, ok=True, reason="")
            return
        if kind == outputAnalyzer.KIND_DOWNLOAD_FAILED:
            self._failed.append(ItemResult(
                verdict.mod_id, ok=False,
                reason=verdict.reason or "未知",
                trigger=self._trigger_for(verdict.mod_id)))
            self._item_done(verdict.mod_id, ok=False,
                            reason=verdict.reason or "未知")
            return
        if kind == outputAnalyzer.KIND_DOWNLOAD_TIMEOUT:
            self._timeout.append(ItemResult(
                verdict.mod_id, ok=False, reason="Timeout",
                trigger=self._trigger_for(verdict.mod_id)))
            self._item_done(verdict.mod_id, ok=False, reason="Timeout")
            return
        # 其余结论（登录完成等杂音）忽略


    def _item_done(self, mod_id: int, ok: bool, reason: str) -> None:
        """当前条出了最终结论：记账、广播、转"等空闲提示符"。"""
        self._emit("item_done", mod_id=mod_id, ok=ok, reason=reason,
                   done=self._done_count())
        if self._state == ST_RUNNING:
            self._state = ST_WAITING_IDLE
        # STOPPING 状态不等空闲事件也行——on_idle 会来收尾；
        # 万一提示符已经打完（信号顺序极端情况），下面 on_idle
        # 兜不住的路径由 stop() 的直接收尾兜底

    def _advance(self) -> bool:
        """发下一条；队列空了就收尾。
        返回批次是否还在正常推进：发送失败（批次已出错收尾）时
        返回 False，供 start() 据此报告"没启动成功"。
        细节：先看队首、发送成功才出队——发不出去的条目留在
        队列里，账面（pending_count）才准确。旧写法先 pop 再发，
        发送失败会把那条悄悄弄丢。
        """
        if not self._pending:
            self._finish(stopped=False)
            return True          # 正常跑完收尾，不算失败
        mod_id = self._pending[0]
        self._current = mod_id
        self._state = ST_RUNNING
        if not self._try_send(
                f"workshop_download_item {self._app_id} {mod_id}"):
            return False         # _try_send 里已经把批次收尾了
        self._pending.pop(0)
        index = self._total - len(self._pending) - 1
        self._emit("item_started", index=index, mod_id=mod_id,
                   done=self._done_count())
        return True
    def _try_send(self, text: str, *, fail_note: str | None = None) -> bool:
        """发命令；发不出去（steamcmd 没在跑等）→ 整批出错收尾。
        fail_note：发送失败时的对外文案（事件 note 与收尾 error 共用）。
        下载命令原文无敏感内容，默认原样展示；登录命令含密码（R2），
        start() 传脱敏文案——命令原文绝不进事件载荷、不进日志。"""
        if self._send(text):
            return True
        self._emit("send_failed", note=fail_note or text)
        self._finish(stopped=False, error=fail_note or f"命令发送失败：{text}")
        return False

    def _done_count(self) -> int:
        return len(self._ok) + len(self._failed) + len(self._timeout)

    def _finish(self, stopped: bool, error: str = "") -> None:
        """批次收尾：状态置 DONE，发一次 batch_done（带汇总）。"""
        self._state = ST_DONE
        self._current = None
        if self._finished_emitted:
            return
        self._finished_emitted = True
        summary = {
            "total": self._total,
            "ok": self._ok,
            "failed": self._failed,
            "timeout": self._timeout,
            "stopped": stopped,
            "error": error,
        }
        self._emit("batch_done", summary=summary)

    def _emit(self, ev_type: str, **fields) -> None:
        """广播事件给 GUI（没注入 on_event 就发进空气，无害）。"""
        if self._on_event is None:
            return
        payload = {"type": ev_type, "state": self._state}
        payload.update(fields)
        self._on_event(payload)

    # ---------------- 只读属性（GUI 展示用） ----------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def total(self) -> int:
        return self._total

    @property
    def current(self) -> int | None:
        return self._current

    @property
    def pending_count(self) -> int:
        return len(self._pending)
