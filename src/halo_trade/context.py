"""Shared application context. Routes read it from `request.app.state.ctx`."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .chain.base import ChainBackend
from .config import Settings


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class AppContext:
    settings: Settings
    db: sqlite3.Connection
    chain: ChainBackend
    clock: Callable[[], int] = wall_clock_ms
    # Serialises every DB write transaction and every per-signer tx flow.
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    llm: Any = None  # an `llm.LLMClient` or a test double; None means no LLM is configured
    feeds: Any = None  # a `desk.Fetcher` or a test double; None means the desk reads nothing
