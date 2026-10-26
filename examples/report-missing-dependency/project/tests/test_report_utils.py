import pytest

from report_utils import average, percent_change


def test_average_of_values():
    assert average([2, 4, 6]) == 4


def test_average_of_empty_sequence_is_zero():
    assert average([]) == 0.0


def test_percent_change():
    assert percent_change(200, 250) == 25.0


def test_percent_change_from_zero_is_rejected():
    with pytest.raises(ValueError):
        percent_change(0, 10)
