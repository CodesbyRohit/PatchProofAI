from lru import LRUCache


def test_missing_key_does_not_change_order():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("zzz", "dflt") == "dflt"
    cache.put("c", 3)
    assert cache.get("a") is None


def test_put_existing_key_refreshes_and_updates():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    cache.put("c", 3)
    assert cache.get("a") == 10 and cache.get("b") is None
    assert len(cache) == 2


def test_falsy_values_are_kept_and_refreshed():
    cache = LRUCache(2)
    cache.put("a", 0)
    cache.put("b", 2)
    assert cache.get("a") == 0
    cache.put("c", 3)
    assert cache.get("a") == 0 and cache.get("b") is None
