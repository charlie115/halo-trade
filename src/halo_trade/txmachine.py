"""The sign-once transaction machine (hard rule 6).

A tx is signed once, persisted (bytes, hash, sequence, timeout_height) in a committed DB
transaction, and only then broadcast. On a transport error the SAME bytes are re-sent; a new tx
is built only after the previous one is final. "Unknown" is resolved only by `lookup`:
included means final, and latest height past `timeout_height` without the hash means it never
executes. At most one tx per signer is open at any time.

A process restart resumes from the `txs` table: an open row is rebroadcast (if the policy for
its purpose allows) and resolved exactly as if the process had never died.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterator, Optional, Sequence, TypeVar

from .chain.base import ChainBackend, ChainUnavailable, OrderRequest, RestingOrder, SignedTx, TxStatus
from .config import WINDOW_MS
from .context import AppContext
from .contracts import ErrorCode

logger = logging.getLogger(__name__)

T = TypeVar("T")

OPEN_STATUSES = ("signed", "sent")
FINAL_STATUSES = ("ok", "failed", "expired")
RAW_LOG_MAX = 500
KILL_SWITCH_KEY = "kill_switch"

# A call gets at least this long even with the budget spent, so a freshly signed tx still leaves.
MIN_CALL_S = 0.05
# Protective txs per user. A failing stop/flatten otherwise loops until its deadline, tick after
# tick, burning gas and the one signer every user shares.
RETRY_WINDOW_MS = 60_000
RETRY_LIMIT = 6
# With the grant gone every tx fails, so one attempt per window is all it is worth.
GRANT_BAD_WINDOW_MS = 300_000
GRANT_BAD_LIMIT = 1


def grant_bad_key(user_id: int) -> str:
    return f"grant_bad:{user_id}"


def kill_switch_on(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (KILL_SWITCH_KEY,)).fetchone()
    return row is not None and row["value"] in ("1", "true", "on")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One BEGIN IMMEDIATE transaction. The caller must hold `ctx.db_lock`."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def make_cid(user_id: int, window_id: int, leg: str) -> str:
    """Deterministic client order id from (user, window, leg). For matching only, never dedupe."""
    return hashlib.sha256(f"{user_id}:{window_id}:{leg}".encode()).hexdigest()[:32]


class SignerBusy(Exception):
    """The signing slot could not be obtained in time. Nothing was signed."""


class EntryHalted(SignerBusy):
    """A stop, a revoke or the kill switch landed while this entry waited to sign. Nothing was signed."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class TxRow:
    id: int
    user_id: int
    intent_id: Optional[int]
    purpose: str
    signer: str
    sequence: int
    timeout_height: int
    tx_hash: str
    tx_bytes: bytes
    status: str
    code: Optional[int]
    raw_log: Optional[str]
    height: Optional[int]
    created_ms: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TxRow":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            intent_id=row["intent_id"],
            purpose=row["purpose"],
            signer=row["signer"],
            sequence=row["sequence"],
            timeout_height=row["timeout_height"],
            tx_hash=row["tx_hash"],
            tx_bytes=bytes(row["tx_bytes"]),
            status=row["status"],
            code=row["code"],
            raw_log=row["raw_log"],
            height=row["height"],
            created_ms=row["created_ms"],
        )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def signed(self) -> SignedTx:
        return SignedTx(self.tx_hash, self.tx_bytes, self.signer, self.sequence, self.timeout_height)


