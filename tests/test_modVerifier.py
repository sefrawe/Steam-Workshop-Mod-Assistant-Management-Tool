"""账实核验器测试
"""
"""
两份对账分开覆盖：
- verify()：tmp_path 造假目录树 + 造假账本字典，不碰数据库
- verify_junctions()：真 junction（mklink /J），skipif 非 Windows；
  建链接失败的极端环境进一步 pytest.skip 兜底
锁定分桶口径、死根短路、排序稳定性、casefold 比对、"悬空归 verify 不归
junction 巡检"的分工。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import modVerifier as mv


def _mk(content: Path, mid: int, *, files: int = 1, subs: int = 0) -> Path:
    """在 content 下造一个 mod 目录：files 个散文件 + subs 个子目录。"""
    d = content / str(mid)
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"f{i}.txt").write_text("x", encoding="utf-8")
    for i in range(subs):
        (d / f"sub{i}").mkdir()
    return d


# ============================================================
# verify()：账本 ↔ content 磁盘
# ============================================================

# ---------- 正常路径 ----------

def test_all_healthy(tmp_path):
    content = tmp_path / "content" / "1"
    _mk(content, 100)
    _mk(content, 200)
    r = mv.verify(content, {100: "downloaded", 200: "downloaded"})
    assert not r.dead_root
    assert r.healthy == 2 and r.missing == [] and r.empty == []
    assert r.untracked_content == [] and r.non_numeric == []


def test_missing_dir(tmp_path):
    # 账本说下载了，盘上没有——核验页的核心价值场景
    content = tmp_path / "content" / "1"
    _mk(content, 100)
    r = mv.verify(content, {100: "downloaded", 200: "downloaded"})
    assert r.missing == [200] and r.healthy == 1


def test_empty_dir(tmp_path):
    content = tmp_path / "content" / "1"
    _mk(content, 100)
    (content / "200").mkdir()  # 空目录
    r = mv.verify(content, {100: "downloaded", 200: "downloaded"})
    assert r.empty == [200] and r.healthy == 1


def test_only_subdirs_counts_as_non_empty(tmp_path):
    # 口径钉死：目录里有任何子目录就算非空（只做一层 scandir，
    # 不递归数文件——mod 目录里只有空子目录的情况不值得特判）
    content = tmp_path / "content" / "1"
    _mk(content, 100, files=0, subs=2)
    r = mv.verify(content, {100: "downloaded"})
    assert r.empty == [] and r.healthy == 1


# ---------- untracked_content 分桶 ----------

def test_untracked_numeric_buckets(tmp_path):
    content = tmp_path / "content" / "1"
    _mk(content, 1)   # 不在库中
    _mk(content, 2)   # tracked
    _mk(content, 3)   # deleted（软删除保留文件，正常）
    _mk(content, 4)   # failed
    r = mv.verify(content, {2: "tracked", 3: "deleted", 4: "failed"})
    assert r.untracked_content == [
        (1, None), (2, "tracked"), (3, "deleted"), (4, "failed")]
    assert r.healthy == 0 and r.missing == []


# ---------- 非数字内容桶 ----------

def test_non_numeric_bucket(tmp_path):
    content = tmp_path / "content" / "1"
    _mk(content, 100)
    (content / "残留目录").mkdir()
    (content / "stray.txt").write_text("x", encoding="utf-8")
    r = mv.verify(content, {100: "downloaded"})
    # 目录和散文件都进桶；只对集合断言，不锁排序细节
    assert len(r.non_numeric) == 2
    assert set(r.non_numeric) == {"残留目录", "stray.txt"}
    assert r.healthy == 1  # 正常 mod 的核对不受影响


# ---------- 死根短路 ----------

def test_dead_root_short_circuits(tmp_path):
    # 根不在：逐条对账全是误报，必须短路（missing 保持空）
    r = mv.verify(tmp_path / "nope", {100: "downloaded"})
    assert r.dead_root and r.missing == [] and r.healthy == 0


def test_blank_or_none_dead_root():
    assert mv.verify("", {100: "downloaded"}).dead_root
    assert mv.verify(None, {}).dead_root


# ---------- 输出稳定性 ----------

def test_sorted_stable_output(tmp_path):
    # 盘上目录的发现顺序是文件系统给的，账本字典的插入顺序也不该影响
    # 产出——两边顺序都打乱，产出的清单必须自己排好序。
    # 构造：盘上有 300/100/200（创建顺序故意乱）；
    # 账本里 400、50 盘上没有，且 400 插在 50 前面（自然迭代是 [400, 50]）
    content = tmp_path / "content" / "1"
    _mk(content, 300)
    _mk(content, 100)
    _mk(content, 200)
    r = mv.verify(content, {
        400: "downloaded", 50: "downloaded",  # 两个 missing，插入顺序故意倒着
        300: "downloaded", 100: "downloaded", 200: "downloaded"})
    assert r.missing == [50, 400]  # 必须是排序结果，不是插入顺序 [400, 50]
    assert r.healthy == 3          # 100/200/300 都在盘上且非空
    assert r.untracked_content == []


def test_untracked_sorted_stable(tmp_path):
    # 账本为空：盘上的数字目录全部进 untracked（状态 None），
    # 产出必须按编号排序，不跟随目录的文件系统顺序
    content = tmp_path / "content" / "1"
    _mk(content, 999)
    _mk(content, 5)
    r = mv.verify(content, {})
    assert r.untracked_content == [(5, None), (999, None)]


# ============================================================
# verify_junctions()：账本 ↔ 游戏侧链接
# ============================================================

_NEEDS_WINDOWS = pytest.mark.skipif(
    sys.platform != "win32", reason="junction 仅 Windows 可建")


def _mk_junction(link: Path, target: Path) -> bool:
    """用 mklink /J 建 junction；失败返回 False（极端受限环境，
    调用方自行 pytest.skip）。"""
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True)
    return r.returncode == 0


def _junction_env(tmp_path, mid: int = 100):
    """标准巡检环境：content 侧一个 mod 目录 + 游戏侧空目录。"""
    content = tmp_path / "content" / "1"
    _mk(content, mid)
    gdir = tmp_path / "game" / "mods"
    gdir.mkdir(parents=True)
    return content, gdir


@_NEEDS_WINDOWS
def test_junction_ok(tmp_path):
    content, gdir = _junction_env(tmp_path)
    assert _mk_junction(gdir / "100", content / "100")
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r is not None and not r.dead_root
    assert r.ok == 1 and r.link_missing == [] and r.extra == []


@_NEEDS_WINDOWS
def test_junction_target_case_insensitive(tmp_path):
    # 建链接时故意用全大写路径——指向同一个地方，必须判 ok
    # （踩坑 ⑩ 同源：Windows 路径比对必须 casefold）
    content, gdir = _junction_env(tmp_path)
    assert _mk_junction(gdir / "100",
                        Path(str(content / "100").upper()))
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r.ok == 1 and r.link_wrong_target == []


@_NEEDS_WINDOWS
def test_junction_missing(tmp_path):
    content, gdir = _junction_env(tmp_path)
    # 游戏侧什么都不放 → 游戏里看不到此 mod，修复 = 重建链接
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r.link_missing == [100] and r.ok == 0


@_NEEDS_WINDOWS
def test_junction_wrong_target(tmp_path):
    content, gdir = _junction_env(tmp_path)
    decoy = tmp_path / "elsewhere"
    decoy.mkdir()
    assert _mk_junction(gdir / "100", decoy)
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert len(r.link_wrong_target) == 1
    mid, detail = r.link_wrong_target[0]
    assert mid == 100 and "elsewhere" in detail  # detail 带实际指向


@_NEEDS_WINDOWS
def test_junction_real_dir(tmp_path):
    content, gdir = _junction_env(tmp_path)
    (gdir / "100").mkdir()  # 真实目录，不是链接——R4 只报不动
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r.link_real_dir == [100] and r.ok == 0
    assert r.link_wrong_target == []  # 不是"指错"，是"根本不是链接"


@_NEEDS_WINDOWS
def test_junction_extra_bucket(tmp_path):
    # 世界状态：content/100 实体在；游戏侧有 100 的正常链接、
    # 999 的账外链接、readme.txt 散文件
    content, gdir = _junction_env(tmp_path)
    (gdir / "readme.txt").write_text("x", encoding="utf-8")
    assert _mk_junction(gdir / "100", content / "100")  # 正常链接
    assert _mk_junction(gdir / "999", content / "100")  # 账外链接
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert set(r.extra) == {"readme.txt", "999"}
    assert r.ok == 1 and r.link_missing == []  # 正常条目不受 extra 干扰

@_NEEDS_WINDOWS
def test_junction_symlink_counts_as_link(tmp_path):
    # 符号链接对游戏等价于 junction，不能误报成"实为目录"。
    # 建符号链接需要管理员/开发者模式，建不了就跳过
    content, gdir = _junction_env(tmp_path)
    try:
        os.symlink(content / "100", gdir / "100", target_is_directory=True)
    except OSError:
        pytest.skip("当前环境无权限创建符号链接")
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r.ok == 1 and r.link_real_dir == []


@_NEEDS_WINDOWS
def test_dangling_link_counts_as_ok_here(tmp_path):
    # 分工口径钉死：链接指向正确但 content 实体已删 = 链接本身没问题
    # （ok），"实体没了"由 verify() 的 missing 桶负责——同一问题不在
    # 同一页报两遍；重下后悬空链接自然复活
    content, gdir = _junction_env(tmp_path)
    assert _mk_junction(gdir / "100", content / "100")
    shutil.rmtree(content / "100")
    r = mv.verify_junctions(content, gdir, {100: "downloaded"})
    assert r.ok == 1 and r.link_missing == [] and r.link_wrong_target == []
    v = mv.verify(content, {100: "downloaded"})
    assert v.missing == [100]


@_NEEDS_WINDOWS
def test_junction_skips_non_downloaded(tmp_path):
    # tracked 的 mod 游戏侧没有链接是正常的（还没下载），
    # 不得报 link_missing——巡检只对 downloaded 负责
    content, gdir = _junction_env(tmp_path)
    r = mv.verify_junctions(content, gdir, {100: "tracked"})
    assert r.link_missing == [] and r.ok == 0
    assert r.extra == []  # 游戏侧本来就空


def test_junction_unconfigured_returns_none(tmp_path):
    content, gdir = _junction_env(tmp_path)
    # None / 空串 / 纯空白引号都算"未配置"（GUI 的"清除"就是写空串）
    assert mv.verify_junctions(content, None, {100: "downloaded"}) is None
    assert mv.verify_junctions(content, "", {100: "downloaded"}) is None
    assert mv.verify_junctions(content, ' "" ', {100: "downloaded"}) is None


@_NEEDS_WINDOWS
def test_junction_dead_root(tmp_path):
    content, _ = _junction_env(tmp_path)
    r = mv.verify_junctions(content, tmp_path / "nope", {100: "downloaded"})
    assert r is not None and r.dead_root and r.ok == 0

def test_junction_same_dir_returns_none(tmp_path):
    # 单目录布局（用户实测形态）：游戏侧与下载目录指向同一处，
    # 仅斜杠风格不同——合法布局，无链接可查，按未启用返回 None
    d = tmp_path / "content" / "1"
    _mk(d, 100)
    alt = str(d).replace("\\", "/")  # 模拟 E:\... vs E:/... 的真实差异
    assert mv.verify_junctions(d, alt, {100: "downloaded"}) is None
    assert mv.verify_junctions(d, str(d), {}) is None
