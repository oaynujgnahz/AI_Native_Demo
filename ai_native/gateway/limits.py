from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Callable, Deque, Dict, Iterator


class RequestLimitExceeded(Exception):
    pass


class ConcurrentLimitExceeded(Exception):
    pass


class RequestLimiter:
    def __init__(
        self,
        per_minute: int = 20,
        concurrent: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.concurrent = concurrent
        self.clock = clock
        self._requests: Dict[str, Deque[float]] = defaultdict(deque)
        self._active: Dict[str, int] = defaultdict(int)
        self._lock = threading.RLock()

    @contextmanager
    def limit(self, user_id: str) -> Iterator[None]:
        now = self.clock()
        with self._lock:
            requests = self._requests[user_id]
            while requests and requests[0] <= now - 60:
                requests.popleft()
            if len(requests) >= self.per_minute:
                raise RequestLimitExceeded(user_id)
            if self._active[user_id] >= self.concurrent:
                raise ConcurrentLimitExceeded(user_id)
            requests.append(now)
            self._active[user_id] += 1
        try:
            yield
        finally:
            with self._lock:
                remaining = max(0, self._active[user_id] - 1)
                if remaining == 0:
                    self._active.pop(user_id, None)
                else:
                    self._active[user_id] = remaining
                requests = self._requests.get(user_id)
                if requests is not None:
                    cutoff = self.clock() - 60
                    while requests and requests[0] <= cutoff:
                        requests.popleft()
                    if not requests:
                        self._requests.pop(user_id, None)
