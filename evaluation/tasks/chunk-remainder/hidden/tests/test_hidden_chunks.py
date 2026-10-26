from chunks import chunk


def test_shorter_than_size():
    assert chunk([1, 2], 5) == [[1, 2]]


def test_empty_input():
    assert chunk([], 3) == []


def test_size_one_and_string_input():
    assert chunk("abc", 1) == [["a"], ["b"], ["c"]]
