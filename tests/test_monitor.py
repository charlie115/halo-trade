"""The price watcher: its loop survives failures, it has no way to order, and its settings are checked at startup."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from decimal import Decimal as D

import pytest

from halo_trade import db as db_module, monitor as monitor_module
from halo_trade.config import Settings
from halo_trade.monitor import Monitor
from halo_trade.startup import ConfigError, validate_settings


DAY = 86_400_000


# ---------------------------------------------------------------- when the monitor cannot read


async def test_the_loop_survives_any_failure_and_stops_when_told(env):
    monitor = Monitor(env.svc)
    env.ctx.settings = replace(env.ctx.settings, monitor_interval_s=0)
    calls = 0

    class Broken:
        async def read(self):
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")

    monitor.source = Broken()
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run(stop))
    while calls < 3:
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_the_app_runs_the_monitor(env):
    from halo_trade.api import create_app

    app = create_app(env.ctx)
    calls = 0
    original = app.state.monitor.tick

    async def tick():
        nonlocal calls
        calls += 1
        return await original()

    app.state.monitor.tick = tick
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert calls >= 1


# ---------------------------------------------------------------- a wake never orders anything


def test_the_monitor_has_no_way_to_order():
    source = inspect.getsource(monitor_module)
    for word in (".submit", ".preview", ".close(", "build_and_sign", ".broadcast", "tx.send", "llm"):
        assert word not in source, word


# ---------------------------------------------------------------- settings
@pytest.mark.parametrize(
    "overrides",
    [
        {"watch_concurrency": 0}, {"watch_concurrency": 201}, {"watch_flat_interval_s": -1}, {"watch_flat_interval_s": 601},
        {"chain_rps": 0}, {"indexer_rps": 0}, {"decision_concurrency": 0}, {"decision_concurrency": 101},
        {"decision_max_per_tick": 0}, {"monitor_interval_s": 0}, {"monitor_interval_s": 61}, {"wake_cooldown_s": 29},
        {"max_skip_s": -1}, {"wake_move_atr": D(0)}, {"wake_move_atr": D(21)}, {"wake_stop_atr": D(0)}, {"wake_stop_atr": D(21)},
        {"skip_move_atr": D("-0.1")}, {"skip_move_atr": D("1.0")},
    ],
    ids=lambda overrides: next(f"{key}={value}" for key, value in overrides.items()),
)
def test_startup_refuses_settings_out_of_range(overrides):
    with pytest.raises(ConfigError):
        validate_settings(Settings(**overrides))


def test_the_defaults_are_the_agreed_ones():
    settings = Settings()
    assert (settings.watch_concurrency, settings.watch_interval_s, settings.watch_flat_interval_s) == (20, 5, 60)
    assert (settings.decision_concurrency, settings.decision_max_per_tick) == (10, 30)
    assert (settings.chain_rps, settings.indexer_rps) == (15, 30)
    assert (settings.monitor_interval_s, settings.wake_move_atr, settings.wake_stop_atr, settings.wake_cooldown_s) == (3, D("1.0"), D("0.5"), 300)
    assert (settings.skip_move_atr, settings.max_skip_s) == (D("0.25"), 3600)
    validate_settings(settings)


# ---------------------------------------------------------------- an older database
def test_a_database_from_before_these_columns_gains_them(tmp_path):
    path = str(tmp_path / "old.sqlite")
    conn = db_module.connect(path)
    conn.execute("ALTER TABLE agent_decisions DROP COLUMN trigger")
    for column in ("last_mark", "wake_reason", "last_wake_ms"):
        conn.execute(f"ALTER TABLE strategies DROP COLUMN {column}")
    conn.close()
    conn = db_module.connect(path)
    assert "trigger" in {row["name"] for row in conn.execute("PRAGMA table_info(agent_decisions)")}
    assert {"last_mark", "wake_reason", "last_wake_ms"} <= {row["name"] for row in conn.execute("PRAGMA table_info(strategies)")}
    conn.close()
    db_module.connect(path).close()  # and running it again changes nothing
