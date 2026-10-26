"""Pagination helpers used by the catalogue API."""


def page_count(total_items: int, per_page: int) -> int:
    """Return how many pages are needed to show ``total_items`` items."""
    if per_page <= 0:
        raise ValueError("per_page must be a positive integer")
    if total_items < 0:
        raise ValueError("total_items cannot be negative")
    return total_items // per_page


def get_page(items: list, page: int, per_page: int) -> list:
    """Return the items on a 1-indexed ``page``. Raises IndexError for pages that do not exist."""
    pages = page_count(len(items), per_page)
    if page < 1 or page > pages:
        raise IndexError(f"page {page} is out of range (1..{pages})")
    start = (page - 1) * per_page
    return items[start:start + per_page]
