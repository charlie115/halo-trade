"""The Telegram lane: webhook, linking, commands, outbox and trade notifications."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.util
import json
import logging
import sqlite3
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from halo_trade import account_view, notify, onboarding, telegram
from halo_trade.api import BodySizeLimit, PATH_BODY_LIMITS
from halo_trade.chain.mock import Failure
from halo_trade.config import Settings
from halo_trade.contracts import Outcome
from halo_trade.startup import ConfigError, validate_settings

from tests.tg_helpers import BOT, SECRET, SECRET_HEADER, SESSION, TOKEN, make_bot, message


SIGNATURE = "0x" + "ab" * 65
OK = lambda _request: httpx.Response(200, json={"ok": True, "result": {}})  # noqa: E731


@pytest.fixture
def bot(env):
    return make_bot(env)


def linked(bot, chat_id=500, **kwargs):
    user, _ = bot.env.add_user(**kwargs)
    bot.link(user, chat_id)
    return user


# ---------------------------------------------------------------------- webhook gate


async def test_wrong_or_missing_secret_is_404_and_queues_nothing(bot):
    update = message(1, 500, "/help")
    assert (await bot.post(update, headers={})).status_code == 404
    assert (await bot.post(update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})).status_code == 404
    assert (await bot.post(update, headers={"X-Telegram-Bot-Api-Secret-Token": "caf\xe9".encode("latin-1")})).status_code == 404
    await bot.worker.flush()
    assert bot.tg.calls == []
    assert (await bot.post(update)).status_code == 200


async def test_routes_are_inert_without_a_bot(env):
    bot = make_bot(env)
    env.ctx.settings = dataclasses.replace(env.ctx.settings, telegram_bot_token="")
    eth = "0x" + "ab" * 20
    assert (await bot.post(message(1, 500, "/help"))).status_code == 404
    response = await bot.client.post("/onboard/telegram/link", json={"eth_address": eth, "signature": SIGNATURE})
    assert (response.status_code, response.json()["code"]) == (409, "TELEGRAM_UNAVAILABLE")
    response = await bot.client.post("/onboard/telegram/unlink", json={"eth_address": eth, "session": SESSION})
    assert (response.status_code, response.json()["code"]) == (409, "TELEGRAM_UNAVAILABLE")
    assert await bot.worker.flush() == 0
    assert bot.tg.calls == []


async def test_malformed_body_is_acknowledged_and_ignored(bot):
    response = await bot.client.post("/telegram/webhook", content=b"{not json", headers=SECRET_HEADER)
    assert response.status_code == 200
    assert (await bot.post({"update_id": "x", "message": {}})).status_code == 200
    await bot.worker.flush()
    assert bot.tg.calls == []


async def test_full_queue_drops_says_busy_and_still_returns_200(bot, caplog):
    linked(bot)
    bot.env.ctx.telegram_gate.updates = asyncio.Queue(maxsize=1)
    assert (await bot.post(message(1, 500, "/help"))).status_code == 200
    assert (await bot.post(message(2, 500, "/help"))).status_code == 200
    assert "queue is full" in caplog.text
    await bot.worker.flush()
    assert telegram.BUSY in bot.tg.texts(500)


async def test_a_database_error_on_the_webhook_asks_telegram_to_redeliver(bot, monkeypatch):
    linked(bot)
    original = telegram._accept

    async def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(telegram, "_accept", broken)
    assert (await bot.post(message(1, 500, "/pause"))).status_code == 503
    monkeypatch.setattr(telegram, "_accept", original)
    assert (await bot.post(message(1, 500, "/pause"))).status_code == 200


async def test_duplicate_update_id_is_handled_once(bot):
    linked(bot)
    update = message(77, 500, "/help")
    await bot.post(update)
    await bot.post(update)
    await bot.worker.flush()
    assert len(bot.tg.texts(500)) == 1
    await bot.post(update)  # a redelivery in a later batch
    await bot.worker.flush()
    assert len(bot.tg.texts(500)) == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_only_private_chats_are_processed(bot, chat_type):
    user = linked(bot)
    await bot.say(500, "/pause", chat_type=chat_type)
    await bot.say(600, "what is my balance?", chat_type=chat_type)
    await bot.press(500, "close:abc", chat_type=chat_type)
    await bot.worker.flush()
    assert bot.tg.calls == []
    assert bot.llm.calls == []
    assert bot.status_of(user) == "active"


async def test_an_update_older_than_five_minutes_is_ignored(bot):
    user = linked(bot)
    now_s = bot.env.clock() // 1000
    await bot.post(message(1, 500, "/pause", date=now_s - 301))
    await bot.worker.flush()
    assert bot.status_of(user) == "active"
    assert bot.tg.texts(500) == []
    await bot.post(message(2, 500, "/pause", date=now_s - 299))
    assert bot.status_of(user) == "paused"


async def test_a_button_on_a_message_older_than_five_minutes_is_ignored(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    update = {
        "update_id": 9001,
        "callback_query": {
            "id": "x",
            "from": {"id": 500},
            "message": {"message_id": 1, "date": bot.env.clock() // 1000 - 301, "chat": {"id": 500, "type": "private"}},
            "data": data,
        },
    }
    await bot.post(update)
    await bot.worker.flush()
    assert bot.env.position(user) is not None


async def test_a_large_real_shaped_update_is_within_the_webhook_limit(bot):
    assert PATH_BODY_LIMITS["/telegram/webhook"] == 256 * 1024
    inner = bot.client._transport.app
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=BodySizeLimit(inner)), base_url="http://t")
    linked(bot)
    text = "\U0001F600" * 4096
    update = message(1, 500, text)
    update["message"]["reply_to_message"] = {"message_id": 2, "chat": {"id": 500, "type": "private"}, "text": text}
    body = json.dumps(update).encode()
    assert len(body) > 64 * 1024
    response = await client.post("/telegram/webhook", content=body, headers={**SECRET_HEADER, "content-type": "application/json"})
    assert response.status_code == 200


def test_startup_refuses_a_short_webhook_secret_when_a_token_is_set():
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        validate_settings(Settings(telegram_bot_token="1:a", telegram_bot_username="b", telegram_webhook_secret="a" * 31))
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        validate_settings(Settings(telegram_bot_token="1:a", telegram_bot_username="b", telegram_webhook_secret=""))
    validate_settings(Settings(telegram_bot_token="1:a", telegram_bot_username="b", telegram_webhook_secret="a" * 32))
    validate_settings(Settings())


# ---------------------------------------------------------------------- linking: needs a signature


async def test_link_needs_a_fresh_signature_and_a_session_is_not_enough(bot):
    _, wallet = bot.add_wallet()
    bot.open_session(wallet)
    response = await bot.client.post("/onboard/telegram/link", json={"eth_address": wallet.address.lower(), "session": SESSION})
    assert response.status_code == 422
    assert bot.env.ctx.db.execute("SELECT COUNT(*) FROM telegram_link_codes").fetchone()[0] == 0


async def test_link_signature_is_single_use_and_bad_ones_are_refused(bot):
    _, wallet = bot.add_wallet()
    eth = wallet.address.lower()
    assert (await bot.client.post("/onboard/telegram/link", json={"eth_address": eth, "signature": SIGNATURE})).status_code == 401
    signature = await bot.signature(wallet, "telegram")
    first = await bot.client.post("/onboard/telegram/link", json={"eth_address": eth, "signature": signature})
    assert first.status_code == 200
    second = await bot.client.post("/onboard/telegram/link", json={"eth_address": eth, "signature": signature})
    assert (second.status_code, second.json()["code"]) == (401, "NONCE_INVALID")


async def test_the_statement_says_what_a_linked_chat_can_do(bot):
    _, wallet = bot.add_wallet()
    body = (await bot.client.post("/onboard/start", json={"eth_address": wallet.address.lower(), "action": "telegram"})).json()
    statement = body["message"].split("\n\n")[1]
    assert "Telegram chat" in statement and "see this wallet's agent, stop it and close its position" in statement
    assert "\n" not in statement and statement.isascii() and statement.isprintable()


async def test_a_wallet_with_a_link_must_disconnect_first(bot):
    user, wallet = bot.add_wallet()
    await bot.connect(wallet, 500)
    assert bot.links(user) == ["500"]
    response = await bot.request_link(wallet)
    assert (response.status_code, response.json()["code"]) == (409, "TELEGRAM_ALREADY_LINKED")
    assert bot.env.ctx.db.execute("SELECT COUNT(*) FROM telegram_link_codes").fetchone()[0] == 0
    assert bot.links(user) == ["500"]


async def test_revoked_wallet_cannot_link(bot):
    _, wallet = bot.add_wallet(status="revoked")
    response = await bot.request_link(wallet)
    assert (response.status_code, response.json()["code"]) == (409, "BAD_STATE")


async def test_link_code_is_stored_only_as_a_hash(bot):
    _, wallet = bot.add_wallet()
    code = await bot.link_code(wallet)
    row = bot.env.ctx.db.execute("SELECT * FROM telegram_link_codes").fetchone()
    assert row["code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in str(dict(row))


# ---------------------------------------------------------------------- linking: only the button links


async def test_start_does_not_link_and_shows_the_masked_wallet(bot):
    user, wallet = bot.add_wallet()
    eth = wallet.address.lower()
    text = await bot.ask(500, f"/start {await bot.link_code(wallet)}")
    assert f"Connect this chat to wallet {eth[:6]}...{eth[-4:]}?" in text
    assert bot.links(user) == []
    assert bot.tg.button(500, telegram.CONNECT_PREFIX)


async def test_the_connect_button_links_and_stores_a_plain_display_name(bot):
    user, wallet = bot.add_wallet()
    await bot.connect(wallet, 500, sender={"username": "alice_t", "first_name": "Alice"})
    row = bot.env.ctx.db.execute("SELECT * FROM telegram_links WHERE user_id=?", (user.id,)).fetchone()
    assert (row["chat_id"], row["display"]) == ("500", "@alice_t")
    eth = wallet.address.lower()
    assert f"Connected to wallet {eth[:6]}...{eth[-4:]}" in bot.tg.texts(500)[-1]


@pytest.mark.parametrize(
    "sender, expected",
    [
        ({"first_name": "Alice"}, "Alice"),
        ({"first_name": "A\nli‮ce"}, "Alice"),  # controls stripped, the name stays plain text
        ({"first_name": "x" * 500}, "x" * telegram.DISPLAY_MAX_CHARS),
        ({}, None),
    ],
)
async def test_display_name_is_plain_and_capped(bot, sender, expected):
    user, wallet = bot.add_wallet()
    await bot.connect(wallet, 500, sender=sender)
    assert bot.env.ctx.db.execute("SELECT display FROM telegram_links WHERE user_id=?", (user.id,)).fetchone()["display"] == expected


async def test_agent_view_shows_the_display_name(bot):
    user, wallet = bot.add_wallet()
    bot.open_session(wallet)
    await bot.connect(wallet, 500, sender={"username": "alice_t"})
    response = await bot.client.post("/onboard/agent", json={"eth_address": wallet.address.lower(), "session": SESSION})
    assert response.json()["telegram"] == {"available": True, "linked": True, "display": "@alice_t"}


async def test_connect_button_is_single_use(bot):
    user, wallet = bot.add_wallet()
    code = await bot.link_code(wallet)
    await bot.ask(500, f"/start {code}")
    data = bot.tg.button(500, telegram.CONNECT_PREFIX)
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.links(user) == ["500"]
    sends = len(bot.tg.calls)
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.links(user) == ["500"]
    assert len(bot.tg.calls) <= sends + 1


async def test_a_wrong_connect_token_does_nothing_and_leaves_the_real_one_valid(bot):
    user, wallet = bot.add_wallet()
    await bot.ask(500, f"/start {await bot.link_code(wallet)}")
    await bot.press(500, telegram.CONNECT_PREFIX + "not-the-token")
    await bot.worker.flush()
    assert bot.links(user) == []
    await bot.press(500, bot.tg.button(500, telegram.CONNECT_PREFIX))
    await bot.worker.flush()
    assert bot.links(user) == ["500"]


async def test_connect_button_is_bound_to_the_chat_it_was_offered_in(bot):
    user, wallet = bot.add_wallet()
    await bot.ask(500, f"/start {await bot.link_code(wallet)}")
    data = bot.tg.button(500, telegram.CONNECT_PREFIX)
    await bot.press(666, data)
    await bot.worker.flush()
    assert bot.links(user) == []
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.links(user) == ["500"]


async def test_connect_button_expires_with_the_code(bot):
    user, wallet = bot.add_wallet()
    await bot.ask(500, f"/start {await bot.link_code(wallet)}")
    bot.env.clock.advance(telegram.LINK_CODE_TTL_MS)
    await bot.press(500, bot.tg.button(500, telegram.CONNECT_PREFIX))
    await bot.worker.flush()
    assert bot.links(user) == []
    assert bot.tg.texts(500)[-1] == telegram.BAD_CODE


async def test_an_expired_link_code_gets_no_prompt(bot):
    user, wallet = bot.add_wallet()
    code = await bot.link_code(wallet)
    bot.env.clock.advance(telegram.LINK_CODE_TTL_MS)
    assert await bot.ask(500, f"/start {code}") == telegram.BAD_CODE
    assert bot.links(user) == []


async def test_a_new_code_replaces_the_old_one(bot):
    _, wallet = bot.add_wallet()
    old = await bot.link_code(wallet)
    new = await bot.link_code(wallet)
    assert await bot.ask(500, f"/start {old}") == telegram.BAD_CODE
    assert "Connect this chat" in await bot.ask(600, f"/start {new}")


async def test_a_forwarded_code_needs_the_button_and_one_press_wins(bot):
    user, wallet = bot.add_wallet()
    code = await bot.link_code(wallet)
    await bot.ask(500, f"/start {code}")
    await bot.ask(666, f"/start {code}")
    assert bot.links(user) == []  # nothing linked by opening the link
    await bot.press(500, bot.tg.button(500, telegram.CONNECT_PREFIX))
    await bot.worker.flush()
    await bot.press(666, bot.tg.button(666, telegram.CONNECT_PREFIX))
    await bot.worker.flush()
    assert bot.links(user) == ["500"]
    assert bot.tg.texts(666)[-1] == telegram.BAD_CODE


async def test_chat_linked_to_another_wallet_is_told_to_unlink_here(bot):
    other = linked(bot)
    user, wallet = bot.add_wallet()
    code = await bot.link_code(wallet)
    text = await bot.ask(500, f"/start {code}")
    assert text == telegram.OTHER_WALLET
    assert "/unlink" in text and "site" not in text
    assert bot.links(user) == []
    assert bot.links(other) == ["500"]
    # The refusal did not spend the code: the owner can still use it from their own chat.
    assert "Connect this chat" in await bot.ask(600, f"/start {code}")


async def test_an_attackers_link_squat_can_be_undone_from_the_chat(bot):
    attacker, attacker_wallet = bot.add_wallet()
    victim, victim_wallet = bot.add_wallet()
    # The victim taps a link the attacker sent, and is shown the attacker's masked wallet.
    code = await bot.link_code(attacker_wallet)
    await bot.ask(500, f"/start {code}")
    attacker_eth = attacker_wallet.address.lower()
    assert attacker_eth[:6] in bot.tg.texts(500)[-1]
    await bot.press(500, bot.tg.button(500, telegram.CONNECT_PREFIX))  # worst case: they press anyway
    await bot.worker.flush()
    assert bot.links(attacker) == ["500"]

    own = await bot.link_code(victim_wallet)
    assert await bot.ask(500, f"/start {own}") == telegram.OTHER_WALLET
    await bot.ask(500, "VICTIM-TYPED-SECRET")
    await bot.say(500, "/unlink")
    assert bot.links(attacker) == []
    assert bot.env.ctx.db.execute("SELECT COUNT(*) FROM chat_turns WHERE user_id=?", (attacker.id,)).fetchone()[0] == 0
    await bot.ask(500, f"/start {own}")
    await bot.press(500, bot.tg.button(500, telegram.CONNECT_PREFIX))
    await bot.worker.flush()
    assert bot.links(victim) == ["500"]


# ---------------------------------------------------------------------- what an unlink forgets


def chat_data_counts(env, user):
    db = env.ctx.db
    return (
        db.execute("SELECT COUNT(*) FROM chat_memory WHERE user_id=?", (user.id,)).fetchone()[0],
        db.execute("SELECT COUNT(*) FROM chat_turns WHERE user_id=?", (user.id,)).fetchone()[0],
        db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE user_id=? AND sent_ms IS NULL AND kind<>'disconnected'", (user.id,)).fetchone()[0],
    )


async def _seed_chat_data(bot, user, chat_id):
    db, now = bot.env.ctx.db, bot.env.clock()
    db.execute("INSERT OR REPLACE INTO chat_memory(user_id,bucket,content,updated_ms) VALUES(?,'user','NOTE-PRIVATE',?)", (user.id, now))
    db.execute("INSERT INTO chat_turns(user_id,role,content,created_ms) VALUES(?,'user','HISTORY-PRIVATE',?)", (user.id, now))
    await notify.enqueue(bot.env.ctx, user.id, "reply", "OWNER-PRIVATE-REPLY")
    assert chat_data_counts(bot.env, user) == (1, 1, 1)


async def test_the_unlink_command_releases_the_chat_and_forgets_its_data(bot):
    user, wallet = bot.add_wallet()
    await bot.connect(wallet, 500)
    sends = len(bot.tg.texts(500))
    await _seed_chat_data(bot, user, 500)
    await bot.say(500, "/unlink")  # applied on the webhook, with no worker pass
    assert bot.links(user) == []
    assert chat_data_counts(bot.env, user) == (0, 0, 0)
    await bot.worker.flush()
    assert bot.tg.texts(500)[sends:] == [onboarding.TELEGRAM_DISCONNECTED]


async def test_the_site_unlink_forgets_the_data_and_tells_the_old_chat(bot):
    user, wallet = bot.add_wallet()
    bot.open_session(wallet)
    await bot.connect(wallet, 500)
    await _seed_chat_data(bot, user, 500)
    response = await bot.client.post("/onboard/telegram/unlink", json={"eth_address": wallet.address.lower(), "session": SESSION})
    assert response.json() == {"ok": True, "linked": False}
    assert chat_data_counts(bot.env, user) == (0, 0, 0)
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == onboarding.TELEGRAM_DISCONNECTED
    assert "OWNER-PRIVATE-REPLY" not in bot.tg.texts()


async def test_revoke_forgets_the_data_tells_the_chat_and_a_returning_wallet_starts_clean(bot):
    user, wallet = bot.add_wallet()
    bot.open_session(wallet)
    await bot.connect(wallet, 500)
    await _seed_chat_data(bot, user, 500)
    signature = await bot.signature(wallet, "revoke")
    response = await bot.client.post("/onboard/revoke", json={"eth_address": wallet.address.lower(), "signature": signature})
    assert response.status_code == 200, response.text
    assert bot.status_of(user) == "revoked"
    assert bot.links(user) == []
    assert chat_data_counts(bot.env, user) == (0, 0, 0)
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == onboarding.TELEGRAM_DISCONNECTED


async def test_a_queued_trade_notice_does_not_follow_a_link_to_a_new_chat(bot):
    user, wallet = bot.add_wallet()
    bot.open_session(wallet)
    await bot.connect(wallet, 500)
    await notify.enqueue(bot.env.ctx, user.id, "stop_hit", "STOP-HIT", dedupe_key="closed:1")
    await bot.client.post("/onboard/telegram/unlink", json={"eth_address": wallet.address.lower(), "session": SESSION})
    await bot.connect(wallet, 777)
    await bot.worker.flush()
    assert "STOP-HIT" not in bot.tg.texts(777)


# ---------------------------------------------------------------------- strangers


async def test_a_stranger_gets_one_line_per_ten_minutes_and_no_database_write(bot, monkeypatch):
    db = bot.env.ctx.db
    before = db.total_changes
    for text in ("hello there", "/status", "/start", "/pause", "/start not-a-code"):
        await bot.say(600, text)
    assert db.total_changes == before
    await bot.worker.flush()
    assert bot.tg.texts(600) == [telegram.NOT_LINKED]
    assert bot.llm.calls == []
    assert bot.outbox() == []
    bot.env.clock.advance(telegram.STRANGER_REPLY_EVERY_MS)
    await bot.say(600, "again")
    await bot.worker.flush()
    assert bot.tg.texts(600) == [telegram.NOT_LINKED, telegram.NOT_LINKED]


async def test_strangers_take_no_queue_slot_so_an_owners_pause_lands(bot):
    user = linked(bot)
    gate = bot.env.ctx.telegram_gate
    for i in range(telegram.UPDATE_QUEUE_MAX + 5):
        await bot.say(10_000 + i, "hi")
    assert gate.updates.qsize() == 0
    assert bot.env.ctx.db.execute("SELECT COUNT(*) FROM telegram_seen_updates").fetchone()[0] == 0
    assert len(gate.direct) <= telegram.DIRECT_SEND_MAX
    await bot.say(500, "/pause")
    assert bot.status_of(user) == "paused"


async def test_the_stranger_map_is_bounded(bot, monkeypatch):
    monkeypatch.setattr(telegram, "BOUNDED_MAP_MAX", 10)
    for i in range(50):
        await bot.say(20_000 + i, "hi")
    assert len(bot.env.ctx.telegram_gate.strangers) == 10


# ---------------------------------------------------------------------- commands


async def test_status_shows_balance_and_position(bot):
    user = linked(bot)
    out = await bot.env.enter(user)
    assert out.outcome is Outcome.FILLED
    text = await bot.ask(500, "/status")
    assert "Agent: running" in text
    assert "Equity: $" in text
    assert "Position: long" in text
    assert "stop" in text
    assert bot.llm.calls == []


async def test_status_says_when_the_chain_cannot_be_read(bot):
    linked(bot)
    bot.env.chain.go_down()
    text = await bot.ask(500, "/status")
    assert f"Equity: {telegram.UNAVAILABLE_TEXT}" in text
    assert f"Position: {telegram.UNAVAILABLE_TEXT}" in text
    assert "$0" not in text


async def test_status_shows_flat_only_when_the_read_succeeded(bot):
    linked(bot)
    assert "Position: none" in await bot.ask(500, "/status")


async def test_status_mentions_the_kill_switch(bot):
    linked(bot)
    await bot.env.svc.set_kill_switch(True)
    assert "paused by the operator" in await bot.ask(500, "/status")


async def test_a_lagging_read_is_reported_unavailable_never_as_no_position(bot):
    user = linked(bot)
    bot.env.chain.advance_block()
    before = bot.env.chain.height
    assert (await bot.env.enter(user)).outcome is Outcome.FILLED
    bot.env.chain.advance_block()
    bot.env.chain.set_read_lag(bot.env.chain.height - before)
    assert bot.env.position(user) is not None
    text = await bot.ask(500, "/status")
    assert f"Position: {telegram.UNAVAILABLE_TEXT}" in text
    assert "Position: none" not in text


async def test_status_is_cached_per_user_so_a_burst_reads_the_chain_once(bot, monkeypatch):
    user = linked(bot)
    original, reads = bot.env.chain.account, []

    async def counting(granter, subaccount_id):
        reads.append(granter)
        return await original(granter, subaccount_id)

    monkeypatch.setattr(bot.env.chain, "account", counting)
    for _ in range(15):
        await bot.say(500, "/status")
    await bot.worker.flush()
    assert len(reads) == 1
    bot.env.clock.advance(account_view.ACCOUNT_CACHE_MS)
    await bot.ask(500, "/status")
    assert len(reads) == 2
    assert user.inj_address in reads


def add_decision(env, user, *, outcome="hold", reason="Trend is flat and funding is neutral.", code=None, side=None):
    env.ctx.db.execute(
        "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,side,reason,code,created_ms) VALUES(?,?,?,?,?,?,?,?)",
        (user.id, env.window(), "trend", outcome, side, reason, code, env.clock()),
    )


async def test_why_shows_the_latest_decision_and_its_reason(bot):
    user = linked(bot)
    assert await bot.ask(500, "/why") == telegram.NO_DECISION_YET
    add_decision(bot.env, user, reason="Older reason.")
    add_decision(bot.env, user, outcome="rejected", reason="Newest reason.", code="COOLDOWN", side="long")
    bot.env.clock.advance(300_000)
    text = await bot.ask(500, "/why")
    assert "Newest reason." in text
    assert "Older reason." not in text
    assert "refused" in text
    assert "COOLDOWN" in text
    assert "5 min ago" in text
    assert bot.llm.calls == []


async def test_why_is_scoped_to_the_linked_wallet(bot):
    linked(bot)
    other, _ = bot.env.add_user()
    add_decision(bot.env, other, reason="Other wallet's reason.")
    assert await bot.ask(500, "/why") == telegram.NO_DECISION_YET


def start_run(env, user):
    env.ctx.db.execute("INSERT INTO strategies(user_id,preset,next_run_ms,updated_ms) VALUES(?,?,?,?)", (user.id, "trend", env.clock(), env.clock()))
    env.ctx.db.execute(
        "INSERT INTO strategy_runs(user_id,preset,status,start_equity,started_ms) VALUES(?,?,'active','100',?)", (user.id, "trend", env.clock())
    )


async def test_pause_stops_the_agent_on_the_webhook_and_closes_the_run(bot):
    user = linked(bot)
    start_run(bot.env, user)
    bot.env.ctx.db.execute("INSERT INTO equity_snapshots(user_id,ts_ms,equity,flat) VALUES(?,?,'97.5',1)", (user.id, bot.env.clock()))
    await bot.say(500, "/pause")  # no worker pass needed
    db = bot.env.ctx.db
    assert bot.status_of(user) == "paused"
    run = db.execute("SELECT * FROM strategy_runs WHERE user_id=?", (user.id,)).fetchone()
    assert run["status"] == "stopped"
    assert run["stop_reason"] == "telegram"
    assert run["stopped_ms"] == bot.env.clock()
    assert D(run["end_equity"]) == D("97.5")
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == telegram.PAUSED
    assert await bot.ask(500, "/pause") == telegram.ALREADY_PAUSED


async def test_pause_needs_no_chain_at_all(bot, monkeypatch):
    user = linked(bot)
    start_run(bot.env, user)

    async def forbidden(*_args):
        raise AssertionError("pause must not read the chain")

    monkeypatch.setattr(bot.env.chain, "account", forbidden)
    await bot.say(500, "/pause")
    assert bot.status_of(user) == "paused"
    run = bot.env.ctx.db.execute("SELECT * FROM strategy_runs WHERE user_id=?", (user.id,)).fetchone()
    assert (run["status"], run["stop_reason"], run["end_equity"]) == ("stopped", "telegram", None)


async def test_slow_status_reads_never_delay_a_pause(bot, monkeypatch):
    monkeypatch.setattr(telegram, "POLL_S", 0.01)
    monkeypatch.setattr(account_view, "CHAIN_READ_TIMEOUT_S", 0.05)
    slow_user, _ = bot.env.add_user()
    other, _ = bot.env.add_user()
    bot.link(slow_user, 500)
    bot.link(other, 600)
    gate = asyncio.Event()
    original = bot.env.chain.account

    async def slow(granter, subaccount_id):
        if granter == slow_user.inj_address:
            await gate.wait()  # stands in for a stalled RPC
        return await original(granter, subaccount_id)

    monkeypatch.setattr(bot.env.chain, "account", slow)
    stop = asyncio.Event()
    task = asyncio.create_task(bot.worker.run(stop))
    await bot.say(500, "/status")
    await bot.say(600, "/pause")
    assert bot.status_of(other) == "paused"  # the moment the webhook answered
    for _ in range(300):
        if bot.tg.texts(500):
            break
        await asyncio.sleep(0.01)
    assert f"Position: {telegram.UNAVAILABLE_TEXT}" in bot.tg.texts(500)[0]  # the read timed out, the worker moved on
    gate.set()
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_pause_leaves_an_open_position_and_its_stop(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/pause")
    assert bot.env.position(user) is not None
    assert bot.env.stop_orders(user)


async def test_close_does_nothing_until_confirmed(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    assert bot.env.position(user) is not None
    assert bot.env.intent(user)["stage"] == "protected"
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.env.position(user) is None
    assert telegram.CLOSE_DONE in bot.tg.texts(500)
    assert any(method == "answerCallbackQuery" for method, _ in bot.tg.calls)


async def test_close_button_is_single_use(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    await bot.press(500, data)
    await bot.worker.flush()
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == telegram.CLOSE_EXPIRED


async def test_double_press_with_real_workers_closes_once(bot, monkeypatch):
    monkeypatch.setattr(telegram, "POLL_S", 0.01)
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    original, closes = bot.env.svc.close, []

    async def counting(*args, **kwargs):
        closes.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(bot.env.svc, "close", counting)
    stop = asyncio.Event()
    task = asyncio.create_task(bot.worker.run(stop))
    await asyncio.gather(bot.press(500, data), bot.press(500, data), bot.press(500, data))
    await asyncio.sleep(0.3)
    assert len(closes) == 1
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_close_confirm_expires(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    bot.env.clock.advance(telegram.CLOSE_CONFIRM_TTL_MS)
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.env.position(user) is not None
    assert bot.tg.texts(500)[-1] == telegram.CLOSE_EXPIRED


async def test_a_button_issued_while_flat_does_not_close_a_later_position(bot):
    user = linked(bot)
    await bot.ask(500, "/close")
    await bot.env.enter(user)
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.env.position(user) is not None
    assert bot.tg.texts(500)[-1] == telegram.CLOSE_CHANGED


async def test_a_button_for_one_position_does_not_close_the_next_one(bot, monkeypatch):
    monkeypatch.setattr(telegram, "CLOSE_CONFIRM_TTL_MS", 24 * 3_600_000)  # entering again needs later windows
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    await bot.env.close(user)
    bot.env.next_window()
    bot.env.next_window()
    bot.env.next_window()
    assert (await bot.env.enter(user)).outcome is Outcome.FILLED
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.env.position(user) is not None
    assert bot.tg.texts(500)[-1] == telegram.CLOSE_CHANGED


async def test_confirm_from_another_chat_does_nothing(bot):
    user = linked(bot, 500)
    other = linked(bot, 700)
    await bot.env.enter(user)
    await bot.env.enter(other)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    await bot.press(600, data)  # a stranger
    await bot.press(700, data)  # another wallet's own chat
    await bot.worker.flush()
    assert bot.env.position(user) is not None
    assert bot.env.position(other) is not None
    # The real button still works afterwards.
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.env.position(user) is None
    assert bot.env.position(other) is not None


async def test_confirm_is_bound_to_the_chat_it_was_sent_to(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    bot.env.ctx.db.execute("UPDATE telegram_links SET chat_id='800' WHERE user_id=?", (user.id,))
    await bot.press(800, data)  # the wallet moved chats after the button was issued
    await bot.worker.flush()
    assert bot.env.position(user) is not None
    assert bot.tg.texts(800)[-1] == telegram.CLOSE_EXPIRED


async def test_a_pause_does_not_cancel_a_pending_close_button(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    await bot.say(500, "/pause")
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.env.position(user) is None


async def test_close_with_no_position_says_so(bot):
    linked(bot)
    await bot.ask(500, "/close")
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == telegram.CLOSE_NO_POSITION


async def test_close_is_allowed_while_paused_and_under_the_kill_switch(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/pause")
    await bot.env.svc.set_kill_switch(True)
    await bot.ask(500, "/close")
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.env.position(user) is None


async def test_close_with_the_chain_down_says_only_what_is_known(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    bot.env.chain.go_down()
    await bot.press(500, data)
    await bot.worker.flush()
    text = bot.tg.texts(500)[-1]
    assert bot.env.txs(user, purpose="close") == []
    assert "is sent" not in text
    assert text == f"{telegram.CLOSE_UNCONFIRMED} {telegram.STATE_UNREADABLE}"


async def test_close_failed_reports_the_stop_it_actually_read(bot):
    user = linked(bot)
    await bot.env.enter(user)
    stop_price = float(bot.env.intent(user)["stop_price"])
    await bot.env.svc._kv_set(bot.env.svc._close_key(user.id), json.dumps({"ms": bot.env.clock(), "attempts": 3}))
    await bot.ask(500, "/close")
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == f"{telegram.CLOSE_FAILED} {telegram.STATE_STOP.format(stop=telegram._number(stop_price))}"


async def test_close_failed_does_not_claim_a_stop_that_does_not_exist(bot):
    user = linked(bot)
    await bot.env.enter(user)
    bot.env.chain.sub(user.subaccount_id).orders.clear()
    await bot.env.svc._kv_set(bot.env.svc._close_key(user.id), json.dumps({"ms": bot.env.clock(), "attempts": 3}))
    await bot.ask(500, "/close")
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert bot.tg.texts(500)[-1] == f"{telegram.CLOSE_FAILED} {telegram.STATE_NO_STOP}"
    assert "no stop on chain" in await bot.ask(500, "/status")


async def test_status_after_a_close_is_not_a_stale_cache(bot):
    user = linked(bot)
    await bot.env.enter(user)
    assert "Position: long" in await bot.ask(500, "/status")
    await bot.ask(500, "/close")
    await bot.press(500, bot.tg.button(500, telegram.CONFIRM_PREFIX))
    await bot.worker.flush()
    assert "Position: none" in await bot.ask(500, "/status")


async def test_unknown_command_and_non_text(bot):
    linked(bot)
    assert await bot.ask(500, "/open_long now") == telegram.UNKNOWN_COMMAND
    assert await bot.ask(500, None) == telegram.TEXT_ONLY
    assert await bot.ask(500, "/help") == telegram.HELP
    assert await bot.ask(500, "/start") == telegram.HELP
    assert "/unlink" in telegram.HELP
    assert bot.llm.calls == []


async def test_commands_accept_the_bot_mention_form(bot):
    linked(bot)
    assert await bot.ask(500, f"/help@{BOT}") == telegram.HELP


# ---------------------------------------------------------------------- limits per chat


async def test_a_chat_is_limited_to_twenty_messages_a_minute_with_one_warning(bot):
    linked(bot)
    for _ in range(50):
        await bot.say(500, "/help")
    assert bot.env.ctx.db.execute("SELECT COUNT(*) FROM telegram_seen_updates").fetchone()[0] == telegram.COMMANDS_PER_MINUTE
    for _ in range(10):
        await bot.worker.flush()
        bot.env.clock.advance(1000)
    assert bot.tg.texts(500).count(telegram.HELP) == telegram.COMMANDS_PER_MINUTE
    assert bot.tg.texts(500).count(telegram.SLOW_DOWN) == 1


async def test_pause_and_unlink_still_work_past_the_limit(bot):
    user = linked(bot)
    for _ in range(telegram.COMMANDS_PER_MINUTE + 1):
        await bot.say(500, "/help")
    await bot.say(500, "/pause")
    assert bot.status_of(user) == "paused"
    await bot.say(500, "/unlink")
    assert bot.links(user) == []


async def test_a_live_close_button_still_works_past_the_limit(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.ask(500, "/close")
    data = bot.tg.button(500, telegram.CONFIRM_PREFIX)
    for _ in range(telegram.COMMANDS_PER_MINUTE + 1):
        await bot.say(500, "/help")
    await bot.press(500, data)
    await bot.worker.flush()
    assert bot.env.position(user) is None


# ---------------------------------------------------------------------- nothing kills a worker


async def test_a_database_error_on_the_reply_does_not_kill_the_chat_worker(bot, monkeypatch):
    user = linked(bot)
    bot.env.ctx.db.execute("INSERT INTO strategies(user_id,preset,next_run_ms,updated_ms) VALUES(?,'trend',0,0)", (user.id,))

    async def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(notify, "enqueue", broken)
    task = asyncio.create_task(bot.worker._serve_chat())
    await bot.worker._queue_chat(user, "hello")
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()


async def test_a_bad_update_does_not_stop_the_queue(bot, monkeypatch):
    linked(bot)
    original = bot.worker._on_message

    async def fail_once(message_, chat_id):
        monkeypatch.setattr(bot.worker, "_on_message", original)
        raise RuntimeError("boom")

    monkeypatch.setattr(bot.worker, "_on_message", fail_once)
    await bot.say(500, "/help")
    await bot.say(500, "/help")
    await bot.worker.flush()
    assert telegram.SOMETHING_WRONG in bot.tg.texts(500)
    assert telegram.HELP in bot.tg.texts(500)


async def test_a_dead_worker_is_restarted(bot, monkeypatch):
    monkeypatch.setattr(telegram, "POLL_S", 0.01)
    linked(bot)
    stop = asyncio.Event()
    task = asyncio.create_task(bot.worker.run(stop))
    await asyncio.sleep(0.05)
    victim = bot.worker._workers[0]
    victim.cancel()
    await asyncio.sleep(0.1)
    assert victim.done()
    assert bot.worker._workers[0] is not victim
    assert not bot.worker._workers[0].done()
    await bot.say(500, "/help")
    for _ in range(200):
        if bot.tg.texts(500):
            break
        await asyncio.sleep(0.01)
    assert bot.tg.texts(500) == [telegram.HELP]
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_a_poison_outbox_row_does_not_block_the_rest(bot):
    user = linked(bot)
    await telegram._queue_outbox(bot.env.ctx, user.id, "close_prompt", "bad", {"a": 1})
    bot.env.ctx.db.execute("UPDATE telegram_outbox SET reply_markup='{not json'")
    await notify.enqueue(bot.env.ctx, user.id, "reply", "fine")
    assert await bot.worker.flush() == 1
    assert bot.tg.texts(500) == ["fine"]


# ---------------------------------------------------------------------- plain text, secrets


async def test_bot_token_and_webhook_secret_never_reach_logs_or_responses(bot, caplog):
    caplog.set_level(logging.DEBUG)
    user = linked(bot)

    bodies = [(await bot.post(message(1, 500, "/help"), headers={})).text]
    bodies.append((await bot.post(message(2, 500, "/help"), headers={"X-Telegram-Bot-Api-Secret-Token": "bad"})).text)
    bodies.append((await bot.post(message(3, 500, "/help"))).text)
    await bot.worker.flush()

    await notify.enqueue(bot.env.ctx, user.id, "reply", "one")
    bot.tg.respond = lambda _r: httpx.Response(500, text=f"upstream error for {TOKEN}")
    await bot.worker.flush()

    await notify.enqueue(bot.env.ctx, user.id, "reply", "two")
    bot.env.clock.advance(telegram.BACKOFF_MAX_MS)

    def explode(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    bot.tg.respond = explode
    await bot.worker.flush()

    bot.env.clock.advance(telegram.BACKOFF_MAX_MS)
    bot.tg.respond = lambda _r: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}, "description": TOKEN})
    await bot.worker.flush()
    assert "api.telegram.org" in bot.tg.urls[0]  # the requests really happened, with the token in the URL
    assert TOKEN in bot.tg.urls[0]

    # A control: httpx's logging reaches caplog, so the checks below are not vacuous.
    await bot.http.get("https://example.test/control")
    assert "example.test" in caplog.text

    for text in [caplog.text, *bodies]:
        assert TOKEN not in text
        assert SECRET not in text


async def test_real_http_stack_logs_no_token_at_debug(env, caplog, monkeypatch):
    """A real socket and httpcore, local only: MockTransport skips httpcore's own logging."""
    caplog.set_level(logging.DEBUG)

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 11\r\nconnection: close\r\n\r\n{"ok":true}')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(telegram, "API_BASE", f"http://127.0.0.1:{port}")
    make_bot(env)
    client = httpx.AsyncClient(timeout=5, follow_redirects=False)
    worker = telegram.TelegramWorker(env.svc, http=client)
    assert (await worker._send("500", "hello")).ok
    await client.get(f"http://localhost:{port}/control")
    await client.aclose()
    server.close()
    assert "httpcore" in caplog.text and "/control" in caplog.text
    assert TOKEN not in caplog.text


