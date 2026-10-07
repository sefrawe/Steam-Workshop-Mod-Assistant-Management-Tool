"""tests/test_depNumChildren.py · 依赖判定 num_children 仲裁用例。"""
from workflows.exceptionFlow import classify_dependencies


def _entry(mid, result=1, children=None, num_children=None):
    e = {"mod_id": mid, "result": result, "title": f"t{mid}"}
    if children is not None:
        e["children"] = children
    if num_children is not None:
        e["num_children"] = num_children
    return e


def test_zero_meta_no_children_key_means_childless():
    """零依赖条目 Steam 不回 children 键：num_children=0 → 无依赖。"""
    f = classify_dependencies([_entry(1, num_children=0)], {1}, {},
                              first_fetch=True)
    assert f.childless == [1] and f.undetermined == [] and f.fetched == 1


def test_positive_meta_without_children_is_undetermined():
    """说有依赖却没给清单 → 保守未判定，不冒充无依赖。"""
    f = classify_dependencies([_entry(2, num_children=3)], {2}, {},
                              first_fetch=True)
    assert f.undetermined == [2] and f.fetched == 0


def test_missing_both_is_undetermined():
    f = classify_dependencies([_entry(3)], {3}, {}, first_fetch=True)
    assert f.undetermined == [3]


def test_children_list_still_normal():
    """children 键在场照旧判缺依赖。"""
    f = classify_dependencies([_entry(4, children=[999], num_children=1)],
                              {4}, {}, first_fetch=False)
    assert f.missing == {4: [999]}


def test_zero_meta_enters_baseline():
    """无依赖也照常入账空清单——下次拉取有基线可比。"""
    f = classify_dependencies([_entry(5, num_children=0)], {5}, {5: [7]},
                              first_fetch=False)
    assert f.childless == [5] and f.changed == [(5, [7], [])]
