"""What the service does to the public nodes' per-IP limits: token buckets, 429s, shared reads."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal as D

import grpc
import httpx
import pytest

from halo_trade.chain.base import Candle, ChainThrottled, ChainUnavailable, SignedTx, requests_are_urgent, urgent_requests
from halo_trade.chain.injective import rate_refusal
from halo_trade.chain.limiter import MAX_BACKOFF_S, RequestLimiter
from halo_trade.chain.mock import MockChain, MockClock
from halo_trade.chain.shared import CANDLE_RETRY_MS, HOUR_MS, QUARTER_MS, SNAPSHOT_TTL_MS, SharedReads

from tests.test_injective_unit import FakeClient, make


NOW = 2_000_000 * 900_000


# ---------------------------------------------------------------- the bucket
async def test_the_bucket_holds_requests_to_its_rate():
    limiter = RequestLimiter(100)
    started = time.monotonic()
    for _ in range(int(limiter.burst) + 10):
        await limiter.acquire(urgent=False, timeout_s=2)
    assert time.monotonic() - started >= 0.09  # the burst is free, the other 10 cost 10 ms each


def test_the_burst_keeps_one_second_inside_the_documented_limit():
    assert RequestLimiter(15).burst == 5  # 15 per second plus a burst of 5 is the 20 the nodes allow


async def drain(limiter: RequestLimiter) -> None:
    while limiter._tokens >= 1:
        await limiter.acquire(urgent=False, timeout_s=1)


async def test_urgent_requests_go_ahead_of_every_waiting_read():
    limiter = RequestLimiter(50)
    await drain(limiter)
    order: list[str] = []

    async def want(name: str, urgent: bool) -> None:
        await limiter.acquire(urgent=urgent, timeout_s=5)
        order.append(name)

    reads = [asyncio.create_task(want(f"read{i}", False)) for i in range(4)]
    await asyncio.sleep(0)
    urgent = [asyncio.create_task(want(f"urgent{i}", True)) for i in range(2)]
    await asyncio.gather(*reads, *urgent)
    assert order[:2] == ["urgent0", "urgent1"]
    assert order[2:] == ["read0", "read1", "read2", "read3"]


async def test_a_read_does_not_jump_the_queue_when_a_token_appears():
    limiter = RequestLimiter(50)
    await drain(limiter)
    order: list[str] = []

    async def want(name: str, urgent: bool) -> None:
        await limiter.acquire(urgent=urgent, timeout_s=5)
        order.append(name)

    first = asyncio.create_task(want("urgent", True))
    for _ in range(3):
        await asyncio.sleep(0)  # the urgent request is queued and the pump is asleep
    limiter._tokens = 1.0  # a token is free, but an urgent request is already waiting for it
    second = asyncio.create_task(want("read", False))
    await asyncio.gather(first, second)
    assert order == ["urgent", "read"]


async def test_waiting_respects_the_deadline():
    limiter = RequestLimiter(1)
    await drain(limiter)
    started = time.monotonic()
    with pytest.raises(ChainUnavailable):
        await limiter.acquire(urgent=True, timeout_s=0.05)
    assert time.monotonic() - started < 0.5
    assert not limiter._urgent


async def test_an_outer_deadline_cancels_the_wait_and_spends_no_token():
    limiter = RequestLimiter(1)
    await drain(limiter)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(limiter.acquire(urgent=False, timeout_s=30), 0.05)
    assert not limiter._reads
    limiter._tokens = 1.0
    await limiter.acquire(urgent=False, timeout_s=0.1)  # the token is still there for the next caller


async def test_a_refusal_pauses_urgent_requests_too_for_as_long_as_the_node_said():
    limiter = RequestLimiter(1000)
    limiter.refused(0.1)
    assert limiter.throttled == 1
    started = time.monotonic()
    await limiter.acquire(urgent=True, timeout_s=2)
    assert 0.09 <= time.monotonic() - started < 0.5  # what the node said, not the default second


async def test_a_refusal_without_retry_after_backs_off_and_doubles():
    limiter = RequestLimiter(1000)
    limiter.refused(None)
    first = limiter._paused_until - time.monotonic()
    limiter.refused(None)
    second = limiter._paused_until - time.monotonic()
    assert 0.9 < first <= 1.0
    assert 1.9 < second <= 2.0


def test_a_success_resets_the_doubling():
    limiter = RequestLimiter(1000)
    limiter.refused(None)
    limiter.refused(None)
    limiter.succeeded()
    limiter._paused_until = 0.0
    limiter.refused(None)
    assert limiter._paused_until - time.monotonic() <= 1.0


def test_an_absurd_retry_after_is_capped():
    limiter = RequestLimiter(1000)
    limiter.refused(10_000)
    assert limiter._paused_until - time.monotonic() <= MAX_BACKOFF_S


# ---------------------------------------------------------------- the urgent flag
async def test_the_urgent_flag_reaches_child_tasks_and_ends_with_the_block():
    assert requests_are_urgent() is False

    async def child() -> bool:
        return requests_are_urgent()

    with urgent_requests():
        assert await asyncio.gather(child(), child()) == [True, True]
    assert requests_are_urgent() is False


def test_close_and_stop_paths_are_marked_urgent(env):
    import inspect

    from halo_trade.service import Service

    for name in ("advance", "close", "drive_close_request", "force_close", "flatten_orphan"):
        assert "urgent_requests()" in inspect.getsource(getattr(Service, name)), name


# ---------------------------------------------------------------- the Injective backend
class _Refused(grpc.aio.AioRpcError):
    def __init__(self, code=grpc.StatusCode.RESOURCE_EXHAUSTED, details="too many", retry_after=None):
        trailing = grpc.aio.Metadata(("retry-after", retry_after)) if retry_after is not None else grpc.aio.Metadata()
        super().__init__(code, grpc.aio.Metadata(), trailing, details=details)


def test_a_rate_refusal_is_recognised_from_both_transports():
    assert rate_refusal(_Refused(retry_after="7")).retry_after_s == 7
    assert isinstance(rate_refusal(_Refused(code=grpc.StatusCode.UNAVAILABLE, details="Received http2 header with status: 429")), ChainThrottled)
    assert rate_refusal(_Refused(code=grpc.StatusCode.UNAVAILABLE, details="connection reset")) is None
    response = httpx.Response(429, headers={"retry-after": "3"}, request=httpx.Request("GET", "https://x.test"))
    assert rate_refusal(httpx.HTTPStatusError("x", request=response.request, response=response)).retry_after_s == 3


def test_a_refusal_hidden_behind_a_wrapper_is_still_seen():
    try:
        try:
            raise _Refused(retry_after="2")
        except _Refused as exc:
            raise ChainUnavailable("authz grants query failed") from exc
    except ChainUnavailable as wrapped:
        assert rate_refusal(wrapped).retry_after_s == 2


async def test_a_refused_chain_read_raises_and_is_counted_never_empty():
    client = FakeClient()

    async def refused(subaccount_id, denom):
        raise _Refused(retry_after="0")

    client.fetch_subaccount_deposit = refused
    backend = make(client)
    with pytest.raises(ChainThrottled):
        await backend.account("inj1x", "0x" + "ab" * 32)
    assert backend.throttled_total == 1


async def test_a_refused_chart_read_raises_and_pauses_only_the_indexer_bucket(monkeypatch):
    class Refusing:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params):
            return httpx.Response(429, headers={"retry-after": "0"}, request=httpx.Request("GET", url))

    monkeypatch.setattr("halo_trade.chain.injective.httpx.AsyncClient", Refusing)
    backend = make()
    with pytest.raises(ChainThrottled):
        await backend.candles_1h(5)
    assert backend._indexer_limiter.throttled == 1 and backend._chain_limiter.throttled == 0


async def test_one_account_read_is_three_requests_with_the_market_shared():
    client = FakeClient()
    market_reads = 0

    def counted(original):
        async def read(*args, **kwargs):
            nonlocal market_reads
            market_reads += 1
            return await original(*args, **kwargs)

        return read

    client.fetch_chain_derivative_markets = counted(client.fetch_chain_derivative_markets)
    client.fetch_chain_derivative_market = counted(client.fetch_chain_derivative_market)
    client.response_heights = {"deposit": 10, "position": 10, "conditional": 10}  # as a real node answers
    backend = make(client)
    await backend.market()
    before = market_reads
    spent: list[bool] = []
    real_acquire = backend._chain_limiter.acquire

    async def acquire(*, urgent, timeout_s):
        spent.append(urgent)
        await real_acquire(urgent=urgent, timeout_s=timeout_s)

    backend._chain_limiter.acquire = acquire
    await backend.account("inj1x", "0x" + "ab" * 32)
    await backend.account("inj1y", "0x" + "cd" * 32)
    assert market_reads == before + 1  # one shared read for both wallets
    assert len(spent) == 7  # 3 per wallet plus the shared market read
    assert sum(spent) == 1  # only the shared read is urgent, because a close may be waiting on it


def tx() -> SignedTx:
    return SignedTx("HASH", b"x", "inj1signer", 1, 2000)


async def test_lookup_and_broadcast_are_urgent_without_being_asked():
    backend = make()
    seen: list[bool] = []
    real_acquire = backend._chain_limiter.acquire

    async def acquire(*, urgent, timeout_s):
        seen.append(urgent)
        await real_acquire(urgent=urgent, timeout_s=timeout_s)

    backend._chain_limiter.acquire = acquire
    await backend.lookup(tx())
    await backend.broadcast(tx())
    assert seen and all(seen)


async def test_a_plain_read_is_not_urgent_and_inside_a_close_path_it_is():
    backend = make()
    seen: list[bool] = []
    real_acquire = backend._chain_limiter.acquire

    async def acquire(*, urgent, timeout_s):
        seen.append(urgent)
        await real_acquire(urgent=urgent, timeout_s=timeout_s)

    backend._chain_limiter.acquire = acquire
    await backend.grantee_balance_inj()
    with urgent_requests():
        await backend.grantee_balance_inj()
    assert seen == [False, True]


async def test_a_slot_that_never_comes_is_unavailable_and_the_call_is_not_made():
    client = FakeClient()
    backend = make(client)
    backend._rpc_timeout_s = 0.05
    backend._chain_limiter.refused(30)
    started = time.monotonic()
    with pytest.raises(ChainUnavailable):
        await backend.grantee_balance_inj()
    assert time.monotonic() - started < 1


def test_the_factory_passes_the_rates_through(tmp_path):
    from tests.test_injective_unit import settings, write_key

    from halo_trade.chain.factory import make_chain

    backend = make_chain(settings(grantee_key_path=write_key(tmp_path), chain_rps=7, indexer_rps=11))
    assert (backend._chain_limiter.rate, backend._indexer_limiter.rate) == (7, 11)


# ---------------------------------------------------------------- shared market reads
class Counting(MockChain):
    def __init__(self, clock):
        super().__init__(clock=clock, auto_mine=True)
        self.reads = {"snapshot": 0, "account": 0, "candles_1h": 0, "candles_15m": 0}
        self.limits: list[int] = []
        self.delay = 0.0

    async def snapshot(self):
        self.reads["snapshot"] += 1
        await asyncio.sleep(self.delay)
        return await super().snapshot()

    async def account(self, granter, subaccount_id):
        self.reads["account"] += 1
        return await super().account(granter, subaccount_id)

    async def candles_1h(self, limit):
        self.reads["candles_1h"] += 1
        self.limits.append(limit)
        await asyncio.sleep(self.delay)
        return await super().candles_1h(limit)

    async def candles_15m(self, limit):
        self.reads["candles_15m"] += 1
        return await super().candles_15m(limit)


@pytest.fixture
def shared():
    clock = MockClock(NOW + 60_000)
    inner = Counting(clock)
    return clock, inner, SharedReads(inner, clock_ms=clock)


async def test_concurrent_snapshot_reads_are_one_request(shared):
    _, inner, reads = shared
    inner.delay = 0.01
    results = await asyncio.gather(*[reads.snapshot() for _ in range(50)])
    assert inner.reads["snapshot"] == 1
    assert len({id(result) for result in results}) == 1


async def test_a_snapshot_is_reused_for_its_ttl_and_then_read_again(shared):
    clock, inner, reads = shared
    await reads.snapshot()
    clock.advance(SNAPSHOT_TTL_MS - 1)
    await reads.snapshot()
    assert inner.reads["snapshot"] == 1
    clock.advance(1)
    await reads.snapshot()
    assert inner.reads["snapshot"] == 2


def test_the_snapshot_ttl_is_well_inside_the_market_age_limit():
    from halo_trade.limits import MARKET_MAX_AGE_MS

    assert SNAPSHOT_TTL_MS <= 2000 < MARKET_MAX_AGE_MS / 10


async def test_a_failed_snapshot_reaches_every_waiter_and_is_not_kept(shared):
    _, inner, reads = shared
    inner.delay = 0.01
    inner.go_down()
    results = await asyncio.gather(*[reads.snapshot() for _ in range(5)], return_exceptions=True)
    assert all(isinstance(result, ChainUnavailable) for result in results)
    assert inner.reads["snapshot"] == 1
    inner.come_back()
    assert (await reads.snapshot()).mark_price > 0
    assert inner.reads["snapshot"] == 2


async def test_one_waiters_deadline_does_not_cancel_the_shared_read(shared):
    _, inner, reads = shared
    inner.delay = 0.05
    impatient = asyncio.create_task(asyncio.wait_for(reads.snapshot(), 0.001))
    patient = asyncio.create_task(reads.snapshot())
    with pytest.raises(TimeoutError):
        await impatient
    assert (await patient).mark_price > 0
    assert inner.reads["snapshot"] == 1


async def test_candles_are_kept_until_a_new_one_can_exist(shared):
    clock, inner, reads = shared
    await reads.candles_1h(80)
    clock.advance(HOUR_MS - 61_000)
    await reads.candles_1h(80)
    assert inner.reads["candles_1h"] == 1
    clock.advance(1_000)  # the next hour has started
    await reads.candles_1h(80)
    assert inner.reads["candles_1h"] == 2


async def test_15_minute_candles_turn_over_on_their_own_bar(shared):
    clock, inner, reads = shared
    await reads.candles_15m(80)
    clock.advance(QUARTER_MS - 61_000)
    await reads.candles_15m(80)
    assert inner.reads["candles_15m"] == 1
    clock.advance(1_000)
    await reads.candles_15m(80)
    assert inner.reads["candles_15m"] == 2


async def test_a_bar_whose_candle_has_not_appeared_is_asked_for_again(shared):
    clock, inner, reads = shared
    bar_start = (clock() - clock() % HOUR_MS)
    inner.set_candles([Candle(bar_start - 2 * HOUR_MS, D(1), D(1), D(1), D(1))])  # the last closed hour is missing
    await reads.candles_1h(80)
    await reads.candles_1h(80)
    assert inner.reads["candles_1h"] == 1
    clock.advance(CANDLE_RETRY_MS)
    await reads.candles_1h(80)
    assert inner.reads["candles_1h"] == 2


async def test_a_longer_request_is_not_served_from_a_shorter_read(shared):
    _, inner, reads = shared
    await reads.candles_1h(10)
    await reads.candles_1h(5)
    assert inner.reads["candles_1h"] == 1
    assert len(await reads.candles_1h(5)) == 5
    await reads.candles_1h(20)
    assert inner.reads["candles_1h"] == 2


async def test_a_new_bar_is_one_read_of_the_longest_list_any_caller_wanted(shared):
    clock, inner, reads = shared
    assert len(await reads.candles_1h(1320)) == 1320
    assert len(await reads.candles_1h(80)) == 80  # the shorter asks come from the cache
    assert len(await reads.candles_1h(240)) == 240
    assert inner.reads["candles_1h"] == 1
    clock.advance(HOUR_MS)
    assert len(await reads.candles_1h(80)) == 80  # whoever asks first in the new bar
    assert inner.limits == [1320, 1320]
    assert len(await reads.candles_1h(1320)) == 1320  # and the longer asks then cost nothing
    assert inner.reads["candles_1h"] == 2


async def test_an_hour_of_cycles_for_many_wallets_is_one_chart_request(shared):
    clock, inner, reads = shared
    await reads.candles_1h(1320)
    clock.advance(HOUR_MS)  # a new bar, with everyone's cycles in it
    for _ in range(40):
        for limit in (80, 240, 1320, 80):
            await reads.candles_1h(limit)
        clock.advance(1_000)
    assert inner.reads["candles_1h"] == 2  # the first hour's read, and this hour's one


async def test_a_caller_that_joins_a_shorter_read_still_gets_all_it_asked_for(shared):
    _, inner, reads = shared
    inner.delay = 0.05

    async def later():
        await asyncio.sleep(0.01)  # the short read is already out by then
        return await reads.candles_1h(1320)

    short, long = await asyncio.gather(reads.candles_1h(80), later())
    assert len(short) == 80 and len(long) == 1320
    assert inner.limits == [80, 1320]


async def test_a_caller_that_arrives_before_the_read_starts_widens_it(shared):
    _, inner, reads = shared
    inner.delay = 0.01
    short, long = await asyncio.gather(reads.candles_1h(80), reads.candles_1h(1320))
    assert len(short) == 80 and len(long) == 1320
    assert inner.limits == [1320]


async def test_concurrent_candle_reads_are_one_request(shared):
    _, inner, reads = shared
    inner.delay = 0.01
    await asyncio.gather(*[reads.candles_1h(80) for _ in range(20)])
    assert inner.reads["candles_1h"] == 1


async def test_a_failed_candle_read_is_not_kept(shared):
    _, inner, reads = shared
    inner.fail_calls("candles_1h", count=1)
    with pytest.raises(ChainUnavailable):
        await reads.candles_1h(10)
    assert len(await reads.candles_1h(10)) == 10


async def test_account_reads_are_never_shared(shared):
    _, inner, reads = shared
    inner.register_account("inj1a", "0xa", D("100"))
    first = await reads.account("inj1a", "0xa")
    inner.deposit("0xa", D("7"))
    second = await reads.account("inj1a", "0xa")
    assert inner.reads["account"] == 2
    assert second.equity == first.equity + 7


async def test_everything_else_passes_through_to_the_backend(shared):
    _, inner, reads = shared
    assert reads.signer == inner.signer
    assert (await reads.market()).ticker == inner.market_info.ticker


def test_the_live_app_shares_market_reads():
    import inspect

    from halo_trade import api

    assert "SharedReads(make_chain(settings))" in inspect.getsource(api._context_from_env)


# ---------------------------------------------------------------- the widest read must not stretch a short one
async def test_a_thin_market_does_not_reach_back_through_the_longest_read():
    from decimal import Decimal as D

    from halo_trade.chain.base import Candle
    from halo_trade.chain.shared import SharedCandles

    hour = 3_600_000
    now = 2_000 * hour + 1_000
    bar = lambda hours_ago: Candle(now - now % hour - hours_ago * hour, D(1), D(2), D(1), D(1))
    # Five bars in the last week, the rest weeks old.
    rows = sorted([bar(ago) for ago in (1, 2, 3, 4, 5)] + [bar(ago) for ago in range(400, 475)], key=lambda candle: candle.open_ms)

    async def read(limit: int) -> list[Candle]:
        return rows[-limit:]

    shared = SharedCandles(read, hour, lambda: now)
    await shared.get(1320)
    assert len(await shared.get(80)) == 5  # what a read of 80 bars would have found
    assert len(await shared.get(1320)) == len(rows)
