import pytest
from stats import median


def test_odd_count():
    assert median([5, 1, 3]) == 3


def test_even_count():
    assert median([1, 2, 3, 4]) == 2.5


def test_empty_raises():
    with pytest.raises(ValueError):
        median([])
