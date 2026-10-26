from powers import next_power_of_two


def test_zero():
    assert next_power_of_two(0) == 1


def test_between_powers():
    assert next_power_of_two(5) == 8


def test_exact_power():
    assert next_power_of_two(4) == 4
