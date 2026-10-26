import pytest

from series import sum_to


def test_zero_is_zero():
    assert sum_to(0) == 0


def test_one():
    assert sum_to(1) == 1


def test_five():
    assert sum_to(5) == 15


def test_negative_is_rejected():
    with pytest.raises(ValueError):
        sum_to(-1)
