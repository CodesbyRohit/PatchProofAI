from dedupe import dedupe


def test_empty():
    assert dedupe([]) == []


def test_unhashable_items_are_supported():
    assert dedupe([[1], [2], [1]]) == [[1], [2]]


def test_strings_keep_order():
    assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
