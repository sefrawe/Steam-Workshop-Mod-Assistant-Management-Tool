-- 建表语句
-- ============================================================
-- SWMAMT v2 · core/schema.sql · user_version = 1（新世系，不接旧迁移链，D9）
-- 9 张表：games / mods / mod_snapshots / verdict_log / translations /
--        backups / operations_log / failed_mods / special_mod_alerts
--        + purged_mods / mod_dependencies（黑名单与依赖边表，不随 ledger 走）
--
-- 全局约定（沿旧 schema 三条 + 判决制新增）：
-- 1. 时间一律 Unix 秒 INTEGER，可读转换全交 GUI 层
-- 2. manifest 一律 TEXT（无符号 64 位逼近 INTEGER 上限，永不参与运算）
-- 3. JSON 字段存 TEXT，代码层 dumps/loads，数据库不做结构化查询
-- 4. ★ 判决制（D1/D2）：mods 表不再有 local_timeupdated / manifest——
--    本地版本基准 = confirmed_version，唯一写入点 = 确认门
--    （confirm_items / claim_accept，R17）；acf 从判定公式退场
-- ============================================================
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

-- ------------------------------------------------------------
-- games 游戏档案（与旧同构，原样迁移 D9）
-- ------------------------------------------------------------
CREATE TABLE games (
                       app_id INTEGER PRIMARY KEY,
                       name TEXT NOT NULL,
                       download_dir TEXT NOT NULL,       -- steamcmd content 目录，自动推导不开放手填
                       game_mod_dir TEXT,
                       backup_dir TEXT,                  -- NULL = 取默认值
                       created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- ------------------------------------------------------------
-- mods mod 主表（D2 判定公式：time_updated > confirmed_version → 需更新，仅 downloaded）
-- ------------------------------------------------------------
CREATE TABLE mods (
                      mod_id INTEGER PRIMARY KEY,
                      game_id INTEGER NOT NULL REFERENCES games(app_id) ON DELETE RESTRICT,
                      status TEXT NOT NULL DEFAULT 'tracked'
                          CHECK (status IN ('tracked','downloaded','deleted','failed')),
                      url TEXT,                          -- 工坊页面；urlParser 单源可现拼
                      title TEXT,
                      creator TEXT,                      -- 作者 SteamID64
                      time_created INTEGER,
                      time_updated INTEGER,              -- ★远端观测（检测三路写库）
                      last_time_updated INTEGER,         -- 上次远端观测（快照分母 + 提醒去重）
    -- ★ 本地确认基准三件套（R17：只许确认门写）
                      confirmed_version INTEGER,         -- NULL = 未确认 / 版本未知（备份守卫拒绝）
                      confirmed_at INTEGER,
                      confirmed_source TEXT
                          CHECK (confirmed_source IN ('verified','unverified','inherited_acf','claim','manual')),
                      local_size INTEGER,                -- 可选，只读盘点回填（D23）；不参与版本判定（R19）
                      file_size INTEGER,
                      subscriptions INTEGER,
                      favorited INTEGER,
                      views INTEGER,
                      tags TEXT,                         -- JSON 字符串数组
                      last_checked_at INTEGER,
                      preview_url TEXT,
                      is_special INTEGER NOT NULL DEFAULT 0 CHECK (is_special IN (0,1)),
                      note TEXT,
                      color_tag TEXT,
                      deleted_at INTEGER,
                      deleted_last_state TEXT,           -- 软删除前末态 JSON
                      first_tracked_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE INDEX idx_mods_status ON mods(status);
CREATE INDEX idx_mods_game ON mods(game_id);
CREATE INDEX idx_mods_special ON mods(is_special);
CREATE INDEX idx_mods_time_update ON mods(time_updated);

-- ------------------------------------------------------------
-- mod_snapshots 远端观测史（滚动保留 snapshot_keep 条，修剪在 repo 层）
-- manifest / local_timeupdated 列随判决制移除——快照只记远端侧
-- ------------------------------------------------------------
CREATE TABLE mod_snapshots (
                               id INTEGER PRIMARY KEY,
                               mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
                               snapshot_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                               time_updated INTEGER
);
CREATE INDEX idx_snapshots_mod ON mod_snapshots(mod_id, snapshot_at);

-- ------------------------------------------------------------
-- verdict_log 判决史 + 确认队列，一张表双角色（§二）：
--   pending = confirmed_at IS NULL 的 success/claim 行 → 确认队列（重启不丢）
--   确认 = 填 confirmed_at + 写 mods.confirmed_version（confirm_items 正门）
--   修剪（D23）：非确认行按 mod 保留最近 verdict_keep 条或 90 天；
--   确认行随 mod 生命周期保留——它是当前 confirmed_version 的票据
-- mod_id 刻意无外键：待确认条目可能尚未入账（D39），继承 failed_mods 先例
-- ------------------------------------------------------------
CREATE TABLE verdict_log (
                             id INTEGER PRIMARY KEY,
                             mod_id INTEGER NOT NULL,
                             game_id INTEGER NOT NULL,          -- C 级提案①：批次/认领归属档案（冗余存档，无外键）
                             kind TEXT NOT NULL
                                 CHECK (kind IN ('success','timeout','fail','claim','manual')),  -- C 级提案②
                             version_trigger INTEGER,           -- 触发值（D3 两来源）
                             version_query INTEGER,             -- 收尾批查现场值（D4）
                             version_written INTEGER,           -- 实际写入 confirmed_version 的值（D4 分支结果）
                             title TEXT,                        -- D39：落 pending 顺手存，离线可显
                             file_size INTEGER,                 -- D39：同上；确认未登记 mod 有据可建行
                             source TEXT,                       -- 写入 mods.confirmed_source 的值
                             confirmed_at INTEGER,              -- NULL = 待确认
                             occurred_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                             note TEXT
);
CREATE INDEX idx_verdict_mod ON verdict_log(mod_id, occurred_at);
CREATE INDEX idx_verdict_pending ON verdict_log(confirmed_at);

-- ------------------------------------------------------------
-- translations 简介翻译缓存（D35）：单 mod 单条当前译文，无历史
-- 翻译时现拉原文 → sha1 对比 source_hash → 相同显示缓存，不同 REPLACE 重译
-- FK CASCADE：mod 没了缓存没意义（机器生成数据，不随 ledger 走）
-- ------------------------------------------------------------
CREATE TABLE translations (
                              mod_id INTEGER PRIMARY KEY REFERENCES mods(mod_id) ON DELETE CASCADE,
                              source_hash TEXT NOT NULL,
                              target_lang TEXT NOT NULL,
                              engine TEXT,                       -- NULL = 浏览器翻译主路径未落缓存
                              text_translated TEXT NOT NULL,
                              translated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- ------------------------------------------------------------
-- backups / operations_log / failed_mods / special_mod_alerts（与旧同构原样迁移）
-- ------------------------------------------------------------
CREATE TABLE backups (
                         id INTEGER PRIMARY KEY,
                         mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE RESTRICT,
                         backup_path TEXT NOT NULL UNIQUE,
                         size_bytes INTEGER NOT NULL,
                         version_timeupdated INTEGER NOT NULL,   -- 取确认值（D14）；NULL 版本由守卫拒绝
                         manifest TEXT,                          -- 可空，备份时从 acf 盘面回填（D14）
                         created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                         pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
                         note TEXT
);
CREATE INDEX idx_backups_mod ON backups(mod_id, created_at);

CREATE TABLE operations_log (
                                id INTEGER PRIMARY KEY,
                                command TEXT NOT NULL,             -- R2：不存登录命令
                                executed_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                                error_count INTEGER NOT NULL DEFAULT 0,
                                result TEXT,
                                backup_id INTEGER REFERENCES backups(id) ON DELETE SET NULL
);
CREATE INDEX idx_oplog_time ON operations_log(executed_at);

-- mod_id 无外键：证据独立存活（彻底清账/删档案后仍可查）
CREATE TABLE failed_mods (
                             id INTEGER PRIMARY KEY,
                             mod_id INTEGER NOT NULL,
                             game_id INTEGER NOT NULL REFERENCES games(app_id) ON DELETE RESTRICT,
                             reason TEXT,
                             detected_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                             last_known_state TEXT,             -- 归档时字段快照 JSON（含 confirmed_version）
                             replaced_by INTEGER REFERENCES mods(mod_id) ON DELETE SET NULL
);
CREATE INDEX idx_failed_game ON failed_mods(game_id);

CREATE TABLE special_mod_alerts (
                                    id INTEGER PRIMARY KEY,
                                    mod_id INTEGER NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
                                    alert_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                                    remote_time_updated INTEGER NOT NULL,
                                    diff_seconds INTEGER,
                                    was_downloaded INTEGER NOT NULL DEFAULT 0 CHECK (was_downloaded IN (0,1)),
                                    note TEXT
);
CREATE INDEX idx_alerts_mod ON special_mod_alerts(mod_id, alert_at);

-- ------------------------------------------------------------
-- purged_mods 黑名单（跨档案存活；拦三处 = 认领/确认门/检测报告，D11）
-- ------------------------------------------------------------
CREATE TABLE purged_mods (
                             mod_id INTEGER PRIMARY KEY,
                             game_id INTEGER NOT NULL,
                             title TEXT,
                             purged_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                             note TEXT
);

-- ------------------------------------------------------------
-- mod_dependencies 依赖边表（schema v3 结构原样；required_mod_id 无外键）
-- ------------------------------------------------------------
CREATE TABLE mod_dependencies (
                                  mod_id INTEGER NOT NULL,
                                  required_mod_id INTEGER NOT NULL,
                                  fetched_at INTEGER NOT NULL,
                                  PRIMARY KEY (mod_id, required_mod_id)
);
CREATE INDEX idx_dep_req ON mod_dependencies(required_mod_id);
