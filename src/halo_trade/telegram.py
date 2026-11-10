"""Telegram: the webhook, wallet linking, commands and outbox delivery.

Only `/pause`, `/close` and `/unlink` change anything, and each only reduces what can happen;
`/close` needs a confirm button. A link needs a fresh wallet signature and a button press in the
chat. Everything sent is plain text (no `parse_mode`), so nothing a model or person writes can
become markup. The Bot API puts the token in the URL, so nothing here logs a URL or an exception
message, and httpx's own request log is filtered below.

The webhook path does database and memory work only. `/pause` and `/unlink` are applied there, and a
chat with no wallet costs no queue slot and no write. Everything else waits on a bounded queue.
"""
# No `from __future__ import annotations`: `_denied` wraps the routes, and FastAPI would resolve
# postponed annotations against the wrapper's module, not this one.
import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
import traceback
from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Coroutine, NamedTuple, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import account_view, alerts, notify, plugin
from .chain.base import ChainUnavailable
from .context import AppContext
from .contracts import CloseIn, ErrorCode, Outcome
from .limits import ApiError
from .onboarding import (
    ProofIn,
    SessionIn,
    _check_signature,
    _ctx,
    _denied,
    _Deny,
    _network,
    _require_session,
    _sha256,
    _write_tx,
    eth_to_inj,
    release_telegram_link,
)
from .service import Service, User

logger = logging.getLogger(__name__)

router = APIRouter()

API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_S = 10.0
MAX_SEND_CHARS = 4096
UPDATE_QUEUE_MAX = 256
UPDATE_WORKERS = 4  # a /close can take the whole submit budget; /pause never waits on these
CHAT_WORKERS = 2  # separate from the above: a slow LLM must never delay a command
CHAT_QUEUE_MAX = 32
CHAT_QUEUE_PER_USER = 3
POLL_S = 1.0
SENDS_PER_SECOND = 25
OUTBOX_BATCH = 25
OUTBOX_CHAT_BURST = 5  # per chat per pass, so one busy chat cannot use up a pass
MAX_ATTEMPTS = 8
TRADE_NOTICE_TTL_MS = 24 * 3_600_000
BACKOFF_BASE_MS = 2_000
BACKOFF_MAX_MS = 5 * 60_000
DEFAULT_RETRY_AFTER_S = 5
MAX_RETRY_AFTER_S = 3600
LINK_CODE_TTL_MS = 10 * 60_000
CLOSE_CONFIRM_TTL_MS = 2 * 60_000
MAX_UPDATE_AGE_MS = 5 * 60_000
COMMANDS_PER_MINUTE = 20
STRANGER_REPLY_EVERY_MS = 10 * 60_000
BOUNDED_MAP_MAX = 4096
DIRECT_SEND_MAX = 256
OFFERS_MAX = 1024
DISPLAY_MAX_CHARS = 64
CONFIRM_PREFIX = "close:"
CONNECT_PREFIX = "connect:"
IMMEDIATE_COMMANDS = ("/pause", "/unlink")
PURGE_INTERVAL_MS = 3_600_000
PURGE_AGE_MS = 7 * 86_400_000
DAY_MS = 86_400_000
SUMMARY_NEWS = ("entered", "closed", "rejected", "unknown", "no_decision")
NO_STORE = {"Cache-Control": "no-store"}
TRADE_KINDS = (
    "position_opened",
    "position_closed",
    "position_flattened",
    "stop_hit",
    "take_profit_hit",
    "agent_entered",
    "agent_closed",
    "agent_unknown",
    "agent_close_failed",
    "agent_entry_failed",
    "agent_flattened",
)

NOT_LINKED = "This chat is not connected to a wallet. Open the site, connect your wallet and follow the Telegram link there."
BAD_CODE = "That link is wrong or has expired. Open the site and get a new Telegram link."
OTHER_WALLET = "This chat is already connected to another wallet. Send /unlink here first."
SAME_WALLET = "This chat is already connected to that wallet."
WALLET_TAKEN = "That wallet is already connected to another chat. Disconnect it there or on the site first."
CONNECT_PROMPT = (
    "Connect this chat to wallet {wallet}?\n"
    "Press Connect only if you started this on the site from that wallet. "
    "This chat will be able to see its agent, stop it and close its position."
)
LINKED = "Connected to wallet {wallet}. I will message you here when the agent opens or closes a position. /help lists the commands."
HELP = (
    "/status  The agent, your balance and any position\n"
    "/why  The latest decision and its reason\n"
    "/pause  Stop new decisions. An open position stays with its stop\n"
    "/close  Close the open position at market. I ask you to confirm first\n"
    "/unlink  Disconnect this chat from the wallet\n"
    "/help  This list\n\n"
    "Anything else you type goes to the assistant, which can read your account but cannot change it."
)
UNKNOWN_COMMAND = "I do not know that command. /help lists what I can do."
NO_ASSISTANT = "The assistant is not part of this build. /help lists the commands that work."
TEXT_ONLY = "I can only read text messages. /help lists what I can do."
BUSY = "I am busy right now. Please try again in a minute."
SLOW_DOWN = "Slow down, please: too many messages this minute. /pause and /unlink still work."
SOMETHING_WRONG = "Something went wrong on my side. Please try again in a minute."
PAUSED = "Paused. The agent will make no new decisions. Any open position stays with its on-chain stop. /close closes it now. Start the agent again on the site."
ALREADY_PAUSED = "The agent is already paused. Any open position stays with its on-chain stop. /close closes it now."
CLOSE_PROMPT = "Close your position at market now? This cannot be undone."
CLOSE_EXPIRED = "That button has expired. Send /close again."
CLOSE_CHANGED = "Your position has changed since that button was sent, so I did nothing. Send /close again if you still want to close."
CLOSE_DONE = "Your position is closed."
CLOSE_NO_POSITION = "You have no open position."
CLOSE_FAILED = "The close failed on chain."
CLOSE_NOT_STARTED = "The close could not be started."
CLOSE_UNCONFIRMED = "I could not confirm the close yet. It will be retried automatically."
STATE_UNREADABLE = "I could not read your account just now, so check /status in a minute."
STATE_FLAT = "Your account shows no open position."
STATE_NO_STOP = "Your position is still open and I found no stop on chain."
STATE_STOP = "Your position is still open with its stop at {stop}."
NO_DECISION_YET = "The agent has not made a decision yet."
UNAVAILABLE_TEXT = "could not be read right now"

