"""One client for any OpenAI-compatible chat endpoint, and the daily call budget.

Used by every lane of the decision engine: the decision and its research step, the chat assistant's
read-only tool loop, the market brief and reflection.
The key is sent only to `llm_base_url`, never logged, and redirects are not followed.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

import httpx

from .config import Settings
from .context import AppContext
from .txmachine import transaction

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
# The cap must cover hidden reasoning as well as the answer: a reasoning model given a small cap
# spends all of it thinking and returns 200 OK with empty content.
MAX_TOKENS = 4000
Lane = Literal["decision", "chat", "research", "reflect", "analyst"]
# `llm_usage` is keyed by (day, user, lane) and `user_id` cannot be null. A lane with no wallet books here.
SERVICE_USER_ID = 0


class LLMError(Exception):
    """The call produced nothing usable. `code` is safe to store and show; no provider text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model; the caller parses and validates it


@dataclass(frozen=True)
class LLMReply:
    content: Optional[str]
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class LLMClient:
    def __init__(self, settings: Settings, http: Optional[httpx.AsyncClient] = None) -> None:
        self._settings = settings
        self._owned = http is None
        self._http = http or httpx.AsyncClient(timeout=settings.llm_timeout_s, follow_redirects=False)

    async def aclose(self) -> None:
        if self._owned:
            await self._http.aclose()

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Optional[Sequence[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        json_mode: bool = False,
        max_tokens: int = MAX_TOKENS,
    ) -> LLMReply:
        """`json_mode` and `tools` are exclusive: combining them made providers return junk in production."""
        if json_mode and tools:
            raise ValueError("json_mode and tools cannot be combined")
        body: dict[str, Any] = {"model": self._settings.llm_model, "messages": list(messages), "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = list(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        started = time.monotonic()
        try:
            response = await self._http.post(
                self._settings.llm_base_url.rstrip("/") + "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise LLMError("LLM_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise LLMError("LLM_TRANSPORT") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 429:
            raise LLMError("LLM_RATE_LIMITED")
        if response.status_code == 402:
            raise LLMError("LLM_BILLING")
        if response.status_code != 200:
            logger.warning("llm http status %s", response.status_code)
            raise LLMError("LLM_HTTP_ERROR")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM_BAD_RESPONSE") from exc
        usage = payload.get("usage") or {}
        calls = tuple(
            ToolCall(str(c.get("id", "")), str(c["function"]["name"]), str(c["function"].get("arguments") or ""))
            for c in (message.get("tool_calls") or [])
            if isinstance(c, dict) and isinstance(c.get("function"), dict) and c["function"].get("name")
        )
        content = message.get("content")
        if choice.get("finish_reason") == "length" and not calls and not (isinstance(content, str) and content.strip()):
            raise LLMError("LLM_TRUNCATED")
        return LLMReply(
            content=content if isinstance(content, str) else None,
            tool_calls=calls,
            model=str(payload.get("model") or self._settings.llm_model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )


def make_client(settings: Settings) -> Optional[LLMClient]:
    return LLMClient(settings) if settings.llm_configured else None


def _day(now_ms: int) -> int:
    return now_ms // DAY_MS


def _lane_budget_left(conn: Any, settings: Settings, day: int, user_id: int, lane: Lane) -> bool:
    """Each lane has its own pools, so no other lane can spend a decision's budget."""
    total = conn.execute("SELECT COALESCE(SUM(calls),0) AS n FROM llm_usage WHERE day_id=? AND lane=?", (day, lane)).fetchone()["n"]
    mine = conn.execute(
        "SELECT calls, input_tokens + output_tokens AS tokens FROM llm_usage WHERE day_id=? AND user_id=? AND lane=?", (day, user_id, lane)
    ).fetchone()
    calls, tokens = (mine["calls"], mine["tokens"]) if mine else (0, 0)
    if lane == "decision":
        return total < settings.llm_daily_calls and calls < settings.llm_user_daily_calls
    if lane == "research":
        return total < settings.research_daily_calls and calls < settings.research_user_daily_calls
    if lane == "reflect":
        return total < settings.reflect_daily_calls and calls < settings.reflect_user_daily_calls
    if lane == "analyst":
        return total < settings.analyst_daily_calls
    return (
        total < settings.chat_global_daily_calls
        and calls < settings.chat_daily_limit * settings.chat_max_steps
        and tokens < settings.chat_user_daily_tokens
    )


async def take_call(ctx: AppContext, user_id: int, lane: Lane) -> bool:
    """Reserve one LLM call for this UTC day. False means a daily cap is reached: make no call."""
    day = _day(ctx.clock())
    async with ctx.db_lock:
        with transaction(ctx.db) as conn:
            if not _lane_budget_left(conn, ctx.settings, day, user_id, lane):
                return False
            conn.execute(
                "INSERT INTO llm_usage(day_id,user_id,lane,calls) VALUES(?,?,?,1) "
                "ON CONFLICT(day_id,user_id,lane) DO UPDATE SET calls=calls+1",
                (day, user_id, lane),
            )
    return True


async def record_tokens(ctx: AppContext, user_id: int, lane: Lane, reply: LLMReply) -> None:
    day = _day(ctx.clock())
    async with ctx.db_lock:
        with transaction(ctx.db) as conn:
            conn.execute(
                "UPDATE llm_usage SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE day_id=? AND user_id=? AND lane=?",
                (reply.input_tokens, reply.output_tokens, day, user_id, lane),
            )


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("duplicate key")
    return data


def _reject_constant(name: str) -> None:
    raise ValueError(name)


def strict_json(content: Optional[str]) -> Any:
    """One JSON value exactly as sent, or None: no duplicate keys, no NaN, no repair of any kind."""
    if content is None:
        return None
    try:
        return json.loads(content, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None


def dumps(value: Any) -> str:
    """Compact, stable JSON for prompts."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
