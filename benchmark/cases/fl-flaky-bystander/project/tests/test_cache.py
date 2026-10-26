import time

# Replaced by the benchmark harness with an epoch timestamp shortly after the run starts.
DEADLINE = __PATCHPROOF_FLAKE_DEADLINE__


def test_cache_warms_up_in_time():
    # A time-dependent test, a common real cause of flakiness: it fails before DEADLINE and passes
    # after it. The harness places DEADLINE inside the first baseline run, so the two identical
    # baseline runs disagree.
    remaining = DEADLINE - time.time()
    if remaining > 0:
        time.sleep(remaining + 0.25)
        raise AssertionError("cache was not warm before the deadline")
