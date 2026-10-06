"""steamcmd 输出分析器
"""
"""
逐行分析 steamcmd 的控制台输出，把原始文本翻译成程序能用的结论。
M3 功能模块的地基：终端面板靠它标注"这行输出意味着什么"，
批量下载靠它判断"当前命令出结果了吗、是成功还是失败"。

为什么放 core、为什么是纯函数：
- 不依赖 Qt、不碰任何控件和数据库——输入一行文本，输出一个结论，
  其余什么都不做；
- steamcmd 的输出格式没有官方文档，全靠真实运行样本逆向归纳。
  配套测试（tests/test_outputAnalyzer.py）用的就是真实遇到的
  输出原文——测试即"格式快照"，将来 steamcmd 改版，一跑测试
  就知道哪里断了、断成什么样。

判定模式总表（记事本概括为"7 输出"，落地为 9 种判定：
7 种命令/连接状态 + 登录完成 + 启动就绪）：

  模式          输出样例（真实原文摘录）
  ----------    ------------------------------------------------
  下载开始      Downloading item 2978597407 ...
  下载成功      Success. Downloaded item 2978597407 to "…" (392314 bytes)
  下载失败      ERROR! Download item 3770450698 failed (Failure).
  下载超时      ERROR! Timeout downloading item 2978597407
  未登录        ERROR! Not logged on.
  断线          steamcmd has been disconnected from steam
                with result 3 (No Connection)
  命令不识别    Command not found: https://…
  登录完成      Waiting for user info...OK
  启动就绪      Loading Steam API...OK

两个从真实样本里挖出来的坑（改这里之前先看）：

1) steamcmd 会把结论续在"Downloading item …"同一行后面，比如
   "Downloading item 2978597407 ...ERROR! Timeout downloading item …"。
   所以"下载开始"必须整行匹配（行尾就是省略号才算开始），
   其余模式用"包含即命中"，且"下载开始"永远排在最后检查——
   混合行先被前面的结论模式接住，轮不到"开始"。
2) 失败原因写在括号里（Failure / No subscription / Timeout…），
   属于 steamcmd 的原文，原样捕获不做翻译，展示层只负责展示。

判定的边界：
- 断线后的 "Retrying..." 行不单独判定——断线那一行已经把事说清，
  重试行只负责在原始输出里刷屏，分析器不必跟着嚷；
- 账实口径不变（决策 23）：这里的"成功/失败"只服务于界面展示
  与控制台日志，版本三件套的唯一来源仍是 acf（扫描本地），
  本模块绝不写库、绝不据此改 mod 状态。

发送命令的节奏钥匙：
- is_idle_prompt：一行剥掉首尾空白后恰好是 "Steam>" = steamcmd
  空闲，等下一条命令。批量下载流程只在这个信号出现时才发下一条，
  等价于"人眼看到提示符再粘贴"，慢下载、断线重试各种节奏
  天然适配，不需要猜时间。
- 命令回显行（"Steam>workshop_download_item …"）虽然也以 Steam>
  开头，但带内容，不算空闲——上面的判定方式天然区分两者。
"""
import re
from dataclasses import dataclass

# ---------- 结论种类（模块常量：调用方按这里比较，别写裸字符串） ----------

KIND_DOWNLOAD_STARTED = "download_started"    # 开始下载某个 mod
KIND_DOWNLOAD_SUCCESS = "download_success"    # 下载成功（附路径与字节数）
KIND_DOWNLOAD_FAILED = "download_failed"      # 下载失败（附括号里的原文原因）
KIND_DOWNLOAD_TIMEOUT = "download_timeout"    # 下载超时（steamcmd 自己判的）
KIND_NOT_LOGGED_ON = "not_logged_on"          # 未登录就执行了需要登录的命令
KIND_DISCONNECTED = "disconnected"            # 与 Steam 断线（steamcmd 自动重试）
KIND_COMMAND_NOT_FOUND = "command_not_found"  # 命令不识别（输错了/粘错了）
KIND_LOGIN_OK = "login_ok"                    # 登录完成（可以开始下命令了）
KIND_READY = "ready"                          # steamcmd 启动就绪


@dataclass(frozen=True)
class Verdict:
    """一行输出得到的结论。

    只填用得上的字段，其余保持默认——调用方按 kind 取自己关心的：
    - success：mod_id / path / size_bytes
    - failed：mod_id / reason（steamcmd 括号里的原文）
    - started / timeout：mod_id
    - disconnected：code（result 数字）/ note（括号里的原文）
    - command_not_found：note（被拒绝的命令原文）
    - not_logged_on / login_ok / ready：只有 kind 本身
    """
    kind: str
    mod_id: int | None = None
    reason: str | None = None
    path: str | None = None
    size_bytes: int | None = None
    code: int | None = None
    note: str | None = None


