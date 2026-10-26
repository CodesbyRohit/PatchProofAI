"""Small statistics helpers for the weekly report."""


def average(values):
    """Mean of a sequence of numbers. An empty sequence has an average of 0.0."""
    return sum(values) / len(values)


def percent_change(old, new):
    """Percentage change from ``old`` to ``new``, rounded to one decimal place."""
    if old == 0:
        raise ValueError("percent change from zero is undefined")
    return round((new - old) / old * 100, 1)
