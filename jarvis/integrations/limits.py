"""Local rate limits for personal integration API calls."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, *, max_calls: int = 10, window_s: float = 60.0, clock=None) -> None:
        if max_calls <= 0 or window_s <= 0:
            raise ValueError("invalid rate limit")
        self._max = max_calls
        self._window = window_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._hits: list[float] = []

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            self._hits = [stamp for stamp in self._hits if now - stamp < self._window]
            if len(self._hits) >= self._max:
                return False
            self._hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
