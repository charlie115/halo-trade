"""Bearer auth against the sha256 `token_hash`, plus a per-token rate limit.

The token itself is never stored or logged. Revoked users have no usable token at all.
`POST /v1/close` authenticates without counting against the rate limit, because hard rule 4
says it is never blocked by a limit.
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Deque

from fastapi import Request

from .context import AppContext
from .contracts import ErrorCode, ErrorOut, Outcome
from .limits import ApiError
from .service import User

RATE_LIMIT_PER_MIN = 60
RATE_WINDOW_MS = 60_000


class RateLimiter:
    def __init__(self, limit: int = RATE_LIMIT_PER_MIN, window_ms: int = RATE_WINDOW_MS) -> None:
        self.limit = limit
        self.window_ms = window_ms
        self._hits: dict[str, Deque[int]] = {}
        self._next_sweep_ms = 0

    def _evict_idle(self, now_ms: int) -> None:
        """Forget keys with no hit inside the window, at most once per window, so random keys cannot grow the map."""
        if now_ms < self._next_sweep_ms:
            return
        cutoff = now_ms - self.window_ms
        self._hits = {key: hits for key, hits in self._hits.items() if hits and hits[-1] > cutoff}
        self._next_sweep_ms = now_ms + self.window_ms

    def allow(self, token_hash: str, now_ms: int) -> bool:
        self._evict_idle(now_ms)
        hits = self._hits.setdefault(token_hash, deque())
        cutoff = now_ms - self.window_ms
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now_ms)
        return True


def _unauthorized() -> ApiError:
    return ApiError(ErrorOut(outcome=Outcome.REJECTED, code=ErrorCode.UNAUTHORIZED))


def _authenticate(request: Request, *, rate_limited: bool) -> User:
    ctx: AppContext = request.app.state.ctx
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    token_hash = hashlib.sha256(token.strip().encode()).hexdigest()
    row = ctx.db.execute("SELECT * FROM users WHERE token_hash=?", (token_hash,)).fetchone()
    if row is None or row["status"] == "revoked":
        raise _unauthorized()
    if rate_limited and not request.app.state.rate_limiter.allow(token_hash, ctx.clock()):
        raise ApiError(ErrorOut(outcome=Outcome.REJECTED, code=ErrorCode.RATE_LIMITED, limit=f"per_min={RATE_LIMIT_PER_MIN}"))
    user = User.from_row(row)
    request.state.user = user
    return user


async def current_user(request: Request) -> User:
    return _authenticate(request, rate_limited=True)


async def current_user_for_close(request: Request) -> User:
    return _authenticate(request, rate_limited=False)
