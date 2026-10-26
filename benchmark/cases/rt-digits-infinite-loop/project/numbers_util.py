def digits(n):
    """Decimal digits of a non-negative integer, most significant first."""
    out = []
    while n > 0:
        out.append(n % 10)
        n //= 10
    return out[::-1]
