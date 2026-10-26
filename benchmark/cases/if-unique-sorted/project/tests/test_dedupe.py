from dedupe import unique


def test_keeps_first_occurrence_order():
    assert unique([3, 1, 3, 2]) == [3, 1, 2]


def test_empty():
    assert unique([]) == []


def test_all_duplicates():
    assert unique([4, 4, 4]) == [4]
