"""账本导出 / 分享包"""
"""两种导出产品、两种导入语义，格式与规则的唯一权威。

ledger（完整账本）——换机迁移 / 整机备份
  7 张表全量、原值保留（含 id 与跨表引用，如 operations_log.backup_id、
  failed_mods.replaced_by）。backups.backup_path 是导出机器上的
  绝对路径：换机后没搬文件就"失联"，用既有的【重定位】通道处理
  （决策 30 本来就为此留了口子），导入时如实灌入、不装聪明。
  导入语义 = 清库重灌，不做合并（拍板记录）：两本账合并没有可判定
  的规则——同一 mod_id 两边 note 不同听谁的？快照时间线怎么交错？
  可预测性 > 看似聪明的折中。GUI 侧确认弹窗必须写明"整体替换"。

sharepack（分享包）——把某游戏的收录清单给别人
  只带清单 + 远端事实 + 整理成果：标题、url、tags、time_*、note、
  color_tag、is_special 原样；状态统一 downloaded→tracked（接收者
  还没下载，据实记账）；机器侧字段全部剥离——local_timeupdated /
  manifest / local_size / local_path / game_mod_dir / backup_dir，
  以及 first_tracked_at / last_checked_at（那是"我的机器史"：接收者
  的收录时间从他导入那一刻起算，本地版本等他第一次【扫描本地】
  自动回填）。不含 deleted / failed / 快照 / 备份 / 操作日志 / 提醒
  ——坟头和本机检测痕迹不外带。
  导入语义 = 增量并入：目标库已有同 AppID 档案 → 复用档案行、一字
  不改；mod_id 已存在 → 跳过；绝不改动目标库现有数据。

两道版本闸：
  1. format_version 比本程序认识的新（FORMAT_VERSION）→ 拒收并
     提示升级——本模块 load() 裁决；
  2. schema_version 比当前库 user_version 新 → 拒收——ledger 路径
     由 repo.import_all 裁决；sharepack 路径由 Game(**row) /
     Mod(**row) 的构造隐式执行：载荷字段与本程序数据类不符会当场
     TypeError 指名道姓，比版本号更直接。

分层与数据路径（架构约定：本模块零 SQL）：
  导出：repo.export_all()（整库倒出，唯一读取原语）→ 本模块切片、
        变换、编解码 → JSON 文件（UTF-8、indent=2、中文原样——
        人能直接打开看，出了问题肉眼可查）。
  导入：JSON 文件 → load()（三道校验关，错误指名道姓）→
        import_ledger 走 repo.import_all（单方法整体事务）；
        import_sharepack 走 add_game / add_mod / filter_existing_ids，
        with repo.transaction() 包住（契约第 4 条）。
  校验分寸：本模块只校验"缺了就没法灌"的最小集合（每表的身份
  字段与类型）；其余形状问题由数据库约束兜底（CHECK / 外键 /
  NOT NULL），炸了也整体回滚——错误显式暴露是本项目的老规矩。

文件头字段（两种产品一致）：format / format_version /
schema_version / exported_at / tables；sharepack 额外带 game_app_id。
"""
import json
import time
from pathlib import Path

from core.models import Game, Mod
from core.modRepository import LEDGER_TABLES, ModRepository

# 两种产品与当前格式版本。载荷结构每变一次 format_version +1；
# 本程序只认 ≤ 自己的版本（旧文件自然能读）。
FORMAT_LEDGER = "steam-mod-assistant/ledger"
FORMAT_SHAREPACK = "steam-mod-assistant/sharepack"
KNOWN_FORMATS = frozenset({FORMAT_LEDGER, FORMAT_SHAREPACK})
FORMAT_VERSION = 1

# 每张表"缺了就没法灌"的最小字段集（字段名 → 期望类型）。
_REQUIRED: dict[str, dict[str, type]] = {
    "games": {"app_id": int, "name": str, "download_dir": str},
    "mods": {"mod_id": int, "game_id": int, "status": str},
    "mod_snapshots": {"mod_id": int},
    "backups": {"mod_id": int, "backup_path": str,
                "size_bytes": int, "version_timeupdated": int},
    "operations_log": {"command": str},
    "failed_mods": {"mod_id": int, "game_id": int},
    "special_mod_alerts": {"mod_id": int, "remote_time_updated": int},
}

