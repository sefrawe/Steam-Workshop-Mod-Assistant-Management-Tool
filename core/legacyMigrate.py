"""旧账本一次性迁移（legacyMigrate）
"""
"""
core/legacyMigrate.py · 把上一代工具的账本搬进新账本，只搬这一次。

背景：新旧两代工具对"本地版本"的定义不同——旧版信 acf 扫描
（local_timeupdated 列，扫到什么写什么）；新版信人工确认
（confirmed_version，只有确认门能写）。表结构因此不同，两代工具
不能直接打开对方的库，本文件就是把旧账"翻译"成新账的搬运工。

对外只暴露两个函数：
1. peek_old_ledger —— 看一眼旧库是什么、里面有多少东西。迁移页先
   拿它出确认弹窗（"旧账里有 2 个档案、237 条 mod…确定搬吗？"），
   先看清楚再动手，与删除档案前的盘点同一哲学。
2. migrate_old_ledger —— 真正搬运。旧库行 → 新库行的字段翻译全部
   在本文件完成（对照表见 _remap_mod 的注释）；写库走新仓库现成的
   import_all / replace_dependencies / add_purged——迁移不发明任何
   新写库代码。搬完返回 MigrationReport（搬了什么、跳过什么）。

四条铁律：
- 对旧库只读（mode=ro 打开），一个字节都不写——旧账本是用户的
  唯一底稿（R10 不碰真库的迁移版：只读不算碰）；
- 新库必须空着才许搬——往有账的库里灌旧账必然重复撞车，直接拒绝；
- 一次事务搬完全部（外层 transaction() 包住，import_all 自动并入），
  中途任何失败整体回滚；
- 继承口径（D9）：旧账带来的本地版本一律标 confirmed_source=
  "inherited_acf"（⤵继承旧账徽章）——它来自 acf 而非人工确认，
  徽章必须说实话。确认时刻记为迁移时刻（旧账没有"何时确认"的记录）。

已知取舍：黑名单（purged_mods）的登记时间不保真——契约的 add_purged
不收时间参数，搬过来的行统一记为迁移时刻。名单"拦不拦"的功能
毫发无损，只有「已清账」页的排序会变；报告里会提醒。
"""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.modRepository import ModRepository

# ------------------------------------------------------------------
# 两个世界的鉴别标记
# ------------------------------------------------------------------
# 旧世界 mods 表独有的列名（新版已改叫 confirmed_version）
_OLD_MARKER_COLUMN = "local_timeupdated"
# 新世界独有的表名（旧版没有判决制）。与 sqliteRepository.__init__ 里
# 的鉴别标记是同一个东西——两处都指 schema.sql 的 verdict_log 表，
# 改表名时两边一起改。
_NEW_MARKER_TABLE = "verdict_log"

# 旧库读表顺序 = 外键依赖序（父表在前）。计数与搬运都用这个序；
# 更老的旧库可能缺尾部几张表（v1 没有黑名单和依赖边），缺了按空表处理。
_OLD_READ_ORDER: tuple[str, ...] = (
    "games", "mods", "mod_snapshots", "backups", "operations_log",
    "failed_mods", "special_mod_alerts", "purged_mods", "mod_dependencies",
)

# 新世系当前的 schema 版本（与 schema.sql 的 PRAGMA user_version 同义）。
# 迁移载荷永远按"当前结构"组装，这里写 1；将来 schema 升版时导入闸
# 依然放行（载荷版本 ≤ 库版本即可），不用跟着改。
_NEW_WORLD_USER_VERSION = 1

# 继承来源的值（与 constants.CONFIRMED_SOURCE_ZH 的键、schema 的
# CHECK 约定一致；这里用字面量并注释指向单源，避免常量文件反向膨胀）
_INHERITED_SOURCE = "inherited_acf"


# ------------------------------------------------------------------
# 两个返回结构
# ------------------------------------------------------------------

@dataclass
class OldLedgerSummary:
    """peek_old_ledger 的结果：旧库是什么、有什么。确认弹窗直接展示。"""
    path: str
    kind: str  # old / new / empty / alien / not_sqlite / missing
    user_version: int | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def is_migratable(self) -> bool:
        """只有旧世界的库才搬得动——其余一切情形调用方直接给对应文案。"""
        return self.kind == "old"

    def summary(self) -> str:
        """一句话人话描述（弹窗第一行）。"""
        return _KIND_ZH.get(self.kind, self.kind)


