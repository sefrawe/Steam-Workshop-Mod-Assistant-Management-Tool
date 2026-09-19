-- 建表语句
-- ============================================================
-- Steam Workshop Mod Assistant Management Tool
-- core/schema.sql · v1.1 · 7 张表
--
-- 全局约定：
--   1. 所有时间字段一律存 Unix 时间戳（秒 / INTEGER），与 Steam API
--      和 acf 的原生格式一致；"3 天前"这类可读转换全部交给 GUI 层
--   2. manifest 一律存 TEXT。Steam 的 manifest 是无符号 64 位整数，
--      样本中实测最大 8715384612056780827（≈8.7e18），已逼近 SQLite
--      INTEGER 上限 9.22e18，且永不参与数学运算，TEXT 绝对安全
--   3. JSON 字段（tags / deleted_last_state / last_known_state）存
--      TEXT，由代码 json.dumps/loads，数据库不做结构化查询
--   4. journal_mode=WAL 是持久属性，建库时设置一次即随库文件生效；
--      foreign_keys 是连接级属性，sqliteRepository 每次建连必须重新
--      执行 PRAGMA foreign_keys = ON（本脚本里的只对建表期生效）
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;   -- schema 版本号，将来表结构变更时 +1，用于迁移判断

-- ------------------------------------------------------------
-- games  游戏档案
-- ------------------------------------------------------------
CREATE TABLE games (
                       app_id       INTEGER PRIMARY KEY,              -- Steam AppID（CK3=1158310 / RimWorld=294100）
                       name         TEXT    NOT NULL,                 -- 游戏名（向导中由 API 自动获取）
                       download_dir TEXT    NOT NULL,                 -- steamcmd force_install_dir 指向的目录
                       game_mod_dir TEXT,                             -- 游戏本体 mod 目录（如 CK3 的 Documents 路径），可空
                       backup_dir   TEXT,                             -- mod 备份根目录；NULL = 代码取默认值
    --   （下载目录同级 mod_backups/<appid>/，见 T6）
                       created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- ------------------------------------------------------------
-- mods  mod 主表
-- 注意两个故意区分的名字：
--   time_updated      = 远端最新版本（API 字段名，带下划线）
--   local_timeupdated = 本地版本（acf 字段名 timeupdated，无下划线）
--   更新判定：time_updated > local_timeupdated 即需更新
-- ------------------------------------------------------------
CREATE TABLE mods (
                      mod_id             INTEGER PRIMARY KEY,         -- Steam publishedfileid，全工坊唯一
                      game_id            INTEGER NOT NULL
                          REFERENCES games(app_id) ON DELETE RESTRICT,
    -- RESTRICT：档案下还有 mod 时禁止删档案
                      status             TEXT    NOT NULL DEFAULT 'tracked'
                          CHECK (status IN ('tracked','downloaded','deleted','failed')),
                      url                TEXT,                        -- 工坊页面；acf 扫描冷启动时可为 NULL，待 API 补
                      title              TEXT,
                      creator            TEXT,                        -- 作者 SteamID64 → 可拼 profiles/<id> 链接
                      time_created       INTEGER,                     -- API time_created
                      time_updated       INTEGER,                     -- ★远端最新版本
                      last_time_updated  INTEGER,                     -- 上次快照时的远端值（快照生成 + 特殊提醒去重用）

    -- ↓ 本地版本三件套，全部来自 acf 解析；NULL = 尚未下载（tracked）
                      local_timeupdated  INTEGER,                     -- ★本地版本（更新判定的本地基准）
                      manifest           TEXT,                        -- ★内容版本号，TEXT！见文件头约定 2
                      local_size         INTEGER,                     -- ★本地占用（字节，来自 acf size）

                      file_size          INTEGER,                     -- API file_size（acf 缺失时的兜底显示）
                      subscriptions      INTEGER,                     -- API 订阅数
                      favorited          INTEGER,                     -- API 收藏数
                      views              INTEGER,                     -- API 浏览数
                      tags               TEXT,                        -- API 标签，JSON 字符串数组
                      last_checked_at    INTEGER,                     -- 仅用于"多久没检测"展示，不参与判定
                      preview_url        TEXT,
                      is_special         INTEGER NOT NULL DEFAULT 0 CHECK (is_special IN (0,1)),
                      note               TEXT,
                      color_tag          TEXT,                        -- 颜色标记（标签分类的弱化替代）
                      local_path         TEXT,                        -- 实际落盘路径缓存（= download_dir/<mod_id>）
                      deleted_at         INTEGER,                     -- 软删除时间（v1 无物理删除入口）
                      deleted_last_state TEXT,                        -- 软删除前的末态 JSON
                      first_tracked_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

CREATE INDEX idx_mods_status      ON mods(status);
CREATE INDEX idx_mods_game        ON mods(game_id);
CREATE INDEX idx_mods_special     ON mods(is_special);
CREATE INDEX idx_mods_time_update ON mods(time_updated);

-- ------------------------------------------------------------
-- mod_snapshots  只读历史时间线
-- 每 mod 仅保留最近 10 条：插入新快照后由 sqliteRepository 删更旧的
-- ------------------------------------------------------------
CREATE TABLE mod_snapshots (
                               id                INTEGER PRIMARY KEY,
                               mod_id            INTEGER NOT NULL
                                   REFERENCES mods(mod_id) ON DELETE CASCADE,
                               snapshot_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                               time_updated      INTEGER,                      -- 快照时刻的远端版本
                               manifest          TEXT,
                               local_timeupdated INTEGER                       -- 快照时刻的本地版本
);

CREATE INDEX idx_snapshots_mod ON mod_snapshots(mod_id, snapshot_at);

-- ------------------------------------------------------------
-- backups  mod 内容备份登记表
-- 对应磁盘上 <modid>_v<timeupdated>_<时间戳> 目录，一目录一行
-- ------------------------------------------------------------
CREATE TABLE backups (
                         id                  INTEGER PRIMARY KEY,
                         mod_id              INTEGER NOT NULL
                             REFERENCES mods(mod_id) ON DELETE RESTRICT,
    -- 备份代表磁盘文件，不随记录级联消失，
    -- 须先经备份管理页显式处理
                         backup_path         TEXT    NOT NULL UNIQUE,    -- UNIQUE：防同一目录重复登记
                         size_bytes          INTEGER NOT NULL,
                         version_timeupdated INTEGER NOT NULL,           -- 备份时的本地版本
                         manifest            TEXT,
                         created_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                         pinned              INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    -- 1 = 豁免自动清理
                         note                TEXT
);

CREATE INDEX idx_backups_mod ON backups(mod_id, created_at);

-- ------------------------------------------------------------
-- operations_log  命令 / 操作日志
-- ------------------------------------------------------------
CREATE TABLE operations_log (
                                id          INTEGER PRIMARY KEY,
                                command     TEXT    NOT NULL,
                                executed_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                                error_count INTEGER NOT NULL DEFAULT 0,
                                result      TEXT,                               -- success / error / timeout / ...
    -- 由 outputAnalyzer 归类填写
                                backup_id   INTEGER
                                                    REFERENCES backups(id) ON DELETE SET NULL
);                                                  -- SET NULL：日志要活得比备份记录久

CREATE INDEX idx_oplog_time ON operations_log(executed_at);

-- ------------------------------------------------------------
-- failed_mods  失效归档表
-- mod_id 故意不设外键：这张表的价值是"证据"——即使对应 mods 行
-- 将来被物理清理，归档记录也必须原样保留（旧 id 复活的查询线索）
-- ------------------------------------------------------------
CREATE TABLE failed_mods (
                             id               INTEGER PRIMARY KEY,
                             mod_id           INTEGER NOT NULL,              -- 原 id（无外键，理由见上）
                             game_id          INTEGER NOT NULL
                                 REFERENCES games(app_id) ON DELETE RESTRICT,
                             reason           TEXT,                          -- result=9 / 文件夹缺失 / ...
                             detected_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                             last_known_state TEXT,                          -- 归档时的字段快照 JSON
                             replaced_by      INTEGER
                                                      REFERENCES mods(mod_id) ON DELETE SET NULL
);                                                  -- 关联替换后指向新 id；新 id 再被删则置空

CREATE INDEX idx_failed_game ON failed_mods(game_id);

-- ------------------------------------------------------------
-- special_mod_alerts  特殊 mod 提醒历史
-- 更新后需人工再处理的 mod（假中文等），每次提醒落一行
-- ------------------------------------------------------------
CREATE TABLE special_mod_alerts (
                                    id                  INTEGER PRIMARY KEY,
                                    mod_id              INTEGER NOT NULL
                                        REFERENCES mods(mod_id) ON DELETE CASCADE,
                                    alert_at            INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                                    remote_time_updated INTEGER NOT NULL,           -- 触发提醒时的远端版本（同一版本只提醒一次的比对依据）
                                    diff_seconds        INTEGER,                    -- 与上次更新的间隔
                                    was_downloaded      INTEGER NOT NULL DEFAULT 0 CHECK (was_downloaded IN (0,1)),
                                    note                TEXT
);

CREATE INDEX idx_alerts_mod ON special_mod_alerts(mod_id, alert_at);
