"""Onboarding wired into the real application: the routers, the service and the decider together."""

from __future__ import annotations

import hashlib
import re
import secrets
from decimal import Decimal

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from halo_trade import onboarding
from halo_trade.api import create_app
from halo_trade.chain.mock import MockChain, MockClock
from halo_trade.config import WINDOW_MS, Settings
from halo_trade.context import AppContext
from halo_trade.db import connect
from halo_trade.llm import LLMReply
from halo_trade.txmachine import KILL_SWITCH_KEY

from tests.conftest import rising_candles


START = 2_000_000 * WINDOW_MS + 60_000


class ScriptedLLM:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls = 0

    async def chat(self, messages, *, tools=None, json_mode=False, max_tokens=2000):
        self.calls += 1
        return LLMReply(content=self.replies.pop(0), model="scripted")


class Bench:
    def __init__(self, llm=None) -> None:
        self.clock = MockClock(START)
        self.chain = MockChain(clock=self.clock, auto_mine=True)
        self.chain.set_candles(rising_candles(START))  # an uptrend, so a preset's rules back an entry
        self.llm = llm if llm is not None else ScriptedLLM()
        self.ctx = AppContext(settings=Settings(network="testnet"), db=connect(":memory:"), chain=self.chain, clock=self.clock, llm=self.llm)
        self.app = create_app(self.ctx)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url="http://test")

    def fund(self, wallet, amount: str = "150") -> tuple[str, str]:
        eth = wallet.address.lower()
        inj, sub = onboarding.eth_to_inj(eth), onboarding.subaccount_id(eth)
        self.chain.register_account(inj, sub, Decimal(amount), granted=True)
        return inj, sub

    async def proof(self, wallet, action: str, **extra) -> dict:
        start = (await self.client.post("/onboard/start", json={"eth_address": wallet.address, "action": action, **extra})).json()
        signature = "0x" + wallet.sign_message(encode_defunct(text=start["message"])).signature.hex().removeprefix("0x")
        return {"eth_address": wallet.address, "signature": signature}

    async def session(self, wallet) -> str:
        verified = await self.client.post("/onboard/verify", json=await self.proof(wallet, "onboard"))
        assert verified.status_code == 200, verified.text
        return verified.json()["session"]

    async def onboarded(self, amount: str = "150"):
        wallet = Account.create()
        self.fund(wallet, amount)
        session = await self.session(wallet)
        done = await self.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": session})
        assert done.status_code == 200, done.text
        return wallet, session

    async def started(self, preset: str = "trend"):
        wallet, session = await self.onboarded()
        resumed = await self.client.post("/onboard/resume", json={**await self.proof(wallet, "resume", preset=preset), "preset": preset})
        assert resumed.status_code == 200, resumed.text
        return wallet, session

    async def agent(self, wallet, session: str) -> httpx.Response:
        return await self.client.post("/onboard/agent", json={"eth_address": wallet.address, "session": session})

    async def close(self, wallet, session: str) -> httpx.Response:
        return await self.client.post("/onboard/agent/close", json={"eth_address": wallet.address, "session": session})

    def user(self, wallet):
        inj = onboarding.eth_to_inj(wallet.address.lower())
        return self.ctx.db.execute("SELECT * FROM users WHERE inj_address=?", (inj,)).fetchone()

    def sub(self, wallet):
        return self.chain.sub(onboarding.subaccount_id(wallet.address.lower()))

    def issue_token(self, wallet) -> str:
        token = "ht_" + secrets.token_urlsafe(32)
        self.ctx.db.execute("UPDATE users SET token_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), self.user(wallet)["id"]))
        return token


@pytest.fixture
def b() -> Bench:
    return Bench()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_routes_are_reachable_through_the_app(b):
    assert (await b.client.get("/onboard/config")).status_code == 200
    assert (await b.client.get("/stats.json")).status_code == 200


async def test_a_token_authenticates_and_revoke_kills_it(b):
    wallet, _ = await b.onboarded()
    token = b.issue_token(wallet)
    state = await b.client.get("/v1/state", headers=auth(token))
    assert state.status_code == 200, state.text
    assert "PAUSED" in state.json()["blocked_reasons"]
    assert (await b.client.post("/onboard/pause", json=await b.proof(wallet, "pause"))).status_code == 200
    assert (await b.client.post("/onboard/revoke", json=await b.proof(wallet, "revoke"))).status_code == 200
    denied = await b.client.get("/v1/state", headers=auth(token))
    assert denied.status_code == 401
    assert denied.json()["code"] == "UNAUTHORIZED"


async def test_placeholder_can_never_authenticate(b):
    wallet, _ = await b.onboarded()
    placeholder = b.user(wallet)["token_hash"]
    # Lookups compare sha256 hex digests, so a stored value of any other shape has no preimage.
    assert not re.fullmatch(r"[0-9a-f]{64}", placeholder)
    for bearer in (placeholder, placeholder.split(":")[1], "pending"):
        assert (await b.client.get("/v1/state", headers=auth(bearer))).status_code == 401, bearer


async def test_two_users_can_be_pending_at_once(b):
    # users.token_hash is UNIQUE, so the placeholder has to differ per user.
    first, _ = await b.onboarded()
    second, _ = await b.onboarded()
    assert b.user(first)["token_hash"] != b.user(second)["token_hash"]


# ---------------------------------------------------------------- /onboard/agent
async def test_agent_view_before_starting(b):
    wallet, session = await b.onboarded()
    response = await b.agent(wallet, session)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "halted_by_operator": False, "status": "paused",
        "market": None,
        "preset": None,
        "tuning": None, "custom": False, "settings": None,
        "next_run_ms": None,
        "now_ms": b.clock(),
        "last_checked_ms": None,
        "explorer_tx": onboarding.NETWORKS["testnet"]["explorer_tx"],
        "run": None,
        "position": None,
        "daily_loss": {"used_usd": "0", "limit_usd": "9.00"},
        "performance": None,
        "stats": dict.fromkeys(onboarding.STAT_KEYS),
        "memory": {"cases": 0, "lessons": [], "playbook": []},
        "decisions": [],
        "trades": [],
        "telegram": {"available": False, "linked": False, "display": None},
    }


