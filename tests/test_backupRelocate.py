"""backupRelocate 重定位引擎测试。"""
"""
世界状态先列表再断言（踩坑⑭⑮）：每个用例开头注释写明
"盘上有什么 / 给哪些相对路径 / 候选根状态"。引擎纯逻辑，
只碰 tmp_path 目录，不建库不建 repo。
"""
from pathlib import Path

from core.backupRelocate import preview


def make_backup_dirs(root: Path, rels: list[str]) -> None:
    """盘上：在 root 下按清单建备份目录（各放一个占位文件）。"""
    for rel in rels:
        d = root / rel
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("x", encoding="utf-8")


# ---------- 命中口径 ----------

def test_all_hit(tmp_path):
    # 盘上：root 下有 2 个备份目录｜清单：同 2 条相对路径
    root = tmp_path / "backups"
    rels = ["294100_v100_1", "294101_v200_2"]
    make_backup_dirs(root, rels)
    out = preview(str(root), rels)
    assert out.root_exists is True
    assert out.total == 2 and out.hit == 2
    assert out.all_hit is True and out.any_hit is True
    assert out.missed_samples == []


def test_root_missing_all_miss(tmp_path):
    # 盘上：无（候选根目录根本不建）｜清单：2 条
    out = preview(str(tmp_path / "nowhere"),
                  ["294100_v100_1", "294101_v200_2"])
    assert out.root_exists is False
    assert out.hit == 0 and out.total == 2
    assert out.missed == 2


def test_root_exists_but_empty(tmp_path):
    # 盘上：候选根存在但是空目录｜清单：2 条
    root = tmp_path / "backups"
    root.mkdir()
    out = preview(str(root), ["294100_v100_1", "294101_v200_2"])
    assert out.root_exists is True
    assert out.hit == 0 and out.missed == 2


def test_mixed_hit_and_miss(tmp_path):
    # 盘上：root 下只有第一个目录｜清单：2 条（1 中 1 不中）
    root = tmp_path / "backups"
    make_backup_dirs(root, ["294100_v100_1"])
    out = preview(str(root), ["294100_v100_1", "999999_v0_0"])
    assert out.hit == 1 and out.missed == 1
    assert out.missed_samples == ["999999_v0_0"]
    assert out.all_hit is False and out.any_hit is True


def test_resolved_to_file_counts_as_miss(tmp_path):
    # 盘上：root 下同名路径是个普通文件不是目录｜清单：1 条
    root = tmp_path / "backups"
    root.mkdir()
    (root / "294100_v100_1").write_text("是个文件", encoding="utf-8")
    out = preview(str(root), ["294100_v100_1"])
    assert out.hit == 0 and out.missed == 1


def test_rel_with_subdir_joins(tmp_path):
    # 盘上：root/294100/inner 存在｜清单：两层相对路径（拼接口径）
    root = tmp_path / "backups"
    make_backup_dirs(root, ["294100/inner"])
    out = preview(str(root), ["294100/inner"])
    assert out.hit == 1


# ---------- 输入规整与防御 ----------

def test_empty_root_string_never_matches(tmp_path, monkeypatch):
    # 盘上：CWD（monkeypatch 到 tmp_path）下有同名目录——若空根被
    # 当成"."就会碰巧对上；正确行为 = 空根一律"根不存在"
    make_backup_dirs(tmp_path, ["trap_dir"])
    monkeypatch.chdir(tmp_path)
    out = preview("", ["trap_dir"])
    assert out.root_exists is False
    assert out.hit == 0


def test_whitespace_root_treated_as_missing(tmp_path, monkeypatch):
    # 候选根："   "（纯空白）——与空串同口径
    make_backup_dirs(tmp_path, ["trap_dir"])
    monkeypatch.chdir(tmp_path)
    out = preview("   ", ["trap_dir"])
    assert out.root_exists is False and out.hit == 0


def test_quoted_root_stripped(tmp_path):
    # 候选根：带引号（资源管理器"复制文件地址"常带引号）
    root = tmp_path / "backups"
    rels = ["294100_v100_1"]
    make_backup_dirs(root, rels)
    out = preview(f'"{root}"', rels)
    assert out.candidate_root == str(root)
    assert out.hit == 1


def test_path_object_root(tmp_path):
    # 候选根：直接给 Path 对象（调用方手里往往是 Path）
    root = tmp_path / "backups"
    rels = ["294100_v100_1"]
    make_backup_dirs(root, rels)
    out = preview(root, rels)
    assert out.hit == 1


def test_missed_samples_capped_at_five(tmp_path):
    # 盘上：候选根存在但空｜清单：8 条 → 样例只留 5，missed 仍为 8
    root = tmp_path / "backups"
    root.mkdir()
    rels = [f"{i}_v0_0" for i in range(8)]
    out = preview(str(root), rels)
    assert len(out.missed_samples) == 5
    assert out.missed == 8 and out.total == 8


def test_absolute_paths_reported_separately(tmp_path):
    # 盘上：root 下 1 个备份目录 + 根外 1 个真实目录
    # 清单：1 相对（对上）+ 1 绝对存在 + 1 绝对缺失
    root = tmp_path / "backups"
    make_backup_dirs(root, ["294100_v100_1"])
    outside = tmp_path / "elsewhere" / "294200_v1_1"
    outside.mkdir(parents=True)
    out = preview(str(root), [
        "294100_v100_1",
        str(outside),            # 绝对且存在：重定位不用管
        str(tmp_path / "gone"),  # 绝对且缺失：重定位救不了，单列
    ])
    assert out.total == 3
    assert out.non_relative_count == 2
    assert out.hit == 1
    assert out.relocatable == 1 and out.all_hit is True
    assert len(out.non_relative_samples) == 2


def test_empty_records(tmp_path):
    # 清单：空 → 全零不崩
    root = tmp_path / "backups"
    root.mkdir()
    out = preview(str(root), [])
    assert out.total == 0 and out.hit == 0
    assert out.all_hit is False and out.any_hit is False