async def test_secrets_are_never_stored(bot):
    user = linked(bot)
    bot.env.ctx.db.execute("INSERT INTO strategies(user_id,preset,next_run_ms,updated_ms) VALUES(?,'trend',0,0)", (user.id,))
    await bot.env.enter(user)
    await bot.ask(500, "/status")
    await bot.ask(500, "free text")
    for table in ("telegram_outbox", "telegram_links", "chat_turns", "alerts", "decisions"):
        dump = str([dict(r) for r in bot.env.ctx.db.execute(f"SELECT * FROM {table}").fetchall()])
        assert TOKEN not in dump
        assert SECRET not in dump


# ---------------------------------------------------------------------- outbox


def per_chat_429(chat, retry_after):
    def respond(request):
        body = json.loads(request.content or b"{}")
        if str(body.get("chat_id")) == str(chat):
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": retry_after}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    return respond


async def test_outbox_delivers_oldest_first(bot):
    user = linked(bot)
    for index in range(3):
        await notify.enqueue(bot.env.ctx, user.id, "reply", f"message {index}")
    assert await bot.worker.flush() == 3
    assert bot.tg.texts(500) == ["message 0", "message 1", "message 2"]
    assert all(row["sent_ms"] == bot.env.clock() for row in bot.outbox(user))
    assert await bot.worker.flush() == 0