@dataclass
class MigrationReport:
    """migrate_old_ledger 的结果：搬了什么、跳过什么、要人知道什么。"""
    games: int = 0
    mods: int = 0
    mods_inherited: int = 0        # 带着旧本地版本进来的（⤵继承旧账）
    mods_version_unknown: int = 0  # 旧账里"已下载但版本未知"的条目
    snapshots: int = 0
    backups: int = 0
    operations: int = 0
    failed: int = 0
    alerts: int = 0
    purged: int = 0
    dependency_edges: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """一行报数（写日志 / 状态栏用），带提醒条数。"""
        parts = [
            f"档案 {self.games}", f"mod {self.mods}",
            f"继承本地版本 {self.mods_inherited}",
            f"版本未知 {self.mods_version_unknown}",
            f"远端观测史 {self.snapshots}", f"备份登记 {self.backups}",
            f"操作日志 {self.operations}", f"失效归档 {self.failed}",
            f"特殊提醒 {self.alerts}", f"黑名单 {self.purged}",
            f"依赖边 {self.dependency_edges}",
        ]
        text = "；".join(parts)
        if self.purged:
            text += "（黑名单登记时间统一记为迁移时刻）"
        if self.warnings:
            text += f"；⚠ {len(self.warnings)} 条提醒（详见运行日志）"
        return text


# peek 结果 kind 的人话对照（弹窗/日志共用，别各写各的）
_KIND_ZH = {
    "old": "上一代工具的旧账本（可迁移）",
    "new": "已经是新版账本，无需迁移",
    "empty": "空库，没有可迁移的数据",
    "alien": "不是本工具的账本（表结构对不上）",
    "not_sqlite": "这个文件不是 SQLite 数据库",
    "missing": "文件不存在",
}

# 计数显示顺序与中文标签（弹窗里"档案 2 · mod 237 …"那一行）
_COUNT_ZH: tuple[tuple[str, str], ...] = (
    ("games", "档案"), ("mods", "mod"), ("mod_snapshots", "远端观测史"),
    ("backups", "备份登记"), ("operations_log", "操作日志"),
    ("failed_mods", "失效归档"), ("special_mod_alerts", "特殊提醒"),
    ("purged_mods", "黑名单"), ("mod_dependencies", "依赖边"),
)


# ------------------------------------------------------------------
# 旧库的小工具（只读）
# ------------------------------------------------------------------

def _open_old(p: Path) -> sqlite3.Connection:
    """只读打开旧库。mode=ro 保证迁移对旧账本一个字节都不写——
    就算代码写错也不会污染用户的底稿。as_uri() 把路径里的空格、
    中文、问号统统转成合法的 URI 转义，Windows 盘符也能正确处理。
    row_factory=Row：后续 dict(row) 全靠它——不设的话 fetchall
    拿到的是裸元组，dict() 转换当场炸（本轮实测修掉的坑）。"""
    uri = p.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn



def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _classify(conn: sqlite3.Connection) -> str:
    """鉴别这个库属于哪个世界（peek 的核心判断）。
    判序很重要：先看 mods 表在不在（空库/陌生文件），再看新世界
    标记表（已是新账本），最后看旧世界的独有列（是旧账本）。
    三样都对不上 = alien，绝不硬吃。"""
    tables = _table_names(conn)
    if "mods" not in tables:
        return "empty"
    if _NEW_MARKER_TABLE in tables:
        return "new"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()}
    if _OLD_MARKER_COLUMN in cols:
        return "old"
    return "alien"


def _read_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """整表读成 dict 列表。表名全部来自 _OLD_READ_ORDER 字面量，
    用户数据走不到这里——无注入。"""
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


# ------------------------------------------------------------------
# 字段翻译（旧行 → 新行）。全部纯函数，方便测试逐个喂行验证。
# ------------------------------------------------------------------

def _loads_json(text, warnings: list[str], label: str):
    """旧库里的 JSON 文本 → Python 对象。坏了不硬吃：记提醒、
    返回 None（这条数据的 JSON 部分放弃，其余字段照搬）。"""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        warnings.append(f"{label} 的 JSON 字段损坏，已放弃该段内容")
        return None