OUTCOME_WORDS = {
    "hold": "held",
    "entered": "opened a position",
    "closed": "closed the position",
    "rejected": "tried to trade and was refused",
    "skipped": "skipped this turn",
    "no_decision": "made no decision",
    "unknown": "sent an order whose result is not known yet",
}

# Round-robin per chat, trade notices first: `turn` is a row's place within its own chat and tier.
_DUE_SQL = (
    "WITH due AS ("
    " SELECT o.*, COALESCE(o.chat_id, l.chat_id) AS target,"
    f" CASE WHEN o.kind IN ({','.join('?' for _ in TRADE_KINDS)}) THEN 0 ELSE 1 END AS tier"
    " FROM telegram_outbox o LEFT JOIN telegram_links l ON l.user_id=o.user_id"
    " WHERE o.sent_ms IS NULL AND o.dropped_ms IS NULL AND o.next_try_ms<=?"
    "), ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY target, tier ORDER BY id) AS turn FROM due)"
    " SELECT * FROM ranked WHERE turn<=? ORDER BY tier, turn, id LIMIT ?"
)


class _HideTelegramUrls(logging.Filter):
    """httpx logs every request URL at INFO, and a Bot API URL holds the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        return API_BASE not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideTelegramUrls())


def _log_failure(where: str, exc: BaseException) -> None:
    """Exception type and source line only: a message might quote a URL or a person's text."""
    frames = traceback.extract_tb(exc.__traceback__)
    origin = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "?"
    logger.warning("telegram %s failed: %s at %s", where, type(exc).__name__, origin)


# ---------------------------------------------------------------------- memory shared with the worker
@dataclass(frozen=True)
class _CloseConfirm:
    nonce: str
    chat_id: str
    expires_ms: int
    intent_id: Optional[int]


@dataclass(frozen=True)
class _ConnectOffer:
    nonce: str
    code_hash: str
    user_id: int
    wallet: str
    created_ms: int


@dataclass
class _Window:
    minute: int
    count: int = 0
    warned: bool = False