async def test_outbox_retries_with_backoff(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "hello")
    bot.tg.respond = lambda _r: httpx.Response(500)
    assert await bot.worker.flush() == 0
    row = bot.outbox(user)[0]
    assert (row["attempts"], row["sent_ms"]) == (1, None)
    assert row["next_try_ms"] == bot.env.clock() + telegram.BACKOFF_BASE_MS
    calls = len(bot.tg.calls)
    assert await bot.worker.flush() == 0
    assert len(bot.tg.calls) == calls  # not due yet
    bot.env.clock.advance(telegram.BACKOFF_BASE_MS)
    bot.tg.respond = OK
    assert await bot.worker.flush() == 1
    assert bot.outbox(user)[0]["sent_ms"] is not None


async def test_a_429_holds_only_that_chat_and_is_not_an_attempt(bot):
    victim = linked(bot, 500)
    spammer = linked(bot, 666)
    await notify.enqueue(bot.env.ctx, spammer.id, "reply", "limited")
    await notify.enqueue(bot.env.ctx, spammer.id, "reply", "limited too")
    await notify.enqueue(bot.env.ctx, victim.id, "stop_hit", "STOP", dedupe_key="s")
    bot.tg.respond = per_chat_429(666, 3600)
    assert await bot.worker.flush() == 1
    assert bot.tg.texts(500) == ["STOP"]
    first = bot.outbox(spammer)[0]
    assert (first["attempts"], first["next_try_ms"]) == (0, bot.env.clock() + 3_600_000)
    # The hold is on chat 666 alone: this new message for another chat goes straight out.
    await notify.enqueue(bot.env.ctx, victim.id, "reply", "after the hold began")
    bot.env.clock.advance(1000)
    assert await bot.worker.flush() == 1
    assert bot.tg.texts(500) == ["STOP", "after the hold began"]
    calls = len(bot.tg.texts(666))
    bot.env.clock.advance(3_598_000)
    await bot.worker.flush()
    assert len(bot.tg.texts(666)) == calls  # still held
    bot.env.clock.advance(1000)
    bot.tg.respond = OK
    assert await bot.worker.flush() == 2
    assert bot.tg.texts(666)[-2:] == ["limited", "limited too"]


