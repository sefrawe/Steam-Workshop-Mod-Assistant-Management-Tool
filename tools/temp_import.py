"""
一次性预演导入（任务 9 预演，正式迁移完成后即弃）
"""
"""
数据源：
  1) ck3_mod_update_state.json —— API 侧字段（title / time_updated / tags / ...）
  2) appworkshop_1158310.acf   —— 本地三件套（timeupdated / manifest / size），
     命中的 mod 顺手 update_local_state(..., status='downloaded')
     —— 正好预演 tracked→downloaded 状态机。

可重复运行：已存在的 mod_id 自动跳过（filter_existing_ids）。
运行：python tools/temp_import.py
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # 允许从 tools/ 直接运行

import vdf  # noqa: E402

from core.models import Mod  # noqa: E402
from core.sqliteRepository import SQLiteRepository  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "mods.db"

# ↓↓↓ 按本机实际路径修改 ↓↓↓
STATE_JSON = PROJECT_ROOT / "tests" / "fixtures" / "ck3_mod_update_state.json"
ACF_PATH   = PROJECT_ROOT / "tests" / "fixtures" / "appworkshop_1158310.acf"
CK3_APP_ID = 1158310
CK3_NAME = "Crusader Kings III"
CK3_DOWNLOAD_DIR = r"E:\games\paradox\mod\ck3\1158310"


def load_acf_local(path: Path) -> dict[int, dict]:
    """acf → {mod_id: {timeupdated, manifest, size}}；文件不存在返回空 dict。"""
    if not path.exists():
        print(f"[警告] 未找到 acf，跳过本地回填：{path}")
        return {}
    # utf-8-sig：兼容带 BOM 的 Valve 文件，首键名不会被 \ufeff 污染
    data = vdf.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    items = data.get("AppWorkshop", {}).get("WorkshopItemsInstalled", {})
    out: dict[int, dict] = {}
    for mid, info in items.items():
        try:
            out[int(mid)] = {
                "timeupdated": int(info.get("timeupdated", 0)),
                "manifest": info.get("manifest"),
                "size": int(info.get("size", 0)),
            }
        except (TypeError, ValueError):
            continue
    return out


def main() -> None:
    repo = SQLiteRepository(DB_PATH)  # 空库时自动执行 schema.sql，幂等

    if repo.get_game(CK3_APP_ID) is None:
        repo.add_game(CK3_APP_ID, CK3_NAME, CK3_DOWNLOAD_DIR)
        print(f"已创建游戏档案：{CK3_NAME}（{CK3_APP_ID}）→ {CK3_DOWNLOAD_DIR}")

    state = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    mods = state.get("mods", {})
    print(f"state.json 共 {len(mods)} 个 mod")

    local = load_acf_local(ACF_PATH)
    if local:
        print(f"acf 共 {len(local)} 条已安装记录")

    existing = repo.filter_existing_ids(int(k) for k in mods)
    fresh, downloaded = 0, 0
    with repo.transaction():  # 整个导入一个事务：中途炸 = 全部回滚
        for key, m in mods.items():
            mid = int(key)
            if mid in existing:
                continue
            repo.add_mod(Mod(
                mod_id=mid,
                game_id=CK3_APP_ID,
                url=m.get("url"),
                title=m.get("title"),
                creator=m.get("creator"),
                time_created=m.get("time_created"),
                time_updated=m.get("time_updated"),
                last_time_updated=m.get("last_time_updated"),
                file_size=m.get("file_size"),
                subscriptions=m.get("subscriptions"),
                favorited=m.get("favorited"),
                views=m.get("views"),
                tags=m.get("tags") or None,
                last_checked_at=m.get("last_checked_at"),
            ))
            fresh += 1
            loc = local.get(mid)
            if loc and loc["timeupdated"] > 0:
                repo.update_local_state(
                    mid,
                    local_timeupdated=loc["timeupdated"],
                    manifest=loc["manifest"],
                    local_size=loc["size"],
                    status="downloaded",  # 状态机：acf 出现 → downloaded
                )
                downloaded += 1

    repo.close()
    print(f"导入完成：新增 {fresh} 个，其中 acf 命中回填 {downloaded} 个（→downloaded）")
    print(f"数据库：{DB_PATH}")


if __name__ == "__main__":
    main()
