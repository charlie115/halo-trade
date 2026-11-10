"""Adversarial proofs from the 2026-11-09 security review, turned around: each test fails without
its fix and passes with it. Everything runs through `create_app`.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import socket
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx
import pytest
import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from halo_trade import onboarding
from halo_trade.api import MAX_BODY_BYTES, create_app, PATH_BODY_LIMITS
from halo_trade.auth import RateLimiter
from halo_trade.chain.mock import MockChain, MockClock
from halo_trade.config import Settings
from halo_trade.context import AppContext
from halo_trade.db import connect
from halo_trade.startup import ConfigError, parse_trusted_proxies, validate_settings

START = 2_000_000 * 900_000 + 60_000
REPO = Path(__file__).resolve().parents[1]


class Lab:
    def __init__(self, max_users: int = 20, **settings_kw) -> None:
        self.clock = MockClock(START)
        self.chain = MockChain(clock=self.clock, auto_mine=True)
        settings_kw.setdefault("public_base_url", "https://halo.test")
        self.ctx = AppContext(
            settings=Settings(network="testnet", max_users=max_users, db_path=":memory:", **settings_kw),
            db=connect(":memory:"),
            chain=self.chain,
            clock=self.clock,
        )
        self.app = create_app(self.ctx)
        self.client = self.client_from("127.0.0.1")

    def client_from(self, address: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False, client=(address, 123))
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    def fund(self, wallet, amount: str = "50", granted: bool = True) -> None:
        eth = wallet.address.lower()
        self.chain.register_account(onboarding.eth_to_inj(eth), onboarding.subaccount_id(eth), Decimal(amount), granted=granted)

    async def start(self, wallet_or_address, action: str = "onboard", client: Optional[httpx.AsyncClient] = None) -> httpx.Response:
        address = getattr(wallet_or_address, "address", wallet_or_address)
        preset = {"preset": "trend"} if action == "resume" else {}
        return await (client or self.client).post("/onboard/start", json={"eth_address": address, "action": action, **preset})

    async def session(self, wallet) -> str:
        challenge = (await self.start(wallet)).json()
        verified = await self.client.post("/onboard/verify", json={"eth_address": wallet.address, "signature": sign(wallet, challenge["message"])})
        assert verified.status_code == 200, verified.text
        return verified.json()["session"]

    async def complete(self, wallet) -> httpx.Response:
        response = await self.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": await self.session(wallet)})
        assert response.status_code == 200, response.text
        return response

    async def onboard(self, wallet, amount: str = "50") -> str:
        self.fund(wallet, amount)
        await self.complete(wallet)
        return self.issue_token(wallet)

    def issue_token(self, wallet) -> str:
        """No route issues a token now, but the agent API is still there and must keep its rules."""
        token = "ht_" + secrets.token_urlsafe(32)
        self.ctx.db.execute("UPDATE users SET token_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), self.user_id(wallet)))
        return token

    async def start_agent(self, wallet) -> None:
        self.ctx.llm = self.ctx.llm or object()
        challenge = (await self.start(wallet, "resume")).json()
        response = await self.client.post("/onboard/resume", json={"eth_address": wallet.address, "signature": sign(wallet, challenge["message"]), "preset": "trend"})
        assert response.status_code == 200, response.text

    def user_id(self, wallet) -> int:
        inj = onboarding.eth_to_inj(wallet.address.lower())
        return self.ctx.db.execute("SELECT id FROM users WHERE inj_address=?", (inj,)).fetchone()["id"]

    def set_equity(self, wallet, equity: str) -> None:
        self.ctx.db.execute(
            "INSERT OR REPLACE INTO equity_snapshots(user_id,ts_ms,equity,flat) VALUES(?,?,?,1)",
            (self.user_id(wallet), self.clock() + 1, equity),
        )


def sign(wallet, message: str) -> str:
    return "0x" + wallet.sign_message(encode_defunct(text=message)).signature.hex().removeprefix("0x")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def status_of(lab: Lab, wallet, session: str) -> httpx.Response:
    return await lab.client.post("/onboard/status", json={"eth_address": wallet.address, "session": session})


async def signed(lab: Lab, wallet, action: str, message: str) -> httpx.Response:
    return await lab.client.post(f"/onboard/{action}", json={"eth_address": wallet.address, "signature": sign(wallet, message)})


@pytest.fixture
def lab() -> Lab:
    return Lab()


# ---------------------------------------------------------------- 1. targeted lock-out
async def test_a_strangers_start_leaves_a_verified_session_alone(lab):
    wallet = Account.create()
    lab.fund(wallet)
    session = await lab.session(wallet)
    # The attacker knows only the public address: no auth, no signature.
    await lab.start(wallet.address, "onboard", client=lab.client_from("10.0.0.9"))
    await lab.start(wallet.address, "revoke", client=lab.client_from("10.0.0.9"))
    assert (await status_of(lab, wallet, session)).status_code == 200


@pytest.mark.parametrize("action,other", [("revoke", "pause"), ("pause", "revoke"), ("revoke", "onboard")])
async def test_a_strangers_start_for_another_action_cannot_cancel_a_signature(lab, action, other):
    wallet = Account.create()
    token = await lab.onboard(wallet)
    challenge = (await lab.start(wallet, action)).json()
    # The real user is reading MetaMask while the attacker starts a different action.
    await lab.start(wallet.address, other, client=lab.client_from("10.0.0.9"))
    response = await signed(lab, wallet, action, challenge["message"])
    assert response.status_code == 200, response.text
    if action == "revoke":
        assert (await lab.client.get("/v1/state", headers=auth(token))).status_code == 401


async def test_a_new_session_does_not_kill_a_pending_revoke_and_vice_versa(lab):
    wallet = Account.create()
    await lab.onboard(wallet)
    revoke = (await lab.start(wallet, "revoke")).json()
    session = await lab.session(wallet)
    assert (await signed(lab, wallet, "revoke", revoke["message"])).status_code == 200
    assert (await status_of(lab, wallet, session)).status_code == 401  # a revoke ends the session too


async def test_a_challenge_is_reused_within_its_action_only(lab):
    wallet = Account.create()
    first = (await lab.start(wallet, "pause")).json()
    second = (await lab.start(wallet, "pause")).json()
    other = (await lab.start(wallet, "resume")).json()
    assert first["nonce"] == second["nonce"]
    assert first["nonce"] != other["nonce"]


# ---------------------------------------------------------------- 2. flooding the challenge table
async def test_a_full_challenge_table_never_blocks_a_wallet_already_in_users(lab, monkeypatch):
    monkeypatch.setattr(onboarding, "START_PER_MIN", 10**9)
    victim = Account.create()
    token = await lab.onboard(victim)
    assert onboarding.MAX_PENDING_NONCES == 5000
    for index in range(onboarding.MAX_PENDING_NONCES):
        assert (await lab.start("0x" + f"{index + 1:040x}")).status_code == 200
    # New addresses are shed, but the victim can still pause and revoke.
    shed = await lab.start("0x" + "f" * 40)
    assert shed.status_code == 429 and shed.json()["code"] == "TOO_MANY_PENDING"
    challenge = (await lab.start(victim, "revoke")).json()
    assert (await signed(lab, victim, "revoke", challenge["message"])).status_code == 200
    assert (await lab.client.get("/v1/state", headers=auth(token))).status_code == 401


async def test_start_is_limited_per_client_and_only_for_new_addresses(lab):
    known = Account.create()
    await lab.onboard(known, "100")  # exempt once in `users`, however exhausted its client is
    limit = onboarding.START_PER_MIN
    client = lab.client_from("10.0.0.3")
    for index in range(limit):
        assert (await lab.start("0x" + f"{index + 1:040x}", client=client)).status_code == 200
    limited = await lab.start("0x" + "e" * 40, client=client)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
    # Another client still gets through, so one attacker cannot lock out everyone.
    assert (await lab.start("0x" + "e" * 40, client=lab.client_from("10.0.0.2"))).status_code == 200
    # A known wallet is never limited, even from the exhausted client.
    assert (await lab.start(known.address, "pause", client=client)).status_code == 200


async def test_expired_challenges_and_sessions_are_purged(lab):
    wallet = Account.create()
    lab.fund(wallet)
    await lab.start(wallet)
    await lab.session(Account.create())
    lab.clock.set(START + onboarding.NONCE_TTL_MS + 1)
    await lab.start(Account.create())  # expired challenges go on the next start
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM onboard_challenges").fetchone()[0] == 1
    lab.clock.set(START + onboarding.SESSION_TTL_MS + 1)
    await lab.session(Account.create())  # expired sessions go on the next verify
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM onboard_sessions").fetchone()[0] == 1


async def test_a_verified_session_does_not_hold_a_challenge_row(lab):
    await lab.session(Account.create())
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM onboard_challenges").fetchone()[0] == 0


# ---------------------------------------------------------------- 3. body size
class _Probe:
    """Tells the test whether the app behind the middleware was reached."""

    def __init__(self, lab: Lab) -> None:
        self.calls = 0
        original = lab.chain.market

        async def counting(*args, **kwargs):
            self.calls += 1
            return await original(*args, **kwargs)

        lab.chain.market = counting  # type: ignore[assignment]


async def test_a_declared_oversized_body_gets_413_without_reading_it(lab):
    response = await lab.client.post(
        "/onboard/start",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


async def test_only_the_telegram_webhook_gets_the_larger_body_limit(lab):
    limit = PATH_BODY_LIMITS["/telegram/webhook"]
    assert limit == 256 * 1024 > MAX_BODY_BYTES
    # Past the normal limit it reaches the route, which is 404 because no bot is configured here.
    response = await lab.client.post("/telegram/webhook", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 404
    response = await lab.client.post("/telegram/webhook", content=b"x" * (limit + 1))
    assert response.status_code == 413


async def test_a_chunked_body_over_the_limit_gets_413_and_is_not_read_on(lab):
    sent = 0

    async def chunks():
        nonlocal sent
        for _ in range(4096):  # 4096 x 8 KB = 32 MB if the server kept reading
            sent += 1
            yield b"a" * 8192

    response = await lab.client.post("/onboard/start", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["limit"] == f"max_body_bytes={MAX_BODY_BYTES}"
    assert sent <= 3  # stopped at the first chunk past 16 KB


async def test_a_body_at_the_limit_reaches_the_application(lab):
    # Exactly 16 KB of JSON: the application sees it and rejects it for its content, not its size.
    shell = '{"eth_address": ""}'
    body = '{"eth_address": "' + "x" * (MAX_BODY_BYTES - len(shell)) + '"}'
    assert len(body) == MAX_BODY_BYTES
    response = await lab.client.post("/onboard/start", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_a_chunked_body_over_the_limit_is_cut_off_by_the_real_server():
    lab = Lab()
    config = uvicorn.Config(lab.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(
                b"POST /onboard/start HTTP/1.1\r\nHost: halo.test\r\nContent-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            frame = b"2000\r\n" + b"a" * 8192 + b"\r\n"
            for _ in range(3):  # 24 KB: past the 16 KB limit, all of it read before the 413
                sock.sendall(frame)
            response = sock.recv(65536)
            status_line = response.split(b"\r\n", 1)[0]
            assert b" 413 " in status_line, status_line
            sock.settimeout(5)
            assert sock.recv(65536) == b""  # the server hangs up; the rest of the body is never read
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _strict_models() -> list[type]:
    return [
        cls
        for cls in vars(onboarding).values()
        if isinstance(cls, type) and issubclass(cls, onboarding._Strict) and cls is not onboarding._Strict
    ]


FREE_TEXT = {("ChatIn", "text"): 2000}


def test_every_onboarding_string_field_has_a_max_length():
    models = _strict_models()
    assert len(models) >= 4
    for model in models:
        for name, field in model.model_fields.items():
            if field.annotation is str:
                lengths = [m.max_length for m in field.metadata if getattr(m, "max_length", None) is not None]
                # A chat message is the one field a person writes freely, so it has the assistant's own cap.
                limit = FREE_TEXT.get((model.__name__, name), 256)
                assert lengths and lengths[0] <= limit, f"{model.__name__}.{name} has no max_length"


def test_compose_restarts_the_server():
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    server = text[text.index("server:"):text.index("test:")]
    assert "restart: unless-stopped" in server


# ---------------------------------------------------------------- 4. Sign-In with Ethereum
SIWE_STATEMENT_RE = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;= ]+")


def parse_siwe(message: str) -> dict:
    """The EIP-4361 layout, field by field, in the required order."""
    lines = message.split("\n")
    assert lines[0].endswith(" wants you to sign in with your Ethereum account:"), lines[0]
    assert lines[2] == "" and lines[4] == ""
    fields = dict(line.split(": ", 1) for line in lines[5:])
    return {
        "domain": lines[0].split(" wants you", 1)[0],
        "address": lines[1],
        "statement": lines[3],
        "keys": [line.split(": ", 1)[0] for line in lines[5:]],
        **fields,
    }


async def test_start_returns_a_real_eip4361_message(lab):
    wallet = Account.create()
    info = (await lab.start(wallet)).json()
    siwe = parse_siwe(info["message"])
    assert siwe["domain"] == "halo.test"
    assert siwe["address"] == to_checksum_address(wallet.address)  # EIP-55 or wallets refuse the format
    assert siwe["URI"] == "https://halo.test"
    assert siwe["Version"] == "1"
    assert siwe["Chain ID"] == "11155111"
    assert re.fullmatch(r"[0-9a-zA-Z]{8,}", siwe["Nonce"])
    assert siwe["Request ID"] == "onboard"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", siwe["Issued At"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", siwe["Expiration Time"])
    assert siwe["keys"] == ["URI", "Version", "Chain ID", "Nonce", "Issued At", "Expiration Time", "Request ID"]
    assert SIWE_STATEMENT_RE.fullmatch(siwe["statement"]), siwe["statement"]
    assert "\n" not in siwe["statement"]


async def test_the_onboard_statement_proves_the_wallet_and_names_no_token(lab):
    message = (await lab.start(Account.create())).json()["message"]
    assert "proves this wallet is yours" in message
    assert "Only sign this on this site" in message
    assert "token" not in message.lower()


async def test_the_resume_statement_says_what_the_agent_can_and_cannot_do(lab):
    statement = parse_siwe((await lab.start(Account.create(), "resume")).json()["message"])["statement"]
    assert "Start the halo-trade AI agent" in statement
    assert "can open and close positions" in statement and "trading subaccount" in statement
    assert "cannot withdraw" in statement
    assert "Trend strategy on BTC/USDC PERP" in statement and "lose your whole deposit" in statement
    assert SIWE_STATEMENT_RE.fullmatch(statement), statement


async def test_the_revoke_statement_says_what_stays_behind(lab):
    statement = parse_siwe((await lab.start(Account.create(), "revoke")).json()["message"])["statement"]
    assert "on-chain grant remains until you remove it" in statement
    assert "any open position stays open" in statement
    assert SIWE_STATEMENT_RE.fullmatch(statement), statement


async def test_the_pause_statement_says_an_open_position_stays(lab):
    statement = parse_siwe((await lab.start(Account.create(), "pause")).json()["message"])["statement"]
    assert "Stop the halo-trade AI agent" in statement
    assert "open position stays open with its stop" in statement


@pytest.mark.parametrize("action", ["pause", "resume", "revoke"])
async def test_every_action_is_a_valid_message_naming_its_action(lab, action):
    message = (await lab.start(Account.create(), action)).json()["message"]
    assert parse_siwe(message)["Request ID"] == action


async def test_a_signature_for_another_domain_is_refused():
    lab = Lab(public_base_url="https://real.example")
    wallet = Account.create()
    message = (await lab.start(wallet)).json()["message"]
    forged = message.replace("real.example", "evil.example")
    assert forged != message
    response = await lab.client.post("/onboard/verify", json={"eth_address": wallet.address, "signature": sign(wallet, forged)})
    assert response.status_code == 401 and response.json()["code"] == "BAD_SIGNATURE"


# ---------------------------------------------------------------- 5. the per-client limit
def fresh_address(index: int) -> str:
    return "0x" + f"{index + 1:040x}"


async def start_from(client: httpx.AsyncClient, index: int, forwarded_for: Optional[str] = None) -> int:
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for else {}
    response = await client.post("/onboard/start", json={"eth_address": fresh_address(index)}, headers=headers)
    return response.status_code


def test_the_rate_limiter_forgets_idle_keys():
    limiter = RateLimiter(limit=30)
    for index in range(100_000):
        limiter.allow(f"2001:db8::{index:x}", START)
    assert len(limiter._hits) == 100_000
    # One key after every old window has passed: everything idle is dropped.
    limiter.allow("fresh", START + limiter.window_ms + 1)
    assert list(limiter._hits) == ["fresh"]


def test_a_key_inside_its_window_survives_a_sweep():
    limiter = RateLimiter(limit=2)
    assert limiter.allow("busy", START)
    limiter.allow("idle", START)
    assert limiter.allow("busy", START + limiter.window_ms - 1)
    assert not limiter.allow("busy", START + limiter.window_ms - 1)


async def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(lab):
    # Without HALO_TRUSTED_PROXY a client cannot shed its own limit by inventing addresses.
    for index in range(onboarding.START_PER_MIN):
        assert await start_from(lab.client, index, f"1.2.3.{index}") == 200
    assert await start_from(lab.client, 999, "9.9.9.9") == 429


async def test_a_trusted_proxy_passes_the_real_client_and_cannot_be_spoofed():
    lab = Lab(trusted_proxy="172.18.0.2,10.0.0.0/8")
    proxy = lab.client_from("172.18.0.2")
    for index in range(onboarding.START_PER_MIN):
        assert await start_from(proxy, index, "203.0.113.5") == 200
    assert await start_from(proxy, 900, "203.0.113.5") == 429
    # A forged left-most hop does not move the client to a fresh bucket: the right-most untrusted hop counts.
    assert await start_from(proxy, 901, "203.0.113.99, 203.0.113.5") == 429
    assert await start_from(proxy, 902, "198.51.100.7") == 200
    # Not from the proxy: the socket address counts and the header is ignored.
    direct = lab.client_from("198.18.0.1")
    for index in range(onboarding.START_PER_MIN):
        assert await start_from(direct, 1000 + index, f"203.0.113.{index % 200}") == 200
    assert await start_from(direct, 2000, "203.0.113.200") == 429


async def test_ipv6_clients_share_a_bucket_per_64(lab):
    first = lab.client_from("2001:db8:0:1::1")
    for index in range(onboarding.START_PER_MIN):
        assert await start_from(first, index) == 200
    assert await start_from(lab.client_from("2001:db8:0:1::2"), 900) == 429
    assert await start_from(lab.client_from("2001:db8:0:2::1"), 901) == 200


@pytest.mark.parametrize("value", ["*", "*,10.0.0.1", "0.0.0.0/0", "::/0", "not-an-ip"])
def test_startup_refuses_a_trust_everyone_or_broken_proxy_setting(value):
    with pytest.raises(ConfigError):
        parse_trusted_proxies(value)
    with pytest.raises(ConfigError):
        validate_settings(Settings(trusted_proxy=value))


def test_a_star_proxy_setting_is_refused_as_a_star_not_as_a_typo():
    with pytest.raises(ConfigError, match="never"):
        parse_trusted_proxies("*")


def test_startup_accepts_listed_proxies():
    assert len(parse_trusted_proxies("172.18.0.2, 10.0.0.0/8")) == 2
    assert parse_trusted_proxies("") == ()


def test_startup_refuses_uvicorns_trust_everyone_setting(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(ConfigError):
        validate_settings(Settings())


# ---------------------------------------------------------------- 6. handled in web/test/guard.test.mjs and the Playwright spec
async def test_config_gives_the_page_what_it_needs_to_compare_and_display(lab):
    body = (await lab.client.get("/onboard/config")).json()
    assert body["public_host"] == "halo.test"
    assert body["min_deposit_usd"] == "50"
    assert body["grantee"] and body["market"]["market_id"]


def test_every_grant_the_server_requires_is_in_the_pages_compiled_list():
    from halo_trade.chain.grants import REQUIRED_GRANTS

    tx_js = (REPO / "web" / "src" / "tx.js").read_text(encoding="utf-8")
    for spec in REQUIRED_GRANTS:
        assert f'"{spec.authz_type_url}"' in tx_js and f'"{spec.msg_type_url}"' in tx_js, spec


# ---------------------------------------------------------------- 7. URLs that reach the signed message or the LLM
@pytest.mark.parametrize(
    "url",
    ["http://plain-http.example", "http://example.com:8080", "ftp://halo.test", "https://x'; touch /tmp/pwn; echo '", "https://halo.test/path", 'https://halo.test"', "https://ha lo.test", "https://halo.test/"],
)
def test_startup_refuses_an_unsafe_base_url(url):
    with pytest.raises(ConfigError):
        validate_settings(Settings(public_base_url=url))
    with pytest.raises(ConfigError):
        Lab(public_base_url=url)


@pytest.mark.parametrize("url", ["https://halo.test", "https://halo.test:8443", "http://127.0.0.1:18080", "http://localhost:18080", "http://[::1]:18080"])
def test_startup_accepts_https_and_localhost(url):
    validate_settings(Settings(public_base_url=url))


@pytest.mark.parametrize(
    "url",
    ["http://plain-http.example/v1", "ftp://llm.test/v1", "https://llm.test/v1?key=abc", "https://user:pw@llm.test/v1", "https://llm.test/v1 ", "https://llm.test/v'1", "https://llm.test/v1#x"],
)
def test_startup_refuses_an_unsafe_llm_url(url):
    with pytest.raises(ConfigError, match="HALO_LLM_BASE_URL"):
        validate_settings(Settings(llm_base_url=url))


@pytest.mark.parametrize("url", ["https://openrouter.ai/api/v1", "https://llm.test", "https://llm.test:8443/v1/", "http://127.0.0.1:11434/v1", "http://localhost:8000/v1"])
def test_startup_accepts_an_https_or_local_llm_url(url):
    validate_settings(Settings(llm_base_url=url))


# ---------------------------------------------------------------- 8. dangerous config at startup
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"chain_backend": "mock", "allow_mainnet": True, "grantee_key_path": "/k", "public_base_url": "https://h.test"}, "backend"),
        ({"chain_backend": "injective", "allow_mainnet": True, "grantee_key_path": "/k", "public_base_url": "http://localhost:1"}, "https"),
        ({"chain_backend": "injective", "allow_mainnet": True, "grantee_key_path": "", "public_base_url": "https://h.test"}, "grantee"),
        ({"chain_backend": "injective", "allow_mainnet": False, "grantee_key_path": "/k", "public_base_url": "https://h.test"}, "allow"),
    ],
)
def test_mainnet_refused_unless_fully_configured(overrides, reason):
    with pytest.raises(ConfigError):
        validate_settings(Settings(network="mainnet", **overrides))


def test_mainnet_passes_when_fully_configured():
    validate_settings(Settings(network="mainnet", chain_backend="injective", allow_mainnet=True, grantee_key_path="/k", public_base_url="https://h.test"))


def test_create_app_refuses_the_mock_backend_on_mainnet():
    ctx = AppContext(
        settings=Settings(network="mainnet", allow_mainnet=True, chain_backend="mock", public_base_url="http://plain-http.example"),
        db=connect(":memory:"),
        chain=MockChain(auto_mine=True),
    )
    with pytest.raises(ConfigError):
        create_app(ctx)


def test_an_empty_webhook_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING, logger="halo_trade.startup"):
        validate_settings(Settings())
    assert "HALO_ALERT_WEBHOOK" in caplog.text


def test_an_unconfigured_llm_is_warned_about(caplog):
    with caplog.at_level(logging.WARNING, logger="halo_trade.startup"):
        validate_settings(Settings(llm_api_key="", llm_model=""))
    assert "the agent cannot be started" in caplog.text


def test_a_configured_llm_is_not_warned_about_and_its_key_is_not_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="halo_trade.startup"):
        validate_settings(Settings(llm_api_key="sk-secret-marker", llm_model="some/model"))
    assert "the agent cannot be started" not in caplog.text
    assert "sk-secret-marker" not in caplog.text


# ---------------------------------------------------------------- 9. dust slots
async def test_a_dust_deposit_cannot_take_a_slot(lab):
    wallet = Account.create()
    lab.fund(wallet, "0.000001")
    response = await lab.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": await lab.session(wallet)})
    assert response.status_code == 409 and response.json()["code"] == "BELOW_MIN_DEPOSIT"
    assert response.json()["limit"] == "MIN_DEPOSIT_USD=50"
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


async def complete_status(lab: Lab, wallet) -> httpx.Response:
    return await lab.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": await lab.session(wallet)})


async def test_a_started_wallet_that_withdrew_its_funds_stops_holding_a_slot():
    lab = Lab(max_users=1)
    first = Account.create()
    await lab.onboard(first, "75")
    await lab.start_agent(first)
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS + 1)
    second = Account.create()
    lab.fund(second, "100")
    blocked = await complete_status(lab, second)
    assert blocked.status_code == 409 and blocked.json()["code"] == "USERS_FULL"
    # The watcher's next snapshot shows the funds are gone (or down to dust).
    lab.set_equity(first, "0.000001")
    allowed = await complete_status(lab, second)
    assert allowed.status_code == 200, allowed.text


async def test_a_wallet_that_never_started_releases_its_slot_after_a_day():
    lab = Lab(max_users=1)
    idle = Account.create()
    lab.fund(idle)
    await lab.complete(idle)  # holds the slot, agent never started
    other = Account.create()
    lab.fund(other)
    assert (await complete_status(lab, other)).json()["code"] == "USERS_FULL"
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS + 1)
    assert (await complete_status(lab, other)).status_code == 200


async def test_completing_again_does_not_extend_the_24_hours():
    lab = Lab(max_users=1)
    idle = Account.create()
    lab.fund(idle)
    await lab.complete(idle)
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS - 1000)
    await lab.complete(idle)  # just before the deadline
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS + 1)
    other = Account.create()
    lab.fund(other)
    response = await complete_status(lab, other)
    assert response.status_code == 200, response.text


async def test_a_started_funded_wallet_keeps_its_slot_and_a_lapsed_one_must_prove_again():
    lab = Lab(max_users=1)
    funded = Account.create()
    await lab.onboard(funded, "100")
    await lab.start_agent(funded)
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS + 1)
    other = Account.create()
    lab.fund(other)
    assert (await complete_status(lab, other)).json()["code"] == "USERS_FULL"
    # Long after, a wallet that never started must show grant and deposit again.
    lab.set_equity(funded, "0")
    idle = Account.create()
    lab.fund(idle, "100")
    await lab.complete(idle)
    lab.clock.set(lab.clock() + onboarding.UNCLAIMED_SLOT_MS + 1)
    lab.chain.sub(onboarding.subaccount_id(idle.address.lower())).deposits = Decimal("0.5")
    again = await complete_status(lab, idle)
    assert again.status_code == 409 and again.json()["code"] == "BELOW_MIN_DEPOSIT"


async def test_resume_refuses_a_wallet_when_the_slots_are_taken():
    lab = Lab(max_users=1)
    first, second = Account.create(), Account.create()
    await lab.onboard(first, "100")
    lab.clock.set(START + onboarding.UNCLAIMED_SLOT_MS + 1)
    lab.fund(second, "100")
    assert (await complete_status(lab, second)).status_code == 200
    await lab.start_agent(second)
    challenge = (await lab.start(first, "resume")).json()
    late = await lab.client.post("/onboard/resume", json={"eth_address": first.address, "signature": sign(first, challenge["message"]), "preset": "trend"})
    assert late.status_code == 409 and late.json()["code"] == "USERS_FULL"
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0] == 1


async def test_a_wallet_holding_the_last_slot_can_start_itself():
    lab = Lab(max_users=1)
    wallet = Account.create()
    await lab.onboard(wallet, "100")
    await lab.start_agent(wallet)


async def test_two_resumes_racing_for_the_last_slot_have_one_winner():
    import asyncio

    lab = Lab(max_users=1)
    lab.ctx.llm = object()
    wallets = [Account.create(), Account.create()]
    for wallet in wallets:  # one at a time: each reservation runs out before the next is made
        await lab.onboard(wallet, "100")
        lab.clock.advance(onboarding.UNCLAIMED_SLOT_MS + 1)
    proofs = []
    for wallet in wallets:
        challenge = (await lab.start(wallet, "resume")).json()
        proofs.append({"eth_address": wallet.address, "signature": sign(wallet, challenge["message"]), "preset": "trend"})
    results = await asyncio.gather(*[lab.client.post("/onboard/resume", json=proof) for proof in proofs])
    assert sorted(r.status_code for r in results) == [200, 409]
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0] == 1


async def test_a_wallet_coming_back_after_a_revoke_is_not_started():
    lab = Lab(max_users=1)
    wallet = Account.create()
    await lab.onboard(wallet, "100")
    await lab.start_agent(wallet)
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 1
    challenge = (await lab.start(wallet, "revoke")).json()
    assert (await signed(lab, wallet, "revoke", challenge["message"])).status_code == 200
    assert lab.ctx.db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0


# ---------------------------------------------------------------- 10. status amplification
async def test_status_is_limited_per_session_and_cached_per_address(lab, monkeypatch):
    calls = {"account": 0, "check_grant": 0}
    original_account, original_grant = lab.chain.account, lab.chain.check_grant

    async def counting_account(*args, **kwargs):
        calls["account"] += 1
        return await original_account(*args, **kwargs)

    async def counting_grant(*args, **kwargs):
        calls["check_grant"] += 1
        return await original_grant(*args, **kwargs)

    monkeypatch.setattr(lab.chain, "account", counting_account)
    monkeypatch.setattr(lab.chain, "check_grant", counting_grant)
    wallet = Account.create()
    lab.fund(wallet)
    session = await lab.session(wallet)
    codes = []
    for _ in range(500):
        response = await status_of(lab, wallet, session)
        codes.append(response.status_code)
    assert codes.count(200) == onboarding.STATUS_PER_MIN
    assert codes.count(429) == 500 - onboarding.STATUS_PER_MIN
    assert calls == {"account": 1, "check_grant": 1}  # the rest were served from the cache
    lab.clock.set(START + 60_000 + 1)
    # A new rate-limit window and a new cache period: one more chain read.
    assert (await status_of(lab, wallet, session)).status_code == 200
    assert calls == {"account": 2, "check_grant": 2}


async def test_complete_never_uses_the_status_cache(lab):
    wallet = Account.create()
    lab.fund(wallet)
    session = await lab.session(wallet)
    assert (await status_of(lab, wallet, session)).status_code == 200  # cached: grant ok, 50 USDC
    lab.chain.set_grant(onboarding.subaccount_id(wallet.address.lower()), False)
    response = await lab.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": session})
    assert response.status_code == 403 and response.json()["code"] == "GRANT_MISSING"


# ---------------------------------------------------------------- 12. the image build
def test_dockerignore_patterns_match_at_any_depth():
    patterns = [line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    required = {"**/.git", "**/.env", "**/.env.*", "**/*.key", "**/*.pem", "**/*.sqlite*", "**/data", "**/node_modules", "**/dist", "**/__pycache__"}
    assert required <= set(patterns), required - set(patterns)
    for pattern in patterns:
        assert pattern.startswith("**/") or pattern.startswith("!"), f"{pattern} only matches at the root"


def test_the_runtime_image_gets_only_what_it_needs():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    stages = re.split(r"^FROM ", dockerfile, flags=re.M)
    final = stages[-1]
    assert "COPY . ." not in final  # the final image is the one that runs in production
    assert "COPY . ." in stages[-2]  # only the test image takes the whole context
    assert "--no-proxy-headers" in final


# ---------------------------------------------------------------- 14. the stats page's claim
def test_stats_copy_does_not_claim_there_are_no_account_details():
    html = (REPO / "web" / "src" / "stats.html").read_text(encoding="utf-8")
    assert "no account details" not in html
    assert "shows the wallet that traded" in html
    docstring = (REPO / "src" / "halo_trade" / "stats.py").read_text(encoding="utf-8").split('"""')[1]
    assert "show the wallet" in docstring