async def test_a_strangers_429_does_not_silence_an_owner(bot):
    user = linked(bot)
    await bot.env.enter(user)
    bot.tg.respond = per_chat_429(666, 3600)
    await bot.say(666, "hi")
    await bot.say(500, "/close")
    await bot.worker.flush()
    assert telegram.CLOSE_PROMPT in bot.tg.texts(500)
    assert bot.outbox(user)[0]["sent_ms"] is not None


async def test_429s_never_use_up_a_notice(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "held back")
    bot.tg.respond = lambda _r: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
    for _ in range(20):
        await bot.worker.flush()
        bot.env.clock.advance(1000)
    assert bot.outbox(user)[0]["attempts"] == 0
    bot.tg.respond = OK
    assert await bot.worker.flush() == 1


async def test_total_sends_are_paced_to_twenty_five_a_second(bot):
    users = [linked(bot, 1000 + i) for i in range(30)]
    for user in users:
        await notify.enqueue(bot.env.ctx, user.id, "reply", "hi")
    assert await bot.worker.flush() == telegram.SENDS_PER_SECOND
    assert await bot.worker.flush() == 0  # the same second
    bot.env.clock.advance(1000)
    assert await bot.worker.flush() == 5
    assert all(row["attempts"] == 1 for row in bot.outbox())


