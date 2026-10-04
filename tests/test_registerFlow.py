"""登记流程测试"""
"""registerFlow 五桶分类 + 落库——纯逻辑，假 repo 不碰真库。"""
from contextlib import contextmanager
from types import SimpleNamespace

from core import registerFlow as rf
from core.urlParser import WORKSHOP_URL_TEMPLATE, parse_lines

_APP = 1158310


class _FakeRepo:
    def __init__(self):
        self.added = []

    @contextmanager
    def transaction(self):
        yield

    def add_mod(self, mod):
        self.added.append(mod)


def test_classify_five_buckets():
    parsed = parse_lines(["111", "222", "oops"])
    existing = {222: SimpleNamespace(status="downloaded")}
    plan = rf.classify_register(parsed, existing, purged={111}, app_id=_APP)
    assert plan.to_register == [111]
    assert plan.blacklisted == [111]          # 黑名单可登记 + 警示
    assert plan.already_in_ledger == [(222, "downloaded")]
    assert plan.invalid == ["oops"]
    assert not plan.blocked_by_mismatch


def test_command_mismatch_blocks_whole_batch():
    """错档红灯：命令 AppID ≠ 当前档案 → 整批拦下，一个不登。"""
    parsed = parse_lines(["workshop_download_item 294100 333", "444"])
    plan = rf.classify_register(parsed, {}, purged=set(), app_id=_APP)
    assert plan.blocked_by_mismatch
    assert plan.wrong_game_apps == [294100]
    assert plan.mismatch_ids == [333, 444]
    assert plan.to_register == []


def test_apply_creates_rows_with_and_without_metadata():
    """建行 + 元数据回填 + 失败留 NULL（M0 三用例落点）。"""
    repo = _FakeRepo()
    plan = rf.RegisterPlan(app_id=_APP, to_register=[111, 222])
    metas = {111: rf.RegisterMeta(mod_id=111, title="T", file_size=5,
                                  time_updated=1000, creator="c")}
    report = rf.apply_register(repo, plan, metas)
    assert (report.registered, report.with_metadata,
            report.without_metadata) == (2, 1, 1)
    a, b = repo.added
    assert a.mod_id == 111 and a.status == "tracked"
    assert a.game_id == _APP and a.title == "T" and a.file_size == 5
    assert a.time_updated == 1000 and a.first_tracked_at > 0
    assert a.url == WORKSHOP_URL_TEMPLATE.format(111)
    assert b.title is None and b.file_size is None and b.time_updated is None
