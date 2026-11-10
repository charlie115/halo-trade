"""Queue a message for the owner's Telegram chat. The Telegram worker delivers it.

Does nothing when the wallet has no linked chat or the bot is not configured, so any code path can
call it without checking. Text is written by this server from codes and numbers, never LLM output,
except where a caller quotes the owner's own agent reason on purpose.
"""
from __future__ import annotations

from typing import Any, Optional

from .context import AppContext
from .txmachine import transaction

# Delivered with `disable_notification`. Everything else makes a sound, so a new risk event cannot
# go quiet by being forgotten here. Holds are not on any list: they are never queued at all.
ROUTINE_KINDS = frozenset({"reply", "connect_prompt", "close_prompt", "disconnected", "agent_unblocked", "daily_summary"})
OPEN_KINDS = ("position_opened", "agent_entered")
CLOSE_KINDS = ("position_closed", "agent_closed")  # not a stop or take-profit: nobody decided those
REASON_MAX_CHARS = 200
REASON_RECENT_MS = 5 * 60_000


def is_routine(kind: str) -> bool:
    return kind in ROUTINE_KINDS


def _stated_reason(conn: Any, user_id: int, kind: str, now: int) -> Optional[str]:
    """The agent's reason for the order behind this notice. Its row is written before the order goes
    out, so it is there even while the order is still in flight. One line, cut short."""
    if kind in OPEN_KINDS:
        which = "stop_atr IS NOT NULL"
    elif kind in CLOSE_KINDS:
        which = "stop_atr IS NULL"
    else:
        return None
    row = conn.execute(
        f"SELECT reason FROM agent_decisions WHERE user_id=? AND created_ms>=? AND outcome IN ('unknown','entered','closed') AND {which} ORDER BY id DESC LIMIT 1",
        (user_id, now - REASON_RECENT_MS),
    ).fetchone()
    line = " ".join(row["reason"].split()) if row else ""
    if not line:
        return None
    return line if len(line) <= REASON_MAX_CHARS else f"{line[:REASON_MAX_CHARS - 3].rstrip()}..."


async def enqueue(ctx: AppContext, user_id: int, kind: str, text: str, *, dedupe_key: Optional[str] = None) -> None:
    if not ctx.settings.telegram_configured:
        return
    now = ctx.clock()
    async with ctx.db_lock:
        with transaction(ctx.db) as conn:
            if conn.execute("SELECT 1 FROM telegram_links WHERE user_id=?", (user_id,)).fetchone() is None:
                return
            reason = _stated_reason(conn, user_id, kind, now)
            if reason is not None:
                text = f"{text}\nAgent's stated reason: {reason}"
            conn.execute(
                "INSERT OR IGNORE INTO telegram_outbox(user_id,kind,text,dedupe_key,created_ms) VALUES(?,?,?,?,?)",
                (user_id, kind, text[:3500], dedupe_key, now),
            )