async def test_trade_notices_go_first_and_chats_take_turns(bot):
    first = linked(bot, 500)
    second = linked(bot, 600)
    for index in range(4):
        await notify.enqueue(bot.env.ctx, first.id, "reply", f"first {index}")
    await notify.enqueue(bot.env.ctx, second.id, "reply", "second 0")
    await notify.enqueue(bot.env.ctx, second.id, "stop_hit", "second trade", dedupe_key="closed:7")
    await bot.worker.flush()
    assert [body["text"] for body in bot.tg.sends()][:3] == ["second trade", "first 0", "second 0"]


async def test_a_spammers_rows_cannot_keep_a_notice_undelivered(bot):
    victim = linked(bot, 500)
    spammer = linked(bot, 666)
    for _ in range(40):
        await notify.enqueue(bot.env.ctx, spammer.id, "reply", "spam")
    await notify.enqueue(bot.env.ctx, victim.id, "position_flattened", "FLATTENED", dedupe_key="f")
    bot.tg.respond = per_chat_429(666, 5)
    await bot.worker.flush()
    assert bot.tg.texts(500) == ["FLATTENED"]


async def test_a_trade_notice_retries_for_a_day_then_raises_an_alert(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "stop_hit", "Stop hit.", dedupe_key="closed:1")
    bot.tg.respond = lambda _r: httpx.Response(502)
    start = bot.env.clock()
    for _ in range(40):
        await bot.worker.flush()
        bot.env.clock.advance(telegram.BACKOFF_MAX_MS)
    assert bot.outbox(user)[0]["attempts"] > telegram.MAX_ATTEMPTS  # well past where a reply would stop
    assert bot.outbox(user)[0]["dropped_ms"] is None
    assert bot.env.alerts("telegram_notice_dropped") == []
    bot.tg.respond = OK
    assert await bot.worker.flush() == 1  # recovered inside the day
    assert bot.outbox(user)[0]["sent_ms"] is not None

    await notify.enqueue(bot.env.ctx, user.id, "position_closed", "Closed.", dedupe_key="closed:2")
    bot.tg.respond = lambda _r: httpx.Response(502)
    bot.env.clock.set(start + telegram.TRADE_NOTICE_TTL_MS * 3)
    await bot.worker.flush()
    row = bot.outbox(user)[1]
    bot.env.clock.advance(telegram.TRADE_NOTICE_TTL_MS)
    await bot.worker.flush()
    row = bot.outbox(user)[1]
    assert row["sent_ms"] is None and row["dropped_ms"] is not None
    alerts = bot.env.alerts("telegram_notice_dropped")
    assert len(alerts) == 1 and alerts[0]["user_id"] == user.id


