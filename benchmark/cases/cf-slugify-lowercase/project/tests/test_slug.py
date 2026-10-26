from slug import slugify


def test_lowercases_words():
    assert slugify("Hello World") == "hello-world"


def test_keeps_digits():
    assert slugify("Release 2 notes") == "release-2-notes"


def test_already_a_slug():
    assert slugify("abc") == "abc"


def test_version_token():
    assert slugify("v2") == "v2"


def test_blank_title():
    assert slugify("   ") == ""
