from text import truncate


def test_short_text_unchanged():
    assert truncate("hello", 10) == "hello"


def test_exact_fit_unchanged():
    assert truncate("abc", 3) == "abc"


def test_cut_with_ellipsis():
    assert truncate("hello world", 8) == "hello..."


def test_never_longer_than_limit():
    assert len(truncate("abcdefghij", 6)) <= 6
