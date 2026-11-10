"""Hard rules that are about the code as a whole."""
import ast
import inspect
from decimal import Decimal as D
from pathlib import Path

import pytest

import halo_trade
from halo_trade import limits, sizing
from halo_trade.chain.mock import Failure
from halo_trade.contracts import ErrorCode as E, Outcome, PreviewIn
from halo_trade.limits import ApiError
from halo_trade.txmachine import make_cid

SRC = Path(halo_trade.__file__).parent


def _tree(name: str) -> ast.Module:
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", ["sizing.py", "limits.py", "features.py"])
def test_money_modules_have_no_float_arithmetic(module):
    tree = _tree(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            raise AssertionError(f"float literal {node.value} in {module}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
            # Allowed only where the limits module compares a Decimal against the float menu.
            assert module == "limits.py", f"float() call in {module}"


@pytest.mark.parametrize("module", ["features.py", "sizing.py", "limits.py", "txmachine.py", "service.py", "watcher.py", "auth.py", "api.py"])
def test_core_makes_no_third_party_network_calls(module):
    tree = _tree(module)
    banned = {"requests", "httpx", "aiohttp", "urllib", "socket", "grpc"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [n.name.split(".")[0] for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        assert not (set(names) & banned), f"{module} imports {names}"


def test_only_alerts_may_import_httpx_in_the_core():
    tree = _tree("alerts.py")
    assert any(isinstance(n, ast.Import) and n.names[0].name == "httpx" for n in ast.walk(tree))


def test_no_wall_clock_in_pure_logic():
    for module in ("features.py", "sizing.py", "limits.py"):
        src = (SRC / module).read_text(encoding="utf-8")
        assert "time.time" not in src
        assert "datetime.now" not in src


def test_sizing_module_is_pure_of_io():
    for name, obj in inspect.getmembers(sizing, inspect.isfunction):
        if obj.__module__ == "halo_trade.sizing":
            assert not inspect.iscoroutinefunction(obj)
    for name, obj in inspect.getmembers(limits, inspect.isfunction):
        if obj.__module__ == "halo_trade.limits":
            assert not inspect.iscoroutinefunction(obj)


def test_quantity_and_prices_are_quantized_with_decimal_rounding():
    assert sizing.round_quantity(D("0.123456"), D("0.0001")) == D("0.1234")
    assert isinstance(sizing.round_quantity(D("1"), D("0.1")), D)


# ---------------------------------------------------------------------- rule-level behaviour


async def test_every_position_has_a_reduce_only_stop_on_chain(env):
    user, _ = env.add_user()
    await env.enter(user)
    assert env.position(user) is not None
    stops = env.stop_orders(user)
    assert len(stops) == 1 and stops[0].reduce_only
    assert stops[0].quantity == env.position(user).quantity


async def test_stop_is_never_batched_with_the_entry(env):
    user, _ = env.add_user()
    await env.enter(user)
    for signed_tx in env.chain._signed.values():
        kinds = [o.kind.value for o in signed_tx.orders]
        assert not ("market" in kinds and "stop" in kinds), kinds


async def test_market_entry_is_rejected_by_mock_if_batched_with_stop(env):
    # The behaviour the separate-tx rule exists for: the mock must refuse it, so code that
    # batches would fail this suite even if it passed everything else.
    from halo_trade.chain.base import OrderKind, OrderRequest, Side, TxStatus

    user, _ = env.add_user()
    env.chain.auto_mine = False
    orders = [
        OrderRequest(OrderKind.MARKET, Side.LONG, D("0.001"), D("70000"), "a", False, margin=D("30")),
        OrderRequest(OrderKind.STOP, Side.SHORT, D("0.001"), D("50000"), "b", True, trigger_price=D("59000")),
    ]
    tx = await env.chain.build_and_sign(user.inj_address, user.subaccount_id, orders)
    await env.chain.broadcast(tx)
    env.chain.advance_block()
    result = await env.chain.lookup(tx)
    assert result.status is TxStatus.FAILED and result.code == 81
    assert env.position(user) is None


async def test_cid_is_deterministic_for_the_same_intent_leg(env):
    user, _ = env.add_user()
    await env.enter(user)
    entry = env.chain._signed[env.txs(purpose="entry")[0]["tx_hash"]]
    assert entry.orders[0].cid == make_cid(user.id, env.window(), "entry")


async def test_cid_is_not_used_to_dedupe_a_retried_stop(env):
    # The retry of a failed stop reuses the same deterministic cid and must still be sent.
    user, _ = env.add_user()
    env.chain.inject(Failure.STOP_PLACEMENT)
    await env.enter(user)
    stops = env.txs(purpose="stop")
    assert len(stops) == 2
    cids = [env.chain._signed[row["tx_hash"]].orders[0].cid for row in stops]
    assert cids[0] == cids[1]


async def test_live_stop_recognised_by_kind_not_cid(env):
    # A resting order with the stop's cid but a different kind must not count as protection.
    from halo_trade.chain.base import OrderKind, RestingOrder, Side
    from halo_trade.service import live_stop

    user, _ = env.add_user()
    await env.enter(user)
    account = await env.chain.account(user.inj_address, user.subaccount_id)
    fake = RestingOrder("h", make_cid(user.id, env.window(), "stop"), OrderKind.TAKE, Side.SHORT, D("0.1"), D("70000"), D("1"), True)
    no_stop = account.__class__(account.subaccount_id, account.equity, account.available, account.position, (fake,), account.height)
    assert live_stop(no_stop) is None


async def test_missed_window_is_never_replayed(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.next_window()
    env.chain.advance_block()
    with pytest.raises(ApiError) as caught:
        await env.submit_ticket(user, preview.ticket)
    assert caught.value.out.code in (E.TICKET_EXPIRED, E.STALE_OBSERVATION)
    await env.tick(3)
    assert env.chain.signed_log == []


async def test_any_failure_ends_in_no_new_risk_on_entry_tx_failure(env):
    user, _ = env.add_user()
    preview = await env.preview(user)
    env.chain.inject(Failure.NEVER_INCLUDED)
    out = await env.submit_ticket(user, preview.ticket)
    assert out.outcome is Outcome.FAILED
    assert env.position(user) is None
    await env.tick(3)
    assert len(env.txs(purpose="entry")) == 1
    assert env.position(user) is None


async def test_watcher_cancel_does_not_touch_a_position(env):
    user, _ = env.add_user()
    await env.enter(user)
    await env.tick(5)
    assert env.position(user) is not None
    assert len(env.stop_orders(user)) == 1


def test_preview_in_forbids_chain_fields():
    with pytest.raises(Exception):
        PreviewIn(observation_id="abcdefgh", side="long", stop_atr=2.0, leverage=10)  # type: ignore[call-arg]


def test_skill_numbers_are_documentation_only():
    # Size, leverage and stop come from config on the server: no money module reads the skill.
    for module in ("features.py", "sizing.py", "limits.py", "txmachine.py", "service.py", "watcher.py"):
        src = (SRC / module).read_text(encoding="utf-8")
        assert "skill" not in src.lower(), module
