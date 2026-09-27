"""高级筛选 core 测试
"""
r"""tests/test_advanced_filter.py · list_mods 高级筛选参数（T12，决策 63）。

只测 core 层：条件下推是否正确、口径是否与界面显示一致、
边界（NULL / 通配符 / LIMIT 顺序）是否按 docstring 承诺工作。
GUI 对话框不在这里测——它不碰 repo，条件摊平的接线归页面轮。
"""
import pytest

from core.models import Mod
from core.sqliteRepository import SQLiteRepository

GAME = 1158310


def make_mod(mod_id: int, **over) -> Mod:
    """按 _mod() 转换器枚举的全字段构造 Mod——全关键字传参，
    不依赖 models.py 里哪些字段有默认值。"""
    fields = dict(
        mod_id=mod_id, game_id=GAME, status="tracked", url=None,
        title=None, creator=None, time_created=None, time_updated=None,
        last_time_updated=None, local_timeupdated=None, manifest=None,
        local_size=None, file_size=None, subscriptions=None, favorited=None,
        views=None, tags=None, last_checked_at=None, preview_url=None,
        is_special=False, note=None, color_tag=None, local_path=None,
        deleted_at=None, deleted_last_state=None, first_tracked_at=1000,
    )
    fields.update(over)
    return Mod(**fields)


@pytest.fixture()
def repo(tmp_path):
    r = SQLiteRepository(tmp_path / "adv.db", snapshot_keep=2)
    r.add_game(GAME, "测试游戏", "D:/dl")
    yield r
    r.close()


def ids(mods):
    return [m.mod_id for m in mods]


def test_title_contains_is_title_only(repo):
    """标题条件只看标题——备注里有同样的字也不算。
    这是它与顶栏 search（两处合查）的分工，必须测死。"""
    repo.add_mod(make_mod(1, title="骑士团物语"))
    repo.add_mod(make_mod(2, title="农场物语", note="讲骑士的"))
    repo.add_mod(make_mod(3, title="完全无关"))
    assert ids(repo.list_mods(GAME, title_contains="骑士")) == [1]


def test_note_contains_is_note_only(repo):
    repo.add_mod(make_mod(1, note="假中文待修"))
    repo.add_mod(make_mod(2, title="假中文待修"))
    assert ids(repo.list_mods(GAME, note_contains="假中文")) == [1]


def test_like_wildcards_are_literal(repo):
    """用户输入里的 % 和 _ 必须当普通字符——不转义的话
    '%' 会命中所有行、'_' 会命中所有非空标题。"""
    repo.add_mod(make_mod(1, title="100%全成就"))
    repo.add_mod(make_mod(2, title="全成就达成"))
    assert ids(repo.list_mods(GAME, title_contains="%")) == [1]
    repo.add_mod(make_mod(3, title="下划线_主题"))
    assert ids(repo.list_mods(GAME, title_contains="_")) == [3]


def test_mod_id_exact(repo):
    repo.add_mod(make_mod(3736343635))
    repo.add_mod(make_mod(2941001))
    assert ids(repo.list_mods(GAME, mod_id=3736343635)) == [3736343635]


def test_size_range_matches_display_semantics(repo):
    """大小口径 = 列表页显示值（local_size or file_size）：
    本地优先；acf 缺失或为 0 退 API；两者都无 = 大小未知，
    不落任何区间（未知 ≠ 0，size_min=0 也不该捞到它）。"""
    repo.add_mod(make_mod(1, local_size=100, file_size=None))
    repo.add_mod(make_mod(2, local_size=None, file_size=500))
    repo.add_mod(make_mod(3, local_size=None, file_size=None))
    repo.add_mod(make_mod(4, local_size=0, file_size=700))  # 0 按 or 语义退 API
    # 有效大小：1→100、2→500、3→未知、4→700（0 触发 or 退到 API）；
    # 4 的有效大小 700 ≥ 200，理应命中
    assert set(ids(repo.list_mods(GAME, size_min=200))) == {2, 4}

    assert set(ids(repo.list_mods(GAME, size_min=200, size_max=800))) == {2, 4}
    assert ids(repo.list_mods(GAME, size_max=300)) == [1]


def test_updated_range_excludes_never_checked(repo):
    """从没查过远端（time_updated=NULL）的条目不命中任何时间范围。"""
    repo.add_mod(make_mod(1, time_updated=1_700_000_000))
    repo.add_mod(make_mod(2, time_updated=1_600_000_000))
    repo.add_mod(make_mod(3, time_updated=None))
    assert ids(repo.list_mods(GAME, updated_from=1_650_000_000)) == [1]
    assert ids(repo.list_mods(GAME, updated_to=1_650_000_000)) == [2]
    # 闭区间：恰好等于下限也算
    assert ids(repo.list_mods(GAME, updated_from=1_600_000_000)) == [1, 2]


def test_tags_all_requires_every_tag(repo):
    """标签精确匹配 + 须全有：相近名字不命中、无标签不命中、
    空条件 = 不筛。"""
    repo.add_mod(make_mod(1, tags=["策略", "中世纪"]))
    repo.add_mod(make_mod(2, tags=["策略"]))
    repo.add_mod(make_mod(3, tags=["战略"]))  # 相近但不同名
    repo.add_mod(make_mod(4, tags=None))
    assert set(ids(repo.list_mods(GAME, tags_all=("策略",)))) == {1, 2}
    assert ids(repo.list_mods(GAME, tags_all=("策略", "中世纪"))) == [1]
    assert ids(repo.list_mods(GAME, tags_all=("战略",))) == [3]
    assert len(repo.list_mods(GAME, tags_all=())) == 4  # 空 = 不限


def test_advanced_combines_with_basic_filters(repo):
    """高级条件与既有筛选 AND 叠加，互不替代。"""
    repo.add_mod(make_mod(1, title="骑士", status="downloaded"))
    repo.add_mod(make_mod(2, title="骑士", status="tracked"))
    repo.add_mod(make_mod(3, title="农场", status="downloaded"))
    got = repo.list_mods(GAME, status="downloaded", title_contains="骑士")
    assert ids(got) == [1]


def test_limit_applies_after_tag_filter(repo):
    """LIMIT 与标签后筛的顺序：必须筛完再切。
    若先 LIMIT 再筛，会得到"比 limit 还少"的错误结果。"""
    for i in range(1, 6):
        repo.add_mod(make_mod(i, title=f"骑士{i}", tags=["策略"]))
    repo.add_mod(make_mod(9, title="无关"))
    got = repo.list_mods(GAME, title_contains="骑士",
                         tags_all=("策略",), limit=2)
    assert len(got) == 2
    assert all(m.tags == ["策略"] for m in got)
    # 无标签筛时 LIMIT 仍在 SQL 层生效
    assert len(repo.list_mods(GAME, limit=2)) == 2