# ---------------------------------------------------------------- 16-20. input edges
async def test_a_trailing_newline_in_an_address_is_refused(lab):
    wallet = Account.create()
    response = await lab.client.post("/onboard/start", json={"eth_address": wallet.address + "\n"})
    assert response.status_code == 422


def test_the_format_checks_match_the_whole_value():
    # The field lengths would catch this too; the regexes must not depend on that.
    wallet = Account.create()
    assert onboarding._check_eth(wallet.address) == wallet.address.lower()
    with pytest.raises(ValueError):
        onboarding._check_eth(wallet.address + "\n")
    with pytest.raises(ValueError):
        onboarding.ProofIn._sig("0x" + "ab" * 65 + "\n")


async def test_a_trailing_newline_in_a_signature_is_refused(lab):
    wallet = Account.create()
    response = await lab.client.post("/onboard/verify", json={"eth_address": wallet.address, "signature": "0x" + "ab" * 65 + "\n"})
    assert response.status_code == 422


async def test_onboard_validation_errors_are_code_only(lab):
    response = await lab.client.post("/onboard/start", json={"eth_address": "<script>alert(1)</script>"})
    assert response.status_code == 422
    assert response.json() == {"ok": False, "code": "INVALID_REQUEST", "limit": None}
    assert "script" not in response.text