async def test_agent_view_says_when_the_operator_halted_trading(b):
    wallet, session = await b.started("steady")
    assert (await b.agent(wallet, session)).json()["halted_by_operator"] is False
    b.ctx.db.execute("INSERT OR REPLACE INTO system_state(key,value) VALUES(?,'1')", (KILL_SWITCH_KEY,))
    assert (await b.agent(wallet, session)).json()["halted_by_operator"] is True


async def test_agent_view_lists_decisions_newest_first(b):
    wallet, session = await b.started()
    uid = b.user(wallet)["id"]
    for index in range(12):
        b.ctx.db.execute("INSERT INTO agent_decisions(user_id,window_id,preset,outcome,reason,created_ms) VALUES(?,?,?,?,?,?)", (uid, 1, "trend", "hold", str(index), index))
    body = (await b.agent(wallet, session)).json()
    assert [d["reason"] for d in body["decisions"]] == [str(i) for i in range(11, -1, -1)]


async def test_agent_view_says_a_position_could_not_be_read(b):
    wallet, session = await b.started()
    b.chain.go_down()
    response = await b.agent(wallet, session)
    assert response.status_code == 200
    body = response.json()
    assert body["position"] is None and body["position_unavailable"] is True


async def test_agent_routes_need_a_session(b):
    wallet, _ = await b.onboarded()
    fake = "x" * 32
    assert (await b.agent(wallet, fake)).status_code == 401
    assert (await b.close(wallet, fake)).status_code == 401


async def test_agent_routes_are_limited_per_session(b):
    wallet, session = await b.started()
    for _ in range(onboarding.AGENT_PER_MIN):
        assert (await b.agent(wallet, session)).status_code == 200
    limited = await b.agent(wallet, session)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
    # A page that polled until it was limited must still be able to close.
    assert (await b.close(wallet, session)).status_code != 429
    other, other_session = await b.started()
    assert (await b.agent(other, other_session)).status_code == 200


async def test_close_with_no_position_is_a_code(b):
    wallet, session = await b.started()
    response = await b.close(wallet, session)
    assert response.status_code == 409 and response.json()["code"] == "NO_POSITION"


async def test_a_revoked_wallet_with_nothing_open_gets_a_code(b):
    wallet, _ = await b.started()
    assert (await b.client.post("/onboard/revoke", json=await b.proof(wallet, "revoke"))).status_code == 200
    response = await b.close(wallet, await b.session(wallet))
    assert response.status_code == 409 and response.json()["code"] == "NO_POSITION"


async def test_a_wallet_that_never_completed_gets_a_code(b):
    wallet = Account.create()
    b.fund(wallet)
    session = await b.session(wallet)
    response = await b.agent(wallet, session)
    assert response.status_code == 404 and response.json()["code"] == "USER_NOT_FOUND"


# ---------------------------------------------------------------- the dashboard