def _remap_state_json(text, warnings: list[str], label: str) -> dict | None:
    """旧"末态快照"JSON → 新口径。用在两处：软删除的
    deleted_last_state、失效归档的 last_known_state——两处的旧字段
    清单相同，翻译规则也相同，所以共用一份。
    改名对照：local_timeupdated → confirmed_version（并补
    confirmed_source=继承旧账）；
    丢弃：manifest / local_path（新账本没有这两个概念）；
    其余键（title/url/time_updated/local_size/note/color_tag/
    is_special）原名原值照搬。"""
    state = _loads_json(text, warnings, label)
    if not isinstance(state, dict):
        return None
    new: dict = {}
    for key in ("title", "url", "time_updated", "local_size",
                "note", "color_tag", "is_special"):
        if key in state:
            new[key] = state[key]
    old_version = state.get("local_timeupdated")
    if old_version is not None:
        new["confirmed_version"] = old_version
        new["confirmed_source"] = _INHERITED_SOURCE
    return new


def _remap_mod(old: dict, migration_time: int,
               warnings: list[str]) -> dict:
    """旧 mods 行 → 新 mods 行（自然类型 dict，直接喂 import_all）。
    字段对照（D9 迁移映射的代码版）：
      local_timeupdated → confirmed_version，来源=⤵继承旧账，
        确认时刻=迁移时刻（旧账没有"何时确认"的记录，迁移时刻
        是唯一诚实的时间）；
      manifest / local_path → 丢弃（新账本不再有这两列）；
      tags / deleted_last_state → JSON 文本还原成 list / dict
        （import_all 吃自然类型，存储格式的转换它自己会做）；
      is_special → 0/1 转 bool；
      其余字段原名原值照搬（含软删除的 deleted_at——已删除的条目
        照样搬成已删除，将来在库页右键恢复，恢复到哪一档由
        有没有确认版本自动判断，正好衔接）。
    旧账里"已下载但没有本地版本"的条目（旧 T18 手动确认入账的）：
      confirmed_* 全空 = 新口径的"版本未知"，下轮检测照常分桶。"""
    old_version = old.get("local_timeupdated")
    return {
        "mod_id": old["mod_id"],
        "game_id": old["game_id"],
        "status": old["status"],
        "url": old.get("url"),
        "title": old.get("title"),
        "creator": old.get("creator"),
        "time_created": old.get("time_created"),
        "time_updated": old.get("time_updated"),
        "last_time_updated": old.get("last_time_updated"),
        "confirmed_version": old_version,
        "confirmed_at": migration_time if old_version is not None else None,
        "confirmed_source": (_INHERITED_SOURCE
                             if old_version is not None else None),
        "local_size": old.get("local_size"),
        "file_size": old.get("file_size"),
        "subscriptions": old.get("subscriptions"),
        "favorited": old.get("favorited"),
        "views": old.get("views"),
        "tags": _loads_json(old.get("tags"), warnings,
                            f"mod {old['mod_id']} 的标签"),
        "last_checked_at": old.get("last_checked_at"),
        "preview_url": old.get("preview_url"),
        "is_special": bool(old.get("is_special")),
        "note": old.get("note"),
        "color_tag": old.get("color_tag"),  # 旧值可能是中文色名等遗留
        # 值——库页渲染的 normalize 函数会救回来，这里原样带过
        "deleted_at": old.get("deleted_at"),
        "deleted_last_state": _remap_state_json(
            old.get("deleted_last_state"), warnings,
            f"mod {old['mod_id']} 的软删除末态"),
        "first_tracked_at": old.get("first_tracked_at"),
    }


# ------------------------------------------------------------------
# peek：先看清楚
# ------------------------------------------------------------------
def peek_old_ledger(old_db_path: str | Path) -> OldLedgerSummary:
    """看一眼目标文件：是什么世界的库、旧账里各表有多少行。
    只读不写、只开不锁——失败每一种都有自己的 kind，迁移页据此
    给对应的文案，不抛异常（看一眼这个动作本身没有对错）。

    坑（本轮实测）：sqlite3.connect 是惰性的——连纯文本冒充的库
    文件也能"连上"，要到第一条真正读文件的语句才炸 DatabaseError。
    所以"是不是库"的判断必须包住鉴别查询本身，不能只包 connect。"""
    p = Path(old_db_path)
    if not p.is_file():
        return OldLedgerSummary(path=str(p), kind="missing")
    conn = None
    try:
        conn = _open_old(p)
        try:
            kind = _classify(conn)   # 第一条真读文件的语句，假库在这炸
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            # 不是 SQLite 库（或已损坏）：给人话结论，不抛
            return OldLedgerSummary(path=str(p), kind="not_sqlite")
        counts: dict[str, int] = {}
        if kind == "old":
            tables = _table_names(conn)
            for table, _zh in _COUNT_ZH:
                if table in tables:
                    counts[table] = conn.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return OldLedgerSummary(path=str(p), kind=kind,
                                user_version=version, counts=counts)
    except sqlite3.DatabaseError:
        # 个别环境在 connect 阶段就炸（磁盘/权限的异常形态）：同口径
        return OldLedgerSummary(path=str(p), kind="not_sqlite")
    finally:
        if conn is not None:
            conn.close()



