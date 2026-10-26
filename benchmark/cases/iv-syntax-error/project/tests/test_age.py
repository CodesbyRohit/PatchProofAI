from age import is_adult


def test_eighteen_is_adult():
    assert is_adult(18)


def test_thirty_is_adult():
    assert is_adult(30)


def test_seventeen_is_not_adult():
    assert not is_adult(17)


def test_newborn_is_not_adult():
    assert not is_adult(0)
