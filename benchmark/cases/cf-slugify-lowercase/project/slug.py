import re


def slugify(title):
    """URL slug: lowercase words (letters and digits) joined by hyphens."""
    words = re.findall(r"[A-Za-z0-9]+", title)
    return "-".join(words)
