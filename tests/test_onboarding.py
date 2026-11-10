"""Onboarding API: proofs, the complete gate, starting and stopping the agent, revoke."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI

from halo_trade import onboarding, stats
from halo_trade import tuning as tunings
from halo_trade.chain.base import ChainUnavailable
from halo_trade.chain.mock import MockChain
from halo_trade.config import Risk, Settings
from halo_trade.context import AppContext
from halo_trade.db import connect
from halo_trade.strategies import PRESETS

GRANTS_MSG_TYPES = {
    "/injective.exchange.v2.MsgCreateDerivativeMarketOrder",
    "/injective.exchange.v2.MsgCancelDerivativeOrder",
}


class Harness:
    def __init__(self, max_users: int = 20, max_deposit: str = "200", network: str = "testnet"):
        self.now_ms = 1_800_000_000_000
        self.chain = MockChain(clock=lambda: self.now_ms, auto_mine=False)
        self.settings = Settings(
            network=network,
            max_users=max_users,
            public_base_url="https://halo.test",
            risk=Risk(max_deposit_usd=Decimal(max_deposit)),
        )
        self.db = connect(":memory:")
        self.ctx = AppContext(settings=self.settings, db=self.db, chain=self.chain, clock=lambda: self.now_ms, llm=object())
        self.app = FastAPI()
        self.app.include_router(onboarding.router)
        self.app.include_router(stats.router)
        self.app.state.ctx = self.ctx
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    def fund(self, wallet, deposits: str = "100", granted: bool = True) -> tuple[str, str]:
        inj = onboarding.eth_to_inj(wallet.address.lower())
        sub = onboarding.subaccount_id(wallet.address.lower())
        self.chain.register_account(inj, sub, Decimal(deposits), granted=granted)
        return inj, sub

    async def start(self, wallet, action: str = "onboard", **extra) -> dict:
        response = await self.client.post("/onboard/start", json={"eth_address": wallet.address, "action": action, **extra})
        assert response.status_code == 200, response.text
        return response.json()

    def sign(self, wallet, message: str) -> str:
        return "0x" + wallet.sign_message(encode_defunct(text=message)).signature.hex().removeprefix("0x")

    async def verify(self, wallet) -> dict:
        info = await self.start(wallet)
        response = await self.client.post(
            "/onboard/verify", json={"eth_address": wallet.address, "signature": self.sign(wallet, info["message"])}
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def complete(self, wallet, session: str) -> httpx.Response:
        return await self.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": session})

    async def onboard(self, wallet, deposits: str = "100") -> tuple[str, dict]:
        self.fund(wallet, deposits)
        proof = await self.verify(wallet)
        response = await self.complete(wallet, proof["session"])
        assert response.status_code == 200, response.text
        return proof["session"], response.json()

    async def signed_action(self, wallet, action: str, **extra) -> httpx.Response:
        info = await self.start(wallet, action, **extra)
        return await self.client.post(
            f"/onboard/{action}", json={"eth_address": wallet.address, "signature": self.sign(wallet, info["message"]), **extra}
        )

    async def resume(self, wallet, preset: str = "trend") -> httpx.Response:
        return await self.signed_action(wallet, "resume", preset=preset)

    def user(self, inj: str):
        return self.db.execute("SELECT * FROM users WHERE inj_address=?", (inj,)).fetchone()


@pytest.fixture
def h():
    return Harness()


def wallet():
    return Account.create()


def test_eth_to_inj_and_subaccount_derivation():
    # Same bytes, different encoding: vector checked against @injectivelabs/sdk-ts getInjectiveAddress.
    assert onboarding.eth_to_inj("0x71c7656ec7ab88b098defb751b7401b5f6d8976f") == "inj1w8rk2mk84wytpxx7ld63kaqpkhmd39m0flj9e0"
    assert (
        onboarding.subaccount_id("0x71C7656EC7ab88b098defB751B7401B5f6d8976F")
        == "0x71c7656ec7ab88b098defb751b7401b5f6d8976f000000000000000000000001"
    )


async def test_config_lists_market_grants_and_slots(h):
    resp = await h.client.get("/onboard/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["network"] == "testnet"
    assert body["chain_id"] == "injective-888"
    assert body["evm_chain_id"] == 11155111
    assert body["market"]["ticker"] == h.chain.market_info.ticker
    assert body["market"]["market_id"] == h.chain.market_info.market_id
    assert body["grantee"] == h.chain.signer
    assert {g["msg_type_url"] for g in body["grants"]} == GRANTS_MSG_TYPES
    assert body["max_deposit_usd"] == "200"
    assert body["slots_left"] == 20
    assert [p["id"] for p in body["presets"]] == list(PRESETS)
    assert all(set(p) == {"id", "name", "summary", "stop_atr", "take_profit_atr", "tuning"} and p["summary"] for p in body["presets"])
    assert body["agent_available"] is True
    assert body["telegram_bot"] is None
    await h.onboard(wallet())
    assert (await h.client.get("/onboard/config")).json()["slots_left"] == 19


async def test_config_says_when_no_agent_can_run(h):
    h.ctx.llm = None
    assert (await h.client.get("/onboard/config")).json()["agent_available"] is False


async def test_config_names_the_bot_only_when_telegram_is_fully_configured(h):
    h.ctx.settings = Settings(network="testnet", public_base_url="https://halo.test", telegram_bot_token="t", telegram_bot_username="halo_bot", telegram_webhook_secret="s")
    assert (await h.client.get("/onboard/config")).json()["telegram_bot"] == "halo_bot"
    h.ctx.settings = Settings(network="testnet", public_base_url="https://halo.test", telegram_bot_username="halo_bot")
    assert (await h.client.get("/onboard/config")).json()["telegram_bot"] is None


async def test_config_refuses_mainnet_without_allow():
    harness = Harness(network="mainnet")
    resp = await harness.client.get("/onboard/config")
    assert resp.status_code == 503
    assert resp.json()["code"] == "NETWORK_DISABLED"


async def test_start_issues_a_nonce_and_exact_message(h):
    w = wallet()
    info = await h.start(w)
    assert info["inj_address"] == onboarding.eth_to_inj(w.address.lower())
    assert info["subaccount_id"].endswith("000000000000000000000001")
    for needle in (info["nonce"], w.address, "https://halo.test", "testnet", "Request ID: onboard", "halo.test wants you to sign in"):
        assert needle in info["message"]


async def test_start_is_idempotent_for_a_pending_nonce(h):
    w = wallet()
    assert (await h.start(w))["nonce"] == (await h.start(w))["nonce"]


async def test_verify_rejects_signature_from_another_wallet(h):
    w, other = wallet(), wallet()
    info = await h.start(w)
    resp = await h.client.post(
        "/onboard/verify", json={"eth_address": w.address, "signature": h.sign(other, info["message"])}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "BAD_SIGNATURE"


async def test_verify_rejects_a_tampered_message(h):
    w = wallet()
    await h.start(w)
    resp = await h.client.post(
        "/onboard/verify", json={"eth_address": w.address, "signature": h.sign(w, "something else")}
    )
    assert resp.status_code == 401


async def test_verify_cannot_be_replayed(h):
    w = wallet()
    info = await h.start(w)
    sig = h.sign(w, info["message"])
    first = await h.client.post("/onboard/verify", json={"eth_address": w.address, "signature": sig})
    assert first.status_code == 200
    second = await h.client.post("/onboard/verify", json={"eth_address": w.address, "signature": sig})
    assert second.status_code == 401
    assert second.json()["code"] == "NONCE_INVALID"


async def test_verify_rejects_expired_nonce(h):
    w = wallet()
    info = await h.start(w)
    h.now_ms += onboarding.NONCE_TTL_MS + 1
    resp = await h.client.post(
        "/onboard/verify", json={"eth_address": w.address, "signature": h.sign(w, info["message"])}
    )
    assert resp.status_code == 401


async def test_signature_is_bound_to_its_action(h):
    w = wallet()
    h.fund(w)
    await h.onboard(wallet())
    pause_info = await h.start(w, "pause")
    resp = await h.client.post(
        "/onboard/verify", json={"eth_address": w.address, "signature": h.sign(w, pause_info["message"])}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "NONCE_INVALID"


async def test_status_requires_a_session(h):
    w = wallet()
    resp = await h.client.post("/onboard/status", json={"eth_address": w.address, "session": "x" * 32})
    assert resp.status_code == 401


async def test_status_reports_balance_and_grant(h):
    w = wallet()
    h.fund(w, "75.5", granted=False)
    proof = await h.verify(w)
    resp = await h.client.post("/onboard/status", json={"eth_address": w.address, "session": proof["session"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["balance_usd"] == "75.5"
    assert body["grant_ok"] is False


async def test_complete_requires_verified_session(h):
    w = wallet()
    h.fund(w)
    await h.start(w)
    resp = await h.complete(w, "not-a-real-session-string")
    assert resp.status_code == 401
    assert resp.json()["code"] == "SESSION_REQUIRED"
    assert h.user(onboarding.eth_to_inj(w.address.lower())) is None


async def test_complete_rejects_someone_elses_session(h):
    w, other = wallet(), wallet()
    h.fund(w)
    h.fund(other)
    proof = await h.verify(other)
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 401


async def test_complete_rejects_without_grant(h):
    w = wallet()
    inj, _ = h.fund(w, granted=False)
    proof = await h.verify(w)
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 403
    assert resp.json()["code"] == "GRANT_MISSING"
    assert h.user(inj) is None


async def test_complete_rejects_empty_subaccount(h):
    w = wallet()
    inj, _ = h.fund(w, "0")
    proof = await h.verify(w)
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "BALANCE_EMPTY"
    assert h.user(inj) is None


async def test_complete_rejects_deposit_over_limit(h):
    w = wallet()
    inj, _ = h.fund(w, "200.01")
    proof = await h.verify(w)
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "OVER_MAX_DEPOSIT"
    assert resp.json()["limit"] == "MAX_DEPOSIT_USD=200"
    assert h.user(inj) is None


async def test_complete_accepts_exactly_the_limit(h):
    await h.onboard(wallet(), "200")


async def test_complete_creates_a_paused_wallet_and_issues_no_secret(h):
    w = wallet()
    inj, sub = h.fund(w)
    proof = await h.verify(w)
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"ok": True, "status": "paused", "inj_address": inj, "subaccount_id": sub}
    row = h.user(inj)
    assert row["token_hash"].startswith(onboarding.PENDING_TOKEN_PREFIX)
    assert row["subaccount_id"] == sub
    assert row["status"] == "paused"
    assert h.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
    assert h.db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 0


async def test_session_stays_valid_after_complete(h):
    w = wallet()
    h.fund(w)
    proof = await h.verify(w)
    assert (await h.complete(w, proof["session"])).status_code == 200
    again = await h.complete(w, proof["session"])
    assert again.status_code == 200
    assert (await h.client.post("/onboard/status", json={"eth_address": w.address, "session": proof["session"]})).status_code == 200


async def test_completing_again_changes_nothing(h):
    w = wallet()
    inj, _ = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    before = dict(h.user(inj))
    assert (await h.complete(w, (await h.verify(w))["session"])).json()["status"] == "paused"
    assert dict(h.user(inj)) == before


async def test_completing_again_is_not_gated_by_balance_or_grant(h):
    # A locked-out user with an open position must still be able to get back to the page.
    w = wallet()
    inj, sub = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    h.chain.set_grant(sub, False)
    h.chain.sub(sub).deposits = Decimal("999")
    resp = await h.complete(w, (await h.verify(w))["session"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"


async def test_max_users_blocks_new_wallets():
    harness = Harness(max_users=2)
    await harness.onboard(wallet())
    await harness.onboard(wallet())
    late = wallet()
    inj, _ = harness.fund(late)
    resp = await harness.complete(late, (await harness.verify(late))["session"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "USERS_FULL"
    assert resp.json()["limit"] == "MAX_USERS=2"
    assert harness.user(inj) is None


async def test_concurrent_completes_cannot_exceed_max_users():
    harness = Harness(max_users=1)
    wallets = [wallet() for _ in range(5)]
    sessions = []
    for w in wallets:
        harness.fund(w)
        sessions.append((await harness.verify(w))["session"])
    responses = await asyncio.gather(*[harness.complete(w, s) for w, s in zip(wallets, sessions)])
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1, codes
    assert harness.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


async def test_concurrent_completes_for_one_wallet_create_one_user(h):
    w = wallet()
    h.fund(w)
    session = (await h.verify(w))["session"]
    responses = await asyncio.gather(*[h.complete(w, session) for _ in range(4)])
    codes = [r.status_code for r in responses]
    assert 200 in codes and set(codes) <= {200, 409}, codes
    assert h.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


async def test_chain_failure_in_complete_is_not_a_grant_denial(h):
    w = wallet()
    h.fund(w)
    proof = await h.verify(w)
    async def boom(*_a, **_k):
        raise ChainUnavailable("x")
    h.chain.check_grant = boom  # type: ignore[assignment]
    resp = await h.complete(w, proof["session"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "CHAIN_UNAVAILABLE"


async def test_extra_fields_rejected(h):
    w = wallet()
    resp = await h.client.post("/onboard/start", json={"eth_address": w.address, "subaccount_id": "0xdead"})
    assert resp.status_code == 422


async def test_bad_address_and_signature_format_rejected(h):
    assert (await h.client.post("/onboard/start", json={"eth_address": "nope"})).status_code == 422
    w = wallet()
    assert (await h.client.post("/onboard/verify", json={"eth_address": w.address, "signature": "0x12"})).status_code == 422


async def test_pause_and_resume_need_fresh_signatures(h):
    w = wallet()
    inj, _ = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    assert (await h.resume(w)).json()["status"] == "active"
    assert h.user(inj)["status"] == "active"
    assert (await h.signed_action(w, "pause")).json()["status"] == "paused"
    assert h.user(inj)["status"] == "paused"
    assert (await h.resume(w)).json()["status"] == "active"
    assert h.user(inj)["status"] == "active"


async def test_action_signature_is_single_use(h):
    w = wallet()
    h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    info = await h.start(w, "pause")
    sig = h.sign(w, info["message"])
    first = await h.client.post("/onboard/pause", json={"eth_address": w.address, "signature": sig})
    assert first.status_code == 200
    second = await h.client.post("/onboard/pause", json={"eth_address": w.address, "signature": sig})
    assert second.status_code == 401


async def test_wrong_wallet_cannot_pause(h):
    w, other = wallet(), wallet()
    inj, _ = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    await h.resume(w)
    info = await h.start(w, "pause")
    resp = await h.client.post(
        "/onboard/pause", json={"eth_address": w.address, "signature": h.sign(other, info["message"])}
    )
    assert resp.status_code == 401
    assert h.user(inj)["status"] == "active"


async def test_pause_for_unknown_wallet_is_404(h):
    resp = await h.signed_action(wallet(), "pause")
    assert resp.status_code == 404


async def test_revoke_kills_the_token_and_reports_position_and_grants(h):
    w = wallet()
    inj, _ = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    old_hash = h.user(inj)["token_hash"]
    resp = await h.signed_action(w, "revoke")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "revoked"
    assert body["position_open"] is False
    assert body["grantee"] == h.chain.signer
    assert {g["msg_type_url"] for g in body["grants"]} == GRANTS_MSG_TYPES
    row = h.user(inj)
    assert row["status"] == "revoked"
    assert row["token_hash"] != old_hash
    assert (await h.client.get("/onboard/config")).json()["slots_left"] == 20


async def test_revoked_user_cannot_pause(h):
    w = wallet()
    h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    await h.signed_action(w, "revoke")
    resp = await h.signed_action(w, "pause")
    assert resp.status_code == 409


async def test_reonboarding_after_revoke_rechecks_grant_and_deposit(h):
    w = wallet()
    inj, sub = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    await h.signed_action(w, "revoke")
    h.chain.set_grant(sub, False)
    denied = await h.complete(w, (await h.verify(w))["session"])
    assert denied.status_code == 403
    assert h.user(inj)["status"] == "revoked"
    h.chain.set_grant(sub, True)
    ok = await h.complete(w, (await h.verify(w))["session"])
    assert ok.status_code == 200
    assert ok.json()["status"] == "paused"
    assert h.user(inj)["status"] == "paused"


async def test_status_change_during_complete_fails_closed(h):
    # A wallet revoked while complete is checking the chain must not come back active unchecked.
    w = wallet()
    inj, sub = h.fund(w)
    await h.complete(w, (await h.verify(w))["session"])
    await h.signed_action(w, "revoke")
    h.chain.set_grant(sub, True)
    session = (await h.verify(w))["session"]
    original = h.chain.check_grant
    async def slow(granter, subaccount):
        h.db.execute("UPDATE users SET status='active' WHERE inj_address=?", (inj,))
        return await original(granter, subaccount)
    h.chain.check_grant = slow  # type: ignore[assignment]
    resp = await h.complete(w, session)
    assert resp.status_code == 409
    assert resp.json()["code"] == "BAD_STATE"


# ---------------------------------------------------------------- starting and stopping the agent
async def ready(h, deposits: str = "100"):
    w = wallet()
    inj, sub = h.fund(w, deposits)
    await h.complete(w, (await h.verify(w))["session"])
    return w, inj, sub


def runs(h, inj):
    return h.db.execute("SELECT r.* FROM strategy_runs r JOIN users u ON u.id=r.user_id WHERE u.inj_address=? ORDER BY r.id", (inj,)).fetchall()


async def test_resume_starts_the_agent_in_one_step(h):
    w, inj, _ = await ready(h, "120.5")
    resp = await h.resume(w, "steady")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "active", "changed": True}
    user_id = h.user(inj)["id"]
    strategy = h.db.execute("SELECT * FROM strategies WHERE user_id=?", (user_id,)).fetchone()
    assert strategy["preset"] == "steady"
    assert strategy["next_run_ms"] == h.now_ms
    [run] = runs(h, inj)
    assert (run["status"], run["preset"], run["start_equity"], run["started_ms"]) == ("active", "steady", "120.5", h.now_ms)


async def test_resume_needs_a_preset(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    resp = await h.client.post("/onboard/resume", json={"eth_address": w.address, "signature": h.sign(w, info["message"])})
    assert resp.status_code == 422
    assert h.user(inj)["status"] == "paused"


@pytest.mark.parametrize("body", [{"action": "resume"}, {"action": "resume", "preset": "martingale"}, {"action": "pause", "preset": "trend"}, {"action": "revoke", "preset": "trend"}])
async def test_start_takes_a_preset_for_resume_and_only_for_resume(h, body):
    resp = await h.client.post("/onboard/start", json={"eth_address": wallet().address, **body})
    assert resp.status_code == 400 and resp.json()["code"] == "BAD_PRESET"


async def test_the_resume_statement_names_the_preset_the_market_and_the_risk(h):
    message = (await h.start(wallet(), "resume", preset="reversal"))["message"]
    assert "using the Reversal strategy on BTC/USDC PERP" in message
    assert "lose your whole deposit" in message


async def test_resume_must_use_the_preset_that_was_signed(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="steady")
    proof = {"eth_address": w.address, "signature": h.sign(w, info["message"])}
    mismatch = await h.client.post("/onboard/resume", json={**proof, "preset": "reversal"})
    assert mismatch.status_code == 401 and mismatch.json()["code"] == "BAD_SIGNATURE"
    assert h.user(inj)["status"] == "paused"
    assert h.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
    assert (await h.client.post("/onboard/resume", json={**proof, "preset": "steady"})).status_code == 200
    assert h.db.execute("SELECT preset FROM strategies").fetchone()["preset"] == "steady"


async def test_an_unknown_preset_is_refused_and_keeps_the_signature(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    proof = {"eth_address": w.address, "signature": h.sign(w, info["message"])}
    bad = await h.client.post("/onboard/resume", json={**proof, "preset": "martingale"})
    assert bad.status_code == 400 and bad.json()["code"] == "BAD_PRESET"
    assert h.user(inj)["status"] == "paused"
    assert (await h.client.post("/onboard/resume", json={**proof, "preset": "trend"})).status_code == 200


async def test_resume_needs_a_configured_llm_and_keeps_the_signature(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    proof = {"eth_address": w.address, "signature": h.sign(w, info["message"]), "preset": "trend"}
    h.ctx.llm = None
    denied = await h.client.post("/onboard/resume", json=proof)
    assert denied.status_code == 409 and denied.json()["code"] == "AGENT_UNAVAILABLE"
    assert h.user(inj)["status"] == "paused"
    h.ctx.llm = object()
    assert (await h.client.post("/onboard/resume", json=proof)).status_code == 200


async def test_resume_needs_the_wallets_own_signature(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    resp = await h.client.post("/onboard/resume", json={"eth_address": w.address, "signature": h.sign(wallet(), info["message"]), "preset": "trend"})
    assert resp.status_code == 401
    assert h.user(inj)["status"] == "paused"
    assert h.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0


async def test_resume_needs_a_grant(h):
    w, inj, sub = await ready(h)
    h.chain.set_grant(sub, False)
    resp = await h.resume(w)
    assert resp.status_code == 403 and resp.json()["code"] == "GRANT_MISSING"
    assert h.user(inj)["status"] == "paused"


async def test_resume_needs_a_balance_within_limits(h):
    w, inj, sub = await ready(h)
    h.chain.sub(sub).deposits = Decimal("10")
    low = await h.resume(w)
    assert low.status_code == 409 and low.json()["code"] == "BELOW_MIN_DEPOSIT"
    h.chain.sub(sub).deposits = Decimal("200.01")
    high = await h.resume(w)
    assert high.status_code == 409 and high.json()["code"] == "OVER_MAX_DEPOSIT"
    assert h.user(inj)["status"] == "paused"
    assert runs(h, inj) == []


async def test_a_wallet_that_has_run_may_grow_past_the_deposit_ceiling_up_to_the_cap(h):
    w, inj, sub = await ready(h, "200")
    assert (await h.resume(w)).status_code == 200
    await h.signed_action(w, "pause")
    h.chain.sub(sub).deposits = Decimal("215")
    assert (await h.resume(w)).status_code == 200
    await h.signed_action(w, "pause")
    h.chain.sub(sub).deposits = Decimal("400.01")
    over = await h.resume(w)
    assert over.status_code == 409 and over.json()["code"] == "OVER_CAP"
    assert h.user(inj)["status"] == "paused"


async def test_resume_with_the_same_settings_while_running_changes_nothing(h):
    w, inj, _ = await ready(h)
    await h.resume(w, "trend")
    before = dict(h.db.execute("SELECT * FROM strategies").fetchone())
    h.now_ms += 5_000
    again = await h.resume(w, "trend")
    assert again.json() == {"ok": True, "status": "active", "changed": False}
    assert dict(h.db.execute("SELECT * FROM strategies").fetchone()) == before
    assert len(runs(h, inj)) == 1


async def test_resume_with_other_settings_while_running_starts_a_new_run_under_them(h):
    """A running wallet changes its settings with one signature: the signed text names the new ones."""
    w, inj, _ = await ready(h, "120")
    await h.resume(w, "trend")
    h.now_ms += 5_000
    custom = {**tunings.defaults("trend", h.settings), "direction": "long", "stop_atr": 1.5}
    changed = await h.signed_action(w, "resume", preset="trend", tuning=custom)
    assert changed.json() == {"ok": True, "status": "active", "changed": True}
    row = h.db.execute("SELECT * FROM strategies").fetchone()
    assert tunings.resolve("trend", row["tuning"], h.settings) == tunings.normalize("trend", custom, h.settings)
    assert (row["next_run_ms"], row["wake_reason"]) == (h.now_ms, "first")  # the agent looks again under the new settings
    first, second = runs(h, inj)
    assert (first["status"], first["stop_reason"], first["stopped_ms"]) == ("stopped", "superseded", h.now_ms)
    assert first["end_equity"] == second["start_equity"] == "120"
    assert (second["status"], second["preset"], second["tuning"]) == ("active", "trend", row["tuning"])
    assert h.user(inj)["status"] == "active"

    h.now_ms += 5_000
    other = await h.resume(w, "steady")  # another strategy is a change too
    assert other.json()["changed"] is True
    assert h.db.execute("SELECT preset FROM strategies").fetchone()["preset"] == "steady"
    assert [run["status"] for run in runs(h, inj)] == ["stopped", "stopped", "active"]


async def test_a_change_of_settings_while_running_needs_a_signature_over_those_settings(h):
    w, inj, _ = await ready(h)
    await h.resume(w, "trend")
    signed_for = {**tunings.defaults("trend", h.settings), "direction": "long"}
    info = await h.start(w, "resume", preset="trend", tuning=signed_for)
    sent = {**signed_for, "direction": "short"}
    refused = await h.client.post(
        "/onboard/resume", json={"eth_address": w.address, "signature": h.sign(w, info["message"]), "preset": "trend", "tuning": sent}
    )
    assert refused.status_code in (400, 401, 403), refused.text
    assert h.db.execute("SELECT tuning FROM strategies").fetchone()["tuning"] is None or tunings.resolve(
        "trend", h.db.execute("SELECT tuning FROM strategies").fetchone()["tuning"], h.settings
    ) == tunings.defaults("trend", h.settings)
    assert len(runs(h, inj)) == 1


async def test_resume_of_a_revoked_wallet_is_refused(h):
    w, inj, _ = await ready(h)
    await h.signed_action(w, "revoke")
    resp = await h.resume(w)
    assert resp.status_code == 409 and resp.json()["code"] == "BAD_STATE"
    assert h.user(inj)["status"] == "revoked"


async def test_a_signature_cannot_be_spent_twice_by_racing_resumes(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    proof = {"eth_address": w.address, "signature": h.sign(w, info["message"]), "preset": "trend"}
    original = h.chain.check_grant

    async def yielding(*args):
        await asyncio.sleep(0)  # both requests get past the first signature check before either writes
        return await original(*args)

    h.chain.check_grant = yielding  # type: ignore[assignment]
    results = await asyncio.gather(*[h.client.post("/onboard/resume", json=proof) for _ in range(2)])
    assert sorted(r.status_code for r in results) == [200, 401]
    assert len(runs(h, inj)) == 1


async def test_resume_replaces_a_run_left_active_by_accident(h):
    w, inj, _ = await ready(h)
    user_id = h.user(inj)["id"]
    h.db.execute("INSERT INTO strategy_runs(user_id,preset,status,started_ms) VALUES(?,'trend','active',1)", (user_id,))
    assert (await h.resume(w)).status_code == 200
    assert [(r["status"], r["stop_reason"]) for r in runs(h, inj)] == [("stopped", "superseded"), ("active", None)]


async def test_pause_closes_the_run_with_the_end_equity(h):
    w, inj, sub = await ready(h)
    await h.resume(w)
    h.chain.sub(sub).deposits = Decimal("90")
    h.now_ms += 1_000
    assert (await h.signed_action(w, "pause")).json() == {"ok": True, "status": "paused", "changed": True}
    [run] = runs(h, inj)
    assert (run["status"], run["stop_reason"], run["end_equity"], run["stopped_ms"]) == ("stopped", "user", "90", h.now_ms)


async def test_pause_works_and_leaves_end_equity_empty_while_the_chain_is_down(h):
    w, inj, _ = await ready(h)
    await h.resume(w)
    info = await h.start(w, "pause")
    h.chain.go_down()
    resp = await h.client.post("/onboard/pause", json={"eth_address": w.address, "signature": h.sign(w, info["message"])})
    assert resp.status_code == 200
    assert h.user(inj)["status"] == "paused"
    [run] = runs(h, inj)
    assert (run["status"], run["stop_reason"], run["end_equity"]) == ("stopped", "user", None)


async def test_a_new_run_starts_after_each_stop(h):
    w, inj, _ = await ready(h)
    await h.resume(w, "trend")
    await h.signed_action(w, "pause")
    await h.resume(w, "reversal")
    assert [(r["preset"], r["status"]) for r in runs(h, inj)] == [("trend", "stopped"), ("reversal", "active")]
    assert h.db.execute("SELECT preset FROM strategies").fetchone()["preset"] == "reversal"


async def test_revoke_stops_the_run_and_unlinks_telegram(h):
    w, inj, _ = await ready(h)
    await h.resume(w)
    user_id = h.user(inj)["id"]
    h.db.execute("INSERT INTO telegram_links(user_id,chat_id,linked_ms) VALUES(?,?,?)", (user_id, "123", h.now_ms))
    h.db.execute("INSERT INTO telegram_link_codes(code_hash,user_id,expires_ms) VALUES(?,?,?)", ("abc", user_id, h.now_ms + 1))
    resp = await h.signed_action(w, "revoke")
    assert resp.status_code == 200
    [run] = runs(h, inj)
    assert (run["status"], run["stop_reason"], run["end_equity"]) == ("stopped", "revoked", "100")
    for table in ("telegram_links", "telegram_link_codes", "strategies"):
        assert h.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


async def test_page_not_built_reports_a_code(h, tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "STATIC_DIR", tmp_path / "missing")
    resp = await h.client.get("/")
    assert resp.status_code == 503
    assert resp.json()["code"] == "PAGE_NOT_BUILT"


async def test_page_served_with_a_strict_csp_and_no_path_traversal(h, tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>page</html>", encoding="utf-8")
    (static / "assets" / "a.js").write_text("1", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(onboarding, "STATIC_DIR", static)

    page = await h.client.get("/")
    assert page.status_code == 200
    assert "page" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "testnet.sentry.lcd.injective.network" in csp
    assert (await h.client.get("/assets/a.js")).status_code == 200
    traversal = await h.client.get("/assets/%2e%2e/%2e%2e/secret.txt")
    assert traversal.status_code == 404