async def test_a_trade_notice_held_only_by_429s_is_dropped_with_an_alert_after_a_day(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "position_opened", "Opened.", dedupe_key="opened:1")
    bot.tg.respond = lambda _r: httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 60}})
    await bot.worker.flush()
    bot.env.clock.advance(telegram.TRADE_NOTICE_TTL_MS)
    await bot.worker.flush()
    assert bot.outbox(user)[0]["dropped_ms"] is not None
    assert len(bot.env.alerts("telegram_notice_dropped")) == 1


async def test_outbox_drops_a_reply_after_eight_attempts_without_an_alert(bot, caplog):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "doomed")
    bot.tg.respond = lambda _r: httpx.Response(500)
    for attempt in range(1, 9):
        assert await bot.worker.flush() == 0
        assert bot.outbox(user)[0]["attempts"] == attempt
        assert len(bot.tg.calls) == attempt
        bot.env.clock.advance(telegram.BACKOFF_MAX_MS)
    assert "dropped after 8 attempts" in caplog.text
    bot.tg.respond = OK
    assert await bot.worker.flush() == 0
    assert len(bot.tg.calls) == 8
    row = bot.outbox(user)[0]
    assert (row["sent_ms"], row["dropped_ms"] is not None) == (None, True)
    assert bot.env.alerts("telegram_notice_dropped") == []


async def test_the_link_is_rechecked_before_every_send(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "one")
    await notify.enqueue(bot.env.ctx, user.id, "reply", "two")
    calls = []

    def respond(_request):
        calls.append(1)
        if len(calls) == 1:  # the wallet unlinks while the pass is under way
            bot.env.ctx.db.execute("DELETE FROM telegram_links WHERE user_id=?", (user.id,))
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot.tg.respond = respond
    await bot.worker.flush()
    assert bot.tg.texts(500) == ["one"]


async def test_a_message_for_a_chat_that_unlinked_is_never_sent(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "stale")
    bot.env.ctx.db.execute("DELETE FROM telegram_links WHERE user_id=?", (user.id,))
    assert await bot.worker.flush() == 0
    assert bot.tg.calls == []
    assert bot.outbox(user)[0]["dropped_ms"] is not None
    assert bot.env.alerts("telegram_notice_dropped") == []