# sharepack 要从 mods 行剥离的机器侧字段：前四个是"我的机器的 acf
# 事实"（接收者第一次扫描本地自动回填），后两个是"我的机器史"
# （收录起算点、检测时刻——接收者应有自己的时间线）。
_SHARE_STRIP_MOD = ("local_timeupdated", "manifest", "local_size",
                    "local_path", "first_tracked_at", "last_checked_at")
# sharepack 要从 games 行剥离的机器侧字段（接收者用连接指引自己
# 检测，检测通过后㉒ 的通道会把 game_mod_dir 写进他的档案）
_SHARE_STRIP_GAME = ("game_mod_dir", "backup_dir")

_SHARE_STATUSES = frozenset({"tracked", "downloaded"})

_UNSAFE_NAME_CHARS = '<>:"/\\|?*'  # Windows 文件名非法字符


# ================= 构建（导出侧） =================

def build_ledger(repo: ModRepository) -> dict:
    """完整账本 payload（内存态）。写文件用 save()，建议文件名用
    default_filename()。schema_version 取自 repo.export_all 同一
    来源，头部与载荷永不脱节。"""
    dump = repo.export_all()
    return {
        "format": FORMAT_LEDGER,
        "format_version": FORMAT_VERSION,
        "schema_version": dump["user_version"],
        "exported_at": int(time.time()),
        "tables": dump["tables"],
    }


def build_sharepack(repo: ModRepository, app_id: int) -> dict:
    """某游戏的分享包 payload。数据路径刻意只有一条：整库经
    repo.export_all 倒出（repo 的唯一读取原语），在这里切片与变换
    ——repo 不为第二种产品单开读取口；user_version 顺路同源。
    变换规则（拍板记录见文件头）：只留本档案且 status ∈
    {tracked, downloaded}；downloaded → tracked；机器侧字段剥离。"""
    dump = repo.export_all()
    games = [g for g in dump["tables"]["games"] if g["app_id"] == app_id]
    if not games:
        raise ValueError(f"游戏档案 {app_id} 不存在")
    game_row = dict(games[0])
    for field in _SHARE_STRIP_GAME:
        game_row[field] = None

    mods: list[dict] = []
    for row in dump["tables"]["mods"]:
        if row["game_id"] != app_id or row["status"] not in _SHARE_STATUSES:
            continue  # 别家档案的、已删/已失效的：不外带
        row = dict(row)  # 拷贝再改，不污染 dump
        if row["status"] == "downloaded":
            row["status"] = "tracked"
        for field in _SHARE_STRIP_MOD:
            row[field] = None
        mods.append(row)

    return {
        "format": FORMAT_SHAREPACK,
        "format_version": FORMAT_VERSION,
        "schema_version": dump["user_version"],
        "exported_at": int(time.time()),
        "game_app_id": app_id,
        "tables": {"games": [game_row], "mods": mods},
    }


def default_filename(payload: dict) -> str:
    """保存对话框的现成建议名。仅对 build_* 的产物（或 load 过的
    payload）调用——依赖 header 字段，随手拼的 dict 不保证有。"""
    stamp = time.strftime("%Y%m%d_%H%M%S",
                          time.localtime(payload["exported_at"]))
    if payload["format"] == FORMAT_LEDGER:
        return f"账本备份_{stamp}.json"
    game = payload["tables"]["games"][0]
    safe = "".join("_" if ch in _UNSAFE_NAME_CHARS else ch
                   for ch in game["name"])
    return f"分享包_{safe}_{game['app_id']}.json"


# ================= 落盘 / 读取 =================

