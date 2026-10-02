"""acf 条目移除的单元测试（合成样本，不碰真实 acf）"""
import pytest
import vdf

from core.localScanner import remove_items_from_acf


def _write_sample(tmp_path):
    """合成一个三条目 acf：Details 区块故意少 333（模拟两块不对齐）。"""
    data = {"AppWorkshop": {
        "size": "600",
        "WorkshopItemsInstalled": {
            "111": {"timeupdated": "1700000000", "manifest": "m1", "size": "100"},
            "222": {"timeupdated": "1700000001", "manifest": "m2", "size": "200"},
            "333": {"timeupdated": "1700000002", "manifest": "m3", "size": "300"},
        },
        "WorkshopItemDetails": {
            "111": {"timeupdated": "1700000000", "manifest": "m1"},
            "222": {"timeupdated": "1700000001", "manifest": "m2"},
        },
    }}
    p = tmp_path / "appworkshop_294100.acf"
    p.write_text(vdf.dumps(data), encoding="utf-8")
    return p


def test_remove_hits_both_blocks(tmp_path):
    p = _write_sample(tmp_path)
    removed, absent = remove_items_from_acf(p, [111, 999])
    assert removed == [111]
    assert absent == [999]
    ws = vdf.loads(p.read_text(encoding="utf-8"))["AppWorkshop"]
    assert "111" not in ws["WorkshopItemsInstalled"]
    assert "111" not in ws["WorkshopItemDetails"]
    assert "222" in ws["WorkshopItemsInstalled"]  # 其余条目原样
    assert ws["size"] == "600"  # 顶层其他字段保留


def test_backup_created_and_has_original(tmp_path):
    p = _write_sample(tmp_path)
    remove_items_from_acf(p, [222])
    backups = list(tmp_path.glob("*.bak_*"))
    assert len(backups) == 1
    data = vdf.loads(backups[0].read_text(encoding="utf-8"))
    assert "222" in data["AppWorkshop"]["WorkshopItemsInstalled"]


def test_corrupt_file_refuses(tmp_path):
    p = tmp_path / "appworkshop_1.acf"
    p.write_text("不是 VDF 的内容 {{{", encoding="utf-8")
    with pytest.raises(ValueError):
        remove_items_from_acf(p, [111])


def test_nothing_to_remove_touches_nothing(tmp_path):
    p = _write_sample(tmp_path)
    before = p.read_bytes()
    removed, absent = remove_items_from_acf(p, [888])
    assert removed == [] and absent == [888]
    assert p.read_bytes() == before  # 文件未动、无备份产生
