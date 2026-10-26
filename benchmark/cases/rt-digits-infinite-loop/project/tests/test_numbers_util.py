from numbers_util import digits


def test_three_digits():
    assert digits(123) == [1, 2, 3]


def test_single_digit():
    assert digits(7) == [7]


def test_zero():
    assert digits(0) == [0]
