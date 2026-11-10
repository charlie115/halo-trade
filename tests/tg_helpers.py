"""Shared fakes for the Telegram and assistant tests: no real Telegram, no real LLM."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence
from urllib.parse import parse_qs, urlparse

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI

from halo_trade import onboarding, telegram
from halo_trade.llm import LLMReply, ToolCall
from halo_trade.onboarding import eth_to_inj, subaccount_id
from halo_trade.service import User
from tests.conftest import Env, api_client

TOKEN = "123456:BOT-TOKEN-MUST-NOT-LEAK"
SECRET = "webhook-secret-must-not-leak-0123456789"
BOT = "halo_test_bot"
SECRET_HEADER = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
SESSION = "session-secret-of-the-wallet-owner"


class FakeTelegram:
    """Records every Bot API call. `respond` builds the next response."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.urls: list[str] = []
        self.respond: Callable[[httpx.Request], httpx.Response] = lambda _request: httpx.Response(200, json={"ok": True, "result": {}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content or b"{}")))
        return self.respond(request)

    def sends(self, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        return [body for method, body in self.calls if method == "sendMessage" and (chat_id is None or body["chat_id"] == str(chat_id))]

    def texts(self, chat_id: Optional[int] = None) -> list[str]:
        return [body["text"] for body in self.sends(chat_id)]

    def button(self, chat_id: int, prefix: str) -> str:
        """The callback data of the last inline button with this prefix sent to this chat."""
        for body in reversed(self.sends(chat_id)):
            if "reply_markup" in body:
                data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                if data.startswith(prefix):
                    return data
        raise AssertionError(f"no {prefix} button in chat {chat_id}")


class FakeLLM:
    def __init__(self, script: Sequence[LLMReply] = ()) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.default = LLMReply(content="Here is your answer.")

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Optional[Sequence[dict[str, Any]]] = None,
        json_mode: bool = False,
        max_tokens: int = 2000,
    ) -> LLMReply:
        self.calls.append({"messages": copy.deepcopy(list(messages)), "tools": tools, "json_mode": json_mode, "max_tokens": max_tokens})
        return self.script.pop(0) if self.script else self.default


def tool_reply(*calls: tuple[str, str]) -> LLMReply:
    """An LLM step that asks for tools: (name, raw JSON arguments) per call."""
    return LLMReply(content=None, tool_calls=tuple(ToolCall(f"call{i}", name, args) for i, (name, args) in enumerate(calls)))


def message(update_id: int, chat_id: int, text: Optional[str], chat_type: str = "private", date: Optional[int] = None) -> dict[str, Any]:
    body: dict[str, Any] = {"message_id": update_id, "chat": {"id": chat_id, "type": chat_type}, "from": {"id": chat_id, "is_bot": False}}
    if text is not None:
        body["text"] = text
    if date is not None:
        body["date"] = date
    return {"update_id": update_id, "message": body}


def button(update_id: int, chat_id: int, data: str, chat_type: str = "private", sender: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": chat_id, "is_bot": False, **(sender or {})},
            "message": {"message_id": 1, "chat": {"id": chat_id, "type": chat_type}},
            "data": data,
        },
    }


