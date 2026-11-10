"""Public stats: aggregate figures, a 30 s cache and no personal data."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from halo_trade import onboarding, stats
from halo_trade.chain.mock import MockChain
from halo_trade.config import Settings
from halo_trade.context import AppContext
from halo_trade.db import connect


class Harness:
    def __init__(self):
        self.now_ms = 1_800_000_000_000
        self.settings = Settings(network="testnet")
        self.db = connect(":memory:")
        self.ctx = AppContext(settings=self.settings, db=self.db, chain=MockChain(clock=lambda: self.now_ms, auto_mine=False), clock=lambda: self.now_ms)
        self.app = FastAPI()
        self.app.include_router(onboarding.router)
        self.app.include_router(stats.router)
        self.app.state.ctx = self.ctx
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    def add_user(self, inj: str, status: str = "active", equity: tuple[str, ...] = ("100",), token_hash: str = "h") -> int:
        cur = self.db.execute(
            "INSERT INTO users(inj_address,eth_address,subaccount_id,status,token_hash,created_ms) VALUES(?,?,?,?,?,?)",
            (inj, "0x" + "ab" * 20, "0x" + "cd" * 32, status, token_hash + inj, self.now_ms),
        )
        for index, value in enumerate(equity):
            self.db.execute(
                "INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat) VALUES(?,?,?,1)", (cur.lastrowid, self.now_ms + index, value)
            )
        return cur.lastrowid

    def add_trade(self, user: int, qty: str, entry: str, exit_price=None, opened=None, closed=None) -> None:
        self.db.execute(
            "INSERT INTO trades(user_id,side,quantity,entry_price,exit_price,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?)",
            (user, "long", qty, entry, exit_price, opened if opened is not None else self.now_ms, closed),
        )

    def add_intent(self, user: int, window: int, stage: str) -> None:
        self.db.execute(
            "INSERT INTO intents(user_id,window_id,ticket_id,side,quantity,stop_price,stage,created_ms,updated_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (user, window, f"t{window}", "long", "1", "100", stage, self.now_ms, self.now_ms),
        )

    def add_tx(self, user: int, purpose: str, status: str, tx_hash: str) -> None:
        self.db.execute(
            "INSERT INTO txs(user_id,purpose,signer,sequence,timeout_height,tx_hash,tx_bytes,status,created_ms,updated_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user, purpose, "inj1signer", 1, 100, tx_hash, b"x", status, self.now_ms, self.now_ms),
        )

    async def get(self) -> dict:
        response = await self.client.get("/stats.json")
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def h():
    return Harness()


async def test_empty_state_is_sane(h):
    body = await h.get()
    assert body["funded_wallets"] == 0
    assert body["total_deposits_usd"] == "0.00"
    assert body["volume_usd"] == "0.00"
    assert body["trades"] == 0
    assert body["positions_open"] == 0
    assert body["stop_coverage_pct"] is None
    assert body["last_fills"] == []


async def test_counts_funded_wallets_and_deposits_from_first_snapshot(h):
    h.add_user("inj1a", equity=("100", "130"))  # later gains are not deposits
    h.add_user("inj1b", equity=("50",))
    h.add_user("inj1c", status="paused", equity=("25",))
    h.add_user("inj1d", status="revoked", equity=("500",))
    h.add_user("inj1e", equity=("40", "0"))  # wiped out, no longer funded
    body = await h.get()
    assert body["funded_wallets"] == 3
    assert body["total_deposits_usd"] == "215.00"


async def test_volume_counts_entry_and_exit_notional_and_the_24h_window(h):
    user = h.add_user("inj1a")
    day_ago = h.now_ms - 86_400_000 - 1
    h.add_trade(user, "0.5", "100", exit_price="110", opened=h.now_ms - 1000, closed=h.now_ms)  # 50 + 55
    h.add_trade(user, "1", "10", exit_price="20", opened=day_ago, closed=day_ago)  # 10 + 20, older than 24h
    h.add_trade(user, "1", "10", opened=h.now_ms)  # open leg only: 10
    body = await h.get()
    assert body["trades"] == 3
    assert body["volume_usd"] == "145.00"
    assert body["volume_24h_usd"] == "115.00"


async def test_positions_open_and_stop_coverage(h):
    user = h.add_user("inj1a")
    h.add_intent(user, 1, "protected")
    h.add_intent(user, 2, "protected")
    h.add_intent(user, 3, "entry_filled")
    h.add_intent(user, 4, "closed")
    h.add_intent(user, 5, "flattened")
    body = await h.get()
    assert body["positions_open"] == 3
    assert body["stop_coverage_pct"] == "66.67"


async def test_last_fills_are_hashes_and_explorer_links_only(h):
    user = h.add_user("inj1secret")
    h.add_tx(user, "entry", "ok", "AA11")
    h.add_tx(user, "close", "ok", "BB22")
    h.add_tx(user, "entry", "failed", "CC33")
    h.add_tx(user, "stop", "ok", "DD44")
    body = await h.get()
    assert [f["tx_hash"] for f in body["last_fills"]] == ["BB22", "AA11"]
    for fill in body["last_fills"]:
        assert set(fill) == {"tx_hash", "explorer_url"}
        assert fill["explorer_url"].startswith("https://testnet.explorer.injective.network/transaction/")


async def test_no_personal_data_in_the_output(h):
    user = h.add_user("inj1verysecretaddress", token_hash="HASHSECRET")
    h.add_trade(user, "1", "100")
    h.add_tx(user, "entry", "ok", "AA11")
    text = json.dumps(await h.get())
    assert "inj1verysecretaddress" not in text
    assert "HASHSECRET" not in text
    assert "0xabab" not in text


async def test_response_is_cached_for_30_seconds(h):
    assert (await h.get())["funded_wallets"] == 0
    h.add_user("inj1a")
    h.now_ms += 29_999
    assert (await h.get())["funded_wallets"] == 0
    h.now_ms += 2
    assert (await h.get())["funded_wallets"] == 1


async def test_stats_page_reports_when_not_built(h, tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "STATIC_DIR", tmp_path / "missing")
    assert (await h.client.get("/stats")).status_code == 503
