from search import find


def test_middle():
    assert find([1, 3, 5], 3) == 1


def test_last():
    assert find([1, 3, 5], 5) == 2


def test_single_element():
    assert find([7], 7) == 0


def test_empty():
    assert find([], 1) == -1


def test_absent():
    assert find([1, 3, 5], 4) == -1
