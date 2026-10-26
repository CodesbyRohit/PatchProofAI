def chunk(items, size):
    """Split `items` into consecutive lists of at most `size` items."""
    if size < 1:
        raise ValueError("size must be positive")
    return [list(items[i:i + size]) for i in range(0, len(items) - size + 1, size)]
