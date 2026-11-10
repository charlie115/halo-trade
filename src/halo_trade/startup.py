"""Checks that refuse a dangerous configuration before the service starts.

Called once by `api.py` before any chain client or database exists. A refusal is a `ConfigError`
naming the setting, so a bad deployment fails at boot instead of serving it.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from decimal import Decimal
from typing import Union
from urllib.parse import urlsplit

from .config import Settings
from .limits import OBSERVATION_MAX_AGE_MS

logger = logging.getLogger(__name__)

# The public base URL goes into the message a wallet signs, so it may not hold a quote, a semicolon,
# a space or anything else with a meaning of its own. Scheme, host and optional port.
_BASE_URL_RE = re.compile(r"(https?)://(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?")
# The same rule for the LLM endpoint, plus a plain path: OpenAI-compatible URLs end in one (/api/v1).
_LLM_URL_RE = re.compile(_BASE_URL_RE.pattern + r"(/[A-Za-z0-9._~/-]*)?")
MIN_WEBHOOK_SECRET_CHARS = 32
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
MAX_LEVERAGE_LIMIT = Decimal("5")
MAX_RISK_PER_TRADE = Decimal("0.1")
MAX_DAILY_LOSS = Decimal("0.25")
MAX_DRAWDOWN = Decimal("0.5")
MIN_DECISION_INTERVAL_S = 60
MAX_WAKE_ATR = Decimal("20")
MAX_LANE_DAILY_CALLS = 1_000_000

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class ConfigError(RuntimeError):
    """The configuration is unsafe to run."""


def _check_url(setting: str, url: str, pattern: re.Pattern[str], shape: str) -> None:
    match = pattern.fullmatch(url)
    if match is None:
        raise ConfigError(f"{setting} must be {shape} with no special characters")
    scheme, host = match.group(1), match.group(2).lower()
    if scheme != "https" and host not in LOCAL_HOSTS:
        raise ConfigError(f"{setting} must use https unless the host is localhost")


def check_public_base_url(url: str) -> None:
    """Scheme, host and port only, https unless the host is this machine."""
    _check_url("HALO_PUBLIC_BASE_URL", url, _BASE_URL_RE, "scheme://host[:port]")


def check_llm_base_url(url: str) -> None:
    """The API key is sent here. The public base URL's rule, with a plain path allowed."""
    _check_url("HALO_LLM_BASE_URL", url, _LLM_URL_RE, "scheme://host[:port][/path]")


def parse_trusted_proxies(value: str) -> tuple[Network, ...]:
    """`HALO_TRUSTED_PROXY`: comma-separated IPs or CIDRs of the reverse proxy in front of us."""
    if "*" in value:
        raise ConfigError("HALO_TRUSTED_PROXY must list proxy addresses, never *")
    networks: list[Network] = []
    for entry in (part.strip() for part in value.split(",")):
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ConfigError("HALO_TRUSTED_PROXY has an invalid entry") from exc
        if network.prefixlen == 0:
            raise ConfigError("HALO_TRUSTED_PROXY must not trust every address")
        networks.append(network)
    return tuple(networks)


