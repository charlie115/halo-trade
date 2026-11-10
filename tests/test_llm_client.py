"""The real `LLMClient` over `httpx.MockTransport`: redirects, transport errors and timeouts. Nothing touches a network."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from halo_trade.llm import LLMClient, LLMError


KEY = "sk-secret-key-marker"


def client(env, handler) -> LLMClient:
    """Built by the real constructor, so its own redirect setting is the one tested."""
    env.ctx.settings = replace(env.ctx.settings, llm_api_key=KEY, llm_model="m", llm_base_url="https://llm.test/v1")
    llm = LLMClient(env.ctx.settings)
    llm._http._transport = httpx.MockTransport(handler)
    env.ctx.llm = llm
    return llm


async def test_a_redirect_is_not_followed_so_the_key_cannot_go_elsewhere(env):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    llm = client(env, handler)
    with pytest.raises(LLMError) as caught:
        await llm.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "LLM_HTTP_ERROR"
    assert seen == ["https://llm.test/v1/chat/completions"]


async def test_a_transport_error_has_its_own_code(env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    llm = client(env, handler)
    with pytest.raises(LLMError) as caught:
        await llm.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "LLM_TRANSPORT"


async def test_a_timeout_has_its_own_code(env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    llm = client(env, handler)
    with pytest.raises(LLMError) as caught:
        await llm.chat([{"role": "user", "content": "x"}])
    assert caught.value.code == "LLM_TIMEOUT"
