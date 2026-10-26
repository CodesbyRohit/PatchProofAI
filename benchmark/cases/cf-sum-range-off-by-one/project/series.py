def sum_to(n):
    """Sum of the integers 1..n inclusive. n must be non-negative."""
    if n < 0:
        raise ValueError("n must be non-negative")
    return sum(range(1, n))
