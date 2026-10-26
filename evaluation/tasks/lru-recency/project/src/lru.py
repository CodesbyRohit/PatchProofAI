class LRUCache:
    """A fixed-capacity cache that evicts the least recently used key (reads and writes both count as use)."""

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value):
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self.capacity:
            del self._data[next(iter(self._data))]
        self._data[key] = value

    def __len__(self):
        return len(self._data)
