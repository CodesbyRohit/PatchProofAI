def word_count(text):
    """Count how often each whitespace-separated word occurs."""
    counts = {}
    for word in text.split():
        counts[word] += 1
    return counts