# ------------------------------------------------------------------
# migrate：真正搬运
# ------------------------------------------------------------------

def migrate_old_ledger(old_db_path: str | Path,
                       new_repo: ModRepository) -> MigrationReport:
    """把旧账本整体搬进 new_repo 指向的新库，返回搬运报告。
    前置两道闸（都在动手之前拦下，不做一半再后悔）：
    1. 旧文件必须是旧世界的库（peek 鉴别）；
    2. 新库必须空着——已有档案或待确认条目都算"不空"
       （mod 必然挂在档案下，查档案就够；待确认判决行没有外键
       约束，单独再查一遍）。
    搬运全程一个事务：翻译阶段出错抛异常回滚；写库阶段任何一行
    被数据库约束拒绝也整体回滚——新库绝不留半截账。"""
    report = MigrationReport()

    # ---- 闸 1：旧文件验明正身 ----
    info = peek_old_ledger(old_db_path)
    if not info.is_migratable:
        raise ValueError(
            f"「{info.path}」{info.summary()}——迁移只处理上一代工具的旧账本")
    # ---- 闸 2：新库必须是空的 ----
    if new_repo.list_games() or new_repo.pending_confirmations():
        raise ValueError(
            "目标账本不是空库（已有档案或待确认条目）：迁移只能搬进"
            "空库。请先确认新工具指向的是一个全新的数据库文件")

    migration_time = int(time.time())
    warnings = report.warnings

    conn = _open_old(Path(old_db_path))
    try:
        tables = _table_names(conn)
        raw: dict[str, list[dict]] = {}
        for table in _OLD_READ_ORDER:
            if table in tables:
                raw[table] = _read_rows(conn, table)
            else:
                # 更老的旧库（v1/v2）天生没有尾部两张表——正常情况，
                # 不算提醒；只有缺中间的表才值得说一声
                if table in ("purged_mods", "mod_dependencies"):
                    raw[table] = []
                else:
                    raw[table] = []
                    warnings.append(f"旧库里没有 {table} 表，按空表处理")

        # ---- games：字段完全同名，原样成行 ----
        game_ids = {g["app_id"] for g in raw["games"]}
        games_rows = raw["games"]
        report.games = len(games_rows)

        # ---- mods：翻译 + 孤儿过滤 + 状态值校验 ----
        valid_statuses = {"tracked", "downloaded", "deleted", "failed"}
        mods_rows: list[dict] = []
        mod_ids: set[int] = set()
        for row in raw["mods"]:
            if row["game_id"] not in game_ids:
                # 正常旧库不该有（外键管着），防御性跳过不让一条
                # 脏行炸掉整个迁移
                warnings.append(
                    f"mod {row['mod_id']} 所属的档案不存在，已跳过")
                continue
            if row["status"] not in valid_statuses:
                warnings.append(
                    f"mod {row['mod_id']} 的状态值无法识别"
                    f"（{row['status']!r}），已跳过")
                continue
            mods_rows.append(_remap_mod(row, migration_time, warnings))
            mod_ids.add(row["mod_id"])
            if row.get("local_timeupdated") is not None:
                report.mods_inherited += 1
            elif row["status"] == "downloaded":
                report.mods_version_unknown += 1
        report.mods = len(mods_rows)

        # ---- 快照：只留远端侧（manifest / 本地版本列丢弃，D9） ----
        snap_rows = [{"id": r["id"], "mod_id": r["mod_id"],
                      "snapshot_at": r["snapshot_at"],
                      "time_updated": r.get("time_updated")}
                     for r in raw["mod_snapshots"] if r["mod_id"] in mod_ids]
        report.snapshots = len(snap_rows)

        # ---- 备份登记：同名照搬，钉住转 bool ----
        backup_ids: set[int] = set()
        backup_rows = []
        for r in raw["backups"]:
            if r["mod_id"] not in mod_ids:
                warnings.append(
                    f"备份登记 {r['id']}（{r['backup_path']}）所属的 "
                    f"mod 不存在，已跳过")
                continue
            if r.get("version_timeupdated") is None:
                # 新 schema version_timeupdated NOT NULL；硬灌会炸整体回滚。
                # 诚实跳过（盘上目录不动，账上不收），与 mod 缺失同款处理。
                warnings.append(
                    f"备份登记 {r['id']}（{r['backup_path']}）没有版本号，已跳过")
                continue
            backup_ids.add(r["id"])
            backup_rows.append({**r, "pinned": bool(r["pinned"])})
        report.backups = len(backup_rows)

        # ---- 操作日志：照搬；backup_id 悬空就置空（不让一条脏引用
        #      炸掉导入，日志少一个关联不影响历史可读） ----
        op_rows = []
        for r in raw["operations_log"]:
            row = dict(r)
            if row.get("backup_id") is not None \
                    and row["backup_id"] not in backup_ids:
                row["backup_id"] = None
                warnings.append(
                    f"操作日志 {row['id']} 指向的备份登记不存在，"
                    "已把关联置空")
            op_rows.append(row)
        report.operations = len(op_rows)

        # ---- 失效归档：JSON 末态换算 + replaced_by 悬空置空 ----
        failed_rows = []
        for r in raw["failed_mods"]:
            if r["game_id"] not in game_ids:
                warnings.append(f"失效归档 {r['id']} 所属档案不存在，已跳过")
                continue
            row = dict(r)
            row["last_known_state"] = _remap_state_json(
                r.get("last_known_state"), warnings,
                f"失效归档 {r['id']} 的末态快照")
            if row.get("replaced_by") is not None \
                    and row["replaced_by"] not in mod_ids:
                row["replaced_by"] = None
                warnings.append(
                    f"失效归档 {r['id']} 指向的新 mod 不存在，已把关联置空")
            failed_rows.append(row)
        report.failed = len(failed_rows)

        # ---- 特殊提醒：照搬 ----
        alert_rows = [r for r in raw["special_mod_alerts"]
                      if r["mod_id"] in mod_ids]
        for r in raw["special_mod_alerts"]:
            if r["mod_id"] not in mod_ids:
                warnings.append(f"特殊提醒 {r['id']} 所属 mod 不存在，已跳过")
        report.alerts = len(alert_rows)
        for r in alert_rows:
            r["was_downloaded"] = bool(r["was_downloaded"])

        # ---- 依赖边：按 mod 分组（契约的写入口是按 mod 整批替换），
        #      fetched_at 取该 mod 各边的最大值（旧库同 mod 各边本就
        #      同批写入；取最大是保守做法）----
        dep_groups: dict[int, tuple[list[int], int]] = {}
        for r in raw["mod_dependencies"]:
            if r["mod_id"] not in mod_ids:
                continue  # 主人的 mod 都没搬成，边没有意义
            ids, ts = dep_groups.get(r["mod_id"], ([], 0))
            ids.append(r["required_mod_id"])
            dep_groups[r["mod_id"]] = (ids, max(ts, r["fetched_at"] or 0))
        report.dependency_edges = sum(len(v[0]) for v in dep_groups.values())

        # ---- 黑名单：照搬（登记时间不保真，见文件头取舍说明） ----
        purged_rows = raw["purged_mods"]
        report.purged = len(purged_rows)

        # ---- 组装标准载荷交给 import_all（写库路径零自研） ----
        payload = {
            "user_version": _NEW_WORLD_USER_VERSION,
            "tables": {
                "games": games_rows,
                "mods": mods_rows,
                "mod_snapshots": snap_rows,
                "verdict_log": [],  # 旧世界没有判决史，不从旧账编造
                "backups": backup_rows,
                "operations_log": op_rows,
                "failed_mods": failed_rows,
                "special_mod_alerts": alert_rows,
            },
        }
    finally:
        conn.close()

    # ---- 落库：一个事务包住全部写入口 ----
    with new_repo.transaction():
        new_repo.import_all(payload)  # 自带外键安全序 + 约束兜底
        for mod_id in sorted(dep_groups):
            ids, ts = dep_groups[mod_id]
            # required_mod_id 不在账本是常态（那正是"缺依赖"要报的），
            # 原样保留，不做过滤
            new_repo.replace_dependencies(mod_id, ids, fetched_at=ts)
        for row in purged_rows:
            new_repo.add_purged(row["mod_id"], row["game_id"],
                                title=row.get("title"),
                                note=row.get("note"))
    return report
