LEGACY_MODE = False


def price(amount, discount=0.0):
    """Price after a fractional discount, e.g. 0.1 for 10 %."""
    return amount - discount