async def test_the_agent_view_returns_the_newest_hundred_decisions(b):
    wallet, session = await b.started()
    user_id = b.user(wallet)["id"]
    for index in range(105):
        b.ctx.db.execute(
            "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,reason,created_ms) VALUES(?,?,?,?,?,?)",
            (user_id, index, "trend", "hold", f"row {index}", START + index),
        )
    decisions = (await b.agent(wallet, session)).json()["decisions"]
    assert len(decisions) == 100
    assert decisions[0]["reason"] == "row 104" and decisions[-1]["reason"] == "row 5"


async def test_two_closes_moments_apart_each_keep_their_own_author(b):
    wallet, session = await b.started()
    user_id = b.user(wallet)["id"]
    for closed_ms, author, reason in ((START + 10_000, "the trend turned", "agent"), (START + 40_000, "owner", "owner")):
        b.ctx.db.execute(
            "INSERT INTO trades(user_id,side,quantity,entry_price,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?)",
            (user_id, "long", "0.01", "60000", "close", START, closed_ms),
        )
        b.ctx.db.execute("INSERT INTO decisions(user_id,window_id,kind,payload_json,created_ms) VALUES(?,?,'close',?,?)", (user_id, 1, '{"agent_reason": "%s"}' % author, closed_ms))
    trades = (await b.agent(wallet, session)).json()["trades"]
    assert [trade["closed_by"] for trade in trades] == ["owner", "agent"]


async def test_a_trade_with_nothing_on_record_reports_nulls_and_not_zeros(b):
    wallet, session = await b.started()
    b.ctx.db.execute(
        "INSERT INTO trades(user_id,side,quantity,entry_price,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?)",
        (b.user(wallet)["id"], "long", "0.01", "60000", "external", START, START + 1000),
    )
    trade = (await b.agent(wallet, session)).json()["trades"][0]
    assert trade["pnl_usd"] is None and trade["exit_price"] is None and trade["tx_hash"] is None and trade["closed_by"] is None


async def test_only_the_newest_twenty_trades_are_returned(b):
    wallet, session = await b.started()
    for index in range(25):
        b.ctx.db.execute(
            "INSERT INTO trades(user_id,side,quantity,entry_price,exit_price,pnl_usd,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?,?,?)",
            (b.user(wallet)["id"], "long", "0.01", "60000", "60100", str(index), "stop", START, START + index),
        )
    trades = (await b.agent(wallet, session)).json()["trades"]
    assert len(trades) == 20 and trades[0]["pnl_usd"] == "24" and trades[-1]["pnl_usd"] == "5"


# ---------------------------------------------------------------- result since start
MINUTE = 60_000


def snapshots(b, wallet, values, *, start_ms=None, flat=True, balance=None) -> int:
    """One equity row a minute. Returns the time of the last."""
    user_id = b.user(wallet)["id"]
    ts = b.clock() if start_ms is None else start_ms
    for offset, value in enumerate(values):
        ts = (b.clock() if start_ms is None else start_ms) + offset * MINUTE
        b.ctx.db.execute(
            "INSERT OR REPLACE INTO equity_snapshots(user_id,ts_ms,equity,flat,balance) VALUES(?,?,?,?,?)",
            (user_id, ts, str(value), 1 if flat else 0, None if balance is None else str(balance)),
        )
    if ts > b.clock():
        b.clock.set(ts)  # a run's history ends now, so "now" has to be past these rows
    return ts


