"""Wallet onboarding: `/onboard/*`, the onboarding page and its assets.

The flow, enforced here and never trusted from the page:

1. `POST /onboard/start` issues a server nonce and the exact EIP-4361 (Sign-In with Ethereum)
   text the wallet must sign. The domain line lets the wallet check it against the site it is on.
2. `POST /onboard/verify` checks the signature over that text and returns a short-lived session
   secret. Only the caller who produced the signature ever holds it.
3. The page deposits USDC and signs the AuthZ grant on chain (the server never sees a key).
4. `POST /onboard/complete` needs the session, re-checks grant and balance on chain, and records
   the wallet as `paused`: created off. The session stays valid for the rest of the page.
5. `POST /onboard/resume` starts the hosted agent, on a fresh signature, with a preset. `pause` and
   `revoke` stop it. Stopping never closes a position; `POST /onboard/agent/close` does that.

Challenges live in `onboard_challenges`, one per (address, action), so starting one action never
disturbs another. Verified sessions live apart in `onboard_sessions`, so `start` cannot touch one,
whoever calls it. Every proof is single-use. `POST /onboard/start` is per-client rate limited, but
wallets already in `users` skip both that limit and the challenge cap, so they can always revoke.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import bech32
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import plugin, tuning as tunings
from .account_view import result_r, unrealized_r
from .auth import RateLimiter
from .chain.base import Account as ChainAccount, ChainUnavailable, OrderKind, Position, Side
from .chain.shared import SharedValue
from .context import AppContext
from .contracts import CloseIn, ErrorCode
from .limits import ApiError, is_transfer, rejected
from .service import TERMINAL_STAGES, Service, User, _live_order, window_of
from .startup import parse_trusted_proxies, site_host
from .strategies import PRESET_EXITS, PRESETS

logger = logging.getLogger(__name__)

router = APIRouter()

SUBACCOUNT_NONCE = 1
NONCE_TTL_MS = 5 * 60 * 1000
SESSION_TTL_MS = 30 * 60 * 1000
MAX_PENDING_NONCES = 5000  # cap on live challenges, and on live sessions. Wallets in `users` are exempt.
START_PER_MIN = 30  # per client, only for addresses not yet in `users`
STATUS_PER_MIN = 60  # per session
STATUS_CACHE_MS = 10_000  # per address
AGENT_PER_MIN = 30  # per session
AGENT_CLOSE_PER_MIN = 30  # per session, its own bucket so polling can never use up a close
CHAT_PER_MIN = 12  # per session; the day's messages and the model budget are counted by the engine
CHAT_MAX_CHARS = 2000  # one chat message; the engine cuts its own input at the same length
CHAT_HISTORY_PER_MIN = 30  # per session
AGENT_DECISIONS = 100  # of the 96 a day at the default interval, enough to reach the last trade
AGENT_TRADES = 20
CLOSE_LOG_GRACE_MS = 60_000  # a close request is logged just after the close it asked for lands
HISTORY_MIN_MS = 3_600_000  # a line drawn from less than this says nothing
HISTORY_MIN_POINTS = 12
HISTORY_BUCKETS = 60  # keeps a low and a high from each, so thinning cannot hide a drop
CANDLES_PER_MIN = 60  # per client
CANDLES_SHOWN = 168  # a week of hours
CANDLES_CACHE_MS = 30_000  # one chain read per this, however many pages are polling
CANDLES_PERIOD_MS = 3_600_000
AGENT_LESSONS = 5
UNCLAIMED_SLOT_MS = 24 * 3600 * 1000  # a wallet that never started its agent holds a slot this long
ACTIONS = ("onboard", "pause", "resume", "revoke", "telegram")
# Fills users.token_hash: the agent API stays in the code but no route issues a token. Lookups
# compare against 64-hex sha256 digests, which can never equal a value with this prefix.
PENDING_TOKEN_PREFIX = "pending:"

NO_STORE = {"Cache-Control": "no-store"}

STATIC_DIR = Path(__file__).resolve().parent / "static"

_ETH_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_SIG_RE = re.compile(r"0x[0-9a-fA-F]{130}")

Action = Literal["onboard", "pause", "resume", "revoke", "telegram"]

_GRANT_PERMITS = {
    "/injective.exchange.v2.MsgCreateDerivativeMarketOrder": "Place market, stop and take-profit orders",
    "/injective.exchange.v2.MsgCancelDerivativeOrder": "Cancel orders",
}

# One line each, printable ASCII only: this becomes the EIP-4361 statement, which a wallet parses.
_ACTION_TEXT = {
    "onboard": "Sign in to halo-trade on Injective {network}. Signing proves this wallet is yours. It costs no gas. Only sign this on this site.",
    "pause": (
        "Stop the halo-trade AI agent for this wallet on Injective {network}. Any open position stays open with its stop. "
        "It costs no gas. Only sign this on this site."
    ),
    "resume": (
        "Start the halo-trade AI agent for this wallet on Injective {network}, using the {preset} strategy on {market}. "
        "Settings: {settings}. It can open and close positions in your trading subaccount and cannot withdraw. "
        "You can lose your whole deposit. "
        "It costs no gas. Only sign this on this site."
    ),
    "revoke": (
        "Revoke halo-trade access for this wallet on Injective {network}. The AI agent stops. The on-chain grant remains until "
        "you remove it, and any open position stays open. It costs no gas. Only sign this on this site."
    ),
    "telegram": (
        "Connect a Telegram chat to the halo-trade AI agent for this wallet on Injective {network}. That chat can see this "
        "wallet's agent, stop it and close its position. It costs no gas. Only sign this on this site."
    ),
}

TELEGRAM_DISCONNECTED = "This chat is now disconnected from its wallet. You will get no more messages about it here."

# chain_id / evm_chain_id match the SDK defaults for MetaMask signing on each network.
NETWORKS: dict[str, dict[str, Any]] = {
    "testnet": {
        "chain_id": "injective-888",
        "evm_chain_id": 11155111,
        "explorer_tx": "https://testnet.explorer.injective.network/transaction/",
        "connect_hosts": (
            "https://testnet.sentry.lcd.injective.network",
            "https://testnet.sentry.tm.injective.network",
            "https://testnet.sentry.chain.grpc-web.injective.network",
        ),
    },
    "mainnet": {
        "chain_id": "injective-1",
        "evm_chain_id": 1,
        "explorer_tx": "https://explorer.injective.network/transaction/",
        "connect_hosts": (
            "https://sentry.lcd.injective.network",
            "https://sentry.tm.injective.network",
            "https://sentry.chain.grpc-web.injective.network",
        ),
    },
}


class _Deny(Exception):
    def __init__(self, status: int, code: str, limit: Optional[str] = None, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.limit = limit
        self.extra = extra


def _denied(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Turns denials and chain transport errors into a code-only JSON body."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except _Deny as deny:
            body = {"ok": False, "code": deny.code, "limit": deny.limit, **deny.extra}
            return JSONResponse(body, status_code=deny.status)
        except ApiError as error:
            return JSONResponse(error.out.model_dump(mode="json"), status_code=error.status)
        except ChainUnavailable:
            return JSONResponse({"ok": False, "code": "CHAIN_UNAVAILABLE", "limit": None}, status_code=503)

    return wrapper


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartIn(_Strict):
    eth_address: str = Field(max_length=42)
    action: Action = "onboard"
    preset: Optional[str] = Field(default=None, max_length=32)  # `resume` only, and required there
    tuning: Optional[dict[str, Any]] = None  # `resume` only: the wallet's settings for the preset, checked in tuning.py

    @field_validator("eth_address")
    @classmethod
    def _addr(cls, value: str) -> str:
        return _check_eth(value)


class ProofIn(_Strict):
    eth_address: str = Field(max_length=42)
    signature: str = Field(max_length=132)

    @field_validator("eth_address")
    @classmethod
    def _addr(cls, value: str) -> str:
        return _check_eth(value)

    @field_validator("signature")
    @classmethod
    def _sig(cls, value: str) -> str:
        if not _SIG_RE.fullmatch(value):
            raise ValueError("signature format")
        return value


class SessionIn(_Strict):
    eth_address: str = Field(max_length=42)
    session: str = Field(min_length=16, max_length=128)

    @field_validator("eth_address")
    @classmethod
    def _addr(cls, value: str) -> str:
        return _check_eth(value)


class ChatIn(SessionIn):
    text: str = Field(min_length=1, max_length=CHAT_MAX_CHARS)


class ResumeIn(ProofIn):
    preset: str = Field(max_length=32)
    tuning: Optional[dict[str, Any]] = None  # must be what `start` was given: the signed text states it


def _check_eth(value: str) -> str:
    if not _ETH_RE.fullmatch(value):
        raise ValueError("eth_address format")
    return value.lower()


def eth_to_inj(eth_address: str) -> str:
    """Injective accounts share the EVM address bytes; only the encoding differs."""
    data = bytes.fromhex(eth_address[2:])
    words = bech32.convertbits(data, 8, 5)
    return bech32.bech32_encode("inj", words)


def subaccount_id(eth_address: str) -> str:
    """The dedicated trading subaccount, always nonce 1, never the default subaccount."""
    return f"0x{eth_address[2:].lower()}{SUBACCOUNT_NONCE:024x}"


def _agent_available(ctx: AppContext) -> bool:
    """An agent needs a model to ask and the engine that asks it. Without either, a wallet can still
    connect, read its account and revoke, and nothing is decided for it."""
    return ctx.llm is not None and plugin.engine() is not None


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@functools.lru_cache(maxsize=8)
def _trusted_proxies(value: str) -> tuple[Any, ...]:
    return parse_trusted_proxies(value)


def _client_ip(request: Request, ctx: AppContext) -> str:
    """The client's address. `X-Forwarded-For` counts only when the peer is a configured proxy."""
    peer = request.client.host if request.client else "unknown"
    proxies = _trusted_proxies(ctx.settings.trusted_proxy)
    if not proxies or not _in_networks(peer, proxies):
        return peer
    hops = [hop.strip() for header in request.headers.getlist("x-forwarded-for") for hop in header.split(",")]
    for hop in reversed([hop for hop in hops if hop]):
        if not _in_networks(hop, proxies):
            return hop
    return peer


