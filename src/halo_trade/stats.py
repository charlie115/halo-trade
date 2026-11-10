"""Public, aggregate-only statistics: `GET /stats.json` and the `/stats` page.

These figures name no one: no addresses, tokens, sizes per wallet or text. Recent fills are
exposed as transaction hashes and explorer links only, and those transactions are public on
chain, where they show the wallet that traded. Results are cached for 30 s per app.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .context import AppContext
from .onboarding import NETWORKS, page_response

router = APIRouter()

CACHE_MS = 30_000
DAY_MS = 86_400_000
LAST_FILLS = 20
OPEN_STAGES = ("entry_filled", "stop_pending", "protected")
FILL_PURPOSES = ("entry", "close", "flatten")


@dataclass
class _Cache:
    expires_ms: int = 0
    body: Optional[dict[str, Any]] = None


def _dec(value: Optional[str]) -> Decimal:
    if value is None:
        return Decimal(0)
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(0)


def _fmt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def compute_stats(ctx: AppContext) -> dict[str, Any]:
    db = ctx.db
    now = ctx.clock()
    since = now - DAY_MS

    funded = 0
    deposits = Decimal(0)
    rows = db.execute(
        "SELECT u.id, "
        "(SELECT equity FROM equity_snapshots e WHERE e.user_id=u.id ORDER BY ts_ms ASC LIMIT 1) AS first_equity, "
        "(SELECT equity FROM equity_snapshots e WHERE e.user_id=u.id ORDER BY ts_ms DESC LIMIT 1) AS last_equity "
        "FROM users u WHERE u.status IN ('active','paused')"
    ).fetchall()
    for row in rows:
        if row["last_equity"] is None or _dec(row["last_equity"]) > 0:
            funded += 1
        deposits += _dec(row["first_equity"])

    volume_total = Decimal(0)
    volume_24h = Decimal(0)
    trades = 0
    for row in db.execute("SELECT quantity, entry_price, exit_price, opened_ms, closed_ms FROM trades"):
        trades += 1
        quantity = _dec(row["quantity"])
        entry_notional = quantity * _dec(row["entry_price"])
        volume_total += entry_notional
        if row["opened_ms"] >= since:
            volume_24h += entry_notional
        if row["exit_price"] is not None:
            exit_notional = quantity * _dec(row["exit_price"])
            volume_total += exit_notional
            if row["closed_ms"] is not None and row["closed_ms"] >= since:
                volume_24h += exit_notional

    stage_marks = ",".join("?" for _ in OPEN_STAGES)
    open_total = db.execute(
        f"SELECT COUNT(*) FROM intents WHERE stage IN ({stage_marks})", OPEN_STAGES
    ).fetchone()[0]
    protected = db.execute("SELECT COUNT(*) FROM intents WHERE stage='protected'").fetchone()[0]
    coverage = None if open_total == 0 else _fmt(Decimal(protected) * 100 / Decimal(open_total))

    explorer = NETWORKS.get(ctx.settings.network, NETWORKS["testnet"])["explorer_tx"]
    purpose_marks = ",".join("?" for _ in FILL_PURPOSES)
    fills = [
        {"tx_hash": row["tx_hash"], "explorer_url": explorer + row["tx_hash"]}
        for row in db.execute(
            f"SELECT tx_hash FROM txs WHERE status='ok' AND purpose IN ({purpose_marks}) "
            "ORDER BY id DESC LIMIT ?",
            (*FILL_PURPOSES, LAST_FILLS),
        )
    ]

    return {
        "ok": True,
        "as_of_ms": now,
        "funded_wallets": funded,
        "total_deposits_usd": _fmt(deposits),
        "volume_usd": _fmt(volume_total),
        "volume_24h_usd": _fmt(volume_24h),
        "trades": trades,
        "positions_open": open_total,
        "stop_coverage_pct": coverage,
        "last_fills": fills,
    }


@router.get("/stats.json")
async def stats_json(request: Request) -> JSONResponse:
    ctx: AppContext = request.app.state.ctx
    cache: Optional[_Cache] = getattr(request.app.state, "stats_cache", None)
    if cache is None:
        cache = _Cache()
        request.app.state.stats_cache = cache
    now = ctx.clock()
    if cache.body is None or now >= cache.expires_ms:
        cache.body = compute_stats(ctx)
        cache.expires_ms = now + CACHE_MS
    return JSONResponse(cache.body, headers={"Cache-Control": "public, max-age=30"})


@router.get("/stats", include_in_schema=False)
async def stats_page(request: Request) -> Response:
    return page_response(request.app.state.ctx, "stats.html")
