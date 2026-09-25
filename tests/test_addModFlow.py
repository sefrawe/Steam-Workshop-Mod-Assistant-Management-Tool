"""tests/test_addModFlow.py —— 「加入新 mod」引擎的单元测试。

只测编排逻辑，用假仓库替代真实数据库：
- 真仓库的写库/事务/回滚语义归它自己的测试管（252 条基线里）；
- 本文件关心的是：引擎把解析、对表、入库、拼命令、盘状态这几步
  按约定衔接——字段填对、分堆分对、边界不炸。
"""
import sqlite3
from contextlib import contextmanager

import pytest

from core.models import Mod
from workflows import addModFlow

APP_ID = 294100  # RimWorld，项目文档里的常用示例


class FakeRepo:
    """只实现引擎用到的三个口（与真实仓库契约对齐）：
    filter_existing_ids / add_mod（撞主键抛 IntegrityError）/ list_mods。
    transaction 提供上下文管理器形态；假仓库不真回滚——
    "整批事务"的语义归真仓库自己的测试管。
    """

    def __init__(self) -> None:
        self.mods: dict[int, Mod] = {}

    def filter_existing_ids(self, ids):
        return [i for i in ids if i in self.mods]

    def add_mod(self, mod: Mod) -> None:
        if mod.mod_id in self.mods:
            raise sqlite3.IntegrityError(
                f"UNIQUE constraint failed: mods.mod_id（假仓库模拟）{mod.mod_id}")
        self.mods[mod.mod_id] = mod

    def list_mods(self, app_id, **kwargs):
        return [m for m in self.mods.values() if m.game_id == app_id]

    @contextmanager
    def transaction(self):
        yield


# ---------- 第②步 preview_input ----------

def test_preview_splits_new_dup_invalid():
    repo = FakeRepo()
    repo.add_mod(Mod(mod_id=111, game_id=APP_ID))
    preview = addModFlow.preview_input(
        ["https://steamcommunity.com/sharedfiles/filedetails/?id=222",
         "111",
         "not_a_url"], repo)
    assert preview.mod_ids == [222, 111]       # 识别到的，按出现顺序
    assert preview.new_ids == [222]            # 账本里没有的
    assert preview.dup_ids == [111]            # 账本里已有的
    assert preview.invalid == ["not_a_url"]    # 认不出的原样带回
    assert preview.has_new is True


def test_preview_dedup_within_batch():
    # 同一个编号在文本里出现多次：只识别一次（去重保序）
    repo = FakeRepo()
    preview = addModFlow.preview_input(
        ["333 333",
         "https://steamcommunity.com/workshop/filedetails/?id=333"], repo)
    assert preview.mod_ids == [333]
    assert preview.new_ids == [333]
    assert preview.dup_ids == []


def test_preview_empty_input():
    preview = addModFlow.preview_input(["", "   "], FakeRepo())
    assert preview.mod_ids == []
    assert preview.has_new is False


def test_preview_all_already_in_ledger():
    repo = FakeRepo()
    repo.add_mod(Mod(mod_id=444, game_id=APP_ID))
    preview = addModFlow.preview_input(["444"], repo)
    assert preview.new_ids == []
    assert preview.dup_ids == [444]
    assert preview.has_new is False


# ---------- 第③步 register_mods ----------

def test_register_adds_tracked_with_url_and_timestamp():
    repo = FakeRepo()
    n = addModFlow.register_mods(repo, APP_ID, [555, 666])
    assert n == 2
    m = repo.mods[555]
    assert m.status == "tracked"
    assert m.game_id == APP_ID                # games 表以 app_id 为主键
    assert m.url == ("https://steamcommunity.com/sharedfiles/"
                     "filedetails/?id=555")
    assert m.first_tracked_at is not None     # 收录时刻已记录
    assert m.title is None                    # 标题留给更新检测补全


def test_register_empty_list_is_noop():
    repo = FakeRepo()
    assert addModFlow.register_mods(repo, APP_ID, []) == 0
    assert repo.mods == {}


def test_register_dedup_keeps_order():
    repo = FakeRepo()
    n = addModFlow.register_mods(repo, APP_ID, [777, 777, 888])
    assert n == 2
    assert 777 in repo.mods and 888 in repo.mods


def test_register_conflict_raises_integrity_error():
    # 预览过期场景：预览时账本没有、入库前别人（另一页）导过了 →
    # 撞主键整批失败，由界面接住提示"请重新预览"
    repo = FakeRepo()
    repo.add_mod(Mod(mod_id=999, game_id=APP_ID))
    with pytest.raises(sqlite3.IntegrityError):
        addModFlow.register_mods(repo, APP_ID, [999, 1000])


# ---------- 第③步 build_commands_text ----------

def test_commands_text_format():
    text = addModFlow.build_commands_text(APP_ID, [111, 222])
    assert text == ("workshop_download_item 294100 111\n"
                    "workshop_download_item 294100 222\n")


def test_commands_text_empty_returns_empty_string():
    # 空清单 → 空串。命令生成器的 build_copy_text 没拦（会返回
    # 一个孤零零的换行符），引擎这层拦住——界面"复制"按钮据此置灰
    assert addModFlow.build_commands_text(APP_ID, []) == ""


# ---------- 第④步 bucket_statuses ----------

def test_buckets_after_scan():
    repo = FakeRepo()
    repo.add_mod(Mod(mod_id=1, game_id=APP_ID, status="downloaded"))
    repo.add_mod(Mod(mod_id=2, game_id=APP_ID, status="tracked"))
    repo.add_mod(Mod(mod_id=3, game_id=APP_ID, status="failed"))
    b = addModFlow.bucket_statuses(repo, APP_ID, [1, 2, 3, 4])
    assert b.downloaded == [1]
    assert b.waiting == [2]
    assert b.failed == [3]
    assert b.unexpected == [4]                # 账本里没有 → 防呆堆


def test_buckets_ignores_other_game():
    # 编号在账本里但属于别的游戏档案 → 本档案视角就是"没有"
    repo = FakeRepo()
    repo.add_mod(Mod(mod_id=5, game_id=1158310, status="downloaded"))
    b = addModFlow.bucket_statuses(repo, APP_ID, [5])
    assert b.unexpected == [5]


def test_buckets_empty_input():
    b = addModFlow.bucket_statuses(FakeRepo(), APP_ID, [])
    assert b.downloaded == [] and b.waiting == []

# ---------- 第②步补充：命令行自带的游戏 AppID 比对（防错档） ----------

def test_preview_flags_command_from_other_game():
    # 命令是 RimWorld(294100) 的，档案却是 CK3(1158310) → 必须亮红灯
    repo = FakeRepo()
    preview = addModFlow.preview_input(
        ["workshop_download_item 294100 123"], repo, app_id=1158310)
    assert preview.mod_ids == [123]
    assert preview.command_app_ids == [294100]
    assert preview.app_id_mismatch is True



def test_preview_no_mismatch_when_app_matches():
    repo = FakeRepo()
    preview = addModFlow.preview_input(
        ["workshop_download_item 294100 123"], repo, app_id=294100)
    assert preview.app_id_mismatch is False


def test_preview_no_command_no_mismatch():
    # 没有命令行（纯网址/纯数字）→ 无从比对，也不该报警
    repo = FakeRepo()
    preview = addModFlow.preview_input(
        ["https://steamcommunity.com/sharedfiles/filedetails/?id=777"],
        repo, app_id=APP_ID)
    assert preview.command_app_ids == []
    assert preview.app_id_mismatch is False