def _in_networks(address: str, networks: tuple[Any, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def _client_key(request: Request, ctx: AppContext) -> str:
    """Rate-limit bucket for a client. An IPv6 client gets one /64, the unit one subscriber holds."""
    address = _client_ip(request, ctx)
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)


def _network(ctx: AppContext) -> dict[str, Any]:
    info = NETWORKS.get(ctx.settings.network)
    if info is None or (ctx.settings.network == "mainnet" and not ctx.settings.allow_mainnet):
        raise _Deny(503, "NETWORK_DISABLED", limit=f"network={ctx.settings.network}")
    return info


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chosen_tuning(ctx: AppContext, action: str, preset: Optional[str], raw: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The complete settings a `resume` asks for, or None for every other action. Only `resume` may carry any."""
    if action != "resume":
        if raw is not None:
            raise _Deny(400, "BAD_TUNING")
        return None
    if preset not in PRESETS:
        raise _Deny(400, "BAD_PRESET")
    try:
        return tunings.normalize(preset, raw, ctx.settings)
    except tunings.TuningError as error:
        raise _Deny(400, "BAD_TUNING", limit=str(error)) from None


def proof_message(
    ctx: AppContext, action: str, eth: str, nonce: str, issued_ms: int, expires_ms: int,
    preset: Optional[str] = None, tuning: Optional[dict[str, Any]] = None,
) -> str:
    """The exact EIP-4361 text signed with personal_sign, so the wallet can check the domain itself.

    The address must be EIP-55 and every field below in this order, or wallets reject the format.
    The preset and every setting are in the text, so a signature only ever starts the strategy its
    owner read. `tuning` is a complete object from `tunings.normalize`; None reads as the preset as designed.
    """
    network = NETWORKS[ctx.settings.network]
    base_url = ctx.settings.public_base_url
    chosen = ""
    if preset:
        chosen = tunings.summary(preset, tuning or tunings.defaults(preset, ctx.settings))
    statement = _ACTION_TEXT[action].format(
        network=ctx.settings.network, preset=PRESETS[preset].name if preset else "", market=ctx.settings.market_ticker, settings=chosen
    )
    return (
        f"{site_host(base_url)} wants you to sign in with your Ethereum account:\n"
        f"{to_checksum_address(eth)}\n\n"
        f"{statement}\n\n"
        f"URI: {base_url}\n"
        "Version: 1\n"
        f"Chain ID: {network['evm_chain_id']}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {_iso(issued_ms)}\n"
        f"Expiration Time: {_iso(expires_ms)}\n"
        f"Request ID: {action}"
    )


def _recovered(message: str, signature: str) -> Optional[str]:
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature).lower()
    except Exception:
        return None


@asynccontextmanager
async def _write_tx(ctx: AppContext):
    async with ctx.db_lock:
        ctx.db.execute("BEGIN IMMEDIATE")
        try:
            yield ctx.db
        except BaseException:
            ctx.db.execute("ROLLBACK")
            raise
        ctx.db.execute("COMMIT")


def _slots_used(ctx: AppContext, db: Any, now: int, exclude_user: Optional[int] = None) -> int:
    """Slots actually held: a wallet that started its agent and still shows the minimum deposit, or
    one that finished `/onboard/complete` less than 24 h ago. Dust cannot sit on a slot for long."""
    floor = ctx.settings.risk.min_deposit_usd
    cutoff = now - UNCLAIMED_SLOT_MS
    used = 0
    for row in db.execute(
        "SELECT u.created_ms, s.user_id IS NOT NULL AS started, "
        "(SELECT equity FROM equity_snapshots e WHERE e.user_id=u.id ORDER BY ts_ms DESC LIMIT 1) AS equity "
        "FROM users u LEFT JOIN strategies s ON s.user_id=u.id WHERE u.status IN ('active','paused') AND u.id IS NOT ?",
        (exclude_user,),
    ):
        funded = row["equity"] is not None and Decimal(row["equity"]) >= floor
        if (row["started"] and funded) or row["created_ms"] > cutoff:
            used += 1
    return used


PER_WALLET_MEMORY_TABLES = ("trade_cases",)


def release_telegram_link(db: Any, user_id: int, now: int) -> None:
    """Forget what the chat lane holds for this wallet, inside the caller's transaction, and tell the
    chat that held the link. Nothing a chat typed or saved may reach whoever connects next."""
    link = db.execute("SELECT chat_id FROM telegram_links WHERE user_id=?", (user_id,)).fetchone()
    for table in ("telegram_links", "telegram_link_codes", "chat_memory", "chat_turns"):
        db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    db.execute("DELETE FROM telegram_outbox WHERE user_id=? AND sent_ms IS NULL AND kind<>'disconnected'", (user_id,))
    if link is not None:
        db.execute(
            "INSERT INTO telegram_outbox(user_id,kind,text,chat_id,created_ms) VALUES(?,'disconnected',?,?,?)",
            (user_id, TELEGRAM_DISCONNECTED, link["chat_id"], now),
        )


def _user_status(db: Any, inj: str) -> Optional[str]:
    row = db.execute("SELECT status FROM users WHERE inj_address=?", (inj,)).fetchone()
    return row["status"] if row else None


def _grant_details(ctx: AppContext) -> tuple[str, list[dict[str, Any]], int]:
    """Grantee address, the grant list for the page, and the grant lifetime in seconds.

    The list itself lives in `chain/grants.py`, the source of truth `check_grant` enforces.
    """
    from .chain import grants  # Lazy: this pulls in the Injective SDK, unused in mock mode.

    # InjectiveChain exposes grantee_address; MockChain calls the same address its signer.
    grantee = getattr(ctx.chain, "grantee_address", None) or getattr(ctx.chain, "signer", None)
    if not grantee:
        raise _Deny(503, "GRANTEE_UNAVAILABLE")
    grant_list = [
        {
            "authz_type_url": spec.authz_type_url,
            "msg_type_url": spec.msg_type_url,
            "permits": _GRANT_PERMITS.get(spec.msg_type_url, spec.msg_type_url),
        }
        for spec in grants.REQUIRED_GRANTS
    ]
    return grantee, grant_list, grants.DEFAULT_GRANT_TTL_S


def _pending_token_hash() -> str:
    return PENDING_TOKEN_PREFIX + secrets.token_hex(32)


@router.get("/onboard/config")
@_denied
async def onboard_config(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    info = _network(ctx)
    market = await ctx.chain.market()
    grantee, grant_list, grant_ttl_s = _grant_details(ctx)
    used = _slots_used(ctx, ctx.db, ctx.clock())
    return {
        "ok": True,
        "network": ctx.settings.network,
        "chain_id": info["chain_id"],
        "evm_chain_id": info["evm_chain_id"],
        "market": {
            "ticker": market.ticker,
            "market_id": market.market_id,
            "quote_denom": market.quote_denom,
            "quote_decimals": 6,
        },
        "grantee": grantee,
        "grants": grant_list,
        "grant_ttl_seconds": grant_ttl_s,
        "subaccount_nonce": SUBACCOUNT_NONCE,
        "public_host": site_host(ctx.settings.public_base_url),
        "min_deposit_usd": str(ctx.settings.risk.min_deposit_usd),
        "max_deposit_usd": str(ctx.settings.risk.max_deposit_usd),
        "max_users": ctx.settings.max_users,
        "slots_left": max(0, ctx.settings.max_users - used),
        "presets": [
            {
                "id": preset.id, "name": preset.name, "summary": preset.summary, **PRESET_EXITS.get(preset.id, {}),
                # What a wallet may change before it signs, and what it gets when it changes nothing.
                "tuning": {"defaults": tunings.defaults(preset.id, ctx.settings), "options": tunings.options(preset.id, ctx.settings)},
            }
            for preset in PRESETS.values()
        ],
        "plan": {
            "decision_interval_s": ctx.settings.decision_interval_s,
            "risk_per_trade": str(ctx.settings.risk.risk_per_trade),
            "daily_loss": str(ctx.settings.risk.daily_loss),
        },
        "agent_available": _agent_available(ctx),
        "telegram_bot": ctx.settings.telegram_bot_username if ctx.settings.telegram_configured else None,
    }


def _limiter(request: Request, name: str, limit: int) -> RateLimiter:
    """A rate limiter living on the app, created on first use."""
    state = request.app.state
    limiter = getattr(state, name, None)
    if limiter is None:
        limiter = RateLimiter(limit=limit)
        setattr(state, name, limiter)
    return limiter


def _intent_open(db: Any, inj: str) -> bool:
    """From our own rows, because `/onboard/start` is public and must not cost a chain read."""
    marks = ",".join("?" for _ in TERMINAL_STAGES)
    row = db.execute(
        f"SELECT 1 FROM intents i JOIN users u ON u.id=i.user_id WHERE u.inj_address=? AND i.stage NOT IN ({marks}) LIMIT 1",
        (inj, *TERMINAL_STAGES),
    ).fetchone()
    return row is not None


@router.post("/onboard/start")
@_denied
async def onboard_start(request: Request, body: StartIn) -> dict[str, Any]:
    ctx = _ctx(request)
    _network(ctx)
    bad_preset = body.preset not in PRESETS if body.action == "resume" else body.preset is not None
    if bad_preset:
        raise _Deny(400, "BAD_PRESET")
    chosen = _chosen_tuning(ctx, body.action, body.preset, body.tuning)
    now = ctx.clock()
    eth = body.eth_address
    inj = eth_to_inj(eth)
    known = _user_status(ctx.db, inj) is not None
    if not known and not _limiter(request, "start_rate_limiter", START_PER_MIN).allow(_client_key(request, ctx), now):
        raise rejected(ErrorCode.RATE_LIMITED, limit=f"per_min={START_PER_MIN}")
    async with _write_tx(ctx) as db:
        db.execute("DELETE FROM onboard_challenges WHERE expires_ms<=?", (now,))
        row = db.execute(
            "SELECT nonce, issued_ms, expires_ms FROM onboard_challenges WHERE inj_address=? AND action=?",
            (inj, body.action),
        ).fetchone()
        if row is not None:
            # Replaying the same pending challenge lets a page reload or a stranger's start() not
            # invalidate the signature request the real wallet owner is about to approve.
            nonce, issued_ms, expires_ms = row["nonce"], row["issued_ms"], row["expires_ms"]
        else:
            if not known:
                pending = db.execute("SELECT COUNT(*) FROM onboard_challenges").fetchone()[0]
                if pending >= MAX_PENDING_NONCES:
                    raise _Deny(429, "TOO_MANY_PENDING", limit=f"MAX_PENDING={MAX_PENDING_NONCES}")
            nonce = secrets.token_hex(16)
            issued_ms, expires_ms = now, now + NONCE_TTL_MS
            db.execute(
                "INSERT INTO onboard_challenges(inj_address,action,nonce,issued_ms,expires_ms) VALUES(?,?,?,?,?)",
                (inj, body.action, nonce, issued_ms, expires_ms),
            )
    return {
        "ok": True,
        "action": body.action,
        "eth_address": eth,
        "inj_address": inj,
        "subaccount_id": subaccount_id(eth),
        "nonce": nonce,
        "expires_ms": expires_ms,
        "message": proof_message(ctx, body.action, eth, nonce, issued_ms, expires_ms, body.preset, chosen),
        **({"tuning": chosen} if chosen is not None else {}),  # complete and canonical: what `resume` must send back
        **({"position_open": _intent_open(ctx.db, inj)} if body.action == "revoke" else {}),
    }


def _load_challenge(ctx: AppContext, inj: str, action: str, now: int) -> tuple[str, int, int]:
    row = ctx.db.execute(
        "SELECT nonce, issued_ms, expires_ms FROM onboard_challenges WHERE inj_address=? AND action=?",
        (inj, action),
    ).fetchone()
    if row is None or row["expires_ms"] <= now:
        raise _Deny(401, "NONCE_INVALID")
    return row["nonce"], row["issued_ms"], row["expires_ms"]


def _check_signature(
    ctx: AppContext, action: str, eth: str, inj: str, signature: str, now: int, *,
    preset: Optional[str] = None, tuning: Optional[dict[str, Any]] = None,
) -> None:
    nonce, issued_ms, expires_ms = _load_challenge(ctx, inj, action, now)
    message = proof_message(ctx, action, eth, nonce, issued_ms, expires_ms, preset, tuning)
    recovered = _recovered(message, signature)
    if recovered is None or not hmac.compare_digest(recovered, eth):
        raise _Deny(401, "BAD_SIGNATURE")


@router.post("/onboard/verify")
@_denied
async def onboard_verify(request: Request, body: ProofIn) -> JSONResponse:
    ctx = _ctx(request)
    _network(ctx)
    now = ctx.clock()
    eth = body.eth_address
    inj = eth_to_inj(eth)
    async with _write_tx(ctx) as db:
        _check_signature(ctx, "onboard", eth, inj, body.signature, now)
        status = _user_status(db, inj)
        db.execute("DELETE FROM onboard_sessions WHERE expires_ms<=?", (now,))
        if status is None:
            live = db.execute("SELECT COUNT(*) FROM onboard_sessions").fetchone()[0]
            if live >= MAX_PENDING_NONCES:
                raise _Deny(429, "TOO_MANY_PENDING", limit=f"MAX_PENDING={MAX_PENDING_NONCES}")
        session = secrets.token_urlsafe(32)
        expires_ms = now + SESSION_TTL_MS
        db.execute("DELETE FROM onboard_challenges WHERE inj_address=? AND action='onboard'", (inj,))
        db.execute(
            "INSERT INTO onboard_sessions(inj_address,session_hash,expires_ms) VALUES(?,?,?) "
            "ON CONFLICT(inj_address) DO UPDATE SET session_hash=excluded.session_hash, expires_ms=excluded.expires_ms",
            (inj, _sha256(session), expires_ms),
        )
    return JSONResponse(
        {
            "ok": True,
            "session": session,
            "expires_ms": expires_ms,
            "inj_address": inj,
            "subaccount_id": subaccount_id(eth),
            "user_status": status,
        },
        headers=NO_STORE,
    )


def _require_session(ctx: AppContext, inj: str, session: str, now: int) -> None:
    row = ctx.db.execute("SELECT session_hash, expires_ms FROM onboard_sessions WHERE inj_address=?", (inj,)).fetchone()
    if row is None or row["expires_ms"] <= now or not hmac.compare_digest(row["session_hash"], _sha256(session)):
        raise _Deny(401, "SESSION_REQUIRED")


@dataclass
class _ChainStatus:
    expires_ms: int
    grant: Any
    account: Any


async def _chain_status(request: Request, ctx: AppContext, inj: str, sub: str, now: int) -> _ChainStatus:
    """Grant and balance for one address, from chain at most once per `STATUS_CACHE_MS`."""
    state = request.app.state
    cache: Optional[dict[str, _ChainStatus]] = getattr(state, "status_cache", None)
    if cache is None:
        cache = state.status_cache = {}
    cached = cache.get(inj)
    if cached is not None and cached.expires_ms > now:
        return cached
    entry = _ChainStatus(now + STATUS_CACHE_MS, await ctx.chain.check_grant(inj, sub), await ctx.chain.account(inj, sub))
    for key in [key for key, value in cache.items() if value.expires_ms <= now]:
        del cache[key]  # Expired entries go, so only recently verified addresses are ever held.
    cache[inj] = entry
    return entry


@router.post("/onboard/status")
@_denied
async def onboard_status(request: Request, body: SessionIn) -> dict[str, Any]:
    ctx = _ctx(request)
    _network(ctx)
    eth = body.eth_address
    inj = eth_to_inj(eth)
    sub = subaccount_id(eth)
    now = ctx.clock()
    _require_session(ctx, inj, body.session, now)
    if not _limiter(request, "status_rate_limiter", STATUS_PER_MIN).allow(_sha256(body.session), now):
        raise rejected(ErrorCode.RATE_LIMITED, limit=f"per_min={STATUS_PER_MIN}")
    status = await _chain_status(request, ctx, inj, sub, now)
    grant = status.grant
    account = status.account
    return {
        "ok": True,
        "inj_address": inj,
        "subaccount_id": sub,
        "user_status": _user_status(ctx.db, inj),
        "grant_ok": grant.ok,
        "grant_missing": list(grant.missing),
        "grant_expires_ms": grant.expires_ms,
        "balance_usd": str(account.equity),
        "available_usd": str(account.available),
        "min_deposit_usd": str(ctx.settings.risk.min_deposit_usd),
        "max_deposit_usd": str(ctx.settings.risk.max_deposit_usd),
    }


def _balance_error(ctx: AppContext, equity: Decimal, *, running: bool = False) -> Optional[_Deny]:
    """`running`: the deposit ceiling holds until a wallet has run once. After that, profit can
    carry it past the ceiling, and the limit on running equity is the cap every entry is checked against."""
    risk = ctx.settings.risk
    if equity <= 0:
        return _Deny(409, "BALANCE_EMPTY")
    if equity < risk.min_deposit_usd:
        return _Deny(409, "BELOW_MIN_DEPOSIT", limit=f"MIN_DEPOSIT_USD={risk.min_deposit_usd}", balance_usd=str(equity))
    if running:
        if equity > risk.max_equity_usd:
            return _Deny(409, "OVER_CAP", limit=f"MAX_EQUITY_USD={risk.max_equity_usd}", balance_usd=str(equity))
    elif equity > risk.max_deposit_usd:
        return _Deny(409, "OVER_MAX_DEPOSIT", limit=f"MAX_DEPOSIT_USD={risk.max_deposit_usd}", balance_usd=str(equity))
    return None


def _wallet(db: Any, inj: str) -> Optional[Any]:
    return db.execute(
        "SELECT u.id, u.status, u.created_ms, s.user_id IS NOT NULL AS started, "
        "EXISTS (SELECT 1 FROM strategy_runs r WHERE r.user_id=u.id) AS has_run "
        "FROM users u LEFT JOIN strategies s ON s.user_id=u.id WHERE u.inj_address=?",
        (inj,),
    ).fetchone()


@router.post("/onboard/complete")
@_denied
async def onboard_complete(request: Request, body: SessionIn) -> JSONResponse:
    ctx = _ctx(request)
    _network(ctx)
    now = ctx.clock()
    eth = body.eth_address
    inj = eth_to_inj(eth)
    sub = subaccount_id(eth)
    _require_session(ctx, inj, body.session, now)

    prior = _wallet(ctx.db, inj)
    existing = prior["status"] if prior is not None else None
    lapsed = prior is not None and existing != "revoked" and not prior["started"] and prior["created_ms"] <= now - UNCLAIMED_SLOT_MS
    account = None
    if existing in (None, "revoked") or lapsed:
        # A new wallet, a returning wallet that revoked, or one that never started within 24 h:
        # all must prove grant and deposit again.
        grant = await ctx.chain.check_grant(inj, sub)
        if not grant.ok:
            raise _Deny(403, "GRANT_MISSING", missing=list(grant.missing))
        account = await ctx.chain.account(inj, sub)
        denial = _balance_error(ctx, account.equity)
        if denial is not None:
            raise denial

    now = ctx.clock()
    async with _write_tx(ctx) as db:
        # Re-check inside the transaction so concurrent completes cannot both proceed.
        _require_session(ctx, inj, body.session, now)
        status = _user_status(db, inj)
        if status != existing:
            # The grant/balance decision above no longer describes this wallet.
            raise _Deny(409, "BAD_STATE", limit=f"status={status}")
        if account is not None and _slots_used(ctx, db, now) >= ctx.settings.max_users:
            raise _Deny(409, "USERS_FULL", limit=f"MAX_USERS={ctx.settings.max_users}")
        if status is None:
            status = "paused"
            user_id = db.execute(
                "INSERT INTO users(inj_address,eth_address,subaccount_id,status,token_hash,"
                "grant_checked_ms,created_ms) VALUES(?,?,?,'paused',?,?,?)",
                (inj, eth, sub, _pending_token_hash(), now, now),
            ).lastrowid
        else:
            user_id = prior["id"]
            if status == "revoked":
                status = "paused"
                db.execute(
                    "UPDATE users SET status='paused', token_hash=?, grant_checked_ms=?, created_ms=? WHERE id=?",
                    (_pending_token_hash(), now, now, user_id),
                )
            elif lapsed:
                db.execute("UPDATE users SET created_ms=? WHERE id=?", (now, user_id))  # a fresh 24 h on the slot
        if account is not None:
            # First snapshot of this onboarding: this is what total deposits are counted from.
            db.execute(
                "INSERT OR IGNORE INTO equity_snapshots(user_id,ts_ms,equity,flat) VALUES(?,?,?,1)",
                (user_id, now, str(account.equity)),
            )
    return JSONResponse({"ok": True, "status": status, "inj_address": inj, "subaccount_id": sub}, headers=NO_STORE)


def _close_run(db: Any, user_id: int, reason: str, now: int) -> Optional[int]:
    row = db.execute("SELECT id FROM strategy_runs WHERE user_id=? AND status='active'", (user_id,)).fetchone()
    if row is None:
        return None
    db.execute("UPDATE strategy_runs SET status='stopped', stop_reason=?, stopped_ms=? WHERE id=?", (reason, now, row["id"]))
    return row["id"]


async def _read_end_equity(ctx: AppContext, inj: str, sub: str, run_id: Optional[int]) -> Optional[ChainAccount]:
    """Done after the status change, so a stop takes effect even while the chain cannot be read."""
    try:
        account = await ctx.chain.account(inj, sub)
    except ChainUnavailable:
        return None
    if run_id is not None:
        async with _write_tx(ctx) as db:
            db.execute("UPDATE strategy_runs SET end_equity=? WHERE id=?", (str(account.equity), run_id))
    return account


async def _status_change(
    ctx: AppContext, body: ProofIn, action: str, allowed_from: tuple[str, ...], new_status: str, stop_reason: str
) -> tuple[dict[str, Any], Optional[int]]:
    """Returns the response body and the strategy run this closed, if any."""
    _network(ctx)
    now = ctx.clock()
    eth = body.eth_address
    inj = eth_to_inj(eth)
    async with _write_tx(ctx) as db:
        _check_signature(ctx, action, eth, inj, body.signature, now)
        wallet = _wallet(db, inj)
        if wallet is None:
            raise _Deny(404, "USER_NOT_FOUND")
        status = wallet["status"]
        user_id = wallet["id"]
        db.execute("DELETE FROM onboard_challenges WHERE inj_address=? AND action=?", (inj, action))
        if status == new_status:
            return {"ok": True, "status": status, "changed": False}, None
        if status not in allowed_from:
            raise _Deny(409, "BAD_STATE", limit=f"status={status}")
        if new_status == "revoked":
            # The token stops matching regardless of what checks the agent API performs.
            db.execute("UPDATE users SET status='revoked', token_hash=? WHERE id=?", ("revoked:" + secrets.token_hex(32), user_id))
            db.execute("DELETE FROM strategies WHERE user_id=?", (user_id,))
            release_telegram_link(db, user_id, now)
            # The agent's memory goes with the wallet's access. Disconnecting Telegram alone keeps it.
            for table in PER_WALLET_MEMORY_TABLES:
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            # Coming back needs a fresh proof.
            db.execute("DELETE FROM onboard_sessions WHERE inj_address=?", (inj,))
            db.execute("DELETE FROM onboard_challenges WHERE inj_address=?", (inj,))
        else:
            db.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
        run_id = _close_run(db, user_id, stop_reason, now)
    return {"ok": True, "status": new_status, "changed": True}, run_id


@router.post("/onboard/pause")
@_denied
async def onboard_pause(request: Request, body: ProofIn) -> dict[str, Any]:
    ctx = _ctx(request)
    result, run_id = await _status_change(ctx, body, "pause", ("active",), "paused", "user")
    if run_id is not None:
        await _read_end_equity(ctx, eth_to_inj(body.eth_address), subaccount_id(body.eth_address), run_id)
    return result


@router.post("/onboard/resume")
@_denied
async def onboard_resume(request: Request, body: ResumeIn) -> dict[str, Any]:
    """Refusals that need no signature come first, so they cannot spend the single-use challenge."""
    ctx = _ctx(request)
    _network(ctx)
    if body.preset not in PRESETS:
        raise _Deny(400, "BAD_PRESET")
    chosen = _chosen_tuning(ctx, "resume", body.preset, body.tuning)
    if not _agent_available(ctx):
        raise _Deny(409, "AGENT_UNAVAILABLE")
    eth = body.eth_address
    inj = eth_to_inj(eth)
    sub = subaccount_id(eth)
    _check_signature(ctx, "resume", eth, inj, body.signature, ctx.clock(), preset=body.preset, tuning=chosen)
    prior = _wallet(ctx.db, inj)
    if prior is None:
        raise _Deny(404, "USER_NOT_FOUND")
    grant = await ctx.chain.check_grant(inj, sub)
    if not grant.ok:
        raise _Deny(403, "GRANT_MISSING", missing=list(grant.missing))
    account = await ctx.chain.account(inj, sub)
    denial = _balance_error(ctx, account.equity, running=bool(prior["has_run"]))
    if denial is not None:
        raise denial

    now = ctx.clock()
    stored = tunings.dumps(chosen)
    async with _write_tx(ctx) as db:
        _check_signature(ctx, "resume", eth, inj, body.signature, now, preset=body.preset, tuning=chosen)
        wallet = _wallet(db, inj)
        if wallet is None:
            raise _Deny(404, "USER_NOT_FOUND")
        db.execute("DELETE FROM onboard_challenges WHERE inj_address=? AND action='resume'", (inj,))
        if wallet["status"] == "active" and wallet["started"]:
            # Running already. The same settings signed again change nothing. Different ones take over
            # below: the run under the old settings ends and one under the signed settings begins.
            current = db.execute("SELECT preset, tuning FROM strategies WHERE user_id=?", (wallet["id"],)).fetchone()
            if current["preset"] == body.preset and tunings.resolve(current["preset"], current["tuning"], ctx.settings) == chosen:
                return {"ok": True, "status": "active", "changed": False}
        if wallet["status"] not in ("active", "paused"):
            raise _Deny(409, "BAD_STATE", limit=f"status={wallet['status']}")
        if _slots_used(ctx, db, now, exclude_user=wallet["id"]) >= ctx.settings.max_users:
            raise _Deny(409, "USERS_FULL", limit=f"MAX_USERS={ctx.settings.max_users}")
        db.execute(
            "INSERT INTO strategies(user_id,preset,tuning,next_run_ms,updated_ms,wake_reason) VALUES(?,?,?,?,?,'first') "
            "ON CONFLICT(user_id) DO UPDATE SET preset=excluded.preset, tuning=excluded.tuning, next_run_ms=excluded.next_run_ms, "
            "updated_ms=excluded.updated_ms, wake_reason='first', last_mark=NULL, last_wake_ms=NULL",
            (wallet["id"], body.preset, stored, now, now),
        )
        db.execute("UPDATE users SET status='active' WHERE id=?", (wallet["id"],))
        ended = _close_run(db, wallet["id"], "superseded", now)  # only one may be active
        if ended is not None:
            db.execute("UPDATE strategy_runs SET end_equity=? WHERE id=?", (str(account.equity), ended))
        db.execute(
            "INSERT INTO strategy_runs(user_id,preset,tuning,status,start_equity,started_ms) VALUES(?,?,?,'active',?,?)",
            (wallet["id"], body.preset, stored, str(account.equity), now),
        )
    return {"ok": True, "status": "active", "changed": True}


@router.post("/onboard/revoke")
@_denied
async def onboard_revoke(request: Request, body: ProofIn) -> dict[str, Any]:
    ctx = _ctx(request)
    result, run_id = await _status_change(ctx, body, "revoke", ("active", "paused"), "revoked", "revoked")
    inj = eth_to_inj(body.eth_address)
    sub = subaccount_id(body.eth_address)
    grantee, grant_list, _ = _grant_details(ctx)
    # None when unknown: the DB change already happened and the page warns about it.
    account = await _read_end_equity(ctx, inj, sub, run_id)
    position_open = None if account is None else account.position is not None
    return {**result, "grantee": grantee, "grants": grant_list, "position_open": position_open}


def _service(request: Request) -> Service:
    return request.app.state.service


def _session_user(request: Request, body: SessionIn, limiter_name: str, per_min: int) -> User:
    ctx = _ctx(request)
    _network(ctx)
    now = ctx.clock()
    inj = eth_to_inj(body.eth_address)
    _require_session(ctx, inj, body.session, now)
    if not _limiter(request, limiter_name, per_min).allow(_sha256(body.session), now):
        raise rejected(ErrorCode.RATE_LIMITED, limit=f"per_min={per_min}")
    row = ctx.db.execute("SELECT * FROM users WHERE inj_address=?", (inj,)).fetchone()
    if row is None:
        raise _Deny(404, "USER_NOT_FOUND")
    return User.from_row(row)


def _loss_if_stopped(position: Position, stop_price: Decimal) -> Decimal:
    """An estimate: a stop fills at or worse than its trigger. Negative if the stop is past entry."""
    move = position.entry_price - stop_price if position.side is Side.LONG else stop_price - position.entry_price
    return move * position.quantity


def _stop_tx_hash(db: Any, user_id: int, intent_id: int) -> Optional[str]:
    row = db.execute(
        "SELECT tx_hash FROM txs WHERE user_id=? AND intent_id=? AND purpose='stop' AND status='ok' ORDER BY id DESC LIMIT 1",
        (user_id, intent_id),
    ).fetchone()
    return row["tx_hash"] if row else None


def _position_fields(service: Service, user: User, account: ChainAccount) -> Optional[dict[str, Any]]:
    out = service._position_out(user, account, window_of(service.ctx.clock()))
    if out is None or account.position is None:
        return None
    intent = service.open_intent(user.id)
    stop = _live_order(account, OrderKind.STOP, account.position.side, intent)
    trigger = None if stop is None else stop.trigger_price
    return {
        **out.model_dump(exclude={"windows_held"}),
        "opened_ms": _opened_ms(service.ctx.db, user.id, intent),
        "unrealized_r": unrealized_r(account.position, None if intent is None else intent.stop_price),
        "loss_if_stopped_usd": None if trigger is None else str(_loss_if_stopped(account.position, trigger)),
        "stop_tx_hash": None if trigger is None or intent is None else _stop_tx_hash(service.ctx.db, user.id, intent.id),
    }


def _opened_ms(db: Any, user_id: int, intent: Any) -> Optional[int]:
    if intent is None:
        return None
    row = db.execute("SELECT opened_ms FROM trades WHERE user_id=? AND intent_id=? AND closed_ms IS NULL ORDER BY id DESC LIMIT 1", (user_id, intent.id)).fetchone()
    return None if row is None else row["opened_ms"]


def _daily_loss(service: Service, user: User, account: ChainAccount) -> dict[str, str]:
    """Used of today's limit, on the same equity and day start as the check every entry goes through."""
    view = service._risk_view(user.id, account.equity, service.ctx.clock())
    limit = view.day_start_equity * service.risk_for(user.id).daily_loss
    return {"used_usd": str(max(view.day_start_equity - account.equity, Decimal(0))), "limit_usd": str(limit)}


async def _account_view(service: Service, user: User) -> tuple[dict[str, Any], Optional[ChainAccount]]:
    """`position` is null only for a read that succeeded on a flat account."""
    try:
        account = await service.read_account(user)
    except ChainUnavailable:
        return {"position": None, "position_unavailable": True, "daily_loss": None}, None
    return {"position": _position_fields(service, user, account), "daily_loss": _daily_loss(service, user, account)}, account


def _read_transfers(db: Any, user_id: int, start_equity: Decimal, since_ms: int, until_ms: int) -> tuple[list[tuple[int, Decimal]], list[tuple[int, Decimal]]]:
    """Equity over the run, and the deposits and withdrawals inside it. While flat with no trade
    closing, nothing but a transfer can move the balance, so even a small one is caught there.
    In a position funding moves it too, so the rule the risk limits use decides."""
    closes = [row["closed_ms"] for row in db.execute(
        "SELECT closed_ms FROM trades WHERE user_id=? AND closed_ms IS NOT NULL AND closed_ms>=? AND closed_ms<=? ORDER BY closed_ms", (user_id, since_ms, until_ms)
    )]
    points: list[tuple[int, Decimal]] = []
    transfers: list[tuple[int, Decimal]] = []
    previous, previous_flat, previous_ms = start_equity, None, since_ms
    for row in db.execute(
        "SELECT ts_ms, equity, flat, balance FROM equity_snapshots WHERE user_id=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms", (user_id, since_ms, until_ms)
    ):
        balance, flat = Decimal(row["balance"] or row["equity"]), bool(row["flat"])
        points.append((row["ts_ms"], Decimal(row["equity"])))
        if previous_flat is None:
            previous_flat = flat  # the start was read with the account in this same state
        closed_between = any(previous_ms <= ms <= row["ts_ms"] for ms in closes)
        moved = balance - previous
        if moved and ((flat and previous_flat and not closed_between) or is_transfer(previous, previous_flat, balance, flat, closed_between)):
            transfers.append((row["ts_ms"], moved))
        previous, previous_flat, previous_ms = balance, flat, row["ts_ms"]
    return points, transfers


def _thin(points: list[tuple[int, Decimal]]) -> list[tuple[int, Decimal]]:
    if len(points) <= HISTORY_BUCKETS * 2:
        return points
    size = len(points) / HISTORY_BUCKETS
    kept: list[tuple[int, Decimal]] = []
    for index in range(HISTORY_BUCKETS):
        chunk = points[int(index * size):int((index + 1) * size)]
        low, high = min(chunk, key=lambda point: point[1]), max(chunk, key=lambda point: point[1])
        kept.extend(sorted({low, high}))
    return kept if kept[-1] == points[-1] else [*kept, points[-1]]


def _hold_result(db: Any, user_id: int, start_equity: Decimal, since_ms: int, until_ms: int) -> Optional[str]:
    """Start equity left in the market, unlevered, from the first mark the agent read to the last."""
    marks = db.execute(
        "SELECT json_extract(snapshot,'$.seen.mark') AS mark FROM agent_decisions "
        "WHERE user_id=? AND created_ms>=? AND created_ms<=? AND json_extract(snapshot,'$.seen.mark') IS NOT NULL ORDER BY id",
        (user_id, since_ms, until_ms),
    ).fetchall()
    if len(marks) < 2 or not marks[0]["mark"]:
        return None
    return str(start_equity * (Decimal(str(marks[-1]["mark"])) / Decimal(str(marks[0]["mark"])) - 1))


def _performance(ctx: AppContext, user_id: int, live_equity: Optional[Decimal]) -> Optional[dict[str, Any]]:
    """The newest run's result, net of transfers. Only what is on record: nothing is projected."""
    run = ctx.db.execute("SELECT * FROM strategy_runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if run is None:
        return None
    active = run["status"] == "active"
    until_ms = ctx.clock() if active else run["stopped_ms"]
    start = None if run["start_equity"] is None else Decimal(run["start_equity"])
    end = live_equity if active else (None if run["end_equity"] is None else Decimal(run["end_equity"]))
    points, transfers = _read_transfers(ctx.db, user_id, start, run["started_ms"], until_ms) if start is not None else ([], [])
    moved = sum((amount for _, amount in transfers), Decimal(0))
    if active and end is not None:
        points = [*points, (until_ms, end)]
    short = len(points) < HISTORY_MIN_POINTS or points[-1][0] - points[0][0] < HISTORY_MIN_MS
    trades = ctx.db.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE user_id=? AND closed_ms IS NOT NULL AND closed_ms>=? AND closed_ms<=?", (user_id, run["started_ms"], until_ms)
    ).fetchone()["n"]
    return {
        "active": active,
        "since_ms": run["started_ms"],
        "until_ms": None if active else until_ms,
        "start_equity": None if start is None else str(start),
        "equity": None if end is None else str(end),
        "net_usd": None if start is None or end is None else str(end - start - moved),
        "transfers_usd": str(moved),
        "transfers": [{"ts_ms": ts_ms, "amount_usd": str(amount)} for ts_ms, amount in transfers],
        "trades": trades,
        "history_too_short": short,
        "points": [] if short else [[ts_ms, float(value)] for ts_ms, value in _thin(points)],
        # With money moved mid-run, the same start amount held is no longer a fair comparison.
        "hold_usd": None if short or transfers or start is None else _hold_result(ctx.db, user_id, start, run["started_ms"], until_ms),
    }


STAT_KEYS = ("trades", "wins", "losses", "win_rate", "avg_r", "best_r", "worst_r", "avg_hold_ms", "max_drawdown_pct")


def _run_trades(ctx: AppContext, user_id: int, since_ms: int, until_ms: int) -> list[Any]:
    return ctx.db.execute(
        "SELECT t.pnl_usd, t.entry_price, t.quantity, t.opened_ms, t.closed_ms, i.stop_price FROM trades t LEFT JOIN intents i ON i.id=t.intent_id "
        "WHERE t.user_id=? AND t.closed_ms IS NOT NULL AND t.closed_ms>=? AND t.closed_ms<=?",
        (user_id, since_ms, until_ms),
    ).fetchall()


def _max_drawdown_pct(ctx: AppContext, user_id: int, run: Any, until_ms: int, live_equity: Optional[Decimal]) -> Optional[float]:
    """Worst fall from a high in percent, net of deposits and withdrawals. Too little history says nothing."""
    if run["start_equity"] is None:
        return None
    start = Decimal(run["start_equity"])
    points, transfers = _read_transfers(ctx.db, user_id, start, run["started_ms"], until_ms)
    if run["status"] == "active" and live_equity is not None:
        points = [*points, (until_ms, live_equity)]
    if len(points) < HISTORY_MIN_POINTS or points[-1][0] - points[0][0] < HISTORY_MIN_MS:
        return None
    moved_at = {ts_ms: amount for ts_ms, amount in transfers}
    # A growth index, not a running balance, so a deposit that doubles the account cannot read as a loss.
    growth = peak = Decimal(1)
    worst = Decimal(0)
    previous = start
    for ts_ms, equity in points:
        base = previous + moved_at.get(ts_ms, Decimal(0))
        if base > 0:
            growth *= equity / base
            peak = max(peak, growth)
            worst = max(worst, (peak - growth) / peak)
        previous = equity
    return round(float(worst * 100), 2)


def _decimal_or_none(text: Any) -> Optional[Decimal]:
    """A stored figure, or None. One unreadable row must not take the whole dashboard down with it."""
    try:
        value = Decimal(str(text)) if text is not None else None
    except InvalidOperation:
        return None
    return value if value is not None and value.is_finite() else None


def _run_stats(ctx: AppContext, user_id: int, live_equity: Optional[Decimal]) -> dict[str, Any]:
    """The newest run only. Null means unknown: a count of zero is real, a zero rate would not be."""
    run = ctx.db.execute("SELECT * FROM strategy_runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if run is None:
        return dict.fromkeys(STAT_KEYS)
    until_ms = ctx.clock() if run["status"] == "active" else run["stopped_ms"]
    rows = _run_trades(ctx, user_id, run["started_ms"], until_ms)
    pnls = [pnl for pnl in (_decimal_or_none(row["pnl_usd"]) for row in rows) if pnl is not None]
    results = [r for r in (result_r(row) for row in rows) if r is not None]
    wins = sum(pnl > 0 for pnl in pnls)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": sum(pnl < 0 for pnl in pnls),
        "win_rate": None if not pnls else round(wins / len(pnls), 3),
        "avg_r": None if not results else round(sum(results) / len(results), 2),
        "best_r": None if not results else round(max(results), 2),
        "worst_r": None if not results else round(min(results), 2),
        "avg_hold_ms": None if not rows else round(sum(row["closed_ms"] - row["opened_ms"] for row in rows) / len(rows)),
        "max_drawdown_pct": _max_drawdown_pct(ctx, user_id, run, until_ms, live_equity),
    }


def _agent_memory(ctx: AppContext, user_id: int) -> dict[str, Any]:
    cases = ctx.db.execute("SELECT COUNT(*) AS n FROM trade_cases WHERE user_id=?", (user_id,)).fetchone()["n"]
    engine = plugin.engine()
    view = {"lessons": [], "playbook": []} if engine is None else engine.memory_view(ctx, user_id, AGENT_LESSONS)
    return {"cases": cases, **view}


def _market_view(ctx: AppContext) -> Optional[dict[str, Any]]:
    """The page's only source of the market desk's words: never a prompt. None when there is nothing to show."""
    engine = plugin.engine()
    return None if engine is None else engine.market_view(ctx)


def _parse_snapshot(text: Optional[str]) -> Optional[dict[str, Any]]:
    try:
        return None if text is None else json.loads(text)
    except ValueError:
        return None


def _closed_by(db: Any, user_id: int, opened_ms: int, closed_ms: int) -> Optional[str]:
    """`close_reason` is "close" whoever asked for it, so only the decision log can say who did."""
    row = db.execute(
        "SELECT payload_json FROM decisions WHERE user_id=? AND kind='close' AND created_ms BETWEEN ? AND ? "
        "ORDER BY ABS(created_ms-?), id DESC LIMIT 1",
        (user_id, opened_ms, closed_ms + CLOSE_LOG_GRACE_MS, closed_ms),
    ).fetchone()
    reason = json.loads(row["payload_json"]).get("agent_reason") if row else None
    if not reason:
        return None
    return "owner" if reason in ("owner", "telegram") else "agent"


def _recent_trades(db: Any, user_id: int) -> list[dict[str, Any]]:
    """Closed round trips only. `pnl_usd` and `exit_price` come from the change in equity, so a
    transfer during a trade moves them: the page marks both as approximate."""
    trades = []
    for row in db.execute(
        "SELECT side, quantity, entry_price, exit_price, pnl_usd, close_reason, opened_ms, closed_ms, intent_id FROM trades "
        "WHERE user_id=? AND closed_ms IS NOT NULL ORDER BY closed_ms DESC, id DESC LIMIT ?",
        (user_id, AGENT_TRADES),
    ).fetchall():
        tx = db.execute(
            "SELECT tx_hash FROM txs WHERE user_id=? AND intent_id=? AND status='ok' AND purpose IN ('close','flatten','entry') "
            "ORDER BY purpose='entry', id DESC LIMIT 1",
            (user_id, row["intent_id"]),
        ).fetchone()
        trade = {key: row[key] for key in ("side", "quantity", "entry_price", "exit_price", "pnl_usd", "close_reason", "opened_ms", "closed_ms")}
        closed_by = _closed_by(db, user_id, row["opened_ms"], row["closed_ms"]) if row["close_reason"] == "close" else None
        trades.append({**trade, "closed_by": closed_by, "tx_hash": tx["tx_hash"] if tx else None})
    return trades


@router.post("/onboard/agent")
@_denied
async def onboard_agent(request: Request, body: SessionIn) -> dict[str, Any]:
    ctx = _ctx(request)
    user = _session_user(request, body, "agent_rate_limiter", AGENT_PER_MIN)
    strategy = ctx.db.execute("SELECT preset, next_run_ms FROM strategies WHERE user_id=?", (user.id,)).fetchone()
    chosen = _service(request).tuning_for(user.id)
    run = ctx.db.execute("SELECT preset, started_ms, start_equity FROM strategy_runs WHERE user_id=? AND status='active'", (user.id,)).fetchone()
    decisions = ctx.db.execute(
        "SELECT created_ms, outcome, side, stop_atr, take_profit_atr, reason, code, trigger, snapshot FROM agent_decisions WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user.id, AGENT_DECISIONS),
    ).fetchall()
    link = ctx.db.execute("SELECT display FROM telegram_links WHERE user_id=?", (user.id,)).fetchone()
    view, account = await _account_view(_service(request), user)
    return {
        "ok": True,
        "status": user.status,
        "halted_by_operator": _service(request).kill_switch(),
        "preset": strategy["preset"] if strategy else None,
        # The settings in force, whether the wallet changed any, and the same line it signed.
        "tuning": chosen[1] if chosen else None,
        "custom": bool(chosen) and not tunings.is_default(chosen[0], chosen[1], ctx.settings),
        "settings": tunings.summary(*chosen) if chosen else None,
        "next_run_ms": strategy["next_run_ms"] if strategy else None,
        "now_ms": ctx.clock(),  # the page judges a late decision on this clock, not the viewer's
        "last_checked_ms": decisions[0]["created_ms"] if decisions else None,
        "explorer_tx": _network(ctx)["explorer_tx"],
        "run": dict(run) if run else None,
        **view,
        "performance": _performance(ctx, user.id, None if account is None else account.equity),
        "stats": _run_stats(ctx, user.id, None if account is None else account.equity),
        "memory": _agent_memory(ctx, user.id),
        "decisions": [{**dict(row), "snapshot": _parse_snapshot(row["snapshot"])} for row in decisions],
        "trades": _recent_trades(ctx.db, user.id),
        "market": _market_view(ctx),
        "telegram": {"available": ctx.settings.telegram_configured, "linked": link is not None, "display": link["display"] if link else None},
    }


@router.post("/onboard/agent/close")
@_denied
async def onboard_agent_close(request: Request, body: SessionIn) -> dict[str, Any]:
    user = _session_user(request, body, "agent_close_rate_limiter", AGENT_CLOSE_PER_MIN)
    out = await _service(request).close(user, CloseIn(reason="owner"))
    return out.model_dump(mode="json")


@router.post("/onboard/chat")
@_denied
async def onboard_chat(request: Request, body: ChatIn) -> JSONResponse:
    """One message to the wallet's assistant, from the dashboard's chat panel. The session proves the
    owner, as for the dashboard itself. A reply may carry a change to the settings: it is only a
    proposal, checked against the menus, and takes effect when the wallet signs it at `/onboard/resume`."""
    user = _session_user(request, body, "chat_rate_limiter", CHAT_PER_MIN)
    engine = plugin.engine()
    if engine is None:
        raise _Deny(409, "AGENT_UNAVAILABLE")
    return JSONResponse({"ok": True, **await engine.chat(_service(request), user, body.text)}, headers=NO_STORE)


@router.post("/onboard/chat/history")
@_denied
async def onboard_chat_history(request: Request, body: SessionIn) -> JSONResponse:
    """The end of the conversation so far, so the panel is not empty after a reload. Words only: a
    change proposed earlier is not offered again."""
    ctx = _ctx(request)
    user = _session_user(request, body, "chat_history_rate_limiter", CHAT_HISTORY_PER_MIN)
    engine = plugin.engine()
    if engine is None:
        raise _Deny(409, "AGENT_UNAVAILABLE")
    return JSONResponse({"ok": True, **engine.chat_history(ctx, user.id)}, headers=NO_STORE)


async def _read_candles(ctx: AppContext) -> dict[str, Any]:
    """Never raises: when the chain cannot be read the page draws nothing, and a failure is kept
    for the same 30 s as a result, so a chain that is down is not asked once per poll either."""
    candles: list[list[Any]] = []
    mark: Optional[str] = None
    try:
        rows = await ctx.chain.live_candles_1h(CANDLES_SHOWN)
        candles = [[row.open_ms, str(row.open), str(row.high), str(row.low), str(row.close)] for row in rows[-CANDLES_SHOWN:]]
    except Exception as exc:
        logger.warning("candles for the page could not be read: %s", type(exc).__name__)
    try:
        mark = str((await ctx.chain.snapshot()).mark_price)
    except Exception as exc:
        logger.warning("the mark for the page could not be read: %s", type(exc).__name__)
    return {"ok": True, "period_ms": CANDLES_PERIOD_MS, "candles": candles, "mark": mark, "as_of_ms": ctx.clock(), "source": getattr(ctx.chain, "candle_source", None)}


def _candles_cache(request: Request, ctx: AppContext) -> SharedValue[dict[str, Any]]:
    state = request.app.state
    cache = getattr(state, "candles_cache", None)
    if cache is None:
        cache = state.candles_cache = SharedValue(CANDLES_CACHE_MS, ctx.clock)
    return cache


@router.get("/onboard/candles")
@_denied
async def onboard_candles(request: Request) -> dict[str, Any]:
    """Public market data, the same for everyone. Read once per 30 s, however many pages poll."""
    ctx = _ctx(request)
    _network(ctx)
    if not _limiter(request, "candles_rate_limiter", CANDLES_PER_MIN).allow(_client_key(request, ctx), ctx.clock()):
        raise rejected(ErrorCode.RATE_LIMITED, limit=f"per_min={CANDLES_PER_MIN}")
    return await _candles_cache(request, ctx).get(lambda: _read_candles(ctx))


def _security_headers(ctx: AppContext) -> dict[str, str]:
    info = NETWORKS.get(ctx.settings.network, NETWORKS["testnet"])
    connect = " ".join(("'self'", *info["connect_hosts"]))
    headers = {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            f"form-action 'self'; connect-src {connect}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if ctx.settings.public_base_url.startswith("https://"):
        headers["Strict-Transport-Security"] = "max-age=31536000"  # no includeSubDomains: the operator's other hosts are not ours
    return headers


def page_response(ctx: AppContext, name: str) -> Response:
    path = STATIC_DIR / name
    if not path.is_file():
        return JSONResponse({"ok": False, "code": "PAGE_NOT_BUILT", "limit": None}, status_code=503)
    return FileResponse(path, media_type="text/html", headers=_security_headers(ctx))


@router.get("/", include_in_schema=False)
async def onboarding_page(request: Request) -> Response:
    return page_response(_ctx(request), "index.html")


@router.get("/demo", include_in_schema=False)
async def demo_page(request: Request) -> Response:
    return page_response(_ctx(request), "index.html")


@router.get("/assets/{asset_path:path}", include_in_schema=False)
async def asset(asset_path: str) -> Response:
    root = (STATIC_DIR / "assets").resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return JSONResponse({"ok": False, "code": "NOT_FOUND", "limit": None}, status_code=404)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "public, max-age=31536000, immutable"})
