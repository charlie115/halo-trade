#!/usr/bin/env python3
"""Register the Telegram webhook from HALO_* settings. Prints only `ok` or an error code.

Run it where the package and the environment are, for example in the server container:

    docker compose -p halo-trade -f compose.yaml exec -T server python - < scripts/telegram_setup.py

It sends the URL, the secret token and the two update types the bot uses. It never prints the
token or the secret, and exits non-zero on anything but `ok`.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import httpx

from halo_trade.config import Settings

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 15.0
ALLOWED_UPDATES = ["message", "callback_query"]


async def register(settings: Settings, *, http: Optional[httpx.AsyncClient] = None) -> str:
    """`ok`, or a code saying what went wrong. Never Telegram's own text or an exception message."""
    if not settings.telegram_configured:
        return "NOT_CONFIGURED"
    base_url = settings.public_base_url.rstrip("/")
    if not base_url.startswith("https://"):
        return "BASE_URL_NOT_HTTPS"
    client = http or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)
    try:
        response = await client.post(
            f"{API_BASE}/bot{settings.telegram_bot_token}/setWebhook",
            json={
                "url": f"{base_url}/telegram/webhook",
                "secret_token": settings.telegram_webhook_secret,
                "allowed_updates": ALLOWED_UPDATES,
                "drop_pending_updates": True,
            },
        )
    except httpx.TimeoutException:
        return "TELEGRAM_TIMEOUT"
    except Exception:
        return "TELEGRAM_UNREACHABLE"
    finally:
        if http is None:
            await client.aclose()
    if response.status_code != 200:
        return f"TELEGRAM_HTTP_{response.status_code}"
    try:
        accepted = response.json().get("ok") is True
    except (ValueError, AttributeError):
        return "TELEGRAM_BAD_RESPONSE"
    return "ok" if accepted else "TELEGRAM_REJECTED"


def main() -> int:
    logging.disable(logging.CRITICAL)  # httpx logs request URLs, and this one holds the token
    code = asyncio.run(register(Settings()))
    print(code)
    return 0 if code == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
