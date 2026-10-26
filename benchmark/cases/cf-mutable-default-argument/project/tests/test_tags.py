from tags import add_tag


def test_fresh_calls_are_independent():
    add_tag("a")
    assert add_tag("b") == ["b"]


def test_existing_list_is_extended():
    assert add_tag("x", ["w"]) == ["w", "x"]


def test_tag_is_present():
    assert "z" in add_tag("z")
