from workflows import exceptionFlow as ef


def test_hit_casefold():
    r = ef.classify_local_titles({1: "Abandoned Mines"}, "ABANDONED")
    assert r.hits == {1: ["abandoned"]}


def test_empty_words_disable():
    assert ef.classify_local_titles({1: "Abandoned Mines"}, "  ").total == 0


def test_multi_hits_sorted():
    r = ef.classify_local_titles(
        {2: "Old deprecated abandoned thing"}, "abandoned,deprecated")
    assert r.hits[2] == ["abandoned", "deprecated"]
