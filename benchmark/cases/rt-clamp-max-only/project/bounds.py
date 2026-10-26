def clamp(value, low, high):
    """Limit value to the closed range [low, high]."""
    if value < low:
        return high
    if value > high:
        return high
    return value
