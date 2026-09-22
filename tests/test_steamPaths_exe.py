"""ensure_steamcmd_exe 补全规则（T21①）
"""
"""文件夹 → 自动补 steamcmd.exe；本身就是文件 → 原样；
没有 exe 的文件夹不瞎拼（让上游存在性检查说真话）。"""
from core.steamPaths import ensure_steamcmd_exe


def test_empty_returns_empty():
    assert ensure_steamcmd_exe("") == ""
    assert ensure_steamcmd_exe(None) == ""


def test_file_passthrough(tmp_path):
    exe = tmp_path / "steamcmd.exe"
    exe.write_bytes(b"")
    assert ensure_steamcmd_exe(str(exe)) == str(exe)


def test_folder_with_exe_gets_joined(tmp_path):
    exe = tmp_path / "steamcmd.exe"
    exe.write_bytes(b"")
    assert ensure_steamcmd_exe(str(tmp_path)) == str(exe)


def test_folder_without_exe_untouched(tmp_path):
    assert ensure_steamcmd_exe(str(tmp_path)) == str(tmp_path)


def test_missing_path_untouched(tmp_path):
    ghost = tmp_path / "no_such_dir"
    assert ensure_steamcmd_exe(str(ghost)) == str(ghost)