async def test_one_failing_chat_does_not_block_the_others(bot):
    first = linked(bot, 500)
    second = linked(bot, 600)
    await notify.enqueue(bot.env.ctx, first.id, "reply", "fails")
    await notify.enqueue(bot.env.ctx, second.id, "reply", "works")
    bot.tg.respond = lambda request: httpx.Response(403) if b'"500"' in request.content else httpx.Response(200, json={"ok": True})
    assert await bot.worker.flush() == 1
    assert bot.tg.texts(600) == ["works"]


async def test_redirects_are_not_followed(bot):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, "reply", "hello")
    bot.tg.respond = lambda _r: httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    assert await bot.worker.flush() == 0
    assert len(bot.tg.calls) == 1
    assert not any("evil.example" in url for url in bot.tg.urls)


def test_the_default_http_client_follows_no_redirects(env):
    worker = telegram.TelegramWorker(env.svc)
    assert worker._http.follow_redirects is False


async def test_old_rows_are_purged_after_seven_days(bot):
    user = linked(bot)
    db, now = bot.env.ctx.db, bot.env.clock()
    old = now - telegram.PURGE_AGE_MS - 1
    db.execute("INSERT INTO chat_turns(user_id,role,content,created_ms) VALUES(?,'user','old',?)", (user.id, old))
    db.execute("INSERT INTO chat_turns(user_id,role,content,created_ms) VALUES(?,'user','new',?)", (user.id, now))
    db.execute("INSERT INTO telegram_seen_updates(update_id,created_ms) VALUES(1,?)", (old,))
    db.execute("INSERT INTO telegram_seen_updates(update_id,created_ms) VALUES(2,?)", (now,))
    db.execute("INSERT INTO telegram_outbox(user_id,kind,text,created_ms,sent_ms) VALUES(?,'reply','x',?,?)", (user.id, old, old))
    db.execute("INSERT INTO telegram_outbox(user_id,kind,text,attempts,dropped_ms,created_ms) VALUES(?,'reply','y',8,?,?)", (user.id, old, old))
    db.execute("INSERT INTO telegram_link_codes(code_hash,user_id,expires_ms) VALUES('h',?,?)", (user.id, old))
    await bot.worker.flush()
    assert [r["content"] for r in db.execute("SELECT content FROM chat_turns")] == ["new"]
    assert [r["update_id"] for r in db.execute("SELECT update_id FROM telegram_seen_updates")] == [2]
    assert db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM telegram_link_codes").fetchone()[0] == 0


async def test_run_serves_updates_and_stops_cleanly(bot, monkeypatch):
    monkeypatch.setattr(telegram, "POLL_S", 0.01)
    linked(bot)
    stop = asyncio.Event()
    task = asyncio.create_task(bot.worker.run(stop))
    await bot.say(500, "/help")
    for _ in range(300):
        if bot.tg.texts(500):
            break
        await asyncio.sleep(0.01)
    assert bot.tg.texts(500) == [telegram.HELP]
    stop.set()
    await asyncio.wait_for(task, 2)


# ---------------------------------------------------------------------- trade notifications


def kinds(bot, user):
    return [row["kind"] for row in bot.outbox(user)]


async def test_open_and_close_are_pushed_once_each(bot):
    user = linked(bot)
    out = await bot.env.enter(user)
    assert out.outcome is Outcome.FILLED
    assert kinds(bot, user) == ["position_opened"]
    intent = bot.env.intent(user)
    opened = bot.outbox(user)[0]
    assert opened["dedupe_key"] == f"opened:{intent['id']}"
    assert "long" in opened["text"] and "Stop at" in opened["text"]
    assert "Stop at 59400." in opened["text"]

    await bot.env.close(user)
    assert kinds(bot, user) == ["position_opened", "position_closed"]
    closed = bot.outbox(user)[1]
    assert closed["dedupe_key"] == f"closed:{intent['id']}"
    assert "P&L:" in closed["text"]


async def test_replayed_transitions_do_not_push_twice(bot):
    user = linked(bot)
    await bot.env.enter(user)
    intent_id = bot.env.intent(user)["id"]
    # A crash after the push but before the result was stored: the watcher records it again.
    bot.env.ctx.db.execute("UPDATE intents SET result_json=NULL WHERE id=?", (intent_id,))
    await bot.env.svc._record_result(bot.env.svc.intent(intent_id))
    assert kinds(bot, user) == ["position_opened"]

    await bot.env.close(user)
    account = await bot.env.chain.account(user.inj_address, user.subaccount_id)
    await bot.env.svc.finalize_flat(bot.env.svc.intent(intent_id), reason="close", account=account)
    assert kinds(bot, user) == ["position_opened", "position_closed"]


async def test_stop_hit_is_pushed_with_pnl(bot):
    user = linked(bot)
    await bot.env.enter(user)
    bot.env.chain.advance_block(mark=D("59000"))
    await bot.env.settle()
    rows = bot.outbox(user)
    assert [r["kind"] for r in rows] == ["position_opened", "stop_hit"]
    assert "Stop hit" in rows[1]["text"] and "P&L: -$" in rows[1]["text"]
    assert rows[1]["dedupe_key"] == f"closed:{bot.env.intent(user)['id']}"


async def test_take_profit_hit_is_pushed(bot):
    user = linked(bot)
    await bot.env.enter(user, tp=3.0)
    bot.env.chain.advance_block(mark=D("61000"))
    await bot.env.settle()
    rows = bot.outbox(user)
    assert [r["kind"] for r in rows] == ["position_opened", "take_profit_hit"]
    assert "P&L: +$" in rows[1]["text"]


async def test_failed_stop_and_flatten_is_pushed(bot):
    user = linked(bot)
    bot.env.chain.inject(Failure.STOP_PLACEMENT, count=2)
    out = await bot.env.enter(user)
    assert out.outcome is Outcome.FAILED
    rows = bot.outbox(user)
    assert [r["kind"] for r in rows] == ["position_flattened"]
    assert "stop could not be placed" in rows[0]["text"]


async def test_pnl_that_cannot_be_computed_says_so(bot):
    user = linked(bot)
    await bot.env.enter(user)
    bot.env.ctx.db.execute("UPDATE intents SET equity_before=NULL WHERE user_id=?", (user.id,))
    await bot.env.close(user)
    assert "could not be read" in bot.outbox(user)[-1]["text"]
    assert "$0" not in bot.outbox(user)[-1]["text"]


async def test_no_push_without_a_link(bot):
    user, _ = bot.env.add_user()
    await bot.env.enter(user)
    await bot.env.close(user)
    assert bot.outbox(user) == []


async def test_failing_notify_never_breaks_a_trade(bot, monkeypatch, caplog):
    user = linked(bot)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("notify is broken")

    monkeypatch.setattr(notify, "enqueue", boom)
    out = await bot.env.enter(user)
    assert out.outcome is Outcome.FILLED
    assert bot.env.intent(user)["stage"] == "protected"
    assert bot.env.position(user) is not None
    closed = await bot.env.close(user)
    assert closed.outcome is Outcome.CLOSED
    assert bot.env.position(user) is None
    assert bot.env.intent(user)["stage"] == "closed"
    assert bot.env.trades(user)[0]["closed_ms"] is not None
    assert "notification position_opened failed" in caplog.text
    assert "notification position_closed failed" in caplog.text


# ---------------------------------------------------------------------- webhook registration


def load_setup_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "telegram_setup.py"
    spec = importlib.util.spec_from_file_location("telegram_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setup_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def test_setup_script_registers_the_webhook(bot):
    script = load_setup_script()
    settings = dataclasses.replace(bot.env.ctx.settings, public_base_url="https://bot.example")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content), path=request.url.path)
        return httpx.Response(200, json={"ok": True, "result": True})

    assert await script.register(settings, http=setup_client(handler)) == "ok"
    assert seen["path"] == f"/bot{TOKEN}/setWebhook"
    assert seen["url"] == "https://bot.example/telegram/webhook"
    assert seen["secret_token"] == SECRET
    assert seen["allowed_updates"] == ["message", "callback_query"]
    assert seen["drop_pending_updates"] is True


