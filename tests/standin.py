"""A stand-in for the decision engine, for trees that do not carry it.

The engine is reached through `halo_trade.plugin.engine()`. This module answers the same calls and
decides nothing: no loop runs, no model is asked, memory is empty. It lets the open tree test what
surrounds the engine (starting and stopping an agent, signatures, slots, the account views) without it.
With the real engine in the tree, the tests use that instead; see `conftest.py`.
"""
from __future__ import annotations

from typing import Any, Coroutine, Optional

CHAT_MAX_CHARS = 2000
TELEGRAM_FAILED = "The assistant could not answer."
NOT_HERE = "This tree has no assistant."


def feeds(settings: Any) -> None:
    return None


def attach(state: Any, service: Any) -> None:
    return None


def loops(state: Any, stop: Any) -> list[Coroutine[Any, Any, None]]:
    return []


def memory_view(ctx: Any, user_id: int, lessons: int) -> dict[str, Any]:
    return {"lessons": [], "playbook": []}


def market_view(ctx: Any) -> Optional[dict[str, Any]]:
    return None


async def chat(service: Any, user: Any, text: str) -> dict[str, Any]:
    return {"reply": NOT_HERE, "notice": True, "proposal": None, "left_today": 0}


def chat_history(ctx: Any, user_id: int) -> dict[str, Any]:
    return {"turns": [], "left_today": 0}


async def telegram_answer(service: Any, user: Any, text: str) -> str:
    return NOT_HERE
