from words import word_count


def test_empty_text():
    assert word_count("") == {}


def test_repeated_words():
    assert word_count("a b a") == {"a": 2, "b": 1}


def test_single_word():
    assert word_count("x") == {"x": 1}