async def test_setup_script_error_codes_carry_no_secrets(bot):
    script = load_setup_script()
    settings = dataclasses.replace(bot.env.ctx.settings, public_base_url="https://bot.example")

    def explode(request):
        raise httpx.ConnectError(f"no route to {request.url}")

    assert await script.register(settings, http=setup_client(explode)) == "TELEGRAM_UNREACHABLE"
    assert await script.register(settings, http=setup_client(lambda _r: httpx.Response(401, text=TOKEN))) == "TELEGRAM_HTTP_401"
    assert await script.register(settings, http=setup_client(lambda _r: httpx.Response(200, json={"ok": False, "description": TOKEN}))) == "TELEGRAM_REJECTED"
    assert await script.register(dataclasses.replace(settings, public_base_url="http://bot.example")) == "BASE_URL_NOT_HTTPS"
    assert await script.register(dataclasses.replace(settings, telegram_bot_token="")) == "NOT_CONFIGURED"


def test_setup_script_prints_only_a_code(monkeypatch, capsys):
    script = load_setup_script()
    monkeypatch.setenv("HALO_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("HALO_TELEGRAM_BOT_USERNAME", BOT)
    monkeypatch.setenv("HALO_TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HALO_PUBLIC_BASE_URL", "http://not-https.example")
    assert script.main() == 1
    captured = capsys.readouterr()
    logging.disable(logging.NOTSET)
    assert captured.out.strip() == "BASE_URL_NOT_HTTPS"
    assert TOKEN not in captured.out + captured.err
    assert SECRET not in captured.out + captured.err


# ---------------------------------------------------------------------- sound, reasons, the daily summary
DAY = 86_400_000


def sends_of(bot, chat_id=500):
    return bot.tg.sends(chat_id)


def decision_row(bot, user, outcome, *, reason="", side=None, stop_atr=None, created_ms=None, code=None):
    bot.env.ctx.db.execute(
        "INSERT INTO agent_decisions(user_id,window_id,preset,outcome,side,stop_atr,reason,code,created_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (user.id, 1, "trend", outcome, side, stop_atr, reason, code, bot.env.clock() if created_ms is None else created_ms),
    )


async def test_trade_events_make_a_sound_and_replies_do_not(bot):
    user = linked(bot)
    await bot.env.enter(user)
    await bot.worker.flush()
    opened = sends_of(bot)[-1]
    assert "Opened" in opened["text"] and "disable_notification" not in opened

    await notify.enqueue(bot.env.ctx, user.id, "reply", "Here you go.")
    await bot.worker.flush()
    assert sends_of(bot)[-1]["disable_notification"] is True


@pytest.mark.parametrize("kind", ["stop_hit", "take_profit_hit", "position_flattened", "agent_blocked", "agent_no_decision", "agent_paused_grant_lost", "agent_paused_balance_low", "grant_expiring", "agent_close_failed"])
async def test_risk_events_make_a_sound(bot, kind):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, kind, "Something needs you.")
    await bot.worker.flush()
    assert "disable_notification" not in sends_of(bot)[-1]


@pytest.mark.parametrize("kind", ["reply", "agent_unblocked", "daily_summary", "disconnected"])
async def test_routine_messages_are_silent(bot, kind):
    user = linked(bot)
    await notify.enqueue(bot.env.ctx, user.id, kind, "For your information.")
    await bot.worker.flush()
    assert sends_of(bot)[-1]["disable_notification"] is True


async def test_a_kind_nobody_listed_makes_a_sound(bot):
    """A new risk event must not go quiet because someone forgot this list."""
    assert notify.is_routine("some_new_risk_event") is False


async def test_an_opened_notice_carries_the_agents_reason_on_one_line(bot):
    user = linked(bot)
    decision_row(bot, user, "unknown", reason="Trend is up\nand\tmomentum agrees.", side="long", stop_atr="2.0")
    await bot.env.enter(user)
    text = bot.outbox(user)[0]["text"]
    assert text.endswith("\nAgent's stated reason: Trend is up and momentum agrees.")
    assert text.count("\n") == 1


async def test_a_long_reason_is_cut_short(bot):
    user = linked(bot)
    decision_row(bot, user, "unknown", reason="x" * 400, side="long", stop_atr="2.0")
    await bot.env.enter(user)
    line = bot.outbox(user)[0]["text"].split("Agent's stated reason: ")[1]
    assert len(line) == notify.REASON_MAX_CHARS and line.endswith("...")


async def test_a_close_by_the_agent_carries_its_reason(bot):
    user = linked(bot)
    await bot.env.enter(user)
    decision_row(bot, user, "unknown", reason="The trend turned.")
    await bot.env.close(user)
    assert "Agent's stated reason: The trend turned." in bot.outbox(user)[1]["text"]


async def test_a_stop_hit_claims_no_agent_reason(bot):
    """Nobody decided a stop, so an earlier decision must not be quoted as the reason for it."""
    user = linked(bot)
    decision_row(bot, user, "unknown", reason="The trend turned.")
    await bot.env.enter(user)
    bot.env.chain.advance_block(mark=D("59000"))
    await bot.env.settle()
    assert "stated reason" not in bot.outbox(user)[-1]["text"]


async def test_an_old_decision_is_not_quoted_on_a_later_close(bot):
    user = linked(bot)
    await bot.env.enter(user)
    decision_row(bot, user, "closed", reason="An old close.", created_ms=bot.env.clock() - notify.REASON_RECENT_MS - 1)
    await bot.env.close(user)
    assert "stated reason" not in bot.outbox(user)[1]["text"]


def at_start_of_day(bot) -> int:
    now = bot.env.clock() // DAY * DAY + 3_600_000
    bot.env.clock.set(now + DAY)
    return now


async def test_the_daily_summary_counts_what_happened_yesterday(bot):
    user = linked(bot)
    day = at_start_of_day(bot)
    for _ in range(3):
        decision_row(bot, user, "hold", created_ms=day)
    decision_row(bot, user, "entered", side="long", created_ms=day)
    decision_row(bot, user, "no_decision", code="LLM_TIMEOUT", created_ms=day)
    bot.env.ctx.db.execute(
        "INSERT INTO trades(user_id,side,quantity,entry_price,exit_price,pnl_usd,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (user.id, "long", "0.01", "60000", "60100", "1.24", "take_profit", day, day + 1000),
    )
    await bot.worker.flush()
    rows = [row for row in bot.outbox(user) if row["kind"] == "daily_summary"]
    assert len(rows) == 1
    assert rows[0]["text"] == "Yesterday (UTC): 4 checks, 1 trade closed, about +$1.24, 1 check failed."


async def test_the_daily_summary_goes_out_once_per_day(bot):
    user = linked(bot)
    day = at_start_of_day(bot)
    decision_row(bot, user, "entered", side="long", created_ms=day)
    await bot.worker.flush()
    await bot.worker.flush()
    # A restart forgets the day it last ran, and the key still holds.
    bot.worker._summary_day = None
    await bot.worker.flush()
    assert [row["kind"] for row in bot.outbox(user)].count("daily_summary") == 1


async def test_a_day_of_only_holds_sends_no_summary(bot):
    user = linked(bot)
    day = at_start_of_day(bot)
    for _ in range(96):
        decision_row(bot, user, "hold", created_ms=day)
    await bot.worker.flush()
    assert bot.outbox(user) == []


async def test_a_trade_with_no_recorded_result_is_not_summed_as_zero(bot):
    user = linked(bot)
    day = at_start_of_day(bot)
    bot.env.ctx.db.execute(
        "INSERT INTO trades(user_id,side,quantity,entry_price,close_reason,opened_ms,closed_ms) VALUES(?,?,?,?,?,?,?)",
        (user.id, "long", "0.01", "60000", "external", day, day + 1000),
    )
    await bot.worker.flush()
    text = [row for row in bot.outbox(user) if row["kind"] == "daily_summary"][0]["text"]
    assert "result unavailable" in text and "$0" not in text


async def test_the_summary_stays_with_its_own_wallet(bot):
    first = linked(bot, chat_id=500)
    second = linked(bot, chat_id=501)
    day = at_start_of_day(bot)
    decision_row(bot, first, "entered", side="long", created_ms=day)
    await bot.worker.flush()
    assert [row["kind"] for row in bot.outbox(first)] == ["daily_summary"]
    assert bot.outbox(second) == []


async def test_todays_activity_waits_for_tomorrows_summary(bot):
    user = linked(bot)
    decision_row(bot, user, "entered", side="long")
    await bot.worker.flush()
    assert bot.outbox(user) == []
