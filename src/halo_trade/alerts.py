"""Operator alerts. Stored in `alerts`, optionally posted to the one configured webhook.

Details are codes and numbers only: never chain raw logs or anything a third party wrote.
This module is the only place that makes a non-Injective network call (hard rule 10).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .context import AppContext
from .txmachine import transaction

logger = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_S = 5.0
HEARTBEAT_KEY = "watcher_heartbeat_ms"
HEARTBEAT_STALE_MS = 60_000


async def raise_alert(
    ctx: AppContext,
    *,
    level: str,
    kind: str,
    detail: str = "",
    user_id: Optional[int] = None,
    dedupe_key: Optional[str] = None,
) -> None:
    now = ctx.clock()
    async with ctx.db_lock:
        with transaction(ctx.db) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts (level, kind, user_id, detail, dedupe_key, created_ms) VALUES (?,?,?,?,?,?)",
                (level, kind, user_id, detail, dedupe_key, now),
            )
    logger.warning("alert level=%s kind=%s user=%s %s", level, kind, user_id, detail)


async def flush(ctx: AppContext, *, client: Optional[httpx.AsyncClient] = None) -> int:
    """Post unsent alerts to the webhook, if configured. Failures are retried next time."""
    url = ctx.settings.alert_webhook
    if not url:
        return 0
    rows = ctx.db.execute("SELECT * FROM alerts WHERE sent_ms IS NULL ORDER BY id").fetchall()
    if not rows:
        return 0
    owned = client is None
    http = client or httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_S)
    sent = 0
    try:
        for row in rows:
            body = {
                "level": row["level"],
                "kind": row["kind"],
                "user_id": row["user_id"],
                "detail": row["detail"],
                "created_ms": row["created_ms"],
            }
            try:
                response = await http.post(url, json=body)
                response.raise_for_status()
            except httpx.HTTPError:
                logger.warning("alert webhook delivery failed for alert %s", row["id"])
                break
            async with ctx.db_lock:
                with transaction(ctx.db) as conn:
                    conn.execute("UPDATE alerts SET sent_ms=? WHERE id=?", (ctx.clock(), row["id"]))
            sent += 1
    finally:
        if owned:
            await http.aclose()
    return sent


def heartbeat_age_ms(ctx: AppContext) -> Optional[int]:
    row = ctx.db.execute("SELECT value FROM system_state WHERE key=?", (HEARTBEAT_KEY,)).fetchone()
    return None if row is None else ctx.clock() - int(row["value"])


async def check_watcher_heartbeat(ctx: AppContext, *, stale_ms: int = HEARTBEAT_STALE_MS) -> bool:
    """Raise a critical alert if the watcher heartbeat is stale. Returns True when stale."""
    age = heartbeat_age_ms(ctx)
    if age is None or age < stale_ms:
        return False
    bucket = ctx.clock() // stale_ms
    await raise_alert(
        ctx,
        level="critical",
        kind="watcher_stale",
        detail=f"heartbeat_age_ms={age}",
        dedupe_key=f"watcher_stale:{bucket}",
    )
    return True
