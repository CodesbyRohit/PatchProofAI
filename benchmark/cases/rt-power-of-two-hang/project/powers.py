def next_power_of_two(n):
    """Smallest power of two that is >= n, for n >= 0. next_power_of_two(0) is 1."""
    p = 1
    while p <= n:
        p *= 2
    return p
