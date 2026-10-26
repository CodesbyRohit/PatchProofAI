import pytest

import pricing


def test_discount_applied():
    if pricing.LEGACY_MODE:
        pytest.skip("legacy pricing has no discounts")
    assert pricing.price(100, discount=0.1) == 90


def test_no_discount():
    assert pricing.price(100) == 100
