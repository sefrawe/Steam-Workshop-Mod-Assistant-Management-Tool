"""M0 测试公共夹具
"""
"""
tests/conftest.py · 各测试文件共用的东西住这里。

提供两样：
1. repo 夹具——现成的新仓库。滚动保留条数压到 3，让"滚动淘汰"
   的用例不用造大量数据就能触发；
2. make_old_db 夹具——在临时目录里搭一个"上一代工具"的旧库。
   表结构是旧版 schema 的精简版（本地版本列叫 local_timeupdated、
   没有 verdict_log），给迁移测试和开库鉴别测试当靶子。
   刻意不打开 foreign_keys：真实旧库里可能存在孤儿行，夹具要能
   造出脏数据，才能测到迁移的防御。
"""
import sqlite3

import pytest

from core.sqliteRepository import SQLiteRepository

# 旧库精简 schema：只保留迁移与鉴别要用的表和列，约束照抄旧版
_OLD_SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE games (
    app_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    download_dir TEXT NOT NULL,
    game_mod_dir TEXT,
    backup_dir TEXT,
    created_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE mods (
    mod_id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(app_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'tracked'
        CHECK (status IN ('tracked','downloaded','deleted','failed')),
    url TEXT, title TEXT, creator TEXT,
    time_created INTEGER, time_updated INTEGER, last_time_updated INTEGER,
    local_timeupdated INTEGER, manifest TEXT, local_size INTEGER,
    file_size INTEGER, subscriptions INTEGER, favorited INTEGER,
    views INTEGER, tags TEXT, last_checked_at INTEGER, preview_url TEXT,
    is_special INTEGER NOT NULL DEFAULT 0 CHECK (is_special IN (0,1)),
    note TEXT, color_tag TEXT, local_path TEXT,
    deleted_at INTEGER, deleted_last_state TEXT,
    first_tracked_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE mod_snapshots (
    id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    snapshot_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    time_updated INTEGER, manifest TEXT, local_timeupdated INTEGER
);
CREATE TABLE backups (
    id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE RESTRICT,
    backup_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    version_timeupdated INTEGER NOT NULL,
    manifest TEXT,
    created_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    note TEXT
);
CREATE TABLE operations_log (
    id INTEGER PRIMARY KEY,
    command TEXT NOT NULL,
    executed_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    error_count INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    backup_id INTEGER REFERENCES backups(id) ON DELETE SET NULL
);
CREATE TABLE failed_mods (
    id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL REFERENCES games(app_id) ON DELETE RESTRICT,
    reason TEXT,
    detected_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    last_known_state TEXT,
    replaced_by INTEGER REFERENCES mods(mod_id) ON DELETE SET NULL
);
CREATE TABLE special_mod_alerts (
    id INTEGER PRIMARY KEY,
    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
    alert_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    remote_time_updated INTEGER NOT NULL,
    diff_seconds INTEGER,
    was_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (was_downloaded IN (0,1)),
    note TEXT
);
CREATE TABLE purged_mods (
    mod_id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL,
    title TEXT,
    purged_at INTEGER NOT NULL
        DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    note TEXT
);
CREATE TABLE mod_dependencies (
    mod_id INTEGER NOT NULL,
    required_mod_id INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (mod_id, required_mod_id)
);
"""


@pytest.fixture
def repo(tmp_path):
    """现成的新仓库：保留条数压到 3，滚动淘汰的用例好触发。"""
    r = SQLiteRepository(tmp_path / "mods.db",
                         snapshot_keep=3, verdict_keep=3)
    yield r
    r.close()


@pytest.fixture
def make_old_db(tmp_path):
    """造一个上一代工具的旧库，返回 (路径, 连接)。
    连接不自动关——用例插完数据自己 close（先关再交给 peek/migrate，
    跟真实使用一样：旧库不会被迁移程序握着不放）。"""
    def _make(name: str = "old.db"):
        path = tmp_path / name
        conn = sqlite3.connect(path)
        conn.executescript(_OLD_SCHEMA)
        return path, conn
    return _make
