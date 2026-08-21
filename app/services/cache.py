from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


class TTLMemoryCache:
    def __init__(self, *, default_ttl_seconds: int, max_entries: int = 256):
        self._default_ttl_seconds = max(1, int(default_ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def _normalize_key(self, key: Any) -> str:
        if isinstance(key, str):
            return key
        return json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)

    def _purge_expired(self, now: float) -> None:
        expired_keys = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired_keys:
            self._entries.pop(key, None)

    def get(self, key: Any) -> Any | None:
        normalized_key = self._normalize_key(key)
        now = monotonic()
        with self._lock:
            self._purge_expired(now)
            entry = self._entries.get(normalized_key)
            if entry is None:
                return None
            self._entries.move_to_end(normalized_key)
            return copy.deepcopy(entry.value)

    def set(self, key: Any, value: Any, *, ttl_seconds: int | None = None) -> None:
        normalized_key = self._normalize_key(key)
        ttl = self._default_ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        expires_at = monotonic() + ttl
        with self._lock:
            self._entries[normalized_key] = _CacheEntry(
                expires_at=expires_at,
                value=copy.deepcopy(value),
            )
            self._entries.move_to_end(normalized_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_prefix(self, prefix: str) -> None:
        with self._lock:
            keys = [key for key in self._entries if key.startswith(prefix)]
            for key in keys:
                self._entries.pop(key, None)
