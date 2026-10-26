from dedupe import dedupe


def test_removes_duplicates():
    assert sorted(dedupe([1, 1, 2])) == [1, 2]


def test_keeps_first_occurrence_order():
    assert dedupe([3, 1, 3, 2]) == [3, 1, 2]