class _Gate:
    """What the webhook and the worker share. Every collection is bounded, so a flood costs a fixed
    amount of memory and strangers cost no queue slot."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue = asyncio.Queue(maxsize=UPDATE_QUEUE_MAX)
        self.direct: deque = deque(maxlen=DIRECT_SEND_MAX)
        self.strangers: OrderedDict[str, int] = OrderedDict()
        self.windows: dict[str, _Window] = {}
        self.offers: OrderedDict[str, _ConnectOffer] = OrderedDict()
        self.confirms: dict[int, _CloseConfirm] = {}

    def answer_stranger(self, chat_id: str, text: str, now: int) -> None:
        last = self.strangers.get(chat_id)
        if last is not None and now - last < STRANGER_REPLY_EVERY_MS:
            return
        self.strangers[chat_id] = now
        self.strangers.move_to_end(chat_id)
        while len(self.strangers) > BOUNDED_MAP_MAX:
            self.strangers.popitem(last=False)
        self.direct.append(("sendMessage", _message_payload(chat_id, text)))

    def allow_message(self, chat_id: str, now: int) -> bool:
        minute = now // 60_000
        window = self.windows.get(chat_id)
        if window is None or window.minute != minute:
            if len(self.windows) >= BOUNDED_MAP_MAX:
                self.windows = {key: value for key, value in self.windows.items() if value.minute == minute}
            window = self.windows[chat_id] = _Window(minute)
        window.count += 1
        return window.count <= COMMANDS_PER_MINUTE

    def first_overflow(self, chat_id: str) -> bool:
        """True once per window, for the one fixed "slow down" reply."""
        window = self.windows[chat_id]
        if window.warned:
            return False
        window.warned = True
        return True

    def add_offer(self, chat_id: str, offer: _ConnectOffer) -> None:
        self.offers[chat_id] = offer
        self.offers.move_to_end(chat_id)
        while len(self.offers) > OFFERS_MAX:
            self.offers.popitem(last=False)


def _gate(ctx: AppContext) -> _Gate:
    """One per app. Kept on the context, the only object both the webhook and the worker reach."""
    gate = getattr(ctx, "telegram_gate", None)
    if gate is None:
        gate = ctx.telegram_gate = _Gate()
    return gate


def _message_of(update: dict[str, Any]) -> Any:
    callback = update.get("callback_query")
    return callback.get("message") if isinstance(callback, dict) else update.get("message")


def _private_chat(update: Any) -> Optional[str]:
    """The chat id of a private message or button press. None for everything else."""
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        return None
    message = _message_of(update)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(chat.get("id"), int):
        return None
    return str(chat["id"])


def _too_old(update: dict[str, Any], now: int) -> bool:
    """A callback carries no time of its own, so a button is as old as the message it sits on."""
    date = _message_of(update).get("date")
    return isinstance(date, int) and now - date * 1000 > MAX_UPDATE_AGE_MS


def _parse_command(text: str) -> tuple[Optional[str], str]:
    if not text.startswith("/"):
        return None, ""
    head, _, rest = text.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def _message_payload(chat_id: str, text: str, reply_markup: Optional[dict[str, Any]] = None, *, silent: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_SEND_CHARS], "disable_web_page_preview": True}
    if silent:
        payload["disable_notification"] = True
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return payload


def _masked_wallet(eth_address: str) -> str:
    return f"{eth_address[:6]}...{eth_address[-4:]}"


def _display_name(sender: Any) -> Optional[str]:
    if not isinstance(sender, dict):
        return None
    username = account_view.clean_text(sender.get("username"))
    name = f"@{username}" if username else account_view.clean_text(sender.get("first_name"))
    return "".join(ch for ch in name if ch.isprintable())[:DISPLAY_MAX_CHARS] or None


def _linked_user(ctx: AppContext, chat_id: str) -> Optional[User]:
    row = ctx.db.execute(
        "SELECT u.* FROM telegram_links l JOIN users u ON u.id=l.user_id WHERE l.chat_id=? AND u.status<>'revoked'", (chat_id,)
    ).fetchone()
    return User.from_row(row) if row else None


async def _queue_outbox(
    ctx: AppContext, user_id: int, kind: str, text: str, reply_markup: dict[str, Any], *, chat_id: Optional[str] = None
) -> None:
    """A message with buttons. `notify.enqueue` carries plain text to the linked chat only."""
    async with _write_tx(ctx) as db:
        db.execute(
            "INSERT INTO telegram_outbox(user_id,kind,text,chat_id,reply_markup,created_ms) VALUES(?,?,?,?,?,?)",
            (user_id, kind, text, chat_id, json.dumps(reply_markup), ctx.clock()),
        )


def _keyboard(label: str, callback_data: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": label, "callback_data": callback_data}]]}


# ---------------------------------------------------------------------- the webhook path
@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request) -> Response:
    ctx = _ctx(request)
    settings = ctx.settings
    received = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not settings.telegram_configured or not hmac.compare_digest(received.encode(), settings.telegram_webhook_secret.encode()):
        return Response(status_code=404)
    try:
        update = await request.json()
    except (ValueError, RecursionError):
        return JSONResponse({"ok": True})
    chat_id = _private_chat(update)
    if chat_id is not None and not _too_old(update, ctx.clock()):
        try:
            await _accept(ctx, update, chat_id)
        except sqlite3.Error as exc:
            _log_failure("webhook", exc)
            return Response(status_code=503)  # the dedupe row rolled back too, so Telegram's redelivery is applied
        except Exception as exc:
            _log_failure("webhook", exc)
    return JSONResponse({"ok": True})


async def _accept(ctx: AppContext, update: dict[str, Any], chat_id: str) -> None:
    gate = _gate(ctx)
    now = ctx.clock()
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        data = account_view.clean_text(callback.get("data"))
        if data.startswith(CONNECT_PREFIX) and await _connect(ctx, gate, callback, chat_id, data[len(CONNECT_PREFIX):], now):
            return
    user = _linked_user(ctx, chat_id)
    if user is None:
        await _from_stranger(ctx, gate, update, chat_id, now)
    else:
        await _from_linked(ctx, gate, update, chat_id, user, now)


async def _from_stranger(ctx: AppContext, gate: _Gate, update: dict[str, Any], chat_id: str, now: int) -> None:
    """Memory and one read only. The queue and the database belong to wallets."""
    command, code = None, ""
    if not isinstance(update.get("callback_query"), dict):
        command, code = _parse_command(account_view.clean_text(update["message"].get("text")))
    if command == "/start" and code:
        if not await _offer_connect(ctx, gate, chat_id, code, now):
            gate.answer_stranger(chat_id, BAD_CODE, now)
        return
    gate.answer_stranger(chat_id, NOT_LINKED, now)


async def _from_linked(ctx: AppContext, gate: _Gate, update: dict[str, Any], chat_id: str, user: User, now: int) -> None:
    callback = update.get("callback_query")
    text = "" if isinstance(callback, dict) else account_view.clean_text(update["message"].get("text"))
    command, argument = _parse_command(text)
    within_limit = gate.allow_message(chat_id, now)
    if command in IMMEDIATE_COMMANDS:
        await _apply_now(ctx, gate, update, user, command, quiet=not within_limit)
    elif (within_limit or _is_live_confirm(gate, callback, user, now)) and await _first_seen(ctx, update["update_id"]):
        if command == "/start" and argument:
            if not await _offer_connect(ctx, gate, chat_id, argument, now):
                await notify.enqueue(ctx, user.id, "reply", BAD_CODE)
        else:
            await _enqueue_update(ctx, gate, update, user)
    if not within_limit and gate.first_overflow(chat_id):
        await notify.enqueue(ctx, user.id, "reply", SLOW_DOWN)


def _is_live_confirm(gate: _Gate, callback: Any, user: User, now: int) -> bool:
    """A live /close button still works for someone over the message limit."""
    if not isinstance(callback, dict):
        return False
    pending = gate.confirms.get(user.id)
    return pending is not None and pending.expires_ms > now and callback.get("data") == CONFIRM_PREFIX + pending.nonce


async def _first_seen(ctx: AppContext, update_id: int) -> bool:
    async with _write_tx(ctx) as db:
        cursor = db.execute("INSERT OR IGNORE INTO telegram_seen_updates(update_id,created_ms) VALUES(?,?)", (update_id, ctx.clock()))
    return cursor.rowcount == 1


async def _enqueue_update(ctx: AppContext, gate: _Gate, update: dict[str, Any], user: User) -> None:
    try:
        gate.updates.put_nowait(update)
    except asyncio.QueueFull:
        logger.warning("telegram update queue is full, dropping an update")
        await notify.enqueue(ctx, user.id, "reply", BUSY)


async def _apply_now(ctx: AppContext, gate: _Gate, update: dict[str, Any], user: User, command: str, *, quiet: bool) -> None:
    """Dedupe and the change in one transaction, never behind a queue."""
    now = ctx.clock()
    reply: Optional[str] = None
    async with _write_tx(ctx) as db:
        seen = db.execute("INSERT OR IGNORE INTO telegram_seen_updates(update_id,created_ms) VALUES(?,?)", (update["update_id"], now))
        if seen.rowcount != 1:
            return
        if command == "/pause":
            reply = _pause_wallet(db, user.id, now)
        else:
            release_telegram_link(db, user.id, now)  # queues the one line telling this chat
            gate.confirms.pop(user.id, None)
    if reply is not None and not quiet:
        await notify.enqueue(ctx, user.id, "reply", reply)


def _pause_wallet(db: Any, user_id: int, now: int) -> str:
    """Database only. End equity is the last snapshot, because the chain may be down."""
    changed = db.execute("UPDATE users SET status='paused' WHERE id=? AND status='active'", (user_id,)).rowcount == 1
    run = db.execute("SELECT id FROM strategy_runs WHERE user_id=? AND status='active'", (user_id,)).fetchone()
    if run is not None:
        snapshot = db.execute("SELECT equity FROM equity_snapshots WHERE user_id=? ORDER BY ts_ms DESC LIMIT 1", (user_id,)).fetchone()
        db.execute(
            "UPDATE strategy_runs SET status='stopped', stop_reason='telegram', stopped_ms=?, end_equity=? WHERE id=?",
            (now, snapshot["equity"] if snapshot else None, run["id"]),
        )
    return PAUSED if changed else ALREADY_PAUSED


# ---------------------------------------------------------------------- linking
async def _offer_connect(ctx: AppContext, gate: _Gate, chat_id: str, code: str, now: int) -> bool:
    """Answer `/start <code>`. This never links: only the Connect button does. False for a bad code."""
    digest = _sha256(code)
    row = ctx.db.execute(
        "SELECT c.user_id, u.status, u.eth_address FROM telegram_link_codes c JOIN users u ON u.id=c.user_id WHERE c.code_hash=? AND c.expires_ms>?",
        (digest, now),
    ).fetchone()
    if row is None or row["status"] == "revoked":
        return False
    holder = ctx.db.execute("SELECT user_id FROM telegram_links WHERE chat_id=?", (chat_id,)).fetchone()
    if holder is not None:
        await notify.enqueue(ctx, holder["user_id"], "reply", SAME_WALLET if holder["user_id"] == row["user_id"] else OTHER_WALLET)
        return True
    live = gate.offers.get(chat_id)
    if live is not None and live.code_hash == digest and now - live.created_ms < MAX_UPDATE_AGE_MS:
        return True  # that prompt's button still works; no second one
    wallet = _masked_wallet(row["eth_address"])
    nonce = secrets.token_urlsafe(16)
    gate.add_offer(chat_id, _ConnectOffer(nonce, digest, row["user_id"], wallet, now))
    await _queue_outbox(ctx, row["user_id"], "connect_prompt", CONNECT_PROMPT.format(wallet=wallet), _keyboard("Connect", CONNECT_PREFIX + nonce), chat_id=chat_id)
    return True


async def _connect(ctx: AppContext, gate: _Gate, callback: dict[str, Any], chat_id: str, nonce: str, now: int) -> bool:
    """The only place a link is made. False when this press matches no offer made to this chat."""
    offer = gate.offers.get(chat_id)
    if offer is None or not hmac.compare_digest(offer.nonce.encode(), nonce.encode()):
        return False
    del gate.offers[chat_id]
    gate.direct.append(("answerCallbackQuery", {"callback_query_id": str(callback.get("id", ""))}))
    async with _write_tx(ctx) as db:
        refusal = _make_link(db, offer, chat_id, _display_name(callback.get("from")), now)
    if refusal is None:
        await notify.enqueue(ctx, offer.user_id, "reply", LINKED.format(wallet=offer.wallet))
    else:
        gate.direct.append(("sendMessage", _message_payload(chat_id, refusal)))
    return True


def _make_link(db: Any, offer: _ConnectOffer, chat_id: str, display: Optional[str], now: int) -> Optional[str]:
    """Why this press cannot link, or None once it has. The code is spent only by a real link."""
    code = db.execute(
        "SELECT u.status FROM telegram_link_codes c JOIN users u ON u.id=c.user_id WHERE c.code_hash=? AND c.expires_ms>?", (offer.code_hash, now)
    ).fetchone()
    if code is None or code["status"] == "revoked":
        return BAD_CODE
    holder = db.execute("SELECT user_id FROM telegram_links WHERE chat_id=?", (chat_id,)).fetchone()
    if holder is not None and holder["user_id"] != offer.user_id:
        return OTHER_WALLET
    if holder is None and db.execute("SELECT 1 FROM telegram_links WHERE user_id=?", (offer.user_id,)).fetchone() is not None:
        return WALLET_TAKEN
    db.execute("DELETE FROM telegram_link_codes WHERE user_id=?", (offer.user_id,))
    db.execute(
        "INSERT INTO telegram_links(user_id,chat_id,linked_ms,display) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET linked_ms=excluded.linked_ms, display=excluded.display",
        (offer.user_id, chat_id, now, display),
    )
    return None


def _require_bot(ctx: AppContext) -> None:
    if not ctx.settings.telegram_configured:
        raise _Deny(409, "TELEGRAM_UNAVAILABLE")


@router.post("/onboard/telegram/link")
@_denied
async def telegram_link(request: Request, body: ProofIn) -> JSONResponse:
    """A fresh signature, not a session: this hands a chat the power to see and stop the agent."""
    ctx = _ctx(request)
    _require_bot(ctx)
    _network(ctx)
    eth = body.eth_address
    inj = eth_to_inj(eth)
    now = ctx.clock()
    code = secrets.token_urlsafe(24)
    expires_ms = now + LINK_CODE_TTL_MS
    async with _write_tx(ctx) as db:
        _check_signature(ctx, "telegram", eth, inj, body.signature, now)
        user = db.execute("SELECT id, status FROM users WHERE inj_address=?", (inj,)).fetchone()
        if user is None:
            raise _Deny(404, "USER_NOT_FOUND")
        if user["status"] == "revoked":
            raise _Deny(409, "BAD_STATE", limit="status=revoked")
        if db.execute("SELECT 1 FROM telegram_links WHERE user_id=?", (user["id"],)).fetchone() is not None:
            raise _Deny(409, "TELEGRAM_ALREADY_LINKED")
        db.execute("DELETE FROM onboard_challenges WHERE inj_address=? AND action='telegram'", (inj,))
        db.execute("DELETE FROM telegram_link_codes WHERE user_id=? OR expires_ms<=?", (user["id"], now))
        db.execute("INSERT INTO telegram_link_codes(code_hash,user_id,expires_ms) VALUES(?,?,?)", (_sha256(code), user["id"], expires_ms))
    url = f"https://t.me/{ctx.settings.telegram_bot_username}?start={code}"
    return JSONResponse({"ok": True, "url": url, "expires_ms": expires_ms}, headers=NO_STORE)


@router.post("/onboard/telegram/unlink")
@_denied
async def telegram_unlink(request: Request, body: SessionIn) -> JSONResponse:
    ctx = _ctx(request)
    _require_bot(ctx)
    now = ctx.clock()
    inj = eth_to_inj(body.eth_address)
    _require_session(ctx, inj, body.session, now)
    row = ctx.db.execute("SELECT id, status FROM users WHERE inj_address=?", (inj,)).fetchone()
    if row is None:
        raise _Deny(404, "USER_NOT_FOUND")
    if row["status"] == "revoked":
        raise _Deny(409, "BAD_STATE", limit="status=revoked")
    async with _write_tx(ctx) as db:
        release_telegram_link(db, row["id"], now)
    _gate(ctx).confirms.pop(row["id"], None)
    return JSONResponse({"ok": True, "linked": False}, headers=NO_STORE)


# ---------------------------------------------------------------------- text
def _usd(value: Any) -> str:
    return UNAVAILABLE_TEXT if value == account_view.UNAVAILABLE else f"${value:,.2f}"


def _number(value: float) -> str:
    return format(value, ".10g")


def _signed_usd(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _position_text(position: Any) -> str:
    if position == account_view.UNAVAILABLE:
        return UNAVAILABLE_TEXT
    if position is None:
        return "none"
    stop = "no stop on chain" if position["stop_price"] is None else f"stop {_number(position['stop_price'])}"
    take = "" if position["take_profit_price"] is None else f", take-profit {_number(position['take_profit_price'])}"
    return (
        f"{position['side']} {_number(position['quantity'])} at {_number(position['entry_price'])}, "
        f"unrealized P&L {_signed_usd(position['unrealized_pnl_usd'])}, {stop}{take}"
    )


def _status_text(view: dict[str, Any]) -> str:
    agent = "running" if view["agent"] == "active" else view["agent"]
    lines = [f"Agent: {agent}" + (f" ({view['preset']})" if view["preset"] else "")]
    if view["halted_by_operator"]:
        lines.append("Trading is paused by the operator. /close still works.")
    lines.append(f"Equity: {_usd(view['equity_usd'])}")
    lines.append(f"Position: {_position_text(view['position'])}")
    if view["next_decision_in_s"] is not None:
        lines.append(f"Next decision in about {max(1, view['next_decision_in_s'] // 60)} min.")
    return "\n".join(lines)


def _ago(elapsed_ms: int) -> str:
    minutes = max(0, elapsed_ms) // 60_000
    if minutes == 0:
        return "just now"
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def _why_text(row: Any, now: int) -> str:
    detail = [row["side"]] if row["side"] else []
    if row["stop_atr"]:
        detail.append(f"stop {row['stop_atr']} ATR")
    if row["take_profit_atr"]:
        detail.append(f"take-profit {row['take_profit_atr']} ATR")
    if row["code"]:
        detail.append(f"code {row['code']}")
    suffix = f" ({', '.join(detail)})" if detail else ""
    text = f"Latest decision, {_ago(now - row['created_ms'])}: {OUTCOME_WORDS.get(row['outcome'], row['outcome'])}{suffix}."
    return f"{text}\nReason: {row['reason']}" if row["reason"] else text


# ---------------------------------------------------------------------- worker
class _Sent(NamedTuple):
    ok: bool
    retry_after_s: Optional[int] = None
    paced: bool = False
    held: bool = False


def _past_its_time(row: Any, attempts: int, now: int) -> bool:
    if row["kind"] in TRADE_KINDS:
        return now - row["created_ms"] >= TRADE_NOTICE_TTL_MS
    return attempts >= MAX_ATTEMPTS


class TelegramWorker:
    """Consumes queued updates and delivers the outbox. Trading never depends on it."""

    def __init__(self, service: Service, *, http: Optional[httpx.AsyncClient] = None) -> None:
        self.service = service
        self.ctx = service.ctx
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=SEND_TIMEOUT_S, follow_redirects=False)
        self._gate = _gate(self.ctx)
        self._chat_ready: asyncio.Queue = asyncio.Queue()  # user ids with a message waiting, round-robin
        self._chat_waiting: dict[int, deque] = {}  # one user's messages: answered one at a time
        self._holds: dict[str, int] = {}  # chat id -> no send before this time, from a 429
        self._pace_second = 0
        self._pace_sent = 0
        self._next_purge_ms = 0
        self._summary_day: Optional[int] = None
        self._workers: list[asyncio.Task] = []

    async def run(self, stop: asyncio.Event) -> None:
        if not self.ctx.settings.telegram_configured:
            return
        makers: list[Callable[[], Coroutine[Any, Any, None]]] = [self._serve_updates] * UPDATE_WORKERS + [self._serve_chat] * CHAT_WORKERS
        self._workers = [asyncio.create_task(make()) for make in makers]
        try:
            while not stop.is_set():
                self._restart_dead_workers(makers)
                try:
                    await self._outbox_tick()
                except Exception as exc:
                    _log_failure("outbox", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_S)
                except TimeoutError:
                    pass
        finally:
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            if self._owns_http:
                await self._http.aclose()

    def _restart_dead_workers(self, makers: list[Callable[[], Coroutine[Any, Any, None]]]) -> None:
        for index, task in enumerate(self._workers):
            if task.done():
                if not task.cancelled() and task.exception() is not None:
                    _log_failure("worker", task.exception())
                self._workers[index] = asyncio.create_task(makers[index]())

    async def flush(self) -> int:
        """Handle everything queued, then one outbox pass. Returns the messages delivered."""
        if not self.ctx.settings.telegram_configured:
            return 0
        while not self._gate.updates.empty():
            await self._process(self._gate.updates.get_nowait())
        while not self._chat_ready.empty():
            await self._answer_next(self._chat_ready.get_nowait())
        return await self._outbox_tick()

    async def _serve_updates(self) -> None:
        while True:
            await self._process(await self._gate.updates.get())

    async def _serve_chat(self) -> None:
        while True:
            await self._answer_next(await self._chat_ready.get())

    # ------------------------------------------------------------------ queued updates
    async def _process(self, update: dict[str, Any]) -> None:
        """One queued update. Never raises: nothing a message does can end a worker."""
        chat_id = _private_chat(update)
        if chat_id is None:
            return
        try:
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                await self._on_button(callback, chat_id)
            else:
                await self._on_message(update["message"], chat_id)
        except Exception as exc:
            _log_failure("update", exc)
            await self._send(chat_id, SOMETHING_WRONG)

    async def _on_message(self, message: dict[str, Any], chat_id: str) -> None:
        user = _linked_user(self.ctx, chat_id)
        if user is None:
            return  # unlinked while this waited
        text = account_view.clean_text(message.get("text"))
        command, _argument = _parse_command(text)
        if command is None:
            await self._queue_chat(user, text)
        elif command == "/close":
            await self._ask_close(user, chat_id)
        else:
            await self._reply(user.id, await self._command_text(user, command))

    async def _command_text(self, user: User, command: str) -> str:
        if command in ("/start", "/help"):
            return HELP
        if command == "/status":
            return _status_text(await account_view.read_status(self.service, user))
        if command == "/why":
            return self._why(user)
        return UNKNOWN_COMMAND

    def _why(self, user: User) -> str:
        row = self.ctx.db.execute(
            "SELECT created_ms, outcome, side, stop_atr, take_profit_atr, reason, code FROM agent_decisions "
            "WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user.id,),
        ).fetchone()
        return NO_DECISION_YET if row is None else _why_text(row, self.ctx.clock())

    # ------------------------------------------------------------------ /close
    async def _ask_close(self, user: User, chat_id: str) -> None:
        nonce = secrets.token_urlsafe(16)
        intent = self.service.open_intent(user.id)
        self._gate.confirms[user.id] = _CloseConfirm(nonce, chat_id, self.ctx.clock() + CLOSE_CONFIRM_TTL_MS, intent.id if intent else None)
        await _queue_outbox(self.ctx, user.id, "close_prompt", CLOSE_PROMPT, _keyboard("Yes, close it", CONFIRM_PREFIX + nonce))

    async def _on_button(self, callback: dict[str, Any], chat_id: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": str(callback.get("id", ""))})
        user = _linked_user(self.ctx, chat_id)
        data = account_view.clean_text(callback.get("data"))
        if user is None or not data.startswith(CONFIRM_PREFIX):
            return
        refusal = self._redeem_confirm(user.id, chat_id, data[len(CONFIRM_PREFIX):])
        await self._reply(user.id, refusal if refusal is not None else await self._close(user))

    def _redeem_confirm(self, user_id: int, chat_id: str, nonce: str) -> Optional[str]:
        """None when this press may close. Good from the chat it was sent to, once, for the same
        position. A wrong press from elsewhere leaves the real one valid."""
        pending = self._gate.confirms.get(user_id)
        if pending is None or pending.chat_id != chat_id or not hmac.compare_digest(pending.nonce.encode(), nonce.encode()):
            return CLOSE_EXPIRED
        del self._gate.confirms[user_id]
        if pending.expires_ms <= self.ctx.clock():
            return CLOSE_EXPIRED
        intent = self.service.open_intent(user_id)
        return None if (intent.id if intent else None) == pending.intent_id else CLOSE_CHANGED

    async def _close(self, user: User) -> str:
        """Says only what is known. Anything short of a confirmed close reports a fresh read."""
        try:
            out = await self.service.close(user, CloseIn(reason="telegram"))
        except ApiError as err:
            if err.out.code is ErrorCode.NO_POSITION:
                return CLOSE_NO_POSITION
            return f"{CLOSE_NOT_STARTED} {await self._position_state(user)}"
        except ChainUnavailable:
            return f"{CLOSE_UNCONFIRMED} {await self._position_state(user)}"
        account_view.forget_account(self.service, user.id)
        if out.outcome is Outcome.CLOSED:
            return CLOSE_DONE
        lead = CLOSE_FAILED if out.outcome is Outcome.FAILED else CLOSE_UNCONFIRMED
        return f"{lead} {await self._position_state(user)}"

    async def _position_state(self, user: User) -> str:
        account = await account_view.read_account(self.service, user, fresh=True)
        position = account_view.position_view(self.service, user, account)
        if position == account_view.UNAVAILABLE:
            return STATE_UNREADABLE
        if position is None:
            return STATE_FLAT
        return STATE_NO_STOP if position["stop_price"] is None else STATE_STOP.format(stop=_number(position["stop_price"]))

    # ------------------------------------------------------------------ free text
    async def _queue_chat(self, user: User, text: str) -> None:
        if not text:
            await self._reply(user.id, TEXT_ONLY)
            return
        waiting = self._chat_waiting.get(user.id)
        queued = sum(len(messages) for messages in self._chat_waiting.values())
        if queued >= CHAT_QUEUE_MAX or (waiting is not None and len(waiting) >= CHAT_QUEUE_PER_USER):
            await self._reply(user.id, BUSY)
        elif waiting is None:
            self._chat_waiting[user.id] = deque([(user, text)])
            self._chat_ready.put_nowait(user.id)
        else:
            waiting.append((user, text))

    async def _answer_next(self, user_id: int) -> None:
        """One message for this user, then back of the line, so one wallet cannot hold the workers."""
        waiting = self._chat_waiting.get(user_id)
        if not waiting:
            self._chat_waiting.pop(user_id, None)
            return
        await self._answer_chat(*waiting.popleft())
        if waiting:
            self._chat_ready.put_nowait(user_id)
        else:
            self._chat_waiting.pop(user_id, None)

    async def _answer_chat(self, user: User, text: str) -> None:
        """Never raises, and a failure here touches nothing but the reply."""
        engine = plugin.engine()
        if engine is None:
            reply = NO_ASSISTANT
        else:
            try:
                reply = await engine.telegram_answer(self.service, user, text)
            except Exception as exc:
                _log_failure("chat", exc)
                reply = engine.TELEGRAM_FAILED
        try:
            await self._reply(user.id, reply)
        except Exception as exc:
            _log_failure("reply", exc)

    # ------------------------------------------------------------------ sending
    async def _reply(self, user_id: int, text: str) -> None:
        """To a linked user, through the outbox: same ordering, retries and rate limit as a push."""
        await notify.enqueue(self.ctx, user_id, "reply", text)

    async def _send(self, chat_id: str, text: str, *, reply_markup: Optional[dict[str, Any]] = None, silent: bool = True) -> _Sent:
        return await self._call("sendMessage", _message_payload(chat_id, text, reply_markup, silent=silent))

    def _take_send_slot(self, now: int) -> bool:
        second = now // 1000
        if second != self._pace_second:
            self._pace_second, self._pace_sent = second, 0
        if self._pace_sent >= SENDS_PER_SECOND:
            return False
        self._pace_sent += 1
        return True

    def _hold_chat(self, chat_id: str, until_ms: int, now: int) -> None:
        if len(self._holds) >= BOUNDED_MAP_MAX:
            self._holds = {key: value for key, value in self._holds.items() if value > now}
        self._holds[chat_id] = until_ms

    async def _call(self, method: str, payload: dict[str, Any]) -> _Sent:
        """Every Bot API call goes through here. Never raises; logs the method and a status only."""
        now = self.ctx.clock()
        chat_id = payload.get("chat_id")
        if chat_id is not None and self._holds.get(chat_id, 0) > now:
            return _Sent(False, held=True)
        if not self._take_send_slot(now):
            return _Sent(False, paced=True)
        try:
            response = await self._http.post(f"{API_BASE}/bot{self.ctx.settings.telegram_bot_token}/{method}", json=payload)
        except Exception as exc:
            logger.warning("telegram %s failed: %s", method, type(exc).__name__)
            return _Sent(False)
        if response.status_code == 429:
            retry_after_s = _retry_after_s(response)
            if chat_id is not None:
                self._hold_chat(chat_id, now + retry_after_s * 1000, now)
            logger.warning("telegram %s rate limited for %ss", method, retry_after_s)
            return _Sent(False, retry_after_s)
        if response.status_code != 200:
            logger.warning("telegram %s http %s", method, response.status_code)
            return _Sent(False)
        return _Sent(True)

    # ------------------------------------------------------------------ outbox
    async def _outbox_tick(self) -> int:
        sent = await self._deliver_due()
        await self._drain_direct()
        await self._purge_old_rows()
        await self._queue_daily_summaries()
        return sent

    async def _drain_direct(self) -> None:
        """One-off sends with no database row. After the outbox, so they never take a notice's slot."""
        direct = self._gate.direct
        while direct:
            method, payload = direct.popleft()
            if (await self._call(method, payload)).paced:
                direct.appendleft((method, payload))
                return

    async def _deliver_due(self) -> int:
        now = self.ctx.clock()
        rows = self.ctx.db.execute(_DUE_SQL, (*TRADE_KINDS, now, OUTBOX_CHAT_BURST, OUTBOX_BATCH)).fetchall()
        delivered = 0
        for row in rows:
            try:
                outcome = await self._deliver_row(row, now)
            except Exception as exc:
                _log_failure("outbox row", exc)
                await self._drop(row, row["attempts"], now)
                continue
            if outcome is None:
                break
            delivered += outcome
        return delivered

    def _current_target(self, row: Any) -> Optional[str]:
        """Read again for every send: the wallet may have unlinked since this pass was selected."""
        if row["chat_id"] is not None:
            return row["chat_id"]
        link = self.ctx.db.execute("SELECT chat_id FROM telegram_links WHERE user_id=?", (row["user_id"],)).fetchone()
        return link["chat_id"] if link is not None and link["chat_id"] == row["target"] else None

    async def _deliver_row(self, row: Any, now: int) -> Optional[int]:
        """1 if delivered, 0 if not, None when this second's send budget is spent."""
        target = self._current_target(row)
        if target is None:
            await self._drop(row, row["attempts"], now, alert=False)
            return 0
        if self._holds.get(target, 0) > now:
            return 0
        if _past_its_time(row, row["attempts"], now):
            await self._drop(row, row["attempts"], now)
            return 0
        markup = json.loads(row["reply_markup"]) if row["reply_markup"] else None
        result = await self._send(target, row["text"], reply_markup=markup, silent=notify.is_routine(row["kind"]))
        if result.paced:
            return None
        if result.held:
            return 0
        if result.ok:
            await self._update_row(row["id"], row["attempts"] + 1, now, now)
            return 1
        if result.retry_after_s is not None:  # Telegram's limit, not a failed attempt
            await self._update_row(row["id"], row["attempts"], now + result.retry_after_s * 1000, None)
            return 0
        attempts = row["attempts"] + 1
        if _past_its_time(row, attempts, now):
            await self._drop(row, attempts, now)
        else:
            await self._update_row(row["id"], attempts, now + _backoff_ms(attempts), None)
        return 0

    async def _update_row(self, row_id: int, attempts: int, next_try_ms: int, sent_ms: Optional[int]) -> None:
        async with _write_tx(self.ctx) as db:
            db.execute("UPDATE telegram_outbox SET attempts=?, next_try_ms=?, sent_ms=? WHERE id=?", (attempts, next_try_ms, sent_ms, row_id))

    async def _drop(self, row: Any, attempts: int, now: int, *, alert: bool = True) -> None:
        async with _write_tx(self.ctx) as db:
            db.execute("UPDATE telegram_outbox SET attempts=?, dropped_ms=? WHERE id=?", (attempts, now, row["id"]))
        logger.warning("telegram outbox message %s (%s) dropped after %s attempts", row["id"], row["kind"], attempts)
        if alert and row["kind"] in TRADE_KINDS:
            await alerts.raise_alert(
                self.ctx, level="warning", kind="telegram_notice_dropped", user_id=row["user_id"],
                detail=f"kind={row['kind']} outbox={row['id']}", dedupe_key=f"telegram_dropped:{row['id']}",
            )

    async def _queue_daily_summaries(self) -> None:
        """Yesterday, once, for each linked wallet something happened to. The dedupe key holds across restarts."""
        today = self.ctx.clock() // DAY_MS
        if today == self._summary_day:
            return
        self._summary_day = today
        rows = self.ctx.db.execute("SELECT l.user_id FROM telegram_links l JOIN users u ON u.id=l.user_id WHERE u.status<>'revoked'").fetchall()
        for row in rows:
            text = _daily_summary(self.ctx.db, row["user_id"], (today - 1) * DAY_MS, today * DAY_MS)
            if text is not None:
                await notify.enqueue(self.ctx, row["user_id"], "daily_summary", text, dedupe_key=f"daily_summary:{row['user_id']}:{today - 1}")

    async def _purge_old_rows(self) -> None:
        now = self.ctx.clock()
        if now < self._next_purge_ms:
            return
        self._next_purge_ms = now + PURGE_INTERVAL_MS
        cutoff = now - PURGE_AGE_MS
        async with _write_tx(self.ctx) as db:
            db.execute("DELETE FROM chat_turns WHERE created_ms<?", (cutoff,))
            db.execute("DELETE FROM telegram_seen_updates WHERE created_ms<?", (cutoff,))
            db.execute("DELETE FROM telegram_link_codes WHERE expires_ms<=?", (now,))
            db.execute("DELETE FROM telegram_outbox WHERE created_ms<?", (cutoff,))