def run_of(b, wallet):
    return b.ctx.db.execute("SELECT * FROM strategy_runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (b.user(wallet)["id"],)).fetchone()


async def performance(b, wallet, session) -> dict:
    return (await b.agent(wallet, session)).json()["performance"]


async def test_the_result_is_equity_change_since_the_run_began(b):
    wallet, session = await b.started()
    b.chain.deposit(onboarding.subaccount_id(wallet.address.lower()), Decimal("0"))
    body = await performance(b, wallet, session)
    assert body["active"] is True and body["since_ms"] == run_of(b, wallet)["started_ms"]
    assert body["start_equity"] == "150" and Decimal(body["net_usd"]) == 0
    assert body["trades"] == 0 and body["transfers"] == []


async def test_a_deposit_is_not_profit(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    snapshots(b, wallet, ["150", "150"], start_ms=start + MINUTE)
    snapshots(b, wallet, ["200", "200"], start_ms=start + 3 * MINUTE)
    b.chain.deposit(onboarding.subaccount_id(wallet.address.lower()), Decimal("50"))
    body = await performance(b, wallet, session)
    assert Decimal(body["equity"]) == 200 and Decimal(body["net_usd"]) == 0
    assert Decimal(body["transfers_usd"]) == 50
    assert [(t["ts_ms"], Decimal(t["amount_usd"])) for t in body["transfers"]] == [(start + 3 * MINUTE, Decimal(50))]


async def test_a_withdrawal_is_not_a_loss(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    snapshots(b, wallet, ["150", "90"], start_ms=start + MINUTE)
    b.chain.withdraw(onboarding.subaccount_id(wallet.address.lower()), Decimal("60"))
    body = await performance(b, wallet, session)
    assert Decimal(body["net_usd"]) == 0 and Decimal(body["transfers_usd"]) == -60


async def test_a_deposit_under_the_risk_limits_one_percent_is_still_not_profit(b):
    """While flat nothing else can move the balance, so the 1% rule is not needed there."""
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    snapshots(b, wallet, ["150", "150.50"], start_ms=start + MINUTE)
    b.chain.deposit(onboarding.subaccount_id(wallet.address.lower()), Decimal("0.5"))
    assert Decimal((await performance(b, wallet, session))["net_usd"]) == 0


async def test_a_trade_result_while_flat_is_profit_and_not_a_transfer(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    b.ctx.db.execute(
        "INSERT INTO trades(user_id,side,quantity,entry_price,exit_price,pnl_usd,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (b.user(wallet)["id"], "long", "0.01", "60000", "60500", "5", "take_profit", start, start + 90_000),
    )
    snapshots(b, wallet, ["150", "155"], start_ms=start + MINUTE)
    b.chain.deposit(onboarding.subaccount_id(wallet.address.lower()), Decimal("5"))
    body = await performance(b, wallet, session)
    assert body["transfers"] == [] and Decimal(body["net_usd"]) == 5 and body["trades"] == 1


async def test_funding_inside_a_position_is_not_taken_for_a_deposit(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    user_id = b.user(wallet)["id"]
    for offset, balance in enumerate(["150", "149.9"]):
        b.ctx.db.execute(
            "INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat,balance) VALUES(?,?,?,0,?)", (user_id, start + (offset + 1) * MINUTE, balance, balance)
        )
    assert (await performance(b, wallet, session))["transfers"] == []


async def test_an_unreadable_account_gives_no_result_at_all(b):
    wallet, session = await b.started()
    b.chain.go_down()
    body = await performance(b, wallet, session)
    assert body["equity"] is None and body["net_usd"] is None


async def test_a_short_record_says_so_and_draws_nothing(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    snapshots(b, wallet, ["150"] * 5, start_ms=start + MINUTE)
    body = await performance(b, wallet, session)
    assert body["history_too_short"] is True and body["points"] == [] and body["hold_usd"] is None


async def test_a_long_record_is_thinned_without_losing_its_low_or_high(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    values = ["150"] * 1440
    values[700], values[900] = "90", "210"
    user_id = b.user(wallet)["id"]
    for offset, value in enumerate(values):
        # Held in a position, so the sharp moves are not read as transfers.
        b.ctx.db.execute("INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat,balance) VALUES(?,?,?,0,'150')", (user_id, start + (offset + 1) * MINUTE, value))
    b.clock.set(start + 1441 * MINUTE)
    body = await performance(b, wallet, await b.session(wallet))
    assert body["history_too_short"] is False and len(body["points"]) <= 121
    drawn = [value for _, value in body["points"]]
    assert min(drawn) == 90 and max(drawn) == 210


async def test_a_stopped_run_ends_at_its_recorded_equity(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    b.clock.advance(5 * MINUTE)
    stopped = await b.client.post("/onboard/pause", json=await b.proof(wallet, "pause"))
    assert stopped.status_code == 200
    b.ctx.db.execute("UPDATE strategy_runs SET end_equity='147.5' WHERE user_id=?", (b.user(wallet)["id"],))
    body = await performance(b, wallet, session)
    assert body["active"] is False and body["until_ms"] == start + 5 * MINUTE
    assert Decimal(body["net_usd"]) == Decimal("-2.5")


async def test_a_run_that_stopped_without_a_final_read_has_no_result(b):
    wallet, session = await b.started()
    await b.client.post("/onboard/pause", json=await b.proof(wallet, "pause"))
    b.ctx.db.execute("UPDATE strategy_runs SET end_equity=NULL WHERE user_id=?", (b.user(wallet)["id"],))
    assert (await performance(b, wallet, session))["net_usd"] is None


async def test_buy_and_hold_uses_the_first_and_last_mark_the_agent_read(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    user_id = b.user(wallet)["id"]
    for index, mark in enumerate((60_000, 61_200)):
        b.ctx.db.execute(
            "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,created_ms,snapshot) VALUES(?,?,?,?,?,?)",
            (user_id, index, "trend", "hold", start + index * 30 * MINUTE, '{"seen":{"mark":%s},"chose":null,"server":{}}' % mark),
        )
    snapshots(b, wallet, ["150"] * 70, start_ms=start + MINUTE)
    b.clock.set(start + 71 * MINUTE)
    body = await performance(b, wallet, await b.session(wallet))
    assert Decimal(body["hold_usd"]) == Decimal("150") * (Decimal("61200") / Decimal("60000") - 1)


async def test_buy_and_hold_is_left_out_when_money_moved_mid_run(b):
    wallet, session = await b.started()
    start = run_of(b, wallet)["started_ms"]
    user_id = b.user(wallet)["id"]
    for index, mark in enumerate((60_000, 61_200)):
        b.ctx.db.execute(
            "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,created_ms,snapshot) VALUES(?,?,?,?,?,?)",
            (user_id, index, "trend", "hold", start + index * 30 * MINUTE, '{"seen":{"mark":%s},"chose":null,"server":{}}' % mark),
        )
    snapshots(b, wallet, ["150"] * 35 + ["190"] * 35, start_ms=start + MINUTE)
    b.clock.set(start + 71 * MINUTE)
    body = await performance(b, wallet, await b.session(wallet))
    assert body["transfers"] and body["hold_usd"] is None


async def test_one_wallets_history_never_reaches_another(b):
    first, _ = await b.started()
    start = run_of(b, first)["started_ms"]
    snapshots(b, first, ["150", "190"], start_ms=start + MINUTE)
    second, second_session = await b.started()
    body = await performance(b, second, second_session)
    assert body["transfers"] == [] and body["points"] == []


# ---------------------------------------------------------------- the plan before starting
async def test_the_config_carries_the_plan_from_settings(b):
    body = (await b.client.get("/onboard/config")).json()
    risk = b.ctx.settings.risk
    assert body["plan"] == {
        "decision_interval_s": b.ctx.settings.decision_interval_s,
        "risk_per_trade": str(risk.risk_per_trade),
        "daily_loss": str(risk.daily_loss),
    }


async def test_each_preset_states_the_exits_its_instructions_use(b):
    """The numbers are shown to people before they start, so they must not drift from the prompt."""
    from halo_trade.strategies import PRESETS

    presets = {preset["id"]: preset for preset in (await b.client.get("/onboard/config")).json()["presets"]}
    assert set(presets) == set(PRESETS)
    for preset_id, preset in presets.items():
        instructions = PRESETS[preset_id].instructions
        assert f"stop_atr {preset['stop_atr']:g}" in instructions
        first, *others = preset["take_profit_atr"]
        assert f"take_profit_atr {first:g}" in instructions
        for other in others:
            assert f"take_profit_atr {first:g}, or {other:g}" in instructions


# ---------------------------------------------------------------- why, on demand


async def test_a_decision_from_before_snapshots_reports_none(b):
    wallet, session = await b.started()
    b.ctx.db.execute(
        "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,reason,created_ms) VALUES(?,?,?,?,?,?)", (b.user(wallet)["id"], 1, "trend", "hold", "old", START)
    )
    assert (await b.agent(wallet, session)).json()["decisions"][0]["snapshot"] is None


def test_a_database_from_before_snapshots_gains_the_column_safely(tmp_path):
    path = str(tmp_path / "old.sqlite")
    conn = connect(path)
    conn.execute("ALTER TABLE agent_decisions DROP COLUMN snapshot")
    conn.execute("INSERT INTO users(inj_address,eth_address,subaccount_id,status,token_hash,created_ms) VALUES('i','e','s','paused','t',1)")
    conn.execute("INSERT INTO agent_decisions(user_id,window_id,preset,outcome,created_ms) VALUES(1,1,'trend','hold',1)")
    conn.close()
    again = connect(path)
    row = again.execute("SELECT snapshot, outcome FROM agent_decisions").fetchone()
    assert row["outcome"] == "hold" and row["snapshot"] is None
    again.close()
    connect(path).close()  # and running it again is harmless


# ---------------------------------------------------------------- withdrawals stay the owner's
def test_the_servers_grant_cannot_withdraw():
    from halo_trade.chain import grants

    urls = " ".join(f"{spec.authz_type_url} {spec.msg_type_url}" for spec in grants.REQUIRED_GRANTS).lower()
    assert "withdraw" not in urls and "send" not in urls and "transfer" not in urls
