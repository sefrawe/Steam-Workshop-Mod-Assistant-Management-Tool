"""程序数据位置与启动守卫（T17）"""
"""appPaths 的四个关键行为各钉一个测试：
- 源码运行定位到项目根（core/ 应该在它下面）
- 打包运行定位到 exe 旁边（monkeypatch 伪装 PyInstaller 环境）
- 数据库路径恒等于 <数据目录>/mods.db
- 守卫：目录可写 → 放行；目录被普通文件挡路 → 说清原因，绝不静默放行
世界状态小表（踩坑⑭⑮）——每个用例先摆清楚：
  tmp_path 里有什么 / appPaths 被指到哪 / 预期动作是什么。
"""
import sys
from pathlib import Path

from core import appPaths


def test_app_root_source_is_project_root():
    # 源码运行：项目根 = core/ 的上一级，core/appPaths.py 应真实存在
    root = appPaths.app_root()
    assert (root / "core" / "appPaths.py").is_file()


def test_app_root_frozen_next_to_exe(tmp_path, monkeypatch):
    # 打包运行：伪装成 PyInstaller 环境（frozen=True + exe 指到 tmp_path）
    exe = tmp_path / "ModAssistant.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert appPaths.app_root() == tmp_path


def test_db_path_under_data_dir():
    assert appPaths.db_path() == appPaths.data_dir() / "mods.db"


def test_writability_ok(tmp_path, monkeypatch):
    # 世界状态：tmp_path 是空目录；data/config 都被指进去
    # → 应放行，且两个目录真的被建出来
    monkeypatch.setattr(appPaths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(appPaths, "config_dir", lambda: tmp_path / "config")
    assert appPaths.writability_problem() is None
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "config").is_dir()
    # 探测用的小文件应已清理，不留垃圾
    assert not (tmp_path / "data" / ".write_probe").exists()


def test_writability_blocked_by_file(tmp_path, monkeypatch):
    # 世界状态：tmp_path 里躺着一个普通文件；data 目录被指到
    # "这个文件的下面"——建目录必然失败（父级不是目录）
    blocker = tmp_path / "occupied"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(appPaths, "data_dir", lambda: blocker / "data")
    monkeypatch.setattr(appPaths, "config_dir", lambda: tmp_path / "config")
    problem = appPaths.writability_problem()
    assert problem is not None
    assert problem is not None
    # 文案要点：说清"拒绝启动"、给出问题目录、给出解决办法
    # （与 appPaths.writability_problem 的实际文案对齐——
    #   之前断言"不可写"是测试笔误，文案里没有这个词）
    assert "拒绝启动" in problem
    assert "解决办法" in problem
    assert str(blocker) in problem