def _daily_summary(db: Any, user_id: int, start_ms: int, end_ms: int) -> Optional[str]:
    """Codes and counts only. None when all the agent did was look: holds are not news."""
    decisions = db.execute(
        "SELECT outcome FROM agent_decisions WHERE user_id=? AND created_ms>=? AND created_ms<?", (user_id, start_ms, end_ms)
    ).fetchall()
    trades = db.execute(
        "SELECT pnl_usd FROM trades WHERE user_id=? AND closed_ms IS NOT NULL AND closed_ms>=? AND closed_ms<?", (user_id, start_ms, end_ms)
    ).fetchall()
    outcomes = [row["outcome"] for row in decisions]
    if not trades and not any(outcome in SUMMARY_NEWS for outcome in outcomes):
        return None
    failed = outcomes.count("no_decision")
    checks = len(outcomes) - failed
    parts = [f"Yesterday (UTC): {checks} check{'' if checks == 1 else 's'}, {len(trades)} trade{'' if len(trades) == 1 else 's'} closed"]
    if trades:
        known = [row["pnl_usd"] for row in trades]
        parts.append("result unavailable" if any(value is None for value in known) else f"about {_signed_usd(sum(Decimal(value) for value in known))}")
    if failed:
        parts.append(f"{failed} check{'' if failed == 1 else 's'} failed")
    return ", ".join(parts) + "."


def _retry_after_s(response: httpx.Response) -> int:
    try:
        value = int(response.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return DEFAULT_RETRY_AFTER_S
    return min(max(value, 1), MAX_RETRY_AFTER_S)


def _backoff_ms(attempts: int) -> int:
    return min(BACKOFF_BASE_MS * 2 ** (attempts - 1), BACKOFF_MAX_MS)