def _check_limits(settings: Settings) -> None:
    risk = settings.risk
    for name, value, ceiling in (
        ("HALO_MAX_LEVERAGE", risk.max_leverage, MAX_LEVERAGE_LIMIT),
        ("HALO_RISK_PER_TRADE", risk.risk_per_trade, MAX_RISK_PER_TRADE),
        ("HALO_DAILY_LOSS", risk.daily_loss, MAX_DAILY_LOSS),
        ("HALO_MAX_DRAWDOWN", risk.max_drawdown, MAX_DRAWDOWN),
    ):
        if not 0 < value <= ceiling:
            raise ConfigError(f"{name} must be above 0 and at most {ceiling}")
    if not 0 < settings.llm_timeout_s * 2 * 1000 < OBSERVATION_MAX_AGE_MS:
        raise ConfigError("HALO_LLM_TIMEOUT_S: two tries must fit inside the observation lifetime")
    if settings.decision_interval_s < MIN_DECISION_INTERVAL_S:
        raise ConfigError(f"HALO_DECISION_INTERVAL_S must be at least {MIN_DECISION_INTERVAL_S}")
    for name, value, low, high in (
        ("HALO_WATCH_CONCURRENCY", settings.watch_concurrency, 1, 200),
        ("HALO_WATCH_FLAT_INTERVAL_S", settings.watch_flat_interval_s, 0, 600),
        ("HALO_CHAIN_RPS", settings.chain_rps, 1, 1000),
        ("HALO_INDEXER_RPS", settings.indexer_rps, 1, 1000),
        ("HALO_DECISION_CONCURRENCY", settings.decision_concurrency, 1, 100),
        ("HALO_DECISION_MAX_PER_TICK", settings.decision_max_per_tick, 1, 1000),
        ("HALO_MONITOR_INTERVAL_S", settings.monitor_interval_s, 1, 60),
        ("HALO_WAKE_COOLDOWN_S", settings.wake_cooldown_s, 30, 86400),
        ("HALO_MAX_SKIP_S", settings.max_skip_s, 0, 86400),
        ("HALO_RESEARCH_DAILY_CALLS", settings.research_daily_calls, 0, MAX_LANE_DAILY_CALLS),
        ("HALO_RESEARCH_USER_DAILY_CALLS", settings.research_user_daily_calls, 0, MAX_LANE_DAILY_CALLS),
        ("HALO_REFLECT_DAILY_CALLS", settings.reflect_daily_calls, 0, MAX_LANE_DAILY_CALLS),
        ("HALO_REFLECT_USER_DAILY_CALLS", settings.reflect_user_daily_calls, 0, MAX_LANE_DAILY_CALLS),
        ("HALO_ANALYST_DAILY_CALLS", settings.analyst_daily_calls, 0, MAX_LANE_DAILY_CALLS),
    ):
        if not low <= value <= high:
            raise ConfigError(f"{name} must be from {low} to {high}")
    if not 0 < settings.wake_move_atr <= MAX_WAKE_ATR:
        raise ConfigError(f"HALO_WAKE_MOVE_ATR must be above 0 and at most {MAX_WAKE_ATR}")
    if not 0 < settings.wake_stop_atr <= MAX_WAKE_ATR:
        raise ConfigError(f"HALO_WAKE_STOP_ATR must be above 0 and at most {MAX_WAKE_ATR}")
    if not 0 <= settings.skip_move_atr < settings.wake_move_atr:
        raise ConfigError("HALO_SKIP_MOVE_ATR must be at least 0 and below HALO_WAKE_MOVE_ATR")


def validate_settings(settings: Settings) -> None:
    """Refuses to start on a dangerous setup. Warnings are for what is merely worth noticing."""
    check_public_base_url(settings.public_base_url)
    check_llm_base_url(settings.llm_base_url)
    parse_trusted_proxies(settings.trusted_proxy)
    _check_limits(settings)
    if os.environ.get("HALO_DESK_ENABLED", "1") not in ("0", "1"):
        raise ConfigError("HALO_DESK_ENABLED must be 0 or 1")
    if "*" in os.environ.get("FORWARDED_ALLOW_IPS", ""):
        raise ConfigError("FORWARDED_ALLOW_IPS=* would let any client forge its address; set HALO_TRUSTED_PROXY instead")
    if settings.network == "mainnet":
        if settings.chain_backend != "injective":
            raise ConfigError("mainnet needs HALO_CHAIN_BACKEND=injective")
        if not settings.public_base_url.startswith("https://"):
            raise ConfigError("mainnet needs an https HALO_PUBLIC_BASE_URL")
        if not settings.grantee_key_path:
            raise ConfigError("mainnet needs HALO_GRANTEE_KEY_PATH")
        if not settings.allow_mainnet:
            raise ConfigError("mainnet needs HALO_ALLOW_MAINNET")
    if settings.telegram_bot_token and len(settings.telegram_webhook_secret) < MIN_WEBHOOK_SECRET_CHARS:
        raise ConfigError(f"HALO_TELEGRAM_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_CHARS} characters when a bot token is set")
    if not settings.alert_webhook:
        logger.warning("HALO_ALERT_WEBHOOK is empty: alerts are stored but nobody is notified")
    if not settings.llm_configured:
        logger.warning("HALO_LLM_API_KEY or HALO_LLM_MODEL is empty: the agent cannot be started")
    elif settings.desk_enabled and settings.analyst_daily_calls == 0:
        logger.warning("HALO_ANALYST_DAILY_CALLS is 0: the desk runs without a market brief")


def site_host(public_base_url: str) -> str:
    """`host[:port]` of the base URL: the domain the wallet shows and checks."""
    return urlsplit(public_base_url).netloc