class TxMachine:
    def __init__(self, ctx: AppContext, *, poll_interval_s: float = 0.5) -> None:
        self.ctx = ctx
        self.poll_interval_s = poll_interval_s
        # One signing flow at a time. The DB partial unique index is the hard guarantee.
        self._slot = asyncio.Lock()

    @property
    def chain(self) -> ChainBackend:
        return self.ctx.chain

    # ------------------------------------------------------------------ queries
    def get(self, tx_id: int) -> TxRow:
        row = self.ctx.db.execute("SELECT * FROM txs WHERE id=?", (tx_id,)).fetchone()
        return TxRow.from_row(row)

    def open_txs(self) -> list[TxRow]:
        rows = self.ctx.db.execute(
            "SELECT * FROM txs WHERE status IN ('signed','sent') ORDER BY id"
        ).fetchall()
        return [TxRow.from_row(r) for r in rows]

    def open_for_user(self, user_id: int) -> list[TxRow]:
        return [t for t in self.open_txs() if t.user_id == user_id]

    def for_intent(self, intent_id: int, purpose: str) -> list[TxRow]:
        rows = self.ctx.db.execute(
            "SELECT * FROM txs WHERE intent_id=? AND purpose=? ORDER BY id", (intent_id, purpose)
        ).fetchall()
        return [TxRow.from_row(r) for r in rows]

    def latest(self, intent_id: int, purpose: str) -> Optional[TxRow]:
        txs = self.for_intent(intent_id, purpose)
        return txs[-1] if txs else None

    def latest_for_user(self, user_id: int, purpose: str) -> Optional[TxRow]:
        row = self.ctx.db.execute(
            "SELECT * FROM txs WHERE user_id=? AND purpose=? ORDER BY id DESC LIMIT 1", (user_id, purpose)
        ).fetchone()
        return TxRow.from_row(row) if row else None

    def hashes_for_intent(self, intent_id: int) -> list[str]:
        rows = self.ctx.db.execute(
            "SELECT tx_hash FROM txs WHERE intent_id=? ORDER BY id", (intent_id,)
        ).fetchall()
        return [r["tx_hash"] for r in rows]

    # ------------------------------------------------------------------ persistence
    def _insert_row(self, conn: sqlite3.Connection, user_id: int, intent_id: Optional[int], purpose: str, signed: SignedTx, now: int) -> int:
        cursor = conn.execute(
            "INSERT INTO txs (user_id, intent_id, purpose, signer, sequence, timeout_height, tx_hash, tx_bytes, status, created_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?,?,?,'signed',?,?)",
            (user_id, intent_id, purpose, signed.signer, signed.sequence, signed.timeout_height, signed.tx_hash, signed.tx_bytes, now, now),
        )
        return int(cursor.lastrowid)

    async def persist(self, *, user_id: int, intent_id: Optional[int], purpose: str, signed: SignedTx, stage: Optional[str] = None) -> TxRow:
        """Commit the signed tx before any broadcast. The intent's stage moves in the same commit."""
        now = self.ctx.clock()
        async with self.ctx.db_lock:
            with transaction(self.ctx.db) as conn:
                tx_id = self._insert_row(conn, user_id, intent_id, purpose, signed, now)
                if stage is not None and intent_id is not None:
                    conn.execute("UPDATE intents SET stage=?, updated_ms=? WHERE id=?", (stage, now, intent_id))
        return self.get(tx_id)

    async def _update_status(self, tx: TxRow, status: str, *, code: Optional[int] = None, raw_log: Optional[str] = None, height: Optional[int] = None) -> TxRow:
        now = self.ctx.clock()
        async with self.ctx.db_lock:
            with transaction(self.ctx.db) as conn:
                conn.execute(
                    "UPDATE txs SET status=?, code=COALESCE(?,code), raw_log=COALESCE(?,raw_log), height=COALESCE(?,height), updated_ms=? "
                    "WHERE id=? AND status IN ('signed','sent')",
                    (status, code, raw_log[:RAW_LOG_MAX] if raw_log else raw_log, height, now, tx.id),
                )
        return self.get(tx.id)

    # ------------------------------------------------------------------ resolution
    def _may_rebroadcast(self, tx: TxRow) -> bool:
        """Hard rule 5: an entry from a closed window is never re-sent, only resolved. The kill
        switch and a user who is no longer active stop it the same way."""
        if tx.purpose != "entry" or tx.intent_id is None:
            return True
        row = self.ctx.db.execute(
            "SELECT i.window_id, u.status FROM intents i JOIN users u ON u.id=i.user_id WHERE i.id=?", (tx.intent_id,)
        ).fetchone()
        if row is None or row["status"] != "active" or kill_switch_on(self.ctx.db):
            return False
        return row["window_id"] == self.ctx.clock() // WINDOW_MS

    async def resolve(self, tx: TxRow) -> TxRow:
        """Advance one open row. Re-broadcasts only the same bytes, only when not yet known to
        the chain, and only from `signed` (a `sent` row was accepted by a node already)."""
        if not tx.is_open:
            return tx
        try:
            result = await self.chain.lookup(tx.signed)
        except ChainUnavailable:
            return tx
        if result.status is TxStatus.PENDING:
            if tx.status == "signed" and self._may_rebroadcast(tx):
                try:
                    await self.chain.broadcast(tx.signed)
                except ChainUnavailable:
                    return tx
                return await self._update_status(tx, "sent")
            return tx
        status = {TxStatus.OK: "ok", TxStatus.FAILED: "failed", TxStatus.EXPIRED: "expired"}[result.status]
        return await self._update_status(tx, status, code=result.code, raw_log=result.raw_log, height=result.height)

    async def _within(self, awaitable: Awaitable[T], deadline: float) -> T:
        """A chain call that outlives its flow's budget counts as unavailable, so a hung call
        cannot keep the one signer slot past `deadline`."""
        try:
            return await asyncio.wait_for(awaitable, max(MIN_CALL_S, deadline - time.monotonic()))
        except TimeoutError:
            raise ChainUnavailable("chain call exceeded the budget") from None

    async def _broadcast_first(self, tx: TxRow, deadline: float) -> TxRow:
        """The first send. A transport error leaves it `signed` for `resolve` to retry."""
        try:
            await self._within(self.chain.broadcast(tx.signed), deadline)
        except ChainUnavailable:
            return tx
        return await self._update_status(tx, "sent")

    async def wait_final(self, tx: TxRow, deadline: float) -> TxRow:
        """Poll `resolve` until the row is final or the deadline passes."""
        while True:
            try:
                tx = await self._within(self.resolve(tx), deadline)
            except ChainUnavailable:
                return tx
            if not tx.is_open or time.monotonic() >= deadline:
                return tx
            await asyncio.sleep(self.poll_interval_s)

    async def _drain_open(self, deadline: float) -> None:
        """Hard rule 6: no new tx while any tx is open. Raises SignerBusy past the deadline."""
        while True:
            open_rows = self.open_txs()
            if not open_rows:
                return
            for row in open_rows:
                try:
                    await self._within(self.resolve(row), deadline)
                except ChainUnavailable:
                    raise SignerBusy() from None
            if not self.open_txs():
                return
            if time.monotonic() >= deadline:
                raise SignerBusy()
            await asyncio.sleep(self.poll_interval_s)

    def _retry_budget_spent(self, user_id: int) -> bool:
        grant_bad = self.ctx.db.execute("SELECT 1 FROM system_state WHERE key=?", (grant_bad_key(user_id),)).fetchone()
        window, limit = (GRANT_BAD_WINDOW_MS, GRANT_BAD_LIMIT) if grant_bad else (RETRY_WINDOW_MS, RETRY_LIMIT)
        sent = self.ctx.db.execute(
            "SELECT COUNT(*) FROM txs WHERE user_id=? AND purpose<>'entry' AND created_ms>?",
            (user_id, self.ctx.clock() - window),
        ).fetchone()[0]
        return sent >= limit

    async def _acquire_slot(self, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=remaining)
        except TimeoutError:
            raise SignerBusy() from None

    def _refuse_stopped_entry(self, user_id: int) -> None:
        if kill_switch_on(self.ctx.db):
            raise EntryHalted(ErrorCode.PAUSED)
        status = self.ctx.db.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()["status"]
        if status != "active":
            raise EntryHalted(ErrorCode.PAUSED if status == "paused" else ErrorCode.UNAUTHORIZED)

    async def send(
        self,
        *,
        user_id: int,
        intent_id: Optional[int],
        purpose: str,
        granter: str,
        subaccount_id: str,
        orders: Sequence[OrderRequest],
        cancels: Sequence[RestingOrder] = (),
        stage: Optional[str] = None,
        deadline: float,
        limited: bool = True,
    ) -> TxRow:
        """Sign once, persist, broadcast, resolve within `deadline`.

        Raises `SignerBusy` or `ChainUnavailable` only when nothing was signed, so the caller
        knows the tx definitively does not exist. Returns the row otherwise, possibly still open.
        `limited=False` is for the user's own close, which has its own attempt cap.
        """
        if limited and self._retry_budget_spent(user_id):
            raise SignerBusy()
        await self._acquire_slot(deadline)
        try:
            await self._drain_open(deadline)
            if purpose == "entry":
                self._refuse_stopped_entry(user_id)  # the queue for this slot can be long
            signed = await self._within(self.chain.build_and_sign(granter, subaccount_id, orders, cancels), deadline)
            row = await self.persist(user_id=user_id, intent_id=intent_id, purpose=purpose, signed=signed, stage=stage)
            row = await self._broadcast_first(row, deadline)
            return await self.wait_final(row, deadline)
        finally:
            self._slot.release()

    @property
    def slot_busy(self) -> bool:
        return self._slot.locked()

    async def resolve_all_open(self) -> None:
        """Watcher entry point: one resolve pass over open rows, unless a flow owns the slot."""
        if self._slot.locked():
            return
        await self._acquire_slot(time.monotonic() + 0.01)
        try:
            for row in self.open_txs():
                await self.resolve(row)
        finally:
            self._slot.release()

    def unresolved_older_than(self, cutoff_ms: int) -> list[TxRow]:
        rows = self.ctx.db.execute(
            "SELECT * FROM txs WHERE status IN ('signed','sent') AND created_ms<=? ORDER BY id", (cutoff_ms,)
        ).fetchall()
        return [TxRow.from_row(r) for r in rows]


Clock = Callable[[], int]
