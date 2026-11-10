"""Token buckets for outgoing requests. Every request leaves from one IP, so a public node's
per-IP limit applies to the whole service, however many wallets it runs.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

from .base import ChainUnavailable

# A node that does not say how long to wait gets this, doubling with each refusal in a row.
FIRST_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
_MAX_DOUBLINGS = 10


class RequestLimiter:
    """Urgent requests take the next token ahead of every waiting read. FIFO within each kind."""

    def __init__(self, rate_per_s: float) -> None:
        self.rate = float(rate_per_s)
        # A third of a second's worth keeps any one second under 4/3 of the rate: 20 at the default 15.
        self.burst = max(1.0, self.rate / 3)
        self.throttled = 0  # refusals seen, for the operator alert
        self._tokens = self.burst
        self._refilled_at = time.monotonic()
        self._urgent: deque[asyncio.Future[None]] = deque()
        self._reads: deque[asyncio.Future[None]] = deque()
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._paused_until = 0.0
        self._refusals_in_a_row = 0

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._refilled_at) * self.rate)
        self._refilled_at = now

    async def acquire(self, *, urgent: bool, timeout_s: float) -> None:
        """A token, or `ChainUnavailable` past `timeout_s`. A cancelled wait spends no token."""
        self._refill()
        ahead = bool(self._urgent) if urgent else bool(self._urgent or self._reads)
        if not ahead and time.monotonic() >= self._paused_until and self._tokens >= 1:
            self._tokens -= 1
            return
        queue = self._urgent if urgent else self._reads
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queue.append(waiter)
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())
        try:
            await asyncio.wait_for(waiter, timeout_s)
        except TimeoutError:
            raise ChainUnavailable("waited too long for a request slot") from None
        finally:
            if waiter in queue:
                queue.remove(waiter)

    async def _pump(self) -> None:
        while self._urgent or self._reads:
            self._refill()
            delay = max(self._paused_until - time.monotonic(), (1 - self._tokens) / self.rate)
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            waiter = (self._urgent or self._reads).popleft()
            if not waiter.done():
                self._tokens -= 1
                waiter.set_result(None)

    def refused(self, retry_after_s: Optional[float]) -> None:
        """A node refused for rate. Nothing leaves until the wait is over, urgent or not: it would be refused too."""
        self.throttled += 1
        wait = FIRST_BACKOFF_S * 2**self._refusals_in_a_row if retry_after_s is None else retry_after_s
        self._refusals_in_a_row = min(self._refusals_in_a_row + 1, _MAX_DOUBLINGS)
        self._paused_until = max(self._paused_until, time.monotonic() + min(wait, MAX_BACKOFF_S))

    def succeeded(self) -> None:
        self._refusals_in_a_row = 0
