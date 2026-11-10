#!/usr/bin/env python3
"""Store the hosted agent's secrets in .env. Typed values are hidden and never printed.

Press Enter at any prompt to keep what is already set.
"""
import getpass
import os
import re
import secrets
import stat
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
TELEGRAM_TOKEN = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,}")
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")


def read() -> list[str]:
    return ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []


def current(lines: list[str], key: str) -> str:
    return next((line.split("=", 1)[1] for line in lines if line.startswith(key + "=")), "")


def put(lines: list[str], key: str, value: str) -> list[str]:
    return [line for line in lines if not line.startswith(key + "=")] + [f"{key}={value}"]


def write(lines: list[str]) -> None:
    temp = ENV.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temp, ENV)


def state(lines: list[str], key: str) -> str:
    return "set" if current(lines, key) else "not set"


def main() -> None:
    lines = read()

    key = getpass.getpass(f"OpenRouter API key [{state(lines, 'HALO_LLM_API_KEY')}] (hidden): ").strip()
    if key:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", key):
            raise SystemExit("That does not look like an API key. Nothing was saved.")
        lines = put(lines, "HALO_LLM_API_KEY", key)

    token = getpass.getpass(f"Telegram bot token [{state(lines, 'HALO_TELEGRAM_BOT_TOKEN')}] (hidden, Enter to skip): ").strip()
    if token:
        if not TELEGRAM_TOKEN.fullmatch(token):
            raise SystemExit("That does not look like a Telegram bot token. Nothing was saved.")
        lines = put(lines, "HALO_TELEGRAM_BOT_TOKEN", token)
        name = input("Telegram bot username, without @: ").strip().lstrip("@")
        if not USERNAME.fullmatch(name):
            raise SystemExit("That does not look like a bot username. Nothing was saved.")
        lines = put(lines, "HALO_TELEGRAM_BOT_USERNAME", name)
    if current(lines, "HALO_TELEGRAM_BOT_TOKEN") and not current(lines, "HALO_TELEGRAM_WEBHOOK_SECRET"):
        lines = put(lines, "HALO_TELEGRAM_WEBHOOK_SECRET", secrets.token_urlsafe(32))

    write(lines)
    for name in ("HALO_LLM_API_KEY", "HALO_TELEGRAM_BOT_TOKEN", "HALO_TELEGRAM_BOT_USERNAME", "HALO_TELEGRAM_WEBHOOK_SECRET"):
        print(f"{name}: {state(lines, name)}")
    print("SECRETS_SAVED")


if __name__ == "__main__":
    main()
