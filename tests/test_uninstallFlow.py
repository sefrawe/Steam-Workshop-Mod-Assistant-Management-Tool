"""卸载盘点逻辑测试（决策 70）"""
from workflows.uninstallFlow import (
    describe_file, registry_key_path, scan_folder,
)


def _touch(p, size: int = 10) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_describe_known_files():
    assert "账本数据库" in describe_file("mods.db")
    assert "全局设置" in describe_file("GlobalSettings.json")


def test_describe_db_extras():
    assert "滚动备份" in describe_file("mods_20250101.db")
    assert "附属文件" in describe_file("mods.db-wal")


def test_describe_settings_and_bak():
    assert "自动备份" in describe_file("GlobalSettings.json.bak")


def test_describe_session_ini():
    assert "界面会话记忆" in describe_file("session.ini")
    assert "界面会话记忆" in describe_file("MyOrg.MyApp.ini")


def test_describe_fallback():
    assert describe_file("something-else.dat")  # 不空，有兜底说明


def test_registry_key_path():
    assert registry_key_path("MyOrg", "MyApp") == \
        r"HKEY_CURRENT_USER\Software\MyOrg\MyApp"
    assert registry_key_path("  ", "MyApp") == \
        r"HKEY_CURRENT_USER\Software\MyApp"


def test_scan_folder_lists_data_and_config(tmp_path):
    _touch(tmp_path / "data" / "mods.db", 100)
    _touch(tmp_path / "config" / "GlobalSettings.json", 50)
    _touch(tmp_path / "config" / "MyOrg" / "MyApp.ini", 20)
    _touch(tmp_path / "readme.txt", 5)  # 程序本体不计入盘点
    rep = scan_folder(tmp_path)
    rels = {e.rel_path for e in rep.entries}
    assert rels == {"data/mods.db", "config/GlobalSettings.json",
                    "config/MyOrg/MyApp.ini"}
    assert rep.total_bytes == 170
    assert rep.data_dir_exists and rep.config_dir_exists


def test_scan_folder_empty_root(tmp_path):
    rep = scan_folder(tmp_path)
    assert rep.entries == []
    assert rep.total_bytes == 0
    assert not rep.data_dir_exists and not rep.config_dir_exists
