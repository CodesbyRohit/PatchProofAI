from stats import mean


def test_two_values():
    assert mean([2, 4]) == 3


def test_single_value():
    assert mean([5]) == 5


def test_empty_is_zero():
    assert mean([]) == 0.0