async def test_a_lone_surrogate_session_is_refused_without_an_error(lab):
    wallet = Account.create()
    response = await lab.client.post(
        "/onboard/status",
        content=b'{"eth_address":"' + wallet.address.encode() + b'","session":"abcdefghijklmnop\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and response.json()["code"] == "INVALID_REQUEST"


async def test_complete_and_verify_are_not_cacheable(lab):
    wallet = Account.create()
    lab.fund(wallet)
    challenge = (await lab.start(wallet)).json()
    verified = await lab.client.post("/onboard/verify", json={"eth_address": wallet.address, "signature": sign(wallet, challenge["message"])})
    assert verified.headers["cache-control"] == "no-store"
    done = await lab.client.post("/onboard/complete", json={"eth_address": wallet.address, "session": verified.json()["session"]})
    assert done.headers["cache-control"] == "no-store"


async def test_https_sites_send_hsts_and_local_sites_do_not(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(onboarding, "STATIC_DIR", static)
    https_lab = Lab(public_base_url="https://halo.test")
    assert "max-age" in (await https_lab.client.get("/")).headers["strict-transport-security"]
    local_lab = Lab(public_base_url="http://127.0.0.1:18080")
    assert "strict-transport-security" not in (await local_lab.client.get("/")).headers


# ---------------------------------------------------------------- what must still hold
async def test_proof_is_action_bound_and_single_use(lab):
    wallet = Account.create()
    await lab.onboard(wallet)
    challenge = (await lab.start(wallet, "pause")).json()
    signature = sign(wallet, challenge["message"])
    assert (await lab.client.post("/onboard/revoke", json={"eth_address": wallet.address, "signature": signature})).status_code == 401
    assert (await lab.client.post("/onboard/pause", json={"eth_address": wallet.address, "signature": signature})).status_code == 200
    assert (await lab.client.post("/onboard/pause", json={"eth_address": wallet.address, "signature": signature})).status_code == 401
