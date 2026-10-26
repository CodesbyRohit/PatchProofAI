import pytest

from cart import Cart


def test_add_items():
    cart = Cart()
    cart.add("apple", 2)
    cart.add("pear")
    assert cart.total_quantity() == 3


def test_new_carts_are_independent():
    first = Cart()
    first.add("apple")
    second = Cart()
    assert second.total_quantity() == 0


def test_initial_items_are_kept():
    cart = Cart([("milk", 1)])
    cart.add("bread")
    assert cart.skus() == ["bread", "milk"]


def test_caller_list_is_not_mutated():
    seed = [("milk", 1)]
    cart = Cart(seed)
    cart.add("bread")
    assert seed == [("milk", 1)]


def test_rejects_zero_quantity():
    with pytest.raises(ValueError):
        Cart().add("apple", 0)
