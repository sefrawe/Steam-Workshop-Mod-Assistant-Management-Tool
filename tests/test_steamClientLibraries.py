r"""Steam 客户端库自动探测 · 合成样本测试（真注册表绝不进 pytest）"""

from core import steamPaths


def _write_vdf(steam_dir, pairs: list[tuple[str, str]]) -> None:
    """写一份 libraryfolders.vdf（新版结构）。路径一律 as_posix 传入，
    免得合成样本里打架反斜杠转义；normcase 去重对两种斜杠一视同仁。"""
    apps = steam_dir / "steamapps"
    apps.mkdir(parents=True)
    inner = ",\n".join(
        f'\t"{key}"\n\t{{\n\t\t"path"\t\t"{path}"\n\t}}'
        for key, path in pairs)
    text = f'"libraryfolders"\n{{\n{inner}\n}}\n'
    (apps / "libraryfolders.vdf").write_text(text, encoding="utf-8")


def test_new_format_lists_all_existing(tmp_path):
    primary = tmp_path / "steam"
    second = tmp_path / "d library"
    primary.mkdir()
    second.mkdir()
    _write_vdf(primary, [("0", primary.as_posix()),
                         ("1", second.as_posix())])
    roots = steamPaths.client_library_roots(install_dir=str(primary))
    assert roots == [str(primary), str(second)]


def test_old_format_string_values(tmp_path):
    primary = tmp_path / "steam"
    other = tmp_path / "lib2"
    primary.mkdir()
    other.mkdir()
    apps = primary / "steamapps"
    apps.mkdir()
    (apps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\t\t"%s"\n\t"1"\t\t"%s"\n}\n'
        % (primary.as_posix(), other.as_posix()), encoding="utf-8")
    roots = steamPaths.client_library_roots(install_dir=str(primary))
    assert roots == [str(primary), str(other)]


def test_missing_vdf_returns_primary(tmp_path):
    primary = tmp_path / "steam"
    primary.mkdir()
    roots = steamPaths.client_library_roots(install_dir=str(primary))
    assert roots == [str(primary)]


def test_broken_vdf_falls_back(tmp_path):
    primary = tmp_path / "steam"
    primary.mkdir()
    apps = primary / "steamapps"
    apps.mkdir()
    (apps / "libraryfolders.vdf").write_text("not a vdf {{{", encoding="utf-8")
    roots = steamPaths.client_library_roots(install_dir=str(primary))
    assert roots == [str(primary)]


def test_dedup_and_missing_filtered(tmp_path):
    primary = tmp_path / "steam"
    ghost = tmp_path / "ghost"  # vdf 里登记了但盘上没有
    primary.mkdir()
    _write_vdf(primary, [("0", primary.as_posix()),
                         ("1", ghost.as_posix()),
                         ("2", str(primary).upper())])
    roots = steamPaths.client_library_roots(install_dir=str(primary))
    assert roots == [str(primary)]  # ghost 被存在性过滤；大写重复被去重


def test_registry_chain_monkeypatched(tmp_path, monkeypatch):
    primary = tmp_path / "steam"
    other = tmp_path / "lib2"
    primary.mkdir()
    other.mkdir()
    _write_vdf(primary, [("0", primary.as_posix()),
                         ("1", other.as_posix())])
    monkeypatch.setattr(steamPaths, "_steam_install_dir", lambda: str(primary))
    assert steamPaths.client_library_roots() == [str(primary), str(other)]


def test_no_steam_found(monkeypatch):
    monkeypatch.setattr(steamPaths, "_steam_install_dir", lambda: None)
    assert steamPaths.client_library_roots() == []