# ---------- 模式表：按顺序逐个试，先命中先用 ----------
# 顺序即优先级（原因见文件头坑 1）："下载开始"必须垫底。
# 除"下载开始"外全部用 search（包含即命中），防结论续行接龙。
_PATTERNS: list[tuple[re.Pattern, object]] = [
    # 下载成功：路径带引号（真实路径含空格，引号里随便长）
    (re.compile(r"Success\. Downloaded item (\d+) to \"(.*)\" \((\d+) bytes\)"),
     lambda m: Verdict(kind=KIND_DOWNLOAD_SUCCESS, mod_id=int(m.group(1)),
                       path=m.group(2), size_bytes=int(m.group(3)))),
    # 下载失败：括号里是 steamcmd 原文原因（Failure / No subscription…）
    (re.compile(r"ERROR! Download item (\d+) failed \((.+?)\)"),
     lambda m: Verdict(kind=KIND_DOWNLOAD_FAILED, mod_id=int(m.group(1)),
                       reason=m.group(2))),
    # 下载超时：单独一种（没有 "failed (...)" 外壳），只报编号
    (re.compile(r"ERROR! Timeout downloading item (\d+)"),
     lambda m: Verdict(kind=KIND_DOWNLOAD_TIMEOUT, mod_id=int(m.group(1)))),
    # 未登录：不带编号的独立错误
    (re.compile(r"ERROR! Not logged on"),
     lambda m: Verdict(kind=KIND_NOT_LOGGED_ON)),
    # 断线：result 数字 + 括号原文（如 3: No Connection）
    (re.compile(r"disconnected from steam with result (\d+) \((.+)\)"),
     lambda m: Verdict(kind=KIND_DISCONNECTED, code=int(m.group(1)),
                       note=m.group(2))),
    # 命令不识别：把被拒绝的原文带回去，界面上用户一眼知道输错了啥
    (re.compile(r"Command not found: (.+)"),
     lambda m: Verdict(kind=KIND_COMMAND_NOT_FOUND, note=m.group(1))),
    # 登录完成：登录流程的最后一步（前面还有 Waiting for client config）
    (re.compile(r"Waiting for user info\.\.\.OK"),
     lambda m: Verdict(kind=KIND_LOGIN_OK)),
    # 启动就绪：出现这行说明 steamcmd 起来了、可以发登录命令
    (re.compile(r"Loading Steam API\.\.\.OK"),
     lambda m: Verdict(kind=KIND_READY)),
    # 下载开始：整行匹配（行尾就是省略号才算）——垫底的原因见文件头坑 1
    (re.compile(r"^Downloading item (\d+)\s+\.\.\.\s*$"),
     lambda m: Verdict(kind=KIND_DOWNLOAD_STARTED, mod_id=int(m.group(1)))),
]

# 空闲提示符：剥掉首尾空白后恰好等于 "Steam>"（见文件头"节奏钥匙"）
_IDLE_PROMPT = re.compile(r"^Steam>$")


def classify_line(line: str) -> Verdict | None:
    """一行输出 → 结论；不在任何模式里返回 None（调用方忽略即可）。

    行尾的 \r（Windows 控制台）先剥掉再比对，避免"看起来一样
    实际差个回车"对不上模式。
    """
    text = line.rstrip("\r\n").strip()
    if not text:
        return None
    for pattern, build in _PATTERNS:
        m = pattern.search(text)
        if m:
            return build(m)
    return None


def is_idle_prompt(line: str) -> bool:
    """这行是不是"空闲提示符"（一行只有 Steam>，后面没内容）。

    是 → steamcmd 在等下一条命令，批量流程可以发下一条；
    否（包括命令回显行 Steam>workshop_…）→ 继续等。
    """
    return _IDLE_PROMPT.match(line.strip()) is not None


def describe(v: Verdict) -> str:
    """结论 → 一句人话（控制台日志与步骤卡片共用，一处算清别处只读）。

    字节数在这里只报原始数字，"392 KiB" 那种格式化归显示层的
    formatters.fmt_size——core 不 import gui，层不倒挂。
    """
    if v.kind == KIND_DOWNLOAD_STARTED:
        return f"开始下载 mod {v.mod_id}"
    if v.kind == KIND_DOWNLOAD_SUCCESS:
        return f"mod {v.mod_id} 下载成功（{v.size_bytes} 字节）"
    if v.kind == KIND_DOWNLOAD_FAILED:
        base = f"mod {v.mod_id} 下载失败：{v.reason}"
        if "no match" in (v.reason or "").casefold():
            # No match = 该游戏名下没有这个条目：AppID 与编号多半
            # 不匹配（条目属于别的游戏），或条目已不存在
            base += "（该游戏名下没有此条目：AppID 与编号多半不匹配，" \
                    "或条目已不存在）"
        return base

    if v.kind == KIND_DOWNLOAD_TIMEOUT:
        return f"mod {v.mod_id} 下载超时（steamcmd 判定）"
    if v.kind == KIND_NOT_LOGGED_ON:
        return "尚未登录：请先执行登录命令再下载"
    if v.kind == KIND_DISCONNECTED:
        if v.code == 1:
            # result 1 (OK) 出现在登录瞬间 = 旧会话被新登录挤掉的
            # 正常提示，不是断线——照实说，免得用户虚惊
            # （2026-10-06 批次日志实证：报这句的下一秒登录成功、
            # 批次全程无碍）
            return ("会话提示（result 1 OK）：旧登录会话被本次登录"
                    "挤掉，无害，无需处理")
        return f"与 Steam 断开连接（result {v.code}：{v.note}），steamcmd 会自动重试"

    if v.kind == KIND_COMMAND_NOT_FOUND:
        return f"steamcmd 不认识这条命令：{v.note}"
    if v.kind == KIND_LOGIN_OK:
        return "登录成功"
    if v.kind == KIND_READY:
        return "steamcmd 就绪"
    return v.kind  # 未知种类原样返回，不吞（与 formatters.status_zh 同款口径）