@dataclass
class Bot:
    env: Env
    tg: FakeTelegram
    llm: FakeLLM
    worker: telegram.TelegramWorker
    client: httpx.AsyncClient
    http: httpx.AsyncClient
    next_update: int = field(default=1000)
    seeds: int = 0

    def link(self, user: User, chat_id: int) -> None:
        self.env.ctx.db.execute("INSERT INTO telegram_links(user_id,chat_id,linked_ms) VALUES(?,?,?)", (user.id, str(chat_id), self.env.clock()))

    def add_wallet(self, *, status: str = "active", deposits: str = "100") -> tuple[User, Any]:
        """A user whose address comes from a real key, so the signed routes accept it."""
        self.seeds += 1
        wallet = Account.from_key("0x" + f"{self.seeds:02x}" * 32)
        eth = wallet.address.lower()
        inj, sub = eth_to_inj(eth), subaccount_id(eth)
        self.env.chain.register_account(inj, sub, Decimal(deposits), granted=True)
        cursor = self.env.ctx.db.execute(
            "INSERT INTO users (inj_address, eth_address, subaccount_id, status, token_hash, created_ms) VALUES (?,?,?,?,?,?)",
            (inj, eth, sub, status, "pending:" + eth, self.env.clock()),
        )
        user = User(int(cursor.lastrowid), inj, sub, status)
        self.env.users[user.id] = user
        return user, wallet

    def open_session(self, wallet: Any, session: str = SESSION) -> None:
        self.env.ctx.db.execute(
            "INSERT OR REPLACE INTO onboard_sessions(inj_address,session_hash,expires_ms) VALUES(?,?,?)",
            (eth_to_inj(wallet.address.lower()), hashlib.sha256(session.encode()).hexdigest(), self.env.clock() + 1_800_000),
        )

    async def signature(self, wallet: Any, action: str) -> str:
        started = await self.client.post("/onboard/start", json={"eth_address": wallet.address.lower(), "action": action})
        assert started.status_code == 200, started.text
        return "0x" + wallet.sign_message(encode_defunct(text=started.json()["message"])).signature.hex().removeprefix("0x")

    async def request_link(self, wallet: Any) -> httpx.Response:
        signature = await self.signature(wallet, "telegram")
        return await self.client.post("/onboard/telegram/link", json={"eth_address": wallet.address.lower(), "signature": signature})

    async def link_code(self, wallet: Any) -> str:
        response = await self.request_link(wallet)
        assert response.status_code == 200, response.text
        url = urlparse(response.json()["url"])
        assert (url.netloc, url.path) == ("t.me", f"/{BOT}")
        return parse_qs(url.query)["start"][0]

    async def connect(self, wallet: Any, chat_id: int, sender: Optional[dict[str, Any]] = None) -> None:
        """The whole happy path: signature, /start, the Connect button."""
        await self.ask(chat_id, f"/start {await self.link_code(wallet)}")
        await self.press(chat_id, self.tg.button(chat_id, telegram.CONNECT_PREFIX), sender=sender)
        await self.worker.flush()

    def _update_id(self) -> int:
        self.next_update += 1
        return self.next_update

    async def post(self, update: dict[str, Any], headers: Optional[dict[str, str]] = None) -> httpx.Response:
        return await self.client.post("/telegram/webhook", json=update, headers=SECRET_HEADER if headers is None else headers)

    async def say(self, chat_id: int, text: Optional[str], chat_type: str = "private") -> httpx.Response:
        return await self.post(message(self._update_id(), chat_id, text, chat_type))

    async def press(self, chat_id: int, data: str, chat_type: str = "private", sender: Optional[dict[str, Any]] = None) -> httpx.Response:
        return await self.post(button(self._update_id(), chat_id, data, chat_type, sender))

    async def ask(self, chat_id: int, text: Optional[str]) -> str:
        """Say something and return the last text sent to that chat ('' if nothing was sent)."""
        await self.say(chat_id, text)
        await self.worker.flush()
        texts = self.tg.texts(chat_id)
        return texts[-1] if texts else ""

    def outbox(self, user: Optional[User] = None) -> list[Any]:
        sql, params = "SELECT * FROM telegram_outbox", ()
        if user is not None:
            sql, params = sql + " WHERE user_id=?", (user.id,)
        return self.env.ctx.db.execute(sql + " ORDER BY id", params).fetchall()

    def status_of(self, user: User) -> str:
        return self.env.ctx.db.execute("SELECT status FROM users WHERE id=?", (user.id,)).fetchone()["status"]

    def links(self, user: User) -> list[str]:
        return [row["chat_id"] for row in self.env.ctx.db.execute("SELECT chat_id FROM telegram_links WHERE user_id=?", (user.id,))]


def make_bot(env: Env, **settings_overrides: Any) -> Bot:
    env.ctx.settings = dataclasses.replace(
        env.ctx.settings,
        telegram_bot_token=TOKEN,
        telegram_bot_username=BOT,
        telegram_webhook_secret=SECRET,
        **settings_overrides,
    )
    tg, llm = FakeTelegram(), FakeLLM()
    env.ctx.llm = llm
    http = httpx.AsyncClient(transport=httpx.MockTransport(tg.handle), follow_redirects=False)
    worker = telegram.TelegramWorker(env.svc, http=http)
    app = FastAPI()
    app.state.ctx, app.state.service = env.ctx, env.svc
    app.include_router(telegram.router)
    app.include_router(onboarding.router)
    return Bot(env, tg, llm, worker, api_client(app), http)
