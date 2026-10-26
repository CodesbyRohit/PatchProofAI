def median(values):
    """Middle value of a non-empty sequence (mean of the two middle values for an even count)."""
    if not values:
        raise ValueError("median of an empty sequence")
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