def save(payload: dict, path: str | Path) -> Path:
    """payload → JSON 文件（UTF-8、indent=2、中文原样）。返回路径。"""
    p = Path(path)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def load(path: str | Path) -> dict:
    """JSON 文件 → payload，全量校验。三道关：
    ① 能不能解析（不是 JSON → ValueError）；
    ② 头部（不是本工具的格式 / 格式版本太新 → ValueError 提示升级）；
    ③ 逐行（缺身份字段 / 类型不对 → ValueError，指名道姓到
       "mods[17] 缺少必要字段 mod_id" 这个粒度）。
    通过三关的 payload 才允许进 import_*。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是有效的 JSON 文件：{exc}") from exc
    _check_header(payload)
    _check_tables(payload)
    return payload


def _check_header(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("文件内容不是 JSON 对象")
    if payload.get("format") not in KNOWN_FORMATS:
        raise ValueError("这不是本工具导出的账本/分享包文件"
                         f"（format={payload.get('format')!r}）")
    ver = payload.get("format_version")
    if not isinstance(ver, int) or isinstance(ver, bool):
        raise ValueError("format_version 缺失或不是整数")
    if ver > FORMAT_VERSION:
        raise ValueError(
            f"文件由更新版本的程序导出（格式 v{ver}，本工具支持到"
            f" v{FORMAT_VERSION}），请先升级本工具再导入")
    sv = payload.get("schema_version")
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise ValueError("schema_version 缺失或不是整数")
    if not isinstance(payload.get("tables"), dict):
        raise ValueError("tables 缺失或不是对象")


def _check_tables(payload: dict) -> None:
    tables = payload["tables"]
    allowed = frozenset(LEDGER_TABLES) if payload["format"] == FORMAT_LEDGER \
        else frozenset({"games", "mods"})
    missing = allowed - set(tables)
    if missing:
        raise ValueError(f"载荷缺少表：{sorted(missing)}（文件可能被截断）")
    extra = set(tables) - allowed
    if extra:
        raise ValueError(f"载荷里出现了不属于该格式的表：{sorted(extra)}")
    for name, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError(f"{name} 不是列表")
        required = _REQUIRED.get(name, {})
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{name}[{i}] 不是对象")
            for key, typ in required.items():
                if key not in row:
                    raise ValueError(f"{name}[{i}] 缺少必要字段 {key}")
                val = row[key]
                # bool 是 int 的子类，必须先排掉：mod_id=True 这种
                # 病态值不能放行
                if typ is int and (not isinstance(val, int)
                                   or isinstance(val, bool)):
                    raise ValueError(f"{name}[{i}].{key} 应为整数")
                if typ is str and not isinstance(val, str):
                    raise ValueError(f"{name}[{i}].{key} 应为字符串")


# ================= 导入 =================

def ledger_summary(payload: dict) -> str:
    """确认弹窗的一句摘要（清库重灌前必须让用户看清代价）。"""
    t = payload["tables"]
    return (f"该账本包含 {len(t['games'])} 个游戏档案、"
            f"{len(t['mods'])} 条 mod 记录（含已删除/已失效）。"
            "导入将整体替换当前账本。")


def import_ledger(payload: dict, repo: ModRepository) -> dict[str, int]:
    """完整账本导入 = 清库重灌（不做合并，拍板记录见文件头）。
    repo.import_all 自带整体事务：任何失败 → 回滚到旧账，绝无
    中间态。返回 {表名: 灌入行数} 给完成提示。"""
    counts = {name: len(rows) for name, rows in payload["tables"].items()}
    repo.import_all({"user_version": payload["schema_version"],
                     "tables": payload["tables"]})
    return counts


def import_sharepack(payload: dict, repo: ModRepository) -> dict:
    """分享包导入 = 增量并入（拍板记录见文件头）：
    - 同 AppID 档案已存在 → 复用档案行，一字不改；
    - mod_id 已存在 → 跳过（filter_existing_ids 一次查重）；
    - 绝不改动目标库的任何现有数据。
    with repo.transaction() 包住（契约第 4 条）：档案与全部 mod
    要么全进、要么全不进。返回 {"app_id", "added", "skipped"}。
    形状闸：Game(**row) / Mod(**row)——载荷字段与本程序数据类不符
    会当场 TypeError 指名道姓（sharepack 的 schema 闸就靠它）。"""
    games = payload["tables"]["games"]
    if len(games) != 1:
        raise ValueError(f"分享包应恰好包含 1 个游戏档案，"
                         f"实际 {len(games)} 个")
    incoming = Game(**games[0])
    incoming_mods = [Mod(**row) for row in payload["tables"]["mods"]]

    added = skipped = 0
    with repo.transaction():
        if repo.get_game(incoming.app_id) is None:
            repo.add_game(incoming.app_id, incoming.name,
                          incoming.download_dir)
        existing = repo.filter_existing_ids(m.mod_id for m in incoming_mods)
        for mod in incoming_mods:
            if mod.mod_id in existing:
                skipped += 1
                continue
            mod.status = "tracked"  # 一律按"待下载"入账（构建侧已保证，
                                    # 这里防手改的载荷，双保险）
            repo.add_mod(mod)
            added += 1
    return {"app_id": incoming.app_id, "added": added, "skipped": skipped}
