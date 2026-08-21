from __future__ import annotations

import threading
from collections import deque
from time import monotonic


_BUCKETS_LOCK = threading.Lock()
_BUCKETS: dict[str, deque[float]] = {}


def check_rate_limit(bucket_key: str, *, limit_per_window: int, window_seconds: float = 60.0) -> float | None:
    normalized_limit = max(1, int(limit_per_window))
    normalized_window = max(1.0, float(window_seconds))
    now = monotonic()
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.setdefault(bucket_key, deque())
        while bucket and now - bucket[0] >= normalized_window:
            bucket.popleft()
        if len(bucket) >= normalized_limit:
            retry_after = max(1.0, normalized_window - (now - bucket[0]))
            return retry_after
        bucket.append(now)
        if len(bucket) == 1:
            stale_keys = [key for key, timestamps in _BUCKETS.items() if not timestamps]
            for key in stale_keys:
                _BUCKETS.pop(key, None)
    return None
