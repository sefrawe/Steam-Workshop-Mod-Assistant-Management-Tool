"""steamcmd 输出分析器测试
"""
"""
测试用的行全部取自真实运行的 steamcmd 输出原文（用户断网事故
现场留存），作用是"格式快照"：steamcmd 将来改版输出格式时，
跑一遍这批测试就知道分析器哪些模式断了、断成什么样。
运行：python -m pytest tests/test_outputAnalyzer.py -v
"""
from core import outputAnalyzer as oa


# ---------- 下载结果四式 ----------

def test_download_success_with_path_and_size():
    v = oa.classify_line(
        'Success. Downloaded item 2978597407 to "D:\\games with non Chinese paths'
        '\\steamcmd\\steamcmd\\steamapps\\workshop\\content\\1158310\\2978597407" '
        "(392314 bytes)")
    assert v is not None and v.kind == oa.KIND_DOWNLOAD_SUCCESS
    assert v.mod_id == 2978597407
    assert v.size_bytes == 392314
    assert v.path.endswith("content\\1158310\\2978597407")


def test_download_failed_reason_captured():
    v = oa.classify_line("ERROR! Download item 3770450698 failed (Failure).")
    assert v.kind == oa.KIND_DOWNLOAD_FAILED
    assert v.mod_id == 3770450698
    assert v.reason == "Failure"


def test_download_failed_reason_no_subscription():
    # 常见原因 No subscription：模式是通用的，括号里写什么都接得住
    v = oa.classify_line("ERROR! Download item 42 failed (No subscription).")
    assert v.reason == "No subscription"


def test_download_timeout():
    v = oa.classify_line("ERROR! Timeout downloading item 2978597407")
    assert v.kind == oa.KIND_DOWNLOAD_TIMEOUT
    assert v.mod_id == 2978597407


def test_mixed_line_timeout_not_started():
    # 真实样本：结论续在"开始下载"同一行——必须判成超时，
    # 不能因为开头有 "Downloading item …" 就误判成刚开始
    v = oa.classify_line(
        "Downloading item 2978597407 ...ERROR! Timeout downloading item 2978597407")
    assert v.kind == oa.KIND_DOWNLOAD_TIMEOUT


def test_download_started_standalone_only():
    v = oa.classify_line("Downloading item 2978597407 ...")
    assert v.kind == oa.KIND_DOWNLOAD_STARTED
    assert v.mod_id == 2978597407


# ---------- 登录与连接 ----------

def test_not_logged_on():
    v = oa.classify_line("ERROR! Not logged on.")
    assert v.kind == oa.KIND_NOT_LOGGED_ON


def test_disconnected_with_code_and_note():
    v = oa.classify_line(
        "steamcmd has been disconnected from steam with result 3 (No Connection)")
    assert v.kind == oa.KIND_DISCONNECTED
    assert v.code == 3
    assert v.note == "No Connection"


def test_retrying_lines_are_ignored():
    # 断线后的重试刷屏不单独判定（断线那一行已经说清了）
    assert oa.classify_line("Retrying...") is None


def test_login_ok_and_ready():
    assert oa.classify_line("Waiting for user info...OK").kind == oa.KIND_LOGIN_OK
    assert oa.classify_line("Loading Steam API...OK").kind == oa.KIND_READY


# ---------- 命令不识别 ----------

def test_command_not_found_captures_input():
    v = oa.classify_line(
        "Command not found: https://steamcommunity.com/sharedfiles/filedetails/?id=3770450698")
    assert v.kind == oa.KIND_COMMAND_NOT_FOUND
    assert "3770450698" in v.note


# ---------- 空闲提示符（批量流程的节奏钥匙） ----------

def test_idle_prompt_exact_only():
    assert oa.is_idle_prompt("Steam>") is True
    assert oa.is_idle_prompt("  Steam>  ") is True      # 带空白也算


def test_echo_line_is_not_idle():
    # 命令回显行以 Steam> 开头但带内容——不算空闲
    assert oa.is_idle_prompt("Steam>workshop_download_item 1158310 2978597407") is False
    assert oa.is_idle_prompt("Steam Console Client (c) Valve Corporation") is False


# ---------- 不认识的行与回显行一律 None ----------

def test_unknown_and_echo_lines_are_ignored():
    assert oa.classify_line("") is None
    assert oa.classify_line("Steam>workshop_download_item 1158310 2978597407") is None
    assert oa.classify_line("Logging directory: 'D:/steamcmd/logs'") is None
    assert oa.classify_line("Looks like steam didn't shutdown cleanly, "
                            "scheduling immediate update check") is None
    assert oa.classify_line("[  0%] 正在检查可用更新...") is None


# ---------- Windows 行尾的 \r 不影响判定 ----------

def test_trailing_carriage_return_stripped():
    assert oa.classify_line("ERROR! Not logged on.\r").kind == oa.KIND_NOT_LOGGED_ON
    assert oa.is_idle_prompt("Steam>\r") is True


# ---------- describe：一句人话 ----------

def test_describe_covers_all_kinds():
    cases = [
        (oa.Verdict(kind=oa.KIND_DOWNLOAD_STARTED, mod_id=1), "开始下载"),
        (oa.Verdict(kind=oa.KIND_DOWNLOAD_SUCCESS, mod_id=1, size_bytes=10), "下载成功"),
        (oa.Verdict(kind=oa.KIND_DOWNLOAD_FAILED, mod_id=1, reason="Failure"), "下载失败"),
        (oa.Verdict(kind=oa.KIND_DOWNLOAD_TIMEOUT, mod_id=1), "超时"),
        (oa.Verdict(kind=oa.KIND_NOT_LOGGED_ON), "登录"),
        (oa.Verdict(kind=oa.KIND_DISCONNECTED, code=3, note="No Connection"), "断开连接"),
        (oa.Verdict(kind=oa.KIND_COMMAND_NOT_FOUND, note="xyz"), "不认识"),
        (oa.Verdict(kind=oa.KIND_LOGIN_OK), "登录成功"),
        (oa.Verdict(kind=oa.KIND_READY), "就绪"),
    ]
    for v, keyword in cases:
        assert keyword in oa.describe(v), f"{v.kind} 的人话里没有「{keyword}」"
