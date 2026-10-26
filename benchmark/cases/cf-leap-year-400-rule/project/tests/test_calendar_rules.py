from calendar_rules import is_leap


def test_ordinary_leap_year():
    assert is_leap(2024)


def test_common_year():
    assert not is_leap(2023)


def test_century_is_not_leap():
    assert not is_leap(1900)


def test_400_year_rule():
    assert is_leap(2000)
