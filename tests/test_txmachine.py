"""Hard rule 6, on the tx machine directly."""
import sqlite3
import time
from decimal import Decimal as D

import pytest

from halo_trade.chain.base import OrderKind, OrderRequest, Side
from halo_trade.chain.mock import Failure, SimulatedCrash
from halo_trade.txmachine import SignerBusy, make_cid

ORDER = OrderRequest(OrderKind.MARKET, Side.LONG, D("0.001"), D("70000"), "cid-1", False, margin=D("30"))


def row_status(env):
    return [r["status"] for r in env.txs()]


async def send(env, user, *, budget=1.0, purpose="entry", orders=(ORDER,)):
    return await env.svc.tx.send(
        user_id=user.id, intent_id=None, purpose=purpose,
        granter=user.inj_address, subaccount_id=user.subaccount_id,
        orders=list(orders), deadline=time.monotonic() + budget,
    )


async def test_tx_row_is_committed_before_broadcast(env):
    user, _ = env.add_user()
    env.chain.arm_crash("broadcast", "before")
    with pytest.raises(SimulatedCrash):
        await send(env, user)
    rows = env.txs()
    assert len(rows) == 1
    assert rows[0]["status"] == "signed"
    assert bytes(rows[0]["tx_bytes"])
    assert rows[0]["tx_hash"] and rows[0]["sequence"] and rows[0]["timeout_height"]
    assert env.chain.broadcast_log == []


async def test_happy_path_signs_once_and_finalizes(env):
    user, _ = env.add_user()
    row = await send(env, user)
    assert row.status == "ok"
    assert len(env.chain.signed_log) == 1
    assert env.chain.broadcast_log == [row.tx_hash]


async def test_transport_error_after_inclusion_never_resigns(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.TRANSPORT_AFTER_INCLUSION)
    row = await send(env, user)
    assert row.status == "ok"
    assert len(env.chain.signed_log) == 1


async def test_transport_error_before_inclusion_resends_same_bytes_only(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.TRANSPORT_BEFORE_INCLUSION)
    row = await send(env, user)
    assert row.status == "ok"
    assert len(env.chain.signed_log) == 1
    assert env.chain.broadcast_log == [row.tx_hash, row.tx_hash]


async def test_sent_row_is_never_rebroadcast(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    row = await send(env, user, budget=0.02)
    assert row.status == "sent"
    for _ in range(3):
        row = await env.svc.tx.resolve(row)
    assert env.chain.broadcast_log == [row.tx_hash]


async def test_never_included_expires_by_timeout_height_only(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.NEVER_INCLUDED)
    env.chain.auto_mine = False
    row = await send(env, user, budget=0.02)
    assert row.status == "sent"
    env.chain.advance_block(blocks=env.chain.tx_timeout_blocks)  # height == timeout_height
    row = await env.svc.tx.resolve(row)
    assert row.status == "sent"  # not found, but timeout_height not yet passed
    env.chain.advance_block()
    row = await env.svc.tx.resolve(row)
    assert row.status == "expired"
    assert len(env.chain.signed_log) == 1


async def test_new_tx_only_after_previous_is_final(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.NEVER_INCLUDED)
    env.chain.auto_mine = False
    await send(env, user, budget=0.02)
    with pytest.raises(SignerBusy):
        await send(env, user, budget=0.02)
    assert len(env.chain.signed_log) == 1
    env.chain.advance_block(blocks=env.chain.tx_timeout_blocks + 1)
    env.chain.auto_mine = True
    row = await send(env, user)
    assert row.status == "ok"
    assert len(env.chain.signed_log) == 2


async def test_one_open_tx_per_signer_enforced_by_database(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    row = await send(env, user, budget=0.02)
    assert row.is_open
    conn = env.ctx.db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO txs (user_id, purpose, signer, sequence, timeout_height, tx_hash, tx_bytes, status, created_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?,?,'signed',?,?)",
            (user.id, "entry", row.signer, 99, 999, "OTHERHASH", b"x", 1, 1),
        )


async def test_one_in_flight_per_signer_across_users(env):
    a, _ = env.add_user()
    b, _ = env.add_user()
    env.chain.auto_mine = False
    await send(env, a, budget=0.02)
    with pytest.raises(SignerBusy):
        await send(env, b, budget=0.02)
    assert len(env.chain.signed_log) == 1


async def test_sequence_mismatch_resolved_by_timeout_then_rebuilt(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.SEQUENCE_MISMATCH)
    env.chain.auto_mine = False
    row = await send(env, user, budget=0.02)
    assert row.status == "sent"
    env.chain.advance_block(blocks=env.chain.tx_timeout_blocks + 1)
    row = await env.svc.tx.resolve(row)
    assert row.status == "expired"
    env.chain.auto_mine = True
    fresh = await send(env, user)
    assert fresh.status == "ok"
    assert fresh.tx_hash != row.tx_hash


async def test_build_and_sign_error_means_nothing_exists(env):
    user, _ = env.add_user()
    env.chain.fail_calls("build_and_sign")
    with pytest.raises(Exception) as caught:
        await send(env, user)
    assert caught.type.__name__ == "ChainUnavailable"
    assert env.txs() == []


async def test_lookup_outage_leaves_row_open_never_resigns(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    env.chain.fail_calls("lookup", count=1000)
    row = await send(env, user, budget=0.02)
    assert row.is_open
    assert len(env.chain.signed_log) == 1


async def test_restart_resolves_an_open_row_from_the_database(env):
    user, _ = env.add_user()
    env.chain.arm_crash("broadcast", "before")
    with pytest.raises(SimulatedCrash):
        await send(env, user)
    env.restart()
    await env.svc.tx.resolve_all_open()
    row = env.txs()[0]
    assert row["status"] in ("sent", "ok")
    await env.svc.tx.resolve_all_open()
    assert env.txs()[0]["status"] == "ok"
    assert len(env.chain.signed_log) == 1  # resumed without signing again


async def test_tx_bytes_resent_are_exactly_what_was_persisted(env):
    user, _ = env.add_user()
    env.chain.inject(Failure.TRANSPORT_BEFORE_INCLUSION)
    row = await send(env, user)
    persisted = bytes(env.txs()[0]["tx_bytes"])
    assert persisted == env.chain.signed_log[0].tx_bytes
    assert row.status == "ok"


async def test_resolve_all_open_skips_when_a_flow_owns_the_slot(env):
    user, _ = env.add_user()
    env.chain.auto_mine = False
    await send(env, user, budget=0.02)
    calls_before = len(env.chain.broadcast_log)
    async with env.svc.tx._slot:
        await env.svc.tx.resolve_all_open()
    assert len(env.chain.broadcast_log) == calls_before


async def test_failed_status_persists_code_and_truncates_log(env):
    user, _ = env.add_user()
    # A reduce-only market order with no position fails with code 81.
    bad = OrderRequest(OrderKind.MARKET, Side.SHORT, D("0.001"), D("1"), "c", True)
    row = await send(env, user, orders=(bad,))
    assert row.status == "failed"
    assert row.code == 81
    assert len(row.raw_log or "") <= 500


def test_cid_is_deterministic_from_user_window_leg():
    assert make_cid(1, 100, "entry") == make_cid(1, 100, "entry")
    assert make_cid(1, 100, "entry") != make_cid(2, 100, "entry")
    assert make_cid(1, 100, "entry") != make_cid(1, 101, "entry")
    assert make_cid(1, 100, "entry") != make_cid(1, 100, "stop")
    assert len(make_cid(1, 100, "entry")) <= 36
