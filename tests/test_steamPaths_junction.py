"""junction_state 联接状态判定（T13，v2 含反向拓扑）
"""
"""各状态一枚钉子：真 junction 用 cmd mklink /J 建（仅 Windows，
技法照抄 test_backupManager 的 R4 用例），其余状态纯目录构造、
任何平台都能跑。反向拓扑三态同样需要真 junction，仅 Windows。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.steamPaths import junction_state


# ---------- 任何平台都能跑的状态 ----------

def test_missing(tmp_path):
    ghost = tmp_path / "no" / "such" / "dir"
    rep = junction_state(str(ghost), str(tmp_path / "content"))
    assert rep.state == "missing" and rep.detail == ""


def test_empty_dir(tmp_path):
    d = tmp_path / "mod"
    d.mkdir()
    rep = junction_state(str(d), str(tmp_path / "content"))
    assert rep.state == "empty_dir"


def test_real_dir(tmp_path):
    d = tmp_path / "mod"
    d.mkdir()
    (d / "some_mod").mkdir()
    rep = junction_state(str(d), str(tmp_path / "content"))
    assert rep.state == "real_dir"


def test_file_blocks(tmp_path):
    f = tmp_path / "mod"
    f.write_text("x", encoding="utf-8")
    rep = junction_state(str(f), str(tmp_path / "content"))
    assert rep.state == "file"


def test_no_target(tmp_path):
    assert junction_state(str(tmp_path / "mod"), "").state == "no_target"
    assert junction_state(str(tmp_path / "mod"), None).state == "no_target"


def test_quotes_and_spaces_tolerated(tmp_path):
    """从资源管理器复制的路径常带引号/空白，不能让格式问题污染判定。"""
    d = tmp_path / "mod"
    d.mkdir()
    rep = junction_state(f' "{d}" ', str(tmp_path / "content"))
    assert rep.state == "empty_dir"


# ---------- 正向拓扑（仅 Windows） ----------

def _mklink(link: Path, target: Path) -> None:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_linked(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    link = tmp_path / "mod"
    _mklink(link, content)
    try:
        rep = junction_state(str(link), str(content))
        assert rep.state == "linked"
        assert rep.detail.casefold() == os.path.realpath(content).casefold()
        # 带尾随反斜杠粘贴也不误判（先归一再 readlink）
        assert junction_state(str(link) + "\\", str(content)).state == "linked"
    finally:
        os.rmdir(link)  # 只摘联接本体（rmtree 会穿进实体——踩坑⑩邻居）


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_wrong_target_and_dangling(tmp_path):
    content = tmp_path / "content"
    content.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    link = tmp_path / "mod"
    _mklink(link, decoy)
    try:
        assert junction_state(str(link), str(content)).state == "wrong_target"
        # 悬空：实体被删后联接还在，同样判"指错"，不崩
        os.rmdir(decoy)
        assert junction_state(str(link), str(content)).state == "wrong_target"
    finally:
        os.rmdir(link)


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_case_insensitive_compare(tmp_path):
    """R14 口径：大小写不同的同一目录视为已连接。"""
    content = tmp_path / "content"
    content.mkdir()
    link = tmp_path / "mod"
    _mklink(link, content)
    try:
        rep = junction_state(str(link), str(content).upper())
        assert rep.state == "linked"
    finally:
        os.rmdir(link)


# ---------- 反向拓扑（仅 Windows）——联接在下载目录侧 ----------

@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_linked_reverse(tmp_path):
    """用户环境的真实形态：游戏目录是真实目录（有内容），
    steamcmd 侧的下载目录反而是联接指向它。v1 在这里误报 real_dir。"""
    game_mod = tmp_path / "game_mod"
    game_mod.mkdir()
    (game_mod / "some_mod").mkdir()
    content = tmp_path / "content"
    _mklink(content, game_mod)
    try:
        rep = junction_state(str(game_mod), str(content))
        assert rep.state == "linked_reverse"
        assert rep.detail.casefold() == os.path.realpath(game_mod).casefold()
    finally:
        os.rmdir(content)


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_linked_reverse_missing(tmp_path):
    """反向接了一半：联接指对了位置，但游戏目录本身被删了。
    此状态绝不能建议 mklink 游戏→下载——那会造出联接套联接的环。"""
    game_mod = tmp_path / "game_mod"
    game_mod.mkdir()
    content = tmp_path / "content"
    _mklink(content, game_mod)
    os.rmdir(game_mod)  # 拆掉实体 → 联接悬空
    try:
        assert junction_state(str(game_mod), str(content)).state \
               == "linked_reverse_missing"
    finally:
        os.rmdir(content)


@pytest.mark.skipif(sys.platform != "win32", reason="junction 仅 Windows 可建")
def test_wrong_target_reverse(tmp_path):
    """下载目录侧的联接指去了别处：内容写不进游戏目录。"""
    game_mod = tmp_path / "game_mod"
    game_mod.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    content = tmp_path / "content"
    _mklink(content, elsewhere)
    try:
        rep = junction_state(str(game_mod), str(content))
        assert rep.state == "wrong_target_reverse"
        assert rep.detail.casefold() == os.path.realpath(elsewhere).casefold()
    finally:
        os.rmdir(content)
