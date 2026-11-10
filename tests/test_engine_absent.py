"""The service without its decision engine: the shape the open tree runs in.

Nothing decides, so nothing is ordered. A wallet can still connect, read its account, stop and
revoke; the stop watcher and its watchdog still run. With the engine in the tree these cases switch
it off at the one door in `plugin`.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from halo_trade import plugin, telegram
from halo_trade.alerts import HEARTBEAT_KEY
from halo_trade.api import create_app
from tests.conftest import api_client
from tests.test_onboarding import Harness, ready
from tests.test_onboarding_api import Bench
from tests.tg_helpers import make_bot

ENGINE_STATE = ("decider", "desk", "reflector")


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    monkeypatch.setattr(plugin, "engine", lambda: None)


@pytest.fixture
def h():
    return Harness()


# ---------------------------------------------------------------------- the door itself
def test_a_tree_without_the_engine_reads_as_none(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(plugin, "ENGINE", "halo_trade.not_in_this_tree")
    assert plugin.engine() is None


def test_an_engine_that_fails_to_import_is_an_error_not_an_absence(monkeypatch):
    monkeypatch.undo()

    def broken(name: str):
        raise ModuleNotFoundError("No module named 'something_else'", name="something_else")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        plugin.engine()


# ---------------------------------------------------------------------- the page's API
async def test_config_says_no_agent_can_run_even_with_a_model_configured(h):
    assert h.ctx.llm is not None
    assert (await h.client.get("/onboard/config")).json()["agent_available"] is False


async def test_an_agent_cannot_be_started_and_the_signature_is_kept(h):
    w, inj, _ = await ready(h)
    info = await h.start(w, "resume", preset="trend")
    proof = {"eth_address": w.address, "signature": h.sign(w, info["message"]), "preset": "trend"}
    denied = await h.client.post("/onboard/resume", json=proof)
    assert denied.status_code == 409 and denied.json()["code"] == "AGENT_UNAVAILABLE"
    assert h.user(inj)["status"] == "paused"
    assert h.db.execute("SELECT COUNT(*) FROM strategy_runs").fetchone()[0] == 0


async def test_the_chat_is_refused_and_asks_no_model(h):
    w = (await ready(h))[0]
    session = (await h.verify(w))["session"]
    proof = {"eth_address": w.address, "session": session}
    said = await h.client.post("/onboard/chat", json={**proof, "text": "how am I doing?"})
    assert said.status_code == 409 and said.json()["code"] == "AGENT_UNAVAILABLE"
    history = await h.client.post("/onboard/chat/history", json=proof)
    assert history.status_code == 409 and history.json()["code"] == "AGENT_UNAVAILABLE"
    assert h.db.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0


async def test_the_chat_still_needs_the_wallets_own_session(h):
    w = (await ready(h))[0]
    denied = await h.client.post("/onboard/chat", json={"eth_address": w.address, "session": "0" * 64, "text": "hi"})
    assert denied.status_code in (401, 403)


async def test_the_dashboard_reads_with_empty_memory_and_no_market_note():
    b = Bench()
    for name in ENGINE_STATE:
        assert not hasattr(b.app.state, name)
    wallet, session = await b.onboarded()
    shown = await b.agent(wallet, session)
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["memory"] == {"cases": 0, "lessons": [], "playbook": []}
    assert body["market"] is None
    assert body["decisions"] == []


# ---------------------------------------------------------------------- the running app
async def test_the_app_runs_its_watcher_and_no_agent(env):
    user, _ = env.add_user()
    env.ctx.db.execute("INSERT INTO strategies(user_id,preset,next_run_ms,updated_ms) VALUES(?,?,?,?)", (user.id, "trend", 0, 0))
    app = create_app(env.ctx)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert env.ctx.db.execute("SELECT value FROM system_state WHERE key=?", (HEARTBEAT_KEY,)).fetchone() is not None
    assert env.ctx.db.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0] == 0
    assert env.ctx.db.execute("SELECT next_run_ms, wake_reason FROM strategies").fetchone()[:] == (0, None)
    async with api_client(app) as client:
        assert (await client.get("/v1/health")).status_code == 200


# ---------------------------------------------------------------------- Telegram
async def test_free_text_gets_a_fixed_sentence_and_asks_no_model(env):
    bot = make_bot(env)
    user, _ = env.add_user()
    bot.link(user, 500)
    assert await bot.ask(500, "how am I doing?") == telegram.NO_ASSISTANT
    assert bot.llm.calls == []
    await bot.ask(500, "/pause")
    assert bot.status_of(user) == "paused"
