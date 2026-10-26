import pytest

from pagination import get_page, page_count


def test_exact_multiple_has_no_extra_page():
    assert page_count(10, 5) == 2


def test_partial_last_page_is_counted():
    assert page_count(11, 5) == 3


def test_last_partial_page_is_reachable():
    assert get_page(list(range(11)), 3, 5) == [10]


def test_empty_collection_has_no_pages():
    assert page_count(0, 5) == 0


def test_page_past_the_end_raises():
    with pytest.raises(IndexError):
        get_page(list(range(10)), 3, 5)


def test_first_page():
    assert get_page(list(range(11)), 1, 5) == [0, 1, 2, 3, 4]


def test_rejects_non_positive_page_size():
    with pytest.raises(ValueError):
        page_count(10, 0)
