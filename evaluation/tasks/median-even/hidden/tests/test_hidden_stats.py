from stats import median


def test_two_values():
    assert median([10, 20]) == 15


def test_unsorted_even_with_duplicates():
    assert median([9, 1, 1, 9, 5, 5]) == 5


def test_single_value():
    assert median([7]) == 7


def test_does_not_mutate_input():
    data = [3, 1, 2]
    median(data)
    assert data == [3, 1, 2]
