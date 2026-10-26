from money import cents


def test_whole():
    assert cents(2) == 200


def test_fraction():
    assert cents(1.25) == 125
